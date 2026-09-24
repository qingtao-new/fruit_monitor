from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

import gui
import mqtt_client
from db import Database
from mqtt_client import (
    MIN_REPORT_POINTS,
    SWEEP_FREQS,
    SWEEP_SEGMENT_SIZE,
    SWEEP_SEGMENTS,
    build_sweep_round,
)
from protocol import ImpedanceData, SensorData
from spectrum import sweep_points_to_spectrum

QApplication.instance() or QApplication([])


class LiveSweepWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "gui.db")
        self.addCleanup(self.db.close)
        self.win = gui.LiveSweepWindow(self.db, None)
        self.addCleanup(self.win.deleteLater)

    def _seg_payload(self, points, round_id=1, k=0):
        seg = points[k * SWEEP_SEGMENT_SIZE:(k + 1) * SWEEP_SEGMENT_SIZE]
        return seg, {
            "type": "sweep",
            "data": seg,
            "report_id": f"GW_001/LORA_NODE_01/R{round_id}",
            "round_id": round_id,
            "points_count": len(seg),
            "min_points": MIN_REPORT_POINTS,
            "seg": k,
            "seg_total": SWEEP_SEGMENTS,
            "segment_complete": len(seg) >= MIN_REPORT_POINTS,
            "buffered_points": len(seg) * (k + 1),
            "complete": k + 1 >= SWEEP_SEGMENTS,
        }

    def test_min_points_cannot_go_below_protocol_floor(self):
        low = gui.LiveSweepWindow(self.db, None, min_points=3)
        self.addCleanup(low.deleteLater)
        self.assertEqual(low._min_points, MIN_REPORT_POINTS)

    def test_feed_report_populates_table(self):
        points, _ = build_sweep_round("GW_001", "LORA_NODE_01", 2200.0, 1)
        _seg, payload = self._seg_payload(points, round_id=1)
        self.win.feed_report(payload)
        self.assertEqual(self.win._table.rowCount(), len(_seg))
        self.assertEqual(len(self.win._points), len(_seg))
        self.assertEqual(
            self.win._count_label.text(), f"{len(_seg)} / {MIN_REPORT_POINTS}")

    def test_feed_report_accumulates_across_segments(self):
        points, _ = build_sweep_round("GW_001", "LORA_NODE_01", 2200.0, 2)
        for k in range(SWEEP_SEGMENTS):
            self.win.feed_report(self._seg_payload(points, round_id=2, k=k)[1])
        self.assertEqual(self.win._table.rowCount(), len(points))
        self.assertEqual(self.win._table.rowCount(), len(SWEEP_FREQS))
        self.assertGreaterEqual(self.win._progress.value(), MIN_REPORT_POINTS)

    def test_small_segment_is_marked_incomplete(self):
        points, _ = build_sweep_round("GW_001", "LORA_NODE_01", 2200.0, 3)
        seg = points[:20]
        self.win.feed_report({
            "type": "sweep",
            "data": seg,
            "report_id": "GW_001/LORA_NODE_01/R3",
            "round_id": 3,
            "points_count": 20,
            "min_points": MIN_REPORT_POINTS,
            "seg": 0,
            "seg_total": 3,
            "segment_complete": False,
            "buffered_points": 20,
            "complete": False,
        })
        self.assertIn(str(MIN_REPORT_POINTS), self.win._state_label.text())
        self.assertEqual(self.win._table.rowCount(), 20)

    def test_feed_spectrum_overwrites(self):
        points, _ = build_sweep_round("GW_001", "LORA_NODE_01", 2200.0, 4)
        spectrum = sweep_points_to_spectrum(points)
        self.win.feed_spectrum({
            "type": "spectrum",
            "data": spectrum,
            "prediction": None,
            "report_id": "GW_001/LORA_NODE_01/R4",
            "round_id": 4,
            "points_count": len(spectrum.points),
            "min_points": MIN_REPORT_POINTS,
            "complete": True,
        })
        self.assertEqual(self.win._table.rowCount(), len(spectrum.points))
        self.assertEqual(len(self.win._points), len(spectrum.points))

    def test_load_round_from_db(self):
        points, _ = build_sweep_round("GW_001", "LORA_NODE_01", 2200.0, 6)
        self.db.insert_sweep_points(points)
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 6)
        self.assertEqual(len(rows), len(SWEEP_FREQS))

        ok = self.win._load_round_from_db("GW_001", "LORA_NODE_01")
        self.assertTrue(ok)
        self.assertEqual(self.win._table.rowCount(), len(SWEEP_FREQS))
        self.assertGreaterEqual(self.win._progress.value(), MIN_REPORT_POINTS)
        self.assertEqual(self.win._report_key, "GW_001/LORA_NODE_01/R6")

    def test_load_round_without_device_selection_fails(self):
        points, _ = build_sweep_round("GW_001", "LORA_NODE_01", 2200.0, 6)
        self.db.insert_sweep_points(points)
        self.assertFalse(self.win._load_round_from_db())

    def test_empty_feed_is_safe(self):
        self.win.feed_report({"type": "sweep", "data": []})
        self.win.feed_spectrum({"type": "spectrum", "data": None})
        self.assertEqual(self.win._table.rowCount(), 0)

    def test_cell_reads_attribute_and_phase_math(self):
        points, _ = build_sweep_round("GW_001", "LORA_NODE_01", 2200.0, 7)
        self.assertIsNotNone(self.win._cell(points[0], "frequency_hz"))
        self.assertIsNotNone(self.win._cell(points[0], "magnitude"))
        self.assertIsNone(self.win._cell(points[0], "no_such_field"))
        self.assertEqual(self.win._cell({"frequency_hz": 1000}, "frequency_hz"), 1000.0)


class MainWindowSmokeTest(unittest.TestCase):
    """端到端冒烟：模拟器出一轮扫频 → 主界面 → 实时阻抗窗口。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        config = json.loads(
            (Path(__file__).resolve().parents[1] / "config" / "config.json").read_text(
                encoding="utf-8")
        )
        config["sweep"] = {"min_points": MIN_REPORT_POINTS}
        self.db = Database(Path(self._tmp.name) / "smoke.db")
        self.addCleanup(self.db.close)
        self.worker = mqtt_client.SimulatorWorker(config, self.db)
        self.addCleanup(self.worker.stop)
        self.window = gui.MainWindow(config, self.db, self.worker)
        self.addCleanup(self.window.close)

    def test_impedance_card_does_not_overlap_sensor_cards(self):
        """阻抗卡片不能和传感器卡片挤在同一个格子里。

        挤在一起时两个卡片互相覆盖，阻抗卡片等于白做——界面上永远见不到。
        """
        layout = self.window._impedance_card.parentWidget().layout()
        cells: dict[tuple[int, int], list] = {}
        for card in list(self.window._cards.values()) + [self.window._impedance_card]:
            cells.setdefault(layout.getItemPosition(layout.indexOf(card)), []).append(card)
        dupes = {pos: len(cards) for pos, cards in cells.items() if len(cards) > 1}
        self.assertEqual(dupes, {})

    def test_simulated_round_reaches_live_window(self):
        self.window._open_live_sweep()
        live = self.window._live_win
        self.assertIsNotNone(live)
        self.assertEqual(live._table.rowCount(), 0)

        self.worker._publish_impedance("GW_001", "LORA_NODE_01")

        self.assertEqual(live._table.rowCount(), len(SWEEP_FREQS))
        self.assertEqual(self.window._current_gw, "GW_001")
        self.assertEqual(self.window._current_node, "LORA_NODE_01")
        self.assertGreaterEqual(self.window._msg_count, SWEEP_SEGMENTS + 1)
        self.assertEqual(
            self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), len(SWEEP_FREQS))
        self.assertEqual(self.db.count_predictions("LORA_NODE_01"), 1)

    def test_sweep_payloads_are_complete_after_last_segment(self):
        self.window._open_live_sweep()
        sweep_payloads = []

        def spy(payload):
            if payload.get("type") == "sweep":
                sweep_payloads.append(payload)

        self.worker.data_received.connect(spy)
        self.worker._publish_impedance("GW_001", "LORA_NODE_01")

        self.assertEqual(len(sweep_payloads), SWEEP_SEGMENTS)
        for payload in sweep_payloads:
            self.assertEqual(payload["points_count"], SWEEP_SEGMENT_SIZE)
            self.assertTrue(payload["segment_complete"])
            self.assertEqual(payload["min_points"], MIN_REPORT_POINTS)
        self.assertTrue(sweep_payloads[-1]["complete"])


class HistorySensorColumnsTest(unittest.TestCase):
    """历史数据表要能把环境量、阻抗量一起摆出来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "hist.db")
        self.addCleanup(self.db.close)
        now = int(time.time())
        self.db.insert_sensor(
            SensorData(
                "GW_001", "LORA_NODE_01", now,
                26.0, 60.0, 50.0, 0.8, 0.2, 800.0, 6.2, round_id=7,
            ),
            impedance=ImpedanceData(
                "GW_001", "LORA_NODE_01", now, "SCAN_1",
                1000.0, 1200.0, -300.0, 1236.93, 14.036,
            ),
        )
        self.dlg = gui.HistoryDialog(self.db, None)
        self.addCleanup(self.dlg.deleteLater)
        # 塞一个字典序排在 lora 前面的残留节点，验证默认不会停在它上面。
        self.db.insert_sensor(
            SensorData("GW_001", "123", now, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
        )
        self.dlg._load_devices()
        self.dlg._hours.setValue(24)
        self.dlg._do_query()

    def _headers(self) -> list[str]:
        return [
            self.dlg._table.horizontalHeaderItem(c).text()
            for c in range(self.dlg._table.columnCount())
        ]

    def _cell(self, header: str) -> str:
        return self.dlg._table.item(0, self._headers().index(header)).text()

    def test_default_node_is_the_lora_one(self):
        nodes = [self.dlg._node.itemText(i) for i in range(self.dlg._node.count())]
        self.assertEqual(nodes, ["123", "LORA_NODE_01"])
        self.assertEqual(self.dlg._node.currentText(), "LORA_NODE_01")

    def test_env_and_impedance_columns_share_one_row(self):
        self.assertEqual(self.dlg._table.rowCount(), 1)
        for key in ("temperature", "soil_moisture", "frequency_hz", "z_real", "z_imag", "magnitude", "phase"):
            self.assertIn(key, self._headers())

    def test_column_order_groups_impedance_after_environment(self):
        order = [
            h for h in self._headers()
            if h in ("round_id", "temperature", "co2", "frequency_hz", "magnitude", "phase")
        ]
        self.assertEqual(order, ["round_id", "temperature", "co2", "frequency_hz", "magnitude", "phase"])

    def test_impedance_cells_hold_the_measured_values(self):
        self.assertEqual(self._cell("round_id"), "7")
        self.assertEqual(self._cell("frequency_hz"), "1000.0")
        self.assertAlmostEqual(float(self._cell("z_real")), 1200.0)
        self.assertAlmostEqual(float(self._cell("z_imag")), -300.0)
        self.assertAlmostEqual(float(self._cell("magnitude")), 1236.9)
        self.assertAlmostEqual(float(self._cell("phase")), 14.04)
        self.assertAlmostEqual(float(self._cell("temperature")), 26.0)
        self.assertAlmostEqual(float(self._cell("soil_moisture")), 50.0)

    def test_stale_column_selection_does_not_break_the_export(self):
        row = {"id": 1, "temperature": 26.0}
        self.assertEqual(
            gui.HistoryDialog._export_columns([row], ["temperature", "a_dropped_column"]),
            ["temperature"],
        )
        self.assertEqual(
            gui.HistoryDialog._export_columns([row], ["a_dropped_column"]),
            list(row.keys()),
        )
        self.assertEqual(gui.HistoryDialog._export_columns([row], []), list(row.keys()))


class HistoryPaginationTest(unittest.TestCase):
    """翻页要回库取那一页：一次只取一页的话，翻过去切出来就是空表。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "page.db")
        self.addCleanup(self.db.close)
        now = int(time.time())
        # 25 行，温度递增且时间戳递增：库里按时间倒序，翻页顺序可验。
        for i in range(25):
            self.db.insert_sensor(SensorData(
                "GW_001", "LORA_NODE_01", now - (25 - i) * 10,
                20.0 + i, 60.0, 50.0, 0.8, 0.2, 800.0, 6.2,
            ))
        self.dlg = gui.HistoryDialog(self.db, None)
        self.addCleanup(self.dlg.deleteLater)
        self.dlg._load_devices()
        self.dlg._hours.setValue(24)
        self.dlg._page_size_box.setValue(10)
        self.dlg._do_query()

    def _page_temps(self) -> list[float]:
        headers = [
            self.dlg._table.horizontalHeaderItem(c).text()
            for c in range(self.dlg._table.columnCount())
        ]
        col = headers.index("temperature")
        return [float(self.dlg._table.item(r, col).text())
                for r in range(self.dlg._table.rowCount())]

    def test_first_page_holds_its_share_of_rows(self):
        self.assertEqual(len(self._page_temps()), 10)

    def test_next_page_shows_a_different_page(self):
        first = self._page_temps()
        self.dlg._next_page()
        second = self._page_temps()
        self.assertEqual(len(second), 10)
        self.assertFalse(set(first) & set(second),
                         "翻页后还是同一批行")

    def test_pages_cover_the_whole_query_without_gaps(self):
        pages: list[float] = []
        for _ in range(3):
            pages += self._page_temps()
            self.dlg._next_page()
        self.assertEqual(sorted(pages), [20.0 + i for i in range(25)])

    def test_last_page_is_shorter(self):
        self.dlg._next_page()
        self.dlg._next_page()
        self.assertEqual(len(self._page_temps()), 5)

    def test_prev_page_goes_back_to_the_first_page(self):
        self.dlg._next_page()
        self.dlg._prev_page()
        self.assertEqual(self.dlg._page.value(), 1)
        self.assertEqual(len(self._page_temps()), 10)

    def test_changing_page_size_requeries_from_page_one(self):
        self.dlg._next_page()
        self.dlg._page_size_box.setValue(20)
        self.assertEqual(self.dlg._page.value(), 1)
        self.assertEqual(len(self._page_temps()), 20)


if __name__ == "__main__":
    unittest.main()
