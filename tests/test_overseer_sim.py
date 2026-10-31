import math
import random

from iterator_pet.simulation import overseer as O

BOUNDS = (0.0, 0.0, 1920.0, 1080.0)


def _scene(rng):
    """滑车贴在随机一条边上，手臂从滑车伸向屏幕里的人偶头。"""
    edge = rng.choice(O.EDGES)
    x, y, w, h = BOUNDS
    if edge in ("top", "bottom"):
        carriage = (rng.uniform(100, w - 100), y if edge == "top" else y + h)
    else:
        carriage = (x if edge == "left" else x + w, rng.uniform(100, h - 100))
    inward = O.INWARD[edge]
    head = (carriage[0] + inward[0] * rng.uniform(150, 400), carriage[1] + inward[1] * rng.uniform(150, 400))
    arm = [carriage, ((carriage[0] + head[0]) / 2 + 20, (carriage[1] + head[1]) / 2 - 20), head]
    return carriage, head, [arm]


def test_hole_respects_distances_over_many_seeds():
    for seed in range(200):
        rng = random.Random(seed)
        carriage, head, obstacles = _scene(rng)
        edge, hole, body, inward = O.pick_hole(BOUNDS, carriage, head, obstacles, rng)
        assert math.dist(hole, carriage) >= O.CARRIAGE_CLEAR, seed
        assert math.dist(body, head) >= O.HEAD_CLEAR, seed
        assert O.dist_to_polyline(hole, obstacles[0]) >= O.ARM_CLEAR, seed
        assert O.dist_to_polyline(body, obstacles[0]) >= O.ARM_CLEAR, seed
        assert inward == O.INWARD[edge]


def test_hole_prefers_carriage_edge():
    rng = random.Random(1)
    carriage = (960.0, 0.0)
    edge, *_ = O.pick_hole(BOUNDS, carriage, (960.0, 300.0), [[carriage, (960.0, 300.0)]], rng)
    assert edge == "top"


def test_falls_back_to_adjacent_edge_when_home_edge_blocked():
    # 手臂横贯整条上边：上边放不下，应换到左 / 右
    rng = random.Random(3)
    wall = [(0.0, 60.0), (1920.0, 60.0)]
    edge, hole, body, _ = O.pick_hole(BOUNDS, (960.0, 0.0), (960.0, 400.0), [wall], rng)
    assert edge in ("left", "right")


def test_dist_to_polyline():
    line = [(0.0, 0.0), (10.0, 0.0)]
    assert O.dist_to_polyline((5.0, 3.0), line) == 3.0
    assert O.dist_to_polyline((-4.0, 3.0), line) == 5.0
    assert O.dist_to_polyline((1.0, 1.0), []) == math.inf


def _run(sim, ticks, **kw):
    for _ in range(ticks):
        sim.step(BOUNDS, (960.0, 0.0), (960.0, 300.0), **kw)


def test_state_machine_cycle():
    sim = O.OverseerSim(seed=5)
    _run(sim, 10)
    assert sim.phase == O.HIDDEN and sim.extended == 0.0   # 没人要就不出来
    sim.want(True)
    _run(sim, 1)
    assert sim.phase == O.EMERGING
    _run(sim, O.EMERGE_TICKS)
    assert sim.phase == O.HOVERING and sim.extended == 1.0
    sim.want(False)
    _run(sim, O.IDLE_TICKS - 1)
    assert sim.phase == O.HOVERING                          # 空闲没满就不缩
    _run(sim, 1)
    assert sim.phase == O.RETRACTING
    _run(sim, O.RETRACT_TICKS)
    assert sim.phase == O.HIDDEN and sim.extended == 0.0


def test_wanted_keeps_hovering():
    sim = O.OverseerSim(seed=5)
    sim.want(True)
    _run(sim, O.EMERGE_TICKS + 1 + O.IDLE_TICKS * 2)
    assert sim.phase == O.HOVERING


def test_retract_now_from_midway_is_continuous():
    sim = O.OverseerSim(seed=5)
    sim.want(True)
    _run(sim, 13)
    before = sim.extended
    assert 0.0 < before < 1.0
    sim.retract_now()
    assert sim.phase == O.RETRACTING
    _run(sim, 1)
    assert sim.extended < before                            # 从当前位置往回走，不跳
    assert before - sim.extended < 0.2
    _run(sim, O.RETRACT_TICKS)
    assert sim.phase == O.HIDDEN


def test_wanting_again_while_retracting_re_emerges():
    sim = O.OverseerSim(seed=5)
    sim.want(True)
    _run(sim, O.EMERGE_TICKS + 1)
    sim.retract_now()
    _run(sim, O.RETRACT_TICKS)
    assert sim.phase == O.HIDDEN
    sim.want(True)
    _run(sim, 1)
    assert sim.phase == O.EMERGING
