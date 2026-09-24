from __future__ import annotations

import math
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from db import Database
from protocol import (
    DeviceStatus,
    ImpedanceData,
    PredictionData,
    SensorData,
    SweepPointData,
)


class TempDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.addCleanup(self._finish)

    def _finish(self) -> None:
        self.db.close()
        self._tmp.cleanup()


def make_sweep(index: int, *, gateway="GW_001", node="LORA_NODE_01",
               round_id=1, timestamp=1700000000, frequency=1000.0) -> SweepPointData:
    return SweepPointData(
        gateway_id=gateway, node_id=node, round_id=round_id, timestamp=timestamp,
        point_index=index, frequency_hz=frequency, z_real=1200.0, z_imag=-300.0,
        magnitude=1236.93, soil_moisture=60.0, temperature=24.0, nh3=0.1, h2s=0.02,
        co2=430.0, ph=6.4, humidity=62.0,
        report_id=f"{gateway}/{node}/R{round_id}",
    )


class SchemaTest(TempDbTest):
    def test_pragmas_tuned(self):
        with self.db._lock:
            journal = self.db._conn.execute("PRAGMA journal_mode").fetchone()[0]
            sync = self.db._conn.execute("PRAGMA synchronous").fetchone()[0]
            busy = self.db._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(str(journal).lower(), "wal")
        self.assertEqual(int(sync), 1)
        self.assertEqual(int(busy), 2500)

    def test_tables_created(self):
        with self.db._lock:
            names = {row[0] for row in self.db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        for table in ("sensor_data", "impedance_data", "device_status",
                      "prediction", "sweep_data", "round_log"):
            self.assertIn(table, names)

    def test_round_log_indexed_by_device_round_and_time(self):
        """轮次完整性得能按"某设备某轮"和"某设备最近时间"两种问法查。"""
        with self.db._lock:
            names = {row[0] for row in self.db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='round_log'"
            ).fetchall()}
        self.assertIn("idx_round_log_dev_round", names)
        self.assertIn("idx_round_log_dev_time", names)


class SensorCrudTest(TempDbTest):
    def test_insert_and_latest(self):
        self.db.insert_sensor(SensorData(
            gateway_id="GW_001", node_id="N1", timestamp=10, temperature=20.0, co2=400))
        self.db.insert_sensor(SensorData(
            gateway_id="GW_001", node_id="N1", timestamp=20, temperature=21.0, co2=420))

        latest = self.db.get_latest_sensor("GW_001", "N1")
        self.assertEqual(latest["timestamp"], 20)
        self.assertEqual(latest["temperature"], 21.0)

        history = self.db.query_sensor_history("GW_001", "N1", limit=10)
        self.assertEqual([row["timestamp"] for row in history], [10, 20])

        self.assertEqual(self.db.count_sensors("GW_001", "N1"), 2)
        self.assertEqual(self.db.count_sensor_rows("GW_001", "N1", start_ts=15), 1)
        self.assertEqual(self.db.count_sensor_rows("GW_001", "N1", end_ts=15), 1)
        self.assertEqual(self.db.count_sensors("GW_002", "N1"), 0)

        devices = self.db.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["gateway_id"], "GW_001")
        self.assertEqual(devices[0]["last_seen"], 20)


class SensorRoundTest(TempDbTest):
    """扫频帧带的环境量按轮折叠：一轮一条，而不是 50 条一样的重复行。"""

    def _sensor(self, **kw) -> SensorData:
        row = {
            "gateway_id": "GW_001", "node_id": "N1", "timestamp": 1000,
            "soil_moisture": 50.0, "temperature": 24.0, "nh3": 10.0,
            "h2s": 5.0, "co2": 450.0, "ph": 6.5, "humidity": 65.0,
        }
        row.update(kw)
        return SensorData(**row)

    def test_same_round_folds_to_one_row(self):
        """同轮的分段到达、整轮补齐、QoS1 重投都只留一条。"""
        self.assertTrue(self.db.insert_sensor(self._sensor(), round_id=1))
        self.assertFalse(self.db.insert_sensor(self._sensor(), round_id=1))
        self.assertFalse(self.db.insert_sensor(self._sensor(), round_id=1))
        self.assertEqual(self.db.count_sensors("GW_001", "N1"), 1)

    def test_different_rounds_keep_their_own_rows(self):
        self.db.insert_sensor(self._sensor(timestamp=1000), round_id=1)
        self.db.insert_sensor(self._sensor(timestamp=2000), round_id=2)
        self.assertEqual(self.db.count_sensors("GW_001", "N1"), 2)
        self.assertEqual([row["round_id"] for row in
                          self.db.query_sensor_history("GW_001", "N1")], [1, 2])

    def test_different_nodes_keep_their_own_rows(self):
        self.db.insert_sensor(self._sensor(), round_id=1)
        self.db.insert_sensor(self._sensor(node_id="N2"), round_id=1)
        self.assertEqual(self.db.count_sensors("GW_001", "N1"), 1)
        self.assertEqual(self.db.count_sensors("GW_001", "N2"), 1)

    def test_refresh_does_not_overwrite_with_nulls(self):
        """后到的段如果缺了某些环境量，不能把已有的实测值刷成空。"""
        self.db.insert_sensor(self._sensor(), round_id=1)
        self.db.insert_sensor(self._sensor(temperature=None, co2=None), round_id=1)
        row = self.db.query_sensor_history("GW_001", "N1")[0]
        self.assertEqual(row["temperature"], 24.0)
        self.assertEqual(row["co2"], 450.0)

    def test_refresh_updates_the_timestamp(self):
        self.db.insert_sensor(self._sensor(timestamp=1000), round_id=1)
        self.db.insert_sensor(self._sensor(timestamp=1060), round_id=1)
        row = self.db.query_sensor_history("GW_001", "N1")[0]
        self.assertEqual((row["timestamp"], row["round_id"]), (1060, 1))

    def test_round_id_takes_the_argument_over_the_payload(self):
        self.db.insert_sensor(self._sensor(round_id=99), round_id=1)
        self.assertEqual(self.db.query_sensor_history("GW_001", "N1")[0]["round_id"], 1)

    def test_without_round_id_still_appends(self):
        """不带轮次号的普通 sensor 帧保持追加写入，原有行为不变。"""
        self.db.insert_sensor(self._sensor(timestamp=10), round_id=None)
        self.db.insert_sensor(self._sensor(timestamp=20), round_id=None)
        self.assertEqual(self.db.count_sensors("GW_001", "N1"), 2)
        self.assertTrue(all(row["round_id"] is None for row in
                            self.db.query_sensor_history("GW_001", "N1")))

    def test_list_devices_falls_back_to_impedance_data(self):
        """只看 sensor_data 会让只上报阻抗的设备从下拉框里消失。"""
        self.db.insert_impedance(ImpedanceData(
            gateway_id="GW_002", node_id="N9", timestamp=500, frequency_hz=1000.0,
            z_real=1200.0, z_imag=-300.0, magnitude=1236.93))
        devices = self.db.list_devices()
        self.assertIn(("GW_002", "N9"), {(d["gateway_id"], d["node_id"]) for d in devices})

    def test_list_devices_skips_rows_without_a_device_id(self):
        """老库里有 gateway_id / node_id 为空的残缺上报，下拉框里不列。"""
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO sensor_data (gateway_id, node_id, timestamp) VALUES "
                "(?,?,10), ('', '', 11), ('GW_001', '', 12)",
                ("GW_001", "N1"))
            self.db._conn.execute(
                "INSERT INTO impedance_data (gateway_id, node_id, timestamp, "
                "frequency_hz) VALUES ('GW_002', '', 13, 1000.0), "
                "('', 'N7', 14, 1000.0)")
            self.db._conn.commit()
        devices = {(d["gateway_id"], d["node_id"]) for d in self.db.list_devices()}
        self.assertEqual(devices, {("GW_001", "N1")})

    def test_sensor_round_index_created(self):
        with self.db._lock:
            names = {row[0] for row in self.db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='sensor_data'"
            ).fetchall()}
        self.assertIn("idx_sensor_dev_round", names)


class SensorImpedanceColumnsTest(TempDbTest):
    """环境行跟着带阻抗列：历史窗口里环境量和阻抗一行看全。"""

    def _sensor(self, **kw) -> SensorData:
        row = {
            "gateway_id": "GW_001", "node_id": "N1", "timestamp": 1000,
            "soil_moisture": 50.0, "temperature": 24.0, "nh3": 10.0,
            "h2s": 5.0, "co2": 450.0, "ph": 6.5, "humidity": 65.0,
        }
        row.update(kw)
        return SensorData(**row)

    @staticmethod
    def _imp(**kw) -> ImpedanceData:
        row = {"gateway_id": "GW_001", "node_id": "N1", "timestamp": 1000,
               "frequency_hz": None, "z_real": 1200.0, "z_imag": -300.0,
               "magnitude": 1236.93, "phase": 14.04}
        row.update(kw)
        return ImpedanceData(**row)

    def test_impedance_columns_exist(self):
        with self.db._lock:
            cols = {row["name"] for row in self.db._conn.execute(
                "PRAGMA table_info(sensor_data)")}
        self.assertGreaterEqual(
            cols, {"frequency_hz", "z_real", "z_imag", "magnitude", "phase"})

    def test_insert_writes_the_impedance_columns(self):
        self.db.insert_sensor(self._sensor(), impedance=self._imp())
        row = self.db.query_sensor_history("GW_001", "N1")[0]
        self.assertEqual((row["frequency_hz"], row["z_real"], row["z_imag"],
                          row["magnitude"], row["phase"]),
                         (None, 1200.0, -300.0, 1236.93, 14.04))
        # 环境量不受影响
        self.assertEqual((row["temperature"], row["co2"]), (24.0, 450.0))

    def test_insert_without_impedance_leaves_the_columns_null(self):
        self.db.insert_sensor(self._sensor())
        row = self.db.query_sensor_history("GW_001", "N1")[0]
        for col in ("frequency_hz", "z_real", "z_imag", "magnitude", "phase"):
            self.assertIsNone(row[col], col)

    def test_fold_keeps_the_impedance_of_the_existing_row(self):
        """后到的段没带阻抗时，不能把已有实测值刷成空。"""
        self.db.insert_sensor(self._sensor(), round_id=1, impedance=self._imp())
        self.db.insert_sensor(self._sensor(), round_id=1)
        row = self.db.query_sensor_history("GW_001", "N1")[0]
        self.assertEqual(row["magnitude"], 1236.93)

    def test_fold_fills_in_impedance_when_the_first_row_had_none(self):
        self.db.insert_sensor(self._sensor(), round_id=1)
        self.db.insert_sensor(self._sensor(), round_id=1, impedance=self._imp())
        row = self.db.query_sensor_history("GW_001", "N1")[0]
        self.assertEqual((row["z_real"], row["z_imag"]), (1200.0, -300.0))

    def test_attach_fills_only_rows_without_impedance(self):
        self.db.insert_sensor(self._sensor(), round_id=1)
        self.db.insert_sensor(self._sensor(), round_id=2, impedance=self._imp())
        self.assertTrue(self.db.attach_sensor_impedance(
            "GW_001", "N1", 1, self._imp(z_real=999.0)))
        rows = {row["round_id"]: row for row in
                self.db.query_sensor_history("GW_001", "N1")}
        self.assertEqual(rows[1]["z_real"], 999.0)
        # round 2 已经有值，普通补录不动它
        self.assertEqual(rows[2]["z_real"], 1200.0)

    def test_attach_force_overwrites(self):
        """整轮收齐后用更准的均值覆盖分段时的半成品。"""
        self.db.insert_sensor(self._sensor(), round_id=1,
                              impedance=self._imp(z_real=1.0, magnitude=1.0))
        self.assertTrue(self.db.attach_sensor_impedance(
            "GW_001", "N1", 1, self._imp(z_real=1200.0), force=True))
        self.assertEqual(
            self.db.query_sensor_history("GW_001", "N1")[0]["z_real"], 1200.0)

    def test_attach_without_a_matching_row_creates_nothing(self):
        """环境行不存在时不新建：环境表一行代表一次环境快照。"""
        self.assertFalse(self.db.attach_sensor_impedance(
            "GW_001", "N1", 42, self._imp()))
        self.assertEqual(self.db.count_sensors("GW_001", "N1"), 0)

    def test_attach_ignores_empty_impedance(self):
        self.db.insert_sensor(self._sensor(), round_id=1)
        self.assertFalse(self.db.attach_sensor_impedance(
            "GW_001", "N1", 1, self._imp(z_real=None, z_imag=None, magnitude=None)))
        self.assertIsNone(self.db.query_sensor_history("GW_001", "N1")[0]["magnitude"])

    def test_attach_with_no_round_id_is_a_noop(self):
        self.db.insert_sensor(self._sensor(timestamp=10))
        self.db.insert_sensor(self._sensor(timestamp=20))
        self.assertFalse(self.db.attach_sensor_impedance(
            "GW_001", "N1", None, self._imp()))
        self.assertEqual(self.db.count_sensors("GW_001", "N1"), 2)
        self.assertTrue(all(row["magnitude"] is None for row in
                            self.db.query_sensor_history("GW_001", "N1")))

    def test_attach_by_time_fills_every_row_in_the_epoch(self):
        """一段阻抗覆盖整段时间：段内每一帧环境量都属于这次测量。"""
        self.db.insert_sensor(self._sensor(timestamp=1000))
        self.db.insert_sensor(self._sensor(timestamp=1002))
        self.db.insert_sensor(self._sensor(timestamp=1004))
        filled = self.db.attach_sensor_impedance_by_time(
            "GW_001", "N1", self._imp(), 1000, 1004)
        self.assertEqual(filled, 3)
        rows = self.db.query_sensor_history("GW_001", "N1")
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["z_real"], 1200.0)

    def test_attach_by_time_skips_rows_that_already_have_impedance(self):
        """别把 sweep 轮次折叠出来的那行覆盖成整谱均值。"""
        self.db.insert_sensor(self._sensor(timestamp=1000))
        self.db.insert_sensor(self._sensor(timestamp=1002, round_id=7),
                              impedance=self._imp(z_real=111.0, magnitude=111.0))
        filled = self.db.attach_sensor_impedance_by_time(
            "GW_001", "N1", self._imp(), 1000, 1002)
        self.assertEqual(filled, 1)
        rows = {row["timestamp"]: row for row in self.db.query_sensor_history("GW_001", "N1")}
        self.assertEqual(rows[1002]["z_real"], 111.0)
        self.assertEqual(rows[1000]["z_real"], 1200.0)

    def test_attach_by_time_ignores_rows_outside_the_window(self):
        self.db.insert_sensor(self._sensor(timestamp=990))
        self.db.insert_sensor(self._sensor(timestamp=1000))
        self.db.insert_sensor(self._sensor(timestamp=1010))
        filled = self.db.attach_sensor_impedance_by_time(
            "GW_001", "N1", self._imp(), 1000, 1000)
        self.assertEqual(filled, 1)
        rows = {row["timestamp"]: row for row in self.db.query_sensor_history("GW_001", "N1")}
        self.assertIsNone(rows[990]["magnitude"])
        self.assertEqual(rows[1000]["z_real"], 1200.0)
        self.assertIsNone(rows[1010]["magnitude"])

    def test_attach_by_time_returns_zero_when_nothing_is_in_range(self):
        """窗里没有环境行就不凭空造一条没有环境量的行。"""
        self.assertEqual(self.db.attach_sensor_impedance_by_time(
            "GW_001", "N1", self._imp(), 500, 600), 0)
        self.assertEqual(self.db.count_sensors("GW_001", "N1"), 0)

    def test_attach_by_time_ignores_other_nodes(self):
        self.db.insert_sensor(self._sensor(timestamp=1000, node_id="N2"))
        self.assertEqual(self.db.attach_sensor_impedance_by_time(
            "GW_001", "N1", self._imp(), 1000, 1000), 0)
        self.assertIsNone(self.db.query_sensor_history("GW_001", "N2")[0]["magnitude"])

    def test_attach_by_time_force_overwrites(self):
        self.db.insert_sensor(self._sensor(timestamp=1000),
                              impedance=self._imp(z_real=1.0, magnitude=1.0))
        self.assertEqual(self.db.attach_sensor_impedance_by_time(
            "GW_001", "N1", self._imp(), 1000, 1000, force=True), 1)
        self.assertEqual(self.db.query_sensor_history("GW_001", "N1")[0]["z_real"], 1200.0)

    def test_attach_by_time_ignores_empty_impedance(self):
        self.db.insert_sensor(self._sensor(timestamp=1000))
        self.assertEqual(self.db.attach_sensor_impedance_by_time(
            "GW_001", "N1",
            self._imp(z_real=None, z_imag=None, magnitude=None), 1000, 1000), 0)
        self.assertIsNone(self.db.query_sensor_history("GW_001", "N1")[0]["magnitude"])

    def test_migration_adds_the_missing_impedance_columns(self):
        """老库只有前四列，phase 是后加的，迁移得补齐。"""
        self.db.close()
        legacy = Path(self._tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        try:
            conn.executescript("""
                CREATE TABLE sensor_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gateway_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    round_id INTEGER,
                    temperature REAL, humidity REAL, soil_moisture REAL,
                    nh3 REAL, h2s REAL, co2 REAL, ph REAL,
                    frequency_hz REAL, z_real REAL, z_imag REAL, magnitude REAL
                );
                INSERT INTO sensor_data
                    (gateway_id, node_id, timestamp, magnitude)
                VALUES ('GW_001', 'N1', 1000, 9908.0);
            """)
        finally:
            conn.close()
        self.db = Database(legacy)
        with self.db._lock:
            cols = {row["name"] for row in self.db._conn.execute(
                "PRAGMA table_info(sensor_data)")}
        self.assertIn("phase", cols)
        row = self.db.query_sensor_history("GW_001", "N1")[0]
        # 已有数据不丢
        self.assertEqual(row["magnitude"], 9908.0)
        self.assertIsNone(row["phase"])


class SweepTest(TempDbTest):
    def test_dedup_within_batch(self):
        points = [make_sweep(0), make_sweep(1), make_sweep(1), make_sweep(2)]
        self.assertEqual(self.db.insert_sweep_points(points), 3)
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 3)

    def test_dedup_across_batches_same_round(self):
        # 分段上报 / 整谱补齐后重复上报同一个点序号，库里只应保留一行。
        self.db.insert_sweep_points([make_sweep(0), make_sweep(1), make_sweep(2)])
        self.assertEqual(self.db.insert_sweep_points([
            make_sweep(1), make_sweep(2), make_sweep(3),
        ]), 1)
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 4)
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 1)
        self.assertEqual([row["point_index"] for row in rows], [0, 1, 2, 3])

    def test_same_point_index_different_rounds_kept(self):
        # 不同轮次天然有不同的 freq→index 对应，不能跨轮去重。
        self.db.insert_sweep_points([make_sweep(0, round_id=1, frequency=1000.0)])
        self.db.insert_sweep_points([make_sweep(0, round_id=2, frequency=2000.0)])
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 2)
        self.assertEqual(
            [row["frequency_hz"] for row in self.db.query_sweep_round(
                "GW_001", "LORA_NODE_01", 2)], [2000.0])

    def test_dedup_rejects_bad_indices(self):
        self.assertEqual(self.db.insert_sweep_points([
            make_sweep(None), make_sweep("x"), make_sweep(5),
        ]), 1)

    def test_dedup_mixed_rounds_in_one_batch(self):
        """同批里混不同轮次，各自只跟自己的轮次比对。"""
        self.db.insert_sweep_points([make_sweep(0, round_id=1), make_sweep(0, round_id=2)])
        self.assertEqual(self.db.insert_sweep_points([
            make_sweep(0, round_id=1),
            make_sweep(1, round_id=1),
            make_sweep(1, round_id=2),
        ]), 2)
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 4)

    def test_dedup_drops_rows_without_round_id(self):
        """round_id 是 NOT NULL，给不出轮次号的点不能整批炸掉插入。"""
        self.assertEqual(self.db.insert_sweep_points([
            make_sweep(0, round_id=None), make_sweep(1, round_id=1),
        ]), 1)
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 1)

    def test_dedup_only_reads_the_rounds_in_the_batch(self):
        """库里有大量历史轮次时，新批只按本批轮次比对。

        不加分轮次的话每次都要把整个 sweep_data 捞进内存，长跑起来
        内存和查询都会跟着行数一起涨。
        """
        self.db.insert_sweep_points([make_sweep(i, round_id=1000 + i)
                                     for i in range(300)])
        self.assertEqual(self.db.insert_sweep_points([
            make_sweep(0, round_id=1), make_sweep(0, round_id=1),
        ]), 1)
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 301)

    def test_empty_batch_is_noop(self):
        self.assertEqual(self.db.insert_sweep_points([]), 0)
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01"), 0)

    def test_query_round_orders_by_point_index(self):
        self.db.insert_sweep_points([
            make_sweep(3, frequency=4000.0),
            make_sweep(1, frequency=2000.0),
            make_sweep(0, frequency=1000.0),
            make_sweep(2, frequency=3000.0),
        ])
        rows = self.db.query_sweep_round("GW_001", "LORA_NODE_01", 1)
        self.assertEqual([row["point_index"] for row in rows], [0, 1, 2, 3])
        self.assertEqual(
            [row["frequency_hz"] for row in rows], [1000.0, 2000.0, 3000.0, 4000.0])

    def test_list_rounds_aggregates(self):
        self.db.insert_sweep_points([
            make_sweep(0, round_id=1, timestamp=100, frequency=1000.0),
            make_sweep(1, round_id=1, timestamp=100, frequency=2000.0),
            make_sweep(0, round_id=2, timestamp=200, frequency=1000.0),
        ])
        rounds = self.db.list_sweep_rounds("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[0]["round_id"], 2)
        self.assertEqual(rounds[0]["points"], 1)
        self.assertEqual(rounds[1]["round_id"], 1)
        self.assertEqual(rounds[1]["points"], 2)
        self.assertEqual(rounds[1]["timestamp"], 100)

    def test_query_history_filtering(self):
        self.db.insert_sweep_points([
            make_sweep(0, round_id=1, timestamp=100),
            make_sweep(0, round_id=2, timestamp=200),
            make_sweep(0, round_id=3, timestamp=300),
        ])
        self.assertEqual(len(self.db.query_sweep_history("GW_001", "LORA_NODE_01", limit=2)), 2)
        self.assertEqual(
            len(self.db.query_sweep_history("GW_001", "LORA_NODE_01", limit=10, offset=2)), 1)
        self.assertEqual(len(self.db.query_sweep_history("GW_001", "LORA_NODE_01", start_ts=200)), 2)
        self.assertEqual(len(self.db.query_sweep_history("GW_001", "LORA_NODE_01", end_ts=200)), 2)
        self.assertEqual(self.db.count_sweep_rows("GW_001", "LORA_NODE_01", round_id=2), 1)


class RoundLogTest(TempDbTest):
    """轮次完整性记录：一轮一条，事后能答"哪几轮不完整、各缺多少"。"""

    def _log_round(self, round_id=1, points=100, total=100, **kw):
        row = {
            "gateway_id": "GW_001",
            "node_id": "LORA_NODE_01",
            "round_id": round_id,
            "points": points,
            "total_points": total,
            "first_ts": 1700000000,
            "last_ts": 1700000060,
        }
        row.update(kw)
        self.db.record_round(**row)

    def test_complete_round_records_one_row(self):
        self._log_round(1, 100, 100)
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["points"], 100)
        self.assertEqual(rows[0]["total_points"], 100)
        self.assertEqual(rows[0]["complete"], 1)

    def test_partial_round_is_marked_incomplete(self):
        self._log_round(2, 50, 100)
        self.assertEqual(self.db.list_round_log("GW_001", "LORA_NODE_01")[0]["complete"], 0)
        self.assertEqual(self.db.count_incomplete_rounds("GW_001", "LORA_NODE_01"), 1)

    def test_only_incomplete_filter(self):
        self._log_round(1, 100, 100)
        self._log_round(2, 50, 100)
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01", only_incomplete=True)
        self.assertEqual([row["round_id"] for row in rows], [2])

    def test_upsert_folds_duplicate_flushes_to_one_row(self):
        """静默窗口强刷一次、整轮摘要再补一次，仍然只有这一轮一条。"""
        self._log_round(3, 50, 100)
        self._log_round(3, 50, 100)
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)

    def test_first_timestamp_wins_on_upsert(self):
        """重复刷轮时 first_ts 留最早一次、last_ts 取最晚一次。"""
        self._log_round(4, 50, 100, first_ts=1000, last_ts=1060)
        self._log_round(4, 50, 100, first_ts=9000, last_ts=9060)
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual((row["first_ts"], row["last_ts"]), (1000, 9060))

    def test_late_flush_grows_points_and_flips_complete(self):
        """先超时刷出半轮，迟到的段到了再刷一次：判定要能跟上翻成完整。

        点数只增不减，不能被早先那次不完整判定把后来补齐的结果锁死。
        """
        self._log_round(40, 50, 100, first_ts=1000, last_ts=1060)
        self.assertEqual(self.db.list_round_log("GW_001", "LORA_NODE_01")[0]["complete"], 0)
        self._log_round(40, 100, 100, first_ts=1000, last_ts=1600)
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual(len(self.db.list_round_log("GW_001", "LORA_NODE_01")), 1)
        self.assertEqual(row["points"], 100)
        self.assertEqual(row["complete"], 1)
        self.assertEqual(row["last_ts"], 1600)

    def test_fewer_points_never_shrink_the_verdict(self):
        """重投报文带来的那次更少点数，不能把已补齐的轮次改回不完整。"""
        self._log_round(41, 100, 100)
        self._log_round(41, 50, 100)
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual((row["points"], row["complete"]), (100, 1))

    def test_round_done_fills_retry_without_overwriting_local_points(self):
        """固件整轮摘要只补 retry / imp_mean，不动本地实际落库的点数。"""
        self._log_round(5, 50, 100)
        self.db.record_round_done("GW_001", "LORA_NODE_01", 5,
                                  total_points=100, retry=2, imp_mean=1234.5)
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual(row["points"], 50)
        self.assertEqual(row["total_points"], 100)
        self.assertEqual(row["retry"], 2)
        self.assertEqual(row["imp_mean"], 1234.5)
        self.assertEqual(row["complete"], 0)

    def test_round_done_alone_leaves_a_row_with_no_local_count(self):
        """整轮摘要先到、本地还没落这一轮：留行但不假装有本地点数。

        固件说"这轮 100 点"不能写成我们的落库结果，否则这轮就再也没
        法看出本地其实一个点都没收到。
        """
        self.db.record_round_done("GW_001", "LORA_NODE_01", 6,
                                  total_points=100, retry=0)
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_points"], 100)
        self.assertIsNone(rows[0]["points"])
        self.assertIsNone(rows[0]["complete"])
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 0)

    def test_local_flush_after_round_done_records_local_truth(self):
        """摘要先到把行建起来了，之后本地刷 50 点也得如实记 50、不完整。"""
        self.db.record_round_done("GW_001", "LORA_NODE_01", 8, total_points=100)
        self._log_round(8, 50, 100)
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual((row["points"], row["complete"]), (50, 0))
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 1)

    def test_retry_is_filled_when_only_the_summary_arrived(self):
        self.db.record_round_done("GW_001", "LORA_NODE_01", 7, retry=1)
        row = self.db.list_round_log("GW_001", "LORA_NODE_01")[0]
        self.assertEqual(row["retry"], 1)
        self.assertIsNone(row["points"])
        self.assertEqual(self.db.list_round_log(
            "GW_001", "LORA_NODE_01", only_incomplete=True)[0]["round_id"], 7)

    def test_falls_back_to_scan_id_without_round_id(self):
        """老报文没有轮次号，退回按 scan_id 折叠。"""
        self.db.record_round("GW_001", "LORA_NODE_01", None,
                             scan_id="SCAN_99", points=100, total_points=100)
        self.db.record_round("GW_001", "LORA_NODE_01", None,
                             scan_id="SCAN_99", points=100, total_points=100)
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["round_id"])

    def test_nodes_and_rounds_are_independent(self):
        self._log_round(1, 100, 100, node_id="LORA_NODE_02")
        self._log_round(1, 50, 100)
        self.assertEqual(len(self.db.list_round_log("GW_001", "LORA_NODE_01")), 1)
        self.assertEqual(len(self.db.list_round_log("GW_001", "LORA_NODE_02")), 1)
        self.assertEqual(self.db.count_incomplete_rounds("GW_001"), 1)

    def test_list_orders_newest_first(self):
        self._log_round(1, 100, 100)
        self._log_round(2, 100, 100)
        self._log_round(3, 100, 100)
        rows = self.db.list_round_log("GW_001", "LORA_NODE_01")
        self.assertEqual([row["round_id"] for row in rows], [3, 2, 1])


class ImpedanceAndPredictionTest(TempDbTest):
    def test_impedance_history(self):
        self.db.insert_impedance(ImpedanceData(
            gateway_id="GW_001", node_id="N1", timestamp=10, scan_id="S1",
            frequency_hz=1000, z_real=1200, z_imag=-300, magnitude=1236.9, phase=-14.0,
            rcal_ohm=1200.0, in_valid_window=True))
        self.db.insert_impedance(ImpedanceData(
            gateway_id="GW_001", node_id="N1", timestamp=20, scan_id="S2",
            frequency_hz=2000, z_real=1100, z_imag=-250))

        rows = self.db.query_impedance_history("GW_001", "N1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["timestamp"], 20)
        self.assertEqual(len(self.db.query_impedance_history("GW_001", "N1", scan_id="S1")), 1)
        self.assertEqual(self.db.count_impedance_rows("GW_001", "N1"), 2)
        self.assertEqual(self.db.count_impedance_rows("GW_001", "N1", start_ts=15), 1)

    def test_status_history(self):
        self.db.insert_status(DeviceStatus(
            gateway_id="GW_001", node_id="N1", status="online", timestamp=10))
        self.db.insert_status(DeviceStatus(
            gateway_id="GW_001", node_id="N2", status="offline", timestamp=20))
        rows = self.db.query_status_history("GW_001", limit=50)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["timestamp"], 20)

    def test_prediction_roundtrip(self):
        self.assertIsNone(self.db.query_latest_prediction("N1"))
        self.db.insert_prediction(PredictionData(
            node_id="N1", timestamp=10, maturity=0.4, maturity_level="ripening",
            harvest_date="2026-10-01", confidence=0.7))
        self.db.insert_prediction(PredictionData(
            node_id="N2", timestamp=20, maturity=0.9, maturity_level="ripe",
            harvest_date="2026-09-25", confidence=0.8))
        self.assertEqual(self.db.query_latest_prediction("N1")["maturity"], 0.4)
        self.assertEqual(self.db.query_latest_prediction()["timestamp"], 20)

    def test_prediction_dedup_identical_duplicate(self):
        # 同一轮走多条路径产出（分段到达 / 整谱补齐 / 模拟器直发）时，
        # 完全相同的预测只能留一条。
        row = PredictionData(
            node_id="N1", timestamp=10, maturity=0.4, maturity_level="ripening",
            harvest_date="2026-10-01", confidence=0.7)
        self.db.insert_prediction(row)
        self.db.insert_prediction(row)
        self.db.insert_prediction(row)
        self.assertEqual(self.db.count_predictions("N1"), 1)
        self.assertEqual(self.db.query_latest_prediction("N1")["maturity"], 0.4)

    def test_prediction_keeps_distinct_same_second_rounds(self):
        # 同一秒内落了两轮不同的上报，数值不同，两条都得留下。
        self.db.insert_prediction(PredictionData(
            node_id="N1", timestamp=10, maturity=0.4, maturity_level="ripening",
            harvest_date="2026-10-01", confidence=0.7))
        self.db.insert_prediction(PredictionData(
            node_id="N1", timestamp=10, maturity=0.6, maturity_level="near_ripe",
            harvest_date="2026-10-05", confidence=0.8))
        self.assertEqual(self.db.count_predictions("N1"), 2)

    def test_prediction_dedup_by_round_id_ignores_timestamp(self):
        # 网关一轮 100 点 2 段上报，可能落在不同秒；按轮次号去重才是准的。
        base = dict(node_id="N1", maturity=0.4, maturity_level="ripening",
                    harvest_date="2026-10-01", confidence=0.7, round_id=42)
        self.db.insert_prediction(PredictionData(timestamp=10, **base))
        self.db.insert_prediction(PredictionData(timestamp=11, **base))
        self.db.insert_prediction(PredictionData(timestamp=12, **base))
        self.assertEqual(self.db.count_predictions("N1"), 1)

    def test_prediction_same_round_id_differs_is_kept(self):
        self.db.insert_prediction(PredictionData(
            node_id="N1", round_id=42, timestamp=10, maturity=0.4,
            maturity_level="ripening", harvest_date="2026-10-01", confidence=0.7))
        self.db.insert_prediction(PredictionData(
            node_id="N1", round_id=42, timestamp=11, maturity=0.9,
            maturity_level="ripe", harvest_date="2026-10-08", confidence=0.9))
        self.assertEqual(self.db.count_predictions("N1"), 2)

    def test_prediction_upsert_allows_other_nodes(self):
        self.db.insert_prediction(PredictionData(
            node_id="N1", timestamp=10, maturity=0.4, maturity_level="ripening",
            harvest_date="2026-10-01", confidence=0.7))
        self.db.insert_prediction(PredictionData(
            node_id="N2", timestamp=10, maturity=0.5, maturity_level="ripening",
            harvest_date="2026-10-02", confidence=0.7))
        self.assertEqual(self.db.count_predictions(), 2)

    def test_prediction_delete_drops_nulls(self):
        self.db._conn.execute(
            "INSERT INTO prediction(node_id, timestamp, maturity) VALUES (NULL, 0, 0)")
        self.db.insert_prediction(PredictionData(
            node_id="N1", timestamp=10, maturity=0.4, maturity_level="ripening",
            harvest_date="2026-10-01", confidence=0.7))
        self.assertEqual(self.db.delete_predictions_for([]), 0)
        self.assertEqual(self.db.count_predictions("N1"), 1)
        self.assertEqual(self.db.count_predictions(), 2)
        self.assertEqual(self.db.delete_predictions_for(["N1"]), 1)
        self.assertEqual(self.db.count_predictions("N1"), 0)
        self.assertEqual(self.db.count_predictions(), 1)

    def test_status_rejects_blank_status(self):
        self.db.insert_status(DeviceStatus(gateway_id="GW_001", node_id="N1",
                                           status="", timestamp=10))
        self.db.insert_status(DeviceStatus(gateway_id="GW_001", node_id="N1",
                                           status=None, timestamp=11))
        self.db.insert_status(DeviceStatus(gateway_id="GW_001", node_id="N1",
                                           status="unknown", timestamp=12))
        self.assertEqual(self.db.count_status_rows("GW_001"), 0)

    def test_status_keeps_known_status(self):
        self.db.insert_status(DeviceStatus(
            gateway_id="GW_001", node_id="N1", status="online", timestamp=10))
        self.assertEqual(self.db.count_status_rows("GW_001"), 1)
        self.assertEqual(self.db.count_status_rows("GW_001", "N1"), 1)
        self.assertEqual(self.db.count_status_rows(gateway_id="GW_002"), 0)


class SweepImpedanceBackfillTest(TempDbTest):
    """把 sweep_data 里有、阻抗历史里没有的频点补齐。"""

    @staticmethod
    def _pt(index: int, **kw) -> SweepPointData:
        # 一轮里每个点的频率都不同，point_index 才和 frequency_hz 一一对应。
        return make_sweep(index, frequency=1000.0 + index * 100.0, **kw)

    def test_missing_sweep_points_land_in_impedance_history(self):
        self.db.insert_sweep_points([self._pt(i) for i in range(3)])
        stats = Database.backfill_impedance_from_sweep(self.db._conn)
        self.assertEqual(stats, {"inserted": 3, "rounds": 1})

        rows = self.db.query_impedance_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["z_real"], 1200.0)
            self.assertEqual(row["z_imag"], -300.0)
            self.assertAlmostEqual(row["magnitude"], 1236.93, places=2)
            # 库内约定：phase = -atan2(z_imag, z_real)，单位是度
            self.assertAlmostEqual(row["phase"], 14.0362618, places=3)
            self.assertEqual(row["rcal_ohm"], 51000.0)
            self.assertEqual(row["in_valid_window"], 1)

    def test_phase_is_in_degrees(self):
        """度换算少写 4 倍会把 14.04 度算成 56.14 度，得守住。"""
        self.db.insert_sweep_points([make_sweep(0)])
        Database.backfill_impedance_from_sweep(self.db._conn)
        phase = self.db.query_impedance_history("GW_001", "LORA_NODE_01")[0]["phase"]
        self.assertAlmostEqual(phase, -math.degrees(math.atan2(-300.0, 1200.0)),
                               places=5)
        self.assertLess(abs(phase), 180.0)

    def test_window_flags_follow_the_legacy_band(self):
        self.db.insert_sweep_points([
            make_sweep(0, frequency=100.0),
            make_sweep(1, frequency=1000.0),
            make_sweep(2, frequency=30000.0),
            make_sweep(3, frequency=40000.0),
        ])
        Database.backfill_impedance_from_sweep(self.db._conn)
        window = {row["frequency_hz"]: row["in_valid_window"]
                  for row in self.db.query_impedance_history("GW_001", "LORA_NODE_01")}
        self.assertEqual(window, {100.0: 0, 1000.0: 1, 30000.0: 1, 40000.0: 0})

    def test_second_run_is_a_noop(self):
        self.db.insert_sweep_points([self._pt(0), self._pt(1)])
        Database.backfill_impedance_from_sweep(self.db._conn)
        self.assertEqual(
            Database.backfill_impedance_from_sweep(self.db._conn),
            {"inserted": 0, "rounds": 0})
        self.assertEqual(self.db.count_impedance_rows("GW_001", "LORA_NODE_01"), 2)

    def test_existing_impedance_rows_are_never_touched(self):
        """同键已有行时保持原样，绝不拿 sweep_data 的值覆盖。"""
        self.db.insert_impedance(ImpedanceData(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000000,
            scan_id="SCAN_1_1700000000", frequency_hz=1000.0,
            z_real=999.0, z_imag=1.0, magnitude=1000.0, phase=0.5))
        self.db.insert_sweep_points([make_sweep(0)])
        self.assertEqual(
            Database.backfill_impedance_from_sweep(self.db._conn)["inserted"], 0)
        row = self.db.query_impedance_history("GW_001", "LORA_NODE_01")[0]
        self.assertEqual(row["z_real"], 999.0)
        self.assertEqual(row["z_imag"], 1.0)
        self.assertEqual(row["phase"], 0.5)

    def test_reuses_the_scan_id_already_on_the_round(self):
        """一轮已有几个点时，补的点必须和它们落进同一个 scan_id。"""
        self.db.insert_impedance(ImpedanceData(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000000,
            scan_id="SCAN_7_1700000000", frequency_hz=1000.0,
            z_real=1200.0, z_imag=-300.0))
        self.db.insert_sweep_points([
            make_sweep(0, round_id=7),
            make_sweep(1, round_id=7, frequency=2000.0),
        ])
        self.assertEqual(
            Database.backfill_impedance_from_sweep(self.db._conn)["inserted"], 1)
        scans = {row["scan_id"]
                 for row in self.db.query_impedance_history("GW_001", "LORA_NODE_01")}
        self.assertEqual(scans, {"SCAN_7_1700000000"})

    def test_mints_a_scan_id_for_a_round_with_no_rows(self):
        """整轮一条都没有时按 SCAN_<轮次>_<起始秒> 造一个，一轮只落一个。"""
        self.db.insert_sweep_points([
            make_sweep(0, round_id=7),
            make_sweep(1, round_id=7, frequency=2000.0, timestamp=1700000005),
        ])
        Database.backfill_impedance_from_sweep(self.db._conn)
        rows = self.db.query_impedance_history("GW_001", "LORA_NODE_01")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["scan_id"] for row in rows}, {"SCAN_7_1700000000"})

    def test_complete_rounds_are_left_alone(self):
        """只补缺失的轮，已经有谱的轮不要重复插。"""
        self.db.insert_impedance(ImpedanceData(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000000,
            scan_id="SCAN_5_1700000000", frequency_hz=1000.0,
            z_real=1.0, z_imag=2.0))
        self.db.insert_sweep_points([
            make_sweep(0, round_id=5),
            make_sweep(0, round_id=9, frequency=5000.0),
        ])
        self.assertEqual(
            Database.backfill_impedance_from_sweep(self.db._conn),
            {"inserted": 1, "rounds": 1})
        self.assertEqual(self.db.count_impedance_rows("GW_001", "LORA_NODE_01"), 2)

    def test_empty_database_is_a_noop(self):
        self.assertEqual(
            Database.backfill_impedance_from_sweep(self.db._conn),
            {"inserted": 0, "rounds": 0})

    def test_round_with_duplicated_sweep_rows_fills_once(self):
        """sweep_data 同键写过多次（分段重投 / 重发）时，只补一条。"""
        self.db.insert_sweep_points([
            self._pt(0), self._pt(1), self._pt(2), self._pt(2),
            self._pt(3), self._pt(3), self._pt(3),
        ])
        self.assertEqual(
            Database.backfill_impedance_from_sweep(self.db._conn)["inserted"], 4)
        self.assertEqual(self.db.count_impedance_rows("GW_001", "LORA_NODE_01"), 4)


class SensorImpedanceBackfillTest(TempDbTest):
    """老固件把阻抗塞在环境帧里上报，这些值要从环境表里拆出来。"""

    @staticmethod
    def _pt(index: int, **kw) -> SweepPointData:
        return make_sweep(index, frequency=1000.0 + index * 100.0, **kw)

    def _add_legacy_columns(self) -> None:
        """老库形态：这几列是固件自己加的，不是建表时就有。

        现在的 SCHEMA 已经把阻抗列写进建表语句，只有 phase 还是后加的；
        补列前先查一遍，模拟和生产的迁移路径保持一致。
        """
        with self.db._lock:
            have = {row["name"] for row in self.db._conn.execute(
                "PRAGMA table_info(sensor_data)")}
            for col in ("frequency_hz", "z_real", "z_imag", "magnitude"):
                if col not in have:
                    self.db._conn.execute(
                        f"ALTER TABLE sensor_data ADD COLUMN {col} REAL")
            self.db._conn.commit()

    def _put(self, ts: int, **vals: float) -> None:
        """往环境表写一行带遗留阻抗列的数据。"""
        with self.db._lock:
            if vals:
                self.db._conn.execute(
                    f"""INSERT INTO sensor_data
                       (gateway_id, node_id, timestamp, {", ".join(vals)})
                       VALUES (?, ?, ?, {", ".join("?" for _ in vals)})""",
                    ("GW_001", "LORA_NODE_01", ts, *vals.values()))
            else:
                self.db._conn.execute(
                    "INSERT INTO sensor_data (gateway_id, node_id, timestamp) "
                    "VALUES (?, ?, ?)", ("GW_001", "LORA_NODE_01", ts))
            self.db._conn.commit()

    def _imp_rows(self) -> list[dict]:
        with self.db._lock:
            return [dict(r) for r in self.db._conn.execute(
                """SELECT gateway_id, node_id, timestamp, frequency_hz, z_real,
                          z_imag, magnitude, phase, scan_id, rcal_ohm,
                          in_valid_window
                     FROM impedance_data
                     ORDER BY timestamp, COALESCE(frequency_hz, 0.0)""")]

    def test_fresh_schema_without_legacy_columns_is_a_noop(self):
        """新库的环境表没有遗留阻抗列，回填要直接跳过不报错。"""
        self.assertEqual(
            Database.backfill_impedance_from_sensor(self.db._conn),
            {"inserted": 0, "merged": 0, "without_frequency": 0})
        self.assertEqual(self._imp_rows(), [])

    def test_row_with_frequency_and_complex_part_is_stored(self):
        self._add_legacy_columns()
        self._put(1700000000, frequency_hz=2000.0, z_real=1000.0,
                  z_imag=300.0, magnitude=1044.0)
        self.assertEqual(
            Database.backfill_impedance_from_sensor(self.db._conn),
            {"inserted": 1, "merged": 0, "without_frequency": 0})
        row = self._imp_rows()[0]
        self.assertEqual((row["frequency_hz"], row["z_real"], row["z_imag"],
                          row["magnitude"]), (2000.0, 1000.0, 300.0, 1044.0))
        self.assertAlmostEqual(row["phase"], -math.degrees(math.atan2(300.0, 1000.0)),
                               places=6)
        self.assertEqual(row["rcal_ohm"], 51000.0)
        self.assertEqual(row["in_valid_window"], 1)
        self.assertEqual(row["scan_id"], "SCAN_SENSOR_1700000000")

    def test_frequency_outside_the_legacy_band_flags_out_of_window(self):
        self._add_legacy_columns()
        self._put(1700000001, frequency_hz=40000.0, magnitude=100.0)
        self._put(1700000002, frequency_hz=100.0, magnitude=100.0)
        Database.backfill_impedance_from_sensor(self.db._conn)
        self.assertEqual(
            sorted(r["frequency_hz"] for r in self._imp_rows()),
            [100.0, 40000.0])
        self.assertEqual({r["in_valid_window"] for r in self._imp_rows()}, {0})

    def test_magnitude_only_resolves_the_matching_spectrum_point(self):
        """环境帧只带幅度时，用同时刻的谱反查是哪一点的测量。"""
        self._add_legacy_columns()
        self.db.insert_sweep_points([self._pt(i) for i in range(5)])
        Database.backfill_impedance_from_sweep(self.db._conn)
        with self.db._lock:
            mag = self.db._conn.execute(
                """SELECT magnitude FROM impedance_data
                   WHERE timestamp=? AND frequency_hz=1300.0""",
                (1700000000,)).fetchone()[0]
        self._put(1700000000, magnitude=mag)
        # 那条谱点已经在阻抗表里，所以只是"已完整"，不该再插一条。
        self.assertEqual(
            Database.backfill_impedance_from_sensor(self.db._conn),
            {"inserted": 0, "merged": 0, "without_frequency": 0})
        self.assertEqual(len(self._imp_rows()), 5)

    def test_flat_spectrum_resolves_to_the_middle_point(self):
        """谱是平的时候好几个点幅度相等，只认落在段中频上的那个。"""
        self._add_legacy_columns()
        with self.db._lock:
            for i, freq in enumerate((1000.0, 1100.0, 1200.0, 1300.0, 1400.0)):
                self.db._conn.execute(
                    """INSERT INTO impedance_data
                       (gateway_id, node_id, timestamp, frequency_hz, magnitude,
                        scan_id, rcal_ohm, in_valid_window)
                       VALUES (?,?,?,?,5000.0,'SCAN_1_1700000010',51000.0,1)""",
                    ("GW_001", "LORA_NODE_01", 1700000010, freq))
            self.db._conn.commit()
        self._put(1700000010, magnitude=5000.0)
        # 5 个点幅度全等，键都在，所以也不该插。
        self.assertEqual(
            Database.backfill_impedance_from_sensor(self.db._conn),
            {"inserted": 0, "merged": 0, "without_frequency": 0})

    def test_no_spectrum_stores_magnitude_without_guessing_a_frequency(self):
        self._add_legacy_columns()
        self._put(1700000020, magnitude=9639.0)
        self.assertEqual(
            Database.backfill_impedance_from_sensor(self.db._conn),
            {"inserted": 1, "merged": 0, "without_frequency": 1})
        row = self._imp_rows()[0]
        self.assertIsNone(row["frequency_hz"])
        self.assertIsNone(row["phase"])
        self.assertIsNone(row["in_valid_window"])
        self.assertEqual(row["magnitude"], 9639.0)
        self.assertEqual(row["scan_id"], "SCAN_SENSOR_1700000020")

    def test_spectrum_without_a_matching_magnitude_keeps_frequency_null(self):
        """有谱但幅度对不上任何点，宁可频率留空也不硬配一个。"""
        self._add_legacy_columns()
        with self.db._lock:
            self.db._conn.execute(
                """INSERT INTO impedance_data
                   (gateway_id, node_id, timestamp, frequency_hz, magnitude, scan_id)
                   VALUES ('GW_001','LORA_NODE_01',1700000030,1000.0,5000.0,'S')""")
            self.db._conn.commit()
        self._put(1700000030, magnitude=777.0)
        self.assertEqual(
            Database.backfill_impedance_from_sensor(self.db._conn),
            {"inserted": 1, "merged": 0, "without_frequency": 1})
        null_rows = [r for r in self._imp_rows() if r["frequency_hz"] is None]
        self.assertEqual(len(null_rows), 1)
        self.assertEqual(null_rows[0]["magnitude"], 777.0)

    def test_magnitude_is_derived_from_the_complex_part_when_absent(self):
        self._add_legacy_columns()
        self._put(1700000040, frequency_hz=3000.0, z_real=3.0, z_imag=4.0)
        Database.backfill_impedance_from_sensor(self.db._conn)
        row = self._imp_rows()[0]
        self.assertEqual(row["magnitude"], 5.0)
        self.assertAlmostEqual(row["phase"], -math.degrees(math.atan2(4.0, 3.0)),
                               places=6)

    def test_merge_fills_only_the_missing_columns(self):
        """同键已有行时只填空列，已有的值一律不覆盖。"""
        self._add_legacy_columns()
        self.db.insert_impedance(ImpedanceData(
            gateway_id="GW_001", node_id="LORA_NODE_01", timestamp=1700000050,
            scan_id="SCAN_9_1700000050", frequency_hz=2000.0,
            z_real=1.0, z_imag=2.0, magnitude=2.24, phase=63.4))
        self._put(1700000050, frequency_hz=2000.0, z_real=999.0,
                  z_imag=998.0, magnitude=1.5)
        self.assertEqual(
            Database.backfill_impedance_from_sensor(self.db._conn)["merged"], 1)
        row = self._imp_rows()[0]
        # 原有实测值保留，只有原本为空的列被补上
        self.assertEqual((row["z_real"], row["z_imag"]), (1.0, 2.0))
        self.assertEqual(row["phase"], 63.4)
        self.assertEqual(row["scan_id"], "SCAN_9_1700000050")
        self.assertEqual(row["rcal_ohm"], 51000.0)
        self.assertEqual(row["in_valid_window"], 1)

    def test_second_run_is_a_noop(self):
        self._add_legacy_columns()
        self._put(1700000060, frequency_hz=2000.0, magnitude=1000.0)
        self._put(1700000061, magnitude=9639.0)
        Database.backfill_impedance_from_sensor(self.db._conn)
        self.assertEqual(
            Database.backfill_impedance_from_sensor(self.db._conn),
            {"inserted": 0, "merged": 0, "without_frequency": 0})
        self.assertEqual(len(self._imp_rows()), 2)


class ThreadSafetyTest(TempDbTest):
    def test_concurrent_writes_do_not_lock(self):
        errors: list[BaseException] = []

        def writer(offset: int) -> None:
            try:
                for i in range(25):
                    self.db.insert_sensor(SensorData(
                        gateway_id="GW_001", node_id=f"N{offset}",
                        timestamp=offset * 100 + i, temperature=float(i)))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.db.count_sensors("GW_001", "N0"), 25)
        self.assertEqual(self.db.count_sensors("GW_001", "N3"), 25)
        total = sum(self.db.count_sensors("GW_001", f"N{i}") for i in range(4))
        self.assertEqual(total, 100)


class SchemaMigrationTest(unittest.TestCase):
    """老库（prediction 表还没有 round_id）升级后不能报错，历史要能对齐。"""

    def setUp(self) -> None:
        import sqlite3

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(self.path))
        conn.execute(
            """CREATE TABLE prediction (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT,
                timestamp INTEGER,
                maturity REAL,
                maturity_level TEXT,
                harvest_date TEXT,
                confidence REAL)""")
        conn.execute(
            """CREATE INDEX idx_prediction_ts ON prediction(timestamp)""")
        conn.execute(
            "CREATE TABLE sweep_data (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "gateway_id TEXT, node_id TEXT, round_id INTEGER, timestamp INTEGER, "
            "point_index INTEGER, frequency_hz REAL, z_real REAL, z_imag REAL, "
            "magnitude REAL, report_id TEXT)")
        conn.executemany(
            "INSERT INTO sweep_data(gateway_id, node_id, round_id, timestamp, "
            "point_index) VALUES (?,?,?,?,?)",
            [("GW_001", "N1", 7, 1000, 0), ("GW_001", "N1", 7, 1000, 1)],
        )
        conn.executemany(
            "INSERT INTO prediction(node_id, timestamp, maturity) VALUES (?,?,?)",
            [("N1", 1000, 0.4), ("N1", 2000, 0.5)],
        )
        conn.commit()
        conn.close()
        self.addCleanup(self._finish)

    def _finish(self) -> None:
        self._tmp.cleanup()

    def test_legacy_db_migrates_cleanly(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        cols = [
            row["name"]
            for row in db._conn.execute("PRAGMA table_info(prediction)")
        ]
        self.assertIn("round_id", cols)
        indexes = {
            row["name"] for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_prediction_round", indexes)

    def test_legacy_predictions_backfilled_from_sweep_round(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        rows = db._conn.execute(
            "SELECT timestamp, round_id FROM prediction ORDER BY timestamp"
        ).fetchall()
        # 有对应扫频轮的补上轮次号，没有的保持 NULL 退回按时间戳去重
        self.assertEqual([r["round_id"] for r in rows], [7, None])

    def test_fresh_database_has_no_migration_errors(self):
        empty = Path(self._tmp.name) / "fresh.db"
        db = Database(empty)
        cols = [row["name"] for row in db._conn.execute(
            "PRAGMA table_info(prediction)")]
        db.close()
        self.assertIn("round_id", cols)


if __name__ == "__main__":
    unittest.main()
