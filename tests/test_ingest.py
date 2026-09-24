from __future__ import annotations

import json
import math
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from db import Database
from mqtt_client import (
    DeviceClock,
    MIN_REPORT_POINTS,
    MQTTWorker,
    RoundBuffer,
    SWEEP_FREQS,
    SWEEP_FREQ_HI,
    SWEEP_FREQ_LO,
    SWEEP_SEGMENT_SIZE,
    SWEEP_SEGMENTS,
    ensure_wallclock_ts,
    impedance_scan_id,
    band_impedance_from_points,
)
from protocol import (
    VALID_MSG_TYPES,
    PredictionData,
    SensorData,
    SweepPointData,
    parse_heartbeat,
    sweep_expected_points,
)


def _points_payload(
    round_id: int,
    start_index: int,
    count: int,
    *,
    gateway_id: str = "GW_001",
    node_id: str = "LORA_NODE_01",
    seg: int | None = None,
    seg_total: int | None = None,
) -> dict:
    """构造一条网关格式的 sweep 报文。"""
    return {
        "type": "sweep",
        "gateway_id": gateway_id,
        "node_id": node_id,
        "round": round_id,
        "report_id": f"{gateway_id}/{node_id}/R{round_id}",
        "timestamp": 1700000000 + round_id,
        "point_start": start_index,
        "seg": seg,
        "seg_total": seg_total,
        "points": [
            {
                "i": start_index + i,
                "freq": 1000.0 * (start_index + i + 1),
                "re": 1200.0,
                "im": -300.0,
                "imp": 1236.93,
            }
            for i in range(count)
        ],
        "context": {"temperature": 24.0, "co2": 430.0, "ph": 6.4},
    }


class WorkerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "ingest.db")
        self.addCleanup(self._finish)
        self.received: list[dict] = []
        self.worker = MQTTWorker({"sweep": {"min_points": 50}}, self.db)
        self.worker.data_received.connect(self.received.append)
        self.addCleanup(self.worker.stop)

    def _finish(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _sweep(self, payload: dict) -> dict:
        self.worker._dispatch("sweep", "GW_001", "LORA_NODE_01", payload)
        sweeps = [p for p in self.received if p["type"] == "sweep"]
        self.assertTrue(sweeps)
        return sweeps[-1]

    def _sensor(self, node_id: str = "LORA_NODE_01") -> None:
        self.worker._dispatch("sensor", "GW_001", node_id, {
            "type": "sensor",
            "gateway_id": "GW_001",
            "node_id": node_id,
            "timestamp": 1700000000,
            "temperature": 24.0,
            "co2": 430.0,
            "ph": 6.4,
        })


class DeviceClockTest(unittest.TestCase):
    """开机秒数折墙钟：既要换成墙钟，又不能把点挤进同一秒。"""

    def test_wallclock_timestamp_passes_through(self):
        clock = DeviceClock()
        self.assertEqual(clock.to_wallclock(1_700_000_000), 1_700_000_000)

    def test_small_timestamp_becomes_wallclock(self):
        wall = DeviceClock().to_wallclock(3600)
        self.assertGreater(wall, 1e8)
        self.assertLessEqual(wall, int(time.time()))

    def test_relative_spacing_is_preserved(self):
        clock = DeviceClock()
        first = clock.to_wallclock(3600)
        second = clock.to_wallclock(3601)
        later = clock.to_wallclock(7200)
        self.assertEqual(second - first, 1)
        self.assertEqual(later - second, 3599)

    def test_restart_rewind_does_not_go_backwards(self):
        """网关重启、开机秒数归零：折出来的时间仍要单调向前。"""
        clock = DeviceClock()
        first = clock.to_wallclock(3600)
        clock.to_wallclock(7200)
        after_restart = clock.to_wallclock(0)
        self.assertGreater(after_restart, first)

    def test_wallclock_passthrough_does_not_reanchor(self):
        clock = DeviceClock()
        clock.to_wallclock(1_700_000_000)
        first = clock.to_wallclock(100)
        self.assertLessEqual(first, int(time.time()))

    def test_scan_id_distinguishes_restarted_rounds(self):
        clock = DeviceClock()
        ts1 = ensure_wallclock_ts({"timestamp": 3600}, clock)["timestamp"]
        ts2 = ensure_wallclock_ts({"timestamp": 7200}, clock)["timestamp"]
        self.assertNotEqual(impedance_scan_id(9, ts1), impedance_scan_id(9, ts2))

    def test_bad_timestamp_is_clamped_to_wallclock(self):
        self.assertGreater(DeviceClock().to_wallclock("not-a-number"), 1e8)


class OnMessageTest(WorkerHarness):
    """喂真实 MQTT payload（末尾没有换行符），验证消息不会被卡住或丢掉。"""

    def _msg(self, payload: dict | bytes,
             topic: str = "fruit/GW_001/LORA_NODE_01/sensor"):
        body = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
        return SimpleNamespace(topic=topic, payload=body)

    def _sensor_payload(self, node_id: str = "LORA_NODE_01") -> dict:
        return {
            "type": "sensor",
            "gateway_id": "GW_001",
            "node_id": node_id,
            "timestamp": 1700000000,
            "temperature": 24.0,
            "co2": 430.0,
            "ph": 6.4,
        }

    def test_payload_without_newline_is_ingested(self):
        self.worker._on_message(None, None, self._msg(self._sensor_payload()))
        self.assertEqual(self.worker._msg_count, 1)
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 1)
        self.assertEqual(self.worker._framer.stats()["buffered"], 0)

    def test_consecutive_payloads_are_not_buffered(self):
        for node in ("LORA_NODE_01", "LORA_NODE_02", "LORA_NODE_03"):
            self.worker._on_message(None, None, self._msg(self._sensor_payload(node)))
        self.assertEqual(self.worker._msg_count, 3)
        self.assertEqual(self.worker._framer.stats()["buffered"], 0)

    def test_heartbeat_on_status_topic_is_dispatched(self):
        hb = {"type": "heartbeat", "gateway_id": "GW_001", "node_id": "N1",
              "timestamp": 1700000000, "ip": "192.168.1.102", "rssi": -72.0}
        self.worker._on_message(None, None, self._msg(hb, "fruit/GW_001/N1/status"))
        self.assertEqual(self.worker._msg_count, 1)
        self.assertEqual(self.db.count_status_rows("GW_001"), 1)

    def test_malformed_payload_does_not_kill_worker(self):
        self.worker._on_message(None, None, self._msg(b"{not json"))
        self.assertEqual(self.worker._msg_count, 0)
        # 之后正常报文还能继续收
        self.worker._on_message(None, None, self._msg(self._sensor_payload()))
        self.assertEqual(self.worker._msg_count, 1)

    def test_bad_topic_is_ignored(self):
        self.worker._on_message(None, None, self._msg({"type": "sensor"}, "fruit/only"))
        self.assertEqual(self.worker._msg_count, 0)


class ConnectGuardTest(unittest.TestCase):
    """连不上时不能装作连上了：client_id 必须非空，连接必须真的建立。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "guard.db")
        self.addCleanup(self._finish)

    def _finish(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _worker(self, mqtt_cfg: dict) -> MQTTWorker:
        worker = MQTTWorker({"mqtt": mqtt_cfg, "sweep": {"min_points": 50}}, self.db)
        self.addCleanup(worker.stop)
        return worker

    def test_gen_client_id_is_never_empty(self):
        cid = MQTTWorker._gen_client_id()
        self.assertTrue(cid)
        self.assertIn(str(os.getpid()), cid)
        self.assertNotEqual(MQTTWorker._gen_client_id(), cid)

    def test_run_reports_error_when_broker_never_confirms(self):
        """对端 accept 了 TCP 却从不回 CONNACK：不能声称已连接。"""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        self.addCleanup(srv.close)

        def _park() -> None:
            conn, _ = srv.accept()
            self.addCleanup(conn.close)

        threading.Thread(target=_park, daemon=True).start()

        worker = self._worker({
            "host": "127.0.0.1", "port": port,
            "client_id": "guard_pc", "connect_timeout_s": 0.5,
        })
        errors: list[str] = []
        states: list[bool] = []
        worker.error_occurred.connect(errors.append)
        worker.connected.connect(states.append)

        worker.run()

        self.assertEqual(len(errors), 1)
        self.assertIn("MQTT 连接未建立", errors[0])
        self.assertNotIn(True, states)

    def test_run_reports_error_when_port_refused(self):
        worker = self._worker({
            "host": "127.0.0.1", "port": 1,
            "client_id": "guard_pc", "connect_timeout_s": 0.5,
        })
        errors: list[str] = []
        worker.error_occurred.connect(errors.append)
        worker.run()
        self.assertEqual(len(errors), 1)
        # 端口拒绝走的是 connect() 抛异常这条路径，不是超时那条
        self.assertNotIn("连接未建立", errors[0])


class ReportGridTest(unittest.TestCase):
    def test_grid_has_at_least_min_report_points(self):
        self.assertGreaterEqual(len(SWEEP_FREQS), MIN_REPORT_POINTS)

    def test_grid_is_distinct_sorted(self):
        self.assertEqual(len(set(SWEEP_FREQS)), len(SWEEP_FREQS))
        self.assertEqual(SWEEP_FREQS, sorted(SWEEP_FREQS))

    def test_grid_covers_expected_band(self):
        self.assertEqual(min(SWEEP_FREQS), SWEEP_FREQ_LO)
        self.assertEqual(max(SWEEP_FREQS), SWEEP_FREQ_HI)

    def test_segment_size_meets_protocol_floor(self):
        self.assertGreaterEqual(SWEEP_SEGMENT_SIZE, MIN_REPORT_POINTS)

    def test_segments_cover_whole_grid(self):
        self.assertGreaterEqual(SWEEP_SEGMENTS * SWEEP_SEGMENT_SIZE, len(SWEEP_FREQS))


class SweepIngestTest(WorkerHarness):
    def test_min_points_floor_is_enforced(self):
        self.assertEqual(self.worker._min_sweep_points, MIN_REPORT_POINTS)

    def test_floor_cannot_be_lowered_by_config(self):
        worker = MQTTWorker({"sweep": {"min_points": 3}}, self.db)
        self.addCleanup(worker.stop)
        self.assertEqual(worker._min_sweep_points, MIN_REPORT_POINTS)

    def test_min_points_comes_from_config(self):
        worker = MQTTWorker({"sweep": {"min_points": 80}}, self.db)
        self.addCleanup(worker.stop)
        self.assertEqual(worker._min_sweep_points, 80)

    def test_single_report_meeting_floor_is_segment_complete(self):
        out = self._sweep(_points_payload(2, 0, SWEEP_SEGMENT_SIZE, seg=0, seg_total=2))
        self.assertEqual(out["points_count"], SWEEP_SEGMENT_SIZE)
        self.assertTrue(out["segment_complete"])
        self.assertFalse(out["complete"])  # 只有一段，整轮还差另一半

    def test_report_payload_carries_completeness_fields(self):
        out = self._sweep(_points_payload(1, 0, 20, seg=0, seg_total=3))
        for field in ("report_id", "round_id", "points_count", "min_points",
                      "buffered_points", "seg", "seg_total",
                      "segment_complete", "complete"):
            self.assertIn(field, out)
        self.assertEqual(out["report_id"], "GW_001/LORA_NODE_01/R1")
        self.assertEqual(out["points_count"], 20)
        self.assertEqual(out["min_points"], MIN_REPORT_POINTS)
        self.assertEqual(out["buffered_points"], 20)
        self.assertEqual(out["seg"], 0)
        self.assertEqual(out["seg_total"], 3)
        self.assertFalse(out["segment_complete"])
        self.assertFalse(out["complete"])

    def test_single_small_report_is_incomplete(self):
        out = self._sweep(_points_payload(1, 0, 20))
        self.assertFalse(out["complete"])
        self.assertFalse(out["segment_complete"])

    def test_round_completes_when_buffer_reaches_threshold(self):
        # 单段 30 点低于协议下限，报文本身不算完整上报；
        # 但两段攒够 50 点，整轮判定为完整，可以出谱。
        out1 = self._sweep(_points_payload(1, 0, 30, seg=0, seg_total=2))
        self.assertFalse(out1["segment_complete"])
        self.assertFalse(out1["complete"])
        out2 = self._sweep(_points_payload(1, 30, 30, seg=1, seg_total=2))
        self.assertEqual(out2["points_count"], 30)
        self.assertFalse(out2["segment_complete"])
        self.assertGreaterEqual(out2["buffered_points"], MIN_REPORT_POINTS)
        self.assertTrue(out2["complete"])

    def test_overlapping_reports_do_not_inflate_buffer(self):
        self._sweep(_points_payload(1, 0, 30))
        out = self._sweep(_points_payload(1, 20, 30))  # 20..49 与上一段重叠
        self.assertEqual(out["buffered_points"], 50)
        self.assertTrue(out["complete"])

    def test_dedup_keeps_one_row_per_point_in_db(self):
        self._sweep(_points_payload(1, 0, 30))
        self._sweep(_points_payload(1, 0, 30))  # QoS1 重投
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 30)

    def test_repeated_round_reuses_same_points(self):
        self._sweep(_points_payload(1, 0, 60))
        self._sweep(_points_payload(1, 0, 60))
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 1)
        self.assertEqual(len(rows), 60)
        self.assertEqual([r["point_index"] for r in rows], list(range(60)))

    def test_incomplete_rounds_flushed_after_idle(self):
        self._sweep(_points_payload(7, 0, 10))
        emitted = len(self.received)
        self.worker._round_idle_s = 0.0
        self.worker._flush_rounds()
        self.assertGreater(len(self.received), emitted)
        spectrum = self.received[-1]
        self.assertEqual(spectrum["type"], "spectrum")
        self.assertEqual(spectrum["points_count"], 10)
        self.assertFalse(spectrum["complete"])

    def test_idle_flush_does_not_duplicate_round(self):
        self._sweep(_points_payload(8, 0, 60))
        self.worker._round_idle_s = 0.0
        self.worker._flush_rounds()
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 60)
        self.assertEqual(self.db.count_predictions("LORA_NODE_01"), 1)

    def test_still_buffered_round_is_not_flushed(self):
        self._sweep(_points_payload(9, 0, 10))
        emitted = len(self.received)
        self.worker._round_idle_s = 9999.0
        self.worker._flush_rounds()
        self.assertEqual(len(self.received), emitted)

    def test_prediction_is_upserted_per_node_and_timestamp(self):
        self._sensor()
        self._sweep(_points_payload(1, 0, 60))
        self.worker._round_idle_s = 0.0
        self.worker._flush_rounds()
        self.assertEqual(self.db.count_predictions("LORA_NODE_01"), 1)
        # 同一轮再重复上报一次（QoS1 重投 / 整谱补齐），预测不该重复。
        self._sweep(_points_payload(1, 0, 60))
        self.worker._round_idle_s = 0.0
        self.worker._flush_rounds()
        self.assertEqual(self.db.count_predictions("LORA_NODE_01"), 1)

    def test_prediction_deleted_when_empty_delete_list(self):
        self.db.insert_prediction(PredictionData(
            node_id="LORA_NODE_01", timestamp=1, maturity=0.5,
            maturity_level="ripening", harvest_date="2026-10-01", confidence=0.7))
        self.assertEqual(self.db.count_predictions("LORA_NODE_01"), 1)
        self.assertEqual(self.worker.delete_predictions_for([]), 0)
        self.assertEqual(self.db.count_predictions("LORA_NODE_01"), 1)
        self.assertEqual(self.worker.delete_predictions_for(["LORA_NODE_01"]), 1)
        self.assertEqual(self.db.count_predictions("LORA_NODE_01"), 0)


class GatewayFirmwareShapeTest(WorkerHarness):
    """模拟 ESP32-S3 网关固件的真实上报形状：只报 seg 不报 seg_total。"""

    def _gateway_seg(self, round_id: int, seg: int, point_start: int, count: int) -> dict:
        return {
            "type": "sweep",
            "gateway_id": "GW_001",
            "node_id": "LORA_NODE_01",
            "round": round_id,
            "ts": 12345,
            "seg": seg,
            "point_start": point_start,
            "freq": [1000.0 * (point_start + i + 1) for i in range(count)],
            "re": [1900.0] * count,
            "im": [-400.0] * count,
            "imp": [1942.0] * count,
            "soil_moisture": [52] * count,
            "temperature": [25.1] * count,
            "nh3": [10] * count,
            "h2s": [5] * count,
            "co2": [450] * count,
            "ph": [6.5] * count,
            "humidity": [65] * count,
        }

    def test_first_segment_is_not_a_complete_round(self):
        out = self._sweep(self._gateway_seg(1, 0, 0, 50))
        self.assertEqual(out["points_count"], 50)
        self.assertTrue(out["segment_complete"])
        self.assertFalse(out["complete"])
        self.assertEqual(out["buffered_points"], 50)
        self.assertEqual(out["total_points"], 100)

    def test_second_segment_completes_round_of_100(self):
        self._sweep(self._gateway_seg(1, 0, 0, 50))
        out = self._sweep(self._gateway_seg(1, 1, 50, 50))
        self.assertEqual(out["buffered_points"], 100)
        self.assertTrue(out["complete"])
        # 攒满即出图，不用等静默窗口。
        spectra = [p for p in self.received if p["type"] == "spectrum"]
        self.assertEqual(len(spectra), 1)
        self.assertEqual(spectra[0]["points_count"], 100)
        self.assertTrue(spectra[0]["complete"])
        self.assertEqual(spectra[0]["total_points"], 100)

    def test_segment_points_do_not_duplicate_in_db(self):
        self._sweep(self._gateway_seg(2, 0, 0, 50))
        self._sweep(self._gateway_seg(2, 1, 50, 50))
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01", round_id=2), 100)

    def test_missing_segment_flushed_after_idle(self):
        self._sweep(self._gateway_seg(3, 0, 0, 50))
        self.worker._round_idle_s = 0.0
        self.worker._flush_rounds()
        spectra = [p for p in self.received if p["type"] == "spectrum"]
        self.assertEqual(spectra[-1]["points_count"], 50)
        self.assertFalse(spectra[-1]["complete"])

    def test_heartbeat_is_recorded_as_online(self):
        self.worker._dispatch("heartbeat", "GW_001", "LORA_NODE_01", {
            "type": "heartbeat",
            "gateway_id": "GW_001",
            "node_id": "LORA_NODE_01",
            "ts": 12345,
            "ip": "192.168.1.5",
            "rssi": -65,
        })
        rows = self.db.query_status_history("GW_001", limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "online")

    def test_unknown_status_is_not_recorded(self):
        self.worker._dispatch("status", "GW_001", "LORA_NODE_01", {
            "type": "status", "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
            "timestamp": 1700000000,
        })
        self.assertEqual(self.db.count_status_rows("GW_001"), 0)

    def test_gateway_info_signal_carries_ip_and_rssi(self):
        infos = []
        self.worker.gateway_info.connect(lambda gw, node, ip, rssi: infos.append((gw, node, ip, rssi)))
        self.worker._dispatch("heartbeat", "GW_001", "LORA_NODE_01", {
            "type": "heartbeat", "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
            "ts": 12345, "ip": "192.168.1.5", "rssi": -65,
        })
        self.assertEqual(infos, [("GW_001", "LORA_NODE_01", "192.168.1.5", -65.0)])


class ExpectedPointsTest(unittest.TestCase):
    def test_declared_total_wins(self):
        self.assertEqual(sweep_expected_points({"total_points": 60}), 60)

    def test_seg_total_multiplied_by_seg_points(self):
        self.assertEqual(sweep_expected_points({"seg_total": 3, "seg_points": 40}), 120)

    def test_point_start_plus_length_is_lower_bound(self):
        payload = {
            "seg": 1, "point_start": 50,
            "freq": list(range(50)), "re": list(range(50)),
        }
        self.assertEqual(sweep_expected_points(payload, 100, 50), 100)

    def test_config_default_is_fallback(self):
        self.assertEqual(sweep_expected_points({"seg": 0}, 100, 50), 100)

    def test_firmware_shape_with_no_total(self):
        payload = {
            "seg": 1, "point_start": 50,
            "freq": list(range(50)), "re": list(range(50)),
            "im": list(range(50)), "imp": list(range(50)),
        }
        self.assertEqual(sweep_expected_points(payload), 100)

    def test_point_count_is_per_report_not_round_total(self):
        # 网关固件报 point_count=SEG_POINTS(50)，是本次报文的点数，
        # 当成整轮总点数会让第一轮在第 1 段就判完成、出半条弧的谱。
        payload = {
            "seg": 0, "seg_total": 2, "point_start": 0, "point_count": 50,
            "freq": list(range(50)),
        }
        self.assertEqual(sweep_expected_points(payload), 100)

    def test_explicit_total_points_wins_over_seg_math(self):
        payload = {
            "total_points": 200, "seg": 0, "seg_total": 2, "seg_points": 50,
        }
        self.assertEqual(sweep_expected_points(payload), 200)


class HeartbeatParseTest(unittest.TestCase):
    def test_heartbeat_maps_to_online(self):
        data = parse_heartbeat({
            "type": "heartbeat", "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
            "ts": 12345, "ip": "192.168.1.5", "rssi": -65,
        })
        self.assertEqual(data.status, "online")
        self.assertEqual(data.gateway_id, "GW_001")
        self.assertEqual(data.node_id, "LORA_NODE_01")
        self.assertEqual(data.ip, "192.168.1.5")
        self.assertEqual(data.rssi, -65.0)
        self.assertEqual(data.timestamp, 12345)

    def test_heartbeat_without_rssi(self):
        data = parse_heartbeat({"type": "heartbeat", "gateway_id": "GW", "ts": 1})
        self.assertIsNone(data.rssi)
        self.assertIsNone(data.ip)
        self.assertEqual(data.status, "online")

    def test_heartbeat_is_a_valid_msg_type(self):
        self.assertIn("heartbeat", VALID_MSG_TYPES)


class RoundBufferTest(unittest.TestCase):
    def _point(self, index: int, round_id: int = 1) -> SweepPointData:
        return SweepPointData(
            gateway_id="GW_001", node_id="LORA_NODE_01", round_id=round_id,
            timestamp=1700000000, point_index=index,
            frequency_hz=1000.0, z_real=100.0, z_imag=-10.0,
            magnitude=100.5, soil_moisture=None, temperature=None,
            nh3=None, h2s=None, co2=None, ph=None, humidity=None,
        )

    def _pts(self, start: int, count: int, round_id: int = 1) -> list:
        return [self._point(start + i, round_id) for i in range(count)]

    def test_second_segment_only_completes_with_total(self):
        buf = RoundBuffer(50, total_points=100, segment_points=50)
        key = ("GW_001", "LORA_NODE_01", 1)
        self.assertFalse(buf.feed(self._pts(0, 50), seg=0, expected_points=100))
        self.assertFalse(buf.complete(key))
        self.assertTrue(buf.feed(self._pts(50, 50), seg=1, expected_points=100))
        self.assertTrue(buf.complete(key))

    def test_declared_seg_total_still_governs(self):
        buf = RoundBuffer(50, total_points=100, segment_points=50)
        key = ("GW_001", "LORA_NODE_01", 1)
        self.assertFalse(buf.feed(self._pts(0, 50), seg=0, seg_total=2))
        self.assertTrue(buf.feed(self._pts(50, 50), seg=1, seg_total=2))

    def test_single_unsegmented_report_is_complete(self):
        buf = RoundBuffer(50)
        key = ("GW_001", "LORA_NODE_01", 1)
        self.assertTrue(buf.feed(self._pts(0, 60)))

    def test_drain_clears_all_state(self):
        buf = RoundBuffer(50, total_points=100)
        key = ("GW_001", "LORA_NODE_01", 1)
        buf.feed(self._pts(0, 50), seg=0, expected_points=100)
        self.assertEqual(buf.expected_points(key), 100)
        self.assertEqual(len(buf.drain(key)), 50)
        self.assertEqual(buf.expected_points(key), 100)
        self.assertFalse(buf.has_key(key))


class SensorIngestTest(WorkerHarness):
    def test_sensor_updates_context(self):
        self._sensor()
        self.assertIn("temperature", self.worker._last_context)
        self.assertEqual(self.worker._last_context["temperature"], 24.0)

    def test_sensor_stored_when_recording(self):
        self._sensor()
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 1)

    def test_sensor_skipped_when_not_recording(self):
        self.worker._recording = False
        self._sensor()
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 0)

    def test_sensor_frame_with_impedance_is_stored_twice(self):
        """网关的 publishSensorPoint 一帧同时带环境量和阻抗三要素。"""
        self.received.clear()
        self.worker._dispatch("sensor", "GW_001", "LORA_NODE_01", {
            "type": "sensor",
            "gateway_id": "GW_001",
            "node_id": "LORA_NODE_01",
            "timestamp": 1700000000,
            "frequency_hz": 2138,
            "z_real": 1975,
            "z_imag": -423,
            "magnitude": 2030,
            "temperature": 25.1,
            "soil_moisture": 52,
            "nh3": 10,
            "h2s": 5,
            "co2": 450,
            "ph": 6.5,
            "humidity": 65,
        })
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 1)
        self.assertEqual(self.db.count_impedance_rows("GW_001", "LORA_NODE_01"), 1)
        row = self.db.query_impedance_history("GW_001", "LORA_NODE_01", limit=1)[0]
        self.assertEqual(row["frequency_hz"], 2138)
        self.assertEqual(row["z_real"], 1975)
        self.assertEqual(row["z_imag"], -423)
        types = [p["type"] for p in self.received]
        self.assertIn("sensor", types)
        self.assertIn("impedance", types)


class SensorImpedanceRowTest(WorkerHarness):
    def test_row_built_without_magnitude(self):
        row = self.worker._sensor_impedance_row({
            "gateway_id": "GW_001", "node_id": "LORA_NODE_01", "timestamp": 1,
            "frequency_hz": 1000, "z_real": 3, "z_imag": 4,
        })
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row.magnitude, 5.0)
        self.assertEqual(row.node_id, "LORA_NODE_01")

    def test_row_rejects_missing_impedance_fields(self):
        self.assertIsNone(self.worker._sensor_impedance_row({
            "gateway_id": "GW", "node_id": "N", "timestamp": 1, "z_real": 3,
        }))

    def test_row_outside_band_is_marked(self):
        row = self.worker._sensor_impedance_row({
            "gateway_id": "GW", "node_id": "N", "timestamp": 1,
            "frequency_hz": 100, "z_real": 3, "z_imag": 4,
        })
        self.assertFalse(row.in_valid_window)

    def test_row_derives_phase_from_the_complex_part(self):
        """网关不报相位时从复数部分推，不推 phase 列又是空的。"""
        row = self.worker._sensor_impedance_row({
            "gateway_id": "GW", "node_id": "N", "timestamp": 1,
            "frequency_hz": 1000, "z_real": 1200, "z_imag": -300,
        })
        self.assertAlmostEqual(row.phase, 14.0362435)

    def test_row_keeps_the_reported_phase(self):
        row = self.worker._sensor_impedance_row({
            "gateway_id": "GW", "node_id": "N", "timestamp": 1,
            "frequency_hz": 1000, "z_real": 3, "z_imag": 4, "phase": -7.15,
        })
        self.assertAlmostEqual(row.phase, -7.15)

    def test_row_skipped_when_not_recording(self):
        self.worker._recording = False
        self.received.clear()
        self.worker._dispatch("sensor", "GW_001", "LORA_NODE_01", {
            "type": "sensor", "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
            "timestamp": 1700000000, "frequency_hz": 1000,
            "z_real": 3, "z_imag": 4, "temperature": 24.0,
        })
        self.assertEqual(self.db.count_impedance_rows("GW_001", "LORA_NODE_01"), 0)
        self.assertEqual([p["type"] for p in self.received], ["sensor", "impedance"])


class SweepContextTest(WorkerHarness):
    def test_context_backfilled_from_sweep_points(self):
        """网关不发独立环境帧，环境量只能从扫频点里回填。"""
        count = 50
        payload = {
            "type": "sweep",
            "gateway_id": "GW_001",
            "node_id": "LORA_NODE_01",
            "round": 3,
            "timestamp": 1700000000,
            "seg": 0,
            "point_start": 0,
            "freq": [1000.0 * (i + 1) for i in range(count)],
            "re": [1200.0] * count,
            "im": [-300.0] * count,
            "imp": [1236.93] * count,
            "soil_moisture": [52.0] * count,
            "temperature": [25.1] * count,
            "co2": [450] * count,
            "humidity": [65] * count,
        }
        self._sweep(payload)
        ctx = self.worker._last_context
        self.assertEqual(ctx.get("soil_moisture"), 52.0)
        self.assertEqual(ctx.get("temperature"), 25.1)
        self.assertEqual(ctx.get("co2"), 450)
        self.assertEqual(ctx.get("humidity"), 65)

    def test_sweep_without_context_arrays_leaves_context_empty(self):
        payload = {
            "type": "sweep",
            "gateway_id": "GW_001",
            "node_id": "LORA_NODE_01",
            "round": 4,
            "timestamp": 1700000000,
            "point_start": 0,
            "freq": [1000.0],
            "re": [1200.0],
            "im": [-300.0],
        }
        self._sweep(payload)
        self.assertEqual(self.worker._last_context, {})


class BandImpedanceTest(unittest.TestCase):
    """一轮扫频点的分析频段均值，写进环境行的阻抗列。"""

    @staticmethod
    def _pt(index: int, freq: float, re: float, im: float,
            imp=None) -> SimpleNamespace:
        return SimpleNamespace(
            frequency_hz=freq, z_real=re, z_imag=im, magnitude=imp)

    def test_averages_only_the_in_band_points(self):
        points = [self._pt(i, 1000.0 + i * 1000.0, 100.0, 0.0)
                  for i in range(5)]  # 1k..5k，都在 1k~30k 内
        imp = band_impedance_from_points(points, 1000.0, 30000.0)
        self.assertIsNotNone(imp)
        self.assertAlmostEqual(imp.z_real, 100.0)
        self.assertAlmostEqual(imp.z_imag, 0.0)

    def test_out_of_band_points_do_not_count(self):
        # 3 个带内点取 100，2 个带外点取 9999；均值只能是 100
        points = [self._pt(i, 1000.0 + i * 100.0, 100.0, 0.0) for i in range(3)]
        points += [self._pt(90, 40000.0, 9999.0, 0.0),
                   self._pt(91, 50000.0, 9999.0, 0.0)]
        imp = band_impedance_from_points(points, 1000.0, 30000.0)
        self.assertAlmostEqual(imp.z_real, 100.0)
        self.assertAlmostEqual(imp.magnitude, 100.0)

    def test_no_in_band_point_returns_none(self):
        points = [self._pt(i, 40000.0 + i, 100.0, 0.0) for i in range(3)]
        self.assertIsNone(band_impedance_from_points(points, 1000.0, 30000.0))

    def test_derives_phase_from_the_complex_part(self):
        imp = band_impedance_from_points(
            [self._pt(0, 1000.0, 1200.0, -300.0)], 1000.0, 30000.0)
        self.assertAlmostEqual(imp.phase, 14.0362435)

    def test_derives_magnitude_when_absent(self):
        imp = band_impedance_from_points(
            [self._pt(0, 1000.0, 3.0, 4.0)], 1000.0, 30000.0)
        self.assertAlmostEqual(imp.magnitude, 5.0)

    def test_frequency_stays_null(self):
        """均值不对应任何单一频率，硬填一个只会误导。"""
        imp = band_impedance_from_points(
            [self._pt(0, 1000.0, 3.0, 4.0)], 1000.0, 30000.0)
        self.assertIsNone(imp.frequency_hz)

    def test_garbage_values_are_skipped(self):
        points = [SimpleNamespace(frequency_hz="oops", z_real=1.0, z_imag=2.0),
                  SimpleNamespace(frequency_hz=1000.0, z_real="x", z_imag="y"),
                  SimpleNamespace(frequency_hz=2000.0, z_real=3.0, z_imag=4.0)]
        imp = band_impedance_from_points(points, 1000.0, 30000.0)
        self.assertAlmostEqual(imp.z_real, 3.0)
        self.assertAlmostEqual(imp.magnitude, 5.0)
        self.assertAlmostEqual(imp.phase, -53.1301024)

    def test_empty_points_returns_none(self):
        self.assertIsNone(band_impedance_from_points([], 1000.0, 30000.0))


class SensorImpedanceIngestTest(WorkerHarness):
    """环境表和历史窗口：同一次采样的环境量和阻抗要一起落库。"""

    def test_impedance_scan_attaches_the_band_average_to_the_env_row(self):
        """真实硬件：一次扫描按频率拆成多条报文，共用一个 scan_id。"""
        self._sensor()
        for freq in (100.0, 500.0, 1000.0, 5000.0, 10000.0, 30000.0, 100000.0):
            self._imp_frame(freq)
        self.worker._flush_impedance_scans(force=True)
        rows = self.db.query_sensor_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["z_real"], 1200.0)
        self.assertAlmostEqual(row["z_imag"], -300.0)
        self.assertAlmostEqual(row["magnitude"], 1236.93, places=2)
        self.assertAlmostEqual(row["phase"], 14.0362435)

    def test_scan_epoch_does_not_spill_past_the_scan_time(self):
        """只往前填：测量之后的环境帧属于下一次测量，不能提前贴上。"""
        self._sensor(ts=1700000000, temperature=20.0)
        self._sensor(ts=1700000006, temperature=24.0)
        for freq in (1000.0, 5000.0, 10000.0):
            self._imp_frame(freq, ts=1700000001)
        self.worker._flush_impedance_scans(force=True)
        rows = {row["timestamp"]: row for row in
                self.db.query_sensor_history("GW_001", "LORA_NODE_01")}
        self.assertAlmostEqual(rows[1700000000]["z_real"], 1200.0)
        self.assertIsNone(rows[1700000006]["z_real"])

    def test_scan_fills_every_env_row_in_its_epoch(self):
        """一段测量覆盖整段时间：段内每一帧环境量都属于这次测量。"""
        for ts in (1700000000, 1700000002, 1700000004):
            self._sensor(ts=ts, temperature=float(ts % 3))
        for freq in (1000.0, 5000.0, 10000.0):
            self._imp_frame(freq, ts=1700000004)
        self.worker._flush_impedance_scans(force=True)
        rows = self.db.query_sensor_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertAlmostEqual(row["z_real"], 1200.0)

    def test_new_scan_id_finalizes_the_previous_scan(self):
        """新扫描号到了说明上一段结束，不必等静默超时。"""
        self._sensor(ts=1700000000, temperature=20.0)
        self._sensor(ts=1700000015, temperature=24.0)
        for freq in (1000.0, 5000.0):
            self._imp_frame(freq, scan_id="SCAN_1", re_value=1000.0, ts=1700000001)
        for freq in (1000.0, 5000.0):
            self._imp_frame(freq, scan_id="SCAN_2", re_value=3000.0, ts=1700000016)
        self.worker._flush_impedance_scans(force=True)
        rows = {row["timestamp"]: row for row in
                self.db.query_sensor_history("GW_001", "LORA_NODE_01")}
        self.assertAlmostEqual(rows[1700000000]["z_real"], 1000.0)
        self.assertAlmostEqual(rows[1700000015]["z_real"], 3000.0)

    def test_all_out_of_band_scan_attaches_nothing(self):
        """频段内一个点都没有，不拿空均值去填环境行。"""
        self._sensor()
        for freq in (10.0, 100.0, 500.0):
            self._imp_frame(freq)
        self.worker._flush_impedance_scans(force=True)
        row = self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]
        for col in ("frequency_hz", "z_real", "z_imag", "magnitude", "phase"):
            self.assertIsNone(row[col], col)

    def test_impedance_scans_without_recording_buffer_nothing(self):
        # 直接落库一条环境行：不录制时 worker 连环境帧也不收，那就验不了配对。
        self.db.insert_sensor(SensorData(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000001,
            temperature=24.0, co2=430.0))
        self.worker._recording = False
        self._imp_frame(1000.0)
        self.worker._flush_impedance_scans(force=True)
        self.assertEqual(len(self.worker._imp_scan_buf), 0)
        row = self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]
        self.assertIsNone(row["z_real"])

    def _sensor(self, ts: int = 1700000000, temperature: float = 24.0) -> None:
        self.worker._dispatch("sensor", "GW_001", "LORA_NODE_01", {
            "type": "sensor",
            "gateway_id": "GW_001",
            "node_id": "LORA_NODE_01",
            "timestamp": ts,
            "temperature": temperature,
            "co2": 430.0,
            "ph": 6.4,
        })

    def _imp_frame(self, freq: float, scan_id: str = "SCAN_1",
                   re_value: float = 1200.0, ts: int = 1700000001) -> None:
        """逐频点上报的阻抗报文，和真实网关的格式一致。"""
        z_imag = -300.0
        magnitude = math.hypot(re_value, z_imag)
        phase = -math.degrees(math.atan2(z_imag, re_value))
        self.worker._dispatch("impedance", "GW_001", "LORA_NODE_01", {
            "type": "impedance", "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
            "timestamp": ts, "scan_id": scan_id, "frequency_hz": freq,
            "z_real": re_value, "z_imag": z_imag,
            "magnitude": round(magnitude, 2), "phase": phase,
            "rcal_ohm": 51000.0,
            "in_valid_window": bool(1000 <= freq <= 30000),
        })

    def _sensor_with_impedance(self, **over: object) -> None:
        payload = {
            "type": "sensor", "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
            "timestamp": 1700000000, "temperature": 24.0, "co2": 430.0,
            "frequency_hz": 1000.0, "z_real": 3.0, "z_imag": 4.0,
        }
        payload.update(over)
        self.worker._dispatch("sensor", "GW_001", "LORA_NODE_01", payload)

    def test_sensor_frame_impedance_lands_in_the_sensor_row(self):
        self._sensor_with_impedance()
        row = self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]
        self.assertEqual((row["frequency_hz"], row["z_real"], row["z_imag"]),
                         (1000.0, 3.0, 4.0))
        self.assertAlmostEqual(row["magnitude"], 5.0)
        self.assertAlmostEqual(row["phase"], -53.1301024)

    def test_sensor_frame_impedance_also_still_goes_to_impedance_data(self):
        """环境行补了阻抗，不代表 impedance_data 那边可以不写。"""
        self._sensor_with_impedance()
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 1)
        self.assertEqual(
            self.db.count_impedance_rows("GW_001", "LORA_NODE_01"), 1)

    def test_sensor_frame_without_impedance_leaves_the_columns_null(self):
        self._sensor()
        row = self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]
        for col in ("frequency_hz", "z_real", "z_imag", "magnitude", "phase"):
            self.assertIsNone(row[col], col)

    def test_sweep_round_folds_into_a_sensor_row_with_the_band_average(self):
        # 50 点：freq 1k..50k，其中 1k..30k 共 30 点在分析频段内
        self._sweep(self._seg_payload(1, 0, 50, 1200.0))
        rows = self.db.query_sensor_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["round_id"], 1)
        self.assertAlmostEqual(row["z_real"], 1200.0)
        self.assertAlmostEqual(row["z_imag"], -300.0)
        self.assertAlmostEqual(row["magnitude"], 1236.93)
        self.assertAlmostEqual(row["phase"], 14.0362435)
        self.assertIsNone(row["frequency_hz"])
        # 环境量照常从扫频点回填
        self.assertEqual((row["temperature"], row["co2"]), (24.0, 430.0))

    @staticmethod
    def _seg_payload(round_id: int, start: int, count: int, re_value: float) -> dict:
        """网关数组格式的扫频段报文，环境量随点一起上报。"""
        return {
            "type": "sweep", "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
            "round": round_id, "timestamp": 1700000000,
            "point_start": start, "seg": start // count, "seg_total": 2,
            "freq": [1000.0 * (start + i + 1) for i in range(count)],
            "re": [re_value] * count,
            "im": [-300.0] * count,
            "imp": [1236.93] * count,
            "soil_moisture": [52.0] * count,
            "temperature": [24.0] * count,
            "co2": [430] * count,
        }

    def test_full_round_overwrites_the_partial_segment_average(self):
        """分段时手里只有前一半频点，整轮齐了得换成全轮的均值。"""
        self._sweep(self._seg_payload(1, 0, 25, 1000.0))
        self.assertAlmostEqual(
            self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]["z_real"],
            1000.0)
        self._sweep(self._seg_payload(1, 25, 25, 3000.0))
        # 带内：前段 25 点 + 后段 5 点（26k..30k）
        expected = (1000.0 * 25 + 3000.0 * 5) / 30
        self.assertAlmostEqual(
            self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]["z_real"],
            expected, places=6)
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 1)

    def test_spectrum_attaches_the_band_average_to_the_nearest_env_row(self):
        """真实硬件走整谱：没有轮次号，只能按时间就近挂到环境帧。"""
        self._sensor()
        self.worker._dispatch(
            "spectrum", "GW_001", "LORA_NODE_01", self._spectrum_payload(1700000010))
        rows = self.db.query_sensor_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["z_real"], 1200.0)
        self.assertAlmostEqual(row["z_imag"], -300.0)
        self.assertAlmostEqual(row["magnitude"], 1236.93, places=2)
        self.assertAlmostEqual(row["phase"], 14.0362435)
        self.assertIsNone(row["round_id"])

    def test_spectrum_fills_every_env_row_in_its_epoch(self):
        """整谱和分帧一样：一次测量覆盖它之前的整段时间。"""
        self._sensor(ts=1700000000, temperature=20.0)
        self._sensor(ts=1700000004, temperature=24.0)
        self.worker._dispatch(
            "spectrum", "GW_001", "LORA_NODE_01", self._spectrum_payload(1700000004))
        rows = {row["timestamp"]: row for row in
                self.db.query_sensor_history("GW_001", "LORA_NODE_01")}
        self.assertAlmostEqual(rows[1700000000]["z_real"], 1200.0)
        self.assertAlmostEqual(rows[1700000004]["z_real"], 1200.0)
        self.assertEqual(rows[1700000004]["temperature"], 24.0)

    def test_spectrum_does_not_fill_env_rows_after_the_measurement(self):
        """测量之后的环境帧属于下一次测量，不能提前贴上。"""
        self._sensor(ts=1700000000, temperature=20.0)
        self._sensor(ts=1700000004, temperature=24.0)
        self.worker._dispatch(
            "spectrum", "GW_001", "LORA_NODE_01", self._spectrum_payload(1700000001))
        rows = {row["timestamp"]: row for row in
                self.db.query_sensor_history("GW_001", "LORA_NODE_01")}
        self.assertAlmostEqual(rows[1700000000]["z_real"], 1200.0)
        self.assertIsNone(rows[1700000004]["z_real"])

    def test_spectrum_far_from_any_env_row_attaches_nothing(self):
        """差得远不能硬挂：宁可空着，也不能把整谱均值记到不搭界的环境快照上。"""
        self._sensor()
        self.worker._dispatch(
            "spectrum", "GW_001", "LORA_NODE_01", self._spectrum_payload(1700999999))
        row = self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]
        for col in ("frequency_hz", "z_real", "z_imag", "magnitude", "phase"):
            self.assertIsNone(row[col], col)

    def test_spectrum_without_an_env_row_creates_nothing(self):
        self.worker._dispatch(
            "spectrum", "GW_001", "LORA_NODE_01", self._spectrum_payload(1700000000))
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 0)

    @staticmethod
    def _spectrum_payload(ts: int) -> dict:
        """节点组装完整谱后的推送：不带轮次号，环境量另有传感器帧在发。"""
        return {
            "type": "spectrum", "gateway_id": "GW_001", "node_id": "LORA_NODE_01",
            "scan_id": 1, "timestamp": ts,
            "points": [
                {"freq": 1000.0 + i * 100.0, "re": 1200.0, "im": -300.0}
                for i in range(25)
            ],
        }

    def test_no_recording_writes_nothing(self):
        self.worker._recording = False
        self._sensor_with_impedance()
        self._sweep(_points_payload(1, 0, 50, seg=0, seg_total=1))
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 0)
        self.assertEqual(
            self.db.count_impedance_rows("GW_001", "LORA_NODE_01"), 0)


if __name__ == "__main__":
    unittest.main()
