"""消息协议定义：数据模型、校验、Topic 解析。"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


VALID_SENSOR_FIELDS = {
    "temperature",
    "humidity",
    "soil_moisture",
    "nh3",
    "h2s",
    "co2",
    "ph",
}

VALID_MSG_TYPES = {
    "sensor",
    "impedance",
    "status",
    "heartbeat",
    "prediction",
    "sweep",
    "impedance_raw",
    "spectrum",
}

FRAME_TERMINATOR = "\n"
DEFAULT_FRAME_LIMIT = 65536

# 协议下限：单个 sweep 报文至少要带上这么多频点，否则视为不完整上报。
# 网关固件按 SEG_POINTS=50 / 2 段上报，正好压在下限上。
# 界面用它判断"这次上报够不够完整"，落库侧按轮次累计，攒够才出整谱。
MIN_REPORT_POINTS = 50


# --------------------------------------------------------------------------- #
#  统一数据帧契约：一条 JSON + 一个换行符，贯穿 UART / MQTT / WebSocket
# --------------------------------------------------------------------------- #
def encode_frame(payload: dict | str, terminator: str = FRAME_TERMINATOR) -> str:
    """序列化为一帧。dict 走紧凑 JSON，字符串原样透传，末尾追加换行符。

    紧凑分隔符是为了让 Uno/ESP 侧 ``Serial.println`` 与 PC 侧
    ``readline()`` 一一对应；MQTT payload 本身按包分割，但保留同一约定
    可以让 broker、FastAPI、小程序三层复用同一份编解码代码。
    """
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )
    return text.rstrip("\r\n") + terminator


class FrameDecodeError(ValueError):
    """单帧 JSON 解析失败。"""


class LineFramer:
    """换行符分隔的 JSON 帧解帧器，容忍粘包、半包、CRLF 与超长帧。

    这是整条链路唯一的解帧入口：
    - Uno/ESP 串口：``Serial.println(json)`` → ``readline()`` 天然按 \n 切分
    - MQTT / WebSocket：payload 里若带 \n，用 ``feed`` 拆开再逐个解析
    - 超长帧直接丢弃并计数，不会把半截残帧留到下一次，避免解析串台

    ``feed`` 接收任意长度的二进制或字符串块，内部只做缓冲，
    不依赖调用方保证帧边界。
    """

    def __init__(self, max_frame_len: int = DEFAULT_FRAME_LIMIT, strict_json: bool = True):
        self._max = int(max_frame_len)
        self._strict = strict_json
        self._buf = bytearray()
        self.frames: int = 0
        self.dropped: int = 0
        self.bad_json: int = 0

    def feed(self, chunk: bytes | str) -> list[str]:
        """投喂一段原始字节，返回本次凑齐的完整帧文本列表（不含分隔符）。"""
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        self._buf.extend(chunk)

        frames: list[str] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx]).rstrip(b"\r")
            del self._buf[: idx + 1]
            if not raw:
                continue
            if len(raw) > self._max:
                self.dropped += 1
                continue
            frames.append(raw.decode("utf-8", errors="replace"))

        if len(self._buf) > self._max:
            self.dropped += 1
            del self._buf[: len(self._buf) - self._max]

        self.frames += len(frames)
        return frames

    def feed_json(self, chunk: bytes | str) -> list[dict]:
        """``feed`` 的严格版本：只返回可解析为 JSON 对象的帧。

        坏帧计数进 ``bad_json``，不抛异常，保证高频链路不会因单条脏帧中断。
        """
        good: list[dict] = []
        for text in self.feed(chunk):
            try:
                obj = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                self.bad_json += 1
                if self._strict:
                    raise FrameDecodeError(text[:120]) from None
                continue
            if isinstance(obj, dict):
                good.append(obj)
            else:
                self.bad_json += 1
        return good

    def feed_complete(self, chunk: bytes | str) -> list[dict]:
        """整块已经是一个完整消息时直接解析（MQTT/WebSocket 单条 payload 走这里）。

        与 ``feed_json`` 的区别：不依赖换行符切帧。MQTT 的 payload 末尾没有
        ``\\n``，若走 ``feed_json`` 会被永远卡在缓冲区里、一条都收不到。
        payload 里若真的带了多个换行分隔的帧，也一并拆开解析。
        """
        if isinstance(chunk, (bytes, bytearray)):
            text_all = bytes(chunk).decode("utf-8", errors="replace")
        else:
            text_all = str(chunk)

        good: list[dict] = []
        for text in text_all.split("\n"):
            text = text.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                self.bad_json += 1
                if self._strict:
                    raise FrameDecodeError(text[:120]) from None
                continue
            if isinstance(obj, dict):
                good.append(obj)
            else:
                self.bad_json += 1

        self.frames += len(good)
        return good

    def reset(self) -> None:
        self._buf.clear()

    def stats(self) -> dict[str, int]:
        return {
            "frames": self.frames,
            "dropped": self.dropped,
            "bad_json": self.bad_json,
            "buffered": len(self._buf),
        }


@dataclass
class SensorData:
    gateway_id: str
    node_id: str
    timestamp: int
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    soil_moisture: Optional[float] = None
    nh3: Optional[float] = None
    h2s: Optional[float] = None
    co2: Optional[float] = None
    ph: Optional[float] = None
    anomaly_flags: list[str] = field(default_factory=list)
    # 扫频帧带的环境量属于哪一轮。网关不单独发环境帧，环境量是跟着频点来的，
    # 一轮里的这些值是同一份；带上轮次号才能把一轮折叠成一条环境数据。
    round_id: Optional[int] = None


@dataclass
class ImpedanceData:
    gateway_id: str
    node_id: str
    timestamp: int
    scan_id: Optional[str] = None
    frequency_hz: Optional[float] = None
    z_real: Optional[float] = None
    z_imag: Optional[float] = None
    magnitude: Optional[float] = None
    phase: Optional[float] = None
    rcal_ohm: Optional[float] = None
    in_valid_window: Optional[bool] = None


@dataclass
class DeviceStatus:
    gateway_id: str
    node_id: Optional[str]
    status: str
    timestamp: int
    ip: Optional[str] = None
    rssi: Optional[float] = None


@dataclass
class PredictionData:
    node_id: Optional[str]
    timestamp: Optional[int]
    maturity: Optional[float]
    maturity_level: Optional[str]
    harvest_date: Optional[str]
    confidence: Optional[float]
    # 对应 sweep_data.round_id。同一秒内可能落下多轮上报，
    # 光靠 (node_id, timestamp) 会把它们压成一条。
    round_id: Optional[int] = None


@dataclass
class SweepPointData:
    gateway_id: str
    node_id: str
    round_id: int
    timestamp: int
    point_index: int
    frequency_hz: Optional[float]
    z_real: Optional[float]
    z_imag: Optional[float]
    magnitude: Optional[float]
    soil_moisture: Optional[float]
    temperature: Optional[float]
    nh3: Optional[float]
    h2s: Optional[float]
    co2: Optional[float]
    ph: Optional[float]
    humidity: Optional[float]
    report_id: Optional[str] = None


@dataclass
class ImpedanceRawPoint:
    """AD5933 单频点原始测量值（未做 |Z|/相位换算）。"""

    i: int
    re: float
    im: float


@dataclass
class ImpedanceRawPacket:
    """AD5933 扫频的一个分包。

    一次完整扫频耗时太长，不能阻塞 1Hz 的温湿度上报，
    因此在节点端按 ``point_count`` 个点切成 ``packet_total`` 个包
    独立发出，PC 侧用 :class:`spectrum.SpectrumAssembler` 拼装回整谱。

    频点用等差递推表达（``frequency_start_hz + k * frequency_increment_hz``），
    40 个频点也只需两个标量，避免每个包都携带完整频率数组。
    """

    gateway_id: str
    node_id: str
    timestamp: int
    scan_id: int
    packet_index: int
    packet_total: int
    point_start: int
    point_count: int
    frequency_start_hz: float
    frequency_increment_hz: float
    frequencies: list[float] = field(default_factory=list)
    points: list[ImpedanceRawPoint] = field(default_factory=list)

    def frequency_at(self, offset: int) -> float:
        idx = self.point_start + offset
        if self.frequencies and 0 <= idx < len(self.frequencies):
            return float(self.frequencies[idx])
        return self.frequency_start_hz + idx * self.frequency_increment_hz

    @property
    def point_end(self) -> int:
        return self.point_start + len(self.points)


@dataclass
class SpectrumPoint:
    """组装完成后的单频点，用于 Nyquist / Bode 绘图。"""

    frequency_hz: float
    z_real: float
    z_imag: float
    magnitude: Optional[float] = None
    phase_deg: Optional[float] = None


@dataclass
class ImpedanceSpectrum:
    """一次完整扫频的结果。"""

    gateway_id: str
    node_id: str
    scan_id: int
    timestamp: int
    points: list[SpectrumPoint]
    meta: dict[str, Any] = field(default_factory=dict)

    def sort_by_frequency(self) -> "ImpedanceSpectrum":
        self.points.sort(key=lambda p: p.frequency_hz)
        return self


def validate_impedance_raw_packet(pkt: ImpedanceRawPacket) -> tuple[bool, list[str]]:
    """校验分包自身一致性，返回 (是否全部有效, 异常标记列表)。"""
    flags: list[str] = []
    if pkt.packet_total < 1:
        flags.append("packet_total_invalid")
    if pkt.point_count < 1:
        flags.append("point_count_invalid")
    if pkt.packet_index < 0:
        flags.append("packet_index_negative")
    elif pkt.packet_total > 0 and pkt.packet_index >= pkt.packet_total:
        flags.append("packet_index_out_of_range")
    if pkt.point_start < 0:
        flags.append("point_start_negative")
    if pkt.frequency_start_hz <= 0 and not pkt.frequencies:
        flags.append("frequency_start_invalid")
    if not pkt.frequencies and pkt.frequency_increment_hz <= 0:
        flags.append("frequency_increment_invalid")
    if pkt.frequencies and len(pkt.frequencies) < pkt.point_start + pkt.point_count:
        flags.append("frequencies_truncated")
    if len(pkt.points) != pkt.point_count:
        flags.append("points_len_mismatch")
    return len(flags) == 0, flags


def parse_impedance_raw(payload: dict) -> ImpedanceRawPacket:
    points: list[ImpedanceRawPoint] = []
    per_point_freq: dict[int, float] = {}
    for raw in payload.get("points") or []:
        if not isinstance(raw, dict):
            continue
        index = int(raw.get("i", len(points)))
        points.append(
            ImpedanceRawPoint(
                i=index,
                re=float(raw.get("re", raw.get("z_real", 0.0))),
                im=float(raw.get("im", raw.get("z_imag", 0.0))),
            )
        )
        freq = raw.get("f", raw.get("freq", raw.get("frequency_hz")))
        if freq is not None:
            try:
                per_point_freq[index] = float(freq)
            except (TypeError, ValueError):
                pass

    frequencies: list[float] = []
    raw_freqs = payload.get("frequencies") or payload.get("freq") or payload.get("freq_array")
    if isinstance(raw_freqs, list):
        for value in raw_freqs:
            try:
                frequencies.append(float(value))
            except (TypeError, ValueError):
                frequencies.append(0.0)

    point_start = int(payload.get("point_start", 0))
    if per_point_freq and not frequencies:
        end = point_start + int(payload.get("point_count", len(points)))
        frequencies = [per_point_freq.get(i, 0.0) for i in range(max(0, end))]

    return ImpedanceRawPacket(
        gateway_id=payload.get("gateway_id", ""),
        node_id=payload.get("node_id", payload.get("node", "")),
        timestamp=int(payload.get("timestamp", payload.get("ts", 0))),
        scan_id=int(payload.get("scan_id", payload.get("round", 0))),
        packet_index=int(payload.get("packet_index", 0)),
        packet_total=int(payload.get("packet_total", 1)),
        point_start=point_start,
        point_count=int(payload.get("point_count", len(points))),
        frequency_start_hz=float(
            payload.get("frequency_start_hz", payload.get("freq_start_hz", 0.0))
        ),
        frequency_increment_hz=float(
            payload.get("frequency_increment_hz", payload.get("freq_step_hz", 0.0))
        ),
        frequencies=frequencies,
        points=points,
    )


def _first_seq(payload: dict, *keys: str) -> Optional[list[Any]]:
    for key in keys:
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return None


def _seq_value(seq: Optional[list[Any]], index: int, default: Optional[float] = None) -> Optional[float]:
    if not isinstance(seq, list) or index >= len(seq):
        return default
    val = seq[index]
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def parse_sweep_points(payload: dict) -> list[SweepPointData]:
    """把网关上报的分片扫频转成落库用的点列。

    兼容两种线上形状：
    - ESP32 网关批量数组：``freq/re/im/imp`` + ``soil_moisture/temperature/...`` 平行数组
    - 逐点字典：``points: [{i, re, im, ...}]``

    ``imp`` 缺省时按 sqrt(re^2 + im^2) 现算，保证绘图不依赖上报端补齐。
    """
    gateway_id = payload.get("gateway_id", "")
    node_id = payload.get("node_id", payload.get("node", ""))
    round_id = int(payload.get("round", payload.get("round_id", 0)))
    timestamp = int(payload.get("timestamp", payload.get("ts", 0)))
    point_start = int(payload.get("point_start", 0))
    report_id = payload.get("report_id") or payload.get("scan_id") or f"{gateway_id}/{node_id}/R{round_id}"

    raw_points = payload.get("points")
    if isinstance(raw_points, list) and raw_points and isinstance(raw_points[0], dict):
        count = len(raw_points)
        get_freq = lambda i: raw_points[i].get("freq", raw_points[i].get("frequency_hz"))  # noqa: E731
        get_re = lambda i: raw_points[i].get("re", raw_points[i].get("z_real"))  # noqa: E731
        get_im = lambda i: raw_points[i].get("im", raw_points[i].get("z_imag"))  # noqa: E731
        get_mag = lambda i: raw_points[i].get("imp", raw_points[i].get("magnitude"))  # noqa: E731
        ctx = lambda key, i: raw_points[i].get(key)  # noqa: E731
    else:
        freq_seq, re_seq = _first_seq(payload, "freq", "frequency_hz"), _first_seq(payload, "re", "z_real")
        im_seq, mag_seq = _first_seq(payload, "im", "z_imag"), _first_seq(payload, "imp", "magnitude")
        count = max(
            (len(seq) for seq in (freq_seq, re_seq, im_seq, mag_seq) if isinstance(seq, list)),
            default=0,
        )
        get_freq = lambda i: _seq_value(freq_seq, i)  # noqa: E731
        get_re = lambda i: _seq_value(re_seq, i)  # noqa: E731
        get_im = lambda i: _seq_value(im_seq, i)  # noqa: E731
        get_mag = lambda i: _seq_value(mag_seq, i)  # noqa: E731
        ctx = lambda key, i: _seq_value(_first_seq(payload, key), i)  # noqa: E731

    out: list[SweepPointData] = []
    for idx in range(count):
        re_val = get_re(idx)
        im_val = get_im(idx)
        mag_val = get_mag(idx)
        if mag_val is None and re_val is not None and im_val is not None:
            mag_val = math.hypot(re_val, im_val)
        out.append(
            SweepPointData(
                gateway_id=gateway_id,
                node_id=node_id,
                round_id=round_id,
                timestamp=timestamp,
                point_index=point_start + idx,
                frequency_hz=get_freq(idx),
                z_real=re_val,
                z_imag=im_val,
                magnitude=mag_val,
                soil_moisture=ctx("soil_moisture", idx),
                temperature=ctx("temperature", idx),
                nh3=ctx("nh3", idx),
                h2s=ctx("h2s", idx),
                co2=ctx("co2", idx),
                ph=ctx("ph", idx),
                humidity=ctx("humidity", idx),
                report_id=report_id,
            )
        )
    return out


def parse_topic(topic: str) -> tuple[str, str, str]:
    """将 fruit/GW_001/LORA_NODE_01/sensor 解析为三元组。"""
    parts = topic.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "fruit":
        raise ValueError(f"unexpected topic format: {topic}")
    return parts[1], parts[2], parts[3]


def validate_sensor(payload: dict, cfg: dict) -> tuple[bool, list[str]]:
    """返回 (是否全部有效, 异常字段列表)。"""
    flags: list[str] = []
    v = cfg.get("validation", {})

    def check_range(key: str, field: str) -> None:
        val = payload.get(key)
        if val is None:
            return
        lo, hi = v.get(f"{field}_min"), v.get(f"{field}_max")
        if lo is not None and val < lo:
            flags.append(f"{field}<{lo}")
        if hi is not None and val > hi:
            flags.append(f"{field}>{hi}")

    check_range("temperature", "temperature")
    check_range("humidity", "humidity")
    check_range("ph", "ph")
    check_range("co2", "co2")
    return len(flags) == 0, flags


def parse_sensor(payload: dict, cfg: dict) -> SensorData:
    ok, flags = validate_sensor(payload, cfg)
    return SensorData(
        gateway_id=payload.get("gateway_id", ""),
        node_id=payload.get("node_id", ""),
        timestamp=int(payload.get("timestamp", int(datetime.now(tz=timezone.utc).timestamp()))),
        temperature=payload.get("temperature"),
        humidity=payload.get("humidity"),
        soil_moisture=payload.get("soil_moisture"),
        nh3=payload.get("nh3"),
        h2s=payload.get("h2s"),
        co2=payload.get("co2"),
        ph=payload.get("ph"),
        anomaly_flags=[] if ok else flags,
    )


def parse_impedance(payload: dict) -> ImpedanceData:
    return ImpedanceData(
        gateway_id=payload.get("gateway_id", ""),
        node_id=payload.get("node_id", ""),
        timestamp=int(payload.get("timestamp", 0)),
        scan_id=payload.get("scan_id"),
        frequency_hz=payload.get("frequency_hz"),
        z_real=payload.get("z_real"),
        z_imag=payload.get("z_imag"),
        magnitude=payload.get("magnitude"),
        phase=payload.get("phase"),
        rcal_ohm=payload.get("rcal_ohm"),
        in_valid_window=payload.get("in_valid_window"),
    )


def parse_status(payload: dict) -> DeviceStatus:
    return DeviceStatus(
        gateway_id=payload.get("gateway_id", payload.get("device_id", "")),
        node_id=payload.get("node_id"),
        status=payload.get("status", "unknown"),
        timestamp=int(payload.get("timestamp", int(datetime.now(tz=timezone.utc).timestamp()))),
        ip=payload.get("ip"),
        rssi=_as_float(payload.get("rssi")),
    )


def parse_heartbeat(payload: dict) -> DeviceStatus:
    """网关存活心跳。

    ESP32-S3 网关固件每 ``HEARTBEAT_INTERVAL_MS`` 发一条
    ``{"type":"heartbeat","ip":...,"rssi":...}``，不携带 ``status`` 字段，
    语义就是"我还活着"，因此统一映射成 online，
    而不是让 ``parse_status`` 填 "unknown" 再被落库侧当噪声丢弃。
    """
    return DeviceStatus(
        gateway_id=payload.get("gateway_id", payload.get("device_id", "")),
        node_id=payload.get("node_id"),
        status="online",
        timestamp=int(payload.get(
            "timestamp",
            payload.get("ts", int(datetime.now(tz=timezone.utc).timestamp())),
        )),
        ip=payload.get("ip"),
        rssi=_as_float(payload.get("rssi")),
    )


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sweep_expected_points(
    payload: dict,
    default_total: int = 100,
    default_seg_points: int = 50,
) -> int:
    """推断一轮扫频的总点数，供 PC 侧判定"整轮收齐"。

    优先取报文显式声明的值，其次按 ``seg_total * seg_points`` 推算，
    最后用 ``point_start + 本次点数`` 作下界，再回落到配置默认值。
    顺序是"声明 > 推算 > 观测 > 默认"，任一步得到更大的数就替换。

    ESP32-S3 网关固件只报 ``seg`` 不报 ``seg_total``，
    所以整轮完成只能靠"攒够 total_points 个点"来判定。
    """
    expected = 0
    # 注意：``point_count`` 是"本次报文里的点数"，不是整轮总点数，
    # 放进这个列表会让整轮在第一段就判完成、出半条弧的谱。
    for key in ("total_points", "points_expected", "total"):
        value = _as_float(payload.get(key))
        if value and value > 0:
            expected = int(value)
            break

    if expected == 0:
        seg_total = _as_float(payload.get("seg_total"))
        if seg_total and seg_total > 0:
            per_seg = _as_float(payload.get("seg_points"))
            per_seg = (
                int(per_seg)
                if per_seg and per_seg > 0
                else max(1, int(default_seg_points))
            )
            expected = int(seg_total) * per_seg

    if expected == 0:
        expected = max(1, int(default_total))

    # 报文没声明总数时，用 point_start + 本次点数 作为下界兜底。
    start = _as_float(payload.get("point_start"))
    if start and start > 0:
        arrays = [
            payload.get(key)
            for key in ("freq", "re", "im", "imp")
            if isinstance(payload.get(key), list)
        ]
        length = max((len(a) for a in arrays), default=0)
        if length:
            expected = max(expected, int(start) + length)

    return max(expected, 1)


def parse_prediction(payload: dict) -> PredictionData:
    return PredictionData(
        node_id=payload.get("node_id"),
        timestamp=payload.get("timestamp"),
        maturity=payload.get("maturity"),
        maturity_level=payload.get("maturity_level"),
        harvest_date=payload.get("harvest_date"),
        confidence=payload.get("confidence"),
    )


def try_parse(payload_str: str, msg_type: str, cfg: dict) -> Any | None:
    """统一解析入口。返回 dataclass 实例，解析失败返回 None。"""
    try:
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, TypeError):
        return None

    if msg_type == "sensor":
        return parse_sensor(payload, cfg)
    if msg_type == "impedance":
        return parse_impedance(payload)
    if msg_type == "status":
        return parse_status(payload)
    if msg_type == "heartbeat":
        return parse_heartbeat(payload)
    if msg_type == "prediction":
        return parse_prediction(payload)
    if msg_type == "impedance_raw":
        return parse_impedance_raw(payload)
    if msg_type == "sweep":
        return parse_sweep_points(payload)
    if msg_type == "spectrum":
        return parse_spectrum(payload)
    return None


def parse_sweep(payload: dict) -> list[SweepPointData]:
    """``parse_sweep_points`` 的别名，供既有脚本沿用。"""
    return parse_sweep_points(payload)


def parse_spectrum(payload: dict) -> ImpedanceSpectrum:
    """解析 ``spectrum`` 整谱报文（节点组装完成后的整段推送）。"""
    points: list[SpectrumPoint] = []
    for raw in payload.get("points") or []:
        if not isinstance(raw, dict):
            continue
        zr = float(raw.get("re", raw.get("z_real", 0.0)))
        zi = float(raw.get("im", raw.get("z_imag", 0.0)))
        points.append(
            SpectrumPoint(
                frequency_hz=float(raw.get("freq", raw.get("frequency_hz", 0.0))),
                z_real=zr,
                z_imag=zi,
                magnitude=raw.get("imp", raw.get("magnitude")),
                phase_deg=raw.get("phase_deg", raw.get("phase")),
            )
        )
    for p in points:
        if p.magnitude is None:
            p.magnitude = math.hypot(p.z_real, p.z_imag)
        if p.phase_deg is None:
            p.phase_deg = math.degrees(math.atan2(-p.z_imag, p.z_real))
    return ImpedanceSpectrum(
        gateway_id=payload.get("gateway_id", ""),
        node_id=payload.get("node_id", payload.get("node", "")),
        scan_id=int(payload.get("scan_id", payload.get("round", 0))),
        timestamp=int(payload.get("timestamp", payload.get("ts", 0))),
        points=points,
        meta={k: v for k, v in payload.items() if k.startswith("ctx_")},
    ).sort_by_frequency()
