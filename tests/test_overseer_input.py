"""第三期：输入投影。真输入框仍是 QuickBar 这个窗口，监视者只负责出来、给它留位置、画光束。"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRectF  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from iterator_pet.platform import pet_window, quick_bar  # noqa: E402
from iterator_pet.platform import projections as P  # noqa: E402
from iterator_pet.rendering.overseer_layer import OverseerLayer  # noqa: E402
from iterator_pet.simulation.appearance import PEBBLES  # noqa: E402
from iterator_pet.simulation.overseer import HOVERING  # noqa: E402

SIZE = (1280, 720)


def _app():
    return QGuiApplication.instance() or __import__("PySide6.QtWidgets", fromlist=["QApplication"]).QApplication([])


def _state():
    return SimpleNamespace(base_point=(300.0, 0.0), head=(300.0, 200.0), joints=[(300.0, 0.0), (310.0, 100.0)],
                           cord=[(300.0, 200.0), (300.0, 250.0)], appearance=PEBBLES, cursor=None)


def test_reserve_gives_a_rect_on_screen_and_draws_no_panel():
    _app()
    layer = OverseerLayer()
    assert layer.reserved_rect is None
    layer.show(7, [], reserve=(560, 90))
    for _ in range(60):
        layer.step(_state(), 1.0, SIZE)
    rect = layer.reserved_rect
    assert rect is not None and (rect.width(), rect.height()) == (560, 90)
    assert 0 <= rect.left() and rect.right() <= SIZE[0] and 0 <= rect.top() and rect.bottom() <= SIZE[1]
    assert layer._holo is None and layer.sim.phase == HOVERING             # 不自己画面板
    first = QRectF(rect)
    for _ in range(40):
        layer.step(_state(), 1.0, SIZE)
    assert layer.reserved_rect == first                                    # 位置钉死，不跟着漂
    layer.show(7, [], reserve=(560, 300))                                  # 补全列表展开：只变高，仍在屏内
    layer.step(_state(), 1.0, SIZE)
    grown = layer.reserved_rect
    assert grown.height() == 300 and grown.bottom() <= SIZE[1]
    layer.clear()
    assert layer.reserved_rect is None


class _FakeBar:
    holo = True

    def __init__(self):
        self.visible, self.docked, self.opacity, self.hidden = True, [], [], 0

    def isVisible(self):
        return self.visible

    def width(self):
        return 560

    def height(self):
        return 90

    def hide(self):
        self.visible = False
        self.hidden += 1

    def dock(self, top_left, extended):
        self.docked.append((top_left.x(), top_left.y()))
        self.opacity.append(extended)


def _fake_window(bar):
    state = _state()
    queue = P.ProjectionQueue()
    return SimpleNamespace(
        simulation=SimpleNamespace(interpolate=lambda a: state, dragging=False, swap_phase=None, ticks=1),
        _fullscreen_quiet=False, director=None, _chat_window=None, _quick_bar=bar, projections=queue,
        _projection_source=P.ProjectionSource(queue), current_task=None, overseer=OverseerLayer(),
        overseer_enabled=True, scale=1.0, width=lambda: SIZE[0], height=lambda: SIZE[1],
        _local_cursor=lambda: (5.0, 5.0), mapToGlobal=lambda p: QPoint(p.x() + 100, p.y() + 50))


def test_window_docks_bar_to_the_reserved_rect_then_withdraws_when_bar_closes():
    _app()
    bar = _FakeBar()
    win = _fake_window(bar)
    win.projections.post("input", P.PRI_INPUT, [], ttl=None)
    for _ in range(60):
        pet_window.PetWindow._step_overseer(win)
    rect = win.overseer.reserved_rect
    assert rect is not None and bar.docked
    assert bar.docked[-1] == (int(rect.x()) + 100, int(rect.y()) + 50)     # 屏幕坐标 = 窗口坐标 + 窗口位置
    assert bar.opacity[-1] == 1.0 and win.overseer.sim.phase == HOVERING
    bar.visible = False                                                    # Esc / 发送 / 点别处
    pet_window.PetWindow._step_overseer(win)
    assert not win.projections.has("input")


def test_higher_priority_projection_hides_the_bar():
    _app()
    bar = _FakeBar()
    win = _fake_window(bar)
    win.projections.post("input", P.PRI_INPUT, [], ttl=None)
    for _ in range(5):
        pet_window.PetWindow._step_overseer(win)
    win.projections.post("approval", P.PRI_APPROVAL, [], ttl=None)
    pet_window.PetWindow._step_overseer(win)
    assert bar.hidden == 1                                                 # 批准要看得见，输入条让位
    pet_window.PetWindow._step_overseer(win)
    assert not win.projections.has("input") and win.projections.current.kind == "approval"


def test_quick_bar_skin_follows_overseer_switch(monkeypatch):
    _app()
    screen = SimpleNamespace(availableGeometry=lambda: __import__("PySide6.QtCore", fromlist=["QRect"]).QRect(0, 0, 1920, 1080))
    pet = SimpleNamespace(character="past_moon", overseer_enabled=True, scale=1.5, _drawn_state=None, screen=lambda: screen,
                          x=lambda: 0, y=lambda: 0, director=None)
    bar = quick_bar.QuickBar(pet)
    bar.popup()
    assert bar.holo and bar._holo_color == (1.0, 0.8, 0.3)                 # 月是黄的
    assert "rgba(" in bar.styleSheet() and bar.windowOpacity() < 1.0
    assert bar.layout().contentsMargins().left() == quick_bar.HOLO_MARGIN
    bar.hide()
    pet.overseer_enabled = False
    bar.popup()
    assert not bar.holo and bar.styleSheet() == "" and bar.windowOpacity() == 1.0
    bar.hide()
