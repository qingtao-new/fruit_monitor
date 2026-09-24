from __future__ import annotations

import json
import unittest

from protocol import (
    FRAME_TERMINATOR,
    FrameDecodeError,
    LineFramer,
    encode_frame,
    parse_sweep_points,
    parse_topic,
    try_parse,
)


class EncodeFrameTest(unittest.TestCase):
    def test_frame_ends_with_terminator(self):
        frame = encode_frame({"a": 1})
        self.assertTrue(frame.endswith(FRAME_TERMINATOR))
        self.assertEqual(frame.count(FRAME_TERMINATOR), 1)

    def test_frame_payload_is_valid_json(self):
        frame = encode_frame({"gateway_id": "GW_001", "node_id": "N1", "v": 1.5})
        body = frame.rstrip(FRAME_TERMINATOR)
        self.assertEqual(json.loads(body), {"gateway_id": "GW_001", "node_id": "N1", "v": 1.5})

    def test_roundtrip_through_framer(self):
        framer = LineFramer()
        frames = [encode_frame({"i": i}) for i in range(3)]
        out = framer.feed("".join(frames).encode("utf-8"))
        self.assertEqual([json.loads(t) for t in out], [{"i": 0}, {"i": 1}, {"i": 2}])
        self.assertEqual(framer.stats()["bad_json"], 0)


class LineFramerTest(unittest.TestCase):
    def test_sticky_frames_split(self):
        framer = LineFramer()
        out = framer.feed(b'{"a":1}\n{"b":2}\n')
        self.assertEqual(len(out), 2)

    def test_half_frames_buffered_until_terminator(self):
        framer = LineFramer()
        self.assertEqual(framer.feed(b'{"a":'), [])
        self.assertEqual(framer.feed(b'1}\n'), ['{"a":1}'])
        self.assertEqual(framer.stats()["buffered"], 0)

    def test_crlf_tolerated(self):
        framer = LineFramer()
        out = framer.feed('{"a":1}\r\n')
        self.assertEqual(json.loads(out[0]), {"a": 1})

    def test_blank_lines_ignored(self):
        framer = LineFramer()
        self.assertEqual(framer.feed(b'\n\n{"a":1}\n\n'), ['{"a":1}'])

    def test_oversized_frame_dropped(self):
        framer = LineFramer(max_frame_len=10)
        framer.feed(b"{" + b"x" * 60 + b"}\n")
        self.assertEqual(framer.stats()["dropped"], 1)
        self.assertEqual(framer.stats()["frames"], 0)

    def test_bad_json_in_strict_mode_raises(self):
        framer = LineFramer(strict_json=True)
        with self.assertRaises(FrameDecodeError):
            framer.feed_json(b"{not json}\n")
        self.assertEqual(framer.stats()["bad_json"], 1)

    def test_bad_json_skipped_in_lenient_mode(self):
        framer = LineFramer(strict_json=False)
        out = framer.feed_json(b"{nope}\n{" + '"a":1}\n'.encode() + b"\n")
        self.assertEqual(out, [{"a": 1}])
        self.assertEqual(framer.stats()["bad_json"], 1)

    def test_reset_clears_buffer(self):
        framer = LineFramer()
        framer.feed(b'{"a":')
        framer.reset()
        self.assertEqual(framer.stats()["buffered"], 0)


class FeedCompleteTest(unittest.TestCase):
    """feed_complete 面向 MQTT/WebSocket 单条 payload：末尾没有换行符。"""

    def test_payload_without_terminator_is_parsed(self):
        framer = LineFramer()
        out = framer.feed_complete(b'{"a":1}')
        self.assertEqual(out, [{"a": 1}])
        self.assertEqual(framer.stats()["frames"], 1)

    def test_buffer_stays_empty(self):
        """MQTT 消息不会被留在缓冲区里等一个永远不来的换行符。"""
        framer = LineFramer()
        framer.feed_complete(b'{"a":1}')
        framer.feed_complete(b'{"b":2}')
        self.assertEqual(framer.stats()["buffered"], 0)
        self.assertEqual(framer.stats()["frames"], 2)

    def test_bytes_payload(self):
        self.assertEqual(
            LineFramer().feed_complete(json.dumps({"x": 2}).encode("utf-8")),
            [{"x": 2}],
        )

    def test_multi_frame_payload_split(self):
        out = LineFramer().feed_complete('{"a":1}\n{"b":2}')
        self.assertEqual(out, [{"a": 1}, {"b": 2}])

    def test_blank_payload_yields_nothing(self):
        self.assertEqual(LineFramer().feed_complete(b"  \n "), [])

    def test_bad_json_in_strict_mode_raises(self):
        with self.assertRaises(FrameDecodeError):
            LineFramer().feed_complete(b"{not json}")

    def test_bad_json_skipped_in_lenient_mode(self):
        out = LineFramer(strict_json=False).feed_complete(b"{nope}\n{\"a\":1}")
        self.assertEqual(out, [{"a": 1}])


class ParseSweepTest(unittest.TestCase):
    def test_parallel_array_shape(self):
        payload = {
            "gateway_id": "GW_001",
            "node_id": "LORA_NODE_01",
            "round": 7,
            "timestamp": 1700000000,
            "point_start": 2,
            "freq": [1000, 2000],
            "re": [1250, 1100],
            "im": [-320, -300],
            "temperature": [24.1, 24.2],
        }
        points = parse_sweep_points(payload)
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].point_index, 2)
        self.assertEqual(points[1].frequency_hz, 2000)
        self.assertAlmostEqual(points[0].magnitude, 1290.31, places=2)
        self.assertEqual(points[0].round_id, 7)
        self.assertEqual(points[0].temperature, 24.1)
        self.assertEqual(points[0].report_id, "GW_001/LORA_NODE_01/R7")

    def test_object_list_shape(self):
        payload = {
            "gateway_id": "GW_001",
            "node": "LORA_NODE_01",
            "round_id": 3,
            "ts": 1700000000,
            "points": [{"i": 0, "freq": 500, "re": 900, "im": -100, "ph": 6.4}],
        }
        points = parse_sweep_points(payload)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].node_id, "LORA_NODE_01")
        self.assertEqual(points[0].frequency_hz, 500)
        self.assertEqual(points[0].ph, 6.4)

    def test_missing_magnitude_is_derived(self):
        payload = {
            "gateway_id": "GW", "node_id": "N", "round": 1, "timestamp": 1,
            "freq": [100], "re": [3], "im": [4],
        }
        points = parse_sweep_points(payload)
        self.assertAlmostEqual(points[0].magnitude, 5.0)

    def test_empty_payload_yields_nothing(self):
        self.assertEqual(parse_sweep_points({"gateway_id": "GW"}), [])

    def test_topic_parse(self):
        self.assertEqual(
            parse_topic("fruit/GW_001/LORA_NODE_01/impedance_raw"),
            ("GW_001", "LORA_NODE_01", "impedance_raw"),
        )
        with self.assertRaises(ValueError):
            parse_topic("fruit/GW_001")

    def test_try_parse_sweep(self):
        payload = {
            "gateway_id": "GW", "node_id": "N", "round": 1, "timestamp": 1,
            "freq": [100], "re": [3], "im": [4],
        }
        parsed = try_parse(json.dumps(payload), "sweep", {})
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed), 1)


if __name__ == "__main__":
    unittest.main()
