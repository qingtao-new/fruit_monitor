"""离线分析 CLI：给定**真实标签**，评估阻抗谱 → 成熟度的预测能力。

为什么推倒重写
--------------

旧版有三处过不了学术审查，且每处都会产出看起来像结论、实际无意义的数字：

1. ``synthesize_labels`` 把谱按时间三等分贴标签。这是循环论证——先假定
   "随时间变熟"，再拿时间当标签训练，模型学的只是时间序号，
   任何 accuracy 都是自证。
2. ``ThresholdModel`` 根本不读标签：阈值取 ``magnitude_mean`` 的 66.7
   分位数，只比较大小。那是无监督分箱，却挂在有监督框架下；
   而且只有两端输出，三分类里的 ``ripe`` 永远不可达。
3. ``CentroidModel`` 拿未标准化的特征算欧氏距离：``magnitude_max``≈10⁴
   会完全压过 ``valid_fraction``≈1.0，距离度量本身没有意义。

并且全无训练/测试划分、无交叉验证、不报任何指标。

现在的做法
----------

本模块只做"取谱 + 装配 + 报告"，**所有方法学环节都交给
:mod:`maturity_ml`**（按果分组切分、折内预处理、基线对比、按组自助
置信区间），特征交给 :mod:`spectral_fit`（Cole-Cole 参数化拟合）。
一个项目里只该有一套方法学实现，两套并存迟早分叉。

**没有标签就不出数**。本 CLI 强制要求 ``--labels``；想验证管线是否
通畅，请用 ``scripts/run_experiment.py``，它会用合成标签并强制加
``SIMULATED`` 水印——那个数字永远不能写进结论。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maturity_ml import (  # noqa: E402
    AVAILABLE_MODELS,
    CsvLabelSource,
    build_dataset,
    cross_validate,
)
from spectral_fit import (  # noqa: E402
    SPECTRAL_FEATURE_NAMES,
    fit_cole_cole,
    spectral_features,
)

# 与 maturity_ml.SPECTRAL_FEATURE_NAMES 同源，保留模块级常量便于旧调用方
FEATURE_NAMES: list[str] = list(SPECTRAL_FEATURE_NAMES)

LABELS_HELP = (
    "真实标签 CSV，三列 sample_id,group_id,target。\n"
    "group_id 是必填的切分键——一只果一个组，缺了它同一只果的多次测量\n"
    "会同时落进训练和测试，精度虚高。\n"
    "\n"
    "sample_id 用 '<scan_id>@<timestamp>'，可由 --dump-samples 打出。"
)


def dump_samples(db_path: Path, gateway: str, node: str,
                 limit: int) -> list[str]:
    """列出库里可用谱的 sample_id，供你去填标签 CSV。"""
    ids, _, _ = load_spectra(db_path, gateway_id=gateway, node_id=node,
                             limit=limit)
    return ids


def load_spectra(
    db_path: Path,
    *,
    gateway_id: str,
    node_id: str,
    limit: int = 200,
    min_points: int = 7,
    min_freq: float = 100.0,
) -> tuple[list[str], np.ndarray, dict[str, int]]:
    """取某节点的可用谱 -> (sample_id, 特征矩阵, 拟合统计)。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("pragma table_info(impedance_data)")}
        if "scan_id" not in cols or "gateway_id" not in cols:
            raise SystemExit("impedance_data 表结构不符，缺 scan_id/gateway_id")

        batches = con.execute("""
            select coalesce(scan_id,0), timestamp, count(*)
            from impedance_data
            where gateway_id = ? and node_id = ?
              and frequency_hz >= ? and z_real is not null and z_imag is not null
            group by 1, 2 having count(*) >= ?
            order by timestamp desc
            limit ?
        """, (gateway_id, node_id, min_freq, min_points, limit)).fetchall()
        if not batches:
            raise SystemExit(
                f"{db_path} 里 {gateway_id}/{node_id} 没有 >= {min_points} 点的谱。")

        ids: list[str] = []
        feats: list[list[float]] = []
        stat = {"total": 0, "fitted": 0, "fit_failed": 0}
        for sid, ts, _n in reversed(batches):
            rows = con.execute("""
                select frequency_hz, z_real, z_imag from impedance_data
                where gateway_id = ? and node_id = ?
                  and coalesce(scan_id,0) = ? and timestamp = ?
                  and frequency_hz >= ? and z_real is not null and z_imag is not null
                order by frequency_hz
            """, (gateway_id, node_id, sid, ts, min_freq)).fetchall()
            if len(rows) < min_points:
                continue
            f = [r[0] for r in rows]
            zr = [r[1] for r in rows]
            zi = [r[2] for r in rows]

            stat["total"] += 1
            if fit_cole_cole(f, zr, zi) is None:
                stat["fit_failed"] += 1
            else:
                stat["fitted"] += 1

            vector = spectral_features(f, zr, zi)
            ids.append(f"{sid}@{ts}")
            feats.append([vector[k] for k in SPECTRAL_FEATURE_NAMES])
    finally:
        con.close()
    if not ids:
        raise SystemExit("没有可用谱")
    return ids, np.asarray(feats, dtype=float), stat


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="阻抗谱成熟度分析（需要真实标签）",
        epilog=LABELS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--labels", type=Path, default=None,
                        help="真实标签 CSV（必填，见文末说明）")
    parser.add_argument("--kind", choices=["classification", "regression"],
                        default="classification")
    parser.add_argument("--model", nargs="*", default=None,
                        help=f"模型名，可选 {list(AVAILABLE_MODELS['classification'])}"
                             " / 回归模型；缺省跑全部")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--limit", type=int, default=200,
                        help="最多取多少条谱")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config.json")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--dump-samples", action="store_true",
                        help="只打印可用谱的 sample_id，用来填标签 CSV")
    parser.add_argument("--seed", type=int, default=20260928)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def _resolve_db(args: argparse.Namespace) -> Path:
    if args.db is not None:
        return args.db
    if args.config.exists():
        with open(args.config, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        raw = Path(config.get("database", {}).get("path", "data/fruit_monitor.db"))
        if not raw.is_absolute():
            raw = args.config.parent.parent / raw
        return raw
    return ROOT / "data" / "fruit_monitor.db"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    db_path = _resolve_db(args)
    if not db_path.exists():
        raise SystemExit(f"找不到数据库: {db_path}")

    if args.dump_samples:
        for sid in dump_samples(db_path, args.gateway, args.node, args.limit):
            print(sid)
        return 0

    # ---- 硬门槛：没有真实标签就不给数字 ----
    if args.labels is None:
        raise SystemExit(
            "缺少 --labels。\n"
            "\n"
            "本脚本**拒绝**用合成标签出数：按时间顺序编造的标签是循环论证，\n"
            "由此得到的 accuracy 没有任何含义，也不该出现在报告或论文里。\n"
            "\n"
            "  · 要真实评估  : 用 --dump-samples 打出 sample_id，填好 CSV 后传入\n"
            "  · 要验证管线  : 跑 scripts/run_experiment.py（报告强制加 SIMULATED 水印）\n"
        )

    t0 = time.time()
    sample_ids, X, fit_stat = load_spectra(
        db_path, gateway_id=args.gateway, node_id=args.node, limit=args.limit)
    print(f"数据库         : {db_path}")
    print(f"可用谱         : {len(sample_ids)}")
    print(f"Cole-Cole 拟合 : {fit_stat['fitted']}/{fit_stat['total']}"
          f" ({fit_stat['fitted'] / max(fit_stat['total'], 1) * 100:.1f}%)")

    source = CsvLabelSource(args.labels, kind=args.kind)
    labels = source.load()
    print(f"标签文件       : {args.labels}（{len(labels)} 条，"
          f"{len({l.group_id for l in labels})} 只独立样本）")

    ds = build_dataset(sample_ids, X, labels, SPECTRAL_FEATURE_NAMES,
                       is_simulated=False)
    print(f"可对齐样本     : {ds.n_samples}  组: {ds.n_groups}  "
          f"特征: {ds.n_features}")
    if ds.kind == "classification":
        print(f"类别分布       : {dict(Counter(str(v) for v in ds.y))}")

    if ds.n_groups < 2:
        raise SystemExit(
            f"只有 {ds.n_groups} 只独立样本，无法按组切分。"
            "样本太少时任何精度都不可信——先增加独立样本数。")
    folds = max(2, min(args.folds, ds.n_groups))
    if folds < args.folds:
        print(f"!! 折数降为 {folds}（组数不足）")

    model_names = args.model or list(AVAILABLE_MODELS[ds.kind])
    unknown = [m for m in model_names if m not in AVAILABLE_MODELS[ds.kind]]
    if unknown:
        raise SystemExit(f"未知模型 {unknown}，可用: {AVAILABLE_MODELS[ds.kind]}")

    print(f"\n{'=' * 74}\n交叉验证（{folds} 折，按果分组，seed={args.seed}）\n{'=' * 74}")
    col = (("accuracy", "balanced_accuracy", "f1_macro")
           if ds.kind == "classification" else ("mae", "rmse", "r2"))
    width = 30
    print(f"{'模型':<12}" + "".join(f"{c:<{width}s}" for c in col))
    print("-" * (12 + width * len(col)))

    outcomes = {}
    for name in model_names:
        try:
            out = cross_validate(ds, name, n_splits=folds, seed=args.seed)
        except Exception as exc:
            print(f"{name:<12}  失败: {type(exc).__name__}: {exc}")
            continue
        outcomes[name] = out
        cells = [f"{out.aggregate[c]:.3f}  [{out.ci95[c][0]:.3f}, "
                 f"{out.ci95[c][1]:.3f}]" for c in col]
        print(f"{name:<12}" + "".join(f"{s:<{width}s}" for s in cells))

    if not outcomes:
        raise SystemExit("没有任何模型跑成功")

    if "baseline" in outcomes:
        base = outcomes["baseline"].aggregate
        target = "accuracy" if ds.kind == "classification" else "r2"
        print(f"\n相对基线（Dummy）提升——基线 {target}="
              f"{base.get(target, float('nan')):.3f}：")
        for name, out in outcomes.items():
            if name == "baseline":
                continue
            d = out.aggregate[target] - base.get(target, 0.0)
            print(f"    {name:<12} {target} {d:+.3f}"
                  f"{'  <<< 优于基线' if d > 0.05 else '  (无优势)'}")
    else:
        print("\n!! 未跑基线 —— 指标缺少参照系，不要单独解读 accuracy")

    print(f"\n耗时 {time.time() - t0:.1f}s")

    if args.output:
        payload = {
            "generated_at": int(datetime.now(tz=timezone.utc).timestamp()),
            "db": str(db_path),
            "gateway_id": args.gateway,
            "node_id": args.node,
            "is_simulated": False,
            "labels_file": str(args.labels),
            "n_samples": ds.n_samples,
            "n_groups": ds.n_groups,
            "kind": ds.kind,
            "folds": folds,
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


if __name__ == "__main__":
    raise SystemExit(main())
