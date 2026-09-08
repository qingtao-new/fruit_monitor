"""轻量 MQTT 订阅工具：订阅 fruit/+/+/+ 并打印消息，用于链路验证。"""
from __future__ import annotations

import json
import time
from datetime import datetime

import paho.mqtt.client as mqtt


HOST = "127.0.0.1"
PORT = 1883
TOPIC = "fruit/+/+/+"
CLIENT_ID = "fruit_monitor_subscriber"


def _on_connect(client: mqtt.Client, _userdata, _flags, rc: int) -> None:
    print(f"connected rc={rc}", flush=True)
    client.subscribe(TOPIC, qos=1)


def _on_message(client: mqtt.Client, _userdata, msg) -> None:
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
    except Exception:
        payload = {"raw": msg.payload.decode("utf-8", errors="replace")}
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {topic} {json.dumps(payload, ensure_ascii=False)}", flush=True)


def main() -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID)
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect(HOST, PORT, keepalive=60)
    client.loop_start()
    print("subscriber ready", flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
