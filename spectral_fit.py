"""阻抗谱的 Cole-Cole 参数拟合与谱特征提取。

为什么不用单标量：``maturity.py`` 把整条谱压成有效频段 |Z| 均值一个数，
丢掉了 R0 / Rinf / f_c / alpha 这些文献里真正承载成熟度信息的量。
本模块改成先做参数化拟合，再把参数交给下游模型。

Cole-Cole 模型（Bode, 1945）::

    Z(w) = Rinf + (R0 - Rinf) / (1 + (j*w/wc)^(1-alpha))

    w -> 0   =>  Z -> R0     低频：电流走细胞外液
    w -> inf =>  Z -> Rinf   高频：细胞膜电容短路，电流穿膜

    alpha = 1 退化为单 Debye 弛豫（Cole-Cole 1941 的特例）
    alpha 越小，弛豫时间分布越宽——细胞尺寸/状态越不均一

拟合用有界多起点非线性最小二乘：7 个频点拟 4 参数只有 10 个自由度，
单一起点容易停在局部极小，所以按 alpha 的多个初值各跑一遍取 R^2 最优。

置信区间来自 ``curve_fit`` 返回的协方差矩阵对角元。它只在残差为
独立同分布高斯噪声时才成立，所以文档里写明这是**条件于模型假设**的
区间，不是无条件的真值区间——这在方法学章节必须写清楚。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Optional, Sequence

import numpy as np

try:
    from scipy.optimize import curve_fit
except ImportError as exc:  # pragma: no cover - 依赖缺失应当立刻暴露
    raise ImportError(
        "spectral_fit 需要 scipy。安装：pip install scipy>=1.11"
    ) from exc


# 拟合优度低于此值视为不可信，不给下游用
MIN_FIT_R2 = 0.95
# alpha 的搜索初值。真实果肉谱 alpha 多在 0.5~0.9，但边界附近也要探到
ALPHA_STARTS: tuple[float, ...] = (0.9, 0.7, 0.5, 0.3, 0.15)
# 每个 alpha 初值下 R0/Rinf 的缩放探针，避免初值偏差导致整条路径失效
SCALE_STARTS: tuple[float, ...] = (1.0, 1.3, 0.8)

# 统计显著性用的正态分位数（双侧 95%）
Z_975 = 1.959963984540054


@dataclass(frozen=True)
class ColeColeParams:
    """一次拟合的完整产出。

    ``sd_*`` 是拟合参数的标准误（``sqrt(diag(pcov))``）；拟合失败时
    协方差不可得，全部置 ``nan``——``nan`` 表示"没测"，不是"等于 0"。
    """

    r0: float
    rinf: float
    fc_hz: float
    alpha: float
    r_squared: float
    rmse: float
    n_points: int
    dof: int
    r0_ci95: float = float("nan")
    rinf_ci95: float = float("nan")
    fc_ci95: float = float("nan")
    alpha_ci95: float = float("nan")

    @property
    def tau_s(self) -> float:
        """弛豫时间 τ = 1 / (2π·f_c)。"""
        return 1.0 / (2.0 * math.pi * self.fc_hz) if self.fc_hz > 0 else float("nan")

    @property
    def membrane_index(self) -> float:
        """膜完整性代理量：R0 - Rinf，即低高频电阻差。

        果肉细胞膜破裂时 R0 向 Rinf 靠拢，该差值下降。
        """
        return self.r0 - self.rinf

    @property
    def polarization_ratio(self) -> float:
        """R0/Rinf。跨膜极化能力的无量纲度量，受绝对量纲影响小。"""
        return self.r0 / self.rinf if self.rinf > 0 else float("nan")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cole_cole_flat(freq: np.ndarray, r0: float, rinf: float,
                    fc: float, alpha: float) -> np.ndarray:
    """curve_fit 需要形如 (2n,) 的一维输出：[实部..., 虚部...]。"""
    x = (1j * np.asarray(freq, dtype=float) / fc) ** (1.0 - alpha)
    z = rinf + (r0 - rinf) / (1.0 + x)
    return np.concatenate([z.real, z.imag])


def _cole_cole_complex(freq: np.ndarray, r0: float, rinf: float,
                       fc: float, alpha: float) -> np.ndarray:
    x = (1j * np.asarray(freq, dtype=float) / fc) ** (1.0 - alpha)
    return rinf + (r0 - rinf) / (1.0 + x)


def fit_cole_cole(
    freq_hz: Sequence[float],
    z_real: Sequence[float],
    z_imag: Sequence[float],
    *,
    min_points: int = 4,
    min_r2: float = MIN_FIT_R2,
) -> Optional[ColeColeParams]:
    """对单条谱做 Cole-Cole 拟合。

    返回 :class:`ColeColeParams`，或在下列情况返回 ``None``：

    * 频点数不足 ``min_points``
    * 所有多起点都收敛失败
    * 最优解不满足物理约束（``r0 > rinf > 0``）
    * 最优 ``r_squared < min_r2``

    返回 ``None`` 而不是抛异常，是因为批量处理 900+ 条谱时个别坏点
    不该中断整批；调用方按 ``None`` 计数即可。
    """
    f = np.asarray(freq_hz, dtype=float).ravel()
    re = np.asarray(z_real, dtype=float).ravel()
    im = np.asarray(z_imag, dtype=float).ravel()

    if f.size != re.size or f.size != im.size:
        raise ValueError("freq_hz / z_real / z_imag 长度必须一致")

    ok = np.isfinite(f) & np.isfinite(re) & np.isfinite(im) & (f > 0)
    f, re, im = f[ok], re[ok], im[ok]
    if f.size < min_points:
        return None

    target = np.concatenate([re, im])
    f_lo, f_hi = float(f.min()), float(f.max())
    lower = np.array([1e-3, 1e-3, f_lo * 0.01, 0.01])
    upper = np.array([1e6, 1e6, f_hi * 100.0, 0.99])

    # 初值：低频 Z'≈R0，高频 Z'≈Rinf，|Z''| 峰≈f_c
    r0_start = float(re[np.argmin(f)]) * 1.05
    rinf_start = float(re[np.argmax(f)]) * 0.9
    if rinf_start >= r0_start:
        rinf_start = r0_start * 0.5
    fc_start = float(f[np.argmax(np.abs(im))])

    best: Optional[tuple[np.ndarray, np.ndarray, float]] = None
    for alpha0 in ALPHA_STARTS:
        for scale in SCALE_STARTS:
            p0 = np.array([r0_start * scale, rinf_start * scale, fc_start, alpha0])
            p0 = np.clip(p0, lower * 1.001, upper * 0.999)
            try:
                params, pcov = curve_fit(
                    _cole_cole_flat, f, target, p0=p0,
                    bounds=(lower, upper), maxfev=20000,
                )
            except (RuntimeError, ValueError, TypeError):
                continue
            if not np.all(np.isfinite(params)):
                continue
            fitted = _cole_cole_flat(f, *params)
            ss_res = float(np.sum((fitted - target) ** 2))
            ss_tot = float(np.sum((target - target.mean()) ** 2))
            if ss_tot <= 0:
                continue
            r2 = 1.0 - ss_res / ss_tot
            if best is None or r2 > best[2]:
                best = (params, pcov, r2)

    if best is None:
        return None

    params, pcov, r2 = best
    r0, rinf, fc, alpha = (float(v) for v in params)
    if not (r0 > rinf > 0 and fc > 0):
        return None
    if r2 < min_r2:
        return None

    dof = int(target.size - params.size)
    rmse = float(np.sqrt(np.sum((_cole_cole_flat(f, *params) - target) ** 2)
                         / max(target.size, 1)))
    if np.all(np.isfinite(pcov)) and dof > 0:
        sd = np.sqrt(np.diag(pcov))
        ci = sd * Z_975
    else:  # 协方差不可得（自由度为 0 或奇异）——记 nan 而不是 0
        ci = np.full(4, float("nan"))

    return ColeColeParams(
        r0=r0, rinf=rinf, fc_hz=fc, alpha=alpha,
        r_squared=r2, rmse=rmse,
        n_points=int(f.size), dof=dof,
        r0_ci95=float(ci[0]), rinf_ci95=float(ci[1]),
        fc_ci95=float(ci[2]), alpha_ci95=float(ci[3]),
    )


def scalar_band_stats(
    freq_hz: Iterable[float],
    z_real: Iterable[float],
    z_imag: Iterable[float],
    lo_hz: float = 0.0,
    hi_hz: float = float("inf"),
) -> dict[str, float]:
    """有效频段内的 |Z| / 相位统计量。拟合失败时仍可给下游兜底。"""
    f = np.asarray(list(freq_hz), dtype=float)
    re = np.asarray(list(z_real), dtype=float)
    im = np.asarray(list(z_imag), dtype=float)
    if f.size == 0 or re.size != f.size or im.size != f.size:
        return {k: float("nan") for k in
                ("count", "mag_mean", "mag_std", "mag_min", "mag_max",
                 "phase_mean", "re_mean", "im_mean")}

    mask = (f >= lo_hz) & (f <= hi_hz)
    if not mask.any():
        return {k: float("nan") for k in
                ("count", "mag_mean", "mag_std", "mag_min", "mag_max",
                 "phase_mean", "re_mean", "im_mean")}

    mag = np.hypot(re[mask], im[mask])
    phase = np.degrees(np.arctan2(-im[mask], re[mask]))
    return {
        "count": float(mask.sum()),
        "mag_mean": float(mag.mean()),
        "mag_std": float(mag.std(ddof=0)),
        "mag_min": float(mag.min()),
        "mag_max": float(mag.max()),
        "phase_mean": float(phase.mean()),
        "re_mean": float(re[mask].mean()),
        "im_mean": float(im[mask].mean()),
    }


# 特征列名，训练与推理必须用同一份顺序
SPECTRAL_FEATURE_NAMES: tuple[str, ...] = (
    "r0", "rinf", "fc_hz", "alpha", "tau_s",
    "membrane_index", "polarization_ratio",
    "mag_mean", "mag_std", "mag_min", "mag_max", "phase_mean",
)


def spectral_features(
    freq_hz: Sequence[float],
    z_real: Sequence[float],
    z_imag: Sequence[float],
    *,
    band_lo_hz: float = 0.0,
    band_hi_hz: float = float("inf"),
) -> dict[str, float]:
    """单条谱 -> 特征向量（含标量兜底项）。

    拟合失败时 Cole-Cole 项为 ``nan``，标量项仍给出——下游必须能处理
    ``nan``（见 :mod:`maturity_ml` 的插补器），否则一条坏谱就废掉整批。
    """
    out: dict[str, float] = {}

    params = fit_cole_cole(freq_hz, z_real, z_imag)
    if params is not None:
        out["r0"] = params.r0
        out["rinf"] = params.rinf
        out["fc_hz"] = params.fc_hz
        out["alpha"] = params.alpha
        out["tau_s"] = params.tau_s
        out["membrane_index"] = params.membrane_index
        out["polarization_ratio"] = params.polarization_ratio
    else:
        for k in ("r0", "rinf", "fc_hz", "alpha", "tau_s",
                  "membrane_index", "polarization_ratio"):
            out[k] = float("nan")

    out.update(scalar_band_stats(freq_hz, z_real, z_imag, band_lo_hz, band_hi_hz))
    # scalar_band_stats 的键名与 SPECTRAL_FEATURE_NAMES 对齐
    return {k: out.get(k, float("nan")) for k in SPECTRAL_FEATURE_NAMES}
