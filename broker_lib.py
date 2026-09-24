"""零依赖 MQTT 3.1.1 Broker。

不依赖 amqtt / mosquitto，作为线程嵌入宿主进程运行，
避免"Broker 独立窗口崩溃 → 端口失守 → 目标计算机积极拒绝"。
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
from typing import List, Optional, Tuple

BIND_HOST = "0.0.0.0"
BIND_PORT = 1883

log = logging.getLogger("broker_lib")


# ---------------- 协议工具 ----------------
def encode_len(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length > 0:
            byte |= 0x80
        out.append(byte)
        if length == 0:
            break
    return bytes(out)


def take_utf8(data: bytes, off: int) -> Tuple[Optional[str], int]:
    if off + 1 > len(data):
        return None, off
    (length,) = struct.unpack_from("!H", data, off)
    off += 2
    if off + length > len(data):
        return None, off
    return data[off:off + length].decode("utf-8", "replace"), off + length


def topic_matches(topic_filter: str, topic: str) -> bool:
    if topic_filter == topic:
        return True
    tf, tp = topic_filter.split("/"), topic.split("/")
    for i, part in enumerate(tf):
        if part == "#":
            return i < len(tp) or len(tp) == len(tf) - 1
        if i >= len(tp):
            return False
        if part == "+":
            continue
        if part != tp[i]:
            return False
    return len(tf) == len(tp)


# ---------------- 网络诊断 ----------------
def get_local_ips() -> List[str]:
    ips = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
    except Exception:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            ips.add(info[4][0])
    except Exception:
        pass
    ips.discard("0.0.0.0")
    return sorted(ips)


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """真实 TCP 握手：确认端口上真的有进程在听，而不只是 netstat 有行。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def who_is_using_port(port: int) -> List[str]:
    out = os.popen(
        'netstat -ano -p TCP | findstr ":%d " | findstr LISTENING' % port
    ).read()
    return out.strip().splitlines()


# ---------------- 订阅条目 ----------------
class Sub:
    __slots__ = ("client", "topic_filter")

    def __init__(self, client: "Client", topic_filter: str):
        self.client = client
        self.topic_filter = topic_filter


# ---------------- 客户端连接 ----------------
class Client(threading.Thread):
    def __init__(self, sock, addr, broker: "Broker"):
        super().__init__(daemon=True)
        self.sock = sock
        self.addr = addr
        self.broker = broker
        self.cid = ""
        self._lock = threading.Lock()

    def _recv(self, n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except Exception:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _send(self, data: bytes) -> bool:
        with self._lock:
            try:
                self.sock.sendall(data)
                return True
            except Exception:
                return False

    def read_packet(self):
        hdr = self._recv(1)
        if hdr is None:
            return None
        first = hdr[0]
        ptype = first >> 4
        mult, length, i = 1, 0, 0
        while True:
            if i > 4:
                return None
            b = self._recv(1)
            if b is None:
                return None
            byte = b[0]
            i += 1
            length += (byte & 0x7F) * mult
            mult *= 128
            if not (byte & 0x80):
                break
        body = self._recv(length) if length else b""
        if body is None:
            return None
        return ptype, first & 0x0F, body

    def _wrap(self, header: bytes, body: bytes = b"") -> bytes:
        return header + encode_len(len(body)) + body

    def send_connack(self, code: int = 0) -> None:
        self._send(b"\x20\x02\x00" + bytes([code]))

    def send_puback(self, pid: int) -> None:
        self._send(self._wrap(b"\x40", struct.pack("!H", pid)))

    def send_suback(self, pid: int, grants) -> None:
        self._send(self._wrap(b"\x90", struct.pack("!H", pid) + bytes(grants)))

    def send_unsuback(self, pid: int) -> None:
        self._send(self._wrap(b"\xB0", struct.pack("!H", pid)))

    def send_pingresp(self) -> None:
        self._send(b"\xD0\x00")

    def forward_publish(self, topic: str, payload: bytes) -> bool:
        t = topic.encode("utf-8")
        body = struct.pack("!H", len(t)) + t + payload
        return self._send(self._wrap(b"\x30", body))

    # ---- 报文处理 ----
    def handle_connect(self, body: bytes) -> bool:
        """协议名 → 级别 → flags → keepalive → clientId → [will] → [user/pwd]"""
        try:
            (nlen,) = struct.unpack_from("!H", body, 0)
            off = 2 + nlen + 1              # 协议名 + 协议级别
            flags = body[off]
            off += 3                         # flags + keepalive
            self.cid, off = take_utf8(body, off)
            if not self.cid:
                return False
            if flags & 0x04:
                _, off = take_utf8(body, off)
                _, off = take_utf8(body, off)
            if flags & 0x80:
                _, off = take_utf8(body, off)
            if flags & 0x40:
                _, off = take_utf8(body, off)
        except Exception as exc:
            log.warning("%s CONNECT 解析异常: %s", self.addr, exc)
            self.send_connack(4)
            return False
        self.send_connack(0)
        log.info("%s 已连接 clientId=%r", self.addr, self.cid)
        return True

    def handle_subscribe(self, body: bytes) -> None:
        if len(body) < 2:
            return
        (pid,) = struct.unpack_from("!H", body, 0)
        off, grants = 2, []
        while off < len(body):
            topic, off = take_utf8(body, off)
            if not topic:
                break
            sub_qos = body[off] if off < len(body) else 0
            off += 1
            if not any(s.client is self and s.topic_filter == topic
                       for s in self.broker.subs):
                self.broker.subs.append(Sub(self, topic))
            grants.append(min(sub_qos, 0))
        self.send_suback(pid, grants)
        log.info("%s 订阅: %s", self.addr,
                 ", ".join(s.topic_filter for s in self.broker.subs
                           if s.client is self))

    def handle_unsubscribe(self, body: bytes) -> None:
        if len(body) < 2:
            return
        (pid,) = struct.unpack_from("!H", body, 0)
        off = 2
        while off < len(body):
            topic, off = take_utf8(body, off)
            if not topic:
                break
            self.broker.subs = [s for s in self.broker.subs
                                if not (s.client is self
                                        and s.topic_filter == topic)]
        self.send_unsuback(pid)

    def handle_publish(self, body: bytes, qos: int = 0) -> None:
        if len(body) < 2:
            return
        (tlen,) = struct.unpack_from("!H", body, 0)
        # 主题长度超过剩余报文就是畸形包，直接丢掉，别把垃圾字节读进 topic/payload。
        if 2 + tlen > len(body):
            return
        off = 2
        topic = body[off:off + tlen].decode("utf-8", "replace")
        off += tlen
        # QoS 1/2 的 PUBLISH 在主题后面多一个 2 字节 Packet Identifier，
        # 不跳过会让它混进 payload 头部，整帧 JSON 变成垃圾，PC 侧一条都解不出来。
        if qos >= 1:
            if off + 2 > len(body):
                return
            (pid,) = struct.unpack_from("!H", body, off)
            off += 2
        payload = body[off:]
        for sub in list(self.broker.subs):
            if sub.client is self or sub.client is None:
                continue
            if topic_matches(sub.topic_filter, topic):
                if sub.client.forward_publish(topic, payload):
                    log.debug("→ %s (%s)", sub.client.addr, sub.topic_filter)
        if qos >= 1:
            # 不回 PUBACK，ESP 侧 PubSubClient 会当成丢包反复重发同一帧。
            self.send_puback(pid)

    def run(self) -> None:
        self.sock.settimeout(300)
        try:
            while True:
                pkt = self.read_packet()
                if pkt is None:
                    break
                ptype, flags, body = pkt
                if ptype == 1:
                    if not self.handle_connect(body):
                        break
                elif ptype == 8:
                    self.handle_subscribe(body)
                elif ptype == 10:
                    self.handle_unsubscribe(body)
                elif ptype == 3:
                    self.handle_publish(body, qos=(flags >> 1) & 0x03)
                elif ptype == 12:
                    self.send_pingresp()
                elif ptype == 14:
                    log.info("%s 断开连接", self.addr)
                    break
                else:
                    log.warning("%s 未知报文类型 0x%02X", self.addr, ptype)
        finally:
            try:
                self.sock.close()
            except Exception:
                pass
            self.broker.on_client_gone(self)


# ---------------- Broker ----------------
class Broker:
    def __init__(self, host: str = BIND_HOST, port: int = BIND_PORT):
        self.subs: List[Sub] = []
        self.clients: List[Client] = []
        self.host, self.port = host, port
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((host, port))
        self.server.listen(16)

    def on_client_gone(self, client: Client) -> None:
        self.subs = [s for s in self.subs if s.client is not client]
        self.clients = [c for c in self.clients if c is not client]

    def run(self) -> None:
        log.info("=" * 60)
        log.info("Broker 监听 %s:%d", self.host, self.port)
        log.info("本机 IP: %s", ", ".join(get_local_ips()))
        log.info("=" * 60)
        while True:
            try:
                sock, addr = self.server.accept()
            except KeyboardInterrupt:
                return
            except OSError as exc:
                log.error("accept 中断: %s", exc)
                time.sleep(0.5)
                continue
            except Exception as exc:
                log.error("accept 异常: %s", exc)
                continue
            client = Client(sock, addr, self)
            self.clients.append(client)
            client.start()


def serve_forever(host: str = BIND_HOST, port: int = BIND_PORT,
                  retry: float = 1.0) -> None:
    """线程入口：绑定失败或意外崩溃都自动重试，保证端口始终有进程在听。"""
    while True:
        try:
            Broker(host, port).run()
        except KeyboardInterrupt:
            return
        except OSError as exc:
            log.error("绑定 %s:%d 失败: %s\n%s", host, port, exc,
                      "\n".join(who_is_using_port(port)) or "(无占用)")
            time.sleep(retry)
        except Exception as exc:
            log.error("Broker 意外退出: %r，%.0f 秒后自动重启", exc, retry)
            time.sleep(retry)
