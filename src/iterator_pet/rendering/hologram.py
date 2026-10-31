"""监视者的全息投影面板：离屏绘制 + 错动 / 扫描线 / 闪缺合成（design §2.3、§3.3）。

风格是用户认可的效果图（``tools/overseer_mockup.py`` 的 ``hologram`` / ``holo_effect`` / ``place_panel`` / ``beam``）直接搬过来的：
单色、字发光、很淡的底色 + 四角括线（原作全息没有框，加它是为了压在花桌面上也读得清）。

字体渲染贵，所以**内容变了才重画面板**（``render_panel``）；每帧只做贴条合成（``Hologram.frame``）。
强错动只在出现 / 消失的头 ``STRONG_TICKS`` 个 tick（0.3 s），平时只留淡扫描线，保证中文好读。

第二期：按钮圆有名字（``Button.name``，点击分发用）、字形（``glyph``，自己画，不依赖游戏素材）和左右分组；
长文用 ``ScrollPanel``（固定高度的取景窗，文字按偏移慢慢上滚）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen

WARN_COLOR = (1.0, 0.35, 0.25)          # 危险提示用红
STRONG_TICKS = 12                       # 出现 / 消失时强效果持续多久（40 TPS 下 0.3 s）
BACKING_ALPHA = 0.62                    # 面板暗底的不透明度（越大背后的字越看不见）
SCAN_STEP = 3                          # 隔几个像素压一条暗扫描线
PAD_X, PAD_Y = 16, 14
BUTTON_SIZE, BUTTON_GAP = 40, 12
BUTTON_ROW = BUTTON_SIZE + 14 + 8       # 按钮行占的高度（圆 + 下面的小字 + 间距）
UI_FAMILIES = ["Segoe UI", "Microsoft YaHei UI", "Noto Sans SC"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Microsoft YaHei UI"]


@dataclass(frozen=True)
class Line:
    text: str
    kind: str = "body"                  # title | body | mono
    alpha: float = 1.0
    warn: bool = False


@dataclass(frozen=True)
class Button:
    name: str                           # 点击分发用
    label: str
    glyph: str = "cross"                # check | cross | folder | read | write | shield | ""（没有）
    side: str = "right"                 # left | right：两组分别从两头排
    selected: bool = False              # 单选组里被选中的（常亮）
    hot: bool = False                   # 光标正压在上面
    symbol: QImage | None = None        # 想用游戏图标时给；优先于 glyph


def qcolor(rgb, a: float = 1.0, mul: float = 1.0) -> QColor:
    c = QColor.fromRgbF(min(1.0, rgb[0] * mul), min(1.0, rgb[1] * mul), min(1.0, rgb[2] * mul))
    c.setAlphaF(a)
    return c


def make_font(kind: str) -> QFont:
    font = QFont()
    if kind == "mono":
        font.setFamilies(MONO_FAMILIES)
        font.setPointSize(10)
    else:
        font.setFamilies(UI_FAMILIES)
        font.setPointSize(12 if kind == "title" else 11)
        if kind == "title":
            font.setWeight(QFont.Weight.DemiBold)
    return font


def wrap_text(text: str, font: QFont, width: float) -> list[str]:
    """按像素宽度逐字折行（中文没有空格，按字折最省事）；保留原有换行。"""
    fm = QFontMetrics(font)
    out: list[str] = []
    for para in text.split("\n"):
        current = ""
        for ch in para:
            if current and fm.horizontalAdvance(current + ch) > width:
                out.append(current)
                current = ch
            else:
                current += ch
        out.append(current)
    return out


def button_rects(width: int, height: int, buttons) -> list[QRectF]:
    """每个按钮圆在面板里的位置（面板像素），顺序和 ``buttons`` 一致。
    ``side="right"`` 的从右边往左排，``"left"`` 的从左边往右排。"""
    y = height - PAD_Y - 14 - BUTTON_SIZE
    step = BUTTON_SIZE + BUTTON_GAP
    rights = [b for b in buttons if b.side != "left"]
    rects: dict[int, QRectF] = {}
    left_i = right_i = 0
    for i, b in enumerate(buttons):
        if b.side == "left":
            rects[i] = QRectF(PAD_X + left_i * step, y, BUTTON_SIZE, BUTTON_SIZE)
            left_i += 1
        else:
            x = width - PAD_X - BUTTON_SIZE - (len(rights) - 1 - right_i) * step
            rects[i] = QRectF(x, y, BUTTON_SIZE, BUTTON_SIZE)
            right_i += 1
    return [rects[i] for i in range(len(buttons))]


def _layout_rows(lines: list[Line], inner: float) -> list[tuple]:
    rows = []                           # (文字, 字体, 行高, 线)
    for line in lines:
        font = make_font(line.kind)
        height = QFontMetrics(font).height() + 4
        for piece in wrap_text(line.text, font, inner):
            rows.append((piece, font, height, line))
    return rows


def _paint_rows(p: QPainter, rows: list[tuple], x: float, y: float, inner: float, color) -> float:
    align = Qt.AlignmentFlag.AlignLeft
    for text, font, h, line in rows:
        c = WARN_COLOR if line.warn else color
        p.setFont(font)
        p.setPen(qcolor(c, 0.25 * line.alpha))              # 发光：错开一像素再画一遍
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            p.drawText(QRectF(x + dx, y + dy, inner, h), align, text)
        p.setPen(qcolor(c, line.alpha, 1.15))
        p.drawText(QRectF(x, y, inner, h), align, text)
        y += h
    return y


def draw_backdrop(p: QPainter, width: float, height: float, color, x: float = 0.0, y: float = 0.0) -> None:
    """面板的底：暗底 + 很淡的色底 + 四角括线（原作全息没有框，这里只给角标，保证中文压在花桌面上也读得清）。
    输入投影（QuickBar）的皮肤也用它，两边观感一致。"""
    # 先铺一层半透明的暗底：桌面上的文字 / 花图案透过很淡的色底会和投影字混在一起（用户实测）。
    # Qt 的分层窗口没法"模糊背后的桌面"，压暗是能做到的最接近的办法
    p.fillRect(QRectF(x, y, width, height), QColor(8, 9, 14, int(255 * BACKING_ALPHA)))
    p.fillRect(QRectF(x, y, width, height), qcolor(color, 0.07))
    p.setPen(QPen(qcolor(color, 0.8), 1.4))
    for (cx, cy, dx, dy) in ((x, y, 1, 1), (x + width, y, -1, 1), (x, y + height, 1, -1),
                             (x + width, y + height, -1, -1)):
        p.drawLine(QPointF(cx, cy), QPointF(cx + dx * 14, cy))
        p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * 14))


def _begin(width: int, height: int, color) -> tuple[QImage, QPainter]:
    img = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    draw_backdrop(p, width, height, color)
    return img, p


def render_panel(width: int, lines: list[Line], color, buttons: list[Button] | tuple = ()) -> QImage:
    """全息面板（还没加错动）：单色、字发光。"""
    inner = width - 2 * PAD_X
    rows = _layout_rows(lines, inner)
    height = sum(r[2] for r in rows) + 2 * PAD_Y + (BUTTON_ROW if buttons else 0)
    img, p = _begin(width, height, color)
    _paint_rows(p, rows, PAD_X, PAD_Y, inner, color)
    for rect, button in zip(button_rects(width, height, buttons), buttons):
        _draw_button(p, rect, button, color)
    p.end()
    return img


def _draw_glyph(p: QPainter, glyph: str, c: QPointF, color, alpha: float) -> None:
    p.setPen(QPen(qcolor(color, alpha, 1.15), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    x, y = c.x(), c.y()
    if glyph == "check":
        p.drawPolyline([QPointF(x - 8, y), QPointF(x - 2, y + 7), QPointF(x + 8, y - 7)])
    elif glyph == "cross":
        p.drawLine(QPointF(x - 7, y - 7), QPointF(x + 7, y + 7))
        p.drawLine(QPointF(x + 7, y - 7), QPointF(x - 7, y + 7))
    elif glyph == "folder":
        p.drawPolyline([QPointF(x - 9, y + 6), QPointF(x - 9, y - 6), QPointF(x - 3, y - 6), QPointF(x - 1, y - 3),
                        QPointF(x + 9, y - 3), QPointF(x + 9, y + 6), QPointF(x - 9, y + 6)])
    elif glyph == "read":               # 眼睛：只能看
        p.drawEllipse(QPointF(x, y), 4.0, 4.0)
        p.drawArc(QRectF(x - 10, y - 7, 20, 14), 0, 180 * 16)
        p.drawArc(QRectF(x - 10, y - 7, 20, 14), 180 * 16, 180 * 16)
    elif glyph == "write":              # 铅笔：能改
        p.drawLine(QPointF(x - 8, y + 8), QPointF(x + 6, y - 6))
        p.drawLine(QPointF(x - 8, y + 8), QPointF(x - 8, y + 3))
        p.drawLine(QPointF(x - 8, y + 8), QPointF(x - 3, y + 8))
        p.drawLine(QPointF(x + 4, y - 8), QPointF(x + 8, y - 4))
    elif glyph == "globe":              # 联网
        p.drawEllipse(QPointF(x, y), 9.0, 9.0)
        p.drawEllipse(QPointF(x, y), 4.0, 9.0)
        p.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
    elif glyph == "shield":             # 沙箱
        p.drawPolyline([QPointF(x - 7, y - 7), QPointF(x + 7, y - 7), QPointF(x + 7, y + 1), QPointF(x, y + 9),
                        QPointF(x - 7, y + 1), QPointF(x - 7, y - 7)])


def _draw_button(p: QPainter, rect: QRectF, button: Button, color) -> None:
    lit = button.hot or button.selected
    p.setPen(QPen(qcolor(color, 0.9 if lit else 0.45), 1.4))
    p.setBrush(qcolor(color, 0.3 if button.hot else 0.22 if button.selected else 0.06))
    p.drawEllipse(rect)
    c = rect.center()
    if button.symbol is not None:
        tinted = button.symbol.convertToFormat(QImage.Format.Format_ARGB32)
        pp = QPainter(tinted)
        pp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        pp.fillRect(tinted.rect(), qcolor(color, 1.0, 1.15))
        pp.end()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        s = 22
        p.drawImage(QRectF(c.x() - s / 2, c.y() - s / 2, s, s), tinted)
    else:
        _draw_glyph(p, button.glyph, c, color, 1.0 if lit else 0.8)
    p.setPen(qcolor(color, 0.85 if lit else 0.7))
    label_font = make_font("body")
    label_font.setPointSize(8)
    p.setFont(label_font)
    p.drawText(QRectF(rect.x() - 14, rect.bottom() + 1, BUTTON_SIZE + 28, 14), Qt.AlignmentFlag.AlignHCenter,
               button.label)


def holo_effect(img: QImage, rng: random.Random, jitter_rows=(), scan: int = SCAN_STEP,
                drop: float = 0.035) -> QImage:
    """Hologram.shader 的观感：几段行左右错开、零星缺行、隔 ``scan`` 像素压一条暗线。"""
    out = QImage(img.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    y = 0
    while y < img.height():
        h = rng.randint(2, 9)
        dx = 0
        if any(a <= y < b for a, b in jitter_rows):
            dx = rng.choice([-6, -4, -3, 3, 4, 7])
        if rng.random() < drop:
            y += h
            continue                                       # 闪缺
        p.drawImage(QPointF(dx, y), img, QRectF(0, y, img.width(), h))
        y += h
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    for sy in range(0, out.height(), scan):
        p.fillRect(QRectF(0, sy, out.width(), 1), QColor(0, 0, 0, 70))
    p.end()
    return out


def scanlines(img: QImage, scan: int = SCAN_STEP) -> QImage:
    """只压扫描线（滚动文字每 tick 都要重合成，不能每次都做整套错动）。"""
    out = QImage(img)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    for sy in range(0, out.height(), scan):
        p.fillRect(QRectF(0, sy, out.width(), 1), QColor(0, 0, 0, 70))
    p.end()
    return out


class Hologram:
    """一块面板的动画：出现 / 消失的头 0.3 s 强错动 + 淡入淡出，其余时间是缓存好的淡扫描线版本。"""

    scrolling = False

    def __init__(self, image: QImage, seed: int = 0):
        self.image = image
        self.seed = seed
        self._calm: QImage | None = None
        self._strong: dict[int, QImage] = {}

    def _jitter_rows(self, rng: random.Random) -> list[tuple[int, int]]:
        """每次随机挑 1–3 段整体错开的行带，高度按面板比例。"""
        h = self.image.height()
        rows = []
        for _ in range(rng.randint(1, 3)):
            top = rng.randint(0, max(0, h - 12))
            rows.append((top, top + rng.randint(6, max(7, h // 5))))
        return rows

    def _strong_frame(self, step: int) -> QImage:
        frame = self._strong.get(step)
        if frame is None:
            rng = random.Random(self.seed * 1000 + step)
            frame = holo_effect(self.image, rng, self._jitter_rows(rng), drop=0.12)
            self._strong[step] = frame
        return frame

    def frame(self, age: int, leaving: int | None = None, offset: float = 0.0) -> tuple[QImage, float]:
        """返回 (这一帧的图, 不透明度)。``age`` 出现后过了几个 tick；``leaving`` 开始消失后过了几个 tick（没消失 = None）。
        ``offset`` 只给滚动面板用，普通面板忽略。"""
        if leaving is not None:
            if leaving >= STRONG_TICKS:
                return self._strong_frame(0), 0.0
            return self._strong_frame(leaving), 1.0 - leaving / STRONG_TICKS
        if age < STRONG_TICKS:
            return self._strong_frame(age), min(1.0, 0.25 + age / STRONG_TICKS)
        if self._calm is None:
            self._calm = holo_effect(self.image, random.Random(self.seed), (), drop=0.0)
        return self._calm, 1.0


class ScrollPanel(Hologram):
    """长文：固定高度的取景窗，正文按像素偏移上滚（偏移由调用方按时间算）。

    布局：标题行 / 取景窗（``view_lines`` 行高）/ 一行小字提示。取景窗上下各留一点渐隐，字不会硬切。
    出现 / 消失的强错动用偏移 0 的那一帧；平时每帧只合成一次 + 压扫描线。"""

    scrolling = True
    FADE = 14

    def __init__(self, width: int, title: Line, body: str, hint: str, color, view_lines: int = 8, seed: int = 0):
        self.color = color
        inner = width - 2 * PAD_X
        self._head = _layout_rows([title], inner)
        self._foot = _layout_rows([Line(hint, "body", 0.55)], inner)
        body_rows = _layout_rows([Line(body, "body")], inner)
        line_h = body_rows[0][2] if body_rows else 20
        self.view_h = line_h * view_lines
        self.chars = max(1, len(body))
        self.body_h = sum(r[2] for r in body_rows)
        self._body = QImage(width, max(1, self.body_h), QImage.Format.Format_ARGB32_Premultiplied)
        self._body.fill(Qt.GlobalColor.transparent)
        bp = QPainter(self._body)
        bp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        _paint_rows(bp, body_rows, PAD_X, 0, inner, color)
        bp.end()
        self._head_h = sum(r[2] for r in self._head) + PAD_Y
        self._height = self._head_h + self.view_h + sum(r[2] for r in self._foot) + PAD_Y
        self._width = width
        super().__init__(self._compose(0.0, scan=False), seed)

    @property
    def max_offset(self) -> float:
        return max(0.0, float(self.body_h - self.view_h))

    @property
    def px_per_char(self) -> float:
        return self.body_h / self.chars

    def _compose(self, offset: float, scan: bool = True) -> QImage:
        img, p = _begin(self._width, self._height, self.color)
        inner = self._width - 2 * PAD_X
        _paint_rows(p, self._head, PAD_X, PAD_Y, inner, self.color)
        _paint_rows(p, self._foot, PAD_X, self._head_h + self.view_h, inner, self.color)
        offset = max(0.0, min(offset, self.max_offset))
        view = QImage(self._width, self.view_h, QImage.Format.Format_ARGB32_Premultiplied)
        view.fill(Qt.GlobalColor.transparent)
        vp = QPainter(view)
        vp.drawImage(QPointF(0, -offset), self._body)
        if self.body_h > self.view_h:                       # 上下渐隐
            vp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            g =QLinearGradient(0, 0, 0, self.view_h)
            f = self.FADE / self.view_h
            g.setColorAt(0.0, QColor(0, 0, 0, 0 if offset > 0 else 255))
            g.setColorAt(f, QColor(0, 0, 0, 255))
            g.setColorAt(1.0 - f, QColor(0, 0, 0, 255))
            g.setColorAt(1.0, QColor(0, 0, 0, 0 if offset < self.max_offset else 255))
            vp.fillRect(view.rect(), g)
        vp.end()
        p.drawImage(QPointF(0, self._head_h), view)
        p.end()
        return scanlines(img) if scan else img

    def view_rect(self) -> QRectF:
        """取景窗在面板里的位置（点击展开用整块面板，这里留给测试 / 悬停判断）。"""
        return QRectF(0, self._head_h, self._width, self.view_h)

    def frame(self, age: int, leaving: int | None = None, offset: float = 0.0) -> tuple[QImage, float]:
        if leaving is not None or age < STRONG_TICKS:
            return super().frame(age, leaving)
        return self._compose(offset), 1.0


def place_panel(w: float, h: float, body, head, inward, screen) -> QRectF:
    """投影放在监视者往屏幕里那一侧，挑离人偶最远、又不出屏幕的位置。screen = (宽, 高)。"""
    sw, sh = screen
    candidates = []
    for side in (-1, 1):
        if inward[1] != 0:              # 上 / 下边：投影在它上方或下方，左右错开
            x = body[0] + side * 30 - (w if side < 0 else 0)
            y = body[1] + inward[1] * 30 - (h if inward[1] < 0 else 0)
        else:                           # 左 / 右边：投影在它旁边，上下错开
            x = body[0] + inward[0] * 30 - (w if inward[0] < 0 else 0)
            y = body[1] + side * 20 - (h if side < 0 else 0)
        x = min(max(x, 16), max(16, sw - w - 16))
        y = min(max(y, 16), max(16, sh - h - 40))
        candidates.append(QRectF(x, y, w, h))
    return max(candidates, key=lambda r: math.dist((r.center().x(), r.center().y()), head))


def beam(p: QPainter, eye, rect: QRectF, color) -> None:
    """监视者的眼到投影两角的光束（原作投影时眼睛和全息之间有一道淡光）。"""
    path = QPainterPath(QPointF(*eye))
    if rect.left() > eye[0]:
        path.lineTo(rect.topLeft())
        path.lineTo(rect.bottomLeft())
    else:
        path.lineTo(rect.topRight())
        path.lineTo(rect.bottomRight())
    path.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(qcolor(color, 0.06))
    p.drawPath(path)
