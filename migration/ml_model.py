from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import AdaBoostClassifier, RandomForestClassifier
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import LinearSVC, SVC
from sklearn.tree import DecisionTreeClassifier


MODEL_REGISTRY = {
    "lr": ("Logistic Regression", lambda: LogisticRegression(max_iter=2000)),
    "linear_svm": ("Linear SVM", lambda: LinearSVC()),
    "lda": ("LDA", lambda: LinearDiscriminantAnalysis()),
    "gnb": ("GaussianNB", lambda: GaussianNB()),
    "dt": ("Decision Tree", lambda: DecisionTreeClassifier(random_state=42)),
    "ada": ("AdaBoost", lambda: AdaBoostClassifier(n_estimators=200, random_state=42)),
    "svm": ("SVM (RBF)", lambda: SVC(kernel="rbf", gamma="scale")),
    "knn": ("KNN", lambda: KNeighborsClassifier(n_neighbors=5)),
    "rf": ("Random Forest", lambda: RandomForestClassifier(n_estimators=200, random_state=42)),
}

SCALED_MODELS = {"lr", "linear_svm", "svm", "knn"}


def read_ml_csv(path: str | Path) -> pd.DataFrame:
    encodings = ["utf-8-sig", "gbk", "gb2312"]
    last_err: Exception | None = None
    for enc in encodings:
        try:
            return pd.read_csv(path, encoding=enc)
        except Exception as err:
            last_err = err
    raise last_err  # type: ignore[misc]


def _build_estimator(model_key: str):
    name, factory = MODEL_REGISTRY[model_key]
    model = factory()
    if model_key in SCALED_MODELS:
        return name, Pipeline([("scaler", StandardScaler()), ("model", model)])
    return name, model


def _get_score_values(estimator, x_test):
    if hasattr(estimator, "predict_proba"):
        return estimator.predict_proba(x_test)
    if hasattr(estimator, "decision_function"):
        return estimator.decision_function(x_test)
    return None


def _plot_confusion_matrix(cm, labels, out_path, title):
    fig, ax = plt.subplots(figsize=(6, 5), dpi=130)
    im = ax.imshow(cm, cmap="Blues")
    ax.figure.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title(title)
    ax.set_xlabel("Predicted")
    ax.set_ylabel("True")
    ax.set_xticks(np.arange(len(labels)))
    ax.set_yticks(np.arange(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_yticklabels(labels)
    thresh = cm.max() / 2.0 if cm.size else 0
    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(
                j,
                i,
                str(cm[i, j]),
                ha="center",
                va="center",
                color="white" if cm[i, j] > thresh else "black",
            )
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _plot_decision_boundary(estimator, x, y, labels, out_path, title, sample_labels=None):
    x_min, x_max = x[:, 0].min() - 1, x[:, 0].max() + 1
    y_min, y_max = x[:, 1].min() - 1, x[:, 1].max() + 1
    xx, yy = np.meshgrid(
        np.linspace(x_min, x_max, 220),
        np.linspace(y_min, y_max, 220),
    )
    grid = np.c_[xx.ravel(), yy.ravel()]
    preds = estimator.predict(grid)
    label_to_idx = {label: idx for idx, label in enumerate(labels)}
    z = np.array([label_to_idx[p] for p in preds]).reshape(xx.shape)

    fig, ax = plt.subplots(figsize=(6, 5), dpi=130)
    ax.contourf(xx, yy, z, alpha=0.25, cmap="tab10")
    dx = (x_max - x_min) * 0.01
    dy = (y_max - y_min) * 0.01
    for label in labels:
        mask = y == label
        ax.scatter(
            x[mask, 0],
            x[mask, 1],
            s=18,
            label=label,
            alpha=0.85,
            edgecolors="white",
            linewidths=0.4,
        )
        if sample_labels is not None:
            for idx in np.where(mask)[0]:
                text = sample_labels[idx] if idx < len(sample_labels) else ""
                if text:
                    ax.text(
                        x[idx, 0] + dx,
                        x[idx, 1] + dy,
                        str(text),
                        fontsize=7,
                        color="#111827",
                        alpha=0.85,
                    )
    ax.set_title(title)
    ax.set_xlabel("PC1")
    ax.set_ylabel("PC2")
    ax.legend(fontsize=8, frameon=False, loc="best")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _plot_prediction_distribution(y_true, y_pred, labels, out_path, title):
    pred_counts = pd.Series(y_pred).value_counts()
    values = [int(pred_counts.get(label, 0)) for label in labels]
    fig, ax = plt.subplots(figsize=(6, 4), dpi=130)
    bars = ax.bar(labels, values, color="#3b82f6")
    ax.set_title(title)
    ax.set_ylabel("Predicted Count")
    ax.set_xlabel("Class")
    ax.set_xticklabels(labels, rotation=45, ha="right")
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, val, str(val), ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def train_models(
    data_path: str | Path,
    model_keys: list[str],
    output_dir: str | Path,
    model_dir: str | Path,
    dataset_key: str = "dataset",
    test_size: float = 0.2,
    random_state: int = 42,
) -> dict:
    if not os.path.exists(str(data_path)):
        return {"error": f"dataset not found: {data_path}"}

    try:
        df = read_ml_csv(data_path)
    except Exception as err:
        return {"error": f"read csv failed: {err}"}

    required = {"PC1", "PC2", "Target", "Label"}
    missing = required - set(df.columns)
    if missing:
        return {"error": f"missing columns: {', '.join(sorted(missing))}"}

    x = df[["PC1", "PC2"]].to_numpy()
    y = df["Target"].astype(str).to_numpy()
    labels = sorted(pd.unique(y).tolist())

    sample_labels = None
    if "Label" in df.columns:
        sample_labels = df["Label"].astype(str).to_numpy()

    if len(labels) < 2:
        return {"error": "need at least 2 classes for classification"}

    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=test_size, random_state=random_state, stratify=y
    )

    os.makedirs(str(output_dir), exist_ok=True)
    os.makedirs(str(model_dir), exist_ok=True)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    metrics_table = []

    for key in model_keys:
        if key not in MODEL_REGISTRY:
            continue

        model_name, estimator = _build_estimator(key)
        estimator.fit(x_train, y_train)
        y_pred = estimator.predict(x_test)

        acc = float(accuracy_score(y_test, y_pred))
        precision = float(precision_score(y_test, y_pred, average="weighted", zero_division=0))
        recall = float(recall_score(y_test, y_pred, average="weighted", zero_division=0))
        f1 = float(f1_score(y_test, y_pred, average="weighted", zero_division=0))
        cm = confusion_matrix(y_test, y_pred, labels=labels)
        report = classification_report(
            y_test, y_pred, labels=labels, output_dict=True, zero_division=0
        )

        roc_auc = None
        scores = _get_score_values(estimator, x_test)
        if scores is not None:
            try:
                if len(labels) > 2:
                    roc_auc = float(
                        roc_auc_score(y_test, scores, multi_class="ovr", average="weighted")
                    )
                else:
                    if scores.ndim == 2:
                        scores = scores[:, 1]
                    roc_auc = float(roc_auc_score(y_test, scores))
            except Exception:
                roc_auc = None

        safe_key = f"{dataset_key}_{key}_{run_id}"
        cm_path = os.path.join(str(output_dir), f"{safe_key}_cm.png")
        db_path = os.path.join(str(output_dir), f"{safe_key}_boundary.png")
        dist_path = os.path.join(str(output_dir), f"{safe_key}_pred_dist.png")

        _plot_confusion_matrix(cm, labels, cm_path, f"{model_name} Confusion Matrix")
        _plot_decision_boundary(
            estimator, x, y, labels, db_path, f"{model_name} Decision Boundary", sample_labels=sample_labels
        )
        _plot_prediction_distribution(y_test, y_pred, labels, dist_path, f"{model_name} Prediction Distribution")

        model_path = os.path.join(str(model_dir), f"{safe_key}.pkl")
        joblib.dump(estimator, model_path)

        metrics_table.append(
            {
                "key": key,
                "name": model_name,
                "accuracy": acc,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "roc_auc": roc_auc,
                "model_path": model_path.replace("\\", "/"),
            }
        )

        results.append(
            {
                "key": key,
                "name": model_name,
                "accuracy": acc,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "roc_auc": roc_auc,
                "model_path": model_path.replace("\\", "/"),
                "per_class": report,
            }
        )

    return {
        "run_id": run_id,
        "labels": labels,
        "metrics_table": metrics_table,
        "models": results,
    }
