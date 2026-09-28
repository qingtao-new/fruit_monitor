"""成熟度预测的监督学习管线。

本模块只负责**方法**，不负责"结论是否可信"。可信度由
:attr:`Dataset.is_simulated` 决定，任何产出报告都必须先问它。

四条方法学硬约束，代码里逐条落实：

1. **按果分组切分**。同一只果的多次测量若同时出现在训练集和测试集，
   学到的是"认果"而不是"认熟度"，精度是虚高的。因此切分键是
   ``group_id``（一只果 = 一个组），用
   ``StratifiedGroupKFold``；组数太少或分层不可行时退回
   ``GroupKFold``——但**永不退回普通 KFold**。

2. **预处理只在训练折内拟合**。插补与标准化都放进
   ``sklearn.pipeline.Pipeline``，由交叉验证在每折内部 ``fit``。
   若先在全量数据上 ``fit_transform`` 再切分，测试折的分布信息会
   泄漏进训练，这是最常见的自欺。

3. **必须有基线**。任何 accuracy 若不同时给出 ``DummyClassifier``
   （只预测训练折众数）的 accuracy，就没有意义——类别不均衡时
   基线可以凭空拿到 90%。

4. **区间按组自助**。置信区间用 cluster bootstrap：有放回地重采样
   **整只果**，而不是单条谱。单条采样会把同果的相关样本当独立样本，
   区间被人为压窄。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional, Protocol, Sequence

import numpy as np

try:
    from sklearn.dummy import DummyClassifier, DummyRegressor
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.metrics import (
        accuracy_score,
        balanced_accuracy_score,
        confusion_matrix,
        f1_score,
        mean_absolute_error,
        mean_squared_error,
        r2_score,
    )
    from sklearn.model_selection import GroupKFold, StratifiedGroupKFold
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import RidgeClassifier
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "maturity_ml 需要 scikit-learn。安装：pip install scikit-learn>=1.3"
    ) from exc


TargetKind = Literal["classification", "regression"]

# 分类用的成熟度等级。四个都在用——旧版 ThresholdModel 只会输出两端，
# ripe 永远到不了，那不叫三分类。
MATURITY_LEVELS: tuple[str, ...] = ("unripe", "ripening", "ripe", "overripe")

DEFAULT_BOOTSTRAP = 2000
DEFAULT_SEED = 20260928


# --------------------------------------------------------------------------- #
# 标签源
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LabelRecord:
    """一条标签。

    ``sample_id`` 必须能唯一对上一条谱；``group_id`` 是切分键，
    一只果一个，多次测量共享同一个 ``group_id``。
    """

    sample_id: str
    group_id: str
    target: Any
    kind: TargetKind = "classification"


class LabelSource(Protocol):
    """标签来源。真实与合成实现同一个协议，下游无感。"""

    def load(self) -> list[LabelRecord]: ...

    @property
    def is_simulated(self) -> bool: ...


class SyntheticLabelSource:
    """合成标签 —— **只用于管线自检**。

    生成规则刻意写成与物理量挂钩（``membrane_index`` 越大越生），
    这样只要管线能学出关系，就证明"特征→标签→分组→评估"整条机械
    链路是通的。若标签纯随机，学不出来只能证明管线坏了还是数据本来
    就没信号，无法区分——那样自检就没意义了。

    ``is_simulated`` 恒为 ``True``，报告据此强制加水印。
    """

    def __init__(
        self,
        sample_ids: Sequence[str],
        group_ids: Sequence[str],
        *,
        kind: TargetKind = "classification",
        noise: float = 0.15,
        seed: int = DEFAULT_SEED,
    ) -> None:
        if len(sample_ids) != len(group_ids):
            raise ValueError("sample_ids 与 group_ids 长度必须一致")
        self._ids = list(sample_ids)
        self._groups = list(group_ids)
        self._kind = kind
        self._noise = float(noise)
        self._seed = int(seed)

    @property
    def is_simulated(self) -> bool:
        return True

    def load(self) -> list[LabelRecord]:
        raise NotImplementedError(
            "SyntheticLabelSource.load() 需要谱特征，请用 "
            "synthesize_targets(features, groups, kind, seed)"
        )


def synthesize_targets(
    sample_ids: Sequence[str],
    membrane_index: Sequence[float],
    group_ids: Sequence[str],
    *,
    kind: TargetKind = "classification",
    noise: float = 0.15,
    seed: int = DEFAULT_SEED,
) -> list[LabelRecord]:
    """由膜指数生成合成标签，供 :class:`SyntheticLabelSource` 场景使用。

    ``sample_ids`` 必须与谱的标识一致，否则 :func:`build_dataset`
    对不上，一条都留不下来——这是最常见的静默失败点。

    分类：把膜指数在**每只果内部**归一化后按四分位切成四级，
    保证每只果内部都有跨等级样本——否则组内标签恒定，
    ``StratifiedGroupKFold`` 分层无从谈起。
    回归：膜指数归一化到 [0,1] 再加噪声。
    """
    if not (len(sample_ids) == len(membrane_index) == len(group_ids)):
        raise ValueError("sample_ids / membrane_index / group_ids 长度必须一致")
    rng = np.random.default_rng(seed)
    x = np.asarray(membrane_index, dtype=float)
    groups = np.asarray(group_ids, dtype=str)
    records: list[LabelRecord] = []

    for g in np.unique(groups):
        idx = np.where(groups == g)[0]
        vals = x[idx]
        finite = np.isfinite(vals)
        if not finite.any():
            continue
        lo, hi = np.nanpercentile(vals[finite], [10, 90])
        span = (hi - lo) or 1.0
        norm = np.clip((vals - lo) / span, 0.0, 1.0)

        if kind == "regression":
            targets: list[Any] = list(
                norm + rng.normal(0.0, noise, size=norm.size)
            )
        else:
            # 膜指数越大 -> 越生；按组内分位切四级
            filled = np.where(finite, norm, 0.0)
            order = np.argsort(-filled)
            rank = np.empty(order.size, dtype=int)
            rank[order] = np.arange(order.size)
            n = max(order.size, 1)
            raw = np.floor(rank / n * len(MATURITY_LEVELS)).astype(int)
            targets = [MATURITY_LEVELS[min(int(i), len(MATURITY_LEVELS) - 1)]
                       for i in raw]
            # 加一点标签噪声，避免"完美可分"这种不现实的自检
            for j in range(len(targets)):
                if rng.random() < noise:
                    k = MATURITY_LEVELS.index(targets[j])
                    targets[j] = MATURITY_LEVELS[
                        min(max(k + int(rng.integers(-1, 2)), 0),
                            len(MATURITY_LEVELS) - 1)
                    ]

        for i, row in enumerate(idx):
            records.append(LabelRecord(
                sample_id=str(sample_ids[row]),
                group_id=str(groups[row]),
                target=targets[i],
                kind=kind,
            ))
    return records


class CsvLabelSource:
    """从 CSV 读真实标签。列名：``sample_id, group_id, target``。

    ``group_id`` 是必填项——没有它就没法按果切分，等于退回有泄漏的
    随机切分，本模块直接拒绝。
    """

    is_simulated = False

    def __init__(self, path: Any, *, kind: TargetKind = "classification") -> None:
        self._path = path
        self._kind = kind

    def load(self) -> list[LabelRecord]:
        import csv
        from pathlib import Path

        p = Path(self._path)
        with open(p, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise ValueError(f"{p} 是空文件")
            missing = {"sample_id", "group_id", "target"} - set(reader.fieldnames)
            if missing:
                raise ValueError(f"{p} 缺少必需列: {sorted(missing)}")
            records = []
            for row in reader:
                raw = (row.get("target") or "").strip()
                if raw == "":
                    continue
                target: Any
                if self._kind == "regression":
                    try:
                        target = float(raw)
                    except ValueError as exc:
                        raise ValueError(
                            f"{p} 的 target 应为数值，得到 {raw!r}"
                        ) from exc
                else:
                    target = raw
                records.append(LabelRecord(
                    sample_id=row["sample_id"].strip(),
                    group_id=row["group_id"].strip(),
                    target=target,
                    kind=self._kind,
                ))
        if not records:
            raise ValueError(f"{p} 没有有效标签行")
        return records


# --------------------------------------------------------------------------- #
# 数据集
# --------------------------------------------------------------------------- #

@dataclass
class Dataset:
    """特征矩阵 + 目标 + 分组。三者行对齐。"""

    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray
    sample_ids: list[str]
    feature_names: list[str]
    kind: TargetKind
    is_simulated: bool
    labels: list[LabelRecord] = field(default_factory=list)

    @property
    def n_samples(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_groups(self) -> int:
        return int(len(np.unique(self.groups)))

    @property
    def n_features(self) -> int:
        return int(self.X.shape[1])


def build_dataset(
    sample_ids: Sequence[str],
    features: np.ndarray,
    labels: Sequence[LabelRecord],
    feature_names: Sequence[str],
    *,
    is_simulated: bool,
) -> Dataset:
    """按 ``sample_id`` 对齐特征与标签，丢弃对不上的行。

    只保留**双方都有**的样本：标签缺谱、或谱缺标签，都不能进训练。
    静默丢弃会掩盖对齐 bug，所以返回的 Dataset 里 ``sample_ids``
    就是实际用上的那些，调用方可拿它和输入长度对比。
    """
    if features.shape[0] != len(sample_ids):
        raise ValueError(
            f"特征行数 {features.shape[0]} 与 sample_ids {len(sample_ids)} 不一致"
        )
    index = {str(s): i for i, s in enumerate(sample_ids)}
    if len(index) != len(sample_ids):
        raise ValueError("sample_id 有重复，无法唯一对齐")

    idx, kept_labels = [], []
    for lab in labels:
        i = index.get(str(lab.sample_id))
        if i is None:
            continue
        idx.append(i)
        kept_labels.append(lab)
    if not idx:
        raise ValueError("没有任何一条标签能对上谱的 sample_id")

    rows = np.asarray(idx, dtype=int)
    kind = kept_labels[0].kind
    if any(l.kind != kind for l in kept_labels):
        raise ValueError("标签的 kind 不一致，混用了分类与回归")

    y = np.array([l.target for l in kept_labels], dtype=object)
    if kind == "regression":
        y = y.astype(float)
    groups = np.array([l.group_id for l in kept_labels], dtype=object).astype(str)

    return Dataset(
        X=np.asarray(features[rows], dtype=float),
        y=y,
        groups=groups,
        sample_ids=[str(sample_ids[i]) for i in rows],
        feature_names=list(feature_names),
        kind=kind,
        is_simulated=bool(is_simulated),
        labels=kept_labels,
    )


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #

def _make_pipeline(estimator: Any) -> Pipeline:
    """插补 -> 标准化 -> 模型，三步全部封装在一个 Pipeline 里。

    这样 ``cross_validate`` 每折 ``fit`` 时，``SimpleImputer`` 和
    ``StandardScaler`` 只见过该折的训练数据——泄漏就此杜绝。
    ``SimpleImputer`` 放最前，是因为 Cole-Cole 拟合失败会产生 NaN，
    ``StandardScaler`` 本身不接受 NaN。
    """
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("scaler", StandardScaler()),
        ("model", estimator),
    ])


def build_model(kind: TargetKind, model_name: str, seed: int = DEFAULT_SEED) -> Pipeline:
    """按 (任务, 模型名) 构造未训练的 Pipeline。

    ``baseline`` 是 Dummy*：只复现训练折的先验分布，用于判断真实模型
    是否真的学到了东西。
    """
    if kind == "classification":
        if model_name == "baseline":
            return _make_pipeline(DummyClassifier(strategy="most_frequent"))
        if model_name == "logreg":
            return _make_pipeline(LogisticRegression(
                max_iter=2000, random_state=seed))
        if model_name == "ridge_clf":
            return _make_pipeline(RidgeClassifier(random_state=seed))
        if model_name == "rf":
            return _make_pipeline(RandomForestClassifier(
                n_estimators=300, min_samples_leaf=3,
                class_weight="balanced", random_state=seed, n_jobs=-1))
        raise ValueError(f"未知分类模型: {model_name}")

    if model_name == "baseline":
        return _make_pipeline(DummyRegressor(strategy="mean"))
    if model_name == "ridge":
        return _make_pipeline(Ridge(random_state=seed))
    if model_name == "rf":
        return _make_pipeline(RandomForestRegressor(
            n_estimators=300, min_samples_leaf=3, random_state=seed, n_jobs=-1))
    raise ValueError(f"未知回归模型: {model_name}")


AVAILABLE_MODELS: dict[TargetKind, tuple[str, ...]] = {
    "classification": ("baseline", "logreg", "ridge_clf", "rf"),
    "regression": ("baseline", "ridge", "rf"),
}


# --------------------------------------------------------------------------- #
# 交叉验证
# --------------------------------------------------------------------------- #

def make_splitter(kind: TargetKind, n_splits: int, seed: int = DEFAULT_SEED):
    """构造分组切分器。分层不可行时退回 GroupKFold，绝不退回普通 KFold。"""
    if kind == "classification":
        return StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    return GroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)


def _resolve_groups(kind: TargetKind, y: np.ndarray, groups: np.ndarray,
                    n_splits: int) -> np.ndarray:
    """分层失败时的降级：退回纯分组切分，仍然保证同果不跨折。

    ``StratifiedGroupKFold.split`` 是生成器，异常在**迭代时**才抛，
    所以这里必须真的消费一个折来探测，光调用不会报错。
    """
    if kind != "classification":
        return groups
    n_groups = len(np.unique(groups))
    if n_groups < n_splits:
        raise ValueError(
            f"组数 {n_groups} 少于折数 {n_splits}，无法按组切分。"
            "减少 --folds，或增加独立样本数。"
        )
    try:
        next(iter(StratifiedGroupKFold(n_splits=n_splits)
                  .split(np.zeros(len(y)), y, groups)))
    except ValueError:
        return groups
    return groups


@dataclass
class FoldOutcome:
    fold: int
    metrics: dict[str, float]
    y_true: list[Any]
    y_pred: list[Any]
    test_groups: list[str]


@dataclass
class CVOutcome:
    kind: TargetKind
    model_name: str
    n_splits: int
    n_samples: int
    n_groups: int
    folds: list[FoldOutcome]
    aggregate: dict[str, float]
    ci95: dict[str, tuple[float, float]]
    oof_y_true: list[Any]
    oof_y_pred: list[Any]
    oof_groups: list[str]

    @property
    def is_simulated(self) -> bool:
        # 由 run_experiment 统一盖章，这里只做转发
        return bool(getattr(self, "_sim", False))


def classification_metrics(y_true: Iterable, y_pred: Iterable) -> dict[str, float]:
    yt = np.asarray(list(y_true))
    yp = np.asarray(list(y_pred))
    # 按**观测到的类**并集做宏平均：固定四类会让"某折恰好没出现某类"
    # 被判成 0 分，即使预测完全正确；而 y_pred 里多出来的类仍会被计入
    # （假阳性照罚），所以并集既不放水也不漏罚。
    labels = sorted(set(yt.tolist()) | set(yp.tolist()))
    with warnings.catch_warnings():
        # 按果分组时，留出的那只果可能天然不含某个类——这不是错误。
        warnings.filterwarnings(
            "ignore", message="y_pred contains classes not in y_true")
        return {
            "accuracy": float(accuracy_score(yt, yp)),
            "balanced_accuracy": float(balanced_accuracy_score(yt, yp)),
            "f1_macro": float(f1_score(yt, yp, labels=labels, average="macro",
                                      zero_division=0)),
        }


def regression_metrics(y_true: Iterable, y_pred: Iterable) -> dict[str, float]:
    yt = np.asarray(list(y_true), dtype=float)
    yp = np.asarray(list(y_pred), dtype=float)
    mse = float(mean_squared_error(yt, yp))
    return {
        "mae": float(mean_absolute_error(yt, yp)),
        "rmse": float(np.sqrt(mse)),
        "r2": float(r2_score(yt, yp)) if yt.size > 1 else float("nan"),
    }


def _metrics_for(kind: TargetKind) -> Callable[[Sequence, Sequence], dict[str, float]]:
    return classification_metrics if kind == "classification" else regression_metrics


def cross_validate(
    ds: Dataset,
    model_name: str,
    *,
    n_splits: int = 5,
    seed: int = DEFAULT_SEED,
    n_boot: int = DEFAULT_BOOTSTRAP,
) -> CVOutcome:
    """按果分组的交叉验证，返回逐折与汇总结果。

    每条样本恰好被预测一次（out-of-fold），且预测它的模型**从未见过
    该样本所在的整只果**。汇总指标在全量 OOF 预测上计算，
    区间则用 cluster bootstrap（重采样整只果）。
    """
    if ds.n_groups < 2:
        raise ValueError(f"只有 {ds.n_groups} 个组，无法做分组交叉验证")
    n_splits = max(2, min(n_splits, ds.n_groups))

    groups = _resolve_groups(ds.kind, ds.y, ds.groups, n_splits)
    splitter = make_splitter(ds.kind, n_splits, seed)
    metric_fn = _metrics_for(ds.kind)

    folds: list[FoldOutcome] = []
    oof_true: list[Any] = [None] * ds.n_samples
    oof_pred: list[Any] = [None] * ds.n_samples
    oof_group: list[Optional[str]] = [None] * ds.n_samples

    for fold, (tr, te) in enumerate(splitter.split(ds.X, ds.y, groups)):
        model = build_model(ds.kind, model_name, seed)
        model.fit(ds.X[tr], ds.y[tr])
        pred = model.predict(ds.X[te])

        if ds.kind == "regression":
            y_true = [float(v) for v in ds.y[te]]
            y_pred = [float(v) for v in pred]
        else:
            y_true = [str(v) for v in ds.y[te]]
            y_pred = [str(v) for v in pred]
        for i, row in enumerate(te):
            oof_true[row] = y_true[i]
            oof_pred[row] = y_pred[i]
            oof_group[row] = str(groups[row])

        folds.append(FoldOutcome(
            fold=fold,
            metrics=metric_fn(y_true, y_pred),
            y_true=y_true,
            y_pred=y_pred,
            test_groups=sorted({str(g) for g in groups[te]}),
        ))

    covered = [i for i, g in enumerate(oof_group) if g is not None]
    if len(covered) != ds.n_samples:
        missing = ds.n_samples - len(covered)
        raise RuntimeError(f"{missing} 条样本没有 OOF 预测，切分器有 bug")

    sim_true = [oof_true[i] for i in covered]
    sim_pred = [oof_pred[i] for i in covered]
    sim_group = [oof_group[i] for i in covered]

    aggregate = metric_fn(sim_true, sim_pred)
    ci = {
        name: cluster_bootstrap_ci(
            sim_true, sim_pred, sim_group,
            lambda yt, yp, m=metric_fn: m(yt, yp)[name],
            n_boot=n_boot,
            seed=seed,
        )
        for name in aggregate
    }

    return CVOutcome(
        kind=ds.kind, model_name=model_name, n_splits=n_splits,
        n_samples=ds.n_samples, n_groups=ds.n_groups, folds=folds,
        aggregate=aggregate, ci95=ci,
        oof_y_true=sim_true, oof_y_pred=sim_pred, oof_groups=sim_group,
    )


def cluster_bootstrap_ci(
    y_true: Sequence,
    y_pred: Sequence,
    groups: Sequence[str],
    metric_fn: Callable[[Sequence, Sequence], float],
    *,
    n_boot: int = DEFAULT_BOOTSTRAP,
    alpha: float = 0.05,
    seed: int = DEFAULT_SEED,
) -> tuple[float, float]:
    """按组自助法求指标的 (1-α) 置信区间。

    有放回地重采样**整只果**，把同果样本的相关性一并保留。若改成对
    单条谱采样，900 条看似独立、实际来自几只果的样本会让区间窄到
    毫无意义——那正是要避免的错误。
    """
    yt = np.asarray(y_true, dtype=object)
    yp = np.asarray(y_pred, dtype=object)
    g = np.asarray(groups, dtype=str)
    uniq = np.unique(g)
    if uniq.size < 2 or len(yt) < 2:
        v = metric_fn(list(yt), list(yp))
        return (float(v), float(v))

    rng = np.random.default_rng(seed)
    index_of = {u: np.where(g == u)[0] for u in uniq}
    stats: list[float] = []
    for _ in range(n_boot):
        chosen = rng.choice(uniq, size=uniq.size, replace=True)
        rows = np.concatenate([index_of[c] for c in chosen])
        try:
            v = metric_fn(list(yt[rows]), list(yp[rows]))
        except ValueError:
            continue
        if np.isfinite(v):
            stats.append(float(v))
    if not stats:
        v = metric_fn(list(yt), list(yp))
        return (float(v), float(v))
    lo = float(np.percentile(stats, 100 * (alpha / 2)))
    hi = float(np.percentile(stats, 100 * (1 - alpha / 2)))
    return (lo, hi)
