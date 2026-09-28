from __future__ import annotations

import math
import unittest

import numpy as np

from spectral_fit import (
    MIN_FIT_R2,
    SPECTRAL_FEATURE_NAMES,
    _cole_cole_flat,
    fit_cole_cole,
    scalar_band_stats,
    spectral_features,
)


def synth_spectrum(r0, rinf, fc, alpha, freqs):
    """按已知参数正向生成一条谱，供拟合测试回收参数。"""
    z = _cole_cole_flat(np.asarray(freqs, dtype=float), r0, rinf, fc, alpha)
    n = len(freqs)
    re, im = z[:n], z[n:]
    return re.copy(), im.copy()


FREQS = [100.0, 500.0, 1000.0, 5000.0, 10000.0, 30000.0, 100000.0]


class FitRecoveryTest(unittest.TestCase):
    """正向生成->拟合回收，是拟合器最硬的正确性证据。"""

    def test_recovers_known_parameters_noiseless(self):
        truth = (2600.0, 220.0, 8000.0, 0.6)
        re, im = synth_spectrum(*truth, FREQS)
        got = fit_cole_cole(FREQS, re, im)
        self.assertIsNotNone(got)
        self.assertGreater(got.r_squared, 0.9999)
        self.assertAlmostEqual(got.r0, truth[0], delta=truth[0] * 0.01)
        self.assertAlmostEqual(got.rinf, truth[1], delta=truth[1] * 0.05)
        self.assertAlmostEqual(got.fc_hz, truth[2], delta=truth[2] * 0.05)
        self.assertAlmostEqual(got.alpha, truth[3], delta=0.05)

    def test_recovers_over_multiple_parameter_sets(self):
        for truth in ((2000.0, 300.0, 3000.0, 0.8),
                      (5000.0, 100.0, 20000.0, 0.4),
                      (1500.0, 400.0, 5000.0, 0.9)):
            re, im = synth_spectrum(*truth, FREQS)
            got = fit_cole_cole(FREQS, re, im)
            self.assertIsNotNone(got, f"拟合失败 {truth}")
            self.assertGreater(got.r_squared, MIN_FIT_R2, f"{truth}")
            self.assertGreater(got.r0, got.rinf, f"R0 必须大于 Rinf {truth}")

    def test_parameters_with_noise_still_fit_well(self):
        rng = np.random.default_rng(11)
        re, im = synth_spectrum(2600.0, 220.0, 8000.0, 0.6, FREQS)
        re = re * (1 + rng.normal(0, 0.005, len(re)))
        im = im * (1 + rng.normal(0, 0.008, len(im)))
        got = fit_cole_cole(FREQS, re, im)
        self.assertIsNotNone(got)
        self.assertGreater(got.r_squared, 0.99)

    def test_confidence_intervals_are_finite_when_fit_succeeds(self):
        re, im = synth_spectrum(2600.0, 220.0, 8000.0, 0.6, FREQS)
        got = fit_cole_cole(FREQS, re, im)
        for value in (got.r0_ci95, got.rinf_ci95, got.fc_ci95, got.alpha_ci95):
            self.assertTrue(math.isfinite(value), "CI 应为有限值")
            self.assertGreaterEqual(value, 0.0)
        self.assertGreater(got.dof, 0)

    def test_tau_and_derived_indices(self):
        re, im = synth_spectrum(2600.0, 220.0, 8000.0, 0.6, FREQS)
        got = fit_cole_cole(FREQS, re, im)
        self.assertAlmostEqual(got.tau_s, 1.0 / (2 * math.pi * got.fc_hz), places=9)
        self.assertAlmostEqual(got.membrane_index, got.r0 - got.rinf, places=6)
        self.assertAlmostEqual(got.polarization_ratio, got.r0 / got.rinf, places=6)


class FitRejectionTest(unittest.TestCase):
    def test_too_few_points_returns_none(self):
        self.assertIsNone(fit_cole_cole([100.0, 500.0], [1000.0, 900.0],
                                        [-10.0, -30.0]))

    def test_mismatched_lengths_raise(self):
        with self.assertRaises(ValueError):
            fit_cole_cole([100, 500, 1000], [1, 2], [1, 2, 3])

    def test_non_finite_values_are_dropped_not_fatal(self):
        re, im = synth_spectrum(2600.0, 220.0, 8000.0, 0.6, FREQS)
        re = list(re)
        re[2] = float("nan")
        got = fit_cole_cole(FREQS, re, im)
        # 少一点仍可拟合，或拒绝——但绝不能抛异常
        if got is not None:
            self.assertEqual(got.n_points, len(FREQS) - 1)

    def test_all_zero_target_rejected(self):
        self.assertIsNone(fit_cole_cole(FREQS, [0.0] * 7, [0.0] * 7))

    def test_flat_non_arc_spectrum_rejected_or_low_r2(self):
        # 完全没有弛豫结构的直线，拟合出来应当被 R2 门槛挡掉
        re = [1000.0 + i for i in range(7)]
        im = [-1.0] * 7
        got = fit_cole_cole(FREQS, re, im)
        if got is not None:
            self.assertGreaterEqual(got.r_squared, MIN_FIT_R2)


class BandStatsTest(unittest.TestCase):
    def test_band_limits_filter_points(self):
        f = [100.0, 1000.0, 10000.0, 100000.0]
        re = [400.0, 300.0, 200.0, 100.0]
        im = [-30.0, -60.0, -40.0, -10.0]
        full = scalar_band_stats(f, re, im)
        band = scalar_band_stats(f, re, im, 500.0, 50000.0)
        self.assertEqual(full["count"], 4.0)
        self.assertEqual(band["count"], 2.0)

    def test_empty_input_returns_nans(self):
        out = scalar_band_stats([], [], [])
        self.assertTrue(all(math.isnan(v) for v in out.values()))

    def test_band_with_no_points_returns_nans(self):
        out = scalar_band_stats([100.0], [500.0], [-10.0], 1000.0, 5000.0)
        self.assertTrue(math.isnan(out["mag_mean"]))


class FeatureVectorTest(unittest.TestCase):
    def test_feature_names_match_vector_length(self):
        re, im = synth_spectrum(2600.0, 220.0, 8000.0, 0.6, FREQS)
        vec = spectral_features(FREQS, re, im)
        self.assertEqual(list(vec.keys()), list(SPECTRAL_FEATURE_NAMES))
        self.assertEqual(len(vec), len(SPECTRAL_FEATURE_NAMES))

    def test_successful_fit_fills_all_columns(self):
        re, im = synth_spectrum(2600.0, 220.0, 8000.0, 0.6, FREQS)
        vec = spectral_features(FREQS, re, im)
        for k in SPECTRAL_FEATURE_NAMES:
            self.assertTrue(math.isfinite(vec[k]), f"{k} 应为有限值")

    def test_failed_fit_yields_nan_for_cole_cole_only(self):
        # 拟合失败时 Cole-Cole 项为 nan，标量项仍可用
        vec = spectral_features([100.0, 500.0], [1000.0, 900.0], [-10.0, -30.0])
        for k in ("r0", "rinf", "fc_hz", "alpha", "tau_s",
                  "membrane_index", "polarization_ratio"):
            self.assertTrue(math.isnan(vec[k]), f"{k} 拟合失败应为 nan")
        self.assertTrue(math.isfinite(vec["mag_mean"]))


if __name__ == "__main__":
    unittest.main()
