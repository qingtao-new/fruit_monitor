"""模拟发布器：向真实 MQTT Broker 发布传感器、阻抗、状态数据。"""
from __future__ import annotations

import argparse
import json
import math
import random
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from mqtt_client import IMPEDANCE_FREQS, R0_END, R0_START, R_INF, RIPEN_SECONDS, _impedance_at


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "config.json"


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="向 MQTT Broker 发布模拟数据")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--gateway", default=None)
    parser.add_argument("--node", default=None)
    parser.add_argument("--interval-ms", type=int, default=None)
    parser.add_argument("--impedance-interval-ms", type=int, default=None)
    parser.add_argument("--no-impedance", action="store_true")
    parser.add_argument("--count", type=int, default=0, help="发布次数，0 表示无限循环")
    return parser.parse_args()


def build_payload(
    gateway_id: str,
    node_id: str,
    msg_type: str,
    extra: dict,
) -> str:
    payload = {
        "gateway_id": gateway_id,
        "node_id": node_id,
        "timestamp": int(time.time()),
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    mqtt_cfg = config.get("mqtt", {})
    sim_cfg = config.get("simulator", {})

    host = args.host or mqtt_cfg.get("host", "127.0.0.1")
    port = args.port or int(mqtt_cfg.get("port", 1883))
    client_id = mqtt_cfg.get("client_id", "fruit_monitor_publisher_sim")
    keepalive = int(mqtt_cfg.get("keepalive", 60))
    qos = int(mqtt_cfg.get("qos", 1))
    gateways = sim_cfg.get("gateways", ["GW_001"])
    nodes = sim_cfg.get("nodes", ["LORA_NODE_01"])
    gateway_id = args.gateway or gateways[0]
    node_id = args.node or nodes[0]

    interval_s = (args.interval_ms or sim_cfg.get("interval_ms", 2000)) / 1000.0
    imp_interval_s = (args.impedance_interval_ms or sim_cfg.get("impedance_interval_ms", 15000)) / 1000.0
    publish_imp = not args.no_impedance and bool(sim_cfg.get("publish_impedance", True))

    temp = 24.0
    humidity = 65.0
    soil = 50.0
    nh3 = 10.0
    h2s = 5.0
    co2 = 450.0
    ph = 6.5
    session_start = time.time()
    last_imp = 0.0
    count = 0

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except AttributeError:
        client = mqtt.Client(client_id=client_id)

    if mqtt_cfg.get("username"):
        client.username_pw_set(mqtt_cfg["username"], mqtt_cfg.get("password"))

    client.connect(host, port, keepalive)
    client.loop_start()

    client.publish(
        f"fruit/{gateway_id}/{node_id}/status",
        build_payload(gateway_id, node_id, "status", {"status": "online"}),
        qos=qos,
    )

    try:
        while True:
            temp = max(20.0, min(30.0, temp + random.gauss(0, 0.1)))
            humidity = max(40.0, min(85.0, humidity + random.gauss(0, 0.3)))
            soil = max(20.0, min(80.0, soil + random.gauss(0, 0.5)))
            nh3 = max(0.0, min(50.0, nh3 + random.gauss(0, 0.5)))
            h2s = max(0.0, min(30.0, h2s + random.gauss(0, 0.3)))
            co2 = max(350.0, min(800.0, co2 + random.gauss(0, 2.0)))
            ph = max(5.0, min(8.0, ph + random.gauss(0, 0.01)))

            sensor_payload = build_payload(
                gateway_id,
                node_id,
                "sensor",
                {
                    "temperature": round(temp, 2),
                    "humidity": round(humidity, 1),
                    "soil_moisture": round(soil, 1),
                    "nh3": round(nh3, 1),
                    "h2s": round(h2s, 1),
                    "co2": int(round(co2)),
                    "ph": round(ph, 2),
                },
            )
            client.publish(f"fruit/{gateway_id}/{node_id}/sensor", sensor_payload, qos=qos)
            count += 1

            if publish_imp:
                now = time.time()
                if now - last_imp >= imp_interval_s:
                    last_imp = now
                    scan_id = f"SCAN_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                    elapsed = now - session_start
                    progress = max(0.0, min(1.0, elapsed / RIPEN_SECONDS))
                    r0 = R0_START - (R0_START - R0_END) * progress
                    for freq in IMPEDANCE_FREQS:
                        zr, zi = _impedance_at(freq, r0)
                        zr *= 1 + random.gauss(0, 0.004)
                        zi *= 1 + random.gauss(0, 0.006)
                        magnitude = math.hypot(zr, zi)
                        phase = -math.degrees(math.atan2(zi, zr))
                        impedance_payload = build_payload(
                            gateway_id,
                            node_id,
                            "impedance",
                            {
                                "scan_id": scan_id,
                                "frequency_hz": float(freq),
                                "z_real": round(zr, 2),
                                "z_imag": round(zi, 2),
                                "magnitude": round(magnitude, 2),
                                "phase": round(phase, 2),
                                "rcal_ohm": 51000.0,
                                "in_valid_window": bool(1000 <= freq <= 30000),
                            },
                        )
                        client.publish(f"fruit/{gateway_id}/{node_id}/impedance", impedance_payload, qos=qos)

            if args.count and count >= args.count:
                break
            time.sleep(interval_s)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()

    print(f"published {count} sensor messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
