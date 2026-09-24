from __future__ import annotations

import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def load_eis_data(csv_path: str | Path) -> pd.DataFrame:
    path = Path(csv_path)
    encodings = ("utf-8-sig", "gbk", "utf-8")
    last_err: Exception | None = None
    df: pd.DataFrame | None = None
    for enc in encodings:
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except Exception as err:
            last_err = err
    if df is None:
        raise last_err  # type: ignore[misc]

    rename_map = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key in {"sample_id", "sampleid", "sample"}:
            rename_map[col] = "sample_id"
        elif key in {"frequency_hz", "frequency", "freq_hz", "freq"}:
            rename_map[col] = "frequency_hz"
        elif key in {"z_real", "real", "zreal"}:
            rename_map[col] = "Z_real"
        elif key in {"z_imag", "imag", "zimag"}:
            rename_map[col] = "Z_imag"
        elif key == "label":
            rename_map[col] = "label"
    if rename_map:
        df = df.rename(columns=rename_map)

    required = {"sample_id", "frequency_hz", "Z_real", "Z_imag"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    if "label" not in df.columns:
        df["label"] = df["sample_id"]

    df["Z_mag"] = np.sqrt(df["Z_real"].astype(float) ** 2 + df["Z_imag"].astype(float) ** 2)
    df["Phase"] = np.arctan2(df["Z_imag"].astype(float), df["Z_real"].astype(float)) * 180.0 / np.pi
    return df


def ensure_dir(path: str | Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def analyze_single_sample(df: pd.DataFrame, sample_id: str, save_dir: str | Path) -> dict:
    ensure_dir(save_dir)
    sdf = df[df["sample_id"] == sample_id]
    if sdf.empty:
        return {"error": f"sample not found: {sample_id}"}

    stats = {
        "Z_mean": float(sdf["Z_mag"].mean()),
        "Z_std": float(sdf["Z_mag"].std()),
        "Phase_mean": float(sdf["Phase"].mean()),
        "Phase_std": float(sdf["Phase"].std()),
        "label": str(sdf["label"].iloc[0]),
    }

    plt.figure()
    plt.boxplot(sdf["Z_mag"].astype(float))
    plt.ylabel("|Z| (Ohm)")
    plt.title(f"Sample {sample_id} Impedance Distribution")
    plt.savefig(os.path.join(str(save_dir), f"{sample_id}_box.png"))
    plt.close()
    return stats


def analyze_label_group(df: pd.DataFrame, label: str, save_dir: str | Path) -> dict:
    ensure_dir(save_dir)
    gdf = df[df["label"] == label]
    if gdf.empty:
        return {"error": f"label not found: {label}"}

    plt.figure()
    plt.boxplot(gdf["Z_mag"].astype(float))
    plt.ylabel("|Z| (Ohm)")
    plt.title(f"Grade {label} Impedance Distribution")
    plt.savefig(os.path.join(str(save_dir), f"label_{label}_box.png"))
    plt.close()

    freq_group = gdf.groupby("frequency_hz")["Z_mag"]
    mean = freq_group.mean()
    std = freq_group.std()
    plt.figure()
    plt.fill_between(mean.index, mean - std, mean + std, alpha=0.3)
    plt.plot(mean.index, mean)
    plt.xscale("log")
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("|Z| (Ohm)")
    plt.title(f"Grade {label} Mean ± STD")
    plt.savefig(os.path.join(str(save_dir), f"label_{label}_mean_std.png"))
    plt.close()

    def band(f: float) -> str:
        if f < 10:
            return "Low"
        if f < 1000:
            return "Mid"
        return "High"

    band_mean = gdf.assign(Band=gdf["frequency_hz"].apply(band)).groupby("Band")["Z_mag"].mean()
    plt.figure()
    band_mean.plot(kind="bar")
    plt.ylabel("|Z| (Ohm)")
    plt.title(f"Grade {label} Band Impedance")
    plt.savefig(os.path.join(str(save_dir), f"label_{label}_band.png"))
    plt.close()

    return {
        "mean_by_frequency": mean.to_dict(),
        "std_by_frequency": std.to_dict(),
        "band_mean": band_mean.to_dict(),
    }
