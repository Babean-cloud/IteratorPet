"""Transparent widget that paints the simulation each frame.

The simulation runs in *game pixels*; ``scale`` maps those onto screen pixels so
the pet keeps the original proportions at any resolution.

**为什么要缓存一帧**：这个窗口是"整屏大小 + 分层透明"的。任何盖在它上面的窗口
（托盘菜单、开发者面板、别的置顶窗口）一走开，Windows 就会让整屏区域重绘一次。
每次重绘都从头画 2 个环 + 100 个 bit + 20 根电光 + 机械臂 + 缆线（Python 侧几毫秒），
用户看到的就是"关掉菜单/面板卡一下"。所以：

* 仿真 tick 没变（关菜单、暂停、面板只是被拖开）→ 直接把上一帧的 pixmap 贴回去；
* tick 变了 → 正常画一遍，并顺手把这一帧存下来。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from .bubble import draw_bubble_world
from .compose import Compositor
from .marks import draw_errand_mark, draw_screen_mark
from .overseer_layer import OverseerLayer
from .pearls import draw_pearls
from .renderer import draw_scene


class PetCanvas(QWidget):
    def __init__(
        self,
        simulation,
        alpha_provider=None,
        parent=None,
        scale: float = 1.5,
        sprites=None,
    ):
        super().__init__(parent)
        self.simulation = simulation
        self.alpha_provider = alpha_provider or (lambda: 0.0)
        self.sprites = sprites
        self.scale = max(0.3, min(4.0, float(scale)))
        # 对话气泡开关（托盘「对话」）。窗口层会按 config 覆盖它。
        self.dialogue_enabled = True
        self._frame: QPixmap | None = None
        self._frame_key: tuple | None = None
        # 像素风：先按游戏像素画进一张小图（不抗锯齿），再最近邻放大。原作就是这样的观感，
        # 而且要画的像素少了 scale² 倍。pixel_snap = 每个游戏像素占整数个屏幕像素（不闪），
        # 代价是"像素"比游戏像素略大或略小。
        self.pixel_art = False
        self.pixel_snap = False
        self.drop_shadow = True
        self.show_pearls = True               # 收着的珍珠绕着它漂（「画面」菜单可关）
        self.errand_marks = 0                 # 在盯着的心事件数：头顶一圈小光点（marks.py）
        self.errand_busy = False
        self.screen_level = 0.0               # 正在读屏幕：0..1，头顶一圈青色小扫描圈（marks.py）
        self.screen_found = 0.0               # 刚认出新内容：0..1，扫描圈亮一下并扩一圈波纹
        self._low: QImage | None = None
        self._compositor = Compositor()
        self.overseer = OverseerLayer()
        self.bubble_rect = None               # 上一帧画出的气泡（本窗口坐标），读屏幕时盖掉
        # 命中测试要用的"屏幕上真正画出去的那一份"（帧缓存/alpha 会让重新插值的
        # 结果和屏幕上差一个 tick，拿它去命中会点不准）
        self._drawn_state = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAutoFillBackground(False)

    # -- 帧缓存 -------------------------------------------------------------
    def _frame_stamp(self) -> tuple:
        """这一帧画的是什么：仿真 tick + 画布尺寸 + 缩放 + 当前仿真对象。"""
        return (
            id(self.simulation),
            int(getattr(self.simulation, "ticks", 0)),
            self.width(),
            self.height(),
            self.scale,
            self.pixel_art,
            self.pixel_snap,
            self.drop_shadow,
            self.show_pearls,
            self.errand_marks,
            self.errand_busy,
            round(self.screen_level, 2),
            round(self.screen_found, 2),
        )

    def pixel_layout(self) -> tuple[float, float] | None:
        """像素风的 (小图每游戏像素几个小图像素, 每个小图像素占几个设备像素)；不适用返回 None。

        在**设备像素**里算：本机 DPR 1.5 × 缩放 1.5 = 一个游戏像素 2.25 个物理像素。
        不对齐时小图就是游戏像素（块宽 2.25，有的 2 有的 3）；对齐时块宽取整，
        小图分辨率随之微调（2.25 → 块 2，小图 1.125 px / 游戏 px）。
        """
        if not self.pixel_art:
            return None
        device = self.scale * (self.devicePixelRatioF() or 1.0)
        if device < 1.25:                     # 便携档：游戏像素比屏幕像素还小，像素风没意义
            return None
        if self.pixel_snap:
            block = float(max(1, round(device)))
            return device / block, block
        return 1.0, device

    def _ensure_frame(self) -> QPixmap:
        """按需分配/复用整屏 pixmap（每 tick 重新分配 14 MB 会自己把自己卡住）。"""
        ratio = self.devicePixelRatioF() or 1.0
        wanted = (int(self.width() * ratio), int(self.height() * ratio))
        if (
            self._frame is None
            or self._frame.width() != wanted[0]
            or self._frame.height() != wanted[1]
        ):
            pixmap = QPixmap(wanted[0], wanted[1])
            pixmap.setDevicePixelRatio(ratio)
            self._frame = pixmap
        return self._frame

    def _ensure_low(self, frame: QPixmap, block: float) -> QImage:
        """像素风的小图（DPR 1，复用）：边长 = 整屏设备像素 / 块宽，向上取整。"""
        wanted = (int(math.ceil(frame.width() / block)), int(math.ceil(frame.height() / block)))
        low = self._low
        if low is None or (low.width(), low.height()) != wanted:
            low = QImage(max(1, wanted[0]), max(1, wanted[1]),
                         QImage.Format.Format_ARGB32_Premultiplied)
            self._low = low
        return low

    def own_regions(self) -> list[tuple[int, int, int, int]]:
        """气泡、监视者和投影面板在屏幕上的物理像素矩形 (左, 上, 右, 下)，给读屏幕盖白用。"""
        from PySide6.QtCore import QPoint

        rects = [self.bubble_rect] if self.bubble_rect is not None else []
        rects += self.overseer.regions()
        if not rects:
            return []
        origin, dpr = self.mapToGlobal(QPoint(0, 0)), self.devicePixelRatioF()
        return [(int((origin.x() + r.left()) * dpr), int((origin.y() + r.top()) * dpr),
                 int((origin.x() + r.right()) * dpr) + 1, int((origin.y() + r.bottom()) * dpr) + 1)
                for r in rects]

    def _paint_dialogue(self, painter: QPainter) -> None:
        """画当前这句台词（屏幕像素，锚点从仿真 px 转换一次）。"""
        if not self.dialogue_enabled:
            return
        view = getattr(self.simulation, "dialogue_view", None)
        if view is None:
            return
        data = view()
        if not data:
            self.bubble_rect = None
            return
        self.bubble_rect = draw_bubble_world(
            painter,
            text=data["text"],
            size_fac=data["size_fac"],
            anchor_sim=data["anchor"],
            scale=self.scale,
            screen=(float(self.width()), float(self.height())),
            below_sim=data.get("below"),
        )

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        key = self._frame_stamp()
        if self._frame_key != key:
            pixmap = self._ensure_frame()
            pixmap.fill(Qt.GlobalColor.transparent)     # 复用同一块，只清内容
            frame_painter = QPainter(pixmap)
            frame_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            frame_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            state = self.simulation.interpolate(self.alpha_provider())
            self._drawn_state = state
            layout = self.pixel_layout()
            if layout is None:
                draw_scene(frame_painter, state, scale=self.scale, sprites=self.sprites,
                           shadow=self.drop_shadow)
                if self.show_pearls:
                    draw_pearls(frame_painter, state, self.scale)
                draw_errand_mark(frame_painter, state, self.scale, self.errand_marks, self.errand_busy)
                draw_screen_mark(frame_painter, state, self.scale, self.screen_level,
                                 lift=bool(self.errand_marks), found=self.screen_found)
            else:
                low_scale, block = layout
                low = self._ensure_low(pixmap, block)
                low.fill(Qt.GlobalColor.transparent)
                self._compositor.render(low, state, low_scale, sprites=self.sprites,
                                        antialias=False, shadow=self.drop_shadow)
                ratio = pixmap.devicePixelRatio() or 1.0
                frame_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                frame_painter.drawImage(
                    QRectF(0.0, 0.0, low.width() * block / ratio, low.height() * block / ratio),
                    low, QRectF(low.rect()),
                )
                if self.show_pearls:
                    # 珍珠画在放大之后（屏幕分辨率、亚像素位置）：它们漂得慢，画进小图会一格一格地跳。
                    # 图元本身仍按最近邻放大，保持像素颗粒感。
                    draw_pearls(frame_painter, state, self.scale)
                draw_errand_mark(frame_painter, state, self.scale, self.errand_marks, self.errand_busy)
                draw_screen_mark(frame_painter, state, self.scale, self.screen_level,
                                 lift=bool(self.errand_marks), found=self.screen_found)
            frame_painter.end()
            self._frame_key = key

        painter.drawPixmap(0, 0, self._frame)
        # 气泡必须在**缓存帧之后**单独画：它每 tick 都在变（逐字/收起），
        # 进 draw_scene 会让整屏每一帧都重画（审计 A1/A2 的代价就是托盘卡顿）。
        self._paint_dialogue(painter)
        # 监视者同理：每 tick 都在动，画在屏幕分辨率上（不进缓存帧），不可见时零开销
        self.overseer.draw(painter, (float(self.width()), float(self.height())))
        painter.end()
