"""程序入口：支持 --mqtt / --simulator 两种运行模式。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from db import Database
from gui import MainWindow
from mqtt_client import MQTTWorker, SimulatorWorker


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "config.json"


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_db_path(config: dict, config_path: Path) -> Path:
    raw_path = config.get("database", {}).get("path", "data/fruit_monitor.db")
    path = Path(raw_path)
    return path if path.is_absolute() else (config_path.parent.parent / path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="水果成熟度实时监测系统")
    parser.add_argument("--mqtt", action="store_true", help="连接真实 MQTT Broker")
    parser.add_argument("--simulator", action="store_true", help="使用内置数据模拟器")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    parser.add_argument("--db", type=Path, default=None, help="SQLite 数据库路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    use_simulator = not args.mqtt or args.simulator
    config = load_config(args.config)
    db_path = args.db or resolve_db_path(config, args.config)
    db = Database(db_path)

    app = QApplication(sys.argv)
    app.setApplicationName("Fruit Monitor")

    worker: QThread
    if use_simulator:
        worker = SimulatorWorker(config, db)
        mode_label = "模拟模式"
    else:
        worker = MQTTWorker(config, db)
        mode_label = "MQTT 模式"

    window = MainWindow(config, db, worker)
    window._mode_label.setText(mode_label)
    window.show()

    worker.start()
    exit_code = app.exec()

    try:
        window.close()
    except Exception:
        pass

    worker.stop()
    worker.wait(3000)
    db.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
