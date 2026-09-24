from __future__ import annotations

import math
import unittest

from protocol import (
    ImpedanceRawPacket,
    ImpedanceRawPoint,
    ImpedanceSpectrum,
    SpectrumPoint,
)
from spectrum import (
    SpectrumAssembler,
    bode_series,
    characteristic_frequency,
    nyquist_series,
    sweep_points_to_spectrum,
    to_sweep_points,
    window_stats,
)


def make_spectrum(points):
    return ImpedanceSpectrum(
        gateway_id="GW_001", node_id="LORA_NODE_01", scan_id=1, timestamp=1700000000,
        points=[
            SpectrumPoint(frequency_hz=f, z_real=re, z_imag=im,
                          magnitude=math.hypot(re, im),
                          phase_deg=math.degrees(math.atan2(-im, re)))
            for f, re, im in points
        ],
    )


class SpectrumAssemblerTest(unittest.TestCase):
    def _pkt(self, index, total, points, start=0, freq0=1000.0, step=100.0, point_count=None):
        return ImpedanceRawPacket(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000000,
            scan_id=1, packet_index=index, packet_total=total,
            point_start=start, point_count=len(points) if point_count is None else point_count,
            frequency_start_hz=freq0, frequency_increment_hz=step,
            points=[ImpedanceRawPoint(i, re, im) for i, (re, im) in enumerate(points)],
        )

    def test_out_of_order_delivery_completes(self):
        asm = SpectrumAssembler()
        pkt2 = self._pkt(2, 3, [(1000.0, -350.0), (950.0, -360.0)], start=4)
        pkt1 = self._pkt(1, 3, [(1100.0, -330.0), (1050.0, -340.0)], start=2)
        pkt0 = self._pkt(0, 3, [(1200.0, -300.0), (1180.0, -310.0)])

        self.assertIsNone(asm.feed(pkt2))
        self.assertIsNone(asm.feed(pkt1))
        spectrum = asm.feed(pkt0)
        self.assertIsNotNone(spectrum)
        self.assertEqual(len(spectrum.points), 6)
        self.assertEqual(spectrum.points[0].frequency_hz, 1000.0)
        self.assertEqual(spectrum.points[-1].frequency_hz, 1500.0)
        self.assertEqual(spectrum.meta["points_missing"], 0)
        self.assertEqual(asm.completed, 1)
        self.assertEqual(asm.pending(), 0)

    def test_explicit_frequency_list_is_honoured(self):
        # 对数扫频：increment 为 0，只能靠显式频率数组定位每个点
        asm = SpectrumAssembler()
        freqs = [1000.0, 2000.0, 5000.0, 10000.0]
        pkt = ImpedanceRawPacket(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000000,
            scan_id=2, packet_index=0, packet_total=1,
            point_start=0, point_count=4,
            frequency_start_hz=0.0, frequency_increment_hz=0.0, frequencies=freqs,
            points=[
                ImpedanceRawPoint(0, 1200.0, -300.0),
                ImpedanceRawPoint(1, 1100.0, -310.0),
                ImpedanceRawPoint(2, 900.0, -330.0),
                ImpedanceRawPoint(3, 700.0, -340.0),
            ],
        )
        spectrum = asm.feed(pkt)
        self.assertEqual([p.frequency_hz for p in spectrum.points], freqs)

    def test_offset_packet_reads_frequency_at_absolute_index(self):
        asm = SpectrumAssembler()
        freqs = [1000.0, 2000.0, 5000.0, 10000.0, 20000.0, 50000.0]
        pkt = ImpedanceRawPacket(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000000,
            scan_id=3, packet_index=1, packet_total=2,
            point_start=3, point_count=3,
            frequency_start_hz=0.0, frequency_increment_hz=0.0, frequencies=freqs,
            points=[
                ImpedanceRawPoint(0, 900.0, -330.0),
                ImpedanceRawPoint(1, 700.0, -340.0),
                ImpedanceRawPoint(2, 500.0, -350.0),
            ],
        )
        self.assertIsNone(asm.feed(pkt))
        head = ImpedanceRawPacket(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000000,
            scan_id=3, packet_index=0, packet_total=2,
            point_start=0, point_count=3,
            frequency_start_hz=0.0, frequency_increment_hz=0.0, frequencies=freqs,
            points=[
                ImpedanceRawPoint(0, 1200.0, -300.0),
                ImpedanceRawPoint(1, 1100.0, -310.0),
                ImpedanceRawPoint(2, 1000.0, -320.0),
            ],
        )
        spectrum = asm.feed(head)
        self.assertEqual([p.frequency_hz for p in spectrum.points], freqs)

    def test_incomplete_scan_returns_none(self):
        asm = SpectrumAssembler()
        pkt = self._pkt(0, 2, [(1200.0, -300.0), (1180.0, -310.0)])
        self.assertIsNone(asm.feed(pkt))
        self.assertEqual(asm.completed, 0)
        self.assertEqual(asm.pending(), 1)

    def test_packet_timeout_expires(self):
        asm = SpectrumAssembler(scan_timeout_s=10.0)
        asm.feed(self._pkt(0, 2, [(1200.0, -300.0), (1180.0, -310.0)]), now_ts=1_700_000_000)
        self.assertEqual(asm.pending(), 1)
        asm._expire(1_700_000_000 + 11)
        self.assertEqual(asm.pending(), 0)

    def test_scan_evicted_when_cache_full(self):
        asm = SpectrumAssembler(max_scans=1)
        asm.feed(self._pkt(0, 2, [(1200.0, -300.0)]), now_ts=1_700_000_000)
        other = ImpedanceRawPacket(
            gateway_id="GW_001", node_id="LORA_NODE_02", timestamp=1_700_000_000,
            scan_id=2, packet_index=0, packet_total=2, point_start=0, point_count=1,
            frequency_start_hz=1000.0, frequency_increment_hz=100.0,
            points=[ImpedanceRawPoint(0, 1200.0, -300.0)],
        )
        asm.feed(other, now_ts=1_700_000_000)
        self.assertEqual(asm.pending(), 1)

    def test_malformed_packet_rejected(self):
        asm = SpectrumAssembler()
        bad = self._pkt(0, 1, [(1200.0, -300.0)], point_count=3)
        self.assertIsNone(asm.feed(bad))
        self.assertEqual(asm.bad_packets, 1)
        self.assertEqual(asm.completed, 0)


class SeriesTest(unittest.TestCase):
    def test_nyquist_series_orientation(self):
        series = nyquist_series(make_spectrum([(1000, 1200, -300), (2000, 1100, -250)]))
        self.assertEqual(series["x"], [1200, 1100])
        self.assertEqual(series["y"], [300, 250])
        self.assertEqual(series["frequency"], [1000, 2000])

    def test_bode_series_orientation(self):
        series = bode_series(make_spectrum([(1000, 1200, -300)]))
        self.assertEqual(series["frequency"], [1000])
        self.assertAlmostEqual(series["magnitude"][0], math.hypot(1200, 300), places=3)
        self.assertAlmostEqual(series["phase"][0], math.degrees(math.atan2(300, 1200)), places=3)

    def test_min_frequency_filter(self):
        spectrum = make_spectrum([(500, 2000, -50), (2000, 1100, -250)])
        self.assertEqual(len(nyquist_series(spectrum, min_frequency_hz=1000)["x"]), 1)
        self.assertEqual(len(bode_series(spectrum, min_frequency_hz=1000)["frequency"]), 1)


class CharacteristicFrequencyTest(unittest.TestCase):
    def test_returns_finite_frequency(self):
        spectrum = make_spectrum([
            (500, 1300, -180),
            (1000, 1000, -500),
            (2000, 700, -260),
            (5000, 300, -40),
            (10000, 100, -5),
        ])
        self.assertEqual(characteristic_frequency(spectrum), 1000)

    def test_respects_frequency_window(self):
        spectrum = make_spectrum([
            (100, 1500, -600),
            (1000, 1000, -200),
        ])
        self.assertEqual(characteristic_frequency(spectrum, lo_hz=1000), 1000)
        self.assertIsNone(characteristic_frequency(spectrum, lo_hz=2000))

    def test_purely_resistive_spectrum_has_no_arc(self):
        spectrum = make_spectrum([(1000, 1000, 0), (2000, 900, 0)])
        self.assertIsNone(characteristic_frequency(spectrum))

    def test_empty_spectrum_has_no_arc(self):
        self.assertIsNone(characteristic_frequency(make_spectrum([])))


class ConversionTest(unittest.TestCase):
    def _assembled(self):
        asm = SpectrumAssembler()
        pkt = ImpedanceRawPacket(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000000,
            scan_id=1, packet_index=0, packet_total=1, point_start=0, point_count=2,
            frequency_start_hz=1000.0, frequency_increment_hz=1000.0,
            points=[ImpedanceRawPoint(0, 1200.0, -300.0), ImpedanceRawPoint(1, 1100.0, -250.0)],
        )
        return asm.feed(pkt)

    def test_to_sweep_points_carries_round_and_context(self):
        spectrum = self._assembled()
        points = to_sweep_points(spectrum, round_id=7, context={"temperature": 24.0, "co2": 430})
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].round_id, 7)
        self.assertEqual(points[0].point_index, 0)
        self.assertEqual(points[0].temperature, 24.0)
        self.assertEqual(points[1].co2, 430)
        self.assertEqual(points[0].frequency_hz, 1000)
        self.assertEqual(points[1].frequency_hz, 2000)

    def test_window_stats_band_averages(self):
        spectrum = make_spectrum([
            (500, 2000, -50),
            (2000, 1100, -250),
            (6000, 500, -120),
            (20000, 150, -20),
        ])
        lo = window_stats(spectrum, 500.0, 3000.0)
        self.assertEqual(lo["count"], 2.0)
        expected = (math.hypot(2000, 50) + math.hypot(1100, 250)) / 2
        self.assertAlmostEqual(lo["mean"], expected, places=2)
        self.assertAlmostEqual(lo["std"], math.hypot(2000, 50) - expected, places=2)

        empty = window_stats(spectrum, 100000.0, 200000.0)
        self.assertEqual(empty["count"], 0.0)
        self.assertEqual(empty["mean"], 0.0)

    def test_roundtrip_sweep_points(self):
        points = to_sweep_points(self._assembled(), round_id=2)
        back = sweep_points_to_spectrum(points)
        self.assertEqual(len(back.points), 2)
        self.assertEqual(back.points[0].frequency_hz, 1000)
        self.assertAlmostEqual(back.points[1].z_imag, -250.0)
        self.assertAlmostEqual(back.points[0].phase_deg, math.degrees(math.atan2(300, 1200)), places=3)


if __name__ == "__main__":
    unittest.main()
