# fruit_monitor 快速启动与查看效果

## 1. 进入项目目录

```powershell
cd E:\opencode\fruit_monitor
```

## 2. 准备 Python 环境

项目已经带有一个本地虚拟环境：

```powershell
.\.venv\Scripts\python.exe
```

如果你需要重新安装依赖，先确认虚拟环境可正常使用，再按项目依赖安装即可。

## 3. 用模拟模式启动 GUI

```powershell
.\.venv\Scripts\python.exe main.py --simulator
```

启动后会直接看到界面。

说明：
- `--simulator` 使用内置模拟器，不需要连接真实 MQTT Broker
- 程序会持续生成传感器数据和阻抗数据
- 模拟器里已经加入了少量异常样本，并在写入前做矫正，方便观察效果

## 4. 观察界面效果

界面中会看到：
- 实时曲线更新
- 传感器数值卡片刷新
- 模拟设备状态显示
- 录制开关、历史查询、导出等入口

如果你想验证“常规模拟数据 + 错误矫正”是否生效：
- 直接运行 `main.py --simulator`
- 观察曲线是否平滑、是否还有明显坏点
- 如有需要，再看数据库里的落库结果

## 5. 运行分析模块

如果你只想快速看分析结果，可以先运行分析脚本：

```powershell
.\.venv\Scripts\python.exe analysis.py --gateway GW_001 --node LORA_NODE_01 --model centroid
```

常用参数：
- `--model threshold`
- `--model centroid`
- `--limit 200`
- `--output result.json`

## 6. 常见问题

### GUI 运行后看起来像卡住
这不是报错。`main.py --simulator` 会持续运行，直到你关闭窗口。

### 命令超时
如果你在命令行里用短超时测试，界面进程可能会显示超时；这是正常现象。

### 想看效果但不需要 MQTT
只用模拟模式即可：

```powershell
.\.venv\Scripts\python.exe main.py --simulator
```

### 想要更慢一点的数据
修改 `config/config.json` 里的 `simulator.interval_ms`，数值越大越慢。

### 想要更频繁的阻抗数据
修改 `config/config.json` 里的 `simulator.impedance_interval_ms`。

## 7. 端到端链路验证（不接真实硬件）

如果你想先验证“发布 -> MQTT -> PC 端接收 -> 分析”这条链路，可以按下面步骤做。

### 7.1 启动一个临时 MQTT Broker

本项目验证时使用了 `amqtt`。如果你已经有 `mosquitto`，也可以用 `mosquitto`。

```powershell
# 用 amqtt 作为临时 broker
.\.venv\Scripts\amqtt.exe -d
```

如果不想留后台进程，可以在验证结束后手动停止它。

### 7.2 用模拟器发布 MQTT 消息

```powershell
.\.venv\Scripts\python.exe publisher_sim.py --host 127.0.0.1 --port 1883 --gateway GW_001 --node LORA_NODE_01
```

说明：
- 这会把模拟传感器数据和阻抗数据发到 `fruit/...` 主题
- 主题格式必须符合 `fruit/{gateway}/{node}/{type}`

### 7.3 用 PC 端接收并显示

```powershell
.\.venv\Scripts\python.exe main.py --mqtt
```

如果界面能打开并且数据能刷新，说明链路已经通了。

### 7.4 验证分析模块

```powershell
.\.venv\Scripts\python.exe analysis.py --gateway GW_001 --node LORA_NODE_01 --model centroid
```

能输出预测结果，说明“数据入库 -> 特征提取 -> 模型训练 -> 预测”这条分析链路也是通的。

## 8. 这次验证结果

已验证通过的链路：

- `amqtt` 临时 Broker 可启动
- `publisher_sim.py` 可成功发布消息
- `main.py --mqtt` 可启动，不报错
- `analysis.py` 可成功训练并输出预测

结论：
- 当前版本已经可以支持“模拟器发 MQTT -> PC 页面接收 -> 分析模块处理”的完整链路

## 9. 推荐的最短验证流程

```powershell
cd E:\opencode\fruit_monitor
.\.venv\Scripts\python.exe main.py --simulator
```

如果界面能打开，就说明当前版本已经可以正常使用。
