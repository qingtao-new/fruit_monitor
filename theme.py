"""苹果风深色毛玻璃主题：色板、全局样式表、氛围光背景、玻璃容器。

对应设计稿的四个层次：
- 背景层：#07090e 极深蓝灰 + 大半径高斯式柔光斑（GlowBackground）。
- 容器层：GlassCard，1px 线性渐变高光边 + 极低透明度毛玻璃底。
- 内容层：文字三级灰度，数字等宽。
- 交互层：按钮渐变发光、悬停上浮、按下缩放。

Qt 的样式表做不到 CSS 的 backdrop-filter，所以「毛玻璃」靠两层实现：
背景真的在动（彩色光斑），容器用半透明底色把它透出来。渐变边框同理，
Qt 不支持 border-image 配 border-radius，用一个 1px 厚的外框垫住内层。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QBrush,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

# ---- 色板 ----
BG_BASE = "#07090e"
SURFACE = "#0b0e15"

TEXT_PRIMARY = "#f3f4f6"
TEXT_SECONDARY = "#9ca3af"
TEXT_MUTED = "#6b7280"

ACCENT = "#0a84ff"
ACCENT_DEEP = "#0062d2"
ACCENT_VIOLET = "#5e5ce6"
UP = "#30d158"
DOWN = "#ff453a"
FLAT = "#8a9099"
WARN = "#ffd60a"

# Qt 的样式表只认 alpha 在前的 #AARRGGBB：写 #RRGGBBAA 会被当成
# 「蓝色多两位」，半透明全部失效（实测 QColor("#ffffff08") = 蓝 8 不透明）。
def alpha(hex6: str, a: int) -> str:
    """给 #RRGGBB 加前置 alpha，返回 Qt 认的 #AARRGGBB。"""
    return f"#{a:02x}{hex6.lstrip('#').lower()}"


GLASS_BG = alpha("#ffffff", 0x08)      # 白 3%
GLASS_BG_STRONG = alpha("#ffffff", 0x0f)  # 白 6%
GLASS_HOVER = alpha("#ffffff", 0x12)    # 白 7%
INSET = alpha("#000000", 0x33)          # 表头比卡片略深
GRID = alpha("#ffffff", 0x0a)           # 横向网格线，几乎看不见
BORDER = alpha("#ffffff", 0x1a)         # 常规 1px 边框
EDGE_HI = alpha("#ffffff", 0x30)        # 渐变边的高光端
EDGE_LO = alpha("#ffffff", 0x0a)        # 渐变边的暗端
ACCENT_SOFT = alpha(ACCENT, 0x22)
ACCENT_MID = alpha(ACCENT, 0x40)
ACCENT_LINE = alpha(ACCENT, 0x55)
ACCENT_DIM = alpha(ACCENT, 0x33)

RADIUS = 16
RADIUS_SM = 10

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


def base_font(size: int = 13, weight: int | None = None) -> QFont:
    """全局正文字体。"""
    font = QFont()
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setFamilies([part.strip().strip("'\"") for part in FONT_STACK.split(",")])
    font.setPixelSize(size)
    if weight is not None:
        font.setWeight(weight)
    return font


def num_qss(size: int = 13, weight: int = 700, color: str = TEXT_PRIMARY) -> str:
    """数字标签的样式片段，配进 setStyleSheet 用。"""
    return (
        f"font-family: {NUM_FONT_STACK}; font-size: {size}px; "
        f"font-weight: {weight}; color: {color};"
    )


class GlowBackground(QWidget):
    """背景层：极深底色 + 几个大半径柔光斑，给毛玻璃提供折射源。"""

    BLOBS = (
        (0.16, 0.10, 0.60, ACCENT, 0.34),
        (0.84, 0.24, 0.46, UP, 0.15),
        (0.58, 0.98, 0.52, ACCENT_VIOLET, 0.16),
        (0.04, 0.72, 0.34, ACCENT_DEEP, 0.14),
    )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(BG_BASE))
        painter.setPen(Qt.PenStyle.NoPen)
        for fx, fy, fr, color, alpha in self.BLOBS:
            center_x = self.width() * fx
            center_y = self.height() * fy
            radius = max(40.0, min(self.width(), self.height()) * fr)
            gradient = QRadialGradient(center_x, center_y, radius)
            inner = QColor(color)
            inner.setAlphaF(alpha)
            outer = QColor(color)
            outer.setAlphaF(0.0)
            gradient.setColorAt(0.0, inner)
            gradient.setColorAt(1.0, outer)
            painter.setBrush(gradient)
            rect = QRectF(
                center_x - radius, center_y - radius, radius * 2, radius * 2)
            painter.drawEllipse(rect)
        painter.end()


class GlassCard(QFrame):
    """标准玻璃容器。

    用法：
        card = theme.GlassCard()
        lay = QVBoxLayout(card.body)
        lay.setContentsMargins(16, 14, 16, 14)

    外层画渐变高光边，内层 card.body 放内容和自己的布局。
    """

    def __init__(self, parent: QWidget | None = None, radius: int = RADIUS,
                 border: int = 1, background: str = GLASS_BG,
                 margin: int = 0) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        object_name = f"glassOuter_{id(self) & 0xfffff:05x}"
        self.setObjectName(object_name)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            f"#{object_name}{{"
            f"border-radius:{radius}px;"
            f"border:0px solid transparent;"
            f"background:qlineargradient(spread:pad,x1:0,y1:0,x2:0.55,y2:1,"
            f"stop:0 {EDGE_HI},stop:0.55 {EDGE_LO},stop:1 {EDGE_LO});"
            f"}}")

        lay = QVBoxLayout(self)
        # 外层留白 = 渐变边厚度 + 额外内距。边是外层背景露出来的一圈，
        # 所以 body 必须往里缩 border 像素，否则那圈渐变被自己盖住。
        edge = border + margin
        lay.setContentsMargins(edge, edge, edge, edge)
        lay.setSpacing(0)

        self.body = QFrame(self)
        self.body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body_name = f"glassInner_{id(self.body) & 0xfffff:05x}"
        self.body.setObjectName(body_name)
        self.body.setStyleSheet(
            f"#{body_name}{{"
            f"border-radius:{radius - border}px;"
            f"background:{background};}}")
        lay.addWidget(self.body)


# ---- 组件级样式（objectName 打头，避免和控件级样式表打架）----

PRIMARY_BUTTON_QSS = f"""
#btnPrimary {{
    background: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,
                                stop:0 {ACCENT}, stop:1 {ACCENT_DEEP});
    color: #ffffff; font-size: 13px; font-weight: 600;
    border-radius: 10px; border: 0px; padding: 8px 18px;
}}
#btnPrimary:hover {{
    background: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,
                                stop:0 #2b93ff, stop:1 {ACCENT});
}}
#btnPrimary:pressed {{ background: {ACCENT_DEEP}; padding: 9px 17px; }}
#btnPrimary:disabled {{ background: #2a3040; color: #6b7280; }}
"""

GLASS_BUTTON_QSS = f"""
#btnGlass {{
    background: {GLASS_BG_STRONG}; color: {TEXT_PRIMARY};
    font-size: 13px; font-weight: 500;
    border: 1px solid {BORDER}; border-radius: 10px; padding: 8px 16px;
}}
#btnGlass:hover {{ background: {GLASS_HOVER}; border-color: {EDGE_HI}; }}
#btnGlass:pressed {{ padding: 9px 15px; }}
#btnGlass:disabled {{ color: {TEXT_MUTED}; background: {GLASS_BG}; }}
"""

PILL_QSS = f"""
#pillUp {{ background: {alpha(UP, 0x26)}; color: {UP}; border-radius: 9px;
           padding: 2px 8px; font-size: 11px; font-weight: 700; }}
#pillDown {{ background: {alpha(DOWN, 0x26)}; color: {DOWN}; border-radius: 9px;
             padding: 2px 8px; font-size: 11px; font-weight: 700; }}
#pillFlat {{ background: {alpha(FLAT, 0x26)}; color: {FLAT}; border-radius: 9px;
             padding: 2px 8px; font-size: 11px; font-weight: 700; }}
"""


def qss() -> str:
    """应用级样式表：一次覆盖按钮、表格、页签、控件、弹窗。"""
    return f"""
* {{ outline: 0; }}

/* 顶层容器给底色，别让 QDialog 透出纯黑；主窗口的发光背景会盖住它。 */
QMainWindow, QDialog, QMessageBox {{ background: {BG_BASE}; }}

QLabel {{ color: {TEXT_PRIMARY}; font-family: {FONT_STACK}; font-size: 13px;
          background: transparent; }}

QWidget {{ color: {TEXT_PRIMARY}; font-family: {FONT_STACK}; font-size: 13px; }}

QPushButton {{
    background: {GLASS_BG_STRONG}; color: {TEXT_PRIMARY};
    font-size: 13px; font-weight: 500;
    border: 1px solid {BORDER}; border-radius: 10px; padding: 7px 14px;
}}
QPushButton:hover {{ background: {GLASS_HOVER}; border-color: {EDGE_HI}; }}
QPushButton:pressed {{ padding: 8px 13px; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: {GLASS_BG}; }}

/* ---- 表格 ---- */
QTableWidget, QTableView, QTreeWidget, QListWidget {{
    background: transparent; alternate-background-color: transparent;
    border: 0px; color: {TEXT_PRIMARY}; font-size: 13px;
    gridline-color: {GRID}; selection-background-color: {ACCENT_MID};
    selection-color: {TEXT_PRIMARY};
}}
QTableWidget::item {{ padding: 6px 10px; border: 0px; }}
QTableWidget::item:hover, QTableView::item:hover {{ background: {GLASS_HOVER}; }}
QTableWidget::item:selected {{ background: {ACCENT_DIM}; }}

QHeaderView {{ background: transparent; border: 0px; }}
QHeaderView::section {{
    background: {INSET}; color: {TEXT_SECONDARY};
    font-size: 11px; font-weight: 600; letter-spacing: 0.6px;
    padding: 9px 10px; border: 0px; border-bottom: 1px solid {BORDER};
}}
QTableCornerButton::section {{ background: {INSET}; border: 0px; }}
QTableWidget {{ vertical-alignment: middle; }}

QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {EDGE_LO}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {EDGE_HI}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {EDGE_LO}; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {EDGE_HI}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ---- 页签 ---- */
QTabWidget::pane {{ border: 0px; background: transparent; }}
QTabBar {{ background: transparent; qproperty-drawingBackground: false; }}
QTabBar::tab {{
    background: {GLASS_BG}; color: {TEXT_SECONDARY};
    padding: 8px 20px; margin-right: 6px; margin-bottom: 0;
    font-size: 13px; font-weight: 500; border-radius: 10px;
    border: 1px solid transparent;
}}
QTabBar::tab:hover {{ background: {GLASS_HOVER}; color: {TEXT_PRIMARY}; }}
QTabBar::tab:selected {{
    background: {ACCENT_SOFT}; color: #ffffff; font-weight: 700;
    border: 1px solid {ACCENT_LINE};
}}

/* ---- 输入控件 ---- */
QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit {{
    background: {GLASS_BG_STRONG}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER}; border-radius: 9px; padding: 6px 10px;
    font-size: 13px;
}}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {EDGE_HI}; }}
QComboBox::drop-down {{
    border: 0px; width: 22px;
    background: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,
                                stop:0 {ACCENT}, stop:1 {ACCENT_DEEP});
    border-top-right-radius: 9px; border-bottom-right-radius: 9px;
}}
QComboBox::down-arrow {{ width: 0; height: 0; }}
QComboBox QAbstractItemView {{
    background: #12161f; color: {TEXT_PRIMARY}; border: 1px solid {BORDER};
    border-radius: 8px; selection-background-color: {ACCENT_MID};
    selection-color: #ffffff; padding: 4px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent; border: 0px; width: 16px;
}}

QCheckBox {{ color: {TEXT_SECONDARY}; font-size: 13px; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid {BORDER}; background: {GLASS_BG};
}}
QCheckBox::indicator:hover {{ border-color: {EDGE_HI}; }}
QCheckBox::indicator:checked {{
    background: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,
                                stop:0 {ACCENT}, stop:1 {ACCENT_DEEP});
    border-color: {ACCENT};
}}

QProgressBar {{
    background: {INSET}; border: 0px; border-radius: 6px;
    text-align: center; color: {TEXT_PRIMARY}; font-size: 11px;
}}
QProgressBar::chunk {{
    background: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0,
                                stop:0 {ACCENT_DEEP}, stop:1 {ACCENT});
    border-radius: 6px;
}}

QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 12px;
    margin-top: 10px; padding-top: 10px;
    color: {TEXT_SECONDARY}; font-size: 12px; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; }}

QToolTip {{
    background: #12161f; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 9px;
}}
QMenu {{
    background: #12161f; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER}; border-radius: 8px; padding: 5px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_MID}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}

QStatusBar {{ background: transparent; color: {TEXT_MUTED}; }}
QSplitter::handle {{ background: {BORDER}; }}
"""


def apply(app) -> None:
    """把主题装到 QApplication 上。"""
    app.setFont(base_font(13))
    app.setStyleSheet(qss() + PRIMARY_BUTTON_QSS + GLASS_BUTTON_QSS + PILL_QSS)


def pen(color: str, width: float = 2.0, style=None) -> QPen:
    return QPen(QColor(color), width) if style is None else QPen(QColor(color), width, style)


def style_plot(plot, *, x_grid: bool = False, y_grid: bool = True,
               grid_alpha: float = 0.14) -> None:
    """pyqtgraph 深色化。

    默认关掉竖直网格——竖线一多，曲线反而读不出来；横向网格只留一条极弱的。
    """
    plot.setBackground(SURFACE)
    plot.showGrid(x=x_grid, y=y_grid, alpha=grid_alpha)
    # 0.14 的 axes 是 {方向: {"item": AxisItem, "visible": bool}}，
    # 直接遍历 values() 拿到的是内层 dict，取不到笔。
    axis_pen = QPen(QColor(TEXT_SECONDARY))
    axis_pen.setWidthF(1.0)
    axis_text = QPen(QColor(TEXT_SECONDARY))
    for info in plot.getPlotItem().axes.values():
        axis = info["item"] if isinstance(info, dict) else info
        axis.setPen(axis_pen)
        axis.setTickPen(axis_pen)
        axis.setTextPen(axis_text)
        # 0.14 没有 setOffsetPen，新版本才有；用 getattr 兼容两边。
        if hasattr(axis, "setOffsetPen"):
            axis.setOffsetPen(axis_pen)
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
