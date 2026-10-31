"""监视者画法（rendering/overseer.py）的冒烟测试：能跑、不炸、触须不飞走、缩回时贴回身体。"""

import math

from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from iterator_pet.rendering.overseer import MAIN_COLORS, OverseerGraphics, v_len, v_sub

app = QApplication.instance() or QApplication([])


def _settle(g, ext=1.0, facing=0.5, ticks=80):
    for _ in range(ticks):
        g.set_pose((100, 60), (100, 130), (0, -1), (300, 40), extended=ext, facing=facing)
        g.update()


def test_tentacles_stay_near_head():
    g = OverseerGraphics(MAIN_COLORS["pebbles"], size=0.85, seed=1)
    _settle(g)
    for m in g.mycelia:
        for p in m.points:
            assert math.isfinite(p[0][0]) and math.isfinite(p[0][1])
            # 每节最长 con_rad，整根不可能比 (点数-1)·con_rad 更远
            assert v_len(v_sub(p[0], g.connection_pos())) <= (len(m.points) - 1) * m.con_rad + 1


def test_retracted_tentacles_collapse_to_body():
    g = OverseerGraphics(MAIN_COLORS["past_moon"], size=0.7, seed=2)
    _settle(g, ext=0.0, ticks=10)
    for m in g.mycelia:
        for p in m.points:
            assert v_len(v_sub(p[0], g.connection_pos())) < 1e-6


def test_draw_does_not_crash():
    g = OverseerGraphics(MAIN_COLORS["pebbles"], seed=3)
    _settle(g, ticks=5)
    img = QImage(200, 160, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    g.draw_glow(p, 1.0)
    g.draw(p)
    p.end()
    assert img.pixelColor(100, 60).alpha() > 0
