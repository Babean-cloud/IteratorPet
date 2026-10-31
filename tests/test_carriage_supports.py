"""The rendered carriage braces must reach their pivots on every rail edge."""
from math import cos, sin, radians, dist
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect
from iterator_pet.rendering import renderer


class Painter:
    def __init__(self):
        self.bars = []

    def save(self): pass
    def restore(self): pass
    def setBrush(self, *args): pass
    def setPen(self, *args): pass
    def drawPath(self, *args): pass
    def translate(self, point): self.origin = (point.x(), point.y())
    def rotate(self, angle): self.angle = radians(angle)

    def drawPixmap(self, rect, *args):
        if rect.height() == 2.0:
            length = rect.width() - 10.0  # decorative overhang past the pivot
            end = (self.origin[0] + cos(self.angle) * length,
                   self.origin[1] + sin(self.angle) * length)
            self.bars.append((self.origin, end))


@pytest.mark.parametrize("direction", [(0, 1), (0, -1), (1, 0), (-1, 0)])
def test_support_bars_reach_their_connecting_pivots(direction, monkeypatch):
    monkeypatch.setattr(renderer, "tinted", lambda *args: SimpleNamespace(rect=lambda: QRect(0, 0, 1, 1)))
    anchor = (200 + direction[0] * 48, 200 + direction[1] * 48)
    painter = Painter()
    renderer.draw_rail(painter, SimpleNamespace(base_point=(200, 200), base_dir=direction, joints=[anchor]))
    assert len(painter.bars) == 4
    for i in (0, 2):
        assert dist(painter.bars[i][1], painter.bars[i + 1][0]) < 1e-6
        assert dist(painter.bars[i + 1][1], anchor) < 1e-6


@pytest.mark.parametrize("joint1", [(230, 250), (290, 180), (150, 125)])
def test_support_anchor_follows_first_arm_at_three_poses(joint1, monkeypatch):
    monkeypatch.setattr(renderer, "tinted", lambda *args: SimpleNamespace(rect=lambda: QRect(0, 0, 1, 1)))
    joint0 = (200, 200)
    state = SimpleNamespace(base_point=joint0, base_dir=(0, 1), joints=[joint0, joint1], body1=joint1)
    elbow, _ = renderer.drawn_elbow(state, 0)
    expected = (joint0[0] + (elbow[0] - joint0[0]) * 0.25,
                joint0[1] + (elbow[1] - joint0[1]) * 0.25)
    painter = Painter()
    renderer.draw_rail(painter, state)
    assert len(painter.bars) == 4
    assert all(dist(painter.bars[i][1], expected) < 1e-6 for i in (1, 3))
