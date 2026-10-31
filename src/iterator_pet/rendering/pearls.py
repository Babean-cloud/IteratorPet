"""画珍珠：照 DataPearl 的三层——柔光（Futile_White + FlatLight）、本体 JetFishEyeA（珍珠色）、
左上一点高光 tinyStar（珍珠色往白里调 80%）。在人偶之后画，压在最上面。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QRadialGradient

from .sprites import tinted

GLOW_RADIUS = 10.0           # 仿真像素
HIGHLIGHT_OFFSET = (-1.0, -1.2)


def _lighter(color, fac: float) -> tuple[int, int, int]:
    return tuple(int(c + (255 - c) * fac) for c in color)


def draw_pearls(painter: QPainter, state, scale: float) -> None:
    pearls = getattr(state, "pearls", ())
    if not pearls:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)   # 图元最近邻放大
    for (x, y), color, glow in pearls:
        cx, cy = x * scale, y * scale
        radius = GLOW_RADIUS * (0.7 + 0.6 * glow) * scale
        gradient = QRadialGradient(QPointF(cx, cy), radius)
        inner = QColor(*_lighter(color, 0.3))
        inner.setAlphaF(min(1.0, 0.18 + 0.42 * glow))
        outer = QColor(*color)
        outer.setAlphaF(0.0)
        gradient.setColorAt(0.0, inner)
        gradient.setColorAt(1.0, outer)
        painter.setPen(QColor(0, 0, 0, 0))
        painter.setBrush(gradient)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        body = tinted("JetFishEyeA", color)
        w, h = body.width() * scale, body.height() * scale
        painter.drawPixmap(QRectF(cx - w / 2, cy - h / 2, w, h), body, QRectF(body.rect()))
        star = tinted("tinyStar", _lighter(color, 0.8))
        sw, sh = star.width() * scale, star.height() * scale
        sx, sy = cx + HIGHLIGHT_OFFSET[0] * scale, cy + HIGHLIGHT_OFFSET[1] * scale
        painter.drawPixmap(QRectF(sx - sw / 2, sy - sh / 2, sw, sh), star, QRectF(star.rect()))
    painter.restore()
