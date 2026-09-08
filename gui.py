"""PySide6 GUI 主窗口 + PyQtGraph 实时曲线。"""
from __future__ import annotations

import collections
import time
from datetime import datetime

import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFrame,
    QLabel,
    QPushButton,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QDialog,
    QSpinBox,
    QFileDialog,
    QCheckBox,
)

from protocol import ImpedanceData, SensorData


SENSOR_FIELDS: list[tuple[str, str, str, int]] = [
    ("temperature", "温度", "°C", 2),
    ("humidity", "湿度", "%RH", 1),
    ("co2", "CO2", "ppm", 0),
    ("ph", "pH", "", 2),
    ("nh3", "NH3", "ppm", 1),
    ("h2s", "H2S", "ppm", 1),
    ("soil_moisture", "土壤湿度", "%", 1),
]

SENSOR_COLORS: dict[str, str] = {
    "temperature": "#f9a23c",
    "humidity": "#4a90d9",
    "co2": "#91CB74",
    "ph": "#EE6666",
    "nh3": "#73C0DE",
    "h2s": "#FC8452",
    "soil_moisture": "#9A60B4",
}



class SensorCard(QFrame):
    def __init__(self, name: str, unit: str, parent=None):
        super().__init__(parent)
        self._unit = unit
        self.setFixedHeight(78)
        self._apply_style(normal=True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        self._name_label = QLabel(name)
        self._name_label.setStyleSheet("color:#8a9099; font-size:12px; font-weight:500;")
        lay.addWidget(self._name_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self._value_label = QLabel("--")
        self._value_label.setStyleSheet("font-size:24px; font-weight:700; color:#2b2f33;")
        row.addWidget(self._value_label)
        if unit:
            self._unit_label = QLabel(unit)
            self._unit_label.setStyleSheet("color:#8a9099; font-size:12px; margin-left:4px;")
            row.addWidget(self._unit_label)
        row.addStretch()
        lay.addLayout(row)

        self._anomaly_label = QLabel("")
        self._anomaly_label.setStyleSheet("color:#f56c6c; font-size:10px;")
        lay.addWidget(self._anomaly_label)

    def set_value(self, value, anomaly: bool = False) -> None:
        if value is None:
            self._value_label.setText("--")
        elif isinstance(value, float):
            self._value_label.setText(f"{value:.2f}")
        elif isinstance(value, int):
            self._value_label.setText(str(value))
        else:
            self._value_label.setText(str(value))
        self._apply_style(normal=not anomaly)

    def set_anomaly_text(self, text: str) -> None:
        self._anomaly_label.setText(text)

    def _apply_style(self, normal: bool) -> None:
        if normal:
            self.setStyleSheet(
                "SensorCard{background:#fff;border-radius:10px;border:1px solid #e6e9ef;}"
            )
        else:
            self.setStyleSheet(
                "SensorCard{background:#fff5f0;border-radius:10px;border:1px solid #f56c6c;}"
            )


class RealTimeChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._max_points = 300
        self._data: dict[str, collections.deque] = {
            key: collections.deque(maxlen=self._max_points) for key, *_ in SENSOR_FIELDS
        }
        self._multi_mode = False
        self._current_sensor = "temperature"
        self._curves: dict[str, pg.PlotDataItem] = {}

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._combo = QComboBox()
        for key, label, *_ in SENSOR_FIELDS:
            self._combo.addItem(label, key)
        self._combo.currentIndexChanged.connect(self._on_sensor_changed)
        header.addWidget(self._combo)

        self._multi_check = QCheckBox("多曲线模式")
        self._multi_check.stateChanged.connect(self._on_mode_changed)
        header.addWidget(self._multi_check)
        header.addStretch()

        self._plot = pg.PlotWidget()
        self._plot.setBackground("w")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setLabel("bottom", "时间")
        self._plot.setAxisItems({"bottom": pg.DateAxisItem()})
        self._plot.setMinimumHeight(240)

        for key, label, unit, _ in SENSOR_FIELDS:
            curve = pg.PlotDataItem(pen=pg.mkPen(SENSOR_COLORS[key], width=2))
            curve.setVisible(key == self._current_sensor)
            self._plot.addItem(curve)
            self._curves[key] = curve

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(header)
        lay.addWidget(self._plot)

    def add_data(self, data: SensorData) -> None:
        ts = data.timestamp
        for key, *_ in SENSOR_FIELDS:
            val = getattr(data, key, None)
            if val is not None:
                self._data[key].append((ts, float(val)))
        self._update_curves()

    def _update_curves(self) -> None:
        for key in self._data:
            items = self._data[key]
            if not items:
                continue
            times = [d[0] for d in items]
            values = [d[1] for d in items]
            self._curves[key].setData(times, values)
            if self._multi_mode:
                self._curves[key].setVisible(True)
            else:
                self._curves[key].setVisible(key == self._current_sensor)
        for sk, label, unit, _ in SENSOR_FIELDS:
            if sk == self._current_sensor:
                self._plot.setLabel("left", f"{label} ({unit})" if unit else label)
                break

    def _on_sensor_changed(self, index: int) -> None:
        self._current_sensor = self._combo.itemData(index)
        self._update_curves()

    def _on_mode_changed(self, state: int) -> None:
        self._multi_mode = bool(state)
        self._update_curves()

    def clear(self) -> None:
        for key in self._data:
            self._data[key].clear()
            self._curves[key].clear()


class HistoryDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("历史数据查询")
        self.resize(880, 520)

        lay = QVBoxLayout(self)

        query = QHBoxLayout()
        query.addWidget(QLabel("网关:"))
        self._gw = QComboBox()
        query.addWidget(self._gw)
        query.addWidget(QLabel("节点:"))
        self._node = QComboBox()
        query.addWidget(self._node)
        query.addWidget(QLabel("最近(小时):"))
        self._hours = QSpinBox()
        self._hours.setRange(1, 720)
        self._hours.setValue(24)
        query.addWidget(self._hours)
        query.addWidget(QLabel("条数:"))
        self._limit = QSpinBox()
        self._limit.setRange(10, 10000)
        self._limit.setValue(500)
        query.addWidget(self._limit)
        query.addStretch()
        self._query_btn = QPushButton("查询")
        self._query_btn.clicked.connect(self._do_query)
        query.addWidget(self._query_btn)
        lay.addLayout(query)

        self._table = QTableWidget()
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._export_btn = QPushButton("导出CSV")
        self._export_btn.clicked.connect(self._export)
        btn_row.addWidget(self._export_btn)
        lay.addLayout(btn_row)

        self._rows: list[dict] = []
        self._load_devices()

    def _load_devices(self) -> None:
        devices = self._db.list_devices()
        gws = sorted({d["gateway_id"] for d in devices})
        nodes = sorted({d["node_id"] for d in devices})
        self._gw.addItems(gws)
        self._node.addItems(nodes)

    def _do_query(self) -> None:
        gw = self._gw.currentText()
        node = self._node.currentText()
        if not gw or not node:
            QMessageBox.information(self, "提示", "请等待数据入库后再查询")
            return
        hours = self._hours.value()
        end_ts = int(time.time())
        start_ts = end_ts - hours * 3600
        self._rows = self._db.query_sensor_history(
            gw, node, limit=self._limit.value(), start_ts=start_ts, end_ts=end_ts
        )
        self._show_table()

    def _show_table(self) -> None:
        self._table.clear()
        if not self._rows:
            return
        keys = list(self._rows[0].keys())
        self._table.setColumnCount(len(keys))
        self._table.setHorizontalHeaderLabels(keys)
        self._table.setRowCount(len(self._rows))
        for r_idx, row in enumerate(self._rows):
            for c_idx, key in enumerate(keys):
                val = row[key]
                if key == "timestamp" and isinstance(val, (int, float)):
                    val = datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S")
                elif isinstance(val, float):
                    val = f"{val:.2f}"
                item = QTableWidgetItem(str(val) if val is not None else "")
                self._table.setItem(r_idx, c_idx, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )

    def _export(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = QFileDialog.getSaveFileName(
            self, "导出CSV", f"sensor_{ts}.csv", "CSV files (*.csv)"
        )[0]
        if filepath:
            df = pd.DataFrame(self._rows)
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
            QMessageBox.information(self, "导出成功", f"已保存 {len(df)} 条记录")


class MainWindow(QMainWindow):
    def __init__(self, config: dict, db, worker) -> None:
        super().__init__()
        self._config = config
        self._db = db
        self._worker = worker
        self._current_gw = ""
        self._current_node = ""
        self._recording = True
        self._msg_count = 0
        self._latest_impedance: ImpedanceData | None = None

        ui_cfg = config.get("ui", {})
        self.setWindowTitle("水果成熟度实时监测系统")
        self.resize(ui_cfg.get("window_width", 1100), ui_cfg.get("window_height", 720))
        self.setStyleSheet("QMainWindow{background:#f2f4f8;}")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(10)

        root.addWidget(self._build_header())
        root.addWidget(self._build_cards())
        root.addWidget(self._build_chart(), stretch=1)
        root.addWidget(self._build_controls())

        worker.data_received.connect(self._on_data)
        worker.status_changed.connect(self._on_status)
        worker.connected.connect(self._on_connected)
        if hasattr(worker, "error_occurred"):
            worker.error_occurred.connect(self._on_error)

        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(2000)

        self._close_event = None

    # ---- 构建 UI ----

    def _build_header(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(52)
        bar.setStyleSheet(
            "QFrame{background:#fff;border-radius:10px;border:1px solid #e6e9ef;}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)

        self._title = QLabel("水果成熟度实时监测系统")
        self._title.setStyleSheet("font-size:16px; font-weight:700; color:#2b2f33;")
        lay.addWidget(self._title)
        lay.addStretch()

        self._gw_label = QLabel("网关: --")
        self._gw_label.setStyleSheet("font-size:12px; color:#5a6068;")
        lay.addWidget(self._gw_label)

        self._node_label = QLabel("节点: --")
        self._node_label.setStyleSheet("font-size:12px; color:#5a6068;")
        lay.addWidget(self._node_label)

        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet("font-size:14px; color:#d0d4da;")
        lay.addWidget(self._status_dot)

        self._update_label = QLabel("更新: --")
        self._update_label.setStyleSheet("font-size:11px; color:#8a9099;")
        lay.addWidget(self._update_label)

        return bar

    def _build_cards(self) -> QFrame:
        wrap = QFrame()
        grid = QGridLayout(wrap)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)

        self._cards: dict[str, SensorCard] = {}
        for idx, (key, name, unit, _) in enumerate(SENSOR_FIELDS):
            card = SensorCard(name, unit)
            self._cards[key] = card
            grid.addWidget(card, idx // 4, idx % 4)

        # 阻抗卡片
        self._impedance_card = SensorCard("阻抗", "Ω", wrap)
        grid.addWidget(self._impedance_card, 1, 3)

        return wrap

    def _build_chart(self) -> QFrame:
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame{background:#fff;border-radius:10px;border:1px solid #e6e9ef;}"
        )
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(8, 8, 8, 8)
        self._chart = RealTimeChart()
        lay.addWidget(self._chart)
        return wrap

    def _build_controls(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(48)
        bar.setStyleSheet(
            "QFrame{background:#fff;border-radius:10px;border:1px solid #e6e9ef;}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 12, 0)

        self._rec_btn = QPushButton("停止记录")
        self._rec_btn.setFixedWidth(90)
        self._rec_btn.clicked.connect(self._toggle_recording)
        lay.addWidget(self._rec_btn)

        self._hist_btn = QPushButton("历史数据")
        self._hist_btn.setFixedWidth(90)
        self._hist_btn.clicked.connect(self._open_history)
        lay.addWidget(self._hist_btn)

        self._export_btn = QPushButton("导出CSV")
        self._export_btn.setFixedWidth(90)
        self._export_btn.clicked.connect(self._export_csv)
        lay.addWidget(self._export_btn)

        self._clear_btn = QPushButton("清空曲线")
        self._clear_btn.setFixedWidth(90)
        self._clear_btn.clicked.connect(self._chart.clear)
        lay.addWidget(self._clear_btn)

        lay.addStretch()

        self._msg_label = QLabel("消息: 0")
        self._msg_label.setStyleSheet("font-size:11px; color:#8a9099;")
        lay.addWidget(self._msg_label)

        self._mode_label = QLabel("")
        self._mode_label.setStyleSheet("font-size:11px; color:#8a9099;")
        lay.addWidget(self._mode_label)

        return bar

    # ---- 事件处理 ----

    def _on_data(self, payload: dict) -> None:
        ptype = payload["type"]
        data = payload["data"]

        if ptype == "sensor":
            self._msg_count += 1
            self._current_gw = data.gateway_id
            self._current_node = data.node_id
            self._update_sensor_cards(data)
            self._chart.add_data(data)
            now_str = datetime.fromtimestamp(data.timestamp).strftime("%H:%M:%S")
            self._update_label.setText(f"更新: {now_str}")

        elif ptype == "impedance":
            self._msg_count += 1
            self._latest_impedance = data
            mag = data.magnitude
            if mag is not None:
                self._impedance_card.set_value(round(mag, 1))

    def _on_status(self, gw: str, node: str, status: str) -> None:
        if gw:
            self._current_gw = gw
            self._gw_label.setText(f"网关: {gw}")
        if node:
            self._current_node = node
            self._node_label.setText(f"节点: {node}")
        if status == "online":
            self._status_dot.setStyleSheet("font-size:14px; color:#07c160;")
        elif status == "offline":
            self._status_dot.setStyleSheet("font-size:14px; color:#f56c6c;")

    def _on_connected(self, connected: bool) -> None:
        if connected:
            self._status_dot.setStyleSheet("font-size:14px; color:#07c160;")
        else:
            self._status_dot.setStyleSheet("font-size:14px; color:#d0d4da;")

    def _on_error(self, msg: str) -> None:
        self._mode_label.setText(f"⚠ {msg}")

    def _update_sensor_cards(self, data: SensorData) -> None:
        for key, name, unit, prec in SENSOR_FIELDS:
            card = self._cards[key]
            val = getattr(data, key, None)
            card.set_value(val, anomaly=False)
            card.set_anomaly_text("")
        if data.anomaly_flags:
            for flag in data.anomaly_flags:
                for key, *_ in SENSOR_FIELDS:
                    if flag.startswith(key):
                        self._cards[key].set_anomaly_text("⚠ 异常")
                        break

    def _toggle_recording(self) -> None:
        self._recording = not self._recording
        self._worker.set_recording(self._recording)
        self._rec_btn.setText("停止记录" if self._recording else "开始记录")
        self._rec_btn.setStyleSheet(
            "QPushButton{background:#07c160;color:#fff;border-radius:8px;padding:6px 12px;}"
            if self._recording
            else "QPushButton{background:#8a9099;color:#fff;border-radius:8px;padding:6px 12px;}"
        )

    def _open_history(self) -> None:
        dlg = HistoryDialog(self._db, self)
        dlg.exec()

    def _export_csv(self) -> None:
        gw = self._current_gw
        node = self._current_node
        if not gw or not node:
            QMessageBox.warning(self, "提示", "暂无数据，请先开始记录")
            return
        rows = self._db.query_sensor_history(gw, node, limit=10000)
        if not rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = QFileDialog.getSaveFileName(
            self, "导出CSV", f"{gw}_{node}_{ts}.csv", "CSV files (*.csv)"
        )[0]
        if filepath:
            df = pd.DataFrame(rows)
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
            QMessageBox.information(self, "导出成功", f"已保存 {len(df)} 条记录到\n{filepath}")

    def _update_stats(self) -> None:
        self._msg_label.setText(f"消息: {self._msg_count}")

    def closeEvent(self, event) -> None:
        try:
            self._worker.stop()
            self._worker.wait(3000)
        except Exception:
            pass
        event.accept()
