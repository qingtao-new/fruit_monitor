from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import gui
from db import Database
from mqtt_client import MQTTWorker, resolve_msg_type
from protocol import LineFramer, MIN_REPORT_POINTS, parse_topic


# 网关固件里这一轮的采样点就是 SEG_POINTS=50 / TOTAL_POINTS=100，
# 节点侧的频点过滤是 900 Hz ~ 12 kHz。下面的构造完全照固件源码来。
SEG_POINTS = 50
TOTAL_POINTS = 100
SEGMENTS = TOTAL_POINTS // SEG_POINTS
FREQ_LO = 900.0
FREQ_HI = 12000.0
# 当前固件把扫频发到 /sweep；老版本发到 /sensor，两种都得能解析。
SWEEP_TOPIC = "fruit/GW_001/LORA_NODE_01/sweep"
LEGACY_TOPIC = "fruit/GW_001/LORA_NODE_01/sensor"
GATEWAY_TOPIC = SWEEP_TOPIC


def _freqs() -> list[int]:
    return [
        round(FREQ_LO * (FREQ_HI / FREQ_LO) ** (i / (TOTAL_POINTS - 1)))
        for i in range(TOTAL_POINTS)
    ]


def firmware_segment(
    round_id: int,
    seg: int,
    *,
    millis_s: int = 3600,
    r0: float = 2200.0,
) -> dict:
    """复刻固件 publishSegment() 的报文：数组形式、只给 seg 不给 seg_total。"""
    freqs = _freqs()
    base = seg * SEG_POINTS
    count = SEG_POINTS

    def series(name: str, cast=None) -> list:
        out = []
        for i in range(base, base + count):
            f = freqs[i]
            tau = 1.2e-4
            z = 220.0 + (r0 - 220.0) / (1 + (1j * f * tau) ** 0.14)
            zr, zi = z.real, z.imag
            mag = math.hypot(zr, zi)
            if name == "freq":
                val = f
            elif name == "re":
                val = zr
            elif name == "im":
                val = zi
            elif name == "imp":
                val = mag
            elif name == "temperature":
                val = 24.0
            elif name == "ph":
                val = 6.5
            else:
                val = {"soil_moisture": 50, "nh3": 10, "h2s": 5,
                       "co2": 450, "humidity": 65}[name]
            out.append(cast(val) if cast else val)
        return out

    return {
        "type": "sweep",
        "gateway_id": "GW_001",
        "node_id": "LORA_NODE_01",
        "report_id": f"GW_001/LORA_NODE_01/R{round_id}",
        "round": round_id,
        "seg": seg,
        "seg_total": SEGMENTS,
        "point_start": base,
        "point_count": count,
        "ts": millis_s,
        "freq": series("freq", int),
        "re": series("re", int),
        "im": series("im", int),
        "imp": series("imp", int),
        "soil_moisture": series("soil_moisture", int),
        "temperature": series("temperature"),
        "nh3": series("nh3", int),
        "h2s": series("h2s", int),
        "co2": series("co2", int),
        "ph": series("ph"),
        "humidity": series("humidity", int),
    }


def firmware_round_done(round_id: int, points: int, millis_s: int = 3600,
                        retry: int = 0) -> dict:
    """复刻固件 publishRoundDone()：整轮摘要走 status 通道。"""
    return {
        "type": "status",
        "gateway_id": "GW_001",
        "node_id": "LORA_NODE_01",
        "status": "round_done",
        "round": round_id,
        "points": points,
        "total": TOTAL_POINTS,
        "retry": retry,
        "imp_mean": 1234.5,
        "timestamp": millis_s,
    }


def firmware_heartbeat(millis_s: int = 3600) -> dict:
    return {
        "type": "heartbeat",
        "gateway_id": "GW_001",
        "node_id": "LORA_NODE_01",
        "ts": millis_s,
        "ip": "192.168.1.50",
        "rssi": -72,
    }


class GatewayFirmwareCompatTest(unittest.TestCase):
    """网关固件当前版本的报文，必须能被完整解析、入库、显示。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "gw.db")
        self.addCleanup(self.db.close)
        self.worker = MQTTWorker({"sweep": {"min_points": MIN_REPORT_POINTS}}, self.db)
        self.addCleanup(self.worker.stop)
        self.received: list[dict] = []
        self.worker.data_received.connect(self.received.append)

    def _feed(self, payload: dict, topic: str = GATEWAY_TOPIC) -> None:
        """走一遍真实链路：拼帧 → 分主题 → 判定消息类型 → 分发落库。"""
        gw, node, topic_type = parse_topic(topic)
        frame = (json.dumps(payload) + "\n").encode("utf-8")
        decoded = self.worker._framer.feed_json(frame.decode("utf-8"))
        self.assertEqual(len(decoded), 1)
        msg_type = resolve_msg_type(topic_type, decoded[0])
        self.assertTrue(self.worker._dispatch(msg_type, gw, node, decoded[0]))

    def test_sweep_on_sensor_topic_is_routed_by_payload_type(self):
        self.assertEqual(
            resolve_msg_type("sensor", firmware_segment(1, 0)), "sweep")

    def test_sweep_on_legacy_sensor_topic_parses(self):
        """老固件把扫频发 /sensor，PC 按报文里的 type 路由，不能丢。"""
        self._feed(firmware_segment(1, 0), topic=LEGACY_TOPIC)
        self._feed(firmware_segment(1, 1), topic=LEGACY_TOPIC)
        self.assertEqual(
            self.db.count_sweep_rows("GW_001", "LORA_NODE_01", round_id=1),
            TOTAL_POINTS)

    def test_point_count_does_not_shorten_the_round(self):
        """固件报 point_count=50（本次报文点数），不能当成整轮总点数。"""
        self._feed(firmware_segment(1, 0))
        sweep_payloads = [p for p in self.received if p["type"] == "sweep"]
        self.assertEqual(len(sweep_payloads), 1)
        self.assertEqual(sweep_payloads[0]["buffered_points"], SEG_POINTS)
        self.assertEqual(sweep_payloads[0]["total_points"], TOTAL_POINTS)
        self.assertFalse(sweep_payloads[0]["complete"])
        self.assertEqual([p["type"] for p in self.received].count("spectrum"), 0)

    def test_round_completes_after_declared_segments(self):
        self._feed(firmware_segment(2, 0))
        self._feed(firmware_segment(2, 1))
        sweep_payloads = [p for p in self.received if p["type"] == "sweep"]
        self.assertTrue(sweep_payloads[-1]["complete"])
        spectrum = [p for p in self.received if p["type"] == "spectrum"][-1]
        self.assertEqual(spectrum["points_count"], TOTAL_POINTS)

    def test_partial_round_is_reported_incomplete_after_idle(self):
        """第 2 段丢了，静默窗口后也要出图，但标记不完整。"""
        self.worker._round_idle_s = 0.0
        self._feed(firmware_segment(3, 0))
        self.worker._flush_rounds()
        spectrum = [p for p in self.received if p["type"] == "spectrum"][-1]
        self.assertEqual(spectrum["points_count"], SEG_POINTS)
        self.assertFalse(spectrum["complete"])

    def test_round_done_is_not_stored_as_device_status(self):
        self._feed(firmware_round_done(1, TOTAL_POINTS))
        self.assertEqual(self.db.count_status_rows("GW_001"), 0)

    def test_round_done_is_surfaced_with_summary(self):
        self._feed(firmware_round_done(1, TOTAL_POINTS, retry=2))
        done = [p for p in self.received if p["type"] == "round_done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["round_id"], 1)
        self.assertEqual(done[0]["points"], TOTAL_POINTS)
        self.assertEqual(done[0]["total_points"], TOTAL_POINTS)
        self.assertEqual(done[0]["retry"], 2)
        self.assertAlmostEqual(done[0]["imp_mean"], 1234.5)

    def test_heartbeat_is_stored_as_status(self):
        self._feed(firmware_heartbeat(), topic="fruit/GW_001/LORA_NODE_01/status")
        self.assertEqual(self.db.count_status_rows("GW_001"), 1)

    def test_one_round_parses_to_all_100_points(self):
        self._feed(firmware_segment(1, 0))
        self._feed(firmware_segment(1, 1))
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 1)
        self.assertEqual(len(rows), TOTAL_POINTS)
        self.assertEqual([r["point_index"] for r in rows], list(range(TOTAL_POINTS)))

    def test_timestamp_is_rebased_to_wallclock(self):
        self._feed(firmware_segment(1, 0))
        self._feed(firmware_segment(1, 1))
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 1)
        for row in rows:
            self.assertGreater(row["timestamp"], 1e8)

    def test_report_id_is_synthesized(self):
        self._feed(firmware_segment(1, 0))
        self._feed(firmware_segment(1, 1))
        rounds = self.db.list_sweep_rounds("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["report_id"], "GW_001/LORA_NODE_01/R1")

    def test_environment_columns_are_synced(self):
        """环境传感器必须和阻抗一起落到 sweep_data 的行级字段。"""
        self._feed(firmware_segment(1, 0))
        self._feed(firmware_segment(1, 1))
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 1)
        for row in rows:
            self.assertEqual(row["soil_moisture"], 50.0)
            self.assertAlmostEqual(row["temperature"], 24.0)
            self.assertEqual(row["nh3"], 10.0)
            self.assertEqual(row["h2s"], 5.0)
            self.assertEqual(row["co2"], 450.0)
            self.assertAlmostEqual(row["ph"], 6.5)
            self.assertEqual(row["humidity"], 65.0)

    def test_environment_values_land_in_sensor_data(self):
        """网关不单独发环境帧，环境量只在扫频帧里。

        不抽出来写 sensor_data，环境卡片和环境历史永远是空的，
        设备也从 list_devices() 里消失。
        """
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 0)
        self._feed(firmware_segment(12, 0))
        self._feed(firmware_segment(12, 1))
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 1)
        row = self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]
        self.assertEqual(row["round_id"], 12)
        self.assertEqual(row["soil_moisture"], 50.0)
        self.assertAlmostEqual(row["temperature"], 24.0)
        self.assertEqual(row["nh3"], 10.0)
        self.assertEqual(row["h2s"], 5.0)
        self.assertEqual(row["co2"], 450.0)
        self.assertAlmostEqual(row["ph"], 6.5)
        self.assertEqual(row["humidity"], 65.0)

    def test_environment_row_is_one_per_round(self):
        """一轮 2 段报文 + QoS1 重投，只出一条环境数据，不刷 50 条一样的。"""
        self._feed(firmware_segment(13, 0))
        self._feed(firmware_segment(13, 0))
        self._feed(firmware_segment(13, 1))
        self._feed(firmware_segment(13, 1))
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 1)
        sensor_payloads = [p for p in self.received if p["type"] == "sensor"]
        self.assertEqual(len(sensor_payloads), 1)
        self.assertEqual(sensor_payloads[0]["data"].round_id, 13)

    def test_environment_rows_are_per_round_and_per_node(self):
        self._feed(firmware_segment(14, 0))
        self._feed(firmware_segment(14, 1))
        self._feed(firmware_segment(15, 0))
        self._feed(firmware_segment(15, 1))
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 2)
        self.assertEqual([row["round_id"] for row in
                          self.db.query_sensor_history("GW_001", "LORA_NODE_01")],
                         [14, 15])

    def test_environment_emitted_even_when_not_recording(self):
        """暂停落库时环境卡片仍然要能刷新。"""
        self.worker.set_recording(False)
        self._feed(firmware_segment(16, 0))
        self.assertEqual(self.db.count_sensors("GW_001", "LORA_NODE_01"), 0)
        sensor_payloads = [p for p in self.received if p["type"] == "sensor"]
        self.assertEqual(len(sensor_payloads), 1)

    def test_environment_timestamp_uses_the_round_anchor(self):
        """环境行的时间戳和阻抗 scan_id 取同一个轮次锚点。"""
        self._feed(firmware_segment(17, 0))
        self._feed(firmware_segment(17, 1))
        scan = self.db.list_impedance_scans("GW_001", "LORA_NODE_01")[0]
        row = self.db.query_sensor_history("GW_001", "LORA_NODE_01")[0]
        self.assertIn(str(row["timestamp"]), scan["scan_id"])

    def test_frequency_band_matches_firmware(self):
        self._feed(firmware_segment(1, 0))
        self._feed(firmware_segment(1, 1))
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 1)
        freqs = [row["frequency_hz"] for row in rows]
        self.assertEqual(len(set(freqs)), TOTAL_POINTS)
        self.assertGreaterEqual(min(freqs), FREQ_LO - 1.0)
        self.assertLessEqual(max(freqs), FREQ_HI + 1.0)

    def test_re_im_and_magnitude_are_consistent(self):
        self._feed(firmware_segment(1, 0))
        self._feed(firmware_segment(1, 1))
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 1)
        for row in rows:
            recomputed = math.hypot(row["z_real"], row["z_imag"])
            self.assertAlmostEqual(row["magnitude"], recomputed, delta=1.5)

    def test_first_segment_alone_does_not_emit_spectrum(self):
        """固件只给 seg 不给 seg_total，第一段 50 点不能当成整轮。"""
        self._feed(firmware_segment(2, 0))
        types = [p["type"] for p in self.received]
        self.assertNotIn("spectrum", types)
        last = [p for p in self.received if p["type"] == "sweep"][-1]
        self.assertFalse(last["complete"])
        self.assertEqual(last["buffered_points"], SEG_POINTS)

    def test_round_completes_after_second_segment(self):
        self._feed(firmware_segment(3, 0))
        self._feed(firmware_segment(3, 1))
        types = [p["type"] for p in self.received]
        self.assertIn("spectrum", types)
        spectrum = [p for p in self.received if p["type"] == "spectrum"][-1]
        self.assertEqual(spectrum["points_count"], TOTAL_POINTS)
        self.assertTrue(spectrum["complete"])

    def test_impedance_rows_written_per_round(self):
        self._feed(firmware_segment(4, 0))
        self._feed(firmware_segment(4, 1))
        imp_rows = self.db.query_impedance_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(imp_rows), TOTAL_POINTS)

    def test_impedance_rows_written_per_segment(self):
        """只到第一段就得有 50 条阻抗历史，不能等整轮收齐。

        整轮收齐出谱时会再写一遍同一批点，去重后总数不变；
        但如果这一轮后面丢包、程序直接退出，这 50 个点不能丢。
        """
        self._feed(firmware_segment(6, 0))
        imp_rows = self.db.query_impedance_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(imp_rows), SEG_POINTS)
        scan_ids = {row["scan_id"] for row in imp_rows}
        self.assertEqual(len(scan_ids), 1)
        self.assertTrue(scan_ids.pop())

    def test_impedance_rows_stable_after_round_end(self):
        """分段写 + 整轮出谱再写一遍，仍然只有 100 条。"""
        self._feed(firmware_segment(7, 0))
        self.assertEqual(
            len(self.db.query_impedance_history("GW_001", "LORA_NODE_01")),
            SEG_POINTS)
        self._feed(firmware_segment(7, 1))
        imp_rows = self.db.query_impedance_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(imp_rows), TOTAL_POINTS)
        # 100 个频点各只有一条，说明分段写和整轮出谱没有叠加。
        self.assertEqual(len({row["frequency_hz"] for row in imp_rows}), TOTAL_POINTS)

    def test_impedance_dedup_survives_qos_redelivery(self):
        """QoS1 重投同一段，阻抗历史也不会翻倍。"""
        self._feed(firmware_segment(8, 0))
        self._feed(firmware_segment(8, 0))
        self._feed(firmware_segment(8, 1))
        self._feed(firmware_segment(8, 1))
        imp_rows = self.db.query_impedance_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(imp_rows), TOTAL_POINTS)

    def test_scan_id_unique_across_restarts(self):
        """轮次号重启会归零，scan_id 得靠墙钟时间区分开。

        只用轮次号的话，两次运行都会产出 SCAN_1，不同轮次的点被混进
        同一个 scan_id，行数就不再能当"这轮收齐了几个点"来校验。
        """
        self._feed(firmware_segment(9, 0, millis_s=3600))
        self._feed(firmware_segment(9, 1, millis_s=3600))
        self._feed(firmware_segment(9, 0, millis_s=7200))
        self._feed(firmware_segment(9, 1, millis_s=7200))
        scans = self.db.list_impedance_scans("GW_001", "LORA_NODE_01")
        self.assertEqual(len(scans), 2)
        self.assertEqual({row["rows"] for row in scans}, {TOTAL_POINTS})

    def test_flush_on_stop_saves_pending_round(self):
        """退出收尾：静默窗口还没走完的轮次也要落库。"""
        self._feed(firmware_segment(10, 0))
        self.assertEqual(self.worker._round_buf.points_count(
            ("GW_001", "LORA_NODE_01", 10)), SEG_POINTS)
        self.worker._flush_rounds(force=True)
        imp_rows = self.db.query_impedance_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(imp_rows), SEG_POINTS)
        self.assertEqual(self.worker._round_buf.points_count(
            ("GW_001", "LORA_NODE_01", 10)), 0)
        self.assertIn(
            "spectrum", [p["type"] for p in self.received])
        spectrum = [p for p in self.received if p["type"] == "spectrum"][-1]
        self.assertFalse(spectrum["complete"])

    def test_partial_round_still_persists_impedance(self):
        """第二段丢了，第一段的 50 个点也已经在历史表里。"""
        self._feed(firmware_segment(11, 0))
        imp_rows = self.db.query_impedance_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(imp_rows), SEG_POINTS)
        self.assertEqual(
            self.db.count_sweep_rows("GW_001", "LORA_NODE_01", round_id=11),
            SEG_POINTS)

    def test_dedup_survives_qos_redelivery(self):
        """QoS1 重投同一段报文，库里仍然只有 100 行。"""
        self._feed(firmware_segment(5, 0))
        self._feed(firmware_segment(5, 0))
        self._feed(firmware_segment(5, 1))
        self._feed(firmware_segment(5, 1))
        self.assertEqual(
            self.db.count_sweep_rows("GW_001", "LORA_NODE_01", round_id=5),
            TOTAL_POINTS)

    def test_heartbeat_is_stored_as_status(self):
        self._feed(firmware_heartbeat(), topic="fruit/GW_001/LORA_NODE_01/status")
        self.assertEqual(self.db.count_status_rows("GW_001"), 1)

    def test_three_rounds_end_to_end(self):
        """连跑三轮，和真实采集节奏一致。"""
        for round_id in (1, 2, 3):
            self._feed(firmware_segment(round_id, 0, millis_s=3600 + round_id * 8))
            self._feed(firmware_segment(round_id, 1, millis_s=3600 + round_id * 8))
        self.assertEqual(
            self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 3 * TOTAL_POINTS)
        rounds = self.db.list_sweep_rounds("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rounds), 3)
        self.assertEqual({r["points"] for r in rounds}, {TOTAL_POINTS})

    # ---- round_log：轮次完整性判定 ----

    def test_round_log_records_complete_round(self):
        """整轮收齐得在 round_log 留一条完整判定，不能只留在扫频行里。"""
        self._feed(firmware_segment(20, 0, millis_s=3600))
        self._feed(firmware_segment(20, 1, millis_s=3600))
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["round_id"], 20)
        self.assertEqual(row["points"], TOTAL_POINTS)
        self.assertEqual(row["total_points"], TOTAL_POINTS)
        self.assertEqual(row["complete"], 1)
        self.assertTrue(row["scan_id"])
        self.assertGreaterEqual(row["last_ts"], row["first_ts"])
        # 时间戳是墙钟秒，不是网关开机秒。
        self.assertGreater(row["first_ts"], 1_700_000_000)

    def test_round_log_flags_missing_segment(self):
        """第二段丢包：判定记不完整，缺口点数能直接查出来。"""
        self.worker._round_idle_s = 0.0
        self._feed(firmware_segment(21, 0))
        self.worker._flush_rounds()
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["points"], SEG_POINTS)
        self.assertEqual(row["total_points"], TOTAL_POINTS)
        self.assertEqual(row["complete"], 0)
        self.assertEqual(self.db.count_incomplete_rounds("GW_001", "LORA_NODE_01"), 1)

    def test_round_done_fills_retry_keeps_local_point_count(self):
        """固件自报的点数不能覆盖本地实际落库的点数。

        固件说 100 点、本地只有 50 点，这一轮恰恰是要被看见的缺口，
        用固件数字盖过去等于把证据抹掉。
        """
        self.worker._round_idle_s = 0.0
        self._feed(firmware_segment(22, 0))
        self.worker._flush_rounds()
        self._feed(firmware_round_done(22, TOTAL_POINTS, retry=2))
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual(row["points"], SEG_POINTS)
        self.assertEqual(row["total_points"], TOTAL_POINTS)
        self.assertEqual(row["retry"], 2)
        self.assertEqual(row["imp_mean"], 1234.5)
        self.assertEqual(row["complete"], 0)

    def test_round_done_alone_leaves_a_record(self):
        """整轮摘要先到、段还没落库：留行，但不假装有本地点数。"""
        self._feed(firmware_round_done(23, TOTAL_POINTS, retry=1))
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_points"], TOTAL_POINTS)
        self.assertEqual(rows[0]["retry"], 1)
        self.assertIsNone(rows[0]["points"])
        self.assertIsNone(rows[0]["complete"])
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 0)

    def test_local_flush_after_round_done_wins(self):
        """摘要先到、本地随后只落了半轮：如实记半轮不完整。

        固件报的 points 不能替我们记账，否则丢了一半包这一轮会显示完整。
        """
        self._feed(firmware_round_done(27, TOTAL_POINTS, retry=2))
        self.worker._round_idle_s = 0.0
        self._feed(firmware_segment(27, 0))
        self.worker._flush_rounds()
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual((row["points"], row["complete"]), (SEG_POINTS, 0))
        self.assertEqual(row["retry"], 2)
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 1)

    def test_late_segment_reverses_incomplete_verdict(self):
        """先超时判不完整、迟到的段到了再刷一次：判定跟着翻成完整。

        迟到的那次手里只有后到的 50 点，点数得从库里这一轮实际有几个点
        数出来，否则会把已经补齐的轮次判回不完整。
        """
        self.worker._round_idle_s = 0.0
        self._feed(firmware_segment(28, 0))
        self.worker._flush_rounds()
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 1)
        self._feed(firmware_segment(28, 1))
        self.worker._flush_rounds()
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["points"], TOTAL_POINTS)
        self.assertEqual(rows[0]["complete"], 1)
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 0)

    def test_round_log_survives_qos_redelivery(self):
        """段和摘要各重投一次，判定仍然一轮一条。"""
        self._feed(firmware_segment(24, 0))
        self._feed(firmware_segment(24, 0))
        self._feed(firmware_segment(24, 1))
        self._feed(firmware_round_done(24, TOTAL_POINTS, retry=0))
        self._feed(firmware_round_done(24, TOTAL_POINTS, retry=0))
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["complete"], 1)
        self.assertEqual(rows[0]["retry"], 0)

    def test_round_log_written_on_force_flush_at_exit(self):
        """退出收尾强刷也留下判定，不能只落阻抗行不落完整性。"""
        self._feed(firmware_segment(25, 0))
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 0)
        self.worker._flush_rounds(force=True)
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual(row["points"], SEG_POINTS)
        self.assertEqual(row["complete"], 0)

    def test_three_rounds_each_get_one_verdict(self):
        """连跑三轮，每轮一条判定、三个不同的 scan_id、没有不完整轮。"""
        for round_id in (30, 31, 32):
            self._feed(firmware_segment(round_id, 0, millis_s=3600 + round_id))
            self._feed(firmware_segment(round_id, 1, millis_s=3600 + round_id))
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["complete"] for row in rows}, {1})
        self.assertEqual({row["points"] for row in rows}, {TOTAL_POINTS})
        self.assertEqual(len({row["scan_id"] for row in rows}), 3)
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 0)


class GatewayLiveWindowTest(unittest.TestCase):
    """网关报文直接喂给实时阻抗窗口，逐点表格 + 环境量都要出来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "gw_gui.db")
        self.addCleanup(self.db.close)
        self.worker = MQTTWorker({"sweep": {"min_points": MIN_REPORT_POINTS}}, self.db)
        self.addCleanup(self.worker.stop)
        self.win = gui.LiveSweepWindow(self.db, None)
        self.addCleanup(self.win.deleteLater)

        self.received: list[dict] = []

        def on_payload(payload: dict) -> None:
            self.received.append(payload)
            ptype = payload.get("type")
            if ptype == "sweep":
                self.win.feed_report(payload)
            elif ptype == "spectrum":
                self.win.feed_spectrum(payload)
            elif ptype == "round_done":
                self.win.show_round_done(payload)

        self.worker.data_received.connect(on_payload)

    def test_gateway_round_fills_live_window(self):
        for seg in (0, 1):
            frame = (json.dumps(firmware_segment(1, seg)) + "\n").encode("utf-8")
            gw, node, topic_type = parse_topic(GATEWAY_TOPIC)
            for decoded in self.worker._framer.feed_json(frame.decode("utf-8")):
                msg_type = resolve_msg_type(topic_type, decoded)
                self.worker._dispatch(msg_type, gw, node, decoded)

        self.assertEqual(self.win._table.rowCount(), TOTAL_POINTS)
        self.assertEqual(self.win._progress.value(), MIN_REPORT_POINTS)
        self.assertIn(str(MIN_REPORT_POINTS), self.win._count_label.text())

        # 表格里必须有逐点频率和行级环境量
        first = self.win._points[0]
        self.assertIsNotNone(self.win._cell(first, "frequency_hz"))
        self.assertIsNotNone(self.win._cell(first, "z_real"))
        self.assertIsNotNone(self.win._cell(first, "z_imag"))
        self.assertIsNotNone(self.win._cell(first, "magnitude"))
        self.assertEqual(self.win._cell(first, "soil_moisture"), 50.0)
        self.assertEqual(self.win._cell(first, "co2"), 450.0)
        self.assertAlmostEqual(self.win._cell(first, "temperature"), 24.0)

    def test_incomplete_gateway_report_is_flagged(self):
        payloads: list[dict] = []
        frame = (json.dumps(firmware_segment(2, 0)) + "\n").encode("utf-8")
        gw, node, topic_type = parse_topic(GATEWAY_TOPIC)
        for decoded in self.worker._framer.feed_json(frame.decode("utf-8")):
            msg_type = resolve_msg_type(topic_type, decoded)
            self.worker._dispatch(msg_type, gw, node, decoded)

        payloads = [p for p in self.received if p["type"] == "sweep"]
        self.assertEqual(len(payloads), 1)
        # 50 点的第一段不算整轮，即使 point_count 也是 50
        self.assertFalse(payloads[0]["complete"])
        self.assertEqual(payloads[0]["buffered_points"], SEG_POINTS)
        self.assertEqual(payloads[0]["total_points"], TOTAL_POINTS)

        self.assertEqual(self.win._table.rowCount(), SEG_POINTS)
        # 一轮 100 点拆 2 段，第一段到这里就停了，要写清还差多少
        self.assertIn("本轮", self.win._state_label.text())
        self.assertIn(f"{SEG_POINTS}/{TOTAL_POINTS}", self.win._state_label.text())
        self.assertNotIn("整轮已满", self.win._state_label.text())

    def test_round_done_updates_window_status(self):
        """整轮摘要到达后，窗口要明确写出收齐了几点、补发过几次。"""
        self.win.show_round_done({
            "round_id": 1, "points": TOTAL_POINTS, "total_points": TOTAL_POINTS,
            "retry": 0, "imp_mean": 1234.5,
        })
        text = self.win._state_label.text()
        self.assertIn("整轮确认", text)
        self.assertIn(f"{TOTAL_POINTS}/{TOTAL_POINTS}", text)
        self.assertNotIn("缺", text)

        self.win.show_round_done({
            "round_id": 2, "points": SEG_POINTS, "total_points": TOTAL_POINTS,
            "retry": 3, "imp_mean": 1234.5,
        })
        text = self.win._state_label.text()
        self.assertIn(f"{SEG_POINTS}/{TOTAL_POINTS}", text)
        self.assertIn("缺 50 点", text)
        self.assertIn("补发 3 次", text)

    def test_round_done_reaches_window_via_worker(self):
        frame = (json.dumps(firmware_round_done(1, TOTAL_POINTS, retry=1)) + "\n").encode("utf-8")
        gw, node, topic_type = parse_topic("fruit/GW_001/LORA_NODE_01/status")
        for decoded in self.worker._framer.feed_json(frame.decode("utf-8")):
            msg_type = resolve_msg_type(topic_type, decoded)
            self.worker._dispatch(msg_type, gw, node, decoded)
        self.assertIn("整轮确认", self.win._state_label.text())


if __name__ == "__main__":
    unittest.main()
