from __future__ import annotations

import unittest

from maturity import (
    MAGNITUDE_HIGH,
    MAGNITUDE_LOW,
    MAX_CONFIDENCE,
    MIN_CONFIDENCE,
    MATURITY_LEVELS,
    SPREAD_RATIO_REF,
    estimate_maturity,
    level_label,
    maturity_level_index,
    progress_uncertainty,
    spectrum_prediction,
)
from protocol import ImpedanceSpectrum, SpectrumPoint
from spectrum import window_stats


def make_spectrum(points):
    return ImpedanceSpectrum(
        gateway_id="GW_001", node_id="LORA_NODE_01", scan_id=1,
        timestamp=1700000000,
        points=[SpectrumPoint(frequency_hz=f, z_real=re, z_imag=im) for f, re, im in points],
    )


class CalibrationTest(unittest.TestCase):
    def test_high_end_is_unripe(self):
        result = estimate_maturity(MAGNITUDE_HIGH)
        self.assertAlmostEqual(result.maturity, 0.0, places=3)
        self.assertEqual(result.maturity_level, "unripe")

    def test_low_end_is_overripe(self):
        result = estimate_maturity(MAGNITUDE_LOW)
        self.assertAlmostEqual(result.maturity, 1.0, places=3)
        self.assertEqual(result.maturity_level, "overripe")

    def test_progress_decreases_with_magnitude(self):
        values = list(range(int(MAGNITUDE_LOW), int(MAGNITUDE_HIGH) + 1, 100))
        progress = [estimate_maturity(v).maturity for v in values]
        self.assertEqual(progress, sorted(progress, reverse=True))
        self.assertTrue(all(0.0 <= p <= 1.0 for p in progress))

    def test_progress_is_monotonic_across_full_scale(self):
        step = (MAGNITUDE_HIGH - MAGNITUDE_LOW) / 4
        progress = [
            estimate_maturity(MAGNITUDE_HIGH - i * step).maturity for i in range(5)
        ]
        self.assertEqual(progress[0] < progress[1] < progress[2] < progress[3] < progress[4], True)

    def test_invalid_calibration_rejected(self):
        with self.assertRaises(ValueError):
            estimate_maturity(1000.0, magnitude_high=1000.0, magnitude_low=1000.0)
        with self.assertRaises(ValueError):
            estimate_maturity(1000.0, magnitude_high=1000.0, magnitude_low=2000.0)


class LevelTest(unittest.TestCase):
    def test_level_boundaries(self):
        self.assertEqual(maturity_level_index(0.0), 0)
        self.assertEqual(maturity_level_index(0.249), 0)
        self.assertEqual(maturity_level_index(0.25), 1)
        self.assertEqual(maturity_level_index(0.5), 2)
        self.assertEqual(maturity_level_index(0.75), 3)
        self.assertEqual(maturity_level_index(1.0), len(MATURITY_LEVELS) - 1)

    def test_out_of_range_clamped(self):
        self.assertEqual(maturity_level_index(-0.5), 0)
        self.assertEqual(maturity_level_index(1.5), len(MATURITY_LEVELS) - 1)

    def test_level_labels_cover_every_key(self):
        for key, label in MATURITY_LEVELS:
            self.assertEqual(level_label(key), label)
        self.assertEqual(level_label("mystery"), "mystery")
        self.assertEqual(level_label(None), "未知")
        self.assertEqual(level_label(""), "未知")


class ConfidenceTest(unittest.TestCase):
    def test_band_center_is_higher_confidence_than_boundary(self):
        band_width = (MAGNITUDE_HIGH - MAGNITUDE_LOW) / len(MATURITY_LEVELS)
        at_boundary = estimate_maturity(MAGNITUDE_HIGH - band_width).confidence
        at_center = estimate_maturity(MAGNITUDE_HIGH - band_width * 1.5).confidence
        self.assertGreater(at_center, at_boundary)

    def test_confidence_within_bounds(self):
        for magnitude in (1100, 1300, 1500, 1700, 1900):
            confidence = estimate_maturity(float(magnitude)).confidence
            self.assertGreaterEqual(confidence, MIN_CONFIDENCE * 0.8)
            self.assertLessEqual(confidence, MAX_CONFIDENCE)

    def test_excess_spread_penalises_confidence(self):
        quiet = estimate_maturity(1500.0, spread_ratio=SPREAD_RATIO_REF).confidence
        noisy = estimate_maturity(1500.0, spread_ratio=SPREAD_RATIO_REF + 0.3).confidence
        self.assertLess(noisy, quiet)

    def test_baseline_spread_is_not_penalised(self):
        below = estimate_maturity(1500.0, spread_ratio=0.0).confidence
        at_ref = estimate_maturity(1500.0, spread_ratio=SPREAD_RATIO_REF).confidence
        self.assertAlmostEqual(below, at_ref, places=6)


class HarvestTest(unittest.TestCase):
    def test_unripe_looks_further_out_than_ripe(self):
        unripe = estimate_maturity(MAGNITUDE_HIGH).harvest_date
        ripe = estimate_maturity(MAGNITUDE_LOW).harvest_date
        self.assertGreater(unripe, ripe)

    def test_timestamp_passthrough(self):
        result = estimate_maturity(1500.0, timestamp=1700000000, node_id="LORA_NODE_01")
        self.assertEqual(result.timestamp, 1700000000)
        self.assertEqual(result.node_id, "LORA_NODE_01")


class UncertaintyTest(unittest.TestCase):
    """progress_uncertainty 是统计意义上的不确定度，与 confidence 不同。"""

    def test_zero_spread_means_zero_sigma(self):
        _, sigma = progress_uncertainty(1500.0, 0.0)
        self.assertEqual(sigma, 0.0)

    def test_sigma_scales_linearly_with_spread(self):
        span = MAGNITUDE_HIGH - MAGNITUDE_LOW
        _, s1 = progress_uncertainty(1500.0, 100.0)
        _, s2 = progress_uncertainty(1500.0, 200.0)
        self.assertAlmostEqual(s1, 100.0 / span, places=9)
        self.assertAlmostEqual(s2, 200.0 / span, places=9)

    def test_same_progress_as_estimate_maturity(self):
        prog, _ = progress_uncertainty(1500.0, 50.0)
        self.assertAlmostEqual(prog, estimate_maturity(1500.0).maturity, places=6)

    def test_interval_not_clipped(self):
        # 裁剪 sigma 会把截断误当成精度，必须返回未裁剪的传播值
        _, sigma = progress_uncertainty(MAGNITUDE_HIGH, 1e6)
        self.assertGreater(sigma, 1.0)

    def test_invalid_calibration_rejected(self):
        with self.assertRaises(ValueError):
            progress_uncertainty(1000.0, 10.0,
                                 magnitude_high=1000.0, magnitude_low=1000.0)

    def test_confidence_is_not_a_confidence_interval(self):
        """heuristic confidence 与统计 sigma 是两回事，互不推导。"""
        _, sigma = progress_uncertainty(1500.0, 0.0)
        conf = estimate_maturity(1500.0, spread_ratio=0.0).confidence
        self.assertEqual(sigma, 0.0)          # 测量无离散
        self.assertGreater(conf, 0.5)         # 但启发式照样给高分
        self.assertNotAlmostEqual(sigma, 1.0 - conf, places=3)


class SpectrumPredictionTest(unittest.TestCase):
    def test_returns_prediction_and_stats(self):
        spectrum = make_spectrum([(1000, 1500, -300), (2000, 1300, -250), (5000, 700, -100)])
        prediction, stats = spectrum_prediction(spectrum, timestamp=1700000000)
        self.assertEqual(prediction.node_id, "LORA_NODE_01")
        self.assertEqual(prediction.timestamp, 1700000000)
        self.assertEqual(stats["count"], 3.0)
        self.assertAlmostEqual(stats["mean"], window_stats(spectrum, 1000.0, 30000.0)["mean"])

    def test_empty_band_is_safe(self):
        spectrum = make_spectrum([(100, 1500, -300), (200000, 700, -100)])
        prediction, stats = spectrum_prediction(spectrum)
        self.assertEqual(stats["count"], 0.0)
        self.assertEqual(prediction.maturity, 1.0)

    def test_band_override_changes_stats(self):
        spectrum = make_spectrum([
            (500, 5000, -800), (2000, 1500, -300),
            (5000, 1100, -220), (10000, 800, -120), (100000, 400, -20),
        ])
        default_stats = spectrum_prediction(spectrum)[1]
        narrow_stats = spectrum_prediction(spectrum, band_lo_hz=1500, band_hi_hz=3000)[1]
        self.assertEqual(default_stats["count"], 3.0)
        self.assertEqual(narrow_stats["count"], 1.0)
        self.assertAlmostEqual(
            narrow_stats["mean"], window_stats(spectrum, 1500.0, 3000.0)["mean"]
        )

    def test_threshold_override_shifts_maturity(self):
        spectrum = make_spectrum([(2000, 1200, -300), (5000, 900, -200)])
        tight = spectrum_prediction(
            spectrum, magnitude_high=1900.0, magnitude_low=1050.0
        )[0]
        loose = spectrum_prediction(
            spectrum, magnitude_high=1900.0, magnitude_low=900.0
        )[0]
        self.assertGreater(tight.maturity, loose.maturity)

    def test_spread_reference_override(self):
        spectrum = make_spectrum([(2000, 1200, -300), (5000, 900, -200)])
        strict = spectrum_prediction(spectrum, spread_ratio_ref=0.0)[0]
        lenient = spectrum_prediction(spectrum, spread_ratio_ref=1.0)[0]
        self.assertLessEqual(strict.confidence, lenient.confidence)


if __name__ == "__main__":
    unittest.main()
