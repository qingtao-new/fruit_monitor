from __future__ import annotations

import json
import unittest

from protocol import (
    ImpedanceRawPacket,
    ImpedanceRawPoint,
    parse_impedance_raw,
    try_parse,
    validate_impedance_raw_packet,
)


class ProtocolTest(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "gateway_id": "GW_001",
            "node": "LORA_NODE_01",
            "timestamp": 1700000000,
            "scan_id": 125,
            "packet_index": 0,
            "packet_total": 13,
            "point_start": 0,
            "point_count": 2,
            "frequency_start_hz": 1000,
            "frequency_increment_hz": 100,
            "points": [
                {"i": 0, "re": 1250, "im": -320},
                {"i": 1, "re": 1238, "im": -331},
            ],
        }
        payload.update(overrides)
        return payload

    def test_parse_impedance_raw(self):
        packet = parse_impedance_raw(self._payload())
        self.assertIsInstance(packet, ImpedanceRawPacket)
        self.assertEqual(packet.scan_id, 125)
        self.assertEqual(packet.packet_total, 13)
        self.assertEqual(packet.point_start, 0)
        self.assertEqual(packet.point_count, 2)
        self.assertEqual(len(packet.points), 2)
        self.assertIsInstance(packet.points[0], ImpedanceRawPoint)
        self.assertEqual(packet.points[0].re, 1250.0)
        self.assertEqual(packet.points[0].im, -320.0)

    def test_try_parse_accepts_impedance_raw(self):
        raw = json.dumps(self._payload())
        parsed = try_parse(raw, "impedance_raw", {})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.packet_index, 0)

    def test_validate_flags_missing_points(self):
        packet = parse_impedance_raw(self._payload(point_count=3))
        ok, flags = validate_impedance_raw_packet(packet)
        self.assertFalse(ok)
        self.assertIn("points_len_mismatch", flags)

    def test_validate_flags_invalid_index(self):
        packet = parse_impedance_raw(self._payload(packet_index=13))
        ok, flags = validate_impedance_raw_packet(packet)
        self.assertFalse(ok)
        self.assertIn("packet_index_out_of_range", flags)

    def test_explicit_frequency_list(self):
        payload = self._payload(
            packet_total=1,
            point_count=2,
            frequency_start_hz=0,
            frequency_increment_hz=0,
            frequencies=[1000, 10000, 100000],
        )
        packet = parse_impedance_raw(payload)
        ok, flags = validate_impedance_raw_packet(packet)
        self.assertTrue(ok, flags)
        self.assertEqual(packet.frequency_at(0), 1000)
        self.assertEqual(packet.frequency_at(1), 10000)

    def test_per_point_frequency_in_points(self):
        payload = self._payload(
            packet_total=1,
            point_count=2,
            frequency_start_hz=0,
            frequency_increment_hz=0,
            points=[{"i": 0, "re": 1250, "im": -320, "f": 1500},
                    {"i": 1, "re": 1238, "im": -331, "f": 4700}],
        )
        packet = parse_impedance_raw(payload)
        ok, flags = validate_impedance_raw_packet(packet)
        self.assertTrue(ok, flags)
        self.assertEqual([packet.frequency_at(i) for i in range(2)], [1500, 4700])

    def test_truncated_frequency_list_flagged(self):
        payload = self._payload(
            packet_total=1, point_count=2, frequency_start_hz=0,
            frequency_increment_hz=0, frequencies=[1000],
        )
        ok, flags = validate_impedance_raw_packet(parse_impedance_raw(payload))
        self.assertFalse(ok)
        self.assertIn("frequencies_truncated", flags)


if __name__ == "__main__":
    unittest.main()
