"""PySide6 GUI 主窗口 + PyQtGraph 实时曲线。"""
from __future__ import annotations

import collections
import math
import time
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QTabWidget,
    QFrame,
    QLabel,
    QPushButton,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QSpinBox,
    QFileDialog,
    QCheckBox,
    QProgressBar,
    QGroupBox,
    QSplitter,
)

from maturity import LEVEL_LABELS, level_label
from protocol import (
    MIN_REPORT_POINTS,
    ImpedanceData,
    ImpedanceSpectrum,
    PredictionData,
    SensorData,
    SweepPointData,
)
from spectrum import (
    bode_series,
    characteristic_frequency,
    nyquist_series,
    sweep_points_to_spectrum,
)


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

# 历史数据表的列序与显示精度。顺序是显式声明的，不受 sqlite ALTER TABLE
# 把新列追加到末尾的影响；表头保持字段原名。
SENSOR_HISTORY_COLUMNS: list[tuple[str, int]] = [
    ("id", 0),
    ("gateway_id", 0),
    ("node_id", 0),
    ("timestamp", 0),
    ("round_id", 0),
    ("temperature", 2),
    ("humidity", 1),
    ("soil_moisture", 1),
    ("nh3", 1),
    ("h2s", 1),
    ("co2", 0),
    ("ph", 2),
    ("frequency_hz", 1),
    ("z_real", 1),
    ("z_imag", 1),
    ("magnitude", 1),
    ("phase", 2),
]

SENSOR_HISTORY_DECIMALS: dict[str, int] = {
    field: n for field, n in SENSOR_HISTORY_COLUMNS
}


TREND_UP = "#e64545"
TREND_DOWN = "#07c160"
TREND_FLAT = "#8a9099"

NYQUIST_COLORMAP = "viridis"
BODE_MAG_COLOR = "#4a90d9"
BODE_PHASE_COLOR = "#f56c6c"

MATURITY_COLORS: dict[str, str] = {
    "unripe": "#4a90d9",
    "ripening": "#91CB74",
    "ripe": "#f9a23c",
    "overripe": "#f56c6c",
}

MATURITY_ORDER: list[tuple[str, str]] = [
    ("unripe", "未成熟"),
    ("ripening", "转熟期"),
    ("ripe", "成熟期"),
    ("overripe", "过熟期"),
]


class StatusLight(QWidget):
    """常驻连接状态指示：绿=连接正常且有数据流，黄=中断重连，灰=设备离线。"""

    COLORS = {
        "online": (7, 193, 96),
        "reconnecting": (250, 173, 20),
        "offline": (160, 166, 175),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self._state = "offline"
        self._label = "设备离线"
        self._phase = 0.0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._dot = QLabel("●")
        self._dot.setStyleSheet("font-size:14px; color:#a0a6af;")
        lay.addWidget(self._dot)
        self._text = QLabel(self._label)
        self._text.setStyleSheet("font-size:11px; color:#5a6068;")
        lay.addWidget(self._text)
        lay.addStretch()

        timer = QTimer(self)
        timer.setInterval(40)
        timer.timeout.connect(self._tick)
        timer.start()

    def set_state(self, state: str, label: str | None = None) -> None:
        if state not in self.COLORS:
            state = "offline"
        self._state = state
        if label is not None:
            self._label = label
        self._text.setText(self._label)

    def _tick(self) -> None:
        self._phase += 0.18
        r, g, b = self.COLORS[self._state]
        if self._state == "online":
            alpha = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(self._phase))
        elif self._state == "reconnecting":
            alpha = 1.0 if math.sin(self._phase * 2.0) > -0.2 else 0.2
        else:
            alpha = 0.55
        self._dot.setStyleSheet(
            f"font-size:14px; color:rgba({r},{g},{b},{alpha:.2f});"
        )


class FirstFrameHint(QLabel):
    """等待首包数据时的占位提示，带轻微呼吸，收到数据后自动隐藏。"""

    def __init__(self, text: str, parent: QWidget):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._armed = True
        self._phase = 0.0
        self.setStyleSheet("color:rgba(138,144,153,0.70); font-size:13px;")

        timer = QTimer(self)
        timer.setInterval(90)
        timer.timeout.connect(self._tick)
        timer.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._armed and self.parentWidget() is not None:
            self.setGeometry(self.parentWidget().rect())

    def dismiss(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self.hide()

    def _tick(self) -> None:
        if not self._armed:
            return
        self._phase += 0.16
        alpha = 0.30 + 0.45 * (0.5 + 0.5 * math.sin(self._phase))
        self.setStyleSheet(
            f"color:rgba(138,144,153,{alpha:.2f}); font-size:13px;"
        )


class ArcGauge(QWidget):
    """半圆仪表盘，用于展示采摘预测置信度。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0.0
        self._caption = "0%"
        self._color = "#8a9099"
        self.setMinimumSize(200, 120)

    def set_value(self, value: float, caption: str = "", color: str = "") -> None:
        self._value = max(0.0, min(1.0, float(value)))
        self._caption = caption
        if color:
            self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(14, 14, self.width() - 28, (self.width() - 28) * 0.86)
        pen = QPen(Qt.GlobalColor.transparent)
        pen.setWidthF(13)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)

        bg = QPen(Qt.GlobalColor.transparent)
        bg.setWidthF(13)
        bg.setColor(pg.mkColor("#e6e9ef"))
        bg.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(bg)
        painter.drawArc(rect, 180 * 16, -180 * 16)

        span = int(-180 * 16 * self._value)
        if span < 0:
            pen.setColor(pg.mkColor(self._color))
            painter.setPen(pen)
            painter.drawArc(rect, 180 * 16, span)

        painter.setPen(pg.mkColor(self._color))
        painter.setFont(pg.QtGui.QFont("Microsoft YaHei", 17, pg.QtGui.QFont.Weight.Bold))
        painter.drawText(
            QRectF(0, rect.bottom() - 46, self.width(), 34),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            self._caption,
        )
        painter.end()



class SensorCard(QFrame):
    def __init__(self, name: str, unit: str, parent=None):
        super().__init__(parent)
        self._unit = unit
        self._prev_value: float | None = None
        # 卡内实际只有标题、数值、趋势三行，180 高留了快一倍空白；
        # 两行卡片白占 360，把下面图区挤到只剩百来像素。
        self.setFixedHeight(140)
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

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        self._trend_label = QLabel("—")
        self._trend_label.setStyleSheet(f"color:{TREND_FLAT}; font-size:10px; font-weight:700;")
        bottom.addWidget(self._trend_label)
        bottom.addStretch()
        self._anomaly_label = QLabel("")
        self._anomaly_label.setStyleSheet("color:#f56c6c; font-size:10px;")
        bottom.addWidget(self._anomaly_label)
        lay.addLayout(bottom)

    def set_value(self, value, anomaly: bool = False) -> None:
        prev = self._prev_value
        current: float | None = None
        if isinstance(value, bool):
            current = float(value)
        elif isinstance(value, (int, float)):
            current = float(value)

        if value is None:
            self._value_label.setText("--")
        elif isinstance(value, float):
            self._value_label.setText(f"{value:.2f}")
        elif isinstance(value, int):
            self._value_label.setText(str(value))
        else:
            self._value_label.setText(str(value))

        self._prev_value = current
        self._update_trend(prev, current)
        self._apply_style(normal=not anomaly)

    def _update_trend(self, prev: float | None, current: float | None) -> None:
        if prev is None or current is None:
            self._trend_label.setText("—")
            self._trend_label.setStyleSheet(
                f"color:{TREND_FLAT}; font-size:10px; font-weight:700;"
            )
            return
        delta = current - prev
        if abs(delta) < 1e-9:
            text, color = "— 持平", TREND_FLAT
        elif delta > 0:
            text, color = f"▲ +{abs(delta):g}", TREND_UP
        else:
            text, color = f"▼ {delta:g}", TREND_DOWN
        self._trend_label.setText(text)
        self._trend_label.setStyleSheet(
            f"color:{color}; font-size:10px; font-weight:700;"
        )

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
        # 只给一个很小的下限：图撑不满窗口时允许被压扁。
        # 之前设 240/320 这种硬下限，三个页签里阻抗谱页要 400 多，
        # 整个窗口最小高度被顶到 976；屏幕放不下的时候，底部那排功能键
        # 就被挤出可视区，切一下页签才能把布局挤回去。
        self._plot.setMinimumHeight(120)

        for key, label, unit, _ in SENSOR_FIELDS:
            curve = pg.PlotDataItem(pen=pg.mkPen(SENSOR_COLORS[key], width=2))
            curve.setVisible(key == self._current_sensor)
            self._plot.addItem(curve)
            self._curves[key] = curve

        self._hint = FirstFrameHint("等待首包数据…", self._plot)
        self._hint.show()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(header)
        lay.addWidget(self._plot, stretch=1)

    def add_data(self, data: SensorData) -> None:
        ts = data.timestamp
        self._hint.dismiss()
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


class ColumnPickerDialog(QDialog):
    def __init__(self, columns: list[str], selected: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择导出列")
        self._columns = columns
        self._selected = set(selected or columns)
        self.resize(320, 460)
        self._checkboxes: dict[str, QCheckBox] = {}

        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("要导出的列"))
        top.addStretch()
        self._search = QLineEdit()
        self._search.setPlaceholderText("筛选列名")
        self._search.textChanged.connect(self._filter)
        top.addWidget(self._search)
        lay.addLayout(top)

        actions = QHBoxLayout()
        self._all_btn = QPushButton("全选")
        self._all_btn.clicked.connect(lambda: self._set_all(True))
        actions.addWidget(self._all_btn)
        self._none_btn = QPushButton("清空")
        self._none_btn.clicked.connect(lambda: self._set_all(False))
        actions.addWidget(self._none_btn)
        lay.addLayout(actions)

        box = QVBoxLayout()
        for col in columns:
            cb = QCheckBox(col)
            cb.setChecked(col in self._selected)
            cb.setProperty("column", col)
            box.addWidget(cb)
            self._checkboxes[col] = cb
        lay.addLayout(box)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._filter("")

    def _set_all(self, checked: bool) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(checked)

    def _filter(self, text: str) -> None:
        text = text.strip().lower()
        for col, cb in self._checkboxes.items():
            cb.setVisible(not text or text in col.lower())

    def selected_columns(self) -> list[str]:
        return [col for col, cb in self._checkboxes.items() if cb.isChecked()]


def page_geometry(page_size_box, page_box) -> tuple[int, int]:
    """当前页大小与页号：页大小夹在 10~1000，页号至少 1。"""
    return max(10, min(1000, int(page_size_box.value()))), max(1, int(page_box.value()))


def sync_page_controls(page_box, total: int, page_size: int, prev_btn, next_btn) -> int:
    """统一翻页控件状态，返回总页数。

    setRange 只在范围真的变了才发 valueChanged；不加这个判断的话，
    每次渲染都会再触发一次翻页，等于重复查库。
    """
    total_pages = max(1, (total + page_size - 1) // page_size if total else 1)
    if (page_box.minimum(), page_box.maximum()) != (1, total_pages):
        page_box.setRange(1, total_pages)
    if page_box.value() < 1:
        page_box.setValue(1)
    elif page_box.value() > total_pages:
        page_box.setValue(total_pages)
    prev_btn.setEnabled(page_box.value() > 1)
    next_btn.setEnabled(page_box.value() < total_pages)
    return total_pages


class SweepDialog(QDialog):
    def __init__(self, db, parent=None, default_gw: str = "", default_node: str = ""):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("扫描详情")
        self.resize(1080, 640)

        lay = QVBoxLayout(self)

        query = QHBoxLayout()
        query.addWidget(QLabel("网关:"))
        self._gw = QComboBox()
        query.addWidget(self._gw)
        query.addWidget(QLabel("节点:"))
        self._node = QComboBox()
        query.addWidget(self._node)
        query.addWidget(QLabel("轮次:"))
        self._round = QSpinBox()
        self._round.setRange(0, 10_000_000)
        query.addWidget(self._round)
        self._all_rounds = QCheckBox("全部轮次")
        self._all_rounds.setChecked(True)
        query.addWidget(self._all_rounds)
        query.addWidget(QLabel("每页"))
        self._page_size_box = QSpinBox()
        self._page_size_box.setRange(10, 1000)
        self._page_size_box.setSingleStep(10)
        self._page_size_box.setValue(50)
        query.addWidget(self._page_size_box)
        query.addWidget(QLabel("页"))
        self._page = QSpinBox()
        self._page.setRange(1, 1)
        self._page.setValue(1)
        query.addWidget(self._page)
        self._prev_btn = QPushButton("<")
        self._prev_btn.setEnabled(False)
        self._prev_btn.clicked.connect(self._prev_page)
        query.addWidget(self._prev_btn)
        self._next_btn = QPushButton(">")
        self._next_btn.setEnabled(False)
        self._next_btn.clicked.connect(self._next_page)
        query.addWidget(self._next_btn)
        query.addStretch()
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._do_query)
        query.addWidget(self._refresh_btn)
        lay.addLayout(query)

        self._info_label = QLabel("等待加载...")
        self._info_label.setStyleSheet("font-size:12px; color:#5a6068;")
        lay.addWidget(self._info_label)

        self._table = QTableWidget()
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._all_btn = QPushButton("全选行")
        self._all_btn.clicked.connect(lambda: self._select_all_rows(True))
        btn_row.addWidget(self._all_btn)
        self._clear_btn = QPushButton("清空选择")
        self._clear_btn.clicked.connect(lambda: self._select_all_rows(False))
        btn_row.addWidget(self._clear_btn)
        self._cols_btn = QPushButton("选择列")
        self._cols_btn.clicked.connect(self._pick_columns)
        btn_row.addWidget(self._cols_btn)
        self._export_btn = QPushButton("导出CSV")
        self._export_btn.clicked.connect(self._export)
        btn_row.addWidget(self._export_btn)
        lay.addLayout(btn_row)

        self._rows: list[dict] = []
        self._page_rows: list[dict] = []
        self._selected_columns: list[str] = []
        self._load_devices(default_gw, default_node)
        self._page.valueChanged.connect(lambda _v: self._render_page())
        self._page_size_box.valueChanged.connect(self._on_page_size_changed)

    def _load_devices(self, default_gw: str, default_node: str) -> None:
        devices = self._db.list_devices()
        gws = sorted({d["gateway_id"] for d in devices})
        nodes = sorted({d["node_id"] for d in devices})
        self._gw.addItems(gws)
        self._node.addItems(nodes)
        if default_gw in gws:
            self._gw.setCurrentText(default_gw)
        if default_node in nodes:
            self._node.setCurrentText(default_node)
        self._default_gw = default_gw
        self._default_node = default_node
        self._round.setValue(self._latest_round(default_gw, default_node))

    def _latest_round(self, gw: str, node: str) -> int:
        rows = self._db.query_sweep_history(gw, node, limit=1)
        return int(rows[0].get("round_id", 0)) if rows else 0

    def _do_query(self) -> None:
        gw = self._gw.currentText()
        node = self._node.currentText()
        if not gw or not node:
            QMessageBox.information(self, "提示", "请等待数据入库后再查询")
            return
        round_id = None if self._all_rounds.isChecked() else int(self._round.value())
        self._total_count = self._db.count_sweep_rows(gw, node, round_id=round_id)
        self._page.setValue(1)
        self._render_page()

    def _fetch_page(self, page_size: int, page: int) -> list[dict]:
        """翻页必须回库取当前页。

        一次只取一页的话内存里没有后面的页，翻过去切出来就是空表。
        库里按时间倒序排，这里直接沿用它的顺序，翻页才连续。
        """
        round_id = None if self._all_rounds.isChecked() else int(self._round.value())
        return self._db.query_sweep_history(
            self._gw.currentText(), self._node.currentText(),
            limit=page_size, offset=(page - 1) * page_size, round_id=round_id)

    def _render_page(self) -> None:
        page_size, page = page_geometry(self._page_size_box, self._page)
        rows = self._fetch_page(page_size, page)
        self._rows = rows
        self._page_rows = rows
        total = getattr(self, "_total_count", 0)
        total_pages = sync_page_controls(
            self._page, total, page_size, self._prev_btn, self._next_btn)
        round_ids = sorted({int(r.get("round_id", 0)) for r in rows})
        round_text = ", ".join(str(r) for r in round_ids[:10])
        if len(round_ids) > 10:
            round_text += "..."
        self._info_label.setText(
            f"轮次: {round_text} · 共 {total} 行 · 第 {self._page.value()}/{total_pages} 页")
        self._show_table(rows)

    def _prev_page(self) -> None:
        self._page.setValue(max(1, self._page.value() - 1))

    def _next_page(self) -> None:
        self._page.setValue(min(self._page.maximum(), self._page.value() + 1))

    def _on_page_size_changed(self) -> None:
        self._page.setValue(1)
        self._render_page()

    def _show_table(self, rows: list[dict]) -> None:
        self._table.clear()
        if not rows:
            self._info_label.setText("暂无扫描数据")
            return
        keys = [
            "round_id",
            "point_index",
            "frequency_hz",
            "z_real",
            "z_imag",
            "magnitude",
            "soil_moisture",
            "temperature",
            "nh3",
            "h2s",
            "co2",
            "ph",
            "humidity",
            "timestamp",
        ]
        self._table.setColumnCount(len(keys) + 1)
        headers = ["选择"] + keys
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Unchecked)
            self._table.setItem(r_idx, 0, check)
            for c_idx, key in enumerate(keys):
                val = row.get(key)
                if key == "timestamp" and isinstance(val, (int, float)):
                    val = datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S")
                elif isinstance(val, float):
                    val = f"{val:.2f}"
                item = QTableWidgetItem(str(val) if val is not None else "")
                self._table.setItem(r_idx, c_idx + 1, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )

    def _select_all_rows(self, checked: bool = True) -> None:
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _pick_columns(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        keys = [
            "round_id",
            "point_index",
            "frequency_hz",
            "z_real",
            "z_imag",
            "magnitude",
            "soil_moisture",
            "temperature",
            "nh3",
            "h2s",
            "co2",
            "ph",
            "humidity",
            "timestamp",
        ]
        chosen = self._selected_columns or keys
        dlg = ColumnPickerDialog(keys, chosen, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._selected_columns = dlg.selected_columns()

    def _export(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        selected_rows = []
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                if r < len(self._page_rows):
                    selected_rows.append(self._page_rows[r])
        if not selected_rows:
            QMessageBox.information(self, "提示", "请勾选至少一行")
            return
        cols = self._selected_columns or list(selected_rows[0].keys())
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = QFileDialog.getSaveFileName(
            self, "导出CSV", f"sweep_{ts}.csv", "CSV files (*.csv)"
        )[0]
        if filepath:
            df = pd.DataFrame(selected_rows)[cols]
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
            QMessageBox.information(self, "导出成功", f"已保存 {len(df)} 条记录到\n{filepath}")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._rows:
            self._do_query()


class ImpedanceDialog(QDialog):
    def __init__(self, db, parent=None, default_gw: str = "", default_node: str = ""):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("阻抗历史")
        self.resize(1120, 680)

        lay = QVBoxLayout(self)

        query = QHBoxLayout()
        query.addWidget(QLabel("网关:"))
        self._gw = QComboBox()
        query.addWidget(self._gw)
        query.addWidget(QLabel("节点:"))
        self._node = QComboBox()
        query.addWidget(self._node)
        query.addWidget(QLabel("最近(小时, 0=全部):"))
        self._hours = QSpinBox()
        self._hours.setRange(0, 720)
        self._hours.setSpecialValueText("全部")
        self._hours.setValue(24)
        query.addWidget(self._hours)
        query.addWidget(QLabel("每页"))
        self._page_size_box = QSpinBox()
        self._page_size_box.setRange(10, 1000)
        self._page_size_box.setSingleStep(10)
        self._page_size_box.setValue(50)
        query.addWidget(self._page_size_box)
        query.addWidget(QLabel("页"))
        self._page = QSpinBox()
        self._page.setRange(1, 1)
        self._page.setValue(1)
        query.addWidget(self._page)
        self._prev_btn = QPushButton("<")
        self._prev_btn.setEnabled(False)
        self._prev_btn.clicked.connect(self._prev_page)
        query.addWidget(self._prev_btn)
        self._next_btn = QPushButton(">")
        self._next_btn.setEnabled(False)
        self._next_btn.clicked.connect(self._next_page)
        query.addWidget(self._next_btn)
        query.addStretch()
        self._query_btn = QPushButton("查询")
        self._query_btn.clicked.connect(self._do_query)
        query.addWidget(self._query_btn)
        lay.addLayout(query)

        self._info_label = QLabel("等待加载...")
        self._info_label.setStyleSheet("font-size:12px; color:#5a6068;")
        lay.addWidget(self._info_label)

        self._table = QTableWidget()
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._all_btn = QPushButton("全选行")
        self._all_btn.clicked.connect(lambda: self._select_all_rows(True))
        btn_row.addWidget(self._all_btn)
        self._clear_btn = QPushButton("清空选择")
        self._clear_btn.clicked.connect(lambda: self._select_all_rows(False))
        btn_row.addWidget(self._clear_btn)
        self._cols_btn = QPushButton("选择列")
        self._cols_btn.clicked.connect(self._pick_columns)
        btn_row.addWidget(self._cols_btn)
        self._export_btn = QPushButton("导出CSV")
        self._export_btn.clicked.connect(self._export)
        btn_row.addWidget(self._export_btn)
        lay.addLayout(btn_row)

        round_box = QGroupBox("轮次完整性")
        round_lay = QVBoxLayout(round_box)
        round_lay.setContentsMargins(8, 6, 8, 6)
        self._round_info = QLabel("")
        self._round_info.setStyleSheet("font-size:12px; font-weight:700; color:#5a6068;")
        round_lay.addWidget(self._round_info)
        self._round_table = QTableWidget(0, 7)
        self._round_table.setAlternatingRowColors(True)
        self._round_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._round_table.verticalHeader().setVisible(False)
        self._round_table.setMaximumHeight(132)
        self._round_table.setHorizontalHeaderLabels(
            ["轮次", "时间", "实际点数", "期望点数", "缺口", "状态", "补发"])
        self._round_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        round_lay.addWidget(self._round_table)
        # 插到信息行和明细表之间：先看每一轮收齐没有，再看逐点数据。
        lay.insertWidget(2, round_box)

        self._rows: list[dict] = []
        self._page_rows: list[dict] = []
        self._selected_columns: list[str] = []
        self._load_devices(default_gw, default_node)
        self._page.valueChanged.connect(lambda _v: self._render_page())
        self._page_size_box.valueChanged.connect(self._on_page_size_changed)

    def _load_devices(self, default_gw: str, default_node: str) -> None:
        devices = self._db.list_devices()
        gws = sorted({d["gateway_id"] for d in devices})
        nodes = sorted({d["node_id"] for d in devices})
        self._gw.addItems(gws)
        self._node.addItems(nodes)
        if default_gw in gws:
            self._gw.setCurrentText(default_gw)
        if default_node in nodes:
            self._node.setCurrentText(default_node)

    def _parse_range(self) -> tuple[Optional[int], int]:
        hours = self._hours.value()
        end_ts = int(time.time())
        # 0 表示不限时间。老数据的时间戳是网关重启前没 rebase 的原始
        # 开机秒，落在任何"最近 N 小时"窗口之外，不放开就永远查不到。
        start_ts = None if hours <= 0 else end_ts - hours * 3600
        return start_ts, end_ts

    def _do_query(self) -> None:
        gw = self._gw.currentText()
        node = self._node.currentText()
        if not gw or not node:
            QMessageBox.information(self, "提示", "请等待数据入库后再查询")
            return
        start_ts, end_ts = self._parse_range()
        self._total_count = self._db.count_impedance_rows(gw, node, start_ts=start_ts, end_ts=end_ts)
        self._page.setValue(1)
        self._render_round_log(gw, node, start_ts, end_ts)
        self._render_page()

    def _fetch_page(self, page_size: int, page: int) -> list[dict]:
        """翻页必须回库取当前页，不能切内存里的一页。

        库里按时间倒序、频率升序排，沿用它的顺序翻页才连续。
        """
        start_ts, end_ts = self._parse_range()
        return self._db.query_impedance_history(
            self._gw.currentText(), self._node.currentText(),
            limit=page_size, start_ts=start_ts, end_ts=end_ts,
            offset=(page - 1) * page_size)

    def _render_round_log(self, gw: str, node: str,
                          start_ts: Optional[int], end_ts: Optional[int]) -> None:
        """刷新"轮次完整性"表：每一轮收了几点、收齐没有。

        只看下面的逐点明细看不出"这轮 50 点是本来就只有 50 还是丢了 50"，
        必须把期望点数摆在一起才能判断。
        """
        rows = self._db.list_round_log(gw, node, limit=50)
        if start_ts is not None:
            rows = [r for r in rows
                    if (r.get("last_ts") or r.get("recorded_at") or 0) >= start_ts]
        if end_ts is not None:
            rows = [r for r in rows
                    if (r.get("first_ts") or r.get("recorded_at") or 0) <= end_ts]
        self._round_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            points = row.get("points")
            total = row.get("total_points")
            missing = max(0, int(total or 0) - int(points or 0)) if total else 0
            complete = row.get("complete")
            if points is None:
                # 固件整轮摘要先回来、本地一段都没落库：这轮我们还没有证据，
                # 既不能说完整也不能说缺了多少点。
                state, color, missing_cell = "未落库", "#8a9099", "—"
            elif complete:
                state, color = "完整", "#07c160"
                missing_cell = str(missing) if missing else "—"
            elif missing:
                state, color, missing_cell = f"缺 {missing} 点", "#d4380d", str(missing)
            else:
                state, color, missing_cell = "未知", "#8a9099", "—"
            when = row.get("last_ts") or row.get("first_ts") or row.get("recorded_at")
            when = (datetime.fromtimestamp(when).strftime("%m-%d %H:%M:%S")
                    if when else "")
            cells = [
                str(row.get("round_id") if row.get("round_id") is not None else
                    (row.get("scan_id") or "—")),
                when,
                str(points) if points is not None else "—",
                str(total) if total is not None else "—",
                missing_cell,
                state,
                str(row.get("retry")) if row.get("retry") else "—",
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QBrush(QColor("#2b2f33")))
                if c == 5:
                    item.setForeground(QBrush(QColor(color)))
                self._round_table.setItem(r, c, item)
        ok = sum(1 for r in rows if r.get("complete"))
        bad = sum(1 for r in rows
                  if r.get("points") is not None and not r.get("complete"))
        pending = sum(1 for r in rows if r.get("points") is None)
        missing_total = sum(
            max(0, int(r.get("total_points") or 0) - int(r.get("points") or 0))
            for r in rows if r.get("total_points") and r.get("points") is not None)
        if not rows:
            self._round_info.setText("没有轮次完整性记录（旧的 impedance_data 行不带轮次判定）")
        else:
            text = (f"{ok + bad} 轮已判定 · 完整 {ok} · 不完整 {bad} · "
                    f"缺 {missing_total} 点")
            if pending:
                text += f" · 未落库 {pending}"
            self._round_info.setText(text)
            self._round_info.setStyleSheet(
                "font-size:12px; font-weight:700; color:"
                f"{'#d4380d' if bad else '#07c160'};")

    def _render_page(self) -> None:
        page_size, page = page_geometry(self._page_size_box, self._page)
        rows = self._fetch_page(page_size, page)
        self._rows = rows
        self._page_rows = rows
        total = getattr(self, "_total_count", 0)
        total_pages = sync_page_controls(
            self._page, total, page_size, self._prev_btn, self._next_btn)
        self._info_label.setText(f"共 {total} 行 · 第 {self._page.value()}/{total_pages} 页")
        self._show_table(rows)

    def _prev_page(self) -> None:
        self._page.setValue(max(1, self._page.value() - 1))

    def _next_page(self) -> None:
        self._page.setValue(min(self._page.maximum(), self._page.value() + 1))

    def _on_page_size_changed(self) -> None:
        self._page.setValue(1)
        self._render_page()

    def _show_table(self, rows: list[dict]) -> None:
        self._table.clear()
        if not rows:
            self._info_label.setText("暂无阻抗数据")
            return
        keys = list(rows[0].keys())
        self._table.setColumnCount(len(keys) + 1)
        headers = ["选择"] + keys
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Unchecked)
            self._table.setItem(r_idx, 0, check)
            for c_idx, key in enumerate(keys):
                val = row.get(key)
                if key == "timestamp" and isinstance(val, (int, float)):
                    val = datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S")
                elif isinstance(val, float):
                    val = f"{val:.2f}"
                item = QTableWidgetItem(str(val) if val is not None else "")
                self._table.setItem(r_idx, c_idx + 1, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )

    def _select_all_rows(self, checked: bool = True) -> None:
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _pick_columns(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        keys = list(self._rows[0].keys())
        chosen = self._selected_columns or keys
        dlg = ColumnPickerDialog(keys, chosen, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._selected_columns = dlg.selected_columns()

    def _export(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        selected_rows = []
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                if r < len(self._page_rows):
                    selected_rows.append(self._page_rows[r])
        if not selected_rows:
            QMessageBox.information(self, "提示", "请勾选至少一行")
            return
        cols = self._selected_columns or list(selected_rows[0].keys())
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = QFileDialog.getSaveFileName(
            self, "导出CSV", f"impedance_{ts}.csv", "CSV files (*.csv)"
        )[0]
        if filepath:
            df = pd.DataFrame(selected_rows)[cols]
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
            QMessageBox.information(self, "导出成功", f"已保存 {len(df)} 条记录到\n{filepath}")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._rows:
            self._do_query()


class HistoryDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("历史数据查询")
        self.resize(1000, 620)

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
        query.addWidget(QLabel("每页"))
        self._page_size_box = QSpinBox()
        self._page_size_box.setRange(10, 1000)
        self._page_size_box.setSingleStep(10)
        self._page_size_box.setValue(50)
        query.addWidget(self._page_size_box)
        query.addWidget(QLabel("页"))
        self._page = QSpinBox()
        self._page.setRange(1, 1)
        self._page.setValue(1)
        query.addWidget(self._page)
        self._prev_btn = QPushButton("<")
        self._prev_btn.setEnabled(False)
        self._prev_btn.clicked.connect(self._prev_page)
        query.addWidget(self._prev_btn)
        self._next_btn = QPushButton(">")
        self._next_btn.setEnabled(False)
        self._next_btn.clicked.connect(self._next_page)
        query.addWidget(self._next_btn)
        query.addStretch()
        self._query_btn = QPushButton("查询")
        self._query_btn.clicked.connect(self._do_query)
        query.addWidget(self._query_btn)
        lay.addLayout(query)

        self._info_label = QLabel("等待加载...")
        self._info_label.setStyleSheet("font-size:12px; color:#5a6068;")
        lay.addWidget(self._info_label)

        self._table = QTableWidget()
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._table)

        btn_row = QHBoxLayout()
        self._note_label = QLabel(
            "阻抗列为该次测量的有效频段均值，不是一根频点；"
            "frequency_hz 留空表示不对应单一频率")
        self._note_label.setStyleSheet("font-size:11px; color:#8a8f99;")
        btn_row.addWidget(self._note_label)
        btn_row.addStretch()
        self._all_btn = QPushButton("全选行")
        self._all_btn.clicked.connect(self._select_all_rows)
        btn_row.addWidget(self._all_btn)
        self._cols_btn = QPushButton("选择列")
        self._cols_btn.clicked.connect(self._pick_columns)
        btn_row.addWidget(self._cols_btn)
        self._export_btn = QPushButton("导出CSV")
        self._export_btn.clicked.connect(self._export)
        btn_row.addWidget(self._export_btn)
        lay.addLayout(btn_row)

        self._rows: list[dict] = []
        self._page_rows: list[dict] = []
        self._selected_columns: list[str] = []
        self._load_devices()
        self._page.valueChanged.connect(lambda _v: self._render_page())
        self._page_size_box.valueChanged.connect(self._on_page_size_changed)

    def _load_devices(self) -> None:
        devices = self._db.list_devices()
        gws = sorted({d["gateway_id"] for d in devices})
        nodes = sorted({d["node_id"] for d in devices})
        # 先清再加，这个方法被重复调用时不会堆出重复项。
        self._gw.clear()
        self._node.clear()
        self._gw.addItems(gws)
        self._node.addItems(nodes)
        # 字典序会把 "123" 这类残留节点排到最前，默认停在那一排不出数据。
        # 真实节点都以 lora 开头，优先停到第一个。
        lora = next((i for i, n in enumerate(nodes) if n.lower().startswith("lora")), None)
        if lora is not None:
            self._node.setCurrentIndex(lora)

    def _parse_range(self) -> tuple[Optional[int], int]:
        hours = self._hours.value()
        end_ts = int(time.time())
        # 0 表示不限时间。老数据的时间戳是网关重启前没 rebase 的原始
        # 开机秒，落在任何"最近 N 小时"窗口之外，不放开就永远查不到。
        start_ts = None if hours <= 0 else end_ts - hours * 3600
        return start_ts, end_ts

    def _do_query(self) -> None:
        gw = self._gw.currentText()
        node = self._node.currentText()
        if not gw or not node:
            QMessageBox.information(self, "提示", "请等待数据入库后再查询")
            return
        start_ts, end_ts = self._parse_range()
        self._total_count = self._db.count_sensor_rows(gw, node, start_ts=start_ts, end_ts=end_ts)
        self._page.setValue(1)
        self._render_page()

    def _fetch_page(self, page_size: int, page: int) -> list[dict]:
        """翻页必须回库取当前页。

        一次只取一页的话内存里没有后面的页，翻过去切出来就是空表。
        库里按时间倒序排，沿用它的顺序翻页才连续。
        """
        start_ts, end_ts = self._parse_range()
        return self._db.query_sensor_history(
            self._gw.currentText(), self._node.currentText(),
            limit=page_size, start_ts=start_ts, end_ts=end_ts,
            offset=(page - 1) * page_size)

    def _render_page(self) -> None:
        page_size, page = page_geometry(self._page_size_box, self._page)
        rows = self._fetch_page(page_size, page)
        self._rows = rows
        self._page_rows = rows
        total = getattr(self, "_total_count", 0)
        total_pages = sync_page_controls(
            self._page, total, page_size, self._prev_btn, self._next_btn)
        self._info_label.setText(f"共 {total} 行 · 第 {self._page.value()}/{total_pages} 页")
        self._show_table(rows)

    def _prev_page(self) -> None:
        self._page.setValue(max(1, self._page.value() - 1))

    def _next_page(self) -> None:
        self._page.setValue(min(self._page.maximum(), self._page.value() + 1))

    def _on_page_size_changed(self) -> None:
        self._page.setValue(1)
        self._render_page()

    @staticmethod
    def _ordered_keys(rows: list[dict]) -> list[str]:
        keys = list(rows[0].keys())
        known = [field for field, _n in SENSOR_HISTORY_COLUMNS if field in keys]
        return known + [k for k in keys if k not in known]

    def _show_table(self, rows: list[dict]) -> None:
        self._table.clear()
        if not rows:
            self._info_label.setText("暂无历史数据")
            return
        keys = self._ordered_keys(rows)
        self._table.setColumnCount(len(keys) + 1)
        self._table.setHorizontalHeaderLabels(["选择"] + keys)
        self._table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Unchecked)
            self._table.setItem(r_idx, 0, check)
            for c_idx, key in enumerate(keys):
                val = row.get(key)
                if key == "timestamp" and isinstance(val, (int, float)):
                    val = datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S")
                elif isinstance(val, float):
                    val = f"{val:.{SENSOR_HISTORY_DECIMALS.get(key, 2)}f}"
                item = QTableWidgetItem(str(val) if val is not None else "")
                self._table.setItem(r_idx, c_idx + 1, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )

    def _select_all_rows(self, checked: bool = True) -> None:
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _pick_columns(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        keys = list(self._rows[0].keys())
        chosen = self._selected_columns or keys
        dlg = ColumnPickerDialog(keys, chosen, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._selected_columns = dlg.selected_columns()

    def _export(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        selected_rows = []
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                if r < len(self._page_rows):
                    selected_rows.append(self._page_rows[r])
        if not selected_rows:
            QMessageBox.information(self, "提示", "请勾选至少一行")
            return
    @staticmethod
    def _export_columns(selected_rows: list[dict], wanted: list[str]) -> list[str]:
        # 上一次选列记下的字段名可能已经不存在了（表结构改过），直接丢给
        # pandas 会 KeyError。只留当前行还认识的列，一个都不剩就导全量。
        keys = list(selected_rows[0].keys())
        cols = [c for c in (wanted or keys) if c in keys]
        return cols or keys

    def _export(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "暂无数据")
            return
        selected_rows = []
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                if r < len(self._page_rows):
                    selected_rows.append(self._page_rows[r])
        if not selected_rows:
            QMessageBox.information(self, "提示", "请勾选至少一行")
            return
        cols = self._export_columns(selected_rows, self._selected_columns)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = QFileDialog.getSaveFileName(
            self, "导出CSV", f"sensor_{ts}.csv", "CSV files (*.csv)"
        )[0]
        if filepath:
            df = pd.DataFrame(selected_rows)[cols]
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
            QMessageBox.information(self, "导出成功", f"已保存 {len(df)} 条记录")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._rows:
            self._do_query()


class EISPanel(QWidget):
    """阻抗谱视图：Nyquist 轨迹 + Bode 双轴，随扫频完成自动刷新。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._arrows: list[pg.ArrowItem] = []
        self._annotations: list[pg.TextItem] = []
        self._fc_marks: list[pg.TextItem] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._info = QLabel("等待扫频数据…")
        self._info.setStyleSheet("font-size:12px; color:#5a6068; font-weight:600;")
        header.addWidget(self._info)
        header.addStretch()
        self._legend = QLabel("点颜色：深→浅 表示频率 低→高")
        self._legend.setStyleSheet("font-size:11px; color:#8a9099;")
        header.addWidget(self._legend)
        lay.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(8)

        nyq_frame = QFrame()
        nyq_frame.setStyleSheet(
            "QFrame{background:#fff;border-radius:10px;border:1px solid #e6e9ef;}"
        )
        nyq_lay = QVBoxLayout(nyq_frame)
        nyq_lay.setContentsMargins(8, 8, 8, 8)
        nyq_title = QLabel("Nyquist 轨迹")
        nyq_title.setStyleSheet("font-size:12px; font-weight:700; color:#2b2f33;")
        nyq_lay.addWidget(nyq_title)

        self._nyq_plot = pg.PlotWidget()
        self._nyq_plot.setBackground("w")
        self._nyq_plot.showGrid(x=True, y=True, alpha=0.25)
        self._nyq_plot.setLabel("bottom", "Z' (Ω)")
        self._nyq_plot.setLabel("left", "-Z'' (Ω)")
        self._nyq_plot.setMinimumHeight(120)
        self._scatter = pg.ScatterPlotItem(size=9, pen=pg.mkPen(width=0))
        self._scatter.setSymbol("o")
        self._scatter.setPen(pg.mkPen("#ffffff", width=1))
        self._nyq_plot.addItem(self._scatter)
        nyq_lay.addWidget(self._nyq_plot)
        self._hint = FirstFrameHint("等待整轮扫频完成…", self._nyq_plot)
        self._hint.show()
        body.addWidget(nyq_frame, stretch=1)

        bode_frame = QFrame()
        bode_frame.setStyleSheet(
            "QFrame{background:#fff;border-radius:10px;border:1px solid #e6e9ef;}"
        )
        bode_lay = QVBoxLayout(bode_frame)
        bode_lay.setContentsMargins(8, 8, 8, 8)
        bode_title = QLabel("Bode 图")
        bode_title.setStyleSheet("font-size:12px; font-weight:700; color:#2b2f33;")
        bode_lay.addWidget(bode_title)

        self._bode_plot = pg.PlotWidget()
        self._bode_plot.setBackground("w")
        bode_item = self._bode_plot.getPlotItem()
        for axis in ("bottom", "top", "left", "right"):
            bode_item.getAxis(axis).setStyle(showValues=True)
        self._bode_plot.setLabel("left", "|Z| (Ω)", color=BODE_MAG_COLOR)
        self._bode_plot.setLabel("right", "相位 (°)", color=BODE_PHASE_COLOR)
        self._bode_plot.setLabel("bottom", "频率 (Hz)", position=1)
        self._bode_plot.setLogMode(x=True, y=False)
        self._bode_plot.setMinimumHeight(120)
        self._mag_curve = bode_item.plot(
            pen=pg.mkPen(BODE_MAG_COLOR, width=2),
            symbol="o",
            symbolSize=5,
            name="|Z|",
        )
        self._mag_curve.setDownsampling()
        self._phase_curve = pg.PlotDataItem(
            pen=pg.mkPen(BODE_PHASE_COLOR, width=2), name="相位"
        )
        bode_item.addItem(self._phase_curve, y="right")
        self._bode_plot.addLegend(offset=(6, 6), labelTextSize="10px")
        bode_lay.addWidget(self._bode_plot)
        body.addWidget(bode_frame, stretch=1)

        lay.addLayout(body, stretch=1)

    def _analysis_band(self) -> tuple[float, float]:
        """分析频段从 config.json 的 maturity 段读，别在代码里写死。"""
        parent = self.parent()
        config = getattr(parent, "_config", None) or {}
        mat = config.get("maturity") or {}
        try:
            return (float(mat.get("band_lo_hz", 1000.0)),
                    float(mat.get("band_hi_hz", 30000.0)))
        except (TypeError, ValueError):
            return 1000.0, 30000.0

    def _band_coverage(self, spectrum: ImpedanceSpectrum) -> str:
        """实测频段没覆盖配置的分析频段时提醒一句。

        真机节点只测 900~12 kHz，如果分析频段还配成 30 kHz，
        上半段永远是空的，标定结果会偏。
        """
        band_lo, band_hi = self._analysis_band()
        try:
            freqs = [float(p.frequency_hz) for p in (spectrum.points or [])
                     if p.frequency_hz]
        except (TypeError, ValueError):
            return ""
        if not freqs:
            return ""
        lo, hi = min(freqs), max(freqs)
        if hi < band_hi * 0.9:
            return (f"频段偏窄：实测 {lo / 1000:.1f}~{hi / 1000:.1f} kHz，"
                    f"分析上界 {band_hi / 1000:.1f} kHz")
        return ""

    def set_spectrum(self, spectrum: ImpedanceSpectrum, stats: dict | None = None) -> None:
        self._hint.dismiss()
        ny = nyquist_series(spectrum)
        if not ny["x"]:
            return

        freqs = np.asarray(ny["frequency"], dtype=float)
        span = float(freqs.max() - freqs.min())
        norm = (freqs - freqs.min()) / span if span > 0 else np.zeros_like(freqs)
        cmap = pg.colormap.get(NYQUIST_COLORMAP)
        brushes = [
            pg.mkBrush(
                QColor(int(cmap.map(float(v))[0]), int(cmap.map(float(v))[1]),
                       int(cmap.map(float(v))[2]))
            )
            for v in norm
        ]
        self._scatter.setData(x=ny["x"], y=ny["y"], brush=brushes)

        for arrow in self._arrows:
            self._nyq_plot.removeItem(arrow)
        self._arrows.clear()
        xs, ys = ny["x"], ny["y"]
        for i in range(len(xs) - 1):
            dx = xs[i + 1] - xs[i]
            dy = ys[i + 1] - ys[i]
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                continue
            arrow = pg.ArrowItem(
                pos=((xs[i] + xs[i + 1]) / 2.0, (ys[i] + ys[i + 1]) / 2.0),
                angle=math.degrees(math.atan2(dy, dx)),
                headLen=9,
                tipAngle=26,
                pxMode=True,
                pen=pg.mkPen("#9aa3ad", width=1),
                brush=pg.mkBrush("#9aa3ad"),
            )
            self._nyq_plot.addItem(arrow)
            self._arrows.append(arrow)

        for item in self._annotations:
            self._nyq_plot.removeItem(item)
        self._annotations.clear()
        for idx in (0, len(xs) - 1):
            text = pg.TextItem(
                f"{freqs[idx] / 1000.0:.2f} kHz", color="#5a6068", anchor=(0, 1)
            )
            self._nyq_plot.addItem(text, x=xs[idx], y=ys[idx])
            self._annotations.append(text)

        bode = bode_series(spectrum)
        self._mag_curve.setData(bode["frequency"], bode["magnitude"])
        self._phase_curve.setData(bode["frequency"], bode["phase"])

        stats = stats or {}
        info = [f"轮次 {spectrum.scan_id}", f"{len(spectrum.points)} 频点"]
        if stats.get("mean"):
            info.append(f"|Z|均值 {stats['mean']:.0f} Ω")
        band_lo, band_hi = self._analysis_band()
        f_c = characteristic_frequency(spectrum, band_lo, band_hi)
        if f_c:
            info.append(f"弛豫频率 {f_c / 1000.0:.2f} kHz")
        missing = (spectrum.meta or {}).get("points_missing", 0)
        if missing:
            info.append(f"缺 {missing} 点")
        covered = self._band_coverage(spectrum)
        if covered:
            info.append(covered)
        self._info.setText(" · ".join(info))

        for item in self._fc_marks:
            self._nyq_plot.removeItem(item)
        self._fc_marks.clear()
        if f_c is not None:
            for x, y, f in zip(xs, ys, freqs):
                if f == f_c:
                    mark = pg.TextItem("f_c", color="#d4380d", anchor=(0, 1))
                    self._nyq_plot.addItem(mark, x=x, y=y)
                    self._fc_marks.append(mark)
                    break


class MaturityPanel(QWidget):
    """采摘预测：置信度仪表盘 + 四阶段步骤条。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._steps: list[tuple[QFrame, QFrame, QLabel]] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(14)

        body = QHBoxLayout()
        body.setSpacing(18)

        left = QVBoxLayout()
        left.setSpacing(4)
        self._gauge = ArcGauge()
        left.addWidget(self._gauge)
        self._confidence_label = QLabel("置信度 --")
        self._confidence_label.setStyleSheet("font-size:12px; color:#5a6068;")
        left.addWidget(self._confidence_label)
        body.addLayout(left)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color:#e6e9ef;")
        body.addWidget(divider)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._make_step_header())

        self._level_label = QLabel("--")
        self._level_label.setStyleSheet("font-size:22px; font-weight:700; color:#8a9099;")
        right.addWidget(self._level_label)

        self._maturity_label = QLabel("成熟度进度 --")
        self._maturity_label.setStyleSheet("font-size:13px; color:#5a6068;")
        right.addWidget(self._maturity_label)
        self._harvest_label = QLabel("预计采摘 --")
        self._harvest_label.setStyleSheet("font-size:13px; color:#5a6068;")
        right.addWidget(self._harvest_label)

        hint_wrap = QWidget()
        hint_lay = QVBoxLayout(hint_wrap)
        hint_lay.setContentsMargins(0, 0, 0, 0)
        hint_wrap.setMinimumHeight(90)
        self._hint = FirstFrameHint("等待首次预测…", hint_wrap)
        hint_lay.addWidget(self._hint)
        right.addWidget(hint_wrap)
        body.addLayout(right, stretch=1)

        steps = QGridLayout()
        steps.setSpacing(8)
        for idx, (_key, cn) in enumerate(MATURITY_ORDER):
            frame, bar, label = self._make_step(cn)
            steps.addWidget(frame, 0, idx)
            self._steps.append((frame, bar, label))
        right.addLayout(steps)

        lay.addLayout(body)

    def _make_step_header(self) -> QLabel:
        title = QLabel("实验阶段")
        title.setStyleSheet("font-size:12px; font-weight:700; color:#2b2f33;")
        return title

    @staticmethod
    def _make_step(text: str) -> tuple[QFrame, QFrame, QLabel]:
        frame = QFrame()
        frame.setFixedHeight(56)
        frame.setStyleSheet(
            "QFrame{background:#f7f9fc;border-radius:8px;border:1px solid #e6e9ef;}"
        )
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(10, 8, 10, 8)
        bar = QFrame()
        bar.setFixedHeight(5)
        bar.setStyleSheet("QFrame{background:#e6e9ef;border-radius:3px;}")
        inner.addWidget(bar)
        label = QLabel(text)
        label.setStyleSheet("font-size:11px; color:#8a9099;")
        inner.addWidget(label)
        return frame, bar, label

    def set_prediction(self, prediction: PredictionData) -> None:
        self._hint.dismiss()
        level = prediction.maturity_level or "unripe"
        color = MATURITY_COLORS.get(level, "#8a9099")
        progress = float(prediction.maturity or 0.0)
        confidence = float(prediction.confidence or 0.0)

        self._gauge.set_value(confidence, caption=f"{confidence * 100:.0f}%", color=color)
        self._confidence_label.setText(f"置信度 {confidence * 100:.1f} %")
        self._level_label.setText(level_label(level))
        self._level_label.setStyleSheet(f"font-size:22px; font-weight:700; color:{color};")
        self._maturity_label.setText(f"成熟度进度 {progress * 100:.1f} %")
        self._harvest_label.setText(f"预计采摘 {prediction.harvest_date or '--'}")

        active = next(
            (i for i, (key, _cn) in enumerate(MATURITY_ORDER) if key == level), -1
        )
        for index, (frame, bar, label) in enumerate(self._steps):
            if index == active:
                frame.setStyleSheet(
                    "QFrame{background:#fff;border-radius:8px;border:1px solid #cfd6e0;}"
                )
                bar.setStyleSheet(f"QFrame{{background:{color};border-radius:3px;}}")
                label.setStyleSheet(f"font-size:11px; color:{color}; font-weight:700;")
            elif index < active:
                frame.setStyleSheet(
                    "QFrame{background:#f7f9fc;border-radius:8px;border:1px solid #e6e9ef;}"
                )
                bar.setStyleSheet("QFrame{background:#d7dce4;border-radius:3px;}")
                label.setStyleSheet("font-size:11px; color:#a7adb5;")
            else:
                frame.setStyleSheet(
                    "QFrame{background:#f7f9fc;border-radius:8px;border:1px solid #e6e9ef;}"
                )
                bar.setStyleSheet("QFrame{background:#e6e9ef;border-radius:3px;}")
                label.setStyleSheet("font-size:11px; color:#8a9099;")


class LiveSweepWindow(QDialog):
    """阻抗上报专用窗口：逐点表格 + 点数进度 + Nyquist / Bode。

    每个 sweep 报文一到就刷新，不等整轮攒齐；整谱出来后用装配好的谱
    覆盖一次，保证图和数据库里的是同一份数据。
    """

    COLS = (
        ("点序号", "point_index", "", 0),
        ("频率 Hz", "frequency_hz", "Hz", 1),
        ("实部 Re", "z_real", "Ω", 1),
        ("虚部 Im", "z_imag", "Ω", 1),
        ("|Z|", "magnitude", "Ω", 1),
        ("相位", None, "°", 2),
        ("土壤湿度", "soil_moisture", "%", 1),
        ("温度", "temperature", "°C", 2),
        ("NH3", "nh3", "ppm", 1),
        ("H2S", "h2s", "ppm", 1),
        ("CO2", "co2", "ppm", 0),
        ("pH", "ph", "", 2),
        ("湿度", "humidity", "%", 1),
    )

    def __init__(self, db, parent=None, min_points: int = 50) -> None:
        super().__init__(parent)
        self._db = db
        # 低于协议下限的阈值没有意义，抬到下限。
        self._min_points = max(MIN_REPORT_POINTS, int(min_points))
        self.setWindowTitle("阻抗实时上报")
        self.resize(1240, 760)
        self._report_key = ""
        self._points: dict[int, object] = {}
        self._rows: list[dict] = []
        self._auto = True

        lay = QVBoxLayout(self)

        meta = QHBoxLayout()
        meta.addWidget(QLabel("报文:"))
        self._report_label = QLabel("—")
        self._report_label.setStyleSheet("font-size:12px; font-weight:700; color:#2b2f33;")
        meta.addWidget(self._report_label)
        meta.addWidget(QLabel("点:"))
        self._count_label = QLabel("0 / 0")
        self._count_label.setStyleSheet("font-size:12px; font-weight:700; color:#2b2f33;")
        meta.addWidget(self._count_label)
        meta.addWidget(QLabel("进度"))
        self._progress = QProgressBar()
        self._progress.setRange(0, self._min_points)
        self._progress.setValue(0)
        self._progress.setFixedWidth(180)
        self._progress.setTextVisible(True)
        meta.addWidget(self._progress)
        self._state_label = QLabel("等待数据")
        self._state_label.setStyleSheet("font-size:12px; font-weight:700; color:#8a9099;")
        meta.addWidget(self._state_label)
        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet("font-size:11px; color:#5a6068;")
        meta.addWidget(self._summary_label)
        meta.addStretch()
        self._auto_chk = QCheckBox("报文到达自动刷新")
        self._auto_chk.setChecked(True)
        meta.addWidget(self._auto_chk)
        self._load_btn = QPushButton("载入最新轮")
        self._load_btn.clicked.connect(self._load_latest)
        meta.addWidget(self._load_btn)
        lay.addLayout(meta)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        nyq_box = QGroupBox("Nyquist 轨迹（-Z''  vs  Z'）")
        nyq_lay = QVBoxLayout(nyq_box)
        self._nyq_plot = pg.PlotWidget()
        self._nyq_plot.setBackground("w")
        self._nyq_plot.setLabel("bottom", "Z' (Ω)")
        self._nyq_plot.setLabel("left", "-Z'' (Ω)")
        ny_item = self._nyq_plot.getPlotItem()
        for axis in ("bottom", "top", "left", "right"):
            ny_item.getAxis(axis).setStyle(showValues=True)
        self._scatter = ny_item.plot(pen=None, symbol="o", symbolSize=6, name="-Z''")
        self._scatter.setDownsampling()
        nyq_lay.addWidget(self._nyq_plot)
        splitter.addWidget(nyq_box)

        bode_box = QGroupBox("Bode 图")
        bode_lay = QVBoxLayout(bode_box)
        self._bode_plot = pg.PlotWidget()
        self._bode_plot.setBackground("w")
        bode_item = self._bode_plot.getPlotItem()
        for axis in ("bottom", "top", "left", "right"):
            bode_item.getAxis(axis).setStyle(showValues=True)
        self._bode_plot.setLabel("left", "|Z| (Ω)", color=BODE_MAG_COLOR)
        self._bode_plot.setLabel("right", "相位 (°)", color=BODE_PHASE_COLOR)
        self._bode_plot.setLabel("bottom", "频率 (Hz)", position=1)
        self._bode_plot.setLogMode(x=True, y=False)
        self._mag_curve = bode_item.plot(
            pen=pg.mkPen(BODE_MAG_COLOR, width=2), symbol="o", symbolSize=5, name="|Z|"
        )
        self._mag_curve.setDownsampling()
        self._phase_curve = pg.PlotDataItem(
            pen=pg.mkPen(BODE_PHASE_COLOR, width=2), name="相位"
        )
        bode_item.addItem(self._phase_curve, y="right")
        self._bode_plot.addLegend(offset=(6, 6), labelTextSize="10px")
        bode_lay.addWidget(self._bode_plot)
        splitter.addWidget(bode_box)
        splitter.setSizes([560, 560])
        lay.addWidget(splitter, stretch=2)

        self._table = QTableWidget()
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setHorizontalHeaderLabels([name for name, _k, _u, _p in self.COLS])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        lay.addWidget(self._table, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._export_btn = QPushButton("导出CSV")
        self._export_btn.clicked.connect(self._export)
        btn_row.addWidget(self._export_btn)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._close_btn)
        lay.addLayout(btn_row)

    # ---- 数据入口 ----

    def _cell(self, point: object, key: str | None) -> float | None:
        """兼容 SweepPointData 对象和数据库字典两种来源。"""
        if key is None:
            return None
        if isinstance(point, dict):
            value = point.get(key)
        else:
            value = getattr(point, key, None)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def feed_report(self, payload: dict) -> None:
        """收到一个 sweep 报文就刷新一次，不等整轮。"""
        points = payload.get("data") or []
        if not points:
            return
        self._report_key = str(payload.get("report_id") or "")
        buffered = int(payload.get("buffered_points") or len(self._points))
        self._set_meta(
            self._report_key or f"轮次 {payload.get('round_id', '?')}",
            buffered,
            int(payload.get("segment_complete") or 0) and True,
            bool(payload.get("complete")),
            payload,
        )
        for point in points:
            idx = int(getattr(point, "point_index", 0) or 0)
            self._points[idx] = point
        self._render_plots()
        self._render_table()

    def feed_spectrum(self, payload: dict) -> None:
        """整谱装配完成后覆盖一次，保证与库内数据一致。

        整谱点是 SpectrumPoint，不带环境量列；必须从本窗口已缓存的分片点里
        把土壤湿度 / 温度 / CO2 / pH / 湿度搬过来，否则整轮出图后表格里的
        环境传感器全变成空，看着就像环境数据和阻抗没同步。
        """
        spectrum = payload.get("data")
        if spectrum is None:
            return
        self._report_key = str(payload.get("report_id") or "")
        if self._report_key == "":
            self._report_key = f"轮次 {getattr(spectrum, 'scan_id', '?')}"
        gateway_id = str(getattr(spectrum, "gateway_id", "") or "")
        node_id = str(getattr(spectrum, "node_id", "") or "")
        round_id = int(getattr(spectrum, "scan_id", 0) or 0)
        timestamp = int(getattr(spectrum, "timestamp", 0) or 0)
        previous = self._points
        self._points = {}
        for i, point in enumerate(spectrum.points):
            old = previous.get(i)
            magnitude = self._cell(point, "magnitude")
            self._points[i] = SweepPointData(
                gateway_id=gateway_id,
                node_id=node_id,
                round_id=round_id,
                timestamp=timestamp,
                point_index=i,
                frequency_hz=float(point.frequency_hz),
                z_real=float(point.z_real),
                z_imag=float(point.z_imag),
                magnitude=magnitude if magnitude is not None else float(
                    math.hypot(point.z_real, point.z_imag)),
                soil_moisture=self._cell(old, "soil_moisture"),
                temperature=self._cell(old, "temperature"),
                nh3=self._cell(old, "nh3"),
                h2s=self._cell(old, "h2s"),
                co2=self._cell(old, "co2"),
                ph=self._cell(old, "ph"),
                humidity=self._cell(old, "humidity"),
                report_id=self._report_key or None,
            )
        self._set_meta(
            self._report_key,
            len(spectrum.points),
            True,
            bool(payload.get("complete")),
            payload,
        )
        self._render_plots()
        self._render_table()

    def refresh_from_db(self) -> None:
        """从数据库载入最近一轮，静默失败（窗口刚打开时可能还没有数据）。"""
        self._load_round_from_db()

    def _load_round_from_db(self, gateway_id: str = "", node_id: str = "") -> bool:
        """读库里的最近一轮扫频并刷新，返回是否成功。"""
        parent = self.parent()
        gw = gateway_id or getattr(parent, "_current_gw", "") or ""
        node = node_id or getattr(parent, "_current_node", "") or ""
        if not gw or not node:
            return False
        rounds = self._db.list_sweep_rounds(gw, node, limit=1)
        if not rounds:
            return False
        rows = self._db.query_sweep_round(gw, node, int(rounds[0]["round_id"]))
        if not rows:
            return False
        self._report_key = str(
            rounds[0].get("report_id") or f"轮次 {rounds[0]['round_id']}"
        )
        self._points = {
            i: self._row_to_point(row, i, self._report_key)
            for i, row in enumerate(rows)
        }
        self._set_meta(
            self._report_key, len(rows),
            len(rows) >= self._min_points, len(rows) >= self._min_points,
            {},
        )
        self._render_plots()
        self._render_table()
        return True

    @staticmethod
    def _row_to_point(row: dict, index: int, report_id: str) -> SweepPointData:
        """把数据库查询出的字典行还原成 SweepPointData，供图件使用。"""
        def pick(key: str, cast=None):
            value = row.get(key)
            if value is None:
                return None
            if cast is not None:
                try:
                    return cast(value)
                except (TypeError, ValueError):
                    return None
            return value

        return SweepPointData(
            gateway_id=str(row.get("gateway_id") or ""),
            node_id=str(row.get("node_id") or ""),
            round_id=int(row.get("round_id") or 0),
            timestamp=int(row.get("timestamp") or 0),
            point_index=index,
            frequency_hz=pick("frequency_hz", float),
            z_real=pick("z_real", float),
            z_imag=pick("z_imag", float),
            magnitude=pick("magnitude", float),
            soil_moisture=pick("soil_moisture", float),
            temperature=pick("temperature", float),
            nh3=pick("nh3", float),
            h2s=pick("h2s", float),
            co2=pick("co2", float),
            ph=pick("ph", float),
            humidity=pick("humidity", float),
            report_id=report_id,
        )

    def _set_meta(
        self,
        key: str,
        buffered: int,
        segment_complete: bool,
        round_complete: bool,
        payload: dict,
    ) -> None:
        self._report_label.setText(key or "—")
        self._count_label.setText(f"{buffered} / {self._min_points}")
        total = payload.get("total_points")
        total = int(total) if total else self._min_points
        # 进度条表示"这一轮还差多少"，分母是整轮总点数，
        # 刻度上限统一压在协议下限上，避免不同轮次之间刻度跳动。
        self._progress.setRange(0, max(1, self._min_points))
        self._progress.setValue(max(
            0, min(self._min_points,
                   int(round(self._min_points * buffered / max(1, total))))))
        if round_complete:
            text, color = "整轮已满", "#07c160"
        elif total > self._min_points:
            # 报文本身够数，但整轮还没收齐：网关一轮 100 点拆 2 段，
            # 第一段到这里就停了，得告诉用户还差多少。
            text, color = f"本轮 {buffered}/{total} 点", "#1890ff"
        elif segment_complete:
            text, color = "本报文已满", "#07c160"
        else:
            text, color = f"本报文不足 {self._min_points} 点", "#d4380d"
        self._state_label.setText(text)
        self._state_label.setStyleSheet(
            f"font-size:12px; font-weight:700; color:{color};"
        )
        seg = payload.get("seg")
        seg_total = payload.get("seg_total")
        bits = []
        if seg is not None and seg_total:
            bits.append(f"报文 {int(seg) + 1}/{int(seg_total)}")
        elif seg is not None:
            # 网关固件只报 seg 不报 seg_total，只能显示当前段号。
            bits.append(f"报文 第 {int(seg) + 1} 段")
        if total > self._min_points:
            bits.append(f"本轮 {buffered}/{total} 点")
        self._summary_label.setText(" · ".join(bits))

    def show_round_done(self, payload: dict) -> None:
        """网关整轮摘要：确认这一轮收了几点、补发过几次。"""
        total = payload.get("total_points")
        points = payload.get("points")
        bits = []
        if payload.get("round_id") is not None:
            bits.append(f"轮次 {payload.get('round_id')}")
        if points is not None and total:
            bits.append(f"{points}/{total} 点")
        retry = payload.get("retry")
        if retry:
            bits.append(f"补发 {retry} 次")
        if payload.get("imp_mean"):
            bits.append(f"|Z|均值 {payload.get('imp_mean')} Ω")
        if not bits:
            return
        missing = None
        if points is not None and total:
            missing = max(0, int(total) - int(points))
        color = "#07c160" if not missing else "#d4380d"
        text = f"整轮确认：" + " · ".join(bits)
        if missing:
            text += f" · 缺 {missing} 点"
        self._state_label.setText(text)
        self._state_label.setStyleSheet(
            f"font-size:12px; font-weight:700; color:{color};"
        )

    # ---- 渲染 ----

    def _render_plots(self) -> None:
        ordered = [self._points[i] for i in sorted(self._points)]
        if not ordered:
            return
        spectrum = sweep_points_to_spectrum([
            p for p in ordered if isinstance(p, SweepPointData)
        ]) if any(isinstance(p, SweepPointData) for p in ordered) else None
        if spectrum is None or not spectrum.points:
            return
        ny = nyquist_series(spectrum)
        if not ny["x"]:
            return
        self._scatter.setData(x=ny["x"], y=ny["y"])
        bode = bode_series(spectrum)
        self._mag_curve.setData(bode["frequency"], bode["magnitude"])
        self._phase_curve.setData(bode["frequency"], bode["phase"])

    def _render_table(self) -> None:
        ordered = [self._points[i] for i in sorted(self._points)]
        self._table.setRowCount(len(ordered))
        for r, point in enumerate(ordered):
            freq = self._cell(point, "frequency_hz")
            zr = self._cell(point, "z_real")
            zi = self._cell(point, "z_imag")
            phase = None
            if zr is not None and zi is not None and (zr or zi):
                phase = math.degrees(math.atan2(-zi, zr))
            values = {
                "point_index": float(r),
                "frequency_hz": freq,
                "z_real": zr,
                "z_imag": zi,
                "magnitude": self._cell(point, "magnitude"),
                "ph": phase,
                "soil_moisture": self._cell(point, "soil_moisture"),
                "temperature": self._cell(point, "temperature"),
                "nh3": self._cell(point, "nh3"),
                "h2s": self._cell(point, "h2s"),
                "co2": self._cell(point, "co2"),
                "humidity": self._cell(point, "humidity"),
            }
            values["pH"] = self._cell(point, "ph")
            for c, (_name, key, _unit, prec) in enumerate(self.COLS):
                lookup = "ph" if key is None else key
                value = values.get(lookup)
                item = QTableWidgetItem(
                    f"{value:.{prec}f}" if value is not None else ""
                )
                item.setForeground(QBrush(QColor("#2b2f33")))
                if key == "point_index":
                    item.setForeground(QBrush(QColor("#1890ff")))
                self._table.setItem(r, c, item)

    # ---- 动作 ----

    def _load_latest(self) -> None:
        if not self._load_round_from_db():
            QMessageBox.information(
                self, "提示", "主界面还没收到数据，或数据库里没有扫频轮次")

    def _export(self) -> None:
        ordered = [self._points[i] for i in sorted(self._points)]
        if not ordered:
            QMessageBox.information(self, "提示", "暂无数据可导出")
            return
        data = []
        for r, point in enumerate(ordered):
            freq = self._cell(point, "frequency_hz")
            zr = self._cell(point, "z_real")
            zi = self._cell(point, "z_imag")
            data.append({
                "report_id": self._report_key,
                "point_index": r,
                "frequency_hz": freq,
                "z_real": zr,
                "z_imag": zi,
                "magnitude": self._cell(point, "magnitude"),
                "phase_deg": math.degrees(math.atan2(-zi, zr))
                if zr is not None and zi is not None and (zr or zi) else None,
                "temperature": self._cell(point, "temperature"),
                "soil_moisture": self._cell(point, "soil_moisture"),
                "nh3": self._cell(point, "nh3"),
                "h2s": self._cell(point, "h2s"),
                "co2": self._cell(point, "co2"),
                "ph": self._cell(point, "ph"),
                "humidity": self._cell(point, "humidity"),
            })
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = QFileDialog.getSaveFileName(
            self, "导出阻抗CSV", f"sweep_{ts}.csv", "CSV files (*.csv)"
        )[0]
        if filepath:
            pd.DataFrame(data).to_csv(filepath, index=False, encoding="utf-8-sig")
            QMessageBox.information(self, "导出成功", f"已保存 {len(data)} 行到\n{filepath}")


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
        self._connected = False
        self._last_data_at = 0.0
        self._last_heartbeat_at = 0.0
        self._gw_ip = ""
        self._gw_rssi: float | None = None
        self._device_offline = False
        self._live_win: LiveSweepWindow | None = None
        self._latest_sweep_payload: dict | None = None
        self._latest_spectrum_payload: dict | None = None
        self._round_log: list[tuple[int, str]] = []
        sim_interval = float(
            config.get("simulator", {}).get("interval_ms", 2000)
        ) / 1000.0
        self._stale_after_s = max(6.0, 3.0 * sim_interval)
        # 网关心跳窗口：固件默认 30s 一次，超时才算链路断。
        # 扫频一轮要好几十秒，只按数据新鲜度判会把灯反复刷成"中断"。
        self._heartbeat_stale_s = float(
            config.get("heartbeat", {}).get("stale_after_s", 45.0))

        ui_cfg = config.get("ui", {})
        self.setWindowTitle("水果成熟度实时监测系统")
        self.resize(ui_cfg.get("window_width", 1280), ui_cfg.get("window_height", 860))
        self.setStyleSheet("QMainWindow{background:#f5f7fa;}")

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
        if hasattr(worker, "gateway_info"):
            worker.gateway_info.connect(self._on_gateway_info)
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

        self._status_light = StatusLight()
        lay.addWidget(self._status_light)

        self._gw_info_label = QLabel("网关: 未上报")
        self._gw_info_label.setStyleSheet("font-size:11px; color:#8a9099;")
        lay.addWidget(self._gw_info_label)

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

        # 阻抗卡片：放到最后一行下一个空位，别压到传感器卡片上。
        # 压在同一格里两个卡片会互相覆盖，阻抗就一直看不见。
        self._impedance_card = SensorCard("阻抗", "Ω", wrap)
        grid.addWidget(self._impedance_card,
                       len(SENSOR_FIELDS) // 4, len(SENSOR_FIELDS) % 4)

        return wrap

    def _build_chart(self) -> QFrame:
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame{background:#fff;border-radius:10px;border:1px solid #e6e9ef;}"
        )
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(8, 8, 8, 8)

        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._tabs.setStyleSheet(
            "QTabWidget::pane{border:none;}"
            "QTabBar{qproperty-drawingBackground:false;}"
            "QTabBar::tab{padding:7px 18px; margin-right:4px; font-size:12px;"
            " color:#5a6068; border-radius:8px;}"
            "QTabBar::tab:hover{background:#eef1f6;}"
            "QTabBar::tab:selected{background:#e8f1fd; color:#1f6feb; font-weight:700;}"
        )

        self._chart = RealTimeChart()
        self._eis_panel = EISPanel()
        self._maturity_panel = MaturityPanel()
        self._tabs.addTab(self._chart, "传感器趋势")
        self._tabs.addTab(self._eis_panel, "阻抗谱 EIS")
        self._tabs.addTab(self._maturity_panel, "成熟度预测")
        # 每个页签都不许往上报自己的理想高度：谁高谁就把整列挤爆，
        # 底部那排功能键跟着被顶出窗口。图区拿不到就缩小自己，别抢别处的地方。
        for panel in (self._chart, self._eis_panel, self._maturity_panel):
            panel.setMinimumHeight(0)
        wrap.setMinimumHeight(180)
        lay.addWidget(self._tabs, stretch=1)
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

        self._sweep_btn = QPushButton("扫描详情")
        self._sweep_btn.setFixedWidth(90)
        self._sweep_btn.clicked.connect(self._open_sweep)
        lay.addWidget(self._sweep_btn)

        self._imp_btn = QPushButton("阻抗历史")
        self._imp_btn.setFixedWidth(90)
        self._imp_btn.clicked.connect(self._open_impedance)
        lay.addWidget(self._imp_btn)

        self._live_btn = QPushButton("实时阻抗")
        self._live_btn.setFixedWidth(90)
        self._live_btn.clicked.connect(self._open_live_sweep)
        lay.addWidget(self._live_btn)

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
        self._last_data_at = time.time()
        self._device_offline = False

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

        elif ptype == "sweep":
            self._msg_count += 1
            for point in data:
                if point.frequency_hz:
                    self._current_gw = point.gateway_id or self._current_gw
                    self._current_node = point.node_id or self._current_node
                    break
            self._handle_live_sweep(payload)

        elif ptype == "spectrum":
            self._msg_count += 1
            self._eis_panel.set_spectrum(data, payload.get("stats"))
            self._tabs.setTabText(1, f"阻抗谱 EIS · R{data.scan_id}")
            stats = payload.get("stats") or {}
            if stats.get("mean"):
                self._impedance_card.set_value(round(float(stats["mean"]), 1))
            prediction = payload.get("prediction")
            if prediction is not None:
                self._maturity_panel.set_prediction(prediction)
            self._handle_live_spectrum(payload)

        elif ptype == "prediction":
            self._maturity_panel.set_prediction(data)

        elif ptype == "round_done":
            # 网关的整轮摘要（走 status 通道），只做展示，不落库。
            self._msg_count += 1
            gw, node = data.gateway_id, data.node_id
            if gw:
                self._current_gw = gw
            if node:
                self._current_node = node
            total = payload.get("total_points")
            retry = payload.get("retry")
            bits = [f"轮次 {payload.get('round_id')}",
                    f"{payload.get('points')}/{total} 点"]
            if retry:
                bits.append(f"补发 {retry} 次")
            if payload.get("imp_mean"):
                bits.append(f"|Z|均值 {payload.get('imp_mean')} Ω")
            self._round_log.append((data.timestamp, " ".join(bits)))
            self._round_log = self._round_log[-8:]
            win = getattr(self, "_live_win", None)
            if win is not None and win.isVisible():
                win.show_round_done(payload)

    def _on_status(self, gw: str, node: str, status: str) -> None:
        if gw:
            self._current_gw = gw
            self._gw_label.setText(f"网关: {gw}")
        if node:
            self._current_node = node
            self._node_label.setText(f"节点: {node}")
        self._device_offline = status == "offline"
        if status == "online":
            self._last_heartbeat_at = time.time()
        self._refresh_status_light()

    def _on_gateway_info(self, gw: str, node: str, ip: str, rssi: float) -> None:
        """网关心跳带来的 IP / RSSI，显示在标题栏右侧。"""
        if gw:
            self._current_gw = gw
            self._gw_label.setText(f"网关: {gw}")
        if node:
            self._current_node = node
            self._node_label.setText(f"节点: {node}")
        self._gw_ip = ip or ""
        self._gw_rssi = float(rssi) if rssi else None
        bits = []
        if self._gw_ip:
            bits.append(self._gw_ip)
        if self._gw_rssi is not None:
            bits.append(f"{self._gw_rssi:.0f} dBm")
        self._gw_info_label.setText(
            ("WiFi " + " · ".join(bits)) if bits else "网关: 未上报")
        self._last_heartbeat_at = time.time()
        self._device_offline = False
        self._refresh_status_light()

    def _on_connected(self, connected: bool) -> None:
        self._connected = bool(connected)
        self._refresh_status_light()

    def _refresh_status_light(self) -> None:
        """按连接状态 + 数据/心跳新鲜度决定呼吸灯颜色。"""
        if not self._connected:
            self._status_light.set_state("offline", "未连接 Broker")
            return
        if self._device_offline:
            self._status_light.set_state("offline", "设备已上报离线")
            return
        alive_at = max(self._last_data_at, self._last_heartbeat_at)
        if alive_at <= 0.0:
            self._status_light.set_state("offline", "等待首包数据")
            return
        idle = time.time() - alive_at
        if idle <= self._stale_after_s:
            self._status_light.set_state("online", "数据流正常")
        elif self._last_heartbeat_at > 0.0 and (
                time.time() - self._last_heartbeat_at) <= self._heartbeat_stale_s:
            self._status_light.set_state(
                "online", f"在线 · 等待数据 {int(time.time() - self._last_data_at)}s")
        else:
            self._status_light.set_state("reconnecting", f"数据中断 {int(idle)}s")

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

    def _open_sweep(self) -> None:
        dlg = SweepDialog(
            self._db,
            self,
            default_gw=self._current_gw,
            default_node=self._current_node,
        )
        dlg.exec()

    def _open_impedance(self) -> None:
        dlg = ImpedanceDialog(
            self._db,
            self,
            default_gw=self._current_gw,
            default_node=self._current_node,
        )
        dlg.exec()

    def _open_live_sweep(self) -> None:
        """打开阻抗上报专用窗口。窗口打开期间每个报文都会即时刷新。"""
        min_points = int(self._config.get("sweep", {}).get("min_points", 50))
        self._live_win = LiveSweepWindow(self._db, self, min_points=min_points)
        self._live_win.show()
        self._live_win.refresh_from_db()
        self._apply_live()

    def _apply_live(self) -> None:
        """把最近一次报文喂给实时窗口（窗口关闭后跳过）。"""
        win = getattr(self, "_live_win", None)
        if win is None or not win.isVisible():
            return
        if self._latest_sweep_payload is not None:
            win.feed_report(self._latest_sweep_payload)
        if self._latest_spectrum_payload is not None:
            win.feed_spectrum(self._latest_spectrum_payload)

    def _handle_live_sweep(self, payload: dict) -> None:
        self._latest_sweep_payload = payload
        self._apply_live()

    def _handle_live_spectrum(self, payload: dict) -> None:
        self._latest_spectrum_payload = payload
        self._apply_live()

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
        self._refresh_status_light()

    def closeEvent(self, event) -> None:
        try:
            self._worker.stop()
            self._worker.wait(3000)
        except Exception:
            pass
        event.accept()
