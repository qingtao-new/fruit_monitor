"""双主题引擎：浅色 SaaS（默认）与深色玻璃拟态。

两套主题共用同一套 token 名，样式表、容器、图表、背景全部从
:func:`C` 取色，所以切换主题只是换 token 表 + 重建应用样式表。

浅色主题按设计稿走「SaaS 白纸卡片」：
- 背景 ``#f1f5f9`` 微灰，不压；卡片纯白 + 轻量扩散阴影，像浮起来的纸。
- 主色 ``#2563EB``，成功 ``#10B981``，异常 ``#EF4444``，都压掉荧光感。
- 网格线只留极淡横向 ``#f1f5f9``，数据本身当主角。

深色主题保留 Apple 玻璃拟态：``#07090e`` 底 + 柔光斑 + 渐变高光边。

Qt 的样式表做不到 CSS 的 backdrop-filter，所以「毛玻璃」靠两层实现：
背景真的在动（彩色光斑），容器用半透明底色把它透出来。渐变边框同理，
Qt 不支持 border-image 配 border-radius，用一个 1px 厚的外框垫住内层。

Qt 的样式表只认 alpha 在前的 ``#AARRGGBB``：写 ``#RRGGBBAA`` 会被当成
「蓝色多两位」，半透明全部失效（实测 ``QColor("#ffffff08")`` = 蓝 8 不透明）。
"""

from __future__ import annotations

import re

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QBrush,
    QLinearGradient,
    QPalette,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QFrame, QWidget

# ---- 公共字族 ----

FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', "
    "'PingFang SC', 'Microsoft YaHei', Roboto, Helvetica, Arial, sans-serif"
)

# 数字专用字族。PySide6 没有暴露 QFontFeatureSettings，拿不到 tnum 特性，
# 只能用等宽字族达到「刷新时数字不横向跳动」的效果。
NUM_FONT_STACK = (
    "'JetBrains Mono', 'Cascadia Mono', 'SF Mono', 'Roboto Mono', "
    "Consolas, Menlo, 'Courier New', monospace"
)

RADIUS = 16
RADIUS_SM = 12


def alpha(hex6: str, a: int) -> str:
    """给 #RRGGBB 加前置 alpha，返回 Qt 认的 #AARRGGBB。"""
    return f"#{a:02x}{hex6.lstrip('#').lower()}"


def alpha_soft(color: str, a: int) -> str:
    """给颜色加前置 alpha，但已经是 8 位 AARRGGBB 的原样返回。

    深色主题的 token 本身就可能带 alpha（如玻璃底 #0effffff），
    再套一层 alpha 会把它当成 6 位 RGB 错误拼接。
    """
    h = color.strip("#").lower()
    return color if len(h) == 8 else alpha(color, a)


# ---- 两套调色板（key 必须完全一致，_hex_table 靠位置配对）----

_DARK = {
    "bg_base": "#07090e",
    "surface": "#0b0e15",
    "text_primary": "#f3f4f6",
    "text_secondary": "#9ca3af",
    "text_muted": "#6b7280",
    "accent": "#0a84ff",
    "accent_deep": "#0062d2",
    "accent_violet": "#5e5ce6",
    "ok": "#30d158",
    "bad": "#ff453a",
    "flat": "#8a9099",
    "warn": "#ffd60a",
    "glass_bg": alpha("#ffffff", 0x08),
    "glass_bg_strong": alpha("#ffffff", 0x0f),
    "glass_hover": alpha("#ffffff", 0x12),
    "inset": alpha("#000000", 0x33),
    "grid": alpha("#ffffff", 0x0a),
    "border": alpha("#ffffff", 0x1a),
    "edge_hi": alpha("#ffffff", 0x30),
    "edge_lo": alpha("#ffffff", 0x0a),
    "accent_soft": alpha("#0a84ff", 0x22),
    "accent_mid": alpha("#0a84ff", 0x40),
    "accent_line": alpha("#0a84ff", 0x55),
    "accent_dim": alpha("#0a84ff", 0x33),
    "menu_bg": "#12161f",
    "card_fill": alpha("#ffffff", 0x08),
    "card_border": alpha("#ffffff", 0x1a),
    "card_shadow": "",
    "arc_track": alpha("#ffffff", 0x1a),
    "arc_caption": "#9ca3af",
    "scatter_pen": "#ffffff",
    "bode_mag": "#0a84ff",
    "bode_phase": "#ff6b63",
    "btn_primary_bg": "#ffffff",
    "btn_primary_fg": "#ffffff",
    "btn_primary_hover": "#2b93ff",
    "btn_primary_disabled": "#2a3040",
    "tab_selected_fg": "#ffffff",
    "hover_bg": alpha("#ffffff", 0x12),
    "sensor_temperature": "#ffd60a",
    "sensor_humidity": "#0a84ff",
    "sensor_co2": "#30d158",
    "sensor_ph": "#ff453a",
    "sensor_nh3": "#64d2ff",
    "sensor_h2s": "#ff9f0a",
    "sensor_soil_moisture": "#bf5af2",
    "maturity_unripe": "#0a84ff",
    "maturity_ripening": "#30d158",
    "maturity_ripe": "#ffd60a",
    "maturity_overripe": "#ff6b63",
}

_LIGHT = {
    "bg_base": "#f1f5f9",
    "surface": "#ffffff",
    "text_primary": "#0f172a",
    "text_secondary": "#64748b",
    "text_muted": "#94a3b8",
    "accent": "#2563eb",
    "accent_deep": "#1d4ed8",
    "accent_violet": "#7c3aed",
    "ok": "#10b981",
    "bad": "#ef4444",
    "flat": "#64748b",
    "warn": "#f59e0b",
    "glass_bg": "#ffffff",
    "glass_bg_strong": "#ffffff",
    "glass_hover": "#f8fafc",
    "inset": "#f8fafc",
    "grid": "#dfe6ee",
    "border": "#e2e8f0",
    "edge_hi": "#ffffff",
    "edge_lo": "#e2e8f0",
    "accent_soft": "#eff6ff",
    "accent_mid": "#bfdbfe",
    "accent_line": "#93c5fd",
    "accent_dim": "#dbeafe",
    "menu_bg": "#ffffff",
    "card_fill": "#ffffff",
    "card_border": "#e2e8f0",
    "card_shadow": "0 1px 3px 0 " + alpha("#0f172a", 0x10) + ", "
                   "0 6px 16px 0 " + alpha("#0f172a", 0x0c),
    "arc_track": "#e2e8f0",
    "arc_caption": "#94a3b8",
    "scatter_pen": "#0f172a",
    "bode_mag": "#2563eb",
    "bode_phase": "#f43f5e",
    "btn_primary_bg": "#2563eb",
    "btn_primary_fg": "#ffffff",
    "btn_primary_hover": "#1d4ed8",
    "btn_primary_disabled": "#dbe4f0",
    "tab_selected_fg": "#2563eb",
    "hover_bg": "#f1f5f9",
    "sensor_temperature": "#f59e0b",
    "sensor_humidity": "#2563eb",
    "sensor_co2": "#10b981",
    "sensor_ph": "#ef4444",
    "sensor_nh3": "#06b6d4",
    "sensor_h2s": "#f97316",
    "sensor_soil_moisture": "#8b5cf6",
    "maturity_unripe": "#2563eb",
    "maturity_ripening": "#10b981",
    "maturity_ripe": "#f59e0b",
    "maturity_overripe": "#ef4444",
}

DEFAULT_THEME = "light"
CURRENT: str | tuple[str, str] = DEFAULT_THEME


def _flat(name: str) -> dict[str, object]:
    return dict(_DARK if name == "dark" else _LIGHT)


def C(key: str) -> object:
    """取当前主题的 token 值；嵌套键写 ``sensor.temperature``（等价于 ``sensor_temperature``）。"""
    name = CURRENT[1] if isinstance(CURRENT, tuple) else CURRENT
    table = _DARK if name == "dark" else _LIGHT
    if key in table:
        return table[key]
    if "." in key:
        a, b = key.split(".", 1)
        group = table.get(a)
        if isinstance(group, dict) and b in group:
            return group[b]
        flat = table.get(key.replace(".", "_"))
        if flat is not None:
            return flat
    raise KeyError(f"theme token {key!r} not in {name!r}")


def current_name() -> str:
    return CURRENT[1] if isinstance(CURRENT, tuple) else CURRENT


def sensor_color(field: str) -> str:
    return str(C(f"sensor.{field}"))


def maturity_color(level: str) -> str:
    return str(C(f"maturity.{level}"))


def _hex_table(old_name: str, new_name: str) -> dict[str, str]:
    """旧主题 -> 新主题 的十六进制色映射，含 8 位 AARRGGBB 整串匹配。

    同一个十六进制色常常被好几个语义不同的 token 共用（#ffffff 在浅色系里
    既是 surface、glass_bg、menu_bg，又是 btn_primary_fg），所以必须「先声明者
    为准」，不能让后声明的把前面的覆盖掉——否则 #ffffff -> #ffffff，所有白卡片
    换深色主题后照样是白的。
    """
    o = _DARK if old_name == "dark" else _LIGHT
    n = _DARK if new_name == "dark" else _LIGHT
    table: dict[str, str] = {}
    for k in n:
        old_v = o.get(k)
        new_v = n.get(k)
        if not (isinstance(old_v, str) and old_v.startswith("#")):
            continue
        key = old_v.lower()
        if key in table:
            continue
        table[key] = str(new_v).lower()
    return table


_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?")


def remap_stylesheet(style_sheet: str, old_name: str, new_name: str,
                     table: dict[str, str]) -> str:
    """把一段样式表里的十六进制色整体搬到新主题。

    先按整串查（这样 6% 白的玻璃底能直接变成纯白），
    查不到再只换 RGB 部分、保留原来的 alpha。
    """
    def _rep(m: "re.Match[str]") -> str:
        s = m.group(0).lower()
        if s in table:
            return table[s]
        if len(s) == 9:
            alpha_part, rgb = s[:3], s[3:]
            if rgb in table:
                return alpha_part + table[rgb][1:]
        return s

    return _HEX_RE.sub(_rep, style_sheet)


def recolor(root: QWidget) -> int:
    """主题切换后把 root 子树刷一遍，返回改写的控件数。

    优先调用控件自己的 ``refresh_theme()``——它按 token 重建样式，语义不会歪；
    没有这个方法的才退回十六进制色替换。替换只是兜底，token 重名（#ffffff 在
    一套主题里被多个语义共用）时它一定会猜错。
    """
    if not isinstance(CURRENT, tuple):
        return 0
    old_name, new_name = CURRENT
    if old_name == new_name:
        return 0
    table = _hex_table(old_name, new_name)
    changed = 0
    for w in [root] + root.findChildren(QWidget):
        refresh = getattr(w, "refresh_theme", None)
        if callable(refresh):
            try:
                refresh()
                changed += 1
            except Exception:  # noqa: BLE001 - 单个控件坏掉不能拖垮整次切换
                continue
            continue
        ss = w.styleSheet()
        if not ss:
            continue
        new = remap_stylesheet(ss, old_name, new_name, table)
        if new != ss:
            w.setStyleSheet(new)
            changed += 1
    return changed


# ---- 调色板（给原生控件和系统对话框兜底）----

def _palette(name: str) -> QPalette:
    t = _DARK if name == "dark" else _LIGHT
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(t["bg_base"]))
    p.setColor(QPalette.ColorRole.WindowText, QColor(t["text_primary"]))
    p.setColor(QPalette.ColorRole.Base, QColor(t["surface"]))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(t["inset"]))
    p.setColor(QPalette.ColorRole.Text, QColor(t["text_primary"]))
    p.setColor(QPalette.ColorRole.Button, QColor(t["glass_bg_strong"]))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(t["text_primary"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(t["menu_bg"]))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(t["text_primary"]))
    p.setColor(QPalette.ColorRole.Highlight, QColor(t["accent"]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(t["text_muted"]))
    return p


# ---- 字体 ----

def base_font(size: int = 13, weight: int | None = None):
    from PySide6.QtGui import QFont
    font = QFont()
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setFamilies([part.strip().strip("'\"") for part in FONT_STACK.split(",")])
    font.setPixelSize(size)
    if weight is not None:
        font.setWeight(weight)
    return font


def num_qss(size: int = 13, weight: int = 700, color: str | None = None) -> str:
    """数字标签的样式片段，配进 setStyleSheet 用。"""
    color = color if color is not None else str(C("text_primary"))
    return (
        f"font-family: {NUM_FONT_STACK}; font-size: {size}px; "
        f"font-weight: {weight}; color: {color};"
    )


def lbl(size: int = 13, color_key: str = "text_primary",
        weight: int | None = None) -> str:
    """普通标签的样式片段。"""
    w = f"font-weight:{weight};" if weight else ""
    return f"font-size:{size}px; color:{C(color_key)}; {w}".rstrip()


# ---- 背景层 ----

BLOBS: dict[str, tuple[tuple[float, float, float, str, float], ...]] = {
    "dark": (
        (0.16, 0.10, 0.60, "accent", 0.34),
        (0.84, 0.24, 0.46, "ok", 0.15),
        (0.58, 0.98, 0.52, "accent_violet", 0.16),
        (0.04, 0.72, 0.34, "accent_deep", 0.14),
    ),
    # 浅色主题只用两三个极淡光斑，别把白纸背景染花。
    "light": (
        (0.14, 0.06, 0.58, "accent", 0.055),
        (0.90, 0.16, 0.44, "accent_violet", 0.040),
        (0.62, 1.00, 0.50, "ok", 0.030),
    ),
}


class GlowBackground(QWidget):
    """背景层：底色 + 几个大半径柔光斑，给卡片提供层次。"""

    def __init__(self, theme_name: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme_name = theme_name or current_name()

    def set_theme(self, theme_name: str) -> None:
        if self._theme_name != theme_name:
            self._theme_name = theme_name
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(str(C("bg_base"))))
        painter.setPen(Qt.PenStyle.NoPen)
        for fx, fy, fr, color_key, alpha_f in BLOBS[self._theme_name]:
            center_x = self.width() * fx
            center_y = self.height() * fy
            radius = max(40.0, min(self.width(), self.height()) * fr)
            gradient = QRadialGradient(center_x, center_y, radius)
            inner = QColor(str(C(color_key)))
            inner.setAlphaF(alpha_f)
            outer = QColor(str(C(color_key)))
            outer.setAlphaF(0.0)
            gradient.setColorAt(0.0, inner)
            gradient.setColorAt(1.0, outer)
            painter.setBrush(gradient)
            painter.drawEllipse(
                QRectF(center_x - radius, center_y - radius, radius * 2, radius * 2))
        painter.end()


# ---- 容器 ----

class GlassCard(QFrame):
    """标准卡片容器。

    浅色主题：纯白底 + 浅灰描边 + 扩散阴影，像浮起来的纸。
    深色主题：1px 线性渐变高光边（外框的渐变底露出 1px）+ 极低透明度玻璃底。

    阴影必须挂在同一个画底的 frame 上——Qt 的 box-shadow 不跟着子控件走，
    套两层的话阴影会被子控件的底盖掉。所以布局直接建在 self 上，
    ``card.body`` 就是 self 的别名，沿用 ``card.body`` 的写法不用改。

    **绝不在这里预建布局。** Qt 里给已经有布局的 widget 再 new 一个布局，
    新布局会变成孤儿（parentWidget() 为空），往里面 addWidget 的控件全部
    失去父级、变成顶层窗口——不报错、不告警，就是不显示。
    布局一律由调用方建：``lay = QHBoxLayout(card.body)``。

    用法：
        card = theme.GlassCard()
        lay = QVBoxLayout(card.body)
        lay.setContentsMargins(16, 14, 16, 14)
    """

    def __init__(self, parent: QWidget | None = None, radius: int = RADIUS,
                 background: str | None = None) -> None:
        super().__init__(parent)
        self._radius = radius
        self._background = background
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        object_name = f"glassOuter_{id(self) & 0xfffff:05x}"
        self.setObjectName(object_name)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._apply_style()
        self.body = self

    def _apply_style(self) -> None:
        """按当前主题重写自己的底/边/阴影，切换主题时重新调用。"""
        background = self._background if self._background is not None else str(C("card_fill"))
        shadow = str(C("card_shadow") or "").strip()
        self.setStyleSheet(
            f"#{self.objectName()}{{"
            f"border-radius:{self._radius}px;"
            f"border:1px solid {C('card_border')};"
            f"background:{background};"
            + (f"box-shadow:{shadow};" if shadow else "")
            + "}")

    def refresh_theme(self) -> None:
        """主题切换后重建自身样式表。"""
        self._apply_style()


# ---- 组件级样式 ----

def qss() -> str:
    """应用级样式表：一次覆盖按钮、表格、页签、控件、弹窗。"""
    t = _DARK if current_name() == "dark" else _LIGHT
    return f"""
* {{ outline: 0; }}

QMainWindow, QDialog, QMessageBox {{ background: {t['bg_base']}; }}

QLabel {{ color: {t['text_primary']}; font-family: {FONT_STACK}; font-size: 13px;
          background: transparent; }}

QWidget {{ color: {t['text_primary']}; font-family: {FONT_STACK}; font-size: 13px; }}

QPushButton {{
    background: {t['glass_bg_strong']}; color: {t['text_primary']};
    font-size: 13px; font-weight: 500;
    border: 1px solid {t['border']}; border-radius: 8px; padding: 7px 14px;
}}
QPushButton:hover {{ background: {t['hover_bg']}; border-color: {t['accent_line']}; }}
QPushButton:pressed {{ background: {t['accent_soft']}; padding: 8px 13px; }}
QPushButton:disabled {{ color: {t['text_muted']}; background: {t['glass_bg']}; }}

/* ---- 表格 ---- */
QTableWidget, QTableView, QTreeWidget, QListWidget {{
    background: transparent; alternate-background-color: transparent;
    border: 0px; color: {t['text_primary']}; font-size: 13px;
    gridline-color: {t['grid']}; selection-background-color: {t['accent_mid']};
    selection-color: {t['text_primary']};
}}
QTableWidget::item {{ padding: 6px 10px; border: 0px; }}
QTableWidget::item:hover, QTableView::item:hover {{ background: {t['hover_bg']}; }}
QTableWidget::item:selected {{ background: {t['accent_dim']}; }}

QHeaderView {{ background: transparent; border: 0px; }}
QHeaderView::section {{
    background: {t['inset']}; color: {t['text_secondary']};
    font-size: 11px; font-weight: 600; letter-spacing: 0.6px;
    padding: 9px 10px; border: 0px; border-bottom: 1px solid {t['border']};
}}
QTableCornerButton::section {{ background: {t['inset']}; border: 0px; }}
QTableWidget {{ vertical-alignment: middle; }}

QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {t['edge_lo']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t['accent_line']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {t['edge_lo']}; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {t['accent_line']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ---- 页签 ---- */
QTabWidget::pane {{ border: 0px; background: transparent; }}
QTabBar {{ background: transparent; qproperty-drawingBackground: false; }}
QTabBar::tab {{
    background: {t['glass_bg']}; color: {t['text_secondary']};
    padding: 8px 20px; margin-right: 6px; margin-bottom: 0;
    font-size: 13px; font-weight: 500; border-radius: {RADIUS_SM}px;
    border: 1px solid {t['border']};
}}
QTabBar::tab:hover {{ background: {t['hover_bg']}; color: {t['text_primary']}; }}
QTabBar::tab:selected {{
    background: {t['accent_soft']}; color: {t['tab_selected_fg']}; font-weight: 700;
    border: 1px solid {t['accent_line']};
}}

/* ---- 输入控件 ---- */
QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit {{
    background: {t['glass_bg_strong']}; color: {t['text_primary']};
    border: 1px solid {t['border']}; border-radius: 8px; padding: 6px 10px;
    font-size: 13px;
}}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QDateEdit:hover {{
    border-color: {t['accent_line']}; }}
QComboBox::drop-down {{
    border: 0px; width: 22px;
    background: {t['accent_soft']};
    border-top-right-radius: 8px; border-bottom-right-radius: 8px;
}}
QComboBox::down-arrow {{ width: 0; height: 0; }}
QComboBox QAbstractItemView {{
    background: {t['menu_bg']}; color: {t['text_primary']};
    border: 1px solid {t['border']}; border-radius: 8px;
    selection-background-color: {t['accent_mid']};
    selection-color: {t['text_primary']}; padding: 4px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent; border: 0px; width: 16px;
}}

QCheckBox {{ color: {t['text_secondary']}; font-size: 13px; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid {t['border']}; background: {t['glass_bg']};
}}
QCheckBox::indicator:hover {{ border-color: {t['accent_line']}; }}
QCheckBox::indicator:checked {{
    background: {t['accent']}; border-color: {t['accent']};
}}

QProgressBar {{
    background: {t['inset']}; border: 0px; border-radius: 6px;
    text-align: center; color: {t['text_primary']}; font-size: 11px;
}}
QProgressBar::chunk {{
    background: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0,
                                stop:0 {t['accent_deep']}, stop:1 {t['accent']});
    border-radius: 6px;
}}

QGroupBox {{
    border: 1px solid {t['border']}; border-radius: {RADIUS_SM}px;
    margin-top: 10px; padding-top: 10px;
    color: {t['text_secondary']}; font-size: 12px; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; }}

QToolTip {{
    background: {t['menu_bg']}; color: {t['text_primary']};
    border: 1px solid {t['border']}; border-radius: 6px; padding: 6px 9px;
}}
QMenu {{
    background: {t['menu_bg']}; color: {t['text_primary']};
    border: 1px solid {t['border']}; border-radius: 8px; padding: 5px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t['accent_mid']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 6px; }}

QStatusBar {{ background: transparent; color: {t['text_muted']}; }}
QSplitter::handle {{ background: {t['border']}; }}

QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus {{
    border-color: {t['accent']}; }}
"""


# ---- 主按钮：主色实底（浅色）/ 渐变发光（深色）----

def primary_button_qss() -> str:
    t = _DARK if current_name() == "dark" else _LIGHT
    if current_name() == "dark":
        normal = (f"background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,"
                  f"stop:0 {t['accent']}, stop:1 {t['accent_deep']});")
        hover = (f"background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,"
                 f"stop:0 {t['btn_primary_hover']}, stop:1 {t['accent']});")
        pressed = f"background:{t['accent_deep']};"
    else:
        normal = f"background:{t['accent']};"
        hover = f"background:{t['accent_deep']};"
        pressed = f"background:{t['accent_deep']};"
    return f"""
#btnPrimary {{
    {normal}
    color: {t['btn_primary_fg']}; font-size: 13px; font-weight: 600;
    border-radius: 8px; border: 0px; padding: 8px 18px;
}}
#btnPrimary:hover {{
    {hover}
}}
#btnPrimary:pressed {{ {pressed} padding: 9px 17px; }}
#btnPrimary:disabled {{ background: {t['btn_primary_disabled']}; color: {t['text_muted']}; }}
"""


def glass_button_qss() -> str:
    t = _DARK if current_name() == "dark" else _LIGHT
    return f"""
#btnGlass {{
    background: {t['glass_bg_strong']}; color: {t['text_primary']};
    font-size: 13px; font-weight: 500;
    border: 1px solid {t['border']}; border-radius: 8px; padding: 8px 16px;
}}
#btnGlass:hover {{ background: {t['hover_bg']}; border-color: {t['accent_line']}; }}
#btnGlass:pressed {{ background: {t['accent_soft']}; padding: 9px 15px; }}
#btnGlass:disabled {{ color: {t['text_muted']}; background: {t['glass_bg']}; }}
"""


def danger_button_qss() -> str:
    """次要 / 危险操作：只有细边框，不做实底，避免和主操作抢注意力。"""
    t = _DARK if current_name() == "dark" else _LIGHT
    return f"""
#btnGhost {{
    background: transparent; color: {t['text_secondary']};
    font-size: 13px; font-weight: 500;
    border: 1px solid {t['border']}; border-radius: 8px; padding: 8px 14px;
}}
#btnGhost:hover {{ background: {t['hover_bg']}; color: {t['text_primary']};
                   border-color: {t['accent_line']}; }}
#btnGhost:pressed {{ background: {t['accent_soft']}; padding: 9px 13px; }}
#btnGhost:disabled {{ color: {t['text_muted']}; }}
"""


def on_button_qss() -> str:
    """进行中状态的主色实底按钮（记录中、运行中）。"""
    t = _DARK if current_name() == "dark" else _LIGHT
    return f"""
#btnOn {{
    background: {t['accent']}; color: {t['btn_primary_fg']};
    font-size: 13px; font-weight: 600;
    border: 0px; border-radius: 8px; padding: 8px 14px;
}}
#btnOn:hover {{ background: {t['btn_primary_hover']}; }}
#btnOn:pressed {{ background: {t['accent_deep']}; padding: 9px 13px; }}
"""


def pill_qss() -> str:
    t = _DARK if current_name() == "dark" else _LIGHT
    return f"""
#pillOk {{ background: {alpha(t['ok'], 0x26)}; color: {t['ok']};
           border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 700; }}
#pillBad {{ background: {alpha(t['bad'], 0x26)}; color: {t['bad']};
             border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 700; }}
#pillFlat {{ background: {alpha(t['flat'], 0x26)}; color: {t['flat']};
             border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 700; }}
#pillWarn {{ background: {alpha(t['warn'], 0x26)}; color: {t['warn']};
             border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 700; }}
"""


def badge_qss() -> str:
    """顶栏的状态胶囊：圆角、细边、留位给前面的呼吸小圆点。"""
    t = _DARK if current_name() == "dark" else _LIGHT
    return f"""
#badge {{ background: {t['glass_bg_strong']}; color: {t['text_secondary']};
          border: 1px solid {t['border']}; border-radius: 999px;
          padding: 4px 12px; font-size: 12px; font-weight: 500; }}
#badgeOn {{ background: {alpha(t['ok'], 0x1a)}; color: {t['ok']};
            border: 1px solid {alpha(t['ok'], 0x40)}; border-radius: 999px;
            padding: 4px 12px; font-size: 12px; font-weight: 600; }}
#badgeOff {{ background: {alpha(t['flat'], 0x1a)}; color: {t['flat']};
             border: 1px solid {t['border']}; border-radius: 999px;
             padding: 4px 12px; font-size: 12px; font-weight: 600; }}
"""


def all_qss() -> str:
    return qss() + primary_button_qss() + glass_button_qss() + danger_button_qss() \
        + on_button_qss() + pill_qss() + badge_qss()


def apply_theme(app, name: str = DEFAULT_THEME) -> str:
    """切换主题并重建应用样式表；返回生效的主题名。"""
    global CURRENT
    target = name if name in BLOBS else DEFAULT_THEME
    previous = current_name()
    CURRENT = target
    app.setFont(base_font(13))
    app.setPalette(_palette(target))
    app.setStyleSheet(all_qss())
    if previous != target:
        CURRENT = (previous, target)  # 标记：调用方需要 recolor 已创建的控件
    return target


def apply(app) -> str:
    """兼容旧调用：按默认主题应用。"""
    return apply_theme(app)


def toggle_theme(app, root: QWidget | None = None) -> str:
    """在浅/深之间切换；传 root 时会顺带重写子树里的内联样式表。"""
    previous = current_name()
    target = "dark" if previous == "light" else "light"
    apply_theme(app, target)
    if root is not None:
        recolor(root)
        for w in root.findChildren(QWidget):
            if hasattr(w, "set_theme"):
                w.set_theme(target)
    CURRENT = target
    return target


def pen(color: str, width: float = 2.0, style=None) -> QPen:
    p = QPen(QColor(color))
    p.setWidthF(width)
    if style is not None:
        p.setStyle(style)
    return p


# ---- pyqtgraph 深色/浅色统一处理 ----

def style_plot(plot, *, x_grid: bool = False, y_grid: bool = True,
               grid_alpha: float = 0.14) -> None:
    """给 pyqtgraph 图表上当前主题的底色、网格和坐标轴配色。

    默认关掉竖直网格——竖线一多，曲线反而读不出来；横向网格只留一条极弱的。
    pyqtgraph 0.14 里网格线的颜色取自 AxisItem 的 tickPen（刻度和网格共用一支笔），
    而 showGrid 的 alpha 参数只认 0..1 的浮点，不接受 QPen，所以颜色与透明度分开给。
    """
    plot.setBackground(str(C("surface")))
    item = plot.getPlotItem()
    axis_pen = QPen(QColor(str(C("text_secondary"))))
    axis_pen.setWidthF(1.0)
    grid_pen = QPen(QColor(str(C("grid"))))
    grid_pen.setWidthF(1.0)
    # 0.14 的 axes 是 {方向: {"item": AxisItem, "visible": bool}}，
    # 直接遍历 values() 拿到的是内层 dict，取不到笔。
    for info in item.axes.values():
        axis = info["item"] if isinstance(info, dict) else info
        axis.setPen(axis_pen)
        axis.setTickPen(grid_pen)
        axis.setTextPen(axis_pen)
        if hasattr(axis, "setOffsetPen"):
            axis.setOffsetPen(axis_pen)
    item.showGrid(x=x_grid, y=y_grid, alpha=float(grid_alpha))
    plot.setMenuEnabled(False)


def area_brush(color: str, top_alpha: int = 0x40) -> QBrush:
    """面积图渐变：从曲线处的半透明，沉到基线处完全透明。

    用 ObjectMode，渐变跟着曲线自身的外框走，窗口怎么拉都不会错位。
    """
    gradient = QLinearGradient(0, 0, 0, 1)
    gradient.setCoordinateMode(QLinearGradient.ObjectMode)
    top = QColor(color)
    top.setAlpha(top_alpha)
    bottom = QColor(color)
    bottom.setAlpha(0)
    gradient.setColorAt(0.0, top)
    gradient.setColorAt(1.0, bottom)
    return QBrush(gradient)
