"""监视者的全息投影面板：离屏绘制 + 错动 / 扫描线 / 闪缺合成（design §2.3、§3.3）。

面板的版式是用户认可的效果图（``tools/overseer_mockup.py`` 的 ``hologram`` / ``place_panel`` / ``beam``）：
单色、字发光、很淡的底色 + 边框线。

字体渲染贵，所以**内容变了才重画面板**（``render_panel``）；每帧只做溶解遮罩（``Hologram.frame``）。
"闪烁"照原版着色器（``HologramImage.shader``）：噪声遮罩连续滚动，边上破洞最密、中间几乎不丢，
淡入淡出 = 溶出来 / 溶没了。不再有行错位和定时故障（用户 2026-11-01：那是我凭印象加的，节奏不对）。

第二期：按钮圆有名字（``Button.name``，点击分发用）、字形（``glyph``，自己画，不依赖游戏素材）和左右分组；
长文用 ``ScrollPanel``（固定高度的取景窗，文字按偏移慢慢上滚）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen

WARN_COLOR = (1.0, 0.35, 0.25)          # 危险提示用红
TPS = 40
STRONG_TICKS = 24                       # 出现 / 消失的溶解持续多久（40 TPS 下 0.6 s）；站稳之后按钮才可点
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
        draw_edge(p, width, height, color, 0.0, 0, x, y)


def _drift(t: float, seed: int, amp: float) -> tuple[float, float]:
    """平滑的小位移：几个频率的正弦叠起来（周期 1–7 s），像原版 ``panPos`` 那种慢悠悠的随机游走，但可复现、不跳。"""
    a = seed * 1.7
    return (amp * (0.6 * math.sin(0.9 * t + a) + 0.3 * math.sin(2.3 * t + 2 * a + 1) + 0.1 * math.sin(5.1 * t + a)),
            amp * (0.6 * math.sin(0.7 * t + 3 * a + 2) + 0.3 * math.sin(1.9 * t + a + 4) + 0.1 * math.sin(4.3 * t)))


def draw_edge(p: QPainter, width: float, height: float, color, t: float = 0.0, seed: int = 0,
              x: float = 0.0, y: float = 0.0) -> None:
    """原版投影框的边（用户 2026-11-01 给的游戏截图）：一条清楚的细线 + 一条错开几像素的淡残影，
    两条都随时间平滑地轻微漂移。边上的断口不是画出来的——是后面 ``dissolve`` 的噪声遮罩在边上最密地挖出来的（和原版同源）。"""
    p.save()
    p.setBrush(Qt.BrushStyle.NoBrush)
    dx, dy = _drift(t, seed, 0.8)
    p.setPen(QPen(qcolor(color, 0.9, 1.15), 1.2))
    p.drawPath(panel_shape(width, height, x + dx, y + dy))
    gx, gy = _drift(t + 3.1, seed + 5, 0.7)
    p.setPen(QPen(qcolor(color, 0.4), 1.0))
    p.drawPath(panel_shape(width, height, x + 2.2 + gx, y - 1.6 + gy))
    p.restore()


EDGE_MARGIN = 5.0                       # 面板四周留给边线位移的余量
CUT = 12.0                              # 八边形切角

# ---- 溶解遮罩：照 Hologram 系着色器（StreamingAssets/shaders/HologramImage.shader）----
# 原版的"闪"不是行错位，也不是定时故障：每个像素按一张噪声图取值 h，h 超过阈值就丢掉。噪声图随游戏时间连续滚动
# （纵向 _RAIN*10 = 每秒 2 个 UV），所以破洞是连续漂移的；离中心越远指数越小（越容易被丢），所以边上洞最密，
# 中间几乎不丢；淡入淡出 = 阈值 × alpha 在变，面板是被"溶"出来、"溶"没的。
BLOCK = 2                               # 遮罩的块大小（屏幕像素）：原版按像素量化，颗粒是块状的
NOISE_N = 256                           # 噪声图边长（原版 _NoiseTex2 也是 256）
THRESHOLD = 0.65                        # 游戏里 _hologramThreshold（render scale 为 1 时）
SCROLL_UV_PER_SEC = 2.0                 # 噪声纵向滚动速度：_RAIN = shadersTime/5，shader 里 _RAIN*10
ROW_SCROLL = 0.03                       # 隔三行加亮那一组行的滚动速度：_RAIN*0.15
DST_SCALE = 0.95                        # 面板边框线落在"距离"0.93 左右：边上有断口但大体是一条清楚的细线（不是一圈点）
EDGE_POW = 8.0                         # 距离中心的"圆度"：原版是椭圆，面板是矩形，用高次范数让角上的字别被溶掉

_NOISE = None


def _noise():
    """噪声图：轻微模糊的随机噪声，均值 0.5、标准差 0.2（和游戏的噪声图统计一致）；不是游戏素材，自己生成的。"""
    global _NOISE
    if _NOISE is None:
        n = np.random.default_rng(20261101).random((NOISE_N, NOISE_N), dtype=np.float32)
        n = (n + np.roll(n, 1, 0) + np.roll(n, -1, 0) + np.roll(n, 1, 1) + np.roll(n, -1, 1)) / 5
        n = (n - n.mean()) / n.std() * 0.197 + 0.505
        _NOISE = np.clip(n, 0.0, 1.0).astype(np.float32)
    return _NOISE


class _Dissolve:
    """一个尺寸的面板的溶解遮罩（不随时间变的那部分算一次）。"""

    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        bw, bh = -(-width // BLOCK), -(-height // BLOCK)
        u = (np.arange(bw, dtype=np.float32) + 0.5) / bw
        self.v = (np.arange(bh, dtype=np.float32) + 0.5) / bh
        self.xi = (u * 4 * NOISE_N).astype(np.int64) % NOISE_N
        ax, ay = np.abs(2 * u - 1)[None, :], np.abs(2 * self.v - 1)[:, None]
        norm = (ax ** EDGE_POW + ay ** EDGE_POW) ** (1 / EDGE_POW)
        self.dst = np.clip(norm * DST_SCALE, 0.0, 1.0).astype(np.float32)

    def keep(self, t: float, alpha: float) -> np.ndarray:
        """(h, w) 的布尔遮罩：True = 这个像素留着。``t`` 秒，``alpha`` 0..1（淡入淡出）。"""
        if alpha <= 0.0:
            return np.zeros((self.h, self.w), dtype=bool)
        yi = (self.v * 8 * NOISE_N - t * SCROLL_UV_PER_SEC * NOISE_N).astype(np.int64) % NOISE_N
        h = _noise()[yi[:, None], self.xi[None, :]].copy()
        stripe = np.round((self.v - t * ROW_SCROLL) * 400) % 3 == 0              # 隔三行
        h[stripe] += 0.25 + (0.05 - 0.25) * alpha
        e0 = 0.5 + 9.5 * alpha                                                   # lerp(0.5, 10, alpha)
        h = h ** (e0 + (0.5 - e0) * self.dst)
        keep = h <= THRESHOLD * alpha
        return np.repeat(np.repeat(keep, BLOCK, 0), BLOCK, 1)[: self.h, : self.w]


def dissolve(img: QImage, mask: np.ndarray) -> QImage:
    """把遮罩为 False 的像素清成全透明（img 必须是 ARGB32_Premultiplied）。"""
    out = QImage(img)
    arr = np.ndarray((out.height(), out.width(), 4), np.uint8, buffer=out.bits(),
                     strides=(out.bytesPerLine(), 4, 1))
    arr[~mask] = 0
    return out


def smooth(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


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
    """一块面板的动画：整块面板是一张底图，每帧用连续滚动的噪声遮罩溶掉一部分像素（见 ``_Dissolve``）。
    出现 = 从全溶到不溶（``STRONG_TICKS`` 个 tick 内淡入），消失反过来；站稳后只剩边上和零星的破洞在漂。"""

    scrolling = False

    def __init__(self, image: QImage, seed: int = 0, color=None):
        self.image = image
        self.seed = seed
        self.color = color                       # 给了才画会动的边（draw_edge）
        self._scanned: QImage | None = None
        self._dissolve = _Dissolve(image.width(), image.height())

    def _base(self, offset: float) -> QImage:
        if self._scanned is None:
            self._scanned = scanlines(self.image)
        return self._scanned

    def frame(self, age: int, leaving: int | None = None, offset: float = 0.0) -> tuple[QImage, float]:
        """返回 (这一帧的图, 不透明度)。``age`` 出现后过了几个 tick；``leaving`` 开始消失后过了几个 tick（没消失 = None）。
        ``offset`` 只给滚动面板用，普通面板忽略。淡入淡出全靠溶解，不透明度恒为 1（消失完了是 0）。"""
        if leaving is not None:
            if leaving >= STRONG_TICKS:
                return self._base(offset), 0.0
            alpha = 1.0 - smooth(leaving / STRONG_TICKS)
        else:
            alpha = smooth(age / STRONG_TICKS)
        t = age / TPS + self.seed * 13.7             # 不同面板的噪声相位不同
        image = self._base(offset)
        if self.color is not None:
            image = QImage(image)
            p = QPainter(image)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            draw_edge(p, image.width(), image.height(), self.color, t, self.seed)
            p.end()
        return dissolve(image, self._dissolve.keep(t, alpha)), 1.0


class ScrollPanel(Hologram):
    """长文：固定高度的取景窗，正文按像素偏移上滚（偏移由调用方按时间算）。

    布局：标题行 / 取景窗（``view_lines`` 行高）/ 一行小字提示。取景窗上下各留一点渐隐，字不会硬切。
    每帧按偏移重新合成，再过一遍溶解遮罩。"""

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

    def _base(self, offset: float) -> QImage:
        return self._compose(offset)


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
