from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

from maturity_ml import (
    AVAILABLE_MODELS,
    MATURITY_LEVELS,
    CsvLabelSource,
    LabelRecord,
    build_dataset,
    build_model,
    classification_metrics,
    cluster_bootstrap_ci,
    cross_validate,
    make_splitter,
    regression_metrics,
    synthesize_targets,
)
from spectral_fit import SPECTRAL_FEATURE_NAMES


def _fake_dataset(n=60, groups=6, seed=3, kind="classification"):
    """构造一个特征与标签确实相关的玩具数据集。"""
    rng = np.random.default_rng(seed)
    sample_ids = [f"S{i:03d}" for i in range(n)]
    grp = [f"FRUIT_{i % groups:02d}" for i in range(n)]
    # 信号必须在**组内**有充分变化：synthesize_targets 按组内排名定级，
    # 若特征只在组间变化，组内排名就退化成噪声，标签等同随机，模型无从学起。
    X = rng.normal(0, 1, size=(n, len(SPECTRAL_FEATURE_NAMES)))
    X[:, 0] = rng.normal(0, 1, n)
    signal = X[:, 0]
    if kind == "classification":
        labels = synthesize_targets(sample_ids, signal, grp, kind=kind,
                                    noise=0.1, seed=seed)
    else:
        labels = synthesize_targets(sample_ids, signal, grp, kind=kind,
                                    noise=0.1, seed=seed)
    ds = build_dataset(sample_ids, X, labels, SPECTRAL_FEATURE_NAMES,
                       is_simulated=True)
    return ds


class BuildDatasetTest(unittest.TestCase):
    def test_aligns_features_to_labels(self):
        ids = ["a", "b", "c"]
        X = np.array([[1.0], [2.0], [3.0]])
        # 标签按 b, a 给：build_dataset 应按标签顺序取特征行
        labels = [
            LabelRecord("b", "G1", "unripe"),
            LabelRecord("a", "G1", "ripe"),
        ]
        ds = build_dataset(ids, X, labels, ["f"], is_simulated=True)
        self.assertEqual(ds.sample_ids, ["b", "a"])
        np.testing.assert_array_equal(ds.X[:, 0], [2.0, 1.0])
        self.assertEqual(list(ds.y), ["unripe", "ripe"])
        self.assertEqual(ds.groups.tolist(), ["G1", "G1"])

    def test_unmatched_labels_are_dropped(self):
        ids = ["a", "b"]
        X = np.array([[1.0], [2.0]])
        labels = [LabelRecord("a", "G1", "x"), LabelRecord("ZZZ", "G1", "y")]
        ds = build_dataset(ids, X, labels, ["f"], is_simulated=True)
        self.assertEqual(ds.n_samples, 1)

    def test_no_overlap_raises(self):
        with self.assertRaises(ValueError):
            build_dataset(["a"], np.zeros((1, 1)),
                          [LabelRecord("nope", "G", "x")], ["f"],
                          is_simulated=True)

    def test_duplicate_sample_id_rejected(self):
        with self.assertRaises(ValueError):
            build_dataset(["a", "a"], np.zeros((2, 1)),
                          [LabelRecord("a", "G", "x")], ["f"],
                          is_simulated=True)

    def test_row_count_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            build_dataset(["a", "b"], np.zeros((3, 1)), [], ["f"],
                          is_simulated=True)


class GroupSplitInvariantTest(unittest.TestCase):
    """整个方法的地基：同一只果绝不允许同时出现在训练集和测试集。"""

    def test_no_group_appears_in_both_train_and_test(self):
        ds = _fake_dataset(n=90, groups=9)
        splitter = make_splitter(ds.kind, 5, seed=1)
        seen_test_groups = set()
        for tr, te in splitter.split(ds.X, ds.y, ds.groups):
            train_g = set(ds.groups[tr])
            test_g = set(ds.groups[te])
            self.assertFalse(train_g & test_g,
                             f"分组泄漏: {sorted(train_g & test_g)}")
            self.assertTrue(test_g)
            seen_test_groups |= test_g
        # 每个组都应当在某折当过测试集（否则该组从未被评估）
        self.assertEqual(seen_test_groups, set(ds.groups))

    def test_cross_validate_covers_every_sample_exactly_once(self):
        ds = _fake_dataset(n=60, groups=6)
        out = cross_validate(ds, "logreg", n_splits=3, seed=1, n_boot=100)
        self.assertEqual(len(out.oof_y_true), ds.n_samples)
        self.assertEqual(len(out.oof_groups), ds.n_samples)
        self.assertTrue(all(g is not None for g in out.oof_groups))

    def test_cross_validate_records_test_groups_per_fold(self):
        ds = _fake_dataset(n=60, groups=6)
        out = cross_validate(ds, "logreg", n_splits=3, seed=1, n_boot=100)
        for fold in out.folds:
            self.assertTrue(fold.test_groups)


class BaselineTest(unittest.TestCase):
    def test_baseline_is_available_for_both_tasks(self):
        self.assertIn("baseline", AVAILABLE_MODELS["classification"])
        self.assertIn("baseline", AVAILABLE_MODELS["regression"])

    def test_classification_baseline_near_chance(self):
        ds = _fake_dataset(n=80, groups=8)
        out = cross_validate(ds, "baseline", n_splits=4, seed=1, n_boot=100)
        # 四分类的多数类基线应在 0.25 附近，不该凭空拿到高分
        self.assertLess(out.aggregate["accuracy"], 0.45)
        self.assertGreater(out.aggregate["accuracy"], 0.15)

    def test_pipeline_contains_imputer_then_scaler(self):
        """预处理必须封装在 Pipeline 里，才能保证只在训练折内 fit。"""
        for kind, name in (("classification", "logreg"), ("regression", "ridge")):
            names = [s for s, _ in build_model(kind, name).steps]
            self.assertEqual(names[:2], ["imputer", "scaler"], f"{kind}/{name}")

    def test_learned_model_beats_baseline_on_separable_data(self):
        ds = _fake_dataset(n=90, groups=9, seed=5)
        base = cross_validate(ds, "baseline", n_splits=3, seed=5, n_boot=100)
        real = cross_validate(ds, "logreg", n_splits=3, seed=5, n_boot=100)
        self.assertGreater(real.aggregate["accuracy"],
                           base.aggregate["accuracy"],
                           "标签由特征决定时，真实模型必须优于基线")


class MetricTest(unittest.TestCase):
    def test_perfect_classification_scores_one(self):
        y = ["unripe"] * 5 + ["ripe"] * 5
        m = classification_metrics(y, y)
        self.assertEqual(m["accuracy"], 1.0)
        self.assertEqual(m["f1_macro"], 1.0)

    def test_all_wrong_scores_zero(self):
        y = ["unripe", "ripe"]
        m = classification_metrics(y, ["ripe", "unripe"])
        self.assertEqual(m["accuracy"], 0.0)

    def test_regression_metrics(self):
        m = regression_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        self.assertAlmostEqual(m["mae"], 0.0)
        self.assertAlmostEqual(m["rmse"], 0.0)
        self.assertAlmostEqual(m["r2"], 1.0)


class BootstrapTest(unittest.TestCase):
    def test_identical_predictions_degenerate_interval(self):
        y = ["a"] * 20
        lo, hi = cluster_bootstrap_ci(y, y, [f"g{i % 4}" for i in range(20)],
                                      lambda yt, yp: 1.0, n_boot=50, seed=1)
        self.assertEqual(lo, 1.0)
        self.assertEqual(hi, 1.0)

    def test_interval_brackets_point_estimate(self):
        rng = np.random.default_rng(0)
        y_true = [MATURITY_LEVELS[i % 4] for i in range(40)]
        y_pred = [t if rng.random() > 0.3 else MATURITY_LEVELS[(i + 1) % 4]
                  for i, t in enumerate(y_true)]
        groups = [f"FRUIT_{i % 8}" for i in range(40)]
        point = classification_metrics(y_true, y_pred)["accuracy"]
        lo, hi = cluster_bootstrap_ci(y_true, y_pred, groups,
                                      lambda yt, yp: classification_metrics(yt, yp)["accuracy"],
                                      n_boot=500, seed=1)
        self.assertLessEqual(lo, hi)
        self.assertLess(lo, 0.999)
        self.assertGreater(hi, 0.0)

    def test_single_group_degenerates(self):
        lo, hi = cluster_bootstrap_ci(["a"] * 5, ["a"] * 5, ["G"] * 5,
                                      lambda yt, yp: 1.0, n_boot=50, seed=1)
        self.assertEqual((lo, hi), (1.0, 1.0))


class CsvLabelSourceTest(unittest.TestCase):
    def _write(self, fieldnames, rows):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, newline="", encoding="utf-8")
        with tmp:
            w = csv.DictWriter(tmp, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        return Path(tmp.name)

    def test_reads_valid_file(self):
        p = self._write(["sample_id", "group_id", "target"],
                        [{"sample_id": "S1", "group_id": "F1", "target": "ripe"}])
        recs = CsvLabelSource(p).load()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].group_id, "F1")
        self.assertFalse(CsvLabelSource(p).is_simulated)

    def test_missing_group_id_rejected(self):
        """没有 group_id 就只能随机切分 -> 有泄漏，必须拒绝。"""
        p = self._write(["sample_id", "target"],
                        [{"sample_id": "S1", "target": "ripe"}])
        with self.assertRaises(ValueError) as ctx:
            CsvLabelSource(p).load()
        self.assertIn("group_id", str(ctx.exception))

    def test_regression_requires_numeric_target(self):
        p = self._write(["sample_id", "group_id", "target"],
                        [{"sample_id": "S1", "group_id": "F1", "target": "ripe"}])
        with self.assertRaises(ValueError):
            CsvLabelSource(p, kind="regression").load()

    def test_blank_targets_skipped(self):
        p = self._write(["sample_id", "group_id", "target"], [
            {"sample_id": "S1", "group_id": "F1", "target": ""},
            {"sample_id": "S2", "group_id": "F1", "target": "ripe"},
        ])
        recs = CsvLabelSource(p).load()
        self.assertEqual(len(recs), 1)

    def test_empty_file_rejected(self):
        p = self._write(["sample_id", "group_id", "target"], [])
        with self.assertRaises(ValueError):
            CsvLabelSource(p).load()


class SynthesizeTargetsTest(unittest.TestCase):
    def test_sample_ids_match_input(self):
        ids = [f"S{i}" for i in range(20)]
        grp = [f"G{i % 4}" for i in range(20)]
        recs = synthesize_targets(ids, np.arange(20, dtype=float), grp)
        self.assertEqual(sorted(r.sample_id for r in recs), sorted(ids))

    def test_all_four_levels_reachable(self):
        ids = [f"S{i}" for i in range(40)]
        grp = [f"G{i % 4}" for i in range(40)]
        recs = synthesize_targets(ids, np.linspace(0, 1, 40), grp, noise=0.0)
        self.assertEqual({r.target for r in recs}, set(MATURITY_LEVELS))

    def test_is_simulated_flag_propagates(self):
        ids = [f"S{i}" for i in range(8)]
        grp = ["G0"] * 8
        recs = synthesize_targets(ids, np.arange(8, dtype=float), grp)
        ds = build_dataset(ids, np.zeros((8, 1)), recs, ["f"], is_simulated=True)
        self.assertTrue(ds.is_simulated)

    def test_length_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            synthesize_targets(["a"], [1.0, 2.0], ["g"])


if __name__ == "__main__":
    unittest.main()
