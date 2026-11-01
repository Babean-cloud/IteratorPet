import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402

from iterator_pet.rendering.overseer_layer import OverseerLayer  # noqa: E402
from iterator_pet.simulation.appearance import PAST_MOON, PEBBLES  # noqa: E402
from iterator_pet.simulation.overseer import HIDDEN, HOVERING  # noqa: E402

SIZE = (1280, 720)


def _state(appearance=PEBBLES):
    return SimpleNamespace(base_point=(300.0, 0.0), head=(300.0, 200.0), joints=[(300.0, 0.0), (310.0, 100.0)],
                           cord=[(300.0, 200.0), (300.0, 250.0)], appearance=appearance, cursor=None)


def _painted_pixels(layer):
    image = QImage(*SIZE, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    layer.draw(painter, SIZE)
    painter.end()
    return sum(1 for y in range(0, SIZE[1], 4) for x in range(0, SIZE[0], 4) if image.pixelColor(x, y).alpha() > 0)


def test_hidden_draws_nothing_then_emerges_and_retracts():
    QGuiApplication.instance() or QGuiApplication([])
    layer = OverseerLayer()
    state = _state()
    layer.step(state, 1.0, SIZE)
    assert layer.version == -1 and _painted_pixels(layer) == 0
    layer.want(True)
    for _ in range(60):
        layer.step(state, 1.0, SIZE)
    assert layer.sim.phase == HOVERING
    assert _painted_pixels(layer) > 50
    layer.step(state, 1.0, SIZE, quiet=True)             # 被拖拽等：立刻缩回
    for _ in range(40):
        layer.step(state, 1.0, SIZE, quiet=True)
    assert layer.sim.phase == HIDDEN and _painted_pixels(layer) == 0


def test_panel_shows_when_hovering_survives_retract_and_clears():
    from iterator_pet.rendering.hologram import STRONG_TICKS, Line

    QGuiApplication.instance() or QGuiApplication([])
    layer = OverseerLayer()
    state = _state()
    layer.show(1, [Line("它在想", "title"), Line("一段比较长的念头" * 6)])
    for _ in range(60):
        layer.step(state, 1.0, SIZE)
    bare = OverseerLayer()
    bare.want(True)
    for _ in range(60):
        bare.step(state, 1.0, SIZE)
    for _ in range(STRONG_TICKS + 2):
        layer.step(state, 1.0, SIZE)
    assert layer._holo is not None and _painted_pixels(layer) > _painted_pixels(bare) + 100
    for _ in range(40):                                     # 被拖拽缩回：内容留着，面板不画
        layer.step(state, 1.0, SIZE, quiet=True)
    assert layer.sim.phase == HIDDEN and layer._content is not None
    for _ in range(60):                                     # 放开后自己再出来
        layer.step(state, 1.0, SIZE)
    assert layer.sim.phase == HOVERING and layer._holo is not None
    layer.clear()
    layer.step(state, 1.0, SIZE)
    assert layer._content is None and layer._ghost is not None
    for _ in range(STRONG_TICKS + 2):
        layer.step(state, 1.0, SIZE)
    assert layer._ghost is None


def test_color_follows_character():
    QGuiApplication.instance() or QGuiApplication([])
    layer = OverseerLayer()
    layer.want(True)
    layer.step(_state(), 1.0, SIZE)
    layer.step(_state(), 1.0, SIZE)
    pebbles = layer._graphics.color
    layer.step(_state(PAST_MOON), 1.0, SIZE)
    assert layer._graphics.color != pebbles


def test_buttons_follow_the_panel_when_a_redraw_makes_it_taller():
    """勾了联网多一行说明：面板变高、按钮被挤下去，命中区域也要跟着下去（用户实测）。"""
    from iterator_pet.rendering.hologram import STRONG_TICKS, Button, Line, button_rects

    QGuiApplication.instance() or QGuiApplication([])
    layer = OverseerLayer()
    state = _state()
    buttons = (Button("cancel", "取消", "cross"), Button("start", "开始", "check"))
    layer.show(1, [Line("让它去做", "title")], buttons=buttons)
    for _ in range(60 + STRONG_TICKS):
        layer.step(state, 1.0, SIZE)
    before = layer._rect.height()
    layer.show(1, [Line("让它去做", "title"), Line("联网：这个任务可以上网查资料"), Line("多一行说明")],
               buttons=buttons, revision=1)
    layer.step(state, 1.0, SIZE)
    rect = layer._rect
    assert rect.height() > before
    start = button_rects(int(rect.width()), int(rect.height()), buttons)[1].center()
    assert layer.button_at((rect.x() + start.x(), rect.y() + start.y())) == "start"
