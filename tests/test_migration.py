from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from migration.data_manager import DatasetManager
from migration.stats_analysis import load_eis_data, analyze_single_sample, analyze_label_group
from migration.ml_model import read_ml_csv, train_models
from migration.perf_report import generate_report


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_csv(self, path: Path, rows):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    def test_dataset_manager_lifecycle(self):
        src = self.root / "sample.csv"
        self._write_csv(src, [
            {"a": "1", "b": "2"},
            {"a": "3", "b": "4"},
        ])

        mgr = DatasetManager(self.root / "data")
        item = mgr.add(src)
        self.assertTrue(Path(item["path"]).exists())
        self.assertEqual(len(mgr.list()), 1)
        self.assertEqual(mgr.get(item["id"])["id"], item["id"])
        mgr.set_active(item["id"])
        self.assertEqual(mgr.get_active()["active_id"], item["id"])

        mgr.remove(item["id"])
        self.assertFalse(Path(item["path"]).exists())
        self.assertEqual(len(mgr.list()), 0)
        self.assertIsNone(mgr.get_active()["active_id"])

    def test_stats_analysis_plots(self):
        src = self.root / "eis.csv"
        rows = [
            {"Sample": "S1", "Frequency": 10, "Real": 100, "Imag": -10},
            {"Sample": "S1", "Frequency": 100, "Real": 50, "Imag": -20},
            {"Sample": "S1", "Frequency": 1000, "Real": 25, "Imag": -5},
        ]
        self._write_csv(src, rows)

        df = load_eis_data(src)
        self.assertIn("Z_mag", df.columns)
        self.assertIn("Phase", df.columns)

        save_dir = self.root / "stats_out"
        stats = analyze_single_sample(df, "S1", save_dir)
        self.assertEqual(stats["label"], "S1")
        group = analyze_label_group(df, "S1", save_dir)
        self.assertIn("band_mean", group)
        self.assertTrue((save_dir / "S1_box.png").exists())
        self.assertTrue((save_dir / "label_S1_mean_std.png").exists())

    def test_ml_training_basic(self):
        src = self.root / "ml.csv"
        rows = [
            {"PC1": 1.0, "PC2": 1.0, "Target": "A", "Label": "A1"},
            {"PC1": 1.5, "PC2": 1.2, "Target": "A", "Label": "A2"},
            {"PC1": 2.0, "PC2": 1.5, "Target": "A", "Label": "A3"},
            {"PC1": 2.5, "PC2": 1.8, "Target": "A", "Label": "A4"},
            {"PC1": 3.0, "PC2": 2.1, "Target": "A", "Label": "A5"},
            {"PC1": 5.0, "PC2": 6.0, "Target": "B", "Label": "B1"},
            {"PC1": 5.5, "PC2": 6.2, "Target": "B", "Label": "B2"},
            {"PC1": 6.0, "PC2": 6.5, "Target": "B", "Label": "B3"},
            {"PC1": 6.5, "PC2": 6.8, "Target": "B", "Label": "B4"},
            {"PC1": 7.0, "PC2": 7.1, "Target": "B", "Label": "B5"},
        ]
        self._write_csv(src, rows)

        df = read_ml_csv(src)
        self.assertEqual(len(df), 10)

        result = train_models(
            data_path=src,
            model_keys=["lr", "knn"],
            output_dir=self.root / "ml_plots",
            model_dir=self.root / "ml_models",
            dataset_key="test",
            test_size=0.25,
        )
        self.assertNotIn("error", result)
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(len(result["labels"]), 2)
        self.assertTrue((self.root / "ml_models" / f"test_lr_{result['run_id']}.pkl").exists())

    def test_perf_report_outputs(self):
        results_csv = self.root / "perf_results.csv"
        summary_csv = self.root / "perf_summary.csv"
        self._write_csv(results_csv, [
            {"endpoint": "page_data", "method": "GET", "path": "/data", "ok": 1, "status": 200, "ms": 10.5, "error": ""},
            {"endpoint": "page_data", "method": "GET", "path": "/data", "ok": 1, "status": 200, "ms": 11.5, "error": ""},
            {"endpoint": "api_ml_datasets", "method": "GET", "path": "/api/ml/datasets", "ok": 1, "status": 200, "ms": 12.5, "error": ""},
        ])
        self._write_csv(summary_csv, [
            {"endpoint": "page_data", "count": 2, "ok": 2, "fail": 0, "avg_ms": 11.0, "p95_ms": 11.5, "min_ms": 10.5, "max_ms": 11.5},
            {"endpoint": "api_ml_datasets", "count": 1, "ok": 1, "fail": 0, "avg_ms": 12.5, "p95_ms": 12.5, "min_ms": 12.5, "max_ms": 12.5},
        ])

        out_xlsx = self.root / "report.xlsx"
        out_png = self.root / "report.png"
        result = generate_report(results_csv, summary_csv, out_xlsx, out_png)
        self.assertTrue(result["plot_path"])
        self.assertTrue(out_png.exists())


if __name__ == "__main__":
    unittest.main()
