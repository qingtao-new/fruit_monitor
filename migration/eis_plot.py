import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import rcParams

rcParams['font.sans-serif'] = ['SimHei']
rcParams['axes.unicode_minus'] = False
def plot_nyquist_bode(df, sample_name, save_dir):
    os.makedirs(save_dir, exist_ok=True)

    freq = df["Frequency"].values
    real = df["Real"].values
    imag = df["Imag"].values

    mag = np.sqrt(real**2 + imag**2)
    phase = np.arctan2(imag, real) * 180 / np.pi

    paths = {}

    # Nyquist
    plt.figure()
    plt.plot(real, -imag, "o-")
    plt.xlabel("Z' (Ω)")
    plt.ylabel("-Z'' (Ω)")
    plt.title(f"Nyquist Plot - {sample_name}")
    plt.grid(True)
    nyq_path = f"{save_dir}/{sample_name}_nyquist.png"
    plt.savefig(nyq_path, dpi=300)
    plt.close()
    paths["nyquist"] = nyq_path

    # Bode |Z|
    plt.figure()
    plt.semilogx(freq, mag, "o-")
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("|Z| (Ω)")
    plt.title(f"Bode Magnitude - {sample_name}")
    plt.grid(True, which="both")
    mag_path = f"{save_dir}/{sample_name}_bode_mag.png"
    plt.savefig(mag_path, dpi=300)
    plt.close()
    paths["bode_mag"] = mag_path

    # Bode Phase
    plt.figure()
    plt.semilogx(freq, phase, "o-")
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Phase (°)")
    plt.title(f"Bode Phase - {sample_name}")
    plt.grid(True, which="both")
    phase_path = f"{save_dir}/{sample_name}_bode_phase.png"
    plt.savefig(phase_path, dpi=300)
    plt.close()
    paths["bode_phase"] = phase_path

    return paths
