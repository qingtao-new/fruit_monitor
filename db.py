"""SQLite 数据库层：建表、写入、查询。线程安全。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from protocol import DeviceStatus, ImpedanceData, PredictionData, SensorData


SCHEMA = """
CREATE TABLE IF NOT EXISTS sensor_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    temperature REAL,
    humidity REAL,
    soil_moisture REAL,
    nh3 REAL,
    h2s REAL,
    co2 REAL,
    ph REAL
);

CREATE TABLE IF NOT EXISTS impedance_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    frequency_hz REAL,
    z_real REAL,
    z_imag REAL,
    magnitude REAL,
    phase REAL,
    scan_id TEXT,
    rcal_ohm REAL,
    in_valid_window INTEGER
);

CREATE TABLE IF NOT EXISTS device_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id TEXT NOT NULL,
    node_id TEXT,
    status TEXT NOT NULL,
    timestamp INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT,
    timestamp INTEGER,
    maturity REAL,
    maturity_level TEXT,
    harvest_date TEXT,
    confidence REAL
);

CREATE INDEX IF NOT EXISTS idx_sensor_dev_ts
    ON sensor_data(gateway_id, node_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_impedance_dev_ts
    ON impedance_data(gateway_id, node_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_status_dev_ts
    ON device_status(gateway_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_prediction_ts
    ON prediction(timestamp);
"""


class Database:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 写入 ----

    def insert_sensor(self, d: SensorData) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sensor_data
                   (gateway_id, node_id, timestamp, temperature, humidity,
                    soil_moisture, nh3, h2s, co2, ph)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (d.gateway_id, d.node_id, d.timestamp, d.temperature, d.humidity,
                 d.soil_moisture, d.nh3, d.h2s, d.co2, d.ph),
            )
            self._conn.commit()

    def insert_impedance(self, d: ImpedanceData) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO impedance_data
                   (gateway_id, node_id, timestamp, frequency_hz, z_real, z_imag,
                    magnitude, phase, scan_id, rcal_ohm, in_valid_window)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (d.gateway_id, d.node_id, d.timestamp, d.frequency_hz, d.z_real,
                 d.z_imag, d.magnitude, d.phase, d.scan_id, d.rcal_ohm,
                 int(d.in_valid_window) if d.in_valid_window is not None else None),
            )
            self._conn.commit()

    def insert_status(self, d: DeviceStatus) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_status
                   (gateway_id, node_id, status, timestamp)
                   VALUES (?,?,?,?)""",
                (d.gateway_id, d.node_id, d.status, d.timestamp),
            )
            self._conn.commit()

    def insert_prediction(self, d: PredictionData) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO prediction
                   (node_id, timestamp, maturity, maturity_level, harvest_date, confidence)
                   VALUES (?,?,?,?,?,?)""",
                (d.node_id, d.timestamp, d.maturity, d.maturity_level,
                 d.harvest_date, d.confidence),
            )
            self._conn.commit()

    # ---- 查询 ----

    def query_sensor_history(
        self,
        gateway_id: str,
        node_id: str,
        limit: int = 300,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sensor_data WHERE gateway_id=? AND node_id=?"
        params: list[Any] = [gateway_id, node_id]
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ts)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in reversed(rows)]

    def query_impedance_history(
        self,
        gateway_id: str,
        node_id: str,
        scan_id: Optional[str] = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM impedance_data WHERE gateway_id=? AND node_id=?"
        params: list[Any] = [gateway_id, node_id]
        if scan_id:
            sql += " AND scan_id=?"
            params.append(scan_id)
        sql += " ORDER BY frequency_hz ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_status_history(
        self,
        gateway_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_status WHERE gateway_id=? ORDER BY timestamp DESC LIMIT ?",
                (gateway_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_sensor(self, gateway_id: str, node_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sensor_data WHERE gateway_id=? AND node_id=? ORDER BY timestamp DESC LIMIT 1",
                (gateway_id, node_id),
            ).fetchone()
        return dict(row) if row else None

    def count_sensors(self, gateway_id: str, node_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM sensor_data WHERE gateway_id=? AND node_id=?",
                (gateway_id, node_id),
            ).fetchone()
        return row["cnt"] if row else 0

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT DISTINCT gateway_id, node_id,
                        MAX(timestamp) as last_seen
                   FROM sensor_data
                   GROUP BY gateway_id, node_id
                   ORDER BY last_seen DESC""",
            ).fetchall()
        return [dict(r) for r in rows]
