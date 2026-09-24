from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt


def _read_csv(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Missing file: {p}")
    return pd.read_csv(p)


def _write_excel(summary_df: pd.DataFrame, results_df: pd.DataFrame, out_path: str | Path) -> tuple[bool, str | None, Exception | None]:
    last_err: Exception | None = None
    for engine in ("openpyxl", "xlsxwriter"):
        try:
            with pd.ExcelWriter(out_path, engine=engine) as writer:
                summary_df.to_excel(writer, index=False, sheet_name="summary")
                results_df.to_excel(writer, index=False, sheet_name="results")
            return True, engine, None
        except Exception as err:
            last_err = err
    return False, None, last_err


def _plot_timeline(results_df: pd.DataFrame, out_path: str | Path, cols: int = 2) -> None:
    endpoints = sorted(results_df["endpoint"].unique().tolist())
    if not endpoints:
        raise ValueError("No endpoint data found")

    rows = int(math.ceil(len(endpoints) / float(cols)))
    fig, axes = plt.subplots(rows, cols, figsize=(6 * cols, 3.2 * rows), sharey=False)
    if rows == 1 and cols == 1:
        axes = [[axes]]
    elif rows == 1:
        axes = [axes]
    elif cols == 1:
        axes = [[ax] for ax in axes]

    for idx, ep in enumerate(endpoints):
        r = idx // cols
        c = idx % cols
        ax = axes[r][c]
        sub = results_df[results_df["endpoint"] == ep].reset_index(drop=True)
        x = list(range(1, len(sub) + 1))
        y = sub["ms"].astype(float).tolist()
        mean = sum(y) / len(y) if y else 0.0
        var = sum((v - mean) ** 2 for v in y) / len(y) if y else 0.0
        std = var ** 0.5

        ax.plot(x, y, marker="o", markersize=2.5, linewidth=0.8, color="#2563eb")
        ax.fill_between(x, [mean - std] * len(x), [mean + std] * len(x), color="#93c5fd", alpha=0.35)
        ax.axhline(mean, color="#1d4ed8", linewidth=1.0, linestyle="--")
        ax.set_title(ep)
        ax.set_xlabel("Request count")
        ax.set_ylabel("Response time (ms)")
        ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.6)

    for j in range(len(endpoints), rows * cols):
        r = j // cols
        c = j % cols
        axes[r][c].axis("off")

    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)


def generate_report(results_csv: str | Path, summary_csv: str | Path, xlsx_path: str | Path, plot_path: str | Path) -> dict:
    results_df = _read_csv(results_csv)
    summary_df = _read_csv(summary_csv)
    ok, engine, err = _write_excel(summary_df, results_df, xlsx_path)
    _plot_timeline(results_df, plot_path)
    return {
        "excel_ok": ok,
        "engine": engine,
        "excel_error": str(err) if err else None,
        "plot_path": str(Path(plot_path)),
    }
