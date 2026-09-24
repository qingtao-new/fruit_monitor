# 阻抗谱硬件链路与标定

这条链路让 ESP32 节点自己扫频测阻抗，经 LoRa 上到 ESP32-S3 网关，
再走 MQTT 进 PC 做 Nyquist / Bode 绘图和成熟度预测。

## 1. 数据流

```text
┌──────────┐  LoRa 433MHz   ┌──────────┐  Wi-Fi MQTT   ┌──────────────────────┐
│ Node.ino │ ─────────────▶ │Gateway.ino│ ────────────▶ │ mqtt_client → 落库   │
│ 环境+扫频 │  逐点 JSON 帧   │ 聚合 100 点│  分段 sweep 帧 │ → 组装整谱 → 预测    │
└──────────┘                └──────────┘                └──────────────────────┘
```

- 节点 → 网关：单频点一条 JSON 帧，`\n` 结尾
- 网关 → PC：每 `SEG_POINTS` 点合并成一条 `sweep` 帧，`\n` 结尾
- PC：按 `round` 缓存分片，静默 5s 后还原成整谱（`spectrum.py`）

## 2. 节点阻抗测量原理

不是黑盒读一个数，而是同步检测（synchronous detection）：

1. `LEDC` 在 `Z_EXC_PIN` 上产生频率 `f` 的方波激励，经耦合电路驱动
   「参考电阻 `R_REF` + 样品」串联支路
2. `esp_timer` 以 `SAMPLE_RATE_HZ`（100 kHz）触发 ISR，同时读两路 ADC：
   - `Z_IN_CH`：样品两端电压
   - `Z_REF_CH`：激励参考
3. 每个采样点用 `esp_timer_get_time()` 推出的相位做一次 DFT 投影，
   累加基波的 `I`（同相）和 `Q`（正交）分量
4. 两条支路做同样的投影得到 `I_r / Q_r`，按串联通路关系反解

```text
Z_sample = R_REF · (I + jQ) / (I_r + jQ_r)
```

好处：激励幅值和相位偏移都被除法消掉，硬件失调和供电漂移不影响结果，
也不需要知道 ADC 的绝对精度。

### 2.1 接线要点

```text
Z_EXC_PIN ── 限幅/耦合电容 ──┬── 电极 +
                             ├── Z_IN_PIN   （经电压跟随 + 偏置到 1.65V 中点）
R_REF ──────────────────────┘
电极 − ──────────────────────────────────────────── 公共地
激励信号 ── 分压 ── Z_REF_PIN
```

- 样品电压要偏置到 `Z_MID_LEVEL`（≈1.65V）中间，否则负半周会被削掉
- `Z_REF_PIN` 采的是激励本身，用来做参考，必须和激励同相
- 电容性样品 `Z''` 为负；如果你的测量结果符号相反，把
  `Z_IM_SIGN` 从 `-1.0f` 改成 `+1.0f`

### 2.2 可调参数（`Node/Node.ino` 顶部）

| 宏 | 默认 | 说明 |
| --- | --- | --- |
| `R_REF_OHM` | `1000.0` | 参考电阻，必须和实物一致 |
| `FREQ_MIN_HZ` | `1000.0` | 扫频下限 |
| `FREQ_MAX_HZ` | `100000.0` | 扫频上限 |
| `TOTAL_POINTS` | `100` | 每轮频点数，对数分布 |
| `SEG_POINTS` | `50` | 每组点数，**必须和网关一致** |
| `SAMPLE_RATE_HZ` | `100000` | 采样率，≥ 4 × 最高激励频率 |
| `SAMPLES_PER_POINT` | `4000` | 每频点采样数 |

采样率上限受 ADC 限制。想扫到更高频，要么提高 ADC 采样率，
要么下调 `FREQ_MAX_HZ`，不要两个方向一起动。

## 3. LoRa 命令

网关通过 433MHz 下发控制，节点按命令采集：

| 字节 | 含义 |
| --- | --- |
| `0x11` START | 新一轮，从第 1 段开始，轮次号 +1 |
| `0x14` NEXT | 进入下一段 |
| `0x13` RETRY | 后跟 7 字节掩码，bit=1 表示该点需要补发 |

节点每 `sendInterval`（2s）还会自发一条不带 `round` 字段的环境帧，
网关识别后原样透传到 `sensor` 主题。

### 3.1 上行帧

逐点帧：

```json
{"round":7,"seg":0,"pt":12,"freq":2138,"re":1975,"im":-423,"imp":2030,
 "soil_moisture":52,"temperature":25.10,"nh3":10,"h2s":5,"co2":450,"ph":6.50,"humidity":65}
```

段完成帧（节点发完本段后主动报告，网关据此判断是否需要补发）：

```json
{"round":7,"seg":0,"done":true,"points":50,"ts":1234}
```

## 4. 网关聚合与补发

网关收到逐点帧后按 `seg*SEG_POINTS + pt` 填入环形缓冲：

1. 第 1 段 50 点收满 → 发 `sweep` 帧 → 下发 `NEXT`
2. 第 2 段收满 → 发 `sweep` 帧 → 整轮完成
3. 整轮完成 → 发 `status: round_done` 摘要 → 下发 `START` 开始下一轮

缺点处理：

- 节点上报 `done` 时若网关发现本段有缺口，立刻下发 `RETRY` 掩码
- 10s 内没收到任何数据也触发补发
- 同一轮最多补发 `MAX_ROUND_RETRY`（默认 3）次，达到上限就放弃本轮
  的缺点直接进入下一轮，避免长时间卡住

`round_done` 里的 `retry` 字段可以看到本轮实际补发了几次；
`imp_mean` 是本轮 100 点的 |Z| 均值，可以直接用来粗判成熟度。

## 5. PC 侧处理

```text
sweep 帧 ─▶ parse_sweep_points ─▶ 落 sweep_data 表 + 按 round 缓存
                                          │
                              静默 5s 后 sweep_points_to_spectrum
                                          │
                                          ▼
                     Nyquist / Bode 绘图 + characteristic_frequency
                                          │
                                          ▼
                              window_stats(1k~30k Hz) ─▶ maturity 预测
```

- 分片缓存的静默阈值是 5s；如果一轮扫频耗时超过这个值，
  中途也会先出一张图
- 有效频段默认 `1000 ~ 30000 Hz`，取该频段 |Z| 均值映射到成熟度
- 弛豫频率 `f_c` 取有效频段内 `-Z''` 最大的点，画在 Nyquist 图上

## 6. 标定

换电极、换激励幅度或换样品夹具后，阻抗绝对值会变，需要重新标定
`config/config.json` 的 `maturity` 段：

```json
"maturity": {
  "band_lo_hz": 1000,
  "band_hi_hz": 30000,
  "magnitude_high": 1900,
  "magnitude_low": 1050,
  "spread_ratio_ref": 0.29
}
```

1. **测未成熟果实**：取有效频段 |Z| 均值，填 `magnitude_high`
2. **测过熟果实**：取有效频段 |Z| 均值，填 `magnitude_low`
   （必须保证 `magnitude_high > magnitude_low`）
3. **`spread_ratio_ref`**：模型形状本身就会带来离散度。取几张正常谱的
   `std/mean` 均值填进去；只有超出这个基线的部分才当作噪声扣减置信度
4. **频段**：如果你的激励范围不是 1k~30k，把 `band_lo_hz` / `band_hi_hz`
   改成实际有效范围

改完配置文件重启 GUI 即可，不用重新编译。

置信度的算法：当前值落在某个成熟度区间内的相对位置决定基础置信度
（越靠近区间中心越确定，越靠近临界线越保守），再用超出基线的离散度
做惩罚。范围固定收敛在 `MIN_CONFIDENCE` 附近到 `MAX_CONFIDENCE` 之间。

## 7. 联调顺序

```powershell
# 1. 编译检查
.\.venv\Scripts\python.exe -m py_compile main.py mqtt_client.py gui.py db.py protocol.py spectrum.py maturity.py

# 2. 单元测试（76 个）
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 3. 本地 MQTT broker
.\.venv\Scripts\amqtt.exe -d

# 4. 订阅器，先确认帧格式
.\.venv\Scripts\python.exe mqtt_subscriber.py

# 5. 烧录 Node.ino / Gateway.ino，把 Gateway 的 WIFI_SSID 和
#    LOCAL_MQTT_HOST 改成你的环境

# 6. PC 端接收
.\.venv\Scripts\python.exe main.py --mqtt
```

没有硬件时可以先跑 `.\run.bat`（模拟模式），链路和真实硬件完全一致：
模拟器用同一份 `build_sweep_round` 生成数据，走的也是
`parse_sweep_points → 缓存 → 整谱 → 预测` 这条路径。
