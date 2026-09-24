"""果实成熟度与采摘置信度估计。

阻抗谱的串联电阻 R0 随果肉细胞破裂单调下降，这是成熟过程里
最稳定的电学指纹。因此这里不做黑盒回归，而是把"有效频段 |Z| 均值"
直接映射到成熟度进度，再用"当前值落在等级区间内的相对位置"给出
置信度：越靠近区间中心越确定，越靠近临界线越保守。

这条链路是确定性的、可解释的，也能在没有训练集的阶段直接上线；
等有真实标注数据后，把 :func:`estimate_maturity` 换成
``analysis.py`` 里的模型即可，协议与界面都不需要动。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from protocol import PredictionData
from spectrum import window_stats

MAGNITUDE_HIGH = 1900.0
MAGNITUDE_LOW = 1050.0

# 有效频段内 |Z| 的相对离散度。模型形状本身就会带来约 0.29 的离散，
# 只有超出这一基线的部分才当作测量噪声来扣减置信度。
SPREAD_RATIO_REF = 0.29

MATURITY_LEVELS: tuple[tuple[str, str], ...] = (
    ("unripe", "未成熟"),
    ("ripening", "转熟期"),
    ("ripe", "成熟期"),
    ("overripe", "过熟期"),
)

LEVEL_LABELS: dict[str, str] = dict(MATURITY_LEVELS)

REMAINING_DAYS_AT_UNRIPE = 14.0

MIN_CONFIDENCE = 0.55
MAX_CONFIDENCE = 0.98


def maturity_level_index(progress: float) -> int:
    bands = len(MATURITY_LEVELS)
    clamped = max(0.0, min(1.0, progress))
    return min(bands - 1, int(clamped * bands))


def estimate_maturity(
    magnitude_mean: float,
    *,
    magnitude_high: float = MAGNITUDE_HIGH,
    magnitude_low: float = MAGNITUDE_LOW,
    spread_ratio: float = 0.0,
    spread_ratio_ref: float = SPREAD_RATIO_REF,
    node_id: Optional[str] = None,
    timestamp: Optional[int] = None,
    round_id: Optional[int] = None,
) -> PredictionData:
    """由有效频段 |Z| 均值估计成熟度、等级、采摘日期与置信度。

    ``magnitude_high`` / ``magnitude_low`` 是"未成熟起点"和"过熟终点"的
    有效频段 |Z| 均值，需要按所用硬件标定一次；当前默认值对应
    ``mqtt_client.build_sweep_round`` 的双参 Cole-Cole 仿真模型。
    """
    if magnitude_high <= magnitude_low:
        raise ValueError("magnitude_high must exceed magnitude_low")

    raw = (magnitude_high - magnitude_mean) / (magnitude_high - magnitude_low)
    progress = max(0.0, min(1.0, raw))

    index = maturity_level_index(progress)
    band = progress * len(MATURITY_LEVELS) - index
    edge_distance = min(band, 1.0 - band) / 0.5

    confidence = MIN_CONFIDENCE + (MAX_CONFIDENCE - MIN_CONFIDENCE) * edge_distance
    excess_spread = max(0.0, spread_ratio - spread_ratio_ref)
    if excess_spread > 0:
        confidence *= max(0.65, 1.0 - excess_spread * 2.0)
    confidence = max(MIN_CONFIDENCE * 0.8, min(MAX_CONFIDENCE, confidence))

    level_key = MATURITY_LEVELS[index][0]
    remaining = (1.0 - progress) * REMAINING_DAYS_AT_UNRIPE
    harvest = date.today() + timedelta(days=int(round(remaining)))

    now_ts = timestamp if timestamp is not None else int(
        datetime.now(tz=timezone.utc).timestamp()
    )

    return PredictionData(
        node_id=node_id,
        timestamp=int(now_ts),
        maturity=round(progress, 4),
        maturity_level=level_key,
        harvest_date=harvest.isoformat(),
        confidence=round(confidence, 4),
        round_id=round_id,
    )


def level_label(level_key: Optional[str]) -> str:
    if not level_key:
        return "未知"
    return LEVEL_LABELS.get(level_key, level_key)


def spectrum_prediction(
    spectrum: object,
    *,
    node_id: Optional[str] = None,
    timestamp: Optional[int] = None,
    round_id: Optional[int] = None,
    band_lo_hz: float = 1000.0,
    band_hi_hz: float = 30000.0,
    magnitude_high: float = MAGNITUDE_HIGH,
    magnitude_low: float = MAGNITUDE_LOW,
    spread_ratio_ref: float = SPREAD_RATIO_REF,
) -> tuple[PredictionData, dict[str, float]]:
    """整谱 → (预测结果, 窗口统计量) 的一站式入口。

    有效频段和两端 |Z| 标定值都允许外部覆盖：换电极、换激励幅度、
    换样品夹具后只需改 ``config/config.json`` 的 ``maturity`` 段，
    不用重新编译。

    ``round_id`` 带上扫频轮次号，同一秒落下多轮上报时不至于互相覆盖。
    """
    stats = window_stats(spectrum, band_lo_hz, band_hi_hz)  # type: ignore[arg-type]
    spread_ratio = stats["std"] / stats["mean"] if stats["mean"] else 0.0
    return estimate_maturity(
        stats["mean"],
        magnitude_high=magnitude_high,
        magnitude_low=magnitude_low,
        spread_ratio=spread_ratio,
        spread_ratio_ref=spread_ratio_ref,
        node_id=node_id or getattr(spectrum, "node_id", None),
        timestamp=timestamp or getattr(spectrum, "timestamp", None),
        round_id=round_id if round_id is not None else getattr(spectrum, "scan_id", None),
    ), stats
