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

## 7. 推荐的最短验证流程

```powershell
cd E:\opencode\fruit_monitor
.\.venv\Scripts\python.exe main.py --simulator
```

如果界面能打开，就说明当前版本已经可以正常使用。
