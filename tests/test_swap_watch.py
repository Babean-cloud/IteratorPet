"""换角色动画、开场白、递珍珠的接球圈。（盯屏幕换成了心事，见 test_errands.py）"""

from __future__ import annotations

import json
import time

from iterator_pet.brain.director import Director
from iterator_pet.brain.reminders import Reminders
from iterator_pet.platform.fixed_loop import GAME_DT
from iterator_pet.rendering.hit import hit_test, in_drop_zone
from iterator_pet.simulation import constants as C
from iterator_pet.simulation.appearance import CHARACTERS
from iterator_pet.simulation.state import SimulationState

WIDE = (0.0, 0.0, 1138.0, 608.0)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, messages, **_):
        self.calls.append(messages)
        return json.dumps(self.reply, ensure_ascii=False), {}


def _settle(director, sim):
    deadline = time.time() + 2
    while director.busy and time.time() < deadline:
        time.sleep(0.005)
    director.tick(sim)


# -- 换角色动画 ------------------------------------------------------------------------
def test_swap_swings_out_offscreen_then_back_with_new_look():
    for seed in (1234, 7, 99):
        sim = SimulationState(WIDE, seed=seed)
        sim.pool_lines = False
        for _ in range(120):
            sim.step(GAME_DT)
        home = sim.creature.chunk0
        assert sim.begin_swap() and not sim.begin_swap(), "动画中不能再开一次"
        assert not sim.begin_drag("body", sim.creature.chunk0), "动画中抓不住"
        ticks = 0
        while not sim.swap_ready:
            sim.step(GAME_DT)
            ticks += 1
            assert sim.interpolate(1.0).halo_sparks == [], "电光不会横贯全屏"
        assert ticks < C.SWAP_OUT_MAX_TICKS, f"seed {seed}: 自己荡出去的，不是超时"
        assert sim._offscreen(sim.creature.head, C.SWAP_CLEAR_PX), "头连光环都看不见了"
        assert sim.appearance.key == "pebbles"
        sim.swap_back(CHARACTERS["past_moon"])
        while sim.swap_phase is not None:
            sim.step(GAME_DT)
        assert sim.appearance.key == "past_moon"
        assert abs(sim.creature.chunk0[0] - home[0]) < 40 and abs(sim.creature.chunk0[1] - home[1]) < 40
        assert sim.pending_events == ["arrive"], "回来了，交给导演说开场白"


def test_swap_without_ai_greets_from_line_pool():
    sim = SimulationState(WIDE)
    sim.begin_swap()
    while not sim.swap_ready:
        sim.step(GAME_DT)
    sim.swap_back(CHARACTERS["past_moon"])
    while sim.swap_phase is not None:
        sim.step(GAME_DT)
    assert sim.dialogue.text in C.ARRIVE_LINES["past_moon"]


def test_arrive_event_asks_for_an_opening_line(tmp_path):
    clock = Clock()
    director = Director(FakeClient({"say": "……又是你。"}), clock=clock, idle_fn=lambda: 0.0)
    sim = SimulationState(WIDE)
    sim.pool_lines = False
    sim.pending_events.append("arrive")
    clock.now += 7
    director.tick(sim)
    _settle(director, sim)
    obs = json.loads(director.client.calls[0][-1]["content"])
    assert "开场白" in obs["event"]
    assert sim.dialogue.text == "……又是你。", "开场白不受话量闸门限制"


def test_reminder_file_from_older_versions_still_loads(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps([{"due": 2e9, "text": "喝水"}]), encoding="utf-8")
    assert Reminders(path).items[0]["text"] == "喝水"
    path.write_text(json.dumps({"timed": [{"due": 2e9, "text": "开会"}],
                                "watches": [{"id": "x", "expires": 2e9}]}), encoding="utf-8")
    assert Reminders(path).items[0]["text"] == "开会"


# -- 接球圈 --------------------------------------------------------------------------
def test_drop_zone_is_much_bigger_than_grab_zone():
    sim = SimulationState(WIDE)
    state = sim.interpolate(1.0)
    x, y = state.body0
    near_robe = (x + 30.0, y + 25.0)               # 袍子上，抓不住的地方
    assert hit_test(state, near_robe, 1.0) is None
    assert in_drop_zone(state, near_robe, 1.0)
    assert not in_drop_zone(state, (x + 300.0, y), 1.0)
