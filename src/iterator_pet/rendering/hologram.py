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
GLITCH_PERIOD = 40 * 3                  # 平时每隔多久闪一次故障（tick）
GLITCH_TICKS = 3                        # 一次故障闪几帧
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
    icon: str = ""                      # 标题前的状态符号：eye | spin | triangle | check | cross | globe（见 STATUS_ICONS）


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
        font.setPixelSize(13)
    else:
        font.setFamilies(UI_FAMILIES)
        font.setPixelSize(15 if kind == "title" else 14)          # 用户 2026-11-01：正文 14 px，标题再大一点
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
    seen: set[int] = set()
    for text, font, h, line in rows:
        c = WARN_COLOR if line.warn else color
        tx = x
        if line.icon and id(line) not in seen:              # 符号只画在这一行折出来的第一段前面
            seen.add(id(line))
            draw_status_icon(p, line.icon, QPointF(x + ICON_R, y + h / 2), c, line.alpha)
            tx = x + ICON_W
        elif line.icon:
            tx = x + ICON_W
        p.setFont(font)
        p.setPen(qcolor(c, 0.25 * line.alpha))              # 发光：错开一像素再画一遍
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            p.drawText(QRectF(tx + dx, y + dy, inner, h), align, text)
        p.setPen(qcolor(c, line.alpha, 1.15))
        p.drawText(QRectF(tx, y, inner, h), align, text)
        y += h
    return y


ICON_R = 6.0                            # 状态符号的半径
ICON_W = 2 * ICON_R + 7                 # 符号占的宽（含与字的间距）


def draw_status_icon(p: QPainter, icon: str, c: QPointF, color, alpha: float = 1.0) -> None:
    """标题前的小几何符号（照游戏里监视者 / 迭代器 UI 那种单色线符号，自己画，不用游戏素材）。"""
    p.save()
    p.setPen(QPen(qcolor(color, alpha, 1.15), 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    x, y, r = c.x(), c.y(), ICON_R
    if icon == "eye":                   # 在看 / 盯着：圈 + 实心瞳
        p.drawEllipse(c, r, r)
        p.setBrush(qcolor(color, alpha, 1.15))
        p.drawEllipse(c, 1.8, 1.8)
    elif icon == "spin":                # 演算中：断开的圈
        p.drawArc(QRectF(x - r, y - r, 2 * r, 2 * r), 30 * 16, 120 * 16)
        p.drawArc(QRectF(x - r, y - r, 2 * r, 2 * r), 210 * 16, 120 * 16)
    elif icon == "triangle":            # 要你批准 / 确认
        p.drawPolygon([QPointF(x, y - r), QPointF(x + r, y + r * 0.8), QPointF(x - r, y + r * 0.8)])
    elif icon == "check":
        p.drawPolyline([QPointF(x - r, y), QPointF(x - r * 0.25, y + r * 0.7), QPointF(x + r, y - r * 0.8)])
    elif icon == "cross":
        p.drawLine(QPointF(x - r * 0.8, y - r * 0.8), QPointF(x + r * 0.8, y + r * 0.8))
        p.drawLine(QPointF(x + r * 0.8, y - r * 0.8), QPointF(x - r * 0.8, y + r * 0.8))
    elif icon == "globe":
        p.drawEllipse(c, r, r)
        p.drawLine(QPointF(x - r, y), QPointF(x + r, y))
        p.drawLine(QPointF(x, y - r), QPointF(x, y + r))
    p.restore()


def octagon(x: float, y: float, w: float, h: float, cut: float) -> QPainterPath:
    """切角矩形（游戏里迭代器 / 监视者投影框那种八边形）。"""
    cut = min(cut, w / 4, h / 4)
    path = QPainterPath()
    path.moveTo(x + cut, y)
    for px, py in ((x + w - cut, y), (x + w, y + cut), (x + w, y + h - cut), (x + w - cut, y + h),
                   (x + cut, y + h), (x, y + h - cut), (x, y + cut)):
        path.lineTo(px, py)
    path.closeSubpath()
    return path


def panel_shape(width: float, height: float, x: float = 0.0, y: float = 0.0) -> QPainterPath:
    m = EDGE_MARGIN
    return octagon(x + m, y + m, width - 2 * m, height - 2 * m, CUT)


def draw_backdrop(p: QPainter, width: float, height: float, color, x: float = 0.0, y: float = 0.0,
                  edge: bool = True) -> None:
    """面板的底：八边形暗底 + 很淡的色底。``edge`` = 顺手画一条静止的细边（输入条皮肤用）；
    监视者面板的边是每帧画的（``draw_edge``：细线轻微位移、闪烁，照原版）。"""
    shape = panel_shape(width, height, x, y)
    # 先铺一层半透明的暗底：桌面上的文字 / 花图案透过很淡的色底会和投影字混在一起（用户实测）。
    # Qt 的分层窗口没法"模糊背后的桌面"，压暗是能做到的最接近的办法
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(8, 9, 14, int(255 * BACKING_ALPHA)))
    p.drawPath(shape)
    p.setBrush(qcolor(color, 0.07))
    p.drawPath(shape)
    p.restore()
    if edge:
        draw_edge(p, width, height, color, random.Random(0), x, y)


def draw_edge(p: QPainter, width: float, height: float, color, rng: random.Random,
              x: float = 0.0, y: float = 0.0) -> None:
    """原版投影框的边（用户 2026-11-01 给的游戏截图）：一条清楚的细线 + 一条错开几像素的淡残影，
    两条都随帧轻微位移；偶尔一段缺掉。不糊。"""
    p.save()
    p.setBrush(Qt.BrushStyle.NoBrush)
    main = panel_shape(width, height, x + rng.uniform(-0.8, 0.8), y + rng.uniform(-0.8, 0.8))
    pen = QPen(qcolor(color, rng.uniform(0.75, 0.95), 1.15), 1.2)
    # 断点密一些、四条边都有（用户 2026-11-01）：长短不一的几段循环，起点随机，每次位移的断口都不一样
    dashes = []
    for _ in range(6):
        dashes += [rng.uniform(18, 70) / 1.2, rng.uniform(2, 9) / 1.2]
    pen.setDashPattern(dashes)
    pen.setDashOffset(rng.uniform(0, sum(dashes)))
    p.setPen(pen)
    p.drawPath(main)
    ghost = panel_shape(width, height, x + rng.choice((-1, 1)) * rng.uniform(1.5, 3.0),
                        y + rng.choice((-1, 1)) * rng.uniform(1.0, 2.5))
    p.setPen(QPen(qcolor(color, rng.uniform(0.3, 0.5)), 1.0))
    p.drawPath(ghost)
    p.restore()


EDGE_MARGIN = 5.0                       # 面板四周留给边线位移的余量
CUT = 12.0                              # 八边形切角
EDGE_VARIANTS, EDGE_HOLD = 12, 1        # 边线预先画几种位移；每种停几个 tick 再换（每 tick 都换 = 抖得勤）


def _begin(width: int, height: int, color) -> tuple[QImage, QPainter]:
    img = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    draw_backdrop(p, width, height, color, edge=False)      # 边由 Hologram 每帧加（会动）
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

    def __init__(self, image: QImage, seed: int = 0, color=None):
        self.image = image
        self.seed = seed
        self.color = color                       # 给了才画会动的边（draw_edge）
        self._edged: dict[tuple, QImage] = {}
        self._calm: QImage | None = None
        self._strong: dict[int, QImage] = {}
        self._glitch: dict[int, QImage] = {}

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
        image, opacity = self._frame(age, leaving, offset)
        return self.with_edge(image, age if leaving is None else leaving), opacity

    def with_edge(self, image: QImage, tick: int, cache: bool = True) -> QImage:
        """在这一帧上加会动的细边；同一张底图的几种位移缓存着（滚动面板每帧新图，不缓存）。"""
        if self.color is None:
            return image
        variant = (tick // EDGE_HOLD + self.seed) % EDGE_VARIANTS
        key = (image.cacheKey(), variant)
        if cache and key in self._edged:
            return self._edged[key]
        out = QImage(image)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_edge(p, out.width(), out.height(), self.color, random.Random(self.seed * 131 + variant))
        p.end()
        if cache:
            if len(self._edged) > 64:
                self._edged.clear()
            self._edged[key] = out
        return out

    def _frame(self, age: int, leaving: int | None = None, offset: float = 0.0) -> tuple[QImage, float]:
        if leaving is not None:
            if leaving >= STRONG_TICKS:
                return self._strong_frame(0), 0.0
            return self._strong_frame(leaving), 1.0 - leaving / STRONG_TICKS
        if age < STRONG_TICKS:
            return self._strong_frame(age), min(1.0, 0.25 + age / STRONG_TICKS)
        if self._calm is None:
            self._calm = holo_effect(self.image, random.Random(self.seed), (), drop=0.0)
        return self._idle(age)

    def _idle(self, age: int) -> tuple[QImage, float]:
        """站稳以后的故障感（用户 2026-11-01）：大部分时间微微呼吸，隔几秒闪暗一下、错开一段行。
        闪的时机按面板 seed 错开，两只监视者不会同时闪；只闪 GLITCH_TICKS 帧，不影响读字。"""
        breath = 0.9 + 0.06 * math.sin(age * 0.11 + self.seed)
        phase = (age + self.seed * 37) % GLITCH_PERIOD
        if phase < GLITCH_TICKS:
            frame = self._glitch.get(phase)
            if frame is None:
                rng = random.Random(self.seed * 7919 + phase)
                frame = holo_effect(self.image, rng, self._jitter_rows(rng)[:1], drop=0.04)
                self._glitch[phase] = frame
            return frame, (0.45, 0.8, 0.6)[phase % 3]
        return self._calm, breath


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
        super().__init__(self._compose(0.0, scan=False), seed, color)

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
        return (self.with_edge(self._compose(offset), age, cache=False),
                0.9 + 0.06 * math.sin(age * 0.11 + self.seed))


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
