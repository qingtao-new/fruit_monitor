"""真实 MQTT 上报一键入口：Broker + GUI 同进程。

解决的问题
----------
原先 start.bat 用 ``amqtt.exe -d`` 开独立窗口当 Broker，start_real_mqtt.bat
干脆没起 Broker。Broker 一崩或没起，端口上没有任何进程，
Windows 直接回 RST → "目标计算机积极拒绝"。

现在 Broker 作为线程跑在 GUI 进程内部，并且启动前做真实 TCP 握手，
握手没通过就不放行 GUI，从程序层面根除该错误。

用法
----
    python real_mqtt.py             # 预检 + 起 Broker + 开 GUI
    python real_mqtt.py --check     # 只跑预检 + 订阅 10 秒后退出（不上 GUI）
    python real_mqtt.py --port 1883 --no-gui --secs 30
"""
from __future__ import annotations

import argparse
import importlib
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import broker_lib as bl

HERE = Path(__file__).resolve().parent

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

LINE = "=" * 66
ESP_EXPECTED_IP = "192.168.1.102"        # 固件 LOCAL_MQTT_HOST 写死值
GUI_TOPIC = "fruit/+/+/+"

# 与固件 publishToLocal() 严格一致的报文
ESP_SENSOR_TOPIC = "fruit/GW_001/LORA_NODE_01/sensor"
ESP_SENSOR_PAYLOAD = json.dumps({
    "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
    "timestamp": int(time.time()),
    "soil_moisture": 23, "temperature": 26.8, "nh3": 18,
    "h2s": 5, "co2": 412, "ph": 6.2, "humidity": 68,
}, ensure_ascii=False, separators=(",", ":"))


def sep(title: str) -> None:
    print("\n" + LINE)
    print(title)
    print(LINE)


# ---------------- 依赖检查 ----------------
DEPS = ["paho.mqtt.client", "PySide6.QtCore", "pyqtgraph", "numpy", "pandas"]
DEPS_PIP = {"PySide6": "PySide6", "pyqtgraph": "PyQtGraph",
            "paho": "paho-mqtt", "numpy": "numpy", "pandas": "pandas"}


def check_deps() -> None:
    missing = []
    for mod in DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod.split(".")[0])
    if missing:
        print(f"[错误] 缺少依赖: {', '.join(missing)}")
        print("       请在 .venv 中执行：")
        print("       .venv\\Scripts\\python.exe -m pip install "
              + " ".join(DEPS_PIP[m] for m in missing))
        sys.exit(1)
    print("  依赖检查 ✔  " + ", ".join(DEPS))


# ---------------- 端口预检 ----------------
def ensure_broker(port: int) -> threading.Thread:
    """返回 broker 线程；端口已占用则复用不重复起。"""
    if bl.is_port_open(port):
        print(f"  端口 {port} 已有进程在监听，直接复用")
        return threading.Thread()          # 占位线程，is_alive() 为 False 但不影响

    print(f"  端口 {port} 无监听 → 本进程自动拉起内嵌 Broker")
    th = threading.Thread(target=bl.serve_forever, args=(bl.BIND_HOST, port),
                          daemon=True, name="broker")
    th.start()
    for _ in range(40):                    # 最多等 10 秒
        time.sleep(0.25)
        if bl.is_port_open(port):
            print(f"       已就绪 {bl.BIND_HOST}:{port}  真实 TCP 握手通过 ✔")
            return th
    print(f"  [错误] Broker 未能在 10 秒内监听 {port}")
    print("         端口占用情况：")
    for line in bl.who_is_using_port(port) or ["(无 LISTENING 进程)"]:
        print("         " + line)
    sys.exit(1)


def verify_ips(port: int) -> list[str]:
    ok = []
    for ip in ["127.0.0.1"] + bl.get_local_ips():
        good = bl.is_port_open(port, host=ip)
        print(f"       {ip}:{port}  {'✔ 可达' if good else '✘ 不可达'}")
        if good:
            ok.append(ip)
    return ok


def check_firewall(port: int) -> bool:
    r = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule",
         "dir=in", "name=MQTT-%d" % port],
        capture_output=True, text=True)
    if "MQTT-%d" % port in (r.stdout or ""):
        print(f"  防火墙入站规则 MQTT-{port} 已存在 ✔")
        return True
    return False


def print_esp_hint(ips: list[str]) -> None:
    lan = [i for i in ips if i != "127.0.0.1"]
    print("\n  ESP8266 侧配置（firmware）")
    print("  " + "-" * 48)
    if ESP_EXPECTED_IP in ips:
        print(f'  const char* LOCAL_MQTT_HOST = "{ESP_EXPECTED_IP}";   // 本机已匹配 ✔')
    elif lan:
        print(f'  const char* LOCAL_MQTT_HOST = "{ESP_EXPECTED_IP}";')
        print(f"  !! 本机 IP 是 {', '.join(lan)}，与固件写死值不符")
        print(f'     请把固件改成 "{lan[0]}"，或把本机 IP 设为 {ESP_EXPECTED_IP}')
    print(f"  const uint16_t LOCAL_MQTT_PORT = 1883;")
    print(f"  订阅主题: {GUI_TOPIC}")


# ---------------- 自检（不上 GUI） ----------------
def run_check(port: int, secs: int, publish_esp: bool = False) -> int:
    import paho.mqtt.client as mqtt

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                      client_id="real_mqtt_check")
    got = []

    def on_msg(_c, _u, msg):
        got.append((msg.topic, msg.payload.decode("utf-8", "replace")))

    cli.on_message = on_msg
    cli.connect("127.0.0.1", port, keepalive=60)
    cli.loop_start()
    cli.subscribe(GUI_TOPIC, qos=1)
    print(f"\n  已订阅 {GUI_TOPIC}，等待 ESP 上报 {secs} 秒 ...")

    # 可选：发一条与固件完全一致的报文，验证链路 + 协议解析
    pub = None
    if publish_esp:
        pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                          client_id="esp_simulator")
        pub.connect("127.0.0.1", port, keepalive=60)
        pub.loop_start()
        time.sleep(0.4)
        print(f"\n  发送固件同款报文 -> {ESP_SENSOR_TOPIC}")
        print(f"     {ESP_SENSOR_PAYLOAD}")
        pub.publish(ESP_SENSOR_TOPIC, ESP_SENSOR_PAYLOAD, qos=0)

    t_end = time.time() + secs
    n = 0
    while time.time() < t_end:
        if len(got) > n:
            for topic, raw in got[n:]:
                print(f"  [{time.strftime('%H:%M:%S')}] {topic}")
                print(f"     {raw}")
            n = len(got)
        time.sleep(0.3)

    cli.loop_stop()
    cli.disconnect()
    if pub:
        pub.loop_stop()
        pub.disconnect()

    # 用 GUI 同一套协议解析，确认报文能被界面正确吃进去
    if got:
        try:
            import json as _json
            from protocol import parse_topic, parse_sensor
            cfg = json.loads(
                (HERE / "config" / "config.json").read_text(encoding="utf-8"))
            print("\n  协议解析校验（GUI 同款 protocol.py）")
            print("  " + "-" * 48)
            for topic, raw in got:
                gw, node, mtype = parse_topic(topic)
                payload = _json.loads(raw)
                data = parse_sensor(payload, cfg)
                print(f"  OK  topic -> gateway={gw} node={node} type={mtype}")
                print(f"      temperature={data.temperature} "
                      f"humidity={data.humidity} ph={data.ph} "
                      f"co2={data.co2} nh3={data.nh3} h2s={data.h2s} "
                      f"soil={data.soil_moisture} 异常={data.anomaly_flags}")
        except Exception as exc:
            print(f"\n  协议解析失败：{exc}")
            return 3

    return 0 if got else 2


# ---------------- 主流程 ----------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--check", action="store_true",
                    help="只跑预检 + 订阅，不启动 GUI")
    ap.add_argument("--secs", type=int, default=10, help="--check 等待秒数")
    ap.add_argument("--publish-esp", action="store_true",
                    help="--check 时先发一条固件同款报文验证链路")
    ap.add_argument("--config", type=Path,
                    default=HERE / "config" / "config.json")
    a, extra = ap.parse_known_args()

    sep("真实 MQTT 上报 —— 启动预检")
    check_deps()
    sep(f"MQTT Broker 自检（端口 {a.port}）")
    ensure_broker(a.port)
    ips = verify_ips(a.port)
    if not ips:
        print("  [错误] 本机没有任何可用 IP，退出")
        return 1
    if not check_firewall(a.port):
        print(f"  防火墙：未发现入站规则 MQTT-{a.port}")
        print(f'         请用管理员 PowerShell 执行（ESP 连不上时最常见原因）：')
        print(f'         netsh advfirewall firewall add rule name="MQTT-{a.port}" '
              f"dir=in action=allow protocol=TCP localport={a.port}")
    print_esp_hint(ips)

    if a.check:
        sep(f"订阅自检（{a.secs} 秒）")
        return run_check(a.port, a.secs, a.publish_esp)

    sep("启动图形界面（Broker 与 GUI 同进程）")
    print(f"  配置: {a.config}", flush=True)

    # main.py 读 sys.argv 决定运行模式。必须显式注入 --mqtt，
    # 否则 use_simulator = not args.mqtt 为真，界面会退化成模拟器模式。
    import main as gui_main
    saved_argv = sys.argv[:]
    sys.argv = [saved_argv[0], "--mqtt", f"--config={a.config}"] + extra
    print("  模式: MQTT 真实上报   main.py argv =",
          " ".join(sys.argv[1:]), flush=True)
    try:
        return gui_main.main()
    finally:
        sys.argv = saved_argv


if __name__ == "__main__":
    raise SystemExit(main())
