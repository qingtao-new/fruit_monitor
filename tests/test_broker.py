"""broker_lib 的 PUBLISH 处理回归测试。

重点防 QoS 1/2 报文里那个 2 字节 Packet Identifier：以前没跳过它，
转发出去的 payload 头部会多出 ``\\x00\\x07``，JSON 变成垃圾，
PC 侧一条消息都解不出来（网关端却以为自己发成功了）。
"""
from __future__ import annotations

import json
import logging
import struct
import threading
import time
import unittest
from types import SimpleNamespace

import broker_lib as bl
import paho.mqtt.client as mqtt

logging.getLogger("broker_lib").setLevel(logging.CRITICAL)


def _publish_body(topic: str, payload: bytes, qos: int, pid: int = 7) -> bytes:
    body = struct.pack("!H", len(topic)) + topic.encode("utf-8")
    if qos >= 1:
        body += struct.pack("!H", pid)
    return body + payload


class _Subscriber:
    addr = ("127.0.0.1", 5000)

    def __init__(self) -> None:
        self.forwarded: list[tuple[str, bytes]] = []

    def forward_publish(self, topic: str, payload: bytes) -> bool:
        self.forwarded.append((topic, bytes(payload)))
        return True


HEARTBEAT = {"type": "heartbeat", "gateway_id": "GW_001", "ip": "192.168.1.102"}


class PublishQosTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sub = _Subscriber()
        self.pub = bl.Client(None, ("127.0.0.1", 1234), SimpleNamespace(subs=[]))
        self.pub.broker.subs.append(bl.Sub(self.sub, "fruit/+/+/+"))

    def _handle(self, topic: str, payload: bytes, qos: int) -> None:
        self.pub.handle_publish(_publish_body(topic, payload, qos), qos=qos)

    def test_qos0_payload_intact(self):
        payload = json.dumps(HEARTBEAT).encode("utf-8")
        self._handle("fruit/GW_001/LORA_NODE_01/status", payload, qos=0)
        self.assertEqual(self.sub.forwarded[0][1], payload)

    def test_qos1_packet_identifier_is_stripped(self):
        payload = json.dumps(HEARTBEAT).encode("utf-8")
        self._handle("fruit/GW_001/LORA_NODE_01/status", payload, qos=1)
        self.assertEqual(self.sub.forwarded[0][1], payload)
        self.assertEqual(json.loads(self.sub.forwarded[0][1]), HEARTBEAT)

    def test_qos2_packet_identifier_is_stripped(self):
        payload = json.dumps(HEARTBEAT).encode("utf-8")
        self._handle("fruit/GW_001/LORA_NODE_01/sensor", payload, qos=2)
        self.assertEqual(json.loads(self.sub.forwarded[0][1]), HEARTBEAT)

    def test_qos1_sends_puback_to_publisher(self):
        acked: list[int] = []
        self.pub.send_puback = acked.append
        payload = json.dumps(HEARTBEAT).encode("utf-8")
        self._handle("fruit/GW_001/LORA_NODE_01/status", payload, qos=1)
        self.assertEqual(acked, [7])

    def test_qos0_sends_no_puback(self):
        acked: list[int] = []
        self.pub.send_puback = acked.append
        self._handle("fruit/GW_001/LORA_NODE_01/status", b'{"a":1}', qos=0)
        self.assertEqual(acked, [])

    def test_qos1_truncated_body_is_dropped(self):
        body = struct.pack("!H", 8) + b"fruit/G"   # 主题都没凑齐
        self.pub.handle_publish(body, qos=1)
        self.assertEqual(self.sub.forwarded, [])

    def test_qos1_skips_empty_pid_too(self):
        topic = "fruit/G/A/sensor"
        payload = b'{"a":1}'
        body = (struct.pack("!H", len(topic)) + topic.encode("utf-8")
                + struct.pack("!H", 0) + payload)
        self.pub.handle_publish(body, qos=1)
        self.assertEqual(self.sub.forwarded[0][1], payload)

    def test_malformed_topic_length_is_dropped(self):
        # 声称 200 字节主题，实际只有几个字节：畸形包不能把 payload 读成 topic。
        body = struct.pack("!H", 200) + b"fruit/G/A"
        self.pub.handle_publish(body, qos=1)
        self.assertEqual(self.sub.forwarded, [])

    def test_not_forwarded_to_non_matching_filter(self):
        self.pub.broker.subs.append(bl.Sub(_Subscriber(), "fruit/GW_001/LORA_NODE_01/status"))
        other = self.pub.broker.subs[-1].client
        self._handle("fruit/GW_001/LORA_NODE_01/sensor", b'{"a":1}', qos=1)
        self.assertEqual(other.forwarded, [])


class BrokerSocketTest(unittest.TestCase):
    """走真实 TCP 握手 + paho，确认 QoS1 报文能原样转给订阅方。"""

    def setUp(self) -> None:
        self.host = "127.0.0.1"
        self.broker = bl.Broker(self.host, 0)
        self.port = self.broker.server.getsockname()[1]
        for _ in range(2):
            threading.Thread(target=self._accept, daemon=True).start()
        self.addCleanup(self._close)

    def _accept(self) -> None:
        try:
            sock, addr = self.broker.server.accept()
        except OSError:
            return
        client = bl.Client(sock, addr, self.broker)
        self.broker.clients.append(client)
        client.start()

    def _close(self) -> None:
        try:
            self.broker.server.close()
        except OSError:
            pass

    def _client(self, cid: str):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        c.connect(self.host, self.port, 30)
        return c

    def test_qos1_publish_arrives_unchanged(self):
        sub = self._client("qos1_sub")
        got: list[bytes] = []
        sub.on_message = lambda c, u, m: got.append(bytes(m.payload))
        sub.loop_start()
        time.sleep(0.2)
        sub.subscribe("fruit/+/+/+", qos=1)
        time.sleep(0.2)

        pub = self._client("qos1_pub")
        pub.loop_start()
        time.sleep(0.2)
        payload = json.dumps(HEARTBEAT).encode("utf-8")
        pub.publish("fruit/GW_001/LORA_NODE_01/status", payload, qos=1)

        deadline = time.time() + 5
        while not got and time.time() < deadline:
            time.sleep(0.05)

        pub.loop_stop()
        sub.loop_stop()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], payload)

    def test_qos0_publish_arrives_unchanged(self):
        sub = self._client("qos0_sub")
        got: list[bytes] = []
        sub.on_message = lambda c, u, m: got.append(bytes(m.payload))
        sub.loop_start()
        time.sleep(0.2)
        sub.subscribe("fruit/#", qos=0)
        time.sleep(0.2)

        pub = self._client("qos0_pub")
        pub.loop_start()
        time.sleep(0.2)
        payload = json.dumps({"type": "sensor", "v": 1}).encode("utf-8")
        pub.publish("fruit/GW_001/LORA_NODE_01/sensor", payload, qos=0)

        deadline = time.time() + 5
        while not got and time.time() < deadline:
            time.sleep(0.05)

        pub.loop_stop()
        sub.loop_stop()
        self.assertEqual(got, [payload])


if __name__ == "__main__":
    unittest.main()
