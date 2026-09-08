"""阻抗数据分析与训练模块。"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from db import Database


FEATURE_NAMES = [
    "magnitude_mean",
    "magnitude_median",
    "magnitude_min",
    "magnitude_max",
    "magnitude_spread",
    "phase_mean",
    "phase_max",
    "phase_min",
    "z_real_mean",
    "z_imag_mean",
    "valid_fraction",
]

MATURITY_CLASSES = ["unripe", "ripe", "overripe"]


def _safe_mean(values: Iterable[float]) -> float:
    arr = [v for v in values if v is not None]
    return float(np.mean(arr)) if arr else 0.0


def _safe_std(values: Iterable[float]) -> float:
    arr = [v for v in values if v is not None]
    return float(np.std(arr)) if arr else 0.0


def _safe_min(values: Iterable[float]) -> float:
    arr = [v for v in values if v is not None]
    return float(min(arr)) if arr else 0.0


def _safe_max(values: Iterable[float]) -> float:
    arr = [v for v in values if v is not None]
    return float(max(arr)) if arr else 0.0


def extract_features(rows: list[dict[str, Any]]) -> dict[str, float]:
    magnitudes = [float(r["magnitude"]) for r in rows if r.get("magnitude") is not None]
    phases = [float(r["phase"]) for r in rows if r.get("phase") is not None]
    z_real = [float(r["z_real"]) for r in rows if r.get("z_real") is not None]
    z_imag = [float(r["z_imag"]) for r in rows if r.get("z_imag") is not None]
    valid_rows = [1.0 if r.get("in_valid_window") else 0.0 for r in rows]

    return {
        "magnitude_mean": _safe_mean(magnitudes),
        "magnitude_median": float(np.median(magnitudes)) if magnitudes else 0.0,
        "magnitude_min": _safe_min(magnitudes),
        "magnitude_max": _safe_max(magnitudes),
        "magnitude_spread": _safe_std(magnitudes),
        "phase_mean": _safe_mean(phases),
        "phase_max": _safe_max(phases),
        "phase_min": _safe_min(phases),
        "z_real_mean": _safe_mean(z_real),
        "z_imag_mean": _safe_mean(z_imag),
        "valid_fraction": float(np.mean(valid_rows)) if rows else 0.0,
    }


def load_scans(db: Database, gateway_id: str, node_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.query_impedance_history(gateway_id, node_id, limit=limit)
    scans: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("scan_id") or row.get("timestamp")
        scans.setdefault(key, {"scan_id": row.get("scan_id"), "timestamp": row.get("timestamp"), "rows": []})
        scans[key]["rows"].append(row)
    return sorted(scans.values(), key=lambda item: (item["timestamp"] or 0, str(item["scan_id"] or "")))


def synthesize_labels(scans: list[dict[str, Any]]) -> list[str]:
    if not scans:
        return []
    if len(scans) < 3:
        return [MATURITY_CLASSES[0]] * len(scans)
    thirds = np.array_split(np.arange(len(scans)), 3)
    labels: list[str] = []
    for idx, idx_group in enumerate(thirds):
        labels.extend([MATURITY_CLASSES[idx]] * len(idx_group))
    return labels


@dataclass
class TrainingResult:
    model: dict[str, Any]
    rows: int
    classes: list[str]
    feature_names: list[str]


class ThresholdModel:
    def __init__(self, model: dict[str, Any]) -> None:
        self._model = model

    def predict(self, feature_vector: dict[str, float]) -> str:
        score = feature_vector.get("magnitude_mean", 0.0)
        threshold = float(self._model.get("threshold", 0.0))
        return "overripe" if score >= threshold else "unripe"


class CentroidModel:
    def __init__(self, model: dict[str, Any]) -> None:
        self._model = model

    def predict(self, feature_vector: dict[str, float]) -> str:
        vectors = np.array([float(feature_vector.get(name, 0.0)) for name in self._model["features"]])
        best_label = self._model["classes"][0]
        best_distance = float("inf")
        for label, centroid in self._model["centroids"].items():
            distance = float(np.linalg.norm(vectors - np.array(centroid)))
            if distance < best_distance:
                best_distance = distance
                best_label = label
        return best_label


def train_model(model_type: str, scans: list[dict[str, Any]], labels: list[str]) -> TrainingResult:
    features = pd.DataFrame([extract_features(scan["rows"]) for scan in scans])
    if len(labels) != len(features):
        raise ValueError("labels and scans length mismatch")

    labels_df = pd.Series(labels, name="label")
    if model_type == "threshold":
        threshold = float(np.percentile(features["magnitude_mean"], 66.7))
        model = {
            "type": "threshold",
            "threshold": threshold,
            "features": FEATURE_NAMES,
            "classes": MATURITY_CLASSES,
        }
        return TrainingResult(model, len(features), MATURITY_CLASSES, FEATURE_NAMES)

    if model_type == "centroid":
        classes = sorted(set(labels_df.tolist()))
        centroids = {label: features[labels_df == label].mean(axis=0).tolist() for label in classes}
        model = {
            "type": "centroid",
            "features": FEATURE_NAMES,
            "classes": classes,
            "centroids": centroids,
        }
        return TrainingResult(model, len(features), classes, FEATURE_NAMES)

    raise ValueError(f"unsupported model_type: {model_type}")


def predict_scan(model: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    features = extract_features(rows)
    if model["type"] == "threshold":
        return ThresholdModel(model).predict(features)
    if model["type"] == "centroid":
        return CentroidModel(model).predict(features)
    raise ValueError(f"unsupported model type: {model['type']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="阻抗数据分析与训练")
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--model", choices=["threshold", "centroid"], default="centroid")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--config", type=Path, default=Path("config/config.json"))
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    config_path = args.config
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    db_path = args.db or Path(config.get("database", {}).get("path", "data/fruit_monitor.db"))
    if not db_path.is_absolute():
        db_path = config_path.parent.parent / db_path
    db = Database(db_path)

    try:
        scans = load_scans(db, args.gateway, args.node, limit=args.limit)
        if not scans:
            print("no impedance scans found")
            return 1

        labels = synthesize_labels(scans)
        result = train_model(args.model, scans, labels)
        prediction = predict_scan(result.model, scans[-1]["rows"])

        payload = {
            "gateway_id": args.gateway,
            "node_id": args.node,
            "model_type": result.model["type"],
            "rows": result.rows,
            "classes": result.classes,
            "prediction": prediction,
            "trained_at": int(datetime.now(tz=timezone.utc).timestamp()),
            "model": result.model,
        }

        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
