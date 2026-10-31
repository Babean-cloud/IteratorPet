import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402

from iterator_pet.config import Config  # noqa: E402
from iterator_pet.platform import pet_window, tray  # noqa: E402
from iterator_pet.platform.projections import PRI_STATUS, ProjectionQueue, ProjectionSource  # noqa: E402
from iterator_pet.rendering.hologram import Line  # noqa: E402
from iterator_pet.rendering.overseer_layer import OverseerLayer  # noqa: E402
from iterator_pet.simulation.appearance import PEBBLES  # noqa: E402
from iterator_pet.simulation.overseer import HIDDEN  # noqa: E402


def test_default_style_is_overseer_and_menu_has_switch():
    assert Config().ui_style == "overseer"
    assert "overseer_enabled" in [key for key, _ in tray.VIEW_LABELS]


def _fake_window(enabled):
    state = SimpleNamespace(base_point=(300.0, 0.0), head=(300.0, 200.0), joints=[(300.0, 0.0)], cord=[(300.0, 200.0)],
                            appearance=PEBBLES, cursor=None)
    queue = ProjectionQueue()
    return SimpleNamespace(
        simulation=SimpleNamespace(interpolate=lambda a: state, dragging=False, swap_phase=None, ticks=1),
        _fullscreen_quiet=False, director=SimpleNamespace(mood="", thought=""), _chat_window=None,
        projections=queue, _projection_source=ProjectionSource(queue), current_task=None,
        overseer=OverseerLayer(), overseer_enabled=enabled, _local_cursor=lambda: (5.0, 5.0), scale=1.0, width=lambda: 1280, height=lambda: 720,
        config=Config(), save=None)


def test_switch_off_projects_nothing_and_retracts():
    QGuiApplication.instance() or QGuiApplication([])
    win = _fake_window(True)
    win.projections.post("x", PRI_STATUS, [Line("hi")])
    for _ in range(80):
        pet_window.PetWindow._step_overseer(win)
    assert win.overseer.visible
    win.overseer_enabled = False
    for _ in range(40):
        pet_window.PetWindow._step_overseer(win)
    assert win.projections.current is None and win.overseer.sim.phase == HIDDEN
