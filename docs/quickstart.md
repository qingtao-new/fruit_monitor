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

- 传感器数值卡片，带红涨绿跌趋势箭头和环比差值
- 多指标实时曲线（趋势叠加，最近 1 段滚动）
- 设备状态呼吸灯：在线 / 重连 / 离线
- `阻抗谱 EIS` 页签：Nyquist 圆弧（含弛豫频率 `f_c` 标注）+ Bode 双轴
- 成熟度面板：置信度仪表、四阶段步骤条、预计采摘日期
- 录制开关、轮次选择、历史查询、导出等入口

如果你只想看分析结果，可以直接跑分析脚本：

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

## 10. 运行测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

覆盖分帧、扫频分包解析与拼装、Nyquist / Bode 序列、弛豫频率、
成熟度标定与置信度、SQLite 建表与并发写入。

## 11. 接真实硬件

节点和网关固件在 `Node/Node.ino` 与 `Gateway/Gateway.ino`，
扫频原理、接线、LoRa 命令和标定流程见 `docs/eis_pipeline.md`。

`config/config.json` 里 `maturity` 段控制成熟度标定的两端 |Z| 值和
有效频段；`assembler` 段控制扫频分包的超时时间和并发扫描数上限。
改配置不用重新编译。

## 12. 自动存档与上报 GitHub

### 12.1 它是怎么跑起来的

**不是 Windows 计划任务**——注册计划任务需要管理员权限，这台机器没有。改用
启动文件夹常驻循环：`scripts/git_archive_loop.cmd` 放在

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
```

开机自动起来，每 30 分钟调一次 `scripts/git_archive.py`，日志追加到
`logs/archive.stdout.log`。

### 12.2 一轮做四件事

1. `git status --porcelain` 为空就直接跳过，**不产生空提交**
2. `git add -A`，然后扫暂存内容里的明文凭据（`WIFI_PASSWORD`、`password=`、
   `api_key`、`PRIVATE KEY`…）
   - 命中就 `git reset` 中止，改动留在工作区等人处理 —— 上次就是靠这条把固件里的
     WiFi 密码拦下来的
3. 提交成 `chore(archive): 自动存档 <时间> (N files)`
4. push 到 `origin`

**push 失败只记一行日志、返回 0，绝不影响本地存档。** github.com 的 443 在这台机器
上时通时断（实测过 `Connection was reset` → `port 443 超时` → 自己恢复），下一轮
30 分钟自动重试。网络抖一下不该打断归档节奏，本地和远端各留一份就不怕丢。

凭据命中或提交失败时**不 push**——问题还在工作区，推上去只会把问题搬到 GitHub。

### 12.3 看日志

`logs/archive.log`：

```
2026-09-28 14:28:10  push failed: fatal: unable to access 'https://github.com/...' Recv failure: Connection was reset
2026-09-28 14:43:11  committed 211af29  1 files
2026-09-28 14:43:16  pushed origin/main
```

`skip: nothing to commit` 是正常的（本轮没改动）。

### 12.4 远端

```powershell
git remote -v
# origin  https://github.com/qingtao-new/fruit_monitor.git   (push 用)
# ssh     git@github.com:qingtao-new/fruit_monitor.git       (备胎，暂时没启用)
```

验证是否同步：`git ls-remote origin main` 应当等于 `git rev-parse HEAD`。

### 12.5 SSH 备胎（已配好，暂时摘下）

已经就位的：`~/.ssh/id_ed25519` 密钥、`~/.ssh/config`（含 `BatchMode yes` 和
`StrictHostKeyChecking accept-new`，保证非交互运行永不弹提示卡死）、remote `ssh`。

**摘下的原因**：公钥还没登记到 GitHub，试 SSH 只会白等一次连接再记一条
`Permission denied (publickey)`，所以 `PUSH_REMOTES` 里暂时只有 `origin`。

要启用，三步：

1. 打开 https://github.com/settings/keys → **New SSH key**
2. Key 框粘贴 `~/.ssh/id_ed25519.pub` 的内容
3. `scripts/git_archive.py` 改一行：

```python
PUSH_REMOTES = ("origin", "ssh")
```

`~/.ssh/config` 里备了两条路：`github.com` 走 22 端口，`github443` 走
`ssh.github.com:443`（22 被封时用，把 remote 换成 `git@github443:...` 即可）。

### 12.6 HTTPS 凭据

```powershell
git config --get credential.helper   # manager = Windows 凭据管理器
```

首次 https push 会弹窗口：**用户名填 GitHub 用户名，密码栏填 PAT**（开了 2FA
的账号不能用登录密码）。PAT 建 fine-grained、只授权这一个仓库、只需要
**Contents: Read and write**。填完就缓存了，之后 30 分钟一轮不再问。
