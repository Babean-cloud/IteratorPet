"""分层合成：自身的光 → 分层落影 → 光环 → 本体。窗口和离线预览共用。

落影是桌面增补：游戏里那一层层力度不同的影子来自房间里的放置光源和地形着色器，
桌面上没有墙，所以用一盏假想主光（``SHADOW_LAYERS``）把本体剪影往右下偏移几次，
越远越淡。剪影 = 本体层用 ``SourceIn`` 涂黑，不模糊——分层本身就是像素风。

只在**像素风**下做影子：剪影要对整层图做合成，按游戏像素做是 1138×608，按屏幕像素
做是 2560×1600 × 3 层，40 TPS 下吃不消。平滑模式只画自身的光。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QPainter

from ..simulation import constants as C
from .renderer import draw_scene

_FORMAT = QImage.Format.Format_ARGB32_Premultiplied


class Compositor:
    """持有几张复用的缓冲图（每 tick 重新分配会自己把自己卡住）。"""

    def __init__(self) -> None:
        self._body: QImage | None = None
        self._silhouette: QImage | None = None

    def _buffer(self, attr: str, width: int, height: int) -> QImage:
        image = getattr(self, attr)
        if image is None or image.width() != width or image.height() != height:
            image = QImage(max(1, width), max(1, height), _FORMAT)
            setattr(self, attr, image)
        image.fill(Qt.GlobalColor.transparent)
        return image

    def render(self, target: QImage, state, scale: float, sprites=None,
               antialias: bool = False, shadow: bool = True) -> None:
        """把整个场景画进 ``target``（调用方负责先清空）。"""
        if not shadow:
            painter = QPainter(target)
            draw_scene(painter, state, scale=scale, sprites=sprites,
                       antialias=antialias, shadow=False)
            painter.end()
            return

        width, height = target.width(), target.height()
        body = self._buffer("_body", width, height)
        painter = QPainter(body)
        draw_scene(painter, state, scale=scale, sprites=sprites,
                   antialias=antialias, layer="body")
        painter.end()

        silhouette = self._buffer("_silhouette", width, height)
        painter = QPainter(silhouette)
        painter.drawImage(0, 0, body)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(silhouette.rect(), Qt.GlobalColor.black)
        painter.end()

        painter = QPainter(target)
        draw_scene(painter, state, scale=scale, sprites=sprites,
                   antialias=antialias, layer="glow")
        for dx, dy, alpha in reversed(C.SHADOW_LAYERS):
            painter.setOpacity(alpha)
            painter.drawImage(QPointF(dx * scale, dy * scale), silhouette)
        painter.setOpacity(1.0)
        draw_scene(painter, state, scale=scale, sprites=sprites,
                   antialias=antialias, layer="halo")
        painter.drawImage(0, 0, body)
        painter.end()
