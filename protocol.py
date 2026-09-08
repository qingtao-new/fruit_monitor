"""消息协议定义：数据模型、校验、Topic 解析。"""
from __future__ import annotations

import json
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

VALID_MSG_TYPES = {"sensor", "impedance", "status", "prediction"}


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


@dataclass
class PredictionData:
    node_id: Optional[str]
    timestamp: Optional[int]
    maturity: Optional[float]
    maturity_level: Optional[str]
    harvest_date: Optional[str]
    confidence: Optional[float]


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
    )


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
    if msg_type == "prediction":
        return parse_prediction(payload)
    return None
