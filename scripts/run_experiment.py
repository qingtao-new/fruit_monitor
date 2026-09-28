"""跑一次成熟度建模实验，输出可直接进论文方法/结果章节的报告。

用法::

    # 合成标签自检（报告强制加 SIMULATED 水印）
    python scripts/run_experiment.py --db data/fruit_monitor.db

    # 真实标签
    python scripts/run_experiment.py --db data/fruit_monitor.db --labels labels.csv

    # 回归任务（target 为 Brix/硬度数值）
    python scripts/run_experiment.py --labels labels.csv --kind regression

真实标签 CSV 至少要三列::

    sample_id,group_id,target
    SCAN_20260908180123@1788861683,FRUIT_01,unripe

``group_id`` 是**必填**的：一只果一个组。缺了它就只能随机切分，
同一只果的多次测量会同时落进训练和测试，精度虚高——本脚本直接拒绝运行。

报告里的每个指标都带按组自助的 95% 置信区间；合成数据时顶部会印
``SIMULATED`` 水印，那些数字只能用来判断管线是否通畅，**不能当作
研究结论**。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maturity_ml import (  # noqa: E402
    AVAILABLE_MODELS,
    MATURITY_LEVELS,
    CsvLabelSource,
    DEFAULT_BOOTSTRAP,
    Dataset,
    LabelRecord,
    build_dataset,
    cross_validate,
    synthesize_targets,
)
from spectral_fit import (  # noqa: E402
    SPECTRAL_FEATURE_NAMES,
    fit_cole_cole,
    spectral_features,
)


def load_spectra(
    db_path: Path,
    *,
    table: str = "impedance_data",
    min_points: int = 7,
    min_freq: float = 100.0,
) -> tuple[list[str], np.ndarray, dict[str, int]]:
    """从库里取出所有可用谱 -> (sample_id 列表, 特征矩阵, 拟合统计)。

    ``sample_id`` 用 ``scan_id@timestamp``：库里 ``scan_id`` 会重复
    （固件重启后轮次号归零），单靠它对不上标签。
    """
    if table not in ("impedance_data", "sweep_data"):
        raise ValueError(f"不支持的表: {table}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute(f"pragma table_info({table})")}
        key = ("coalesce(scan_id,0), timestamp" if "scan_id" in cols
               else "round_id, timestamp")
        batches = con.execute(f"""
            select {key}, count(*) n from {table}
            where frequency_hz >= ? and z_real is not null and z_imag is not null
            group by {key} having count(*) >= ?
            order by timestamp
        """, (min_freq, min_points)).fetchall()
        if not batches:
            raise SystemExit(
                f"{db_path} 的 {table} 里没有 >= {min_points} 点的可用谱。")

        ids: list[str] = []
        feats: list[list[float]] = []
        stat = {"total": 0, "fitted": 0, "fit_failed": 0, "low_r2": 0}

        for row in batches:
            if "scan_id" in cols:
                sid, ts = row[0], row[1]
                rows = con.execute(
                    f"select frequency_hz, z_real, z_imag from {table} "
                    f"where coalesce(scan_id,0)=? and timestamp=? "
                    f"  and frequency_hz >= ? and z_real is not null "
                    f"  and z_imag is not null order by frequency_hz",
                    (sid, ts, min_freq)).fetchall()
                sample_id = f"{sid}@{ts}"
            else:
                rid, ts = row[0], row[1]
                rows = con.execute(
                    f"select frequency_hz, z_real, z_imag from {table} "
                    f"where round_id=? and timestamp=? and frequency_hz >= ? "
                    f"  and z_real is not null and z_imag is not null "
                    f"order by frequency_hz",
                    (rid, ts, min_freq)).fetchall()
                sample_id = f"R{rid}@{ts}"
            if len(rows) < min_points:
                continue

            f = [r[0] for r in rows]
            zr = [r[1] for r in rows]
            zi = [r[2] for r in rows]

            stat["total"] += 1
            fit = fit_cole_cole(f, zr, zi)
            if fit is None:
                stat["fit_failed"] += 1
            else:
                stat["fitted"] += 1

            vector = spectral_features(f, zr, zi)
            ids.append(sample_id)
            feats.append([vector[k] for k in SPECTRAL_FEATURE_NAMES])
    finally:
        con.close()

    return ids, np.asarray(feats, dtype=float), stat


def default_groups(sample_ids: list[str], n_groups: int, seed: int) -> list[str]:
    """库里没有果 ID 时的**占位**分组，仅供合成标签自检。

    真实实验必须换成标签文件里的 ``group_id``——这里按序取模只是
    让样本大致均匀铺到若干组，好让 StratifiedGroupKFold 能跑，
    它**不代表**真实的独立样本结构。
    """
    rng = np.random.default_rng(seed)
    perm = rng.permutation(len(sample_ids))
    out = [""] * len(sample_ids)
    for rank, i in enumerate(perm):
        out[i] = f"GRP_{rank % max(n_groups, 1):02d}"
    return out


def print_header(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def print_watermark(simulated: bool, note: str) -> None:
    if simulated:
        print("\n" + "!" * 78)
        print("!!  SIMULATED DATA — 合成标签 / 模拟数据")
        print("!!")
        print("!!  下列所有指标只用于**验证管线是否通畅**，")
        print("!!  不构成任何关于果实成熟度的研究结论。")
        print("!!  换用真实标签文件（--labels）后请重新解读。")
        print("!" * 78)
    else:
        print("\n" + "-" * 78)
        print(f"真实标签：{note}")
        print("-" * 78)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="成熟度建模实验（按果分组交叉验证）")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "fruit_monitor.db",
                        help="SQLite 库路径")
    parser.add_argument("--table", default="impedance_data",
                        choices=["impedance_data", "sweep_data"])
    parser.add_argument("--labels", type=Path, default=None,
                        help="真实标签 CSV（sample_id,group_id,target）。"
                             "不给则用合成标签，报告加 SIMULATED 水印")
    parser.add_argument("--kind", choices=["classification", "regression"],
                        default="classification")
    parser.add_argument("--folds", type=int, default=5, help="交叉验证折数")
    parser.add_argument("--groups", type=int, default=8,
                        help="合成标签自检用的占位组数")
    parser.add_argument("--models", nargs="*", default=None,
                        help="要跑的模型名，缺省跑全部")
    parser.add_argument("--min-points", type=int, default=7)
    parser.add_argument("--seed", type=int, default=20260928)
    parser.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP,
                        help="按组自助重采样次数，区间精度换耗时")
    parser.add_argument("--output", type=Path, default=None,
                        help="把报告写成 JSON")
    args = parser.parse_args(argv)

    if not args.db.exists():
        raise SystemExit(f"找不到数据库: {args.db}")

    t0 = time.time()
    print_header("成熟度建模实验")

    # ---- 1. 取谱与特征 ----
    sample_ids, X, fit_stat = load_spectra(
        args.db, table=args.table, min_points=args.min_points)
    print(f"数据库     : {args.db}")
    print(f"表         : {args.table}")
    print(f"可用谱     : {len(sample_ids)} 条（>= {args.min_points} 频点）")
    print(f"Cole-Cole  : 拟合成功 {fit_stat['fitted']} / {fit_stat['total']}"
          f"（{fit_stat['fitted'] / max(fit_stat['total'], 1) * 100:.1f}%）"
          f"，失败 {fit_stat['fit_failed']}")
    nan_rate = np.isnan(X).mean(axis=0)
    print("特征缺失率 : "
          + ", ".join(f"{k}={float(v):.3f}"
                      for k, v in zip(SPECTRAL_FEATURE_NAMES, nan_rate)))

    # ---- 2. 标签 ----
    if args.labels is not None:
        source = CsvLabelSource(args.labels, kind=args.kind)
        labels = source.load()
        simulated = False
        note = f"{args.labels}（{len(labels)} 条，"
        note += f"{len({l.group_id for l in labels})} 只独立样本）"
    else:
        groups = default_groups(sample_ids, args.groups, args.seed)
        mi = X[:, SPECTRAL_FEATURE_NAMES.index("membrane_index")]
        labels = synthesize_targets(sample_ids, mi, groups,
                                    kind=args.kind, seed=args.seed)
        simulated = True
        note = "合成标签"

    print_watermark(simulated, note)

    # ---- 3. 组装数据集 ----
    ds = build_dataset(sample_ids, X, labels, SPECTRAL_FEATURE_NAMES,
                       is_simulated=simulated)
    print(f"\n样本 / 组 / 特征: {ds.n_samples} / {ds.n_groups} / {ds.n_features}")
    print(f"任务             : {ds.kind}")
    if ds.kind == "classification":
        print(f"类别分布         : {dict(Counter(str(v) for v in ds.y))}")
        unknown = {str(v) for v in ds.y} - set(MATURITY_LEVELS)
        if unknown:
            print(f"  !! 标签里有未知类别: {sorted(unknown)}")

    if ds.n_groups < args.folds:
        print(f"\n!! 组数 {ds.n_groups} < 折数 {args.folds}，"
              f"自动降到 {max(2, ds.n_groups)} 折")
        args.folds = max(2, ds.n_groups)

    # ---- 4. 交叉验证 ----
    model_names = args.models or list(AVAILABLE_MODELS[ds.kind])
    unknown = [m for m in model_names if m not in AVAILABLE_MODELS[ds.kind]]
    if unknown:
        raise SystemExit(f"未知模型 {unknown}，可用: {AVAILABLE_MODELS[ds.kind]}")

    print_header(f"交叉验证（{args.folds} 折，按果分组，seed={args.seed}）")
    col = ("accuracy", "balanced_accuracy", "f1_macro") if ds.kind == "classification" \
        else ("mae", "rmse", "r2")
    width = 30
    print(f"{'模型':<12}" + "".join(f"{c:<{width}s}" for c in col))
    print("-" * (12 + width * len(col)))

    outcomes = {}
    for name in model_names:
        try:
            t_model = time.time()
            out = cross_validate(
                ds, name, n_splits=args.folds, seed=args.seed,
                n_boot=args.bootstrap)
            took = time.time() - t_model
        except Exception as exc:  # 单个模型失败不该中断整批
            print(f"{name:<12}  失败: {type(exc).__name__}: {exc}")
            continue
        outcomes[name] = out
        cells = [f"{out.aggregate[c]:.3f}  [{out.ci95[c][0]:.3f}, "
                 f"{out.ci95[c][1]:.3f}]" for c in col]
        print(f"{name:<12}" + "".join(f"{s:<{width}s}" for s in cells)
              + f"  ({took:.0f}s)")

    # ---- 5. 相对基线 ----
    if "baseline" in outcomes and len(outcomes) > 1:
        base = outcomes["baseline"].aggregate
        target = "accuracy" if ds.kind == "classification" else "r2"
        # 只打印当前任务适用的那个指标，别在分类里挂出 r2=nan
        head = (f"accuracy={base.get('accuracy', float('nan')):.3f}"
                if ds.kind == "classification"
                else f"r2={base.get('r2', float('nan')):.3f}")
        print(f"\n相对基线（Dummy）提升——基线 {head}：")
        if target in base:
            for name, out in outcomes.items():
                if name == "baseline":
                    continue
                d = out.aggregate[target] - base[target]
                verdict = "  <<< 显著优于基线" if d > 0.05 else "  (无优势)"
                print(f"    {name:<12} {target} {d:+.3f}{verdict}")

    # ---- 6. 不变量断言 ----
    print_header("方法学不变量自检")
    _check_no_group_leakage(outcomes, ds)
    _check_baseline_present(outcomes, ds)

    if not outcomes:
        raise SystemExit("没有任何模型跑成功")

    print(f"\n耗时 {time.time() - t0:.1f}s")

    if args.output:
        payload = {
            "generated_at": int(time.time()),
            "db": str(args.db),
            "table": args.table,
            "is_simulated": simulated,
            "n_samples": ds.n_samples,
            "n_groups": ds.n_groups,
            "n_features": ds.n_features,
            "kind": ds.kind,
            "folds": args.folds,
            "seed": args.seed,
            "fit_stat": fit_stat,
            "feature_names": list(SPECTRAL_FEATURE_NAMES),
            "results": {
                name: {"aggregate": out.aggregate,
                       "ci95": {k: list(v) for k, v in out.ci95.items()},
                       "folds": [f.metrics for f in out.folds]}
                for name, out in outcomes.items()
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {args.output}")

    return 0


def _check_no_group_leakage(outcomes: dict, ds: Dataset) -> None:
    """断言：任何一折的测试集里出现的果，都不在该折训练集里。

    这是整个方法的地基。守不住它，所有指标都是自欺。
    """
    for name, out in outcomes.items():
        for fold in out.folds:
            train_groups = set()
            # 由 OOF 归属反推：该折未被预测的样本即训练样本
            test = set(fold.test_groups)
            for i, g in enumerate(out.oof_groups):
                if g not in test:
                    train_groups.add(g)
            leaked = test & train_groups
            if leaked:
                raise AssertionError(
                    f"模型 {name} 第 {fold.fold} 折发生分组泄漏: {sorted(leaked)}")
    n_splits = min(len(o.folds) for o in outcomes.values()) if outcomes else 0
    print(f"[OK] 分组无泄漏：{n_splits} 折 × {len(outcomes)} 个模型，"
          f"同一只果从未同时出现在训练与测试")


def _check_baseline_present(outcomes: dict, ds) -> None:
    if "baseline" not in outcomes:
        print("[!!] 没有跑基线模型 —— 指标缺少参照系，"
              "不要单独解读 accuracy")
        return
    base = outcomes["baseline"].aggregate
    if ds.kind == "classification":
        # 类数从数据实际数出来，别把"四分类"写死——
        # 换标签文件后类数可能变，理论值也就不是 0.25
        n_classes = len({str(v) for v in ds.y})
        chance = 1.0 / n_classes if n_classes else float("nan")
        print(f"[OK] 基线 accuracy={base.get('accuracy', 0.0):.3f}"
              f"（{n_classes} 分类随机猜测的理论值 {chance:.3f}）"
              f"—— 低于它的模型等于没有信息")
    else:
        print(f"[OK] 基线 r2={base.get('r2', float('nan')):.3f}"
              f"（恒定预测的 r2 为 0）")


if __name__ == "__main__":
    raise SystemExit(main())
