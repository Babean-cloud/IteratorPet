"""把监视者行为（simulation/overseer.py）和画法（rendering/overseer.py）接到画布上。

整层都在**屏幕像素**里：行为的距离常量是屏幕像素，本体先画进一张低分辨率透明图（屏幕 / OVERSEER_ZOOM），
再最近邻放大贴上，和人偶一样是像素风；柔光直接画在屏幕分辨率上（design §3.3）。
触须状态留在 ``OverseerGraphics`` 里，行为只存头 / 根 / extended。
"""

from __future__ import annotations

import math

from dataclasses import dataclass, replace

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from ..simulation.overseer import HOVERING, OverseerSim
from .hologram import (STRONG_TICKS, Hologram, Line, ScrollPanel, beam, button_rects, place_panel,
                       render_panel)
from .overseer import MAIN_COLORS, OverseerGraphics

PANEL_WIDTH = 360
SCROLL_DELAY = 1.5                      # 长文出现后先停这么久再开始滚（秒）


@dataclass(frozen=True)
class _Content:
    key: int
    lines: list
    buttons: tuple
    width: int
    revision: int
    scroll_text: str
    scroll_cps: float
    reserve: tuple | None = None        # (宽, 高)：不画面板，只留出这块地方给真实窗口（输入投影的 QuickBar）

# 游戏像素 → 屏幕像素 = 画面缩放 × 这个系数，监视者跟着迭代器一起大小。
# 用户实机反馈要比效果图（固定 2.5）小 30%：他的缩放是 1.5，所以 2.5×0.7÷1.5
ZOOM_PER_SCALE = 7 / 6
SIZE = 0.765                            # 用户 2026-11-01：在 0.85 基础上再小 10%
ROOT_OUTSIDE = 8.0                      # 根点放在屏幕外几像素 = 墙里
SWAY = 6.0                              # 悬停时的小幅漂浮
FACING_IDLE, FACING_SHOW = 0.3, 0.55


class OverseerLayer:
    def __init__(self, seed: int = 11, prefer=None):
        self.sim = OverseerSim(seed, prefer)
        self._seed = seed
        self._graphics: OverseerGraphics | None = None
        self._key: str | None = None
        self._low: QImage | None = None
        self._tick = 0
        self._zoom = 1.75
        # 投影面板：内容（key, 行）留在这里，监视者缩回再出来时还在（design §3.1 "投影内容不丢"）
        self._content: _Content | None = None
        self._holo: Hologram | None = None
        self._holo_key: tuple | None = None                      # (key, revision, hover)：变了就重画面板
        self._rect: QRectF | None = None
        self._age = 0
        self._leaving: int | None = None
        self._ghost: tuple[Hologram, QRectF] | None = None      # 正在消失的旧面板
        self._color = MAIN_COLORS["pebbles"]
        self._cursor: tuple[float, float] | None = None         # 光标（屏幕像素），悬停高亮 / 暂停滚动用
        self._scroll_ticks = 0
        self._reserved: QRectF | None = None
        self._reserve_stamp: tuple | None = None

    @property
    def visible(self) -> bool:
        return self.sim.visible

    @property
    def version(self) -> int:
        """帧缓存用：每 tick 变一次（可见时）。"""
        return self._tick if self.sim.visible else -1

    def want(self, wanted: bool) -> None:
        self.sim.want(wanted)

    def retract_now(self) -> None:
        self.sim.retract_now()

    def show(self, key: int, lines: list[Line], *, buttons=(), width: int = PANEL_WIDTH, revision: int = 0,
             scroll_text: str = "", scroll_cps: float = 0.0, reserve: tuple | None = None) -> None:
        """投一块面板。``key`` 变了就是换了内容（重画面板、重放出现效果）；``revision`` 变了只重画（选了别的选项）。
        ``reserve=(宽, 高)``：只给监视者留位置和光束，面板本身是外面的真实窗口（输入投影），位置从 ``reserved_rect`` 取。"""
        self._content = _Content(key, lines, tuple(buttons), width, revision, scroll_text, scroll_cps, reserve)

    @property
    def reserved_rect(self) -> QRectF | None:
        """``reserve`` 面板该放的位置（本窗口坐标）；监视者还没钻出来选好洞之前是 None。"""
        c = self._content
        return self._reserved if c is not None and c.reserve is not None else None

    @property
    def extended(self) -> float:
        return self.sim.extended

    def _step_reserve(self, body, head, inward, size) -> None:
        c = self._content
        if c is None or c.reserve is None:
            self._reserved = None
            return
        stamp = (c.key, tuple(c.reserve), self.sim.hole)      # 换洞 / 换尺寸才重算，平时位置钉死不跟着漂
        if stamp != self._reserve_stamp:
            self._reserve_stamp = stamp
            self._reserved = place_panel(c.reserve[0], c.reserve[1], body, head, inward, size)

    def clear(self) -> None:
        """撤掉面板：淡出一下，监视者空闲后自己缩回。"""
        if self._content is not None:
            self._content = None
            if self._holo is not None and self._rect is not None:
                self._ghost, self._leaving = (self._holo, self._rect), 0
            self._holo = self._rect = self._holo_key = None
        self.sim.want(False)

    # -- 交互：渲染层只负责"光标在哪个按钮上"，按下 / 松开的配对判断在窗口里 ----------
    def set_cursor(self, point: tuple[float, float] | None) -> None:
        self._cursor = point

    @property
    def interactive(self) -> bool:
        """面板已经站稳（出现的强错动播完）、监视者在悬停：这时按钮才可点。"""
        return (self._holo is not None and self._rect is not None and self._content is not None
                and self.sim.phase == HOVERING and self._age >= STRONG_TICKS)

    def button_at(self, point: tuple[float, float] | None) -> str | None:
        """屏幕像素的点落在哪个按钮上；长文面板整块算 ``expand``。"""
        if point is None or not self.interactive:
            return None
        rect, content = self._rect, self._content
        if not rect.contains(point[0], point[1]):
            return None
        if content.scroll_text:
            return "expand"
        local = (point[0] - rect.x(), point[1] - rect.y())
        for button, r in zip(content.buttons, button_rects(int(rect.width()), int(rect.height()), content.buttons)):
            if r.contains(*local):
                return button.name
        return None

    @property
    def panel_hovered(self) -> bool:
        """光标压在面板上：长文暂停滚动、队列暂停倒计时。"""
        return bool(self._cursor is not None and self._holo is not None and self._rect is not None
                    and self.sim.phase == HOVERING and self._rect.contains(*self._cursor))

    def _build(self, content: "_Content", hover: str | None) -> Hologram:
        if content.scroll_text:
            return ScrollPanel(content.width, content.lines[0], content.scroll_text, "点一下展开完整对话窗口",
                               self._color, seed=content.key)
        buttons = [replace(b, hot=b.name == hover) for b in content.buttons]
        return Hologram(render_panel(content.width, content.lines, self._color, buttons), seed=content.key)

    def step(self, state, scale: float, size: tuple[float, float], look=None, quiet: bool = False,
             avoid=()) -> None:
        """每仿真 tick 一次。``state`` 是仿真快照（仿真像素），``look`` 是屏幕像素的视线目标（没有就看人偶头）。
        ``quiet``：人偶被拖拽 / 全屏勿扰 / 换角色，监视者立刻缩回。``avoid``：钻出时别用的边（另一只占着）。"""
        self.sim.avoid = tuple(avoid)
        if quiet:
            self.sim.retract_now()
        elif self._content is not None:
            self.sim.want(True)
        bounds = (0.0, 0.0, float(size[0]), float(size[1]))
        scr = lambda p: (p[0] * scale, p[1] * scale)       # noqa: E731
        carriage, head = scr(state.base_point), scr(state.head)
        obstacles = [[scr(j) for j in state.joints], [scr(n) for n in state.cord]]
        self.sim.step(bounds, carriage, head, obstacles)
        if not self.sim.visible:
            return
        key = state.appearance.key
        if self._graphics is None or key != self._key:
            self._key = key
            self._graphics = OverseerGraphics(MAIN_COLORS.get(key, MAIN_COLORS["pebbles"]),
                                              size=SIZE, seed=self._seed)
        self._tick += 1
        snap = self.sim.snapshot()
        z = self._zoom = max(0.5, scale * ZOOM_PER_SCALE)
        sway = math.sin(self._tick * 0.045) * SWAY, math.cos(self._tick * 0.037) * SWAY
        body = (snap["body"][0] + sway[0], snap["body"][1] + sway[1]) if self.sim.phase == HOVERING else snap["body"]
        hole, inward = snap["hole"], snap["inward"]
        root = (hole[0] - inward[0] * ROOT_OUTSIDE, hole[1] - inward[1] * ROOT_OUTSIDE)
        target = look or head
        self._graphics.set_pose((body[0] / z, body[1] / z), (root[0] / z, root[1] / z), inward,
                                (target[0] / z, target[1] / z), extended=snap["extended"],
                                facing=FACING_SHOW if self.sim.wanted else FACING_IDLE)
        self._graphics.update()
        self._step_reserve(snap["body"], head, inward, size)
        self._step_panel(state, body, head, inward, size, MAIN_COLORS.get(key, MAIN_COLORS["pebbles"]))

    def regions(self) -> list[QRectF]:
        """本窗口坐标里监视者和它的面板占的矩形（读屏幕时盖掉，别把自己的投影当成屏幕内容）。"""
        if not self.sim.visible:
            return []
        out = [r for r in (self._rect, self.reserved_rect, self._ghost[1] if self._ghost else None) if r is not None]
        g, z = self._graphics, self._zoom
        if g is not None:
            pad = 60.0 * z * SIZE
            out.append(QRectF(g.head[0] * z - pad, g.head[1] * z - pad, 2 * pad, 2 * pad))
        return out

    def _step_panel(self, state, body, head, inward, size, color) -> None:
        if self._ghost is not None:
            self._leaving += 1
            if self._leaving > STRONG_TICKS:
                self._ghost = self._leaving = None
        if self.sim.phase != HOVERING:
            self._age = 0                  # 还没钻出来 / 已经在缩：面板不画，内容留着
            self._ghost = self._leaving = None
            return
        content = self._content
        if content is None or content.reserve is not None:
            return
        self._color = color
        hover = self.button_at(self._cursor) if not content.scroll_text else None
        stamp = (content.key, content.revision, hover)
        if self._holo is None or content.key != self._holo_key[0]:
            self._holo, self._holo_key, self._age, self._scroll_ticks = self._build(content, hover), stamp, 0, 0
            image = self._holo.image
            self._rect = place_panel(image.width(), image.height(), body, head, inward, size)
        else:
            self._age += 1
            if stamp != self._holo_key:                  # 同一块内容里选项 / 高亮变了：原地重画，不重放出现效果
                self._holo, self._holo_key = self._build(content, hover), stamp
            if self._age >= STRONG_TICKS and not self.panel_hovered:
                self._scroll_ticks += 1

    def _scroll_offset(self) -> float:
        """长文滚到哪了（像素）：先停 SCROLL_DELAY 再按字/秒匀速走，停在底。"""
        c, holo = self._content, self._holo
        if c is None or not holo.scrolling:
            return 0.0
        seconds = max(0.0, self._scroll_ticks / 40.0 - SCROLL_DELAY)
        return min(holo.max_offset, seconds * c.scroll_cps * holo.px_per_char)

    def draw(self, painter: QPainter, size: tuple[float, float]) -> None:
        g = self._graphics
        if g is None or not self.sim.visible:
            return
        z = self._zoom
        g.draw_glow(painter, z)
        w, h = math.ceil(size[0] / z), math.ceil(size[1] / z)
        if self._low is None or (self._low.width(), self._low.height()) != (w, h):
            self._low = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        self._low.fill(Qt.GlobalColor.transparent)
        lp = QPainter(self._low)
        lp.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        g.draw(lp)
        lp.end()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(QRectF(0.0, 0.0, w * z, h * z), self._low, QRectF(self._low.rect()))
        painter.restore()
        self._draw_panels(painter, g)

    def _draw_panels(self, painter: QPainter, g: OverseerGraphics) -> None:
        z = self._zoom
        eye = (g.head[0] * z, g.head[1] * z)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        shown = []
        if self._ghost is not None:
            shown.append((*self._ghost, self._age, self._leaving))
        if self._holo is not None and self._rect is not None and self.sim.phase == HOVERING:
            shown.append((self._holo, self._rect, self._age, None))
        for holo, rect, age, leaving in shown:
            image, opacity = holo.frame(age, leaving, offset=self._scroll_offset() if leaving is None else 0.0)
            if opacity <= 0.0:
                continue
            painter.setOpacity(opacity)
            beam(painter, eye, rect, g.color)
            painter.drawImage(rect.topLeft(), image)
        if self.reserved_rect is not None:                  # 输入投影：面板是外面的真实窗口，这里只画光束
            painter.setOpacity(max(0.0, min(1.0, self.sim.extended)))
            beam(painter, eye, self.reserved_rect, g.color)
        painter.restore()
