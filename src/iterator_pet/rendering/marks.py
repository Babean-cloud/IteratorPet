"""心事标记：它在替你盯着什么时，光环正上方有一圈慢慢转的细弧，弧上几颗小光点 = 几件心事。

刚被叫醒、正在看（``busy``）时转得快、亮一些——"在想"。压在人偶之上、屏幕分辨率下画，和珍珠一样。
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient

from ..simulation import constants as C

GAP = 9.0                    # 光环最外圈再往上多少仿真像素
RADIUS = 6.0


def _anchor(state) -> tuple[float, float] | None:
    """光环正上方（光环跟着头转，画在头顶会被光环的圈套住，看着像光环的一部分）。"""
    center = getattr(state, "halo_center", None)
    if center is None:
        head = getattr(state, "head", None)
        return None if head is None else (head[0], head[1] - 40.0)
    # 最外一圈 bit 的中心在 Radius(最后一环 + 0.5)，bit 半长 4 px（renderer.py 第 2 步）
    rings = len(C.HALO_RINGS)
    outer = (C.HALO_RADIUS_BASE + rings - 0.5 + getattr(state, "halo_push", 0.0)) \
        * getattr(state, "halo_expand", 1.0) * 10.0 + 4.0
    return center[0], center[1] - outer - GAP - RADIUS


def _palette(state) -> tuple[QColor, QColor]:
    """和当前迭代器电弧、光晕取同一色系。"""
    base = QColor(*state.appearance.halo)
    light = QColor(*(min(255, int(channel * 0.65 + 255 * 0.35))
                     for channel in state.appearance.halo))
    return base, light


def _glow(painter: QPainter, cx: float, cy: float, radius: float,
          color: QColor, strength: float) -> None:
    gradient = QRadialGradient(QPointF(cx, cy), radius)
    inner = QColor(color)
    inner.setAlphaF(strength)
    outer = QColor(color)
    outer.setAlpha(0)
    gradient.setColorAt(0.0, inner)
    gradient.setColorAt(1.0, outer)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawEllipse(QPointF(cx, cy), radius, radius)


def draw_screen_mark(painter: QPainter, state, scale: float, level: float, lift: bool = False,
                     found: float = 0.0) -> None:
    """读屏幕时光环正上方的一小圈全息圆：细环 + 一条自上而下扫过的线。``level`` 0..1 是淡入淡出。
    ``found`` 0..1：刚认出新内容，环亮一下、向外扩一圈淡淡的波纹（随时间衰减到 0）。
    有心事标记时（``lift``）再往上让一格，免得叠在一起。"""
    anchor = _anchor(state) if max(level, found) > 0.01 else None
    if anchor is None:
        return
    now = time.monotonic()
    radius = RADIUS * 0.85 * scale
    cx = anchor[0] * scale
    cy = (anchor[1] - (2 * RADIUS + 3.0 if lift else 0.0)) * scale
    sweep = (now * 1.5) % 1.0                                    # 0→1 扫一遍
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setOpacity(min(1.0, max(level, found)))
    base, light = _palette(state)
    _glow(painter, cx, cy, radius * 2.0, base, 0.12 + 0.2 * found)
    if found > 0.01:
        wave = QColor(base)
        wave.setAlphaF(0.5 * found)
        painter.setPen(QPen(wave, max(1.0, scale), Qt.PenStyle.DashLine,
                            Qt.PenCapStyle.FlatCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        spread = radius * (1.0 + 0.7 * (1.0 - found))
        painter.drawEllipse(QPointF(cx, cy), spread, spread)
    ring = QColor(base)
    ring.setAlphaF(0.7 + 0.3 * found)
    painter.setPen(QPen(ring, max(1.0, scale), Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.FlatCap))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius),
                    20 * 16, 320 * 16)
    # 方形发光点与逐像素跳动的扫描线；保留原版 bit 的颗粒感
    block = max(1, round(scale))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(light)
    painter.drawRect(round(cx - block / 2), round(cy - radius - block / 2), block, block)
    y = round(cy - radius + 2 * radius * sweep)
    half = math.sqrt(max(0.0, radius * radius - (y - cy) ** 2))
    line = QColor(light)
    line.setAlphaF(0.9 * (1.0 - 0.5 * abs(sweep - 0.5) * 2))
    painter.setBrush(line)
    painter.drawRect(round(cx - half), y, max(1, round(2 * half)), block)
    painter.restore()


def draw_errand_mark(painter: QPainter, state, scale: float, count: int, busy: bool = False) -> None:
    anchor = _anchor(state) if count else None
    if anchor is None:
        return
    now = time.monotonic()
    speed = 2.4 if busy else 0.6
    phase = now * speed
    cx, cy = anchor[0] * scale, anchor[1] * scale
    radius = RADIUS * scale
    pulse = 0.5 + 0.5 * math.sin(now * (5.0 if busy else 1.6))
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    base, light = _palette(state)
    _glow(painter, cx, cy, radius * 2.2, base, 0.22 + 0.18 * pulse)
    arc = QColor(base)
    arc.setAlphaF(0.6 + (0.4 if busy else 0.25) * pulse)
    painter.setPen(QPen(arc, max(1.0, scale), Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.FlatCap))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
    start = int(-math.degrees(phase) * 16)
    painter.drawArc(rect, start, 250 * 16)                     # 缺一个口的圈：在"看"，不是装饰环
    dot = QColor(light)
    dot.setAlphaF(0.85 + 0.15 * pulse)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(dot)
    for index in range(min(count, 5)):
        angle = phase + index * (2 * math.pi / max(3, count))
        x, y = cx + radius * math.cos(angle), cy - radius * math.sin(angle)
        size = max(1, round(2 * scale))
        painter.drawRect(round(x - size / 2), round(y - size / 2), size, size)
    pupil = QColor(light)
    pupil.setAlphaF(0.63 + 0.35 * pulse)
    painter.setBrush(pupil)
    size = max(1, round(scale))
    painter.drawRect(round(cx - size / 2), round(cy - size / 2), size, size)
    painter.restore()
