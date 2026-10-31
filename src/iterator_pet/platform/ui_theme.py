"""界面主题：照雨世界**菜单**的美术语言（读的是游戏代码与图集，不是凭印象）。

* 颜色 = ``Menu.Menu::MenuColor`` 的 HSL 原值：White、MediumGrey（带一点紫的灰）、DarkGrey、
  VeryDarkGrey、DarkRed、SaturatedGold（游戏里的高亮金）。闲置是 MediumGrey，悬停 / 选中变 White。
  中间几档灰（LINE / SURFACE / FAINT）是同一个色相与饱和度、只改亮度插出来的。
* 框 = ``Menu.RoundedRect``：圆角 7（``UIroundedCorner`` 7×7），黑色半透明填充。窗口外框用游戏的
  2 px；框里的控件一律 1 px——两档粗细才有层次，处处 2 px 会显得笨重。
* 字 = 游戏烘焙字体的原字面：拉丁 Segoe UI（``font`` = Segoe UI Bold 15，``DisplayFont`` =
  Segoe UI Semibold 30），中文 Noto Sans SC Medium（``fontChi``）；没装 Noto 就退到雅黑。
* 图 = 从用户本机游戏的 ``uiSprites`` 图集切出来的（assets_extractor.ensure_ui_sprites）：
  两头渐隐的 ``listDivider`` 做标题 / 分节线，``Menu_Symbol_CheckBox`` 做勾，箭头做子菜单标记、
  发送键。切不到就用画出来的替身，不随仓库分发任何游戏素材。

**尺寸只从下面几个常量来**（对齐不齐是"廉价感"的头号来源）：内容左右边距 ``PAD``，块间距
``GAP``，控件高 ``CONTROL_H``，按钮高 ``BUTTON_H``。标题栏、分隔线、正文的左右边都对齐到 ``PAD``。
"""

from __future__ import annotations

import colorsys
from math import cos, pi, sin
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)


def _hsl(h: float, s: float, l: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


_HUE, _SAT = 0.73055553, 0.08                  # MenuColor 的灰都是这个色相 / 饱和度

# Menu.Menu::MenuColor（IL 常量原样）
WHITE = _hsl(0.0, 0.0, 1.0)
MEDIUM_GREY = _hsl(_HUE, _SAT, 0.67)
DARK_GREY = _hsl(_HUE, _SAT, 0.30)
VERY_DARK_GREY = _hsl(_HUE, _SAT, 0.15)
GOLD = _hsl(0.094444446, 1.0, 0.685)
RED_HOVER = _hsl(0.033333335, 0.65, 0.55)      # DarkRed 提亮，当按钮文字才看得清
# 同色相插出来的几档
FAINT = _hsl(_HUE, _SAT, 0.47)                 # 次要文字（时间戳、说明）
LINE = _hsl(_HUE, _SAT, 0.24)                  # 1 px 描边
SURFACE = _hsl(_HUE, _SAT, 0.11)               # 卡片 / 你说的话
BASE = _hsl(_HUE, _SAT, 0.035)                 # 窗口底

# ---- 尺寸 token ----
PAD = 20            # 内容左右边距（标题、分隔线、正文都对齐到这里）
GAP = 12            # 块与块
GAP_S = 8           # 块内
CONTROL_H = 34      # 输入框 / 方形图标按钮
BUTTON_H = 32       # 文字按钮
RADIUS = 7.0        # UIroundedCorner
FRAME_BORDER = 2.0  # 窗口外框（游戏 RoundedRect 的 2 px）
SHADOW = 14         # 窗口外的投影留白
FILL_ALPHA = 0.94   # RoundedRect 是 0.75；独立窗口压在花花的桌面上，压深一点才读得清

ACCENTS = {"pebbles": "#ff9a4d", "past_moon": "#8fb8ff"}
NAMES = {"pebbles": "五块卵石", "past_moon": "仰望皓月"}
PRONOUNS = {"pebbles": "他", "past_moon": "她"}

UI_FONT = '"Segoe UI", "Noto Sans SC Medium", "Noto Sans SC", "Microsoft YaHei UI", sans-serif'
MONO_FONT = '"Cascadia Mono", "Consolas", "Noto Sans SC Medium", "Microsoft YaHei UI", monospace'


def accent_for(character: str) -> str:
    return ACCENTS.get(character, ACCENTS["pebbles"])


def ui_sprite(name: str) -> Path | None:
    """缓存里的游戏菜单图元；没有就 None（调用方退回画的样子）。"""
    try:
        from ..assets_extractor import ui_dir

        path = ui_dir() / f"{name}.png"
        return path if path.is_file() else None
    except Exception:
        return None


def _url(name: str) -> str | None:
    path = ui_sprite(name)
    return path.as_posix() if path else None


def app_stylesheet(accent: str) -> str:
    check = _url("Menu_Symbol_CheckBox")
    arrow = _url("arrow_right")
    checked_rule = f"image: url({check});" if check else f"background: {GOLD};"
    arrow_rule = (f"QMenu::right-arrow {{ image: url({arrow}); width: 6px; height: 10px; margin-right: 10px; }}"
                  if arrow else "")
    r = int(RADIUS)
    # 按钮：边框 1 + 内边距 0 + min-height → 总高 BUTTON_H；输入框同理 → CONTROL_H
    btn_inner = BUTTON_H - 2
    input_inner = CONTROL_H - 2 - 2 * 7
    return f"""
    QWidget {{
        color: {WHITE};
        font-family: {UI_FONT};
        font-size: 13px;
    }}

    /* ---- 字级：14 标题 / 13 正文 / 12 次要 / 11 注脚 ---- */
    QLabel#title {{ font-size: 14px; font-weight: 600; }}
    QLabel#muted, QLabel#status {{ color: {FAINT}; font-size: 12px; }}
    QLabel#section {{ color: {MEDIUM_GREY}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
    QLabel#key {{ color: {FAINT}; font-size: 12px; }}
    QLabel#value {{ color: {WHITE}; font-size: 13px; }}
    QLabel#bigNumber {{ font-size: 22px; font-weight: 600; }}
    QLabel#cardCaption {{ color: {FAINT}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
    QLabel#cardNote {{ color: {FAINT}; font-size: 11px; }}
    QLabel#choiceTitle {{ font-size: 13px; font-weight: 600; }}
    QLabel#choiceDetail {{ color: {FAINT}; font-size: 12px; }}
    QLabel#warn {{ color: {GOLD}; font-size: 12px; }}
    QLabel#empty {{ color: {FAINT}; font-size: 12px; }}

    /* ==== 框里的块一律不描边，靠底色深浅分层（外框才是游戏的 RoundedRect）==== */
    QFrame#card {{
        background: rgba(255, 255, 255, 0.045);
        border: none;
        border-radius: {r}px;
    }}

    /* ---- 按钮：次要 = 浅色底；主要 = 实心反色；悬停变亮（游戏里是灰→白）---- */
    QPushButton {{
        background: rgba(255, 255, 255, 0.07);
        border: 1px solid transparent;
        border-radius: {r}px;
        min-height: {btn_inner}px;
        max-height: {btn_inner}px;
        padding: 0px 16px;
        color: {MEDIUM_GREY};
        font-weight: 600;
    }}
    QPushButton:hover {{ background: rgba(255, 255, 255, 0.12); color: {WHITE}; }}
    QPushButton:pressed {{ background: rgba(255, 255, 255, 0.17); }}
    QPushButton:disabled {{ background: rgba(255, 255, 255, 0.03); color: {DARK_GREY}; }}
    QPushButton#primary {{ background: {WHITE}; color: {BASE}; }}
    QPushButton#primary:hover {{ background: {GOLD}; color: {BASE}; }}
    QPushButton#primary:pressed {{ background: {MEDIUM_GREY}; }}
    QPushButton#danger:hover {{ background: rgba(224, 96, 80, 0.16); color: {RED_HOVER}; }}
    QPushButton#square {{
        min-height: {CONTROL_H - 2}px; max-height: {CONTROL_H - 2}px;
        min-width: {CONTROL_H - 2}px; max-width: {CONTROL_H - 2}px;
        padding: 0px;
    }}
    QPushButton#titleButton {{
        background: transparent; border: none; border-radius: 4px; padding: 0px;
        min-height: 24px; max-height: 24px; min-width: 24px; max-width: 24px;
        color: {FAINT}; font-size: 17px; font-weight: 400;
    }}
    QPushButton#titleButton:hover {{ color: {WHITE}; background: rgba(255, 255, 255, 0.06); }}

    /* ---- 输入框 ---- */
    /* ---- 输入框：浅色底，聚焦才出现一圈极淡的线 ---- */
    QLineEdit {{
        background: rgba(255, 255, 255, 0.055);
        border: 1px solid transparent;
        border-radius: {r}px;
        padding: 7px 11px;
        min-height: {input_inner}px;
        selection-background-color: {GOLD};
        selection-color: #000000;
    }}
    QPlainTextEdit {{
        background: rgba(255, 255, 255, 0.055);
        border: 1px solid transparent;
        border-radius: {r}px;
        padding: 6px 7px;
        selection-background-color: {GOLD};
        selection-color: #000000;
    }}
    QLineEdit:focus, QPlainTextEdit:focus {{
        background: rgba(255, 255, 255, 0.07);
        border-color: rgba(255, 255, 255, 0.22);
    }}

    /* ---- 选择卡（权限）：选中 = 更亮的底 + 游戏的勾，不加框 ---- */
    QPushButton#choice {{
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid transparent;
        min-height: 0px; max-height: 1000px;
        padding: 0px;
    }}
    QPushButton#choice:hover {{ background: rgba(255, 255, 255, 0.07); }}
    QPushButton#choice:checked {{ background: rgba(255, 255, 255, 0.11); }}

    /* ---- 对话记录：它说的偏暗、你说的偏亮，都不描边 ---- */
    QScrollArea, QWidget#transcript {{ background: transparent; border: none; }}
    QFrame#petMsg {{ background: rgba(255, 255, 255, 0.065); border: none; border-radius: {r}px; }}
    QFrame#userMsg {{ background: rgba(255, 255, 255, 0.13); border: none; border-radius: {r}px; }}
    QFrame#taskMsg {{ background: rgba(0, 0, 0, 0.32); border: none; border-radius: {r}px; }}
    QFrame#approval {{ background: rgba(255, 186, 92, 0.09); border: none; border-radius: {r}px; }}
    QLabel#msgMeta {{ color: {FAINT}; font-size: 11px; }}
    QLabel#taskMeta {{ color: {GOLD}; font-size: 11px; font-weight: 600; }}
    QLabel#msgText {{ font-size: 13px; }}
    QLabel#taskText {{ font-family: {MONO_FONT}; font-size: 12px; color: {MEDIUM_GREY}; }}
    QLabel#systemText {{ color: {FAINT}; font-size: 12px; }}

    /* ---- 进度条：金 ---- */
    QProgressBar {{
        background: rgba(255, 255, 255, 0.06);
        border: none;
        border-radius: 2px;
        color: transparent;
    }}
    QProgressBar::chunk {{ background: {GOLD}; border-radius: 2px; }}

    /* ---- 滚动条：细，悬停才显眼 ---- */
    QScrollBar:vertical {{ background: transparent; width: 6px; margin: 0px; }}
    QScrollBar::handle:vertical {{ background: {LINE}; min-height: 28px; border-radius: 3px; }}
    QScrollBar::handle:vertical:hover {{ background: {DARK_GREY}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: none; height: 0px;
    }}

    /* ---- 菜单（右键 / 托盘）：游戏的勾和箭头 ---- */
    QMenu {{
        background: {BASE};
        border: 1px solid {DARK_GREY};
        border-radius: 0px;
        padding: 6px 0px;
    }}
    QMenu::item {{ padding: 7px 34px 7px 16px; color: {MEDIUM_GREY}; background: transparent; }}
    QMenu::item:selected {{ color: {WHITE}; background: {SURFACE}; }}
    QMenu::item:disabled {{ color: {DARK_GREY}; }}
    QMenu::separator {{ height: 1px; background: {LINE}; margin: 6px 14px; }}
    QMenu::indicator {{ width: 14px; height: 12px; margin-left: 8px; }}
    QMenu::indicator:checked {{ {checked_rule} }}
    QMenu::indicator:unchecked {{ image: none; }}
    {arrow_rule}

    QToolTip {{ background: {BASE}; color: {WHITE}; border: 1px solid {DARK_GREY}; padding: 5px 8px; }}
    """


def apply_theme(app, character: str) -> None:
    app.setStyleSheet(app_stylesheet(accent_for(character)))


# --------------------------------------------------------------------------
# 小部件
# --------------------------------------------------------------------------
class Divider(QWidget):
    """``listDivider``：两头渐隐的细线。没有游戏图元时画一条同样渐隐的线。"""

    def __init__(self, parent=None, opacity: float = 0.5):
        super().__init__(parent)
        self.setFixedHeight(2)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.opacity = opacity
        path = ui_sprite("listDivider")
        self._pixmap = QPixmap(str(path)) if path else None

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setOpacity(self.opacity)
        if self._pixmap is not None and not self._pixmap.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(self.rect(), self._pixmap)
        else:
            from PySide6.QtGui import QLinearGradient

            grad = QLinearGradient(0, 0, self.width(), 0)
            grad.setColorAt(0.0, QColor(255, 255, 255, 0))
            grad.setColorAt(0.5, QColor(255, 255, 255, 255))
            grad.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillRect(self.rect(), grad)
        painter.end()


def section(text: str) -> QWidget:
    """分节标题：灰字 + 一条渐隐线（游戏菜单列表的分节就是这样）。上下留白固定。"""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 4, 0, 0)
    layout.setSpacing(6)
    label = QLabel(text)
    label.setObjectName("section")
    layout.addWidget(label)
    layout.addWidget(Divider(opacity=0.35))
    return box


def sprite_icon(name: str) -> QIcon | None:
    path = ui_sprite(name)
    return QIcon(str(path)) if path else None


def label(text: str, name: str, wrap: bool = True, selectable: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def button_row(*buttons: QPushButton, left: tuple[QPushButton, ...] = ()) -> QHBoxLayout:
    """底部按钮行：左边放次要的，右边放主要的，间距统一。"""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(GAP_S)
    for button in left:
        row.addWidget(button)
    row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    return row


class ChoiceCard(QPushButton):
    """可选中的卡片：粗体标题 + 一行灰色说明。用在确认卡的"权限"上。"""

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setObjectName("choice")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 宽度只听布局的 stretch：并排的两张卡必须等宽，不能按各自文字长短分
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 12)
        layout.setSpacing(3)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self.title = label(title, "choiceTitle", wrap=False)
        self.tick = QLabel()
        tick = ui_sprite("Menu_Symbol_CheckBox")          # 游戏菜单的勾
        if tick is not None:
            self.tick.setPixmap(QPixmap(str(tick)))
        else:
            self.tick.setText("✓")
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.tick)
        self.detail = label(detail, "choiceDetail")
        layout.addLayout(head)
        layout.addWidget(self.detail)
        for child in (self.title, self.detail, self.tick):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.toggled.connect(self._restyle)
        self._restyle(False)

    def _restyle(self, checked: bool) -> None:
        self.title.setStyleSheet(f"color: {WHITE if checked else MEDIUM_GREY};")
        self.tick.setVisible(checked)

    def sizeHint(self):  # noqa: N802
        return self.layout().sizeHint()

    def minimumSizeHint(self):  # noqa: N802
        return self.layout().minimumSize()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self.layout().totalHeightForWidth(width)


class HaloGlyph(QWidget):
    """标题栏的小光环：两圈 + 一圈刻度，跟桌宠头顶那个同形，用角色色。"""

    def __init__(self, accent: str, size: int = 16, parent=None):
        super().__init__(parent)
        self.accent = QColor(accent)
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QPointF(self.width() / 2, self.height() / 2)
        r = self.width() / 2 - 1.0
        painter.setPen(QPen(self.accent, 1.1))
        painter.drawEllipse(c, r * 0.60, r * 0.60)
        painter.drawEllipse(c, r * 0.34, r * 0.34)
        painter.setPen(QPen(self.accent, 1.3))
        for i in range(12):
            a = i * pi / 6
            painter.drawLine(QPointF(c.x() + cos(a) * r * 0.80, c.y() + sin(a) * r * 0.80),
                             QPointF(c.x() + cos(a) * r, c.y() + sin(a) * r))
        painter.end()


class _TitleBar(QWidget):
    """光环 · 标题 · 关闭；下面一条渐隐线。左右都对齐到 PAD。"""

    def __init__(self, window: QWidget, title: str, accent: str):
        super().__init__(window)
        self.window_ = window
        self._drag: QPoint | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(PAD, 13, PAD - 6, 0)    # 关闭键自带 6 px 的点击余量
        outer.setSpacing(11)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(HaloGlyph(accent))
        self.label = QLabel(title)
        self.label.setObjectName("title")
        row.addWidget(self.label)
        row.addStretch(1)
        close = QPushButton("×")
        close.setObjectName("titleButton")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(window.close)
        row.addWidget(close)
        outer.addLayout(row)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 6, 0)               # 抵掉上面的 -6，线和正文同宽
        line.addWidget(Divider(opacity=0.55))
        outer.addLayout(line)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.window_.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window_.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None


def _frame(widget: QWidget, title: str, accent: str) -> QVBoxLayout:
    """装上投影留白、外框和标题栏，返回内容区的布局。"""
    widget.setWindowTitle(title)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    outer = QVBoxLayout(widget)
    edge = SHADOW + int(FRAME_BORDER)
    outer.setContentsMargins(edge, edge, edge, edge)
    outer.setSpacing(0)
    widget.title_bar = _TitleBar(widget, title, accent)
    outer.addWidget(widget.title_bar)
    content = QWidget()
    body = QVBoxLayout(content)
    body.setContentsMargins(PAD, 16, PAD, PAD)
    body.setSpacing(GAP)
    outer.addWidget(content, 1)
    return body


def _paint_frame(widget: QWidget) -> None:
    """柔和投影 + 近黑半透明填充 + 2 px MediumGrey 圆角外框（RoundedRect）。"""
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    inner = QRectF(widget.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)
    # 投影：一圈圈往外放大、越外越淡，略往下偏——不用 QGraphicsDropShadowEffect（会让整窗离屏重绘）
    painter.setPen(Qt.PenStyle.NoPen)
    for i in range(SHADOW, 0, -1):
        t = 1.0 - i / SHADOW
        painter.setBrush(QColor(0, 0, 0, int(34 * t * t)))
        painter.drawRoundedRect(inner.adjusted(-i, -i + 3, i, i + 3), RADIUS + i, RADIUS + i)
    half = FRAME_BORDER / 2
    rect = inner.adjusted(half, half, -half, -half)
    path = QPainterPath()
    path.addRoundedRect(rect, RADIUS, RADIUS)
    base = QColor(BASE)
    base.setAlphaF(FILL_ALPHA)
    painter.fillPath(path, base)
    border = QColor(MEDIUM_GREY)
    border.setAlphaF(0.75)
    painter.setPen(QPen(border, FRAME_BORDER))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.end()


class FramedWindow(QWidget):
    """无边框 + 自绘标题栏的置顶窗口。内容放进 ``self.body``（一个 QVBoxLayout）。"""

    def __init__(self, title: str, accent: str, parent=None, tool: bool = False):
        super().__init__(parent)
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        flags |= Qt.WindowType.Tool if tool else Qt.WindowType.Window
        self.setWindowFlags(flags)
        self.body = _frame(self, title, accent)

    def paintEvent(self, event) -> None:  # noqa: N802
        _paint_frame(self)

    def set_title(self, title: str) -> None:
        self.title_bar.label.setText(title)
        self.setWindowTitle(title)


class FramedDialog(QDialog):
    """模态版（确认卡、输入框、确认框）。"""

    def __init__(self, title: str, accent: str, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.body = _frame(self, title, accent)

    def paintEvent(self, event) -> None:  # noqa: N802
        _paint_frame(self)


class TextPrompt(FramedDialog):
    """代替 QInputDialog：一行说明 + 输入框 + 确定/取消，同一套外观。"""

    def __init__(self, title: str, label_text: str, accent: str, text: str = "",
                 password: bool = False, placeholder: str = "", parent=None):
        super().__init__(title, accent, parent)
        self.setMinimumWidth(460)
        caption = label(label_text, "muted")
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        if password:
            self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.returnPressed.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("确定")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        self.body.addWidget(caption)
        self.body.addWidget(self.edit)
        self.body.addSpacing(GAP_S)
        self.body.addLayout(button_row(cancel, ok))
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 对话框一显示 Qt 会把已有内容全选（整格金色高亮，看着像出错）：光标放到末尾
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda: (self.edit.setFocus(), self.edit.deselect(),
                                      self.edit.end(False)))

    def value(self) -> str | None:
        return self.edit.text() if self.exec() == QDialog.DialogCode.Accepted else None


class ConfirmDialog(FramedDialog):
    """代替 QMessageBox.question：同一套外观，危险操作的按钮悬停变红。"""

    def __init__(self, title: str, message: str, accent: str, confirm: str = "确定",
                 danger: bool = False, parent=None):
        super().__init__(title, accent, parent)
        self.setMinimumWidth(400)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton(confirm)
        ok.setObjectName("danger" if danger else "primary")
        ok.clicked.connect(self.accept)
        self.body.addWidget(label(message, "value"))
        self.body.addSpacing(GAP_S)
        self.body.addLayout(button_row(cancel, ok))
        cancel.setFocus()

    def ask(self) -> bool:
        return self.exec() == QDialog.DialogCode.Accepted


class CopyDialog(FramedDialog):
    """一大段可以复制的文字（接入编程 agent 的配置之类）：等宽、只读、「复制全部」。"""

    def __init__(self, title: str, text: str, accent: str, parent=None):
        from PySide6.QtWidgets import QApplication, QPlainTextEdit

        super().__init__(title, accent, parent)
        self.setMinimumWidth(720)
        self.text = QPlainTextEdit(text)
        self.text.setReadOnly(True)
        self.text.setStyleSheet(f"font-family: {MONO_FONT};")
        self.text.setMinimumHeight(420)
        copy = QPushButton("复制全部")
        copy.clicked.connect(lambda: (QApplication.clipboard().setText(self.text.toPlainText()),
                                      copy.setText("已复制")))
        close = QPushButton("关闭")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        self.body.addWidget(self.text, 1)
        self.body.addLayout(button_row(close, left=(copy,)))


def _cjk(ch: str) -> bool:
    return "㐀" <= ch <= "鿿"


def spaced(text: str) -> str:
    """标题字间距：Qt 的 letter-spacing 对中文不生效，在**相邻两个汉字之间**插窄空格
    （"AI 设置"不能变成"A I 设置"）。"""
    out = []
    for i, ch in enumerate(text):
        if i and _cjk(ch) and _cjk(text[i - 1]):
            out.append(" ")
        out.append(ch)
    return "".join(out)
