# 成熟度建模方法学

本文件是果实成熟度预测的**方法与限制**记录。写成独立文档而不是塞进
`quickstart.md`，是因为方法学结论（尤其是数据来源那一节）会随数据变化，
不该和操作手册混在一起。

---

## 1. 问题定义

给定一条阻抗谱（频率 → Z' + jZ''），预测果实成熟度。

- **分类**：`unripe / ripening / ripe / overripe` 四级
- **回归**：Brix、硬度等连续真值

## 2. 数据来源（**先读这一节**）

### 2.1 现状：当前库里的谱绝大部分不是真机数据

对 `data/` 下**全部 8 个 SQLite 库**做了来源鉴定，判据与代码出处：

| 判据 | 命中 | 出处 |
|---|---|---|
| `rcal_ohm = 51000` | 745 批 | `publisher_sim.py:155` 写死；三份固件**均不发送**该字段 |
| 首频 900 Hz | 140 批 | `SWEEP_FREQS`，`mqtt_client.py:1123` |
| 7 点 `[100,500,1000,5000,10000,30000,100000]` | 666 批 | `IMPEDANCE_FREQS`，`mqtt_client.py:1111` |
| **`Node.ino` 对数表 `[1000,1026,1052,…]`** | **0 批** | `Node.ino:258-264` |

**结论：库里没有可确认的真机 EIS 数据。** 生成侧的证据在
`mqtt_client.py:1146`：

```python
def build_sweep_round(...):
    """按当前 R0 生成一整轮扫频...
    模拟器、发布器和测试共用这一份生成逻辑..."""
    zr, zi = _impedance_at(freq, r0)      # 双参 Cole-Cole 生成
    zr *= 1 + random.gauss(0, 0.005)      # 0.5% 高斯噪声
```

### 2.2 由此得出的三条硬性限制

1. **在当前数据上拟合 Cole-Cole 得到的 R²≈0.9999 是循环论证**——
   数据本就由 Cole-Cole 模型生成。这个数不能作为拟合优度的证据。
2. **"19.8 天内参数无漂移"（趋势 0.00x%/天、组间 SD < 组内 SD）
   不能解释为生物信号**，因为模拟器的 `R0` 不随时间变化。
3. **`maturity.py` 的标定端点 `1900/1050` 同样取自该仿真模型**
   （`maturity.py` docstring 自述）。真机中位数 |Z|≈2025 Ω 落在区间外，
   导致 `progress` 恒钳在 0、260 条预测全为 `unripe`。

**在真机数据到位并完成标定之前，任何 accuracy / R² 数字都只能用来
判断管线是否通畅，不构成研究结论。**

## 3. 特征提取（`spectral_fit.py`）

不用单标量。把整条谱拟合成 Cole-Cole 模型：

```
Z(ω) = R∞ + (R0 - R∞) / (1 + (jω/ωc)^(1-α))
```

| 参数 | 物理含义 | 与成熟度的关系 |
|---|---|---|
| `R0` | 低频电阻（电流走胞外液） | 膜破裂 → 下降 |
| `R∞` | 高频电阻（电流穿膜） | 胞内液变化 |
| `f_c` | 弛豫特征频率 | 界面/组织弛豫位置 |
| `α` | 弛豫时间分布宽度 | 细胞状态均一性 |
| `R0 - R∞` | `membrane_index` | 膜完整性代理量 |
| `R0 / R∞` | `polarization_ratio` | 无量纲，抗量纲漂移 |

**求解**：`scipy.optimize.curve_fit`，有界 + 多起点（α 初值
`0.9/0.7/0.5/0.3/0.15` × 缩放 `1.0/1.3/0.8`）取 R² 最优。
7 点拟 4 参数只有 10 自由度，单起点易陷局部极小。

**拒绝规则**（返回 `None` 而非抛异常，批量处理不因个别坏点中断）：

- 频点 < 4
- 所有起点收敛失败
- 违反物理约束 `R0 > R∞ > 0`
- `R² < 0.95`

**置信区间**：`sqrt(diag(pcov)) × 1.96`。这只在残差为独立同分布
高斯噪声时成立——**是条件于模型假设的区间**，引用时必须写明。

拟合失败时 Cole-Cole 项置 `NaN`，标量兜底项照常给出，由下游插补器处理。

## 4. 标签（**最容易出问题的一环**）

### 4.1 不允许合成标签参与评估

`analysis.py` 旧版的 `synthesize_labels` 按**时间三等分**贴标签：先假定
"随时间变熟"，再拿时间当标签训练模型。这是循环论证，学出来的是时间
序号，任何 accuracy 都是自证。**该函数已删除。**

现行策略：

- `analysis.py` **没有 `--labels` 就直接退出**，不给数字
- `scripts/run_experiment.py` 允许合成标签，但报告顶部强制打印
  `SIMULATED DATA` 水印，且 `is_simulated` 随 JSON 落盘

### 4.2 标签文件格式

```csv
sample_id,group_id,target
SCAN_20260908180123@1788861683,FRUIT_01,unripe
```

- `sample_id` = `scan_id@timestamp`（`scan_id` 因固件重启会重复）
- **`group_id` 必填**，缺列直接拒绝加载
- `--dump-samples` 可打印库中可用的 `sample_id` 供填表

## 5. 验证协议（`maturity_ml.py`）

### 5.1 按果分组切分 —— 不可妥协

同一只果的多次测量若同时出现在训练集与测试集，模型学到的是"认果"
而非"认熟度"，精度虚高。切分键为 `group_id`，用
`StratifiedGroupKFold`；分层不可行时退回 `GroupKFold`，
**永不退回普通 `KFold`**。测试 `GroupSplitInvariantTest` 直接断言
每折训练/测试组交集为空。

### 5.2 预处理只在训练折内拟合

`SimpleImputer → StandardScaler → 模型` 三步封装进 `sklearn.pipeline.Pipeline`，
由交叉验证在每折 `fit`。先在全量数据上 `fit_transform` 再切分，
会让测试折的分布信息泄漏进训练。

### 5.3 必须有基线

`DummyClassifier(most_frequent)` / `DummyRegressor(mean)` 走同一套 CV。
四分类下基线 accuracy≈0.25；类别不均衡时基线可以凭空拿到 90%，
不给基线的 accuracy 没有意义。

### 5.4 区间按组自助（cluster bootstrap）

有放回地重采样**整只果**（默认 2000 次），而非单条谱。900 条看似独立、
实际来自几只果的样本，若按单条采样会把区间压窄到毫无意义。

### 5.5 报告的不变量自检

每次实验结束打印：

```
[OK] 分组无泄漏：N 折 × M 个模型，同一只果从未同时出现在训练与测试
[OK] 基线 accuracy=0.xxx（四分类理论值 0.25）—— 低于它的模型等于没有信息
```

## 6. 复现

```powershell
# 管线自检（合成标签 + SIMULATED 水印）
.venv\Scripts\python scripts\run_experiment.py --db data\fruit_monitor.db

# 真实评估
.venv\Scripts\python analysis.py --gateway GW_001 --node LORA_NODE_01 --dump-samples > samples.txt
# 填好 labels.csv 后：
.venv\Scripts\python analysis.py --gateway GW_001 --node LORA_NODE_01 --labels labels.csv
```

全部指标带 95% CI，固定 `--seed`（默认 `20260928`）可复现。

## 7. 诚实的限制清单

| # | 限制 | 影响 |
|---|---|---|
| 1 | **库中无可确认的真机数据** | 现阶段所有指标只验证管线，不是结论 |
| 2 | **标定端点 `1900/1050` 来自仿真** | 真机上 `progress` 恒为 0 |
| 3 | **独立样本数未知** | 少数几只果反复测时有效 n 远小于样本数，区间会很宽 |
| 4 | 7 点谱拟 4 参数 | 自由度仅 10，参数 CI 偏乐观 |
| 5 | 无真值的日期/批次标注 | 分组只能按占位符切，不代表真实独立样本结构 |
| 6 | `confidence` 是启发式 | 见 `maturity.py` docstring，不是统计置信度 |
| 7 | `α` 与 `f_c` 在 7 点谱上弱可辨识 | 更换频点表后需重新验证 |

## 8. 下一步（按优先级）

1. **接真机采集**，确认频点表命中 `Node.ino` 判据
2. **重新标定** `magnitude_high/low`，或改用 Cole-Cole 参数直接建模
3. 收集真值（Brix/硬度），填 `labels.csv`
4. 验证组间变异是否 > 组内变异（信号是否存在）
5. 若有效独立样本 < 10，考虑报描述性统计而非预测模型
