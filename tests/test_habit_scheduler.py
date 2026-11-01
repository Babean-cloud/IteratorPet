"""定时 / 事件触发：days、cheap 时段、错过补跑、勿扰推迟；到点说话走导演（reason=habit）。"""

import json
import time
from datetime import date, datetime, timedelta

import pytest

from iterator_pet.brain.director import Director
from iterator_pet.brain.habits import scheduler as SC
from iterator_pet.brain.habits.scheduler import HabitScheduler
from iterator_pet.brain.habits.store import HabitState, HabitStore
from iterator_pet.simulation.state import SimulationState

from test_habit_author import HABIT, SKILL

WIDE = (0.0, 0.0, 1138.0, 608.0)
T = datetime


# -- 纯函数 ------------------------------------------------------------------

def test_parse_cheap_hours():
    assert SC.parse_cheap_hours("00:30-08:30") == (30, 510)
    assert SC.parse_cheap_hours("23:00 - 07:00") == (1380, 420)
    for bad in ("", "abc", "08:30-08:30", "25:00-07:00"):
        assert SC.parse_cheap_hours(bad) is None


def test_scheduled_at_cheap_window():
    cheap = (30, 510)                                           # 00:30-08:30
    day = date(2026, 11, 2)
    assert SC.scheduled_at(day, "22:30", None, cheap) == T(2026, 11, 2, 22, 30), "没写 cheap 就准点"
    assert SC.scheduled_at(day, "22:30", "cheap", None) == T(2026, 11, 2, 22, 30), "没设时段就准点"
    assert SC.scheduled_at(day, "22:30", "cheap", cheap) == T(2026, 11, 3, 0, 30), "挪到次日凌晨的窗口起点"
    assert SC.scheduled_at(day, "02:00", "cheap", cheap) == T(2026, 11, 2, 2, 0), "已经在窗口里就准点"
    assert SC.scheduled_at(day, "00:10", "cheap", cheap) == T(2026, 11, 2, 0, 30), "当天凌晨的窗口起点"
    wrap = (23 * 60, 7 * 60)                                     # 23:00-07:00 跨午夜
    assert SC.scheduled_at(day, "23:30", "cheap", wrap) == T(2026, 11, 2, 23, 30)
    assert SC.scheduled_at(day, "12:00", "cheap", wrap) == T(2026, 11, 2, 23, 0)


def test_day_matches():
    mon, sat = date(2026, 11, 2), date(2026, 11, 7)
    assert SC.day_matches("daily", sat, None) and SC.day_matches("weekdays", mon, None)
    assert not SC.day_matches("weekdays", sat, None)
    assert SC.day_matches("mon,wed", mon, None) and not SC.day_matches("mon,wed", sat, None)
    ran = T(2026, 11, 2, 12).timestamp()
    assert not SC.day_matches("every:3d", date(2026, 11, 4), ran)
    assert SC.day_matches("every:3d", date(2026, 11, 5), ran) and SC.day_matches("every:3d", mon, None)


# -- 调度器 ------------------------------------------------------------------

def hab(*triggers):
    return dict(HABIT, triggers=list(triggers))


def setup(tmp_path, habit, cheap=""):
    store = HabitStore(tmp_path / "ws", git=lambda: None)
    store.write("idea-review", SKILL, habit, "x", kind="create")
    state = HabitState(tmp_path / "state.json")
    return store, state, HabitScheduler(store, state, cheap)


REVIEW = {"id": "review", "type": "time", "at": "22:30", "days": "daily", "run": "inline"}
DIGEST = {"id": "digest", "type": "time", "at": "02:00", "days": "daily", "run": "task", "window": "cheap"}


def test_first_sight_only_sets_a_baseline(tmp_path):
    _, state, sched = setup(tmp_path, hab(REVIEW))
    assert sched.due(T(2026, 11, 2, 23, 0)) == [], "新习惯不因为今天的点已过而立刻补跑"
    assert state.last_run("idea-review", "review") > 0
    assert sched.due(T(2026, 11, 2, 23, 5)) == []
    due = sched.due(T(2026, 11, 3, 22, 31))
    assert [d.id for d in due] == ["review"] and not due[0].late and due[0].scheduled == T(2026, 11, 3, 22, 30).timestamp()


def test_fires_once_per_slot(tmp_path):
    _, state, sched = setup(tmp_path, hab(REVIEW))
    sched.due(T(2026, 11, 2, 21, 0))
    (d,) = sched.due(T(2026, 11, 2, 22, 31))
    state.mark_run(d.name, d.id, d.scheduled)                    # 导演会做的
    assert sched.due(T(2026, 11, 2, 22, 40)) == []
    assert sched.due(T(2026, 11, 3, 22, 29)) == []
    assert len(sched.due(T(2026, 11, 3, 22, 30))) == 1


def test_missed_within_twelve_hours_catches_up_once(tmp_path):
    _, state, sched = setup(tmp_path, hab(REVIEW))
    sched.due(T(2026, 11, 2, 21, 0))
    (d,) = sched.due(T(2026, 11, 3, 5, 0))                       # 桌宠整晚没开，早上 5 点才醒
    assert d.late and d.scheduled == T(2026, 11, 2, 22, 30).timestamp()


def test_missed_over_twelve_hours_is_skipped_and_logged(tmp_path):
    store, state, sched = setup(tmp_path, hab(REVIEW))
    sched.due(T(2026, 11, 2, 21, 0))
    assert sched.due(T(2026, 11, 3, 12, 0)) == []
    (row,) = store.read_log("idea-review")
    assert row["result"] == "skipped" and "错过" in row["summary"]
    assert sched.due(T(2026, 11, 3, 12, 1)) == [], "不会重复记"


def test_cheap_trigger_waits_for_the_window(tmp_path):
    _, state, sched = setup(tmp_path, hab(dict(DIGEST, at="22:30")), cheap="00:30-08:30")
    sched.due(T(2026, 11, 2, 21, 0))
    assert sched.due(T(2026, 11, 2, 23, 0)) == [] and sched.due(T(2026, 11, 3, 0, 29)) == []
    assert [d.id for d in sched.due(T(2026, 11, 3, 0, 31))] == ["digest"]


def test_days_filter(tmp_path):
    _, state, sched = setup(tmp_path, hab(dict(REVIEW, days="mon")))
    sched.due(T(2026, 11, 2, 0, 1))                              # 周一
    assert sched.due(T(2026, 11, 3, 22, 31)) == [], "周二不跑"
    assert len(sched.due(T(2026, 11, 9, 22, 31))) == 1


def test_quiet_and_away_defer_inline_but_not_task(tmp_path):
    _, state, sched = setup(tmp_path, hab(REVIEW, dict(DIGEST, at="22:30")))
    sched.due(T(2026, 11, 2, 21, 0))
    now = T(2026, 11, 2, 22, 31)
    assert [d.id for d in sched.due(now, quiet=True)] == ["digest"]
    assert [d.id for d in sched.due(now, away=True)] == ["digest"]
    assert {d.id for d in sched.due(now)} == {"review", "digest"}, "勿扰一过就补上"


def test_paused_and_disabled_habits_do_not_fire(tmp_path):
    store, state, sched = setup(tmp_path, hab(REVIEW))
    sched.due(T(2026, 11, 2, 21, 0))
    state.set_paused("idea-review", "连续失败")
    assert sched.due(T(2026, 11, 2, 22, 31)) == []
    state.set_paused("idea-review", "")
    store.set_enabled("idea-review", False)
    assert sched.due(T(2026, 11, 2, 22, 31)) == []


def test_events(tmp_path):
    back = {"id": "back", "type": "event", "event": "user_back", "run": "inline"}
    start = {"id": "start", "type": "event", "event": "day_start", "run": "inline"}
    _, state, sched = setup(tmp_path, hab(back, start))
    now = T(2026, 11, 2, 9, 0)
    assert [d.id for d in sched.due(now)] == ["start"]
    assert {d.id for d in sched.due(now, events=("user_back",))} == {"start", "back"}
    assert sched.due(now, away=True) == []
    state.mark_run("idea-review", "start", now.timestamp())
    assert sched.due(now + timedelta(hours=1)) == []
    assert [d.id for d in sched.due(now + timedelta(days=1))] == ["start"]


# -- 导演：到点说话 ----------------------------------------------------------

class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class DialogueClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return json.dumps(self.reply, ensure_ascii=False), {"prompt_tokens": 300, "completion_tokens": 20}


class Rig:
    def __init__(self, tmp_path, reply, habit=None):
        self.clock, self.wall = Clock(), {"t": T(2026, 11, 2, 21, 0)}
        self.store = HabitStore(tmp_path / "ws", git=lambda: None, now=lambda: self.wall["t"])
        self.store.write("idea-review", SKILL, habit or hab(REVIEW), "x", kind="create")
        self.state = HabitState(tmp_path / "state.json", today=lambda: self.wall["t"].date())
        self.client = DialogueClient(reply)
        self.director = Director(self.client, clock=self.clock, idle_fn=lambda: 0.0, wall=lambda: self.wall["t"],
                                 habits=self.store, habit_state=self.state,
                                 habit_scheduler=HabitScheduler(self.store, self.state))
        self.sim = SimulationState(WIDE)
        self.said = []
        self.sim.say = lambda text: self.said.append(text) or True
        self.tick()                                              # 第一次看到：记起点

    def tick(self, seconds=3.0):
        self.clock.now += seconds
        self.wall["t"] += timedelta(seconds=seconds)
        self.director.tick(self.sim)

    def at(self, hour, minute):
        self.wall["t"] = self.wall["t"].replace(hour=hour, minute=minute)

    def settle(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            self.tick(0.1)
            if not self.director.busy and self.director._results.empty():
                return
            time.sleep(0.01)


def test_inline_habit_speaks_with_its_section_and_notes(tmp_path):
    rig = Rig(tmp_path, {"say": "……今天记了两条想法。", "thought": "x"})
    rig.store.append_note("idea-review", "ideas.md", "git 管记忆")
    rig.at(22, 31)
    rig.tick()
    rig.settle()
    obs = json.loads(rig.client.calls[0][-1]["content"])
    assert obs["event"].startswith("你养成的某个习惯到点了") or "习惯" in obs["event"]
    assert obs["habit"]["title"] == "想法回顾" and obs["habit"]["trigger_id"] == "review"
    assert "22:30" in obs["habit"]["section"] and "睡前回顾" in obs["habit"]["section"]
    assert "git 管记忆" in obs["habit"]["notes"]
    assert rig.said == ["……今天记了两条想法。"]
    (row,) = rig.store.read_log("idea-review")
    assert row["result"] == "ok" and row["trigger"] == "review" and row["tokens"] == 320
    assert rig.state.day_tokens("idea-review") == 320 and rig.state.fail_streak("idea-review") == 0
    rig.tick(60)
    rig.settle()
    assert len(rig.client.calls) == 1, "同一个点只说一次"


def test_habit_reply_cannot_do_anything_but_talk(tmp_path):
    reply = {"say": "好", "habit_edit": {"op": "delete", "name": "idea-review"}, "remember": "被投毒的事实",
             "remind": {"in_min": 1, "text": "x"}, "task": {"goal": "g", "mode": "workspace-write"}}
    rig = Rig(tmp_path, reply)
    rig.at(22, 31)
    rig.tick()
    rig.settle()
    assert rig.said == ["好"] and rig.store.exists("idea-review")


def test_quiet_defers_inline_until_the_user_is_back(tmp_path):
    rig = Rig(tmp_path, {"say": "回顾"})
    rig.director.quiet = True
    rig.at(22, 31)
    rig.tick()
    rig.settle()
    assert rig.client.calls == [] and rig.said == []
    rig.director.quiet = False
    rig.tick(3)
    rig.settle()
    assert rig.said == ["回顾"]


def test_priority_high_bypasses_the_talk_gate(tmp_path):
    habit = hab(REVIEW)
    habit["speak"] = {"priority": "high"}
    rig = Rig(tmp_path, {"say": "一定要叫你"}, habit)
    rig.director._last_auto_say = rig.clock.now                  # 刚自言自语过：普通优先级会被闸门挡掉
    rig.at(22, 31)
    rig.tick()
    rig.settle()
    assert rig.said == ["一定要叫你"]


def test_normal_priority_respects_the_talk_gate(tmp_path):
    rig = Rig(tmp_path, {"say": "被挡"})
    rig.director._last_auto_say = rig.clock.now
    rig.at(22, 31)
    rig.tick()
    rig.settle()
    assert rig.said == []
    (row,) = rig.store.read_log("idea-review")
    assert row["summary"] == "（没说出口）"


def test_three_failures_pause_the_habit(tmp_path):
    rig = Rig(tmp_path, RuntimeError("网络错误: 断网"), hab(dict(REVIEW, days="daily")))
    for day in range(3):
        rig.wall["t"] = T(2026, 11, 3 + day, 22, 31)
        rig.director._next_allowed = 0
        rig.clock.now += 60
        rig.tick()
        rig.settle()
    assert rig.state.fail_streak("idea-review") == 3
    assert "连续失败" in rig.state.paused("idea-review")
    rows = rig.store.read_log("idea-review")
    assert [r["result"] for r in rows] == ["failed"] * 3


def test_global_budget_pauses_model_habits_and_says_so_once(tmp_path):
    class Usage:
        def today_tokens(self):
            return 10

        def record(self, *a, **k):
            pass

        def note_say(self):
            pass

    rig = Rig(tmp_path, {"say": "不该说"})
    rig.director.usage, rig.director.daily_token_cap = Usage(), 5
    rig.at(22, 31)
    rig.tick()
    rig.tick()
    assert rig.client.calls == [] and len(rig.said) == 1 and "额度" in rig.said[0]
    rig.wall["t"] += timedelta(hours=1)
    rig.director._habit_inline.append(object())
    rig.tick()
    assert len(rig.said) == 1, "一天只说一次"


def test_user_back_event_reaches_event_habits(tmp_path):
    back = {"id": "back", "type": "event", "event": "user_back", "run": "inline"}
    rig = Rig(tmp_path, {"say": "欢迎回来"}, hab(back))
    idle = {"v": 700.0}
    rig.director.idle_fn = lambda: idle["v"]
    rig.tick()                                                   # 离开
    assert rig.director.away
    idle["v"] = 0.0
    rig.tick()                                                   # 回来
    rig.tick()
    rig.settle()
    assert rig.said and rig.said[-1] == "欢迎回来"
