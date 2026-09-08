"""MQTT 客户端 + 数据模拟器。均基于 QThread，通过 Qt 信号与 GUI 通信。"""
from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime
from typing import Any, Optional

import paho.mqtt.client as mqtt
from PySide6.QtCore import QThread, Signal

from protocol import (
    SensorData,
    ImpedanceData,
    parse_sensor,
    parse_impedance,
    parse_status,
    parse_prediction,
    parse_topic,
)


# --------------------------------------------------------------------------- #
#  MQTT 真实连接
# --------------------------------------------------------------------------- #
class MQTTWorker(QThread):
    """订阅 fruit/+/+/+ 主题，解析后落库并发信号。"""

    data_received = Signal(dict)
    status_changed = Signal(str, str, str)
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

    # ---- 生命周期 ----

    def run(self) -> None:
        self._running = True
        mqtt_cfg = self._config.get("mqtt", {})
        host = mqtt_cfg.get("host", "127.0.0.1")
        port = int(mqtt_cfg.get("port", 1883))
        client_id = mqtt_cfg.get("client_id", "fruit_monitor_pc")
        keepalive = int(mqtt_cfg.get("keepalive", 60))
        qos = int(mqtt_cfg.get("qos", 1))
        topic_filter = mqtt_cfg.get("topic_filter", "fruit/+/+/+")

        try:
            try:
                self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
            except AttributeError:
                self._client = mqtt.Client()

            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message
            self._client.on_disconnect = self._on_disconnect

            if mqtt_cfg.get("username"):
                self._client.username_pw_set(
                    mqtt_cfg["username"], mqtt_cfg.get("password")
                )

            self._client.connect(host, port, keepalive)
            self._client.loop_start()
            self._client.subscribe(topic_filter, qos)
            self.connected.emit(True)

            while self._running:
                time.sleep(0.5)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            self.connected.emit(False)
        finally:
            self._cleanup()

    def stop(self) -> None:
        self._running = False

    def set_recording(self, recording: bool) -> None:
        self._recording = bool(recording)

    def _cleanup(self) -> None:
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def stats(self) -> dict[str, Any]:
        return {
            "messages": self._msg_count,
            "last_msg_time": self._last_msg_time,
        }

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
            gateway_id, node_id, msg_type = parse_topic(msg.topic)
        except ValueError:
            return

        raw = msg.payload.decode("utf-8", errors="replace")

        if msg_type == "sensor":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return
            data = parse_sensor(payload, self._config)
            if self._recording:
                self._db.insert_sensor(data)
            self.data_received.emit({"type": "sensor", "data": data})
            self.status_changed.emit(gateway_id, node_id, "online")

        elif msg_type == "impedance":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return
            data = parse_impedance(payload)
            if self._recording:
                self._db.insert_impedance(data)
            self.data_received.emit({"type": "impedance", "data": data})

        elif msg_type == "status":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return
            data = parse_status(payload)
            if self._recording:
                self._db.insert_status(data)
            self.status_changed.emit(data.gateway_id, data.node_id or "", data.status)

        elif msg_type == "prediction":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return
            data = parse_prediction(payload)
            if self._recording:
                self._db.insert_prediction(data)
            self.data_received.emit({"type": "prediction", "data": data})

        else:
            return

        self._msg_count += 1
        self._last_msg_time = time.time()


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
        scan_id = f"SCAN_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        elapsed = time.time() - self._session_start
        progress = max(0.0, min(1.0, elapsed / RIPEN_SECONDS))
        r0 = R0_START - (R0_START - R0_END) * progress

        for freq in IMPEDANCE_FREQS:
            zr, zi = _impedance_at(freq, r0)
            zr *= 1 + random.gauss(0, 0.004)
            zi *= 1 + random.gauss(0, 0.006)
            mag = math.hypot(zr, zi)
            phase = -math.degrees(math.atan2(zi, zr))

            data = ImpedanceData(
                gateway_id=gateway_id,
                node_id=node_id,
                timestamp=int(time.time()),
                scan_id=scan_id,
                frequency_hz=float(freq),
                z_real=round(zr, 2),
                z_imag=round(zi, 2),
                magnitude=round(mag, 2),
                phase=round(phase, 2),
                rcal_ohm=51000.0,
                in_valid_window=bool(1000 <= freq <= 30000),
            )
            self._db.insert_impedance(data)
            self.data_received.emit({"type": "impedance", "data": data})
            self._msg_count += 1
