"""MQTT 客户端 + 数据模拟器。均基于 QThread，通过 Qt 信号与 GUI 通信。"""
from __future__ import annotations

import math
import os
import random
import time
import uuid
from typing import Any, Optional

import paho.mqtt.client as mqtt
from PySide6.QtCore import QThread, Signal

from db import SENSOR_IMPEDANCE_WINDOW_S
from maturity import spectrum_prediction
from protocol import (
    LineFramer,
    SensorData,
    ImpedanceData,
    ImpedanceSpectrum,
    SpectrumPoint,
    VALID_MSG_TYPES,
    MIN_REPORT_POINTS,
    parse_sensor,
    parse_impedance,
    parse_status,
    parse_heartbeat,
    parse_prediction,
    parse_topic,
    parse_sweep_points,
    parse_impedance_raw,
    parse_spectrum,
    sweep_expected_points,
    SweepPointData,
)
from spectrum import SpectrumAssembler, to_sweep_points, sweep_points_to_spectrum


# --------------------------------------------------------------------------- #
#  MQTT 真实连接
# --------------------------------------------------------------------------- #
def resolve_msg_type(topic_type: str, payload: Optional[dict]) -> str:
    """决定按哪种消息类型分发。

    主题里的类型优先，但线上固件存在把 ``sweep`` 整段发到
    ``/sensor`` 主题的历史版本，此时退回到报文内声明的 ``type``，
    避免一整段扫频数据被当成标量传感器读帧丢弃。
    """
    if isinstance(payload, dict):
        declared = payload.get("type")
        if declared in VALID_MSG_TYPES:
            return declared
    return topic_type


def impedance_scan_id(round_id: Optional[int], timestamp: int) -> Optional[str]:
    """生成全局唯一的扫描轮标识。

    固件的 roundId 从 millis() 派生、PC 模拟器的轮次号是进程内自增，两者
    重启都归零。只用轮次号的话，两次运行会产出同一个 SCAN_5，不同轮次的
    点被混进同一个 scan_id，行数也就不再能当"这轮到底收齐了几个点"来校验。
    拼上入库时已经 rebase 成墙钟的秒，跨重启、跨进程都不会撞。
    """
    if not round_id:
        return None
    return f"SCAN_{int(round_id)}_{int(timestamp)}"


def impedance_rows_from_points(
    points: list,
    *,
    gateway_id: str,
    node_id: str,
    timestamp: int,
    scan_id: Optional[str],
    band_lo_hz: float,
    band_hi_hz: float,
) -> list[ImpedanceData]:
    """一段扫频点 → 单频点阻抗行。

    分段到达和整谱收齐两条路径共用，保证 in_valid_window、scan_id、
    环境量口径完全一致，不会出现"界面一套数、入库另一套数"。
    """
    rows: list[ImpedanceData] = []
    for point in points:
        freq = getattr(point, "frequency_hz", None)
        if freq is None:
            continue
        try:
            freq = float(freq)
        except (TypeError, ValueError):
            continue
        rows.append(
            ImpedanceData(
                gateway_id=gateway_id,
                node_id=node_id,
                timestamp=timestamp,
                scan_id=scan_id,
                frequency_hz=freq,
                z_real=getattr(point, "z_real", None),
                z_imag=getattr(point, "z_imag", None),
                magnitude=getattr(point, "magnitude", None),
                phase=getattr(point, "phase_deg", None),
                in_valid_window=band_lo_hz <= freq <= band_hi_hz,
            )
        )
    return rows


def band_impedance_from_points(
    points: list,
    band_lo_hz: float,
    band_hi_hz: float,
    gateway_id: Optional[str] = None,
    node_id: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> Optional[ImpedanceData]:
    """一轮扫频点在分析频段内的平均阻抗，补进环境行的阻抗列。

    环境表一轮只有一行，频谱却有几十个频点，直接挑一个点当整轮阻抗会
    受落点位置影响；取有效频段内的均值，和成熟度算法用的是同一份口径
    （band_lo_hz/band_hi_hz），两个界面上的数才能对上。

    frequency_hz 留空：均值不对应任何单一频率，硬填一个只会误导。
    频段内一个点都没有就返回 None，不拿空值去覆盖环境表。
    """
    reals: list[float] = []
    imags: list[float] = []
    mags: list[float] = []
    phases: list[float] = []
    for point in points:
        freq = getattr(point, "frequency_hz", None)
        try:
            freq = float(freq)
        except (TypeError, ValueError):
            continue
        if not (band_lo_hz <= freq <= band_hi_hz):
            continue
        z_real = getattr(point, "z_real", None)
        z_imag = getattr(point, "z_imag", None)
        real = imag = None
        try:
            real = float(z_real) if z_real is not None else None
            imag = float(z_imag) if z_imag is not None else None
        except (TypeError, ValueError):
            real = imag = None
        if real is not None and imag is not None:
            reals.append(real)
            imags.append(imag)
        mag = getattr(point, "magnitude", None)
        try:
            mag = float(mag) if mag is not None else None
        except (TypeError, ValueError):
            mag = None
        if mag is None and real is not None and imag is not None:
            mag = math.hypot(real, imag)
        if mag is not None:
            mags.append(mag)
        # sweep 点只报 re/im，没有相位；按项目统一的约定从复数部分推出来，
        # 不推的话环境行里相位一列又是空的。
        # 两个属性名都认：sweep 点用 phase_deg，阻抗分帧用 phase。
        phase = getattr(point, "phase_deg", None)
        if phase is None:
            phase = getattr(point, "phase", None)
        if phase is None and real is not None and imag is not None:
            phase = -math.degrees(math.atan2(imag, real))
        else:
            try:
                phase = float(phase)
            except (TypeError, ValueError):
                phase = None
        if phase is not None:
            phases.append(phase)
    if not mags and not reals:
        return None
    z_real = sum(reals) / len(reals) if reals else None
    z_imag = sum(imags) / len(imags) if imags else None
    magnitude = sum(mags) / len(mags) if mags else None
    phase = sum(phases) / len(phases) if phases else None
    if magnitude is None and z_real is not None and z_imag is not None:
        magnitude = math.hypot(z_real, z_imag)
    return ImpedanceData(
        gateway_id=gateway_id or "",
        node_id=node_id or "",
        timestamp=int(timestamp or 0),
        frequency_hz=None,
        z_real=z_real,
        z_imag=z_imag,
        magnitude=magnitude,
        phase=phase,
    )


def sweep_to_impedance_rows(
    spectrum: ImpedanceSpectrum,
    band_lo_hz: float = 1000.0,
    band_hi_hz: float = 30000.0,
    scan_id: Optional[str] = None,
) -> list[ImpedanceData]:
    """整谱 → 单频点行，写入 impedance_data 供 analysis.py 复用。

    ``in_valid_window`` 按配置的分析频段判定。真机节点只测 900~12 kHz，
    频段写死成 30 kHz 会让标记失去意义。

    ``scan_id`` 有值时优先用它：整谱对象的时间戳是轮内最晚一段的，和分段
    写入时用的轮次锚点不一致的话，一轮会被拆成两个 scan。
    """
    return impedance_rows_from_points(
        spectrum.points,
        gateway_id=spectrum.gateway_id,
        node_id=spectrum.node_id,
        timestamp=spectrum.timestamp,
        scan_id=scan_id or impedance_scan_id(spectrum.scan_id, spectrum.timestamp),
        band_lo_hz=band_lo_hz,
        band_hi_hz=band_hi_hz,
    )


class DeviceClock:
    """设备时间戳 → 墙上时钟的折算器。

    ESP32 网关上报的是 ``millis()/1000``（开机秒数），直接落库会让历史查询
    和过期判定全部错位，必须折成墙钟。

    但不能简单替换成 ``time.time()``：一轮 100 个点全挤进同一秒，网关重启
    后的新轮次又会和前一轮撞上同一个秒，``scan_id`` 就分不清轮次、
    ``SCAN_9_x`` 的行数也不再能当"这轮到底收齐几个点"来校验。

    这里按设备时钟的相对偏移折算，并把结果钳制成单调向前：
    既保住点与点之间的真实间隔，又不会因为设备重启回拨而落到过去。
    """

    def __init__(self) -> None:
        self._dev_last: Optional[float] = None
        self._wall_last: int = 0

    def to_wallclock(self, raw: object) -> int:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value >= 1e8:
            return int(value)
        now = int(time.time())
        if self._dev_last is None:
            wall = now
        elif value < self._dev_last:
            # 设备时钟回拨（网关重启、millis 溢出）：重新锚定到当前时刻。
            wall = max(now, self._wall_last + 1)
        elif value == self._dev_last:
            # 同一时间戳的重复帧（QoS1 重投）必须映射到同一个墙钟秒，
            # 否则 (网关, 节点, scan_id, 频率) 去重键失效，同一轮会被写两遍。
            wall = self._wall_last
        else:
            # 设备时钟单调前进：按它的真实间隔推进，至少 +1 秒保证不撞秒。
            wall = max(self._wall_last + int(round(value - self._dev_last)),
                       self._wall_last + 1)
        self._dev_last = value
        self._wall_last = wall
        return wall


def ensure_wallclock_ts(payload: dict, clock: Optional[DeviceClock] = None) -> dict:
    """把设备侧的时间戳归一化成墙上时钟（保持相对间隔、不回退）。"""
    wall = (clock or DeviceClock()).to_wallclock(
        payload.get("timestamp", payload.get("ts", 0)))
    payload["timestamp"] = wall
    payload["ts"] = wall
    return payload


class RoundBuffer:
    """按轮次缓存分段扫频点，并判断这一轮是不是收齐了。

    和具体 worker 解耦：MQTTWorker 和 SimulatorWorker 各自持一个实例，
    完整性判定只有一份实现，不会出现"入库一套规则、出图另一套规则"。
    """

    def __init__(self, min_points: int, total_points: int = 100,
                 segment_points: int = 50) -> None:
        self.min_points = max(1, int(min_points))
        # 一轮的总点数 / 单段点数，对应固件里的 TOTAL_POINTS / SEG_POINTS。
        self.total_points = max(1, int(total_points))
        self.segment_points = max(1, int(segment_points))
        self._by_idx: dict[tuple[str, str, int], dict[int, object]] = {}
        self._segs: dict[tuple[str, str, int], dict[int, int]] = {}
        self._expected: dict[tuple[str, str, int], int] = {}
        self._last: dict[tuple[str, str, int], float] = {}
        # 一轮最多花多久。同编号轮次间隔超过这个量再出现，就当是重启后的新轮次。
        self.round_span_s = max(120.0, float(total_points) * 5.0)
        self._anchors: dict[tuple[str, str, int], int] = {}

    @staticmethod
    def key_of(points: list) -> tuple[str, str, int]:
        return (points[0].gateway_id, points[0].node_id, points[0].round_id)

    def feed(self, points: list, seg: Optional[int] = None,
             seg_total: Optional[int] = None,
             expected_points: Optional[int] = None) -> bool:
        """写入一个报文的点。同一 point_index 只保留一个版本。"""
        key = self.key_of(points)
        by_idx = self._by_idx.setdefault(key, {})
        ts = int(getattr(points[0], "timestamp", 0) or 0)
        prev = self._anchors.get(key)
        if prev is None:
            # 锚定在首个报文的时间戳上。后续报文的时间戳会随 rebase 逐次
            # 前移，同一轮的不同段不能各自当锚，否则一轮会被拆成多个 scan。
            self._anchors[key] = ts
        elif ts > prev + self.round_span_s:
            # 固件重启后 roundId 重新从 1 开始数：同编号的轮次必须换锚点，
            # 否则两次运行的点会混进同一个 scan_id。
            # 一轮内部的正常间隔远小于 round_span_s，不会误判成新一轮。
            self._anchors[key] = ts
        for point in points:
            by_idx[point.point_index] = point
        self._last[key] = time.time()
        if seg is not None:
            self._segs.setdefault(key, {})[int(seg)] = int(seg_total or 0)
        if expected_points is not None:
            self._expected[key] = max(self._expected.get(key, 0), int(expected_points))
        return self.complete(key)

    def points_count(self, key: tuple[str, str, int]) -> int:
        return len(self._by_idx.get(key) or {})

    def expected_points(self, key: tuple[str, str, int]) -> int:
        return int(self._expected.get(key, self.total_points))

    def complete(self, key: tuple[str, str, int]) -> bool:
        """整轮收齐判定，按声明信息的强弱分三级：

        1. 报文声明了 ``seg_total``：段收齐即完整
        2. 报文只给了 ``seg`` 没给 ``seg_total``（ESP32-S3 网关固件就是
           这种情况，只报 ``seg``/``point_start``）：按一轮总点数判，
           50 点的第一段不能当成整轮，否则会出两条半弧谱
        3. 什么都没声明：达到单报文下限就算，剩下靠静默窗口兜底，
           保证至少能出一张图，不会把整轮卡死在缓存里
        """
        have = self.points_count(key)
        if have < self.min_points:
            return False
        segs = self._segs.get(key) or {}
        seg_total = max(segs.values()) if segs else 0
        if seg_total >= 1:
            return len(segs) >= seg_total
        if segs and self.expected_points(key) > self.min_points:
            return have >= self.expected_points(key)
        return True

    def is_idle(self, key: tuple[str, str, int], idle_s: float,
                 now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return now - self._last.get(key, 0.0) >= idle_s

    def anchor(self, key: tuple[str, str, int]) -> int:
        """该轮首个报文的时间戳，分段和整轮出谱共用同一个 scan_id 锚点。"""
        return int(self._anchors.get(key, 0) or 0)

    def drain(self, key: tuple[str, str, int]) -> list:
        # 注意：_anchors 在这里不清。整轮出谱后要拿锚点算 scan_id，
        # 而且迟到的重复段（QoS1 重投）也得和原来落在同一个 scan 上。
        # 锚点按 (网关, 节点, 轮次号) 索引，同轮次号重启时会被新锚覆盖，
        # 字典规模受 clear() 控制，不会无限涨。
        by_idx = self._by_idx.pop(key, {})
        self._segs.pop(key, None)
        self._expected.pop(key, None)
        self._last.pop(key, None)
        return [by_idx[i] for i in sorted(by_idx)]

    def keys(self) -> list[tuple[str, str, int]]:
        return list(self._by_idx)

    def has_key(self, key: tuple[str, str, int]) -> bool:
        return key in self._by_idx

    def clear(self) -> None:
        self._by_idx.clear()
        self._segs.clear()
        self._expected.clear()
        self._last.clear()
        self._anchors.clear()


class MQTTWorker(QThread):
    """订阅 fruit/+/+/+ 主题，解析后落库并发信号。"""

    data_received = Signal(dict)
    status_changed = Signal(str, str, str)
    gateway_info = Signal(str, str, str, float)
    connected = Signal(bool)
    error_occurred = Signal(str)

    def __init__(self, config: dict, db) -> None:
        super().__init__()
        self._config = config
        self._db = db
        self._running = False
        self._client: Optional[mqtt.Client] = None
        self._msg_count = 0
        self._last_msg_time = 0.0
        self._recording = True

        # 分帧器 / 组装器 / 成熟度标定都可以从 config.json 调，
        # 换硬件后不必改代码。
        self._framer = LineFramer(strict_json=False)
        asm_cfg = self._config.get("assembler", {})
        self._assembler = SpectrumAssembler(
            scan_timeout_s=float(asm_cfg.get("scan_timeout_s", 180.0)),
            max_scans=int(asm_cfg.get("max_scans", 32)),
        )
        mat_cfg = self._config.get("maturity", {})
        self._band_lo_hz = float(mat_cfg.get("band_lo_hz", 1000.0))
        self._band_hi_hz = float(mat_cfg.get("band_hi_hz", 30000.0))
        self._magnitude_high = float(mat_cfg.get("magnitude_high", 1900.0))
        self._magnitude_low = float(mat_cfg.get("magnitude_low", 1050.0))
        self._spread_ratio_ref = float(mat_cfg.get("spread_ratio_ref", 0.29))

        # 单个报文至少要多少个频点才算"完整上报"，少于此数视为不完整报文。
        # 轮次攒够同样多频点才出谱，避免半条弧干扰 Nyquist 图。
        swp_cfg = self._config.get("sweep", {})
        self._min_sweep_points = max(
            MIN_REPORT_POINTS, int(swp_cfg.get("min_points", MIN_REPORT_POINTS)))
        self._round_idle_s = float(swp_cfg.get("round_idle_s", 3.0))
        # 一轮总点数 / 单段点数，对应固件 TOTAL_POINTS / SEG_POINTS。
        # 网关固件只报 seg 不报 seg_total，整轮收齐只能靠点数判，这是分母。
        self._round_total_points = max(1, int(swp_cfg.get("total_points", 100)))
        self._segment_points = max(1, int(swp_cfg.get("segment_points", 50)))
        self._round_buf = RoundBuffer(
            self._min_sweep_points,
            total_points=self._round_total_points,
            segment_points=self._segment_points,
        )
        # 心跳过期窗口：网关每 30s 发一次，超时即认为链路中断。
        self._heartbeat_stale_s = float(
            self._config.get("heartbeat", {}).get("stale_after_s", 45.0))
        self._last_context: dict[str, float] = {}
        # 这一轮的环境数据已经发过。固件每个频点都带同一份环境量，
        # 一轮 2 段报文 + QoS1 重投不能让环境卡片刷出多条一样的数据。
        self._env_emitted: set[tuple[str, str, int]] = set()
        # 网关上报的是开机秒数，折墙钟要按相对偏移，不能一刀切替换成 now。
        self._clock = DeviceClock()
        # type=impedance 的逐频点分帧按扫描段攒着，段结束才挂环境行。
        # 键是 (网关, 节点, scan_id)，没有 scan_id 的报文退回按秒分段。
        self._imp_scan_buf: dict[tuple[str, str, str], list[ImpedanceData]] = {}
        # 上一段扫描的结束时刻：这一段覆盖的时间从它算起。
        self._last_imp_scan_ts: dict[tuple[str, str], int] = {}

    # ---- 生命周期 ----

    def run(self) -> None:
        self._running = True
        mqtt_cfg = self._config.get("mqtt", {})
        host = mqtt_cfg.get("host", "127.0.0.1")
        port = int(mqtt_cfg.get("port", 1883))
        client_id = mqtt_cfg.get("client_id") or self._gen_client_id()
        keepalive = int(mqtt_cfg.get("keepalive", 60))
        qos = int(mqtt_cfg.get("qos", 1))
        topic_filter = mqtt_cfg.get("topic_filter", "fruit/+/+/+")
        connect_timeout_s = float(mqtt_cfg.get("connect_timeout_s", 8.0))

        try:
            try:
                self._client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
            except AttributeError:
                self._client = mqtt.Client(client_id=client_id)

            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message
            self._client.on_disconnect = self._on_disconnect

            if mqtt_cfg.get("username"):
                self._client.username_pw_set(
                    mqtt_cfg["username"], mqtt_cfg.get("password")
                )

            self._client.connect(host, port, keepalive)
            self._client.loop_start()
            if not self._wait_connected(connect_timeout_s):
                # paho 的 connect() 在 client_id 为空等情况下会返回成功却没有真正建连，
                # 不校验就会显示"已连接"却永远收不到数据。
                raise RuntimeError("MQTT 连接未建立 (%s:%d, client_id=%s)"
                                   % (host, port, client_id))
            self._client.subscribe(topic_filter, qos)
            self.connected.emit(True)

            while self._running:
                self._flush_rounds()
                time.sleep(0.5)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            self.connected.emit(False)
        finally:
            # 收尾：把还攒在缓冲里的轮次全部落库。静默窗口靠循环 tick 触发，
            # 退出那一刻之后不会再有人来收；不兜底的话最后一轮（甚至最后
            # 半轮）只能永远留在内存里，历史表就少了这一轮。
            try:
                self._flush_rounds(force=True)
            except Exception:
                pass
            self._cleanup()

    def stop(self) -> None:
        self._running = False

    def set_recording(self, recording: bool) -> None:
        self._recording = bool(recording)

    def _cleanup(self) -> None:
        self._env_emitted.clear()
        self._imp_scan_buf.clear()
        self._last_imp_scan_ts.clear()
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    @staticmethod
    def _gen_client_id() -> str:
        """配置没给 client_id 时兜底生成一个。

        paho-mqtt 2.x 下 client_id 留空会让 connect() 假装成功（返回 0 但
        根本不建连），所以这里必须保证非空。
        """
        return "fruit-pc-%d-%s" % (os.getpid(), uuid.uuid4().hex[:8])

    def _wait_connected(self, timeout_s: float) -> bool:
        """轮询真实连接状态，别只信 connect() 的返回值。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not self._running:
                return False
            if self._client is not None and self._client.is_connected():
                return True
            time.sleep(0.1)
        return bool(self._client is not None and self._client.is_connected())

    def stats(self) -> dict[str, Any]:
        return {
            "messages": self._msg_count,
            "last_msg_time": self._last_msg_time,
        }

    def delete_predictions_for(self, nodes) -> int:
        """按节点清单清理预测；空列表不删任何东西。"""
        cleaned = [n for n in (nodes or []) if n]
        if not cleaned:
            return 0
        return self._db.delete_predictions_for(cleaned)

    # ---- MQTT 回调 ----

    def _on_connect(self, client: mqtt.Client, _ud, _flags, rc: int) -> None:
        if rc == 0:
            self.connected.emit(True)
        else:
            self.connected.emit(False)
            self.error_occurred.emit(f"MQTT connect failed rc={rc}")

    def _on_disconnect(self, _client: mqtt.Client, _ud, rc: int) -> None:
        if rc != 0:
            self.connected.emit(False)
            self.error_occurred.emit(f"MQTT unexpected disconnect rc={rc}")

    def _on_message(self, _client: mqtt.Client, _ud, msg) -> None:
        try:
            gateway_id, node_id, topic_type = parse_topic(msg.topic)
        except ValueError:
            return

        # MQTT 的 payload 末尾没有换行符，必须走 feed_complete。
        # 走 feed_json 会一直等一个永远不来的 \n，消息全被卡在缓冲区里。
        try:
            payloads = self._framer.feed_complete(msg.payload)
        except Exception:
            return

        for payload in payloads:
            try:
                msg_type = resolve_msg_type(topic_type, payload)
                if self._dispatch(msg_type, gateway_id, node_id, payload):
                    self._msg_count += 1
                    self._last_msg_time = time.time()
            except Exception:
                # 单条脏消息不能把整条 MQTT 链路打断
                continue

    def _dispatch(self, msg_type: str, gateway_id: str, node_id: str, payload: dict) -> bool:
        payload = ensure_wallclock_ts(payload, self._clock)
        if msg_type == "sensor":
            data = parse_sensor(payload, self._config)
            impedance = self._sensor_impedance_row(payload)
            if self._recording:
                # 同一帧里的阻抗也跟着进环境行，历史窗口一行看全，
                # impedance_data 那边照常另存一份供频谱查询用。
                self._db.insert_sensor(data, impedance=impedance)
                if impedance is not None:
                    self._db.insert_impedance(impedance)
            self._update_context(data)
            self.data_received.emit({"type": "sensor", "data": data})
            if impedance is not None:
                # 同一帧里的阻抗三要素也推给界面，阻抗卡片才有实时值。
                self.data_received.emit({"type": "impedance", "data": impedance})
            self.status_changed.emit(data.gateway_id or gateway_id, data.node_id or node_id, "online")

        elif msg_type == "impedance":
            data = parse_impedance(payload)
            if self._recording:
                self._db.insert_impedance(data)
                # 单次扫描按频率拆成多条报文，一个 scan_id 串着。攒齐一段再挂
                # 环境行，不然每条都会去抢时间最近的那条环境帧。
                self._feed_impedance_scan(data, gateway_id, node_id)
            self.data_received.emit({"type": "impedance", "data": data})

        elif msg_type == "heartbeat":
            data = parse_heartbeat(payload)
            if self._recording:
                self._db.insert_status(data)
            self.status_changed.emit(
                data.gateway_id or gateway_id, data.node_id or node_id, data.status)
            if data.ip or data.rssi is not None:
                self.gateway_info.emit(
                    data.gateway_id or gateway_id,
                    data.node_id or node_id,
                    data.ip or "",
                    data.rssi if data.rssi is not None else 0.0,
                )

        elif msg_type == "impedance_raw":
            packet = parse_impedance_raw(payload)
            spectrum = self._assembler.feed(packet, now_ts=int(time.time()))
            if spectrum is not None:
                self._on_spectrum(spectrum)

        elif msg_type == "sweep":
            points = parse_sweep_points(payload)
            if not points:
                return False
            seg_total = payload.get("seg_total")
            try:
                seg_total = int(seg_total) if seg_total is not None else None
            except (TypeError, ValueError):
                seg_total = None
            seg = payload.get("seg")
            try:
                seg = int(seg) if seg is not None else None
            except (TypeError, ValueError):
                seg = None
            key = (points[0].gateway_id, points[0].node_id, points[0].round_id)
            expected = sweep_expected_points(
                payload, self._round_total_points, self._segment_points)
            self._buffer_round(
                points, seg=seg, seg_total=seg_total, expected_points=expected)
            # 网关固件不单独发环境帧：7 个环境量跟着每个频点一起上报，而一轮
            # 里这些值是同一份。抽一条当环境数据，一轮只发一次——不然
            # sensor_data 一直是空的，环境卡片和环境历史什么都看不到。
            sensor = self._sensor_from_sweep(points, key)
            if sensor is not None and key not in self._env_emitted:
                self._env_emitted.add(key)
                if self._recording:
                    self._db.insert_sensor(
                        sensor, round_id=key[2],
                        impedance=band_impedance_from_points(
                            points, self._band_lo_hz, self._band_hi_hz,
                            gateway_id=key[0], node_id=key[1],
                            timestamp=sensor.timestamp))
                self.data_received.emit({"type": "sensor", "data": sensor})
            if self._recording:
                self._db.insert_sweep_points(points)
                # 分段到达就落阻抗历史，不等整轮收齐。整轮出谱时会再写一遍
                # 同一批点，insert_impedance 按 (网关, 节点, scan_id, 频率)
                # 去重，不会重复。换来的是：后面丢包、程序中途退出，已经
                # 测到的点也不会从历史里消失。
                # scan_id 用轮次锚点而不是本段报文的时间戳：rebase 后同轮
                # 各段的时间戳会逐次前移，各写各的会把一轮拆成多个 scan。
                scan_id = self._round_scan_id(key)
                for row in impedance_rows_from_points(
                    points,
                    gateway_id=key[0],
                    node_id=key[1],
                    timestamp=points[0].timestamp,
                    scan_id=scan_id,
                    band_lo_hz=self._band_lo_hz,
                    band_hi_hz=self._band_hi_hz,
                ):
                    self._db.insert_impedance(row)
            self._update_context_from_sweep(points)
            self.data_received.emit({
                "type": "sweep",
                "data": points,
                "report_id": payload.get("report_id") or points[0].report_id,
                "round_id": points[0].round_id,
                "points_count": len(points),
                "min_points": self._min_sweep_points,
                "buffered_points": self._round_points_count(key),
                "total_points": expected,
                "seg": seg,
                "seg_total": seg_total,
                "segment_complete": len(points) >= self._min_sweep_points,
                "complete": self._round_complete(key),
            })
            # 攒够一整轮就立刻出图，不再等静默窗口。
            if self._round_complete(key):
                self._flush_round(key)

        elif msg_type == "spectrum":
            self._on_spectrum(parse_spectrum(payload))

        elif msg_type == "status":
            data = parse_status(payload)
            if data.status == "round_done":
                # 整轮摘要是轮次元数据，不是设备状态；落 device_status 表
                # 会让状态历史每轮多一条，淹真心跳和上下线事件。
                round_id = payload.get("round")
                try:
                    round_id = int(round_id) if round_id is not None else None
                except (TypeError, ValueError):
                    round_id = None
                self._db.record_round_done(
                    gateway_id=str(data.gateway_id or gateway_id or ""),
                    node_id=str(data.node_id or node_id or ""),
                    round_id=round_id,
                    total_points=payload.get("total"),
                    retry=payload.get("retry"),
                    imp_mean=payload.get("imp_mean"),
                )
                self.data_received.emit({
                    "type": "round_done",
                    "data": data,
                    "round_id": round_id,
                    "points": payload.get("points"),
                    "total_points": payload.get("total"),
                    "retry": payload.get("retry"),
                    "imp_mean": payload.get("imp_mean"),
                })
                self.status_changed.emit(
                    data.gateway_id, data.node_id or "", data.status)
                return True
            if self._recording:
                self._db.insert_status(data)
            self.status_changed.emit(data.gateway_id, data.node_id or "", data.status)

        elif msg_type == "prediction":
            # 空节点列表的 delete 是危险操作，直接忽略。
            if str(payload.get("action", "")) == "delete":
                self.delete_predictions_for(payload.get("nodes") or [])
                return True
            data = parse_prediction(payload)
            if self._recording:
                self._db.insert_prediction(data)
            self.data_received.emit({"type": "prediction", "data": data})

        else:
            return False

        return True

    def _buffer_round(self, points: list, seg: Optional[int] = None,
                      seg_total: Optional[int] = None,
                      expected_points: Optional[int] = None) -> bool:
        """按轮次缓存分片，返回整轮是否已收齐。"""
        return self._round_buf.feed(
            points, seg=seg, seg_total=seg_total, expected_points=expected_points)

    def _round_points_count(self, key: tuple[str, str, int]) -> int:
        return self._round_buf.points_count(key)

    def _round_scan_id(self, key: tuple[str, str, int]) -> Optional[str]:
        """该轮的全局唯一扫描标识，分段写入和整轮出谱必须拿到同一个值。

        内存里没有锚点（整轮已出谱、缓冲被清）就从库里取回该轮最早入库的
        时间戳。没有这步的话，迟到的重复段会落到另一个 scan_id 上，
        同一轮在历史表里被拆成两条。
        """
        anchor = self._round_buf.anchor(key)
        if not anchor:
            anchor = self._db.latest_sweep_round_ts(key[0], key[1], key[2])
        return impedance_scan_id(key[2], anchor)

    def _round_complete(self, key: tuple[str, str, int]) -> bool:
        """该轮是否已经攒够点数、并且声明的分段也收齐。"""
        return self._round_buf.complete(key)

    def _flush_round(self, key: tuple[str, str, int]) -> None:
        """把一轮缓存的点还原成整谱出图并落库，同时记下完整性判定。"""
        expected = self._round_buf.expected_points(key)
        scan_id = self._round_scan_id(key)
        points = self._round_buf.drain(key)
        if points:
            self._on_spectrum(
                sweep_points_to_spectrum(points), emit_sweep=False,
                expected_points=expected, scan_id=scan_id,
                attach_env_by_time=False)
            if self._recording:
                # 分段到达时环境行只拿到了部分频点的均值，整轮齐了用全轮
                # 的有效频段均值覆盖，和成熟度算法用的数一致。
                band_imp = band_impedance_from_points(
                    points, self._band_lo_hz, self._band_hi_hz)
                if not self._db.attach_sensor_impedance(
                        key[0], key[1], key[2], band_imp, force=True):
                    # 频点里没带环境量就折叠不出环境行，退而按时间就近填一段，
                    # 否则这一整轮的阻抗只躺在 impedance_data 里，历史表看不到。
                    to_ts = max(p.timestamp for p in points)
                    self._db.attach_sensor_impedance_by_time(
                        key[0], key[1], band_imp,
                        to_ts - SENSOR_IMPEDANCE_WINDOW_S, to_ts)
            self._log_round(key, points, expected, scan_id)

    def _log_round(
        self,
        key: tuple[str, str, int],
        points: list,
        expected: int,
        scan_id: Optional[str],
    ) -> None:
        """把这轮收了几点、收齐没有写进 round_log。

        不分完整与否都记：只看 impedance_data / sweep_data 的行数，
        分不清"这轮本来只有 50 点"和"这轮丢了 50 点"。

        点数不数本次刷了多少点，而是数库里这一轮实际有几个点。一轮可能被
        刷两次（先超时强刷出半轮、迟到的段到了再刷一次），第二次那次手里
        只有后到的 50 点，拿它当整轮点数会把已经补齐的轮次判成不完整。
        """
        try:
            gateway_id, node_id, round_id = key
            actual = (self._db.count_sweep_rows(gateway_id, node_id, round_id=round_id)
                      if round_id is not None else len(points))
            self._db.record_round(
                gateway_id=str(gateway_id or ""),
                node_id=str(node_id or ""),
                round_id=round_id,
                scan_id=scan_id,
                report_id=str(getattr(points[0], "report_id", "") or ""),
                points=actual,
                total_points=int(expected or 0),
                complete=actual >= int(expected or 0),
                first_ts=int(getattr(points[0], "timestamp", 0) or 0),
                last_ts=int(getattr(points[-1], "timestamp", 0) or 0),
            )
        except Exception:
            # 记日志失败不能反过来把整轮数据丢弃。
            pass

    def _feed_impedance_scan(
        self, data: ImpedanceData, gateway_id: str, node_id: str,
    ) -> None:
        """把一条阻抗分帧攒进它所属的扫描段。

        真实硬件的阻抗不跟在环境帧后面发，是一次扫描按频率拆成多条
        impedance 报文，共用一个 scan_id。逐条挂环境行的话，每条都会去抢
        时间最近的那条环境帧，最后环境表上只留下最后一个频率的值，前面
        那条环境帧就白填了。先攒齐一段算有效频段均值，再挂一次。
        """
        gw = data.gateway_id or gateway_id or ""
        node = data.node_id or node_id or ""
        key = (gw, node, str(data.scan_id or f"ts{data.timestamp}"))
        # 同一节点出了新扫描号，上一段就算结束了。
        for old_key in [
            k for k in self._imp_scan_buf
            if k[0] == gw and k[1] == node and k[2] != key[2]
        ]:
            self._flush_impedance_scan(old_key, self._imp_scan_buf.pop(old_key))
        self._imp_scan_buf.setdefault(key, []).append(data)

    def _advance_imp_epoch(self, gateway_id: str, node_id: str, to_ts: int,
                           impedance: Optional[ImpedanceData]) -> int:
        """把一次测量的有效频段均值填进它覆盖的那段时间内的环境行。

        覆盖区间是 [上一次测量时刻, 本次测量时刻]：阻抗是一次测量，环境量是
        连续采样，段内每一帧都属于这次测量。只填离测量时刻最近的一行的话，
        环境表绝大部分行还是空的。第一次没有上一次可参照，退回到固定窗口。
        """
        node_key = (gateway_id, node_id)
        if impedance is None:
            # 一个带内频点都没有：不推进时间锚点，免得下一段把这段该填的
            # 环境帧也跳过去。
            return 0
        from_ts = self._last_imp_scan_ts.get(node_key)
        if from_ts is None:
            from_ts = to_ts - SENSOR_IMPEDANCE_WINDOW_S
        self._last_imp_scan_ts[node_key] = to_ts
        return self._db.attach_sensor_impedance_by_time(
            gateway_id, node_id, impedance, from_ts, to_ts)

    def _flush_impedance_scan(
        self, key: tuple[str, str, str], points: list[ImpedanceData],
    ) -> None:
        """把攒齐的一段阻抗填进它覆盖的那段时间内的环境行。"""
        if not points:
            return
        self._advance_imp_epoch(
            key[0], key[1], max(p.timestamp for p in points),
            band_impedance_from_points(points, self._band_lo_hz, self._band_hi_hz))

    def _flush_impedance_scans(self, force: bool = False, idle_s: float = 3.0) -> None:
        """段静默超时、或退出收尾时，把还攒着的扫描段全部挂出去。

        节点一直没换扫描号的时候不会有"新扫描号到达"这个信号，只能靠静默
        判定；退出那一刻之后没人再来收，不兜底最后一段就永远留在内存里。
        """
        now = int(time.time())
        stale = [
            key for key, points in self._imp_scan_buf.items()
            if force or (now - max(p.timestamp for p in points)) >= idle_s
        ]
        for key in stale:
            self._flush_impedance_scan(key, self._imp_scan_buf.pop(key))

    def _flush_rounds(self, force: bool = False) -> None:
        """已收齐的轮次立即出图；没收齐的等静默窗口超时后强制出图。

        点数不够的轮次即使超时也照样出图，但整谱带上 ``complete=False``，
        界面标记为不完整、不会被当成完整阻抗谱展示。半条弧该被看见，
        只是不能被误读。

        force=True 用于退出收尾：跳过静默窗口判定，把还攒在缓冲里的
        轮次全部落库。窗口靠循环 tick 推进，连接断开或程序退出后就不
        再有下一次 tick，不兜底的话最后一轮永远只留在内存里。
        """
        for key in self._round_buf.keys():
            if self._round_complete(key):
                self._flush_round(key)
                continue
            if not force and not self._round_buf.is_idle(key, self._round_idle_s):
                continue
            self._flush_round(key)
        self._flush_impedance_scans(force=force, idle_s=self._round_idle_s)

    def _update_context(self, data: SensorData) -> None:
        """记录最新的环境量快照，供整谱落库时补写上下文列。"""
        self._last_context = {
            "soil_moisture": data.soil_moisture,
            "temperature": data.temperature,
            "nh3": data.nh3,
            "h2s": data.h2s,
            "co2": data.co2,
            "ph": data.ph,
            "humidity": data.humidity,
        }

    def _sensor_from_sweep(
        self, points: list, key: tuple[str, str, int],
    ) -> Optional[SensorData]:
        """从扫频频点里抽出一条环境传感器数据。

        固件把 7 个环境量以平行数组的形式跟着频点一起上报，一轮里各点的
        值是同一份，按轮取一个代表值就够，不必让 50 个点各占一条环境记录。
        时间戳用轮次锚点而不是本段报文的时间戳，和 impedance_data 的
        scan_id 锚点取同一处：分段到达、QoS1 重投、迟到的段，同一轮都
        落在同一条环境数据上。
        """
        if not points:
            return None
        keys = ("soil_moisture", "temperature", "nh3", "h2s", "co2", "ph", "humidity")
        values: dict[str, Optional[float]] = {k: None for k in keys}
        for point in points:
            for name in keys:
                if values[name] is not None:
                    continue
                raw = getattr(point, name, None)
                if raw is None:
                    continue
                try:
                    values[name] = float(raw)
                except (TypeError, ValueError):
                    continue
        if all(v is None for v in values.values()):
            return None
        anchor = self._round_buf.anchor(key)
        if not anchor:
            anchor = self._db.latest_sweep_round_ts(key[0], key[1], key[2])
        return SensorData(
            gateway_id=str(key[0]),
            node_id=str(key[1]),
            timestamp=int(anchor or getattr(points[0], "timestamp", 0) or 0),
            round_id=key[2],
            soil_moisture=values["soil_moisture"],
            temperature=values["temperature"],
            nh3=values["nh3"],
            h2s=values["h2s"],
            co2=values["co2"],
            ph=values["ph"],
            humidity=values["humidity"],
        )

    def _update_context_from_sweep(self, points: list) -> None:
        """从扫频点里取环境量补上下文。

        网关固件不发独立的环境帧，7 个环境量是随扫频点一起上来的，
        不回填的话整谱路径写库时这几列会是 NULL。
        """
        keys = ("soil_moisture", "temperature", "nh3", "h2s", "co2", "ph", "humidity")
        for point in reversed(points):
            for key in keys:
                value = getattr(point, key, None)
                if value is not None:
                    self._last_context[key] = value

    def _sensor_impedance_row(self, payload: dict) -> Optional[ImpedanceData]:
        """网关的单点 sensor 帧若同时带阻抗三要素，转成阻抗历史行。

        ESP32-S3 网关的 publishSensorPoint 一帧里同时发
        frequency_hz / z_real / z_imag / magnitude 和 7 个环境量：
        环境量进 sensor_data 驱动卡片，阻抗进 impedance_data 进历史窗口，
        两个历史查询界面都能查到同一次采样。
        """
        freq = payload.get("frequency_hz")
        z_real = payload.get("z_real")
        z_imag = payload.get("z_imag")
        if freq is None or z_real is None or z_imag is None:
            return None
        try:
            freq = float(freq)
            z_real = float(z_real)
            z_imag = float(z_imag)
        except (TypeError, ValueError):
            return None
        magnitude = payload.get("magnitude")
        try:
            magnitude = float(magnitude) if magnitude is not None else math.hypot(z_real, z_imag)
        except (TypeError, ValueError):
            magnitude = math.hypot(z_real, z_imag)
        # 网关不报相位时按库内统一约定从复数部分推出来，
        # 不推的话环境行和历史窗口的 phase 列又是空的。
        phase = payload.get("phase")
        if phase is None:
            phase = -math.degrees(math.atan2(z_imag, z_real))
        else:
            try:
                phase = float(phase)
            except (TypeError, ValueError):
                phase = -math.degrees(math.atan2(z_imag, z_real))
        return ImpedanceData(
            gateway_id=str(payload.get("gateway_id", "")),
            node_id=str(payload.get("node_id", "")),
            timestamp=int(payload.get("timestamp", 0)),
            frequency_hz=freq,
            z_real=z_real,
            z_imag=z_imag,
            magnitude=magnitude,
            phase=phase,
            in_valid_window=self._band_lo_hz <= freq <= self._band_hi_hz,
        )

    def _on_spectrum(self, spectrum: ImpedanceSpectrum, emit_sweep: bool = True,
                      expected_points: Optional[int] = None,
                      scan_id: Optional[str] = None,
                      attach_env_by_time: bool = True) -> None:
        prediction, stats = spectrum_prediction(
            spectrum,
            node_id=spectrum.node_id,
            timestamp=spectrum.timestamp,
            round_id=spectrum.scan_id,
            band_lo_hz=self._band_lo_hz,
            band_hi_hz=self._band_hi_hz,
            magnitude_high=self._magnitude_high,
            magnitude_low=self._magnitude_low,
            spread_ratio_ref=self._spread_ratio_ref,
        )
        if self._recording:
            # scan_id 优先用缓冲的轮次锚点：整谱对象的时间戳是轮内最晚一段
            # 的，和分段写入时用的锚点不一致会把一轮拆成两个 scan。
            # 没有缓冲来源（impedance_raw / spectrum 直发）时才退回自算。
            for row in sweep_to_impedance_rows(
                spectrum, self._band_lo_hz, self._band_hi_hz, scan_id=scan_id):
                self._db.insert_impedance(row)
            points = to_sweep_points(spectrum, context=self._last_context)
            if emit_sweep:
                self._db.insert_sweep_points(points)
            if attach_env_by_time:
                # 挂到它覆盖的那段时间内的环境行：真实硬件走 impedance_raw 分包
                # -> 整谱，既没有轮次号，也从不把阻抗塞进环境帧。少了这一刀的
                # 话历史数据窗口的阻抗列永远空着——只有走 sweep 轮次折叠才填得进去。
                self._advance_imp_epoch(
                    spectrum.gateway_id, spectrum.node_id, spectrum.timestamp,
                    band_impedance_from_points(
                        points, self._band_lo_hz, self._band_hi_hz))
            self._db.insert_prediction(prediction)
        self.data_received.emit(
            {
                "type": "spectrum",
                "data": spectrum,
                "stats": stats,
                "prediction": prediction,
                "report_id": getattr(spectrum, "report_id", None),
                "round_id": spectrum.scan_id,
                "points_count": len(spectrum.points),
                "min_points": self._min_sweep_points,
                "total_points": expected_points or self._min_sweep_points,
                "complete": len(spectrum.points) >= (expected_points or self._min_sweep_points),
            }
        )


# --------------------------------------------------------------------------- #
#  数据模拟器（无需硬件 / MQTT Broker 即可测试）
# --------------------------------------------------------------------------- #
R0_START = 2600.0
R0_END = 1500.0
R_INF = 220.0
TAU = 1.2e-4
ALPHA = 0.86
RIPEN_SECONDS = 6 * 3600.0
CA = math.cos(math.pi * ALPHA / 2)
SA = math.sin(math.pi * ALPHA / 2)

IMPEDANCE_FREQS = [100, 500, 1000, 5000, 10000, 30000, 100000]

# 单次上报的频点数与分段方式，和网关固件保持一致：
# TOTAL_POINTS=100 / SEG_POINTS=50 / 2 段。
# 协议约定单个上报报文不少于 50 个频点，否则视为不完整报文
# （照样入库，但界面标记警告且不参与出谱）。
#
# 频段也和固件对齐：节点侧的判频过滤是 900 Hz ~ 12 kHz
# （固件 loop() 里的 freq >= 900 && freq <= 12000），
# 模拟数据用别的频段会让成熟度标定完全脱离真机。
SWEEP_POINT_COUNT = 100
SWEEP_SEGMENT_SIZE = 50
SWEEP_FREQ_LO = 900.0
SWEEP_FREQ_HI = 12_000.0

SWEEP_FREQS = sorted({
    round(SWEEP_FREQ_LO * (SWEEP_FREQ_HI / SWEEP_FREQ_LO) ** (i / (SWEEP_POINT_COUNT - 1)))
    for i in range(SWEEP_POINT_COUNT)
})
SWEEP_SEGMENTS = max(1, -(-len(SWEEP_FREQS) // SWEEP_SEGMENT_SIZE))
assert SWEEP_SEGMENT_SIZE >= MIN_REPORT_POINTS, (
    "单个上报报文的频点数不能少于协议下限"
)

CONTEXT_KEYS = (
    "soil_moisture",
    "temperature",
    "nh3",
    "h2s",
    "co2",
    "ph",
    "humidity",
)


def build_sweep_round(
    gateway_id: str,
    node_id: str,
    r0: float,
    round_id: int,
    *,
    timestamp: Optional[int] = None,
    context: Optional[dict] = None,
    noise: float = 0.005,
) -> tuple[list[SweepPointData], ImpedanceSpectrum]:
    """按当前 R0 生成一整轮扫频，返回 (落库点列, 整谱对象)。

    模拟器、发布器和测试共用这一份生成逻辑，保证三处的 R0→成熟度
    行为完全一致，不会出现"界面一套数、入库另一套数"。
    """
    ts = int(timestamp if timestamp is not None else time.time())
    context = context or {}
    points: list[SweepPointData] = []
    spectrum_points: list[SpectrumPoint] = []

    for index, freq in enumerate(SWEEP_FREQS):
        zr, zi = _impedance_at(freq, r0)
        zr *= 1 + random.gauss(0, noise)
        zi *= 1 + random.gauss(0, noise * 1.5)
        mag = math.hypot(zr, zi)
        phase = -math.degrees(math.atan2(zi, zr))

        points.append(
            SweepPointData(
                gateway_id=gateway_id,
                node_id=node_id,
                round_id=round_id,
                timestamp=ts,
                point_index=index,
                frequency_hz=float(freq),
                z_real=round(zr, 2),
                z_imag=round(zi, 2),
                magnitude=round(mag, 2),
                soil_moisture=context.get("soil_moisture"),
                temperature=context.get("temperature"),
                nh3=context.get("nh3"),
                h2s=context.get("h2s"),
                co2=context.get("co2"),
                ph=context.get("ph"),
                humidity=context.get("humidity"),
                report_id=f"{gateway_id}/{node_id}/R{round_id}",
            )
        )
        spectrum_points.append(
            SpectrumPoint(
                frequency_hz=float(freq),
                z_real=round(zr, 2),
                z_imag=round(zi, 2),
                magnitude=round(mag, 2),
                phase_deg=round(phase, 2),
            )
        )

    spectrum = ImpedanceSpectrum(
        gateway_id=gateway_id,
        node_id=node_id,
        scan_id=round_id,
        timestamp=ts,
        points=spectrum_points,
        meta={
            "points_expected": len(SWEEP_FREQS),
            "points_missing": 0,
            "r0": round(r0, 2),
        },
    )
    return points, spectrum


def _impedance_at(freq: float, r0: float) -> tuple[float, float]:
    m = (freq * TAU) ** ALPHA
    denom_real = 1.0 + m * CA
    denom_imag = m * SA
    norm = denom_real * denom_real + denom_imag * denom_imag
    z_real = R_INF + (r0 - R_INF) * denom_real / norm
    z_imag = -(r0 - R_INF) * denom_imag / norm
    return z_real, z_imag


class SimulatorWorker(QThread):
    """内部生成传感器 + 阻抗数据，落库并发信号。"""

    data_received = Signal(dict)
    status_changed = Signal(str, str, str)
    connected = Signal(bool)

    def __init__(self, config: dict, db) -> None:
        super().__init__()
        self._config = config
        self._db = db
        self._running = True
        self._recording = True
        self._last_error_at = 0.0
        swp_cfg = config.get("sweep", {})
        self._min_sweep_points = max(
            MIN_REPORT_POINTS, int(swp_cfg.get("min_points", MIN_REPORT_POINTS)))
        self._round_buf = RoundBuffer(
            self._min_sweep_points,
            total_points=max(1, int(swp_cfg.get("total_points", 100))),
            segment_points=max(1, int(swp_cfg.get("segment_points", 50))),
        )

        self._last_good = {
            "temperature": 24.0,
            "humidity": 65.0,
            "soil_moisture": 50.0,
            "nh3": 10.0,
            "h2s": 5.0,
            "co2": 450.0,
            "ph": 6.5,
        }
        self._temp = 24.0
        self._humidity = 65.0
        self._soil = 50.0
        self._nh3 = 10.0
        self._h2s = 5.0
        self._co2 = 450.0
        self._ph = 6.5
        self._session_start = time.time()
        self._last_impedance = 0.0
        self._msg_count = 0
        self._round_id = 0

    def run(self) -> None:
        sim_cfg = self._config.get("simulator", {})
        interval_s = sim_cfg.get("interval_ms", 2000) / 1000.0
        gateways = sim_cfg.get("gateways", ["GW_001"])
        nodes = sim_cfg.get("nodes", ["LORA_NODE_01"])
        imp_interval_s = sim_cfg.get("impedance_interval_ms", 15000) / 1000.0
        publish_imp = sim_cfg.get("publish_impedance", True)

        self.connected.emit(True)
        self.status_changed.emit(gateways[0], nodes[0], "online")

        while self._running:
            self._update_state()

            for gw in gateways:
                for node in nodes:
                    data = self._make_sensor(gw, node)
                    if self._recording:
                        self._db.insert_sensor(data)
                    self.data_received.emit({"type": "sensor", "data": data})
                    self._msg_count += 1

            if publish_imp:
                now = time.time()
                if now - self._last_impedance >= imp_interval_s:
                    self._last_impedance = now
                    for gw in gateways:
                        for node in nodes:
                            self._publish_impedance(gw, node)

            time.sleep(interval_s)

    def stop(self) -> None:
        self._running = False

    def set_recording(self, recording: bool) -> None:
        self._recording = bool(recording)

    def stats(self) -> dict[str, Any]:
        return {"messages": self._msg_count}

    def _update_state(self) -> None:
        self._last_good["temperature"] = max(20.0, min(30.0, self._last_good["temperature"] + random.gauss(0, 0.1)))
        self._last_good["humidity"] = max(40.0, min(85.0, self._last_good["humidity"] + random.gauss(0, 0.3)))
        self._last_good["soil_moisture"] = max(20.0, min(80.0, self._last_good["soil_moisture"] + random.gauss(0, 0.5)))
        self._last_good["nh3"] = max(0.0, min(50.0, self._last_good["nh3"] + random.gauss(0, 0.5)))
        self._last_good["h2s"] = max(0.0, min(30.0, self._last_good["h2s"] + random.gauss(0, 0.3)))
        self._last_good["co2"] = max(350.0, min(800.0, self._last_good["co2"] + random.gauss(0, 2.0)))
        self._last_good["ph"] = max(5.0, min(8.0, self._last_good["ph"] + random.gauss(0, 0.01)))

    def _make_sensor(self, gateway_id: str, node_id: str) -> SensorData:
        raw = dict(self._last_good)
        now = time.time()
        corrected = dict(raw)

        if now - self._last_error_at > 30:
            roll = random.random()
            if roll < 0.15:
                raw["temperature"] = raw["temperature"] + random.uniform(8.0, 14.0)
                self._last_error_at = now
            elif roll < 0.28:
                raw["humidity"] = None
                self._last_error_at = now
            elif roll < 0.40:
                raw["nh3"] = random.uniform(40.0, 50.0)
                raw["h2s"] = random.uniform(20.0, 30.0)
                self._last_error_at = now

        if raw["temperature"] is None or abs(raw["temperature"] - self._last_good["temperature"]) > 5.0:
            corrected["temperature"] = self._last_good["temperature"]
        if raw["humidity"] is None:
            corrected["humidity"] = self._last_good["humidity"]
        if raw["nh3"] is None or raw["nh3"] > self._last_good["nh3"] + 10.0:
            corrected["nh3"] = self._last_good["nh3"]
        if raw["h2s"] is None or raw["h2s"] > self._last_good["h2s"] + 8.0:
            corrected["h2s"] = self._last_good["h2s"]

        return SensorData(
            gateway_id=gateway_id,
            node_id=node_id,
            timestamp=int(now),
            temperature=round(corrected["temperature"], 2),
            humidity=round(corrected["humidity"], 1),
            soil_moisture=round(corrected["soil_moisture"], 1),
            nh3=round(corrected["nh3"], 1),
            h2s=round(corrected["h2s"], 1),
            co2=int(round(corrected["co2"])),
            ph=round(corrected["ph"], 2),
        )

    def _publish_impedance(self, gateway_id: str, node_id: str) -> None:
        """生成一整轮扫频，按报文分段发出，再级联产出整谱与成熟度预测。

        分段方式模拟真实网关：一轮 100 点拆成 2 个报文，每个报文 50 点。
        单个报文少于 min_points 会被标记为不完整报文（照样入库、不出谱）。
        """
        elapsed = time.time() - self._session_start
        progress = max(0.0, min(1.0, elapsed / RIPEN_SECONDS))
        r0 = R0_START - (R0_START - R0_END) * progress
        self._round_id += 1

        context = {key: self._last_good[key] for key in CONTEXT_KEYS}
        points, spectrum = build_sweep_round(
            gateway_id, node_id, r0, self._round_id, context=context
        )

        min_points = self._min_sweep_points
        key = (gateway_id, node_id, self._round_id)

        for k in range(SWEEP_SEGMENTS):
            seg = points[k * SWEEP_SEGMENT_SIZE:(k + 1) * SWEEP_SEGMENT_SIZE]
            if not seg:
                continue
            if self._recording:
                self._db.insert_sweep_points(seg)
            if self._config.get("simulator", {}).get("buffer_rounds", True):
                self._round_buf.feed(seg, seg=k, seg_total=SWEEP_SEGMENTS)
                buffered = self._round_buf.points_count(key)
                complete = self._round_buf.complete(key)
            else:
                buffered, complete = len(seg), len(seg) >= min_points
            payload: dict[str, Any] = {
                "type": "sweep",
                "data": seg,
                "report_id": f"{gateway_id}/{node_id}/R{self._round_id}",
                "round_id": self._round_id,
                "points_count": len(seg),
                "min_points": min_points,
                "total_points": len(points),
                "buffered_points": buffered,
                "seg": k,
                "seg_total": SWEEP_SEGMENTS,
                "segment_complete": len(seg) >= min_points,
                "complete": complete,
            }
            self.data_received.emit(payload)
            self._msg_count += 1

        # 模拟器自己直出整谱，不靠静默窗口，缓存用不着留；不清的话
        # 每轮都留在内存里，跑久了白涨。
        self._round_buf.drain(key)

        mat_cfg = self._config.get("maturity", {})
        sweep_scan_id = impedance_scan_id(spectrum.scan_id, spectrum.timestamp)
        prediction, stats = spectrum_prediction(
            spectrum,
            node_id=node_id,
            timestamp=spectrum.timestamp,
            round_id=spectrum.scan_id,
            band_lo_hz=float(mat_cfg.get("band_lo_hz", 1000.0)),
            band_hi_hz=float(mat_cfg.get("band_hi_hz", 30000.0)),
            magnitude_high=float(mat_cfg.get("magnitude_high", 1900.0)),
            magnitude_low=float(mat_cfg.get("magnitude_low", 1050.0)),
            spread_ratio_ref=float(mat_cfg.get("spread_ratio_ref", 0.29)),
        )
        if self._recording:
            for row in sweep_to_impedance_rows(
                spectrum,
                band_lo_hz=float(mat_cfg.get("band_lo_hz", 1000.0)),
                band_hi_hz=float(mat_cfg.get("band_hi_hz", 30000.0)),
                scan_id=sweep_scan_id):
                self._db.insert_impedance(row)
            self._db.insert_prediction(prediction)
            # 模拟器没有独立的环境帧，一轮扫频的环境量和阻抗都出自同一次采样；
            # 按轮次折叠出一条环境行，环境历史里的阻抗列才不会一直空着。
            self._db.insert_sensor(
                SensorData(
                    gateway_id=gateway_id,
                    node_id=node_id,
                    timestamp=int(points[0].timestamp) if points else int(time.time()),
                    round_id=self._round_id,
                    soil_moisture=context.get("soil_moisture"),
                    temperature=context.get("temperature"),
                    nh3=context.get("nh3"),
                    h2s=context.get("h2s"),
                    co2=context.get("co2"),
                    ph=context.get("ph"),
                    humidity=context.get("humidity"),
                ),
                impedance=band_impedance_from_points(
                    points,
                    band_lo_hz=float(mat_cfg.get("band_lo_hz", 1000.0)),
                    band_hi_hz=float(mat_cfg.get("band_hi_hz", 30000.0))),
            )
            try:
                first = points[0] if points else None
                last = points[-1] if points else None
                self._db.record_round(
                    gateway_id=gateway_id,
                    node_id=node_id,
                    round_id=self._round_id,
                    scan_id=sweep_scan_id,
                    report_id=f"{gateway_id}/{node_id}/R{self._round_id}",
                    points=len(spectrum.points),
                    total_points=len(points),
                    complete=len(spectrum.points) >= len(points),
                    first_ts=int(getattr(first, "timestamp", 0) or 0),
                    last_ts=int(getattr(last, "timestamp", 0) or 0),
                )
            except Exception:
                pass
        self.data_received.emit({
            "type": "spectrum",
            "data": spectrum,
            "stats": stats,
            "prediction": prediction,
            "report_id": f"{gateway_id}/{node_id}/R{self._round_id}",
            "round_id": self._round_id,
            "points_count": len(spectrum.points),
            "min_points": min_points,
            "complete": len(spectrum.points) >= min_points,
        })
        self._msg_count += 1
