# 本地 MQTT 联调操作说明

这份文档用于在没有真实硬件时，先用本地 MQTT Broker 跑通“模拟发布 -> PC 接收 -> 分析预测”链路。

## 1. 相关文件

- `run.bat`：一键启动模拟 GUI
- `mqtt_test.bat`：一键做本地 MQTT 链路验证
- `publisher_sim.py`：模拟 MQTT 发布器
- `mqtt_subscriber.py`：本地 MQTT 订阅器，打印收到的消息
- `main.py --mqtt`：PC 端 MQTT 接收与显示
- `analysis.py`：阻抗分析与成熟度预测

## 2. 最短启动方式

```powershell
cd E:\opencode\fruit_monitor
.\.venv\Scripts\python.exe main.py --simulator
```

这只是本地模拟，不需要 Broker。

## 3. 本地 MQTT 联调

### 3.1 启动 Broker

```powershell
.\.venv\Scripts\amqtt.exe -d
```

默认地址：

- host: `127.0.0.1`
- port: `1883`

### 3.2 启动订阅器

```powershell
.\.venv\Scripts\python.exe mqtt_subscriber.py
```

它会订阅：

- `fruit/+/+/+`

并在控制台打印收到的消息。

### 3.3 启动模拟器发布器

```powershell
.\.venv\Scripts\python.exe publisher_sim.py --host 127.0.0.1 --port 1883 --gateway GW_001 --node LORA_NODE_01
```

### 3.4 启动 PC 端 MQTT 接收

```powershell
.\.venv\Scripts\python.exe main.py --mqtt
```

如果界面能打开，并且曲线/数值开始刷新，就说明本地 MQTT 链路通了。

## 4. 一键验证

不想手动逐条执行时，直接运行：

```powershell
.\mqtt_test.bat
```

它会依次拉起：

- Broker
- 订阅器
- 模拟器发布器
- PC GUI
- 分析检查

## 5. 运行分析预测

```powershell
# 打出可用谱的 sample_id，用来填标签表
.\.venv\Scripts\python.exe analysis.py --gateway GW_001 --node LORA_NODE_01 --dump-samples

# 有真实标签后
.\.venv\Scripts\python.exe analysis.py --gateway GW_001 --node LORA_NODE_01 --labels labels.csv
```

模型名与 `maturity_ml.AVAILABLE_MODELS` 一致：

- 分类：`baseline` `logreg` `ridge_clf` `rf`
- 回归：`baseline` `ridge` `rf`

`--labels` 是**必填**的。旧版可不给标签就跑，靠时间顺序编造标签，
那是循环论证，已移除。详见 [methodology.md](methodology.md)。

## 6. 协议约定

### 主题格式

```text
fruit/{gateway_id}/{node_id}/{msg_type}
```

常见 `msg_type`：

- `sensor`：标量环境量
- `impedance`：单频点（历史格式，保留兼容）
- `sweep`：一整段扫频，`freq/re/im/imp` 并行数组
- `impedance_raw`：AD5933 分包扫频，PC 侧自动拼装成整谱
- `spectrum`：完整阻抗谱
- `status`：心跳 / 轮次完成摘要
- `prediction`：成熟度预测

主题里声明的类型优先；若固件把 `sweep` 发到 `/sensor` 主题，报文内
的 `"type"` 字段会覆盖主题类型（`mqtt_client.resolve_msg_type`）。

### 分帧约定

所有 MQTT payload 与串口上行都以 `\n` 结尾。PC 侧 `protocol.LineFramer`
按行切帧、逐帧校验 JSON，粘包 / 半帧 / 超长帧都不会打断后续数据。

### sensor 示例

```json
{
  "type": "sensor",
  "gateway_id": "GW_001",
  "node_id": "LORA_NODE_01",
  "timestamp": 1788863789,
  "temperature": 25.1,
  "humidity": 65,
  "soil_moisture": 52,
  "nh3": 10,
  "h2s": 5,
  "co2": 450,
  "ph": 6.5
}
```

### sweep 示例（网关分段上报）

```json
{
  "type": "sweep",
  "gateway_id": "GW_001",
  "node_id": "LORA_NODE_01",
  "report_id": "GW_001/LORA_NODE_01/R7",
  "round": 7,
  "seg": 0,
  "seg_total": 2,
  "point_start": 0,
  "point_count": 50,
  "timestamp": 1788863789,
  "freq": [1000, 1010, 1020],
  "re": [1980, 1978, 1975],
  "im": [-420, -421, -423],
  "imp": [2026, 2027, 2030],
  "temperature": [25.1, 25.1, 25.2]
}
```

`imp` 省略时 PC 侧按 `sqrt(re^2 + im^2)` 补算。环境量字段可省略，
PC 会用最近一次 `sensor` 帧的值补齐上下文。

### impedance_raw 示例（节点分包上报）

```json
{
  "type": "impedance_raw",
  "gateway_id": "GW_001",
  "node_id": "LORA_NODE_01",
  "timestamp": 1788863789,
  "scan_id": 7,
  "packet_index": 0,
  "packet_total": 2,
  "point_start": 0,
  "point_count": 50,
  "frequency_start_hz": 1000,
  "frequency_increment_hz": 100,
  "points": [{"i": 0, "re": 1980, "im": -420}, {"i": 1, "re": 1978, "im": -421}]
}
```

两种频点表达方式：

1. 等差：给 `frequency_start_hz` + `frequency_increment_hz`
2. 对数：给 `frequencies` 数组，或每个点自带 `"f"` 字段

第 2 种必须把 `frequency_increment_hz` 设为 `0`。分包乱序到达也没关系，
PC 侧 `SpectrumAssembler` 按 `scan_id` 分桶拼装，最后一个包到位即出整谱；
超时未集齐的扫描会被丢弃并计入 `bad_packets`。

### status 示例

```json
{
  "type": "status",
  "gateway_id": "GW_001",
  "node_id": "LORA_NODE_01",
  "status": "heartbeat",
  "timestamp": 1788863789
}
```

一轮扫频收满时网关会额外发一条 `status: "round_done"`，附带
`round` / `points` / `total` / `retry` / `imp_mean`。

### impedance 建议格式

```json
{
  "gateway_id": "GW_001",
  "node_id": "LORA_NODE_01",
  "timestamp": 1788863789,
  "scan_id": "SCAN_20260908192432",
  "frequency_hz": 1000,
  "z_real": 987.65,
  "z_imag": -123.45,
  "magnitude": 995.3,
  "phase": -7.15,
  "rcal_ohm": 51000,
  "in_valid_window": true
}
```

## 7. 本地调试注意事项

- `127.0.0.1` 只能在本机使用
- 如果要让局域网里的 ESP01 / 网关直连你的电脑，请改成电脑局域网 IP，例如 `192.168.x.x`
- OneNET 的上报配置不要改，建议额外并行发一份到本地 MQTT Broker

## 8. 快速检查顺序

```powershell
.\.venv\Scripts\python.exe -m py_compile main.py mqtt_client.py gui.py db.py protocol.py spectrum.py maturity.py analysis.py publisher_sim.py mqtt_subscriber.py
```

单元测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

如果编译通过，再运行：

```powershell
.\mqtt_test.bat
```

这就是本地联调的最短路径。
