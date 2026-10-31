"""重启后从上次的位置出来，而不是每次都在默认位置。"""

from types import SimpleNamespace

from iterator_pet.platform.pet_window import saved_position
from iterator_pet.simulation.state import SimulationState

WIDE = (0.0, 0.0, 1138.0, 608.0)


def test_starts_where_it_was_left():
    default = SimulationState(WIDE, warmup=0).creature.chunk0
    sim = SimulationState(WIDE, warmup=0, start=(200.0, 180.0))
    assert sim.creature.chunk0 == (200.0, 180.0) and default != (200.0, 180.0)
    assert abs(sim.rail.pose(sim.frame_pos).point[0] - 200.0) < 400, "滑车也停在附近的轨道上"
    for seed in (3, 99):
        roamed = SimulationState(WIDE, seed=seed)
        for _ in range(1600):
            roamed.step(1 / 40)
        left_at = roamed.creature.chunk0                 # 上次退出时站过的地方（一定够得着）
        warmed = SimulationState(WIDE, start=left_at)
        x, y = warmed.creature.chunk0
        assert abs(x - left_at[0]) < 60 and abs(y - left_at[1]) < 60, f"预热之后还在那一带：{left_at} → {(x, y)}"


def test_start_is_clamped_into_the_chamber():
    x, y = SimulationState(WIDE, warmup=0, start=(-500.0, 5000.0)).creature.chunk0
    assert 0 < x < WIDE[2] and 0 < y < WIDE[3]


def test_saved_position_is_screen_ratio():
    cfg = SimpleNamespace(last_position=[0.25, 0.5])
    assert saved_position(cfg, WIDE) == (284.5, 304.0)
    assert saved_position(SimpleNamespace(last_position=[]), WIDE) is None
    assert saved_position(SimpleNamespace(last_position=["x", 1]), WIDE) is None
