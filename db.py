"""SQLite 数据库层：建表、写入、查询。线程安全。"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from protocol import DeviceStatus, ImpedanceData, PredictionData, SensorData, SweepPointData


SCHEMA = """
CREATE TABLE IF NOT EXISTS sensor_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    round_id INTEGER,
    temperature REAL,
    humidity REAL,
    soil_moisture REAL,
    nh3 REAL,
    h2s REAL,
    co2 REAL,
    ph REAL,
    frequency_hz REAL,
    z_real REAL,
    z_imag REAL,
    magnitude REAL,
    phase REAL
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
    round_id INTEGER,
    timestamp INTEGER,
    maturity REAL,
    maturity_level TEXT,
    harvest_date TEXT,
    confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_prediction_round
    ON prediction(node_id, round_id);
CREATE INDEX IF NOT EXISTS idx_status_dev_ts
    ON device_status(gateway_id, timestamp);
CREATE TABLE IF NOT EXISTS sweep_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT,
    gateway_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    round_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    point_index INTEGER NOT NULL,
    frequency_hz REAL,
    z_real REAL,
    z_imag REAL,
    magnitude REAL,
    soil_moisture REAL,
    temperature REAL,
    nh3 REAL,
    h2s REAL,
    co2 REAL,
    ph REAL,
    humidity REAL
);

CREATE INDEX IF NOT EXISTS idx_sensor_dev_ts
    ON sensor_data(gateway_id, node_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_sensor_dev_round
    ON sensor_data(gateway_id, node_id, round_id);
CREATE INDEX IF NOT EXISTS idx_impedance_dev_ts
    ON impedance_data(gateway_id, node_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_impedance_dedup
    ON impedance_data(gateway_id, node_id, scan_id, frequency_hz);
CREATE INDEX IF NOT EXISTS idx_status_dev_ts
    ON device_status(gateway_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_prediction_ts
    ON prediction(timestamp);
CREATE INDEX IF NOT EXISTS idx_sweep_dev_ts
    ON sweep_data(gateway_id, node_id, timestamp, round_id, point_index);
CREATE INDEX IF NOT EXISTS idx_sweep_report
    ON sweep_data(report_id);

CREATE TABLE IF NOT EXISTS round_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    round_id INTEGER,
    scan_id TEXT,
    report_id TEXT,
    points INTEGER,
    total_points INTEGER,
    complete INTEGER,
    retry INTEGER,
    imp_mean REAL,
    first_ts INTEGER,
    last_ts INTEGER,
    recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_round_log_dev_round
    ON round_log(gateway_id, node_id, round_id);
CREATE INDEX IF NOT EXISTS idx_round_log_dev_time
    ON round_log(gateway_id, node_id, recorded_at);
"""


# 老库 impedance_data 的字段约定。回填 sweep_data 里漏掉的点时按同一套算，
# 否则新旧行对不上号、按 scan_id 点数做完整性校验就会出错。
# 这三条是在老库 12601 行上反推校验过的，零偏差：
#   phase           = -atan2(z_imag, z_real)（度）
#   magnitude       = hypot(z_real, z_imag)
#   in_valid_window = 1000 <= frequency_hz <= 30000
# rcal_ohm 是老库 4831 行里恒定的参考电阻标称值，不是从测量结果算出来的。
LEGACY_R_CAL_OHM = 51000.0
LEGACY_WINDOW_LO_HZ = 1000.0
LEGACY_WINDOW_HI_HZ = 30000.0

# sensor_data 里跟着环境量一起落库的阻抗列。环境行一轮只有一条，
# 频谱却有几个频点，所以这里存的是分析频段内的均值，不是某个频点的原值。
SENSOR_IMP_COLS = ("frequency_hz", "z_real", "z_imag", "magnitude", "phase")

# 按时间就近配对时的可接受偏差。环境帧 2s 一帧、整谱十几秒一次，
# 窗口开宽一点是为了环境帧偶尔断几秒时还能配上；开太宽会把阻抗挂到
# 明显不是同一时刻的环境快照上，240s 是折中。
SENSOR_IMPEDANCE_WINDOW_S = 240


def impedance_fields(impedance: Optional[ImpedanceData]) -> tuple:
    """阻抗对象 -> 环境表阻抗列的取值顺序，和 SENSOR_IMP_COLS 一一对应。"""
    if impedance is None:
        return (None, None, None, None, None)
    return (impedance.frequency_hz, impedance.z_real, impedance.z_imag,
            impedance.magnitude, impedance.phase)


class Database:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # WAL 让内嵌 Broker 线程写入时不阻塞 GUI 线程查询；
        # busy_timeout 避免多写入者短暂竞争时直接报 database is locked。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=2500")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # 先补列再建表建索引：老库的 prediction 表还没有 round_id，
            # 直接跑整段 schema 会因为新索引引用不存在的列而报错。
            self._migrate()
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _migrate(self) -> None:
        """老库补列。``ALTER TABLE`` 不支持 IF NOT EXISTS，得自己查一遍。"""
        tables = {row["name"] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in (("prediction", "round_id"), ("sensor_data", "round_id")):
            if table not in tables:
                continue
            cols = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")
        # 环境行要一并带上同一次采样的阻抗值，老库缺这几列时补上。
        # 早期固件只写了前四列，phase 是后加的，所以不能假设整组都在。
        if "sensor_data" in tables:
            cols = {row["name"] for row in self._conn.execute(
                "PRAGMA table_info(sensor_data)")}
            for column in SENSOR_IMP_COLS:
                if column not in cols:
                    self._conn.execute(
                        f"ALTER TABLE sensor_data ADD COLUMN {column} REAL")
        indexes = {row["name"] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        if "prediction" in tables and "idx_prediction_round" not in indexes:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_prediction_round "
                "ON prediction(node_id, round_id)")
        if "prediction" in tables:
            self._backfill_prediction_round_id(self._conn)

    @staticmethod
    def _backfill_prediction_round_id(conn: sqlite3.Connection) -> int:
        """给加列前的老预测行补轮次号。

        轮次号只在 sweep_data 里，同一 (node_id, timestamp) 对一轮，
        按时间序回填一次即可；回填不上就留 NULL，退回按时间戳去重。
        """
        cur = conn.execute(
            """UPDATE prediction
               SET round_id = (
                     SELECT sd.round_id
                       FROM sweep_data sd
                      WHERE sd.node_id = prediction.node_id
                        AND sd.timestamp = prediction.timestamp
                      ORDER BY sd.id DESC LIMIT 1
               )
               WHERE round_id IS NULL
                 AND EXISTS (
                     SELECT 1 FROM sweep_data sd
                      WHERE sd.node_id = prediction.node_id
                        AND sd.timestamp = prediction.timestamp)"""
         )
        return int(cur.rowcount)

    @staticmethod
    def backfill_impedance_from_sweep(conn: sqlite3.Connection) -> dict[str, int]:
        """把 sweep_data 里有、impedance_data 里没有的频点补齐，返回统计。

        早期写入路径只把扫频点进了 sweep_data，没同步到阻抗历史表，于是有十几
        轮完整扫频在 impedance_data 里一个点都没有。客户端按 scan_id 数点数
        判断"这轮到底收齐了几个点"时，那些轮会被误判成彻底残缺。

        只补缺失的 (网关, 节点, 秒, 频率)，不删不改任何已有行。sweep_data 里
        同一个键写过多次（分段重投、MQTT 重发）时取最后一条，和
        ``insert_impedance`` 的去重语义一致。

        scan_id 优先复用该轮已有的扫描标识，该轮一条都没有才按
        ``SCAN_<轮次>_<起始秒>`` 现造，保证一轮只落在一个 scan_id 下，
        点数仍然能当完整性校验用。

        幂等：键已存在就不插，重跑一次是空操作。
        """
        # SQLite 没有 degrees()：pi = 4*atan(1)，所以 180/pi = 180/(4*atan(1))。
        # 写成 180/atan(1) 会差 4 倍，把 -63.43 度算成 -253.74。
        rad2deg = "180.0 / (4.0 * atan(1.0))"
        sql = f"""
WITH sweep_key AS (
    SELECT gateway_id, node_id, timestamp, frequency_hz, z_real, z_imag,
           magnitude, round_id
      FROM sweep_data
     WHERE round_id IS NOT NULL AND frequency_hz IS NOT NULL
       AND id IN (SELECT MAX(id) FROM sweep_data
                  GROUP BY gateway_id, node_id, timestamp, frequency_hz)
),
round_overlap AS (
    SELECT x.round_id, x.gateway_id, x.node_id, i.scan_id, COUNT(*) AS n
      FROM sweep_key x
      JOIN impedance_data i
        ON i.gateway_id = x.gateway_id AND i.node_id = x.node_id
       AND i.timestamp = x.timestamp AND i.frequency_hz = x.frequency_hz
     GROUP BY x.round_id, x.gateway_id, x.node_id, i.scan_id
),
round_scan AS (
    SELECT gateway_id, node_id, round_id, scan_id AS reused_scan
      FROM (SELECT o.gateway_id, o.node_id, o.round_id, o.scan_id,
                   ROW_NUMBER() OVER (PARTITION BY o.gateway_id, o.node_id,
                                      o.round_id
                                      ORDER BY o.n DESC, o.scan_id) AS rn
              FROM round_overlap o)
     WHERE rn = 1
),
round_key AS (
    SELECT gateway_id, node_id, round_id, MIN(timestamp) AS min_ts
      FROM sweep_key GROUP BY gateway_id, node_id, round_id
)
INSERT INTO impedance_data
    (gateway_id, node_id, timestamp, frequency_hz, z_real, z_imag, magnitude,
     phase, scan_id, rcal_ohm, in_valid_window)
SELECT s.gateway_id, s.node_id, s.timestamp, s.frequency_hz, s.z_real, s.z_imag,
       COALESCE(s.magnitude, sqrt(s.z_real * s.z_real + s.z_imag * s.z_imag)),
       -1.0 * atan2(s.z_imag, s.z_real) * {rad2deg},
       COALESCE(rs.reused_scan, 'SCAN_' || s.round_id || '_' || rk.min_ts),
       ?,
       CASE WHEN s.frequency_hz >= ? AND s.frequency_hz <= ? THEN 1 ELSE 0 END
  FROM sweep_key s
  LEFT JOIN round_scan rs
    ON rs.gateway_id = s.gateway_id AND rs.node_id = s.node_id
   AND rs.round_id = s.round_id
  JOIN round_key rk
    ON rk.gateway_id = s.gateway_id AND rk.node_id = s.node_id
   AND rk.round_id = s.round_id
 WHERE NOT EXISTS (
       SELECT 1 FROM impedance_data i
        WHERE i.gateway_id = s.gateway_id AND i.node_id = s.node_id
          AND i.timestamp = s.timestamp AND i.frequency_hz = s.frequency_hz)
"""
        before = conn.execute("SELECT COUNT(*) FROM impedance_data").fetchone()[0]
        # 受影响轮数要在插入前算，插完这些轮就已经补齐了。
        rounds = conn.execute(
            """SELECT COUNT(*) FROM (
                SELECT DISTINCT gateway_id, node_id, round_id
                  FROM sweep_data
                 WHERE NOT EXISTS (
                       SELECT 1 FROM impedance_data i
                        WHERE i.gateway_id = sweep_data.gateway_id
                          AND i.node_id = sweep_data.node_id
                          AND i.timestamp = sweep_data.timestamp
                          AND i.frequency_hz = sweep_data.frequency_hz))""",
        ).fetchone()[0]
        conn.execute(sql, (LEGACY_R_CAL_OHM, LEGACY_WINDOW_LO_HZ,
                           LEGACY_WINDOW_HI_HZ))
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM impedance_data").fetchone()[0]
        return {"inserted": int(after - before), "rounds": int(rounds)}

    @staticmethod
    def backfill_impedance_from_sensor(conn: sqlite3.Connection) -> dict[str, int]:
        """把 sensor_data 遗留阻抗列里的值补进 impedance_data。

        早期固件把阻抗塞在环境帧里上报，落在 sensor_data 的
        frequency_hz / z_real / z_imag / magnitude 四列上。后来的写入路径只把
        这些行当环境量用，阻抗值就一直留在环境表里没人读。这里把它们拆出来。

        频率怎么定：
        1. 行自带 frequency_hz 就直接用；
        2. 同一 (网关, 节点, 秒) 有扫频谱时，这条值对应"段中频"——一段 50 点
           取中间那点。先在谱里找幅度相等的频率，唯一就采纳；谱是平的会有
           好几个相等，这时只采纳恰好落在段中频上的那个；
        3. 都对不上就只存幅度、频率留空，不猜。

        同键已有阻抗行时只填它缺的列，已有的值一律不覆盖。幂等。
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sensor_data)")}
        imp_cols = ("frequency_hz", "z_real", "z_imag", "magnitude")
        have = [c for c in imp_cols if c in cols]
        empty = {"inserted": 0, "merged": 0, "without_frequency": 0}
        if not have:
            return empty

        where = " OR ".join(f"{c} IS NOT NULL" for c in have)
        sel = ", ".join(f"s.{c}" for c in imp_cols)
        rows = conn.execute(
            f"""SELECT s.gateway_id, s.node_id, s.timestamp, {sel}
                  FROM sensor_data s WHERE {where}""",
        ).fetchall()
        inserted = merged = without_frequency = 0
        for s in rows:
            mag = s["magnitude"]
            freq = s["frequency_hz"]
            z_real, z_imag = s["z_real"], s["z_imag"]
            spec = conn.execute(
                """SELECT frequency_hz, magnitude, z_real, z_imag, scan_id
                     FROM impedance_data
                    WHERE gateway_id=? AND node_id=? AND timestamp=?
                    ORDER BY frequency_hz""",
                (s["gateway_id"], s["node_id"], s["timestamp"]),
            ).fetchall()
            src = None
            if freq is None and spec and mag is not None:
                middle = spec[len(spec) // 2]["frequency_hz"]
                hits = [r for r in spec
                        if r["magnitude"] is not None
                        and abs(r["magnitude"] - mag) < 0.01]
                if len(hits) == 1:
                    freq = hits[0]["frequency_hz"]
                elif any(r["frequency_hz"] == middle for r in hits):
                    freq = middle
            if freq is not None:
                src = next((r for r in spec if r["frequency_hz"] == freq), None)
                if z_real is None and src is not None:
                    z_real = src["z_real"]
                if z_imag is None and src is not None:
                    z_imag = src["z_imag"]
            if mag is None and z_real is not None and z_imag is not None:
                mag = math.hypot(z_real, z_imag)
            phase = (math.degrees(math.atan2(z_imag, z_real)) * -1.0
                     if z_real is not None and z_imag is not None else None)
            window = (None if freq is None
                      else int(LEGACY_WINDOW_LO_HZ <= freq <= LEGACY_WINDOW_HI_HZ))
            scan_id = ((src["scan_id"] if src is not None else None)
                       or f"SCAN_SENSOR_{s['timestamp']}")
            existing = conn.execute(
                    """SELECT frequency_hz, z_real, z_imag, magnitude, phase,
                              scan_id, rcal_ohm, in_valid_window
                       FROM impedance_data
                       WHERE gateway_id=? AND node_id=? AND timestamp=?
                         AND frequency_hz IS ?""",
                    (s["gateway_id"], s["node_id"], s["timestamp"], freq),
            ).fetchone()
            if existing is not None:
                # SQLite 的 rowcount 不算"值没变"的 UPDATE，自己判断有没有可填的列。
                if not any(o is None and n is not None for o, n in zip(
                        (existing["frequency_hz"], existing["z_real"],
                         existing["z_imag"], existing["magnitude"], existing["phase"],
                         existing["scan_id"], existing["rcal_ohm"],
                         existing["in_valid_window"]),
                        (freq, z_real, z_imag, mag, phase, scan_id,
                         LEGACY_R_CAL_OHM, window))):
                    continue
                conn.execute(
                    """UPDATE impedance_data SET
                           frequency_hz = COALESCE(frequency_hz, ?),
                           z_real       = COALESCE(z_real, ?),
                           z_imag       = COALESCE(z_imag, ?),
                           magnitude    = COALESCE(magnitude, ?),
                           phase        = COALESCE(phase, ?),
                           scan_id      = COALESCE(scan_id, ?),
                           rcal_ohm     = COALESCE(rcal_ohm, ?),
                           in_valid_window = COALESCE(in_valid_window, ?)
                       WHERE gateway_id=? AND node_id=? AND timestamp=?
                         AND frequency_hz IS ?""",
                    (freq, z_real, z_imag, mag, phase, scan_id,
                     LEGACY_R_CAL_OHM, window,
                     s["gateway_id"], s["node_id"], s["timestamp"], freq),
                )
                merged += 1
                continue
            conn.execute(
                """INSERT INTO impedance_data
                   (gateway_id, node_id, timestamp, frequency_hz, z_real, z_imag,
                    magnitude, phase, scan_id, rcal_ohm, in_valid_window)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (s["gateway_id"], s["node_id"], s["timestamp"], freq, z_real, z_imag,
                 mag, phase, scan_id, LEGACY_R_CAL_OHM, window),
            )
            inserted += 1
            if freq is None:
                without_frequency += 1
        conn.commit()
        return {"inserted": inserted, "merged": merged,
                "without_frequency": without_frequency}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 写入 ----

    def insert_sensor(self, d: SensorData, round_id: Optional[int] = None,
                      impedance: Optional[ImpedanceData] = None) -> bool:
        """写入一条环境传感器数据，返回是否新建了一行。

        网关固件不单独发环境帧：7 个环境量跟着每个扫频频点一起上报，而一轮
        里 50 个点的这些值是同一份。扫频路径带上 round_id 时按
        (网关, 节点, 轮次) 折叠成一行——同一轮的分段到达、整轮补齐、
        MQTT QoS1 重投都只刷这一行，环境历史里一轮就是一条数据，
        而不是 50 条内容完全相同的重复行。

        普通 sensor 帧没有轮次号，round_id 为空时保持追加写入，
        原有行为不变。

        impedance 带上同一次采样的阻抗值，写进环境行的阻抗列，
        历史窗口里环境量和阻抗一并对得上，不再每行都缺。
        """
        rid = round_id if round_id is not None else d.round_id
        env = (d.temperature, d.humidity, d.soil_moisture, d.nh3,
               d.h2s, d.co2, d.ph)
        imp = impedance_fields(impedance)
        with self._lock:
            if rid is not None:
                row = self._conn.execute(
                    "SELECT id FROM sensor_data "
                    "WHERE gateway_id=? AND node_id=? AND round_id=?",
                    (d.gateway_id, d.node_id, rid),
                ).fetchone()
                if row is not None:
                    # 已有这一轮的环境行：刷新一遍，但不用空值覆盖已有实测值。
                    self._conn.execute(
                        """UPDATE sensor_data SET
                               timestamp=?, temperature=COALESCE(?, temperature),
                               humidity=COALESCE(?, humidity),
                               soil_moisture=COALESCE(?, soil_moisture),
                               nh3=COALESCE(?, nh3),
                               h2s=COALESCE(?, h2s),
                               co2=COALESCE(?, co2),
                               ph=COALESCE(?, ph),
                               frequency_hz=COALESCE(?, frequency_hz),
                               z_real=COALESCE(?, z_real),
                               z_imag=COALESCE(?, z_imag),
                               magnitude=COALESCE(?, magnitude),
                               phase=COALESCE(?, phase)
                           WHERE id=?""",
                        (d.timestamp, *env, *imp, row["id"]),
                    )
                    self._conn.commit()
                    return False
            self._conn.execute(
                """INSERT INTO sensor_data
                   (gateway_id, node_id, timestamp, round_id, temperature, humidity,
                    soil_moisture, nh3, h2s, co2, ph,
                    frequency_hz, z_real, z_imag, magnitude, phase)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d.gateway_id, d.node_id, d.timestamp, rid, *env, *imp),
            )
            self._conn.commit()
            return True

    def attach_sensor_impedance(self, gateway_id: str, node_id: str,
                                round_id: Optional[int],
                                impedance: Optional[ImpedanceData],
                                force: bool = False) -> bool:
        """把阻抗值补进该轮已有的环境行，不新建行。

        只在环境行存在时补：环境表一行代表一次采样的环境快照，
        不该为了一个阻抗值凭空多出一条没有环境量的行。
        没环境行就返回 False，阻抗数据本来就在 impedance_data 里，不丢。

        force=True 无条件覆盖——整轮收齐后刷第二次时用，
        分段到达时手里只有部分频点，算出来的均值是半成品，
        全轮齐了得拿更准的数把它换掉。默认只填空行，不动已有值。
        """
        if round_id is None:
            return False
        imp = impedance_fields(impedance)
        # 只看 z_real / z_imag / magnitude：只有相位没有实测值不算一次测量，
        # 不该拿它去补一行。
        if impedance is None or all(v is None for v in imp[1:4]):
            return False
        with self._lock:
            if force:
                cur = self._conn.execute(
                    """UPDATE sensor_data
                        SET frequency_hz=?, z_real=?, z_imag=?, magnitude=?, phase=?
                      WHERE gateway_id=? AND node_id=? AND round_id=?""",
                    (*imp, gateway_id, node_id, round_id),
                )
            else:
                cur = self._conn.execute(
                    """UPDATE sensor_data
                        SET frequency_hz=COALESCE(?, frequency_hz),
                            z_real=COALESCE(?, z_real),
                            z_imag=COALESCE(?, z_imag),
                            magnitude=COALESCE(?, magnitude),
                            phase=COALESCE(?, phase)
                      WHERE gateway_id=? AND node_id=? AND round_id=?
                        AND z_real IS NULL AND magnitude IS NULL""",
                    (*imp, gateway_id, node_id, round_id),
                )
            self._conn.commit()
        return cur.rowcount > 0

    def attach_sensor_impedance_by_time(self, gateway_id: str, node_id: str,
                                        impedance: Optional[ImpedanceData],
                                        from_ts: int, to_ts: int,
                                        force: bool = False) -> int:
        """把有效频段均值填进 [from_ts, to_ts] 内还没填过阻抗的环境行。

        真实硬件的阻抗按扫描段上报，不带轮次号，也不塞进环境帧。一段扫描是对
        它前面那段时间里状态的一次测量，段内每一帧环境量都属于这次测量——
        只填离扫描时刻最近的一行的话，环境表绝大部分行还是空的。

        不新建行：环境表一行代表一次环境快照，不该为了一个阻抗值凭空多出一条
        没有环境量的行。窗里没有可填的行就返回 0，阻抗本来就在 impedance_data。

        非 force 时只填还没填过阻抗的行，免得把 sweep 轮次折叠出来的那行
        （用的是带轮次号的一轮点）又覆盖成整谱均值。
        返回填了几行。
        """
        imp = impedance_fields(impedance)
        # 只看 z_real / z_imag / magnitude：只有相位没有实测值不算一次测量。
        if impedance is None or all(v is None for v in imp[1:4]):
            return 0
        with self._lock:
            if force:
                cur = self._conn.execute(
                    """UPDATE sensor_data
                        SET frequency_hz=?, z_real=?, z_imag=?, magnitude=?, phase=?
                       WHERE gateway_id=? AND node_id=? AND timestamp BETWEEN ? AND ?""",
                    (*imp, gateway_id, node_id, from_ts, to_ts))
            else:
                cur = self._conn.execute(
                    """UPDATE sensor_data
                        SET frequency_hz=?, z_real=?, z_imag=?, magnitude=?, phase=?
                       WHERE gateway_id=? AND node_id=? AND timestamp BETWEEN ? AND ?
                         AND z_real IS NULL AND magnitude IS NULL""",
                    (*imp, gateway_id, node_id, from_ts, to_ts))
            self._conn.commit()
        return cur.rowcount

    def insert_impedance(self, d: ImpedanceData) -> None:
        """写入单个阻抗频点，同一频点只保留一条。

        一个扫频点会被多条路径写进来：分段到达时逐点写一次、整轮收齐出谱
        再写一次、静默窗口补发又写一次、MQTT QoS1 还会重投。没有去重的话
        历史表会成倍膨胀，而且行数再也无法当"这轮收齐了几个点"来校验。

        去重键是 (网关, 节点, 扫描轮, 频率)：一次扫描里每个频率只该有一个
        测量值，后来的覆盖先前的（重投/补发通常带着更新鲜的环境量上下文）。
        scan_id 为空的老固件报文退回 (网关, 节点, 秒, 频率)。
        """
        if d.frequency_hz is None:
            return
        fields = (d.gateway_id, d.node_id, d.timestamp, d.frequency_hz, d.z_real,
                  d.z_imag, d.magnitude, d.phase, d.scan_id, d.rcal_ohm,
                  int(d.in_valid_window) if d.in_valid_window is not None else None)
        with self._lock:
            if d.scan_id:
                self._conn.execute(
                    """DELETE FROM impedance_data
                       WHERE gateway_id=? AND node_id=? AND scan_id=? AND frequency_hz=?""",
                    (d.gateway_id, d.node_id, d.scan_id, d.frequency_hz),
                )
            else:
                self._conn.execute(
                    """DELETE FROM impedance_data
                       WHERE gateway_id=? AND node_id=?
                         AND (scan_id IS NULL OR scan_id='')
                         AND timestamp=? AND frequency_hz=?""",
                    (d.gateway_id, d.node_id, d.timestamp, d.frequency_hz),
                )
            self._conn.execute(
                """INSERT INTO impedance_data
                    (gateway_id, node_id, timestamp, frequency_hz, z_real, z_imag,
                     magnitude, phase, scan_id, rcal_ohm, in_valid_window)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                fields,
            )
            self._conn.commit()

    def insert_status(self, d: DeviceStatus) -> None:
        # 固件老版本不发 status 字段时 parse_status 会填 "unknown"，
        # 那类记录没有状态语义，直接丢弃，避免状态历史被噪声灌满。
        if not d.status or str(d.status).lower() in ("unknown", "none", ""):
            return
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_status
                    (gateway_id, node_id, status, timestamp)
                    VALUES (?,?,?,?)""",
                (d.gateway_id, d.node_id, d.status, d.timestamp),
            )
            self._conn.commit()

    def insert_prediction(self, d: PredictionData) -> None:
        """只折叠真正重复的预测。

        去重键分两级：
        1. 有 round_id 时按 (node_id, round_id) —— 一轮只留一条，
           不管它落在哪一秒、被哪条路径写了几次（分段到达 / 整谱补齐 /
           模拟器直发 / QoS1 重投）。
        2. 老固件没有轮次号时退回 (node_id, timestamp) + 内容指纹，
           完全相同才算重复；同一秒落了两轮不同的上报仍然各留一条。
        """
        def _fp(maturity, level, harvest, confidence):
            return (
                maturity if maturity is None else round(float(maturity), 6),
                str(level) if level is not None else "",
                str(harvest) if harvest is not None else "",
                confidence if confidence is None else round(float(confidence), 6),
            )

        incoming = _fp(d.maturity, d.maturity_level, d.harvest_date, d.confidence)
        with self._lock:
            if d.node_id and d.round_id is not None:
                rows = self._conn.execute(
                    """SELECT maturity, maturity_level, harvest_date, confidence
                       FROM prediction WHERE node_id=? AND round_id=?""",
                    (d.node_id, d.round_id),
                ).fetchall()
                if any(_fp(r["maturity"], r["maturity_level"],
                           r["harvest_date"], r["confidence"]) == incoming
                       for r in rows):
                    return
            elif d.node_id and d.timestamp:
                rows = self._conn.execute(
                    """SELECT maturity, maturity_level, harvest_date, confidence
                       FROM prediction WHERE node_id=? AND timestamp=?""",
                    (d.node_id, d.timestamp),
                ).fetchall()
                if any(_fp(r["maturity"], r["maturity_level"],
                           r["harvest_date"], r["confidence"]) == incoming
                       for r in rows):
                    return
            self._conn.execute(
                """INSERT INTO prediction
                    (node_id, round_id, timestamp, maturity, maturity_level,
                     harvest_date, confidence)
                    VALUES (?,?,?,?,?,?,?)""",
                (d.node_id, d.round_id, d.timestamp, d.maturity, d.maturity_level,
                 d.harvest_date, d.confidence),
            )
            self._conn.commit()

    def delete_predictions_for(self, nodes: list[str] | tuple[str, ...]) -> int:
        """按节点清单删除预测，用于"停止记录"后的清理。

        空列表是危险操作（会清空全表），直接拒绝。
        """
        cleaned = [n for n in (nodes or []) if n]
        if not cleaned:
            return 0
        placeholders = ",".join("?" for _ in cleaned)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM prediction WHERE node_id IN ({placeholders})",
                list(cleaned),
            )
            self._conn.commit()
        return int(cur.rowcount)

    def record_round(
        self,
        gateway_id: str,
        node_id: str,
        round_id: Optional[int],
        scan_id: Optional[str] = None,
        report_id: Optional[str] = None,
        points: Optional[int] = None,
        total_points: Optional[int] = None,
        complete: Optional[bool] = None,
        first_ts: Optional[int] = None,
        last_ts: Optional[int] = None,
    ) -> None:
        """记录一轮扫频的完整性判定，一轮只留一条。

        这一表存在的意义：只有扫频点落库是"结果"，看不出这一轮到底收齐
        没有。丢一段包、程序提前退出，缺口只会表现为"这轮只有 50 个点"，
        而 50 个点是本来就该只有 50，还是丢了 50，光看点分不清。
        所以每次整轮落库（无论收齐没收齐）都记一条判定，事后可以一条
        SQL 答完"哪几轮不完整、各缺多少"。

        同一轮可能被写两次：静默窗口超时强刷一次、固件整轮摘要回来再补
        一次，所以按 (网关, 节点, 轮次号) 折叠成 upsert，不追加。
        没有轮次号的老报文退回 (网关, 节点, scan_id)。
        """
        if complete is None:
            complete = bool(points is not None and total_points
                            and points >= total_points)
        with self._lock:
            row = None
            if round_id is not None:
                row = self._conn.execute(
                    "SELECT id FROM round_log WHERE gateway_id=? AND node_id=? "
                    "AND round_id=?",
                    (gateway_id, node_id, round_id),
                ).fetchone()
            elif scan_id:
                row = self._conn.execute(
                    "SELECT id FROM round_log WHERE gateway_id=? AND node_id=? "
                    "AND scan_id=?",
                    (gateway_id, node_id, scan_id),
                ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO round_log
                        (gateway_id, node_id, round_id, scan_id, report_id,
                         points, total_points, complete, first_ts, last_ts,
                         recorded_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (gateway_id, node_id, round_id, scan_id, report_id,
                     points, total_points, int(complete), first_ts, last_ts,
                     int(time.time())),
                )
            else:
                # 已有判定就只补缺、点数只增不减：
                # 同一轮可能被刷两次（先超时强刷出 50 点、迟到段到了再刷一次），
                # 那次更少点数的旧判定不能把后来补齐的结果盖回不完整，
                # 也不能反过来因为重投报文把点数往上虚增。
                existing = self._conn.execute(
                    "SELECT points, total_points, first_ts, last_ts, complete "
                    "FROM round_log WHERE id=?",
                    (row["id"],),
                ).fetchone()
                new_points = max(int(existing["points"] or 0), int(points or 0))
                new_total = max(int(existing["total_points"] or 0),
                                int(total_points or 0))
                # 期望点数已知才重算完整判定；还没有期望点数时保留原值。
                new_complete = (int(bool(existing["complete"]))
                                if not new_total
                                else int(new_points >= new_total))
                new_first = (existing["first_ts"]
                             if existing["first_ts"] is not None else first_ts)
                new_last = max(int(existing["last_ts"] or 0), int(last_ts or 0))
                self._conn.execute(
                    """UPDATE round_log SET
                            report_id    = COALESCE(report_id, ?),
                            points       = ?,
                            total_points = ?,
                            complete     = ?,
                            first_ts     = ?,
                            last_ts      = ?,
                            recorded_at  = ?
                        WHERE id=?""",
                    (report_id, new_points, new_total, new_complete,
                     new_first, new_last or None, int(time.time()), row["id"]),
                )
            self._conn.commit()

    def record_round_done(
        self,
        gateway_id: str,
        node_id: str,
        round_id: Optional[int],
        total_points: Optional[int] = None,
        retry: Optional[int] = None,
        imp_mean: Optional[float] = None,
    ) -> None:
        """固件整轮摘要回来：补上补发次数和整轮均值。

        固件说"这轮 100 点、补发 2 次"和我们本地实际落了几点经常对不上——
        对不上的那几轮恰恰是最该看的。所以固件自报的点数一律不入库
        （这个函数连收都不收那个参数）：本地没有这一轮记录时只新建一条
        ``points`` 为空、完整性未知的行，真实收到几个点由 ``record_round``
        自己填。期望点数取 max，不让摘要把已经知道的更大值改小。
        """
        if not gateway_id or not node_id:
            return
        with self._lock:
            row = None
            if round_id is not None:
                row = self._conn.execute(
                    "SELECT id, total_points FROM round_log "
                    "WHERE gateway_id=? AND node_id=? AND round_id=?",
                    (gateway_id, node_id, round_id),
                ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO round_log
                        (gateway_id, node_id, round_id, total_points,
                         retry, imp_mean, recorded_at)
                        VALUES (?,?,?,?,?,?,?)""",
                    (gateway_id, node_id, round_id, total_points,
                     retry, imp_mean, int(time.time())),
                )
            else:
                new_total = max(int(row["total_points"] or 0),
                                int(total_points or 0))
                self._conn.execute(
                    """UPDATE round_log SET
                            total_points = COALESCE(?, total_points),
                            retry        = COALESCE(?, retry),
                            imp_mean     = COALESCE(?, imp_mean),
                            recorded_at  = ?
                        WHERE id=?""",
                    (new_total or None, retry, imp_mean, int(time.time()),
                     row["id"]),
                )
            self._conn.commit()

    def list_round_log(
        self,
        gateway_id: str = "",
        node_id: str = "",
        limit: int = 100,
        only_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        """列轮次完整性记录，最近的在前。"""
        sql = ("SELECT id, gateway_id, node_id, round_id, scan_id, report_id, "
               "points, total_points, complete, retry, imp_mean, "
               "first_ts, last_ts, recorded_at FROM round_log")
        clauses, params = [], []
        if gateway_id:
            clauses.append("gateway_id=?")
            params.append(gateway_id)
        if node_id:
            clauses.append("node_id=?")
            params.append(node_id)
        if only_incomplete:
            clauses.append("(complete IS NULL OR complete=0)")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY recorded_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_incomplete_rounds(
        self,
        gateway_id: str = "",
        node_id: str = "",
    ) -> int:
        sql = "SELECT COUNT(*) AS c FROM round_log WHERE complete=0"
        params: list[Any] = []
        if gateway_id:
            sql += " AND gateway_id=?"
            params.append(gateway_id)
        if node_id:
            sql += " AND node_id=?"
            params.append(node_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"]) if row else 0

    def insert_sweep_points(self, points: list[SweepPointData]) -> int:
        """批量落库扫频点。

        MQTT QoS1 会重投，同一轮也可能被分多批到达（分段上报、整谱补齐
        再写一遍），任意两批都可能出现相同的
        (网关, 节点, 轮次, 点序号)。只在本批内去重不够，还要比对库里
        已存在的键，否则同一轮会出现重复点、Nyquist 轨迹上出现叠影。
        返回实际新写入的条数。
        """
        if not points:
            return 0
        seen: set[tuple[str, str, int, int]] = set()
        unique: list[SweepPointData] = []
        # 网关 -> {(节点, 轮次号)}，查库时只捞这一批真正涉及的轮次。
        rounds: dict[str, set[tuple[str, int]]] = {}
        for point in points:
            try:
                index = int(point.point_index)
                round_id = int(point.round_id)
            except (TypeError, ValueError):
                # 索引或轮次号非法的点直接丢弃，否则 NOT NULL 约束会整批报错。
                continue
            key = (point.gateway_id, point.node_id, round_id, index)
            if key in seen:
                continue
            seen.add(key)
            unique.append((point, index))
            rounds.setdefault(point.gateway_id, set()).add((point.node_id, round_id))
        if not unique:
            return 0

        existing: set[tuple[str, str, int, int]] = set()
        with self._lock:
            for gateway_id, pairs in rounds.items():
                node_ids = sorted({node for node, _ in pairs})
                round_ids = sorted({rd for _, rd in pairs})
                sql = (
                    "SELECT node_id, round_id, point_index FROM sweep_data "
                    "WHERE gateway_id=? AND node_id IN (%s) "
                    "AND round_id IN (%s)"
                    % (",".join("?" for _ in node_ids),
                       ",".join("?" for _ in round_ids))
                )
                rows = self._conn.execute(
                    sql, [gateway_id, *node_ids, *round_ids]).fetchall()
                for row in rows:
                    existing.add((gateway_id, row["node_id"], row["round_id"], row["point_index"]))
            fresh = [
                (p, index) for p, index in unique
                if (p.gateway_id, p.node_id, p.round_id, index) not in existing
            ]
            if not fresh:
                return 0
            self._conn.executemany(
                """INSERT INTO sweep_data
                    (report_id, gateway_id, node_id, round_id, timestamp, point_index, frequency_hz,
                     z_real, z_imag, magnitude, soil_moisture, temperature, nh3, h2s, co2, ph, humidity)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        p.report_id,
                        p.gateway_id,
                        p.node_id,
                        p.round_id,
                        p.timestamp,
                        index,
                        p.frequency_hz,
                        p.z_real,
                        p.z_imag,
                        p.magnitude,
                        p.soil_moisture,
                        p.temperature,
                        p.nh3,
                        p.h2s,
                        p.co2,
                        p.ph,
                        p.humidity,
                    )
                    for p, index in fresh
                ],
            )
            self._conn.commit()
        return len(fresh)

    # ---- 查询 ----

    def query_sensor_history(
        self,
        gateway_id: str,
        node_id: str,
        limit: int = 300,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sensor_data WHERE gateway_id=? AND node_id=?"
        params: list[Any] = [gateway_id, node_id]
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ts)
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in reversed(rows)]

    def query_impedance_history(
        self,
        gateway_id: str,
        node_id: str,
        scan_id: Optional[str] = None,
        limit: int = 1000,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM impedance_data WHERE gateway_id=? AND node_id=?"
        params: list[Any] = [gateway_id, node_id]
        if scan_id:
            sql += " AND scan_id=?"
            params.append(scan_id)
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ts)
        sql += " ORDER BY timestamp DESC, frequency_hz ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
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

    def count_sweep_rows(
        self,
        gateway_id: str,
        node_id: str,
        round_id: Optional[int] = None,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM sweep_data WHERE gateway_id=? AND node_id=?"
        params: list[Any] = [gateway_id, node_id]
        if round_id is not None:
            sql += " AND round_id=?"
            params.append(round_id)
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ts)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row else 0

    def query_sweep_history(
        self,
        gateway_id: str,
        node_id: str,
        limit: int = 10000,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        offset: int = 0,
        round_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sweep_data WHERE gateway_id=? AND node_id=?"
        params: list[Any] = [gateway_id, node_id]
        if round_id is not None:
            sql += " AND round_id=?"
            params.append(round_id)
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ts)
        sql += " ORDER BY timestamp DESC, round_id DESC, point_index ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_latest_sensor(self, gateway_id: str, node_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sensor_data WHERE gateway_id=? AND node_id=? ORDER BY timestamp DESC LIMIT 1",
                (gateway_id, node_id),
            ).fetchone()
        return dict(row) if row else None

    def query_sweep_round(
        self,
        gateway_id: str,
        node_id: str,
        round_id: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """取单轮完整扫频点，按点序号升序，供 Nyquist / Bode 直接绘图。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM sweep_data
                   WHERE gateway_id=? AND node_id=? AND round_id=?
                   ORDER BY point_index ASC LIMIT ?""",
                (gateway_id, node_id, round_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_sweep_rounds(
        self,
        gateway_id: str,
        node_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出该设备最近若干轮扫频及点数，用于选择要查看哪一轮。"""
        with self._lock:
              rows = self._conn.execute(
                  """SELECT round_id,
                            MAX(timestamp) AS timestamp,
                            COUNT(*) AS points,
                            AVG(magnitude) AS magnitude_mean,
                            MAX(report_id) AS report_id
                     FROM sweep_data
                     WHERE gateway_id=? AND node_id=?
                     GROUP BY round_id
                     ORDER BY timestamp DESC LIMIT ?""",
                  (gateway_id, node_id, limit),
              ).fetchall()
        return [dict(r) for r in rows]

    def latest_sweep_round_ts(
        self,
        gateway_id: str,
        node_id: str,
        round_id: Optional[int],
    ) -> int:
        """该轮最早入库的时间戳，用作 scan_id 的锚点。

        整轮出谱会清掉轮次缓冲，迟到的重复段（QoS1 重投）拿不到内存里的
        锚点了，得从库里取回，否则同一段会被写进另一个 scan_id。
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT MIN(timestamp) AS ts FROM sweep_data
                   WHERE gateway_id=? AND node_id=? AND round_id=?""",
                (gateway_id, node_id, round_id),
            ).fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else 0

    def list_impedance_scans(
        self,
        gateway_id: str,
        node_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出该设备最近若干次阻抗扫描及实测点数。

        每行的 rows 就是这次扫描实际落库的频点数，和 sweep_data 的
        轮次点数对上，才能判断"这一轮是不是收齐了"。
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT scan_id,
                          MAX(timestamp) AS timestamp,
                          COUNT(*) AS rows,
                          COUNT(DISTINCT frequency_hz) AS distinct_freqs,
                          MIN(frequency_hz) AS freq_lo,
                          MAX(frequency_hz) AS freq_hi,
                          AVG(magnitude) AS magnitude_mean
                   FROM impedance_data
                   WHERE gateway_id=? AND node_id=? AND scan_id IS NOT NULL
                         AND scan_id<>''
                   GROUP BY scan_id
                   ORDER BY timestamp DESC LIMIT ?""",
                (gateway_id, node_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def query_latest_prediction(self, node_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        sql = "SELECT * FROM prediction"
        params: list[Any] = []
        if node_id:
            sql += " WHERE node_id=?"
            params.append(node_id)
        sql += " ORDER BY timestamp DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def count_sensors(self, gateway_id: str, node_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM sensor_data WHERE gateway_id=? AND node_id=?",
                (gateway_id, node_id),
            ).fetchone()
        return row["cnt"] if row else 0

    def count_sensor_rows(
        self,
        gateway_id: str,
        node_id: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM sensor_data WHERE gateway_id=? AND node_id=?"
        params: list[Any] = [gateway_id, node_id]
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ts)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row else 0

    def count_impedance_rows(
        self,
        gateway_id: str,
        node_id: str,
        scan_id: Optional[str] = None,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM impedance_data WHERE gateway_id=? AND node_id=?"
        params: list[Any] = [gateway_id, node_id]
        if scan_id:
            sql += " AND scan_id=?"
            params.append(scan_id)
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ts)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row else 0

    def count_predictions(self, node_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM prediction"
        params: list[Any] = []
        if node_id:
            sql += " WHERE node_id=?"
            params.append(node_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row else 0

    def count_status_rows(
        self, gateway_id: Optional[str] = None, node_id: Optional[str] = None
    ) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM device_status"
        where: list[str] = []
        params: list[Any] = []
        if gateway_id:
            where.append("gateway_id=?")
            params.append(gateway_id)
        if node_id:
            where.append("node_id=?")
            params.append(node_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row else 0

    def list_devices(self) -> list[dict[str, Any]]:
        """列出出现过的设备，按最近一次上报时间排序。

        不能只看 sensor_data：网关的环境量是跟着扫频帧一起来的，
        环境历史还是空的时只看 sensor_data，设备下拉框会是空的，
        阻抗/扫频历史整个没法选设备。

        老库里还有 gateway_id / node_id 为空的残缺上报，那种记录拿任何
        设备条件都查不出来，放进下拉框只是噪声。
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT gateway_id, node_id, MAX(timestamp) as last_seen
                   FROM (
                       SELECT gateway_id, node_id, timestamp FROM sensor_data
                       UNION ALL
                       SELECT gateway_id, node_id, timestamp FROM impedance_data
                   )
                   WHERE gateway_id <> '' AND node_id <> ''
                   GROUP BY gateway_id, node_id
                   ORDER BY last_seen DESC""",
            ).fetchall()
        return [dict(r) for r in rows]
