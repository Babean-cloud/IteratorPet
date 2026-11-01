"""「今天别说话」与用量折线的数据。"""

from __future__ import annotations

from datetime import date, datetime

from iterator_pet.brain.director import Director
from iterator_pet.brain.usage import UsageTracker
from iterator_pet.simulation.state import SimulationState


class NoClient:
    def complete(self, *a, **k):
        raise AssertionError("今天别说话：不该主动请求")


def test_muted_today_blocks_autonomous_talk_and_requests_but_not_direct():
    wall = datetime(2026, 11, 2, 15, 0)
    director = Director(NoClient(), clock=lambda: 10_000.0, idle_fn=lambda: 0.0, wall=lambda: wall)
    sim = SimulationState((0.0, 0.0, 800.0, 600.0), warmup=0)
    sim.pool_lines = False
    director.mute_until = datetime(2026, 11, 3).timestamp()
    assert director.muted_today
    assert not director._say(sim, "……你在写什么。", direct=False)
    director.salience = 50.0
    director.tick(sim)                      # 显著度再高也不主动请求
    assert not director.busy
    assert director._say(sim, "……在。", direct=True), "你找它、提醒、任务结果照常"
    director.mute_until = 0.0
    assert not director.muted_today


def test_usage_recent_and_says(tmp_path):
    day = {"d": date(2026, 11, 2)}
    usage = UsageTracker(tmp_path / "u.json", today=lambda: day["d"])
    usage.note_say()
    usage.note_say()
    usage.record({"prompt_tokens": 100, "completion_tokens": 10}, source="dialogue")
    rows = usage.recent(3)
    assert [d for d, _ in rows] == ["2026-10-31", "2026-11-01", "2026-11-02"]
    assert rows[-1][1]["says"] == 2 and rows[0][1]["says"] == 0
    again = UsageTracker(tmp_path / "u.json", today=lambda: day["d"])
    assert again.today_row()["says"] == 2, "存盘再读回来"
