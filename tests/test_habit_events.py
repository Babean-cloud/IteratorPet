"""B1：event 触发补全——Watcher 类事件（窗口 / 进程 / 文件）、task_done / reflect、10 分钟防抖、校验。"""

import json
import time
from datetime import timedelta

import pytest

from iterator_pet.brain.errands import Watcher
from iterator_pet.brain.habits import store as S
from iterator_pet.brain.habits.scheduler import EVENT_DEBOUNCE, HabitScheduler
from iterator_pet.brain.habits.store import HabitError, HabitState, HabitStore

from test_errands import FakeSensors
from test_habit_author import HABIT, SKILL
from test_habit_scheduler import Rig, T, hab


def ev(event, run="inline", **params):
    return {"id": "e", "type": "event", "event": event, "run": run, **params}


# -- 校验 --------------------------------------------------------------------

@pytest.mark.parametrize("trigger", [
    ev("process_exit", name="pytest"), ev("file_change", path="D:/x/out.log"), ev("window_gone", window="终端"),
    ev("title_change", window="终端"), ev("window_appears", window="Chrome"),
    ev("text_appears", window="终端", words=["passed", "failed"]), ev("task_done"), ev("reflect"),
])
def test_valid_events(trigger):
    out = S.validate_habit(hab(trigger))["triggers"][0]
    assert out["event"] == trigger["event"] and all(out[k] == v for k, v in trigger.items() if k in ("name", "path", "window"))


@pytest.mark.parametrize("trigger, message", [
    (ev("process_exit"), "name"), (ev("file_change"), "path"), (ev("title_change"), "window"),
    (ev("text_appears", window="终端"), "words"), (ev("text_appears", window="终端", words=["a"]), "words"),
    (ev("nope"), "只支持"),
])
def test_invalid_events(trigger, message):
    with pytest.raises(HabitError, match=message):
        S.validate_habit(hab(trigger))


# -- 调度 --------------------------------------------------------------------

def sched(tmp_path, trigger, sensors):
    store = HabitStore(tmp_path / "ws", git=lambda: None)
    store.write("idea-review", SKILL, hab(trigger), "x", kind="create")
    state = HabitState(tmp_path / "state.json")
    return store, state, HabitScheduler(store, state, watcher=Watcher(sensors, clock=lambda: 0.0))


def test_process_exit_fires_with_reason_and_debounces(tmp_path):
    eyes = FakeSensors()
    store, state, s = sched(tmp_path, ev("process_exit", name="python"), eyes)
    now = T(2026, 11, 2, 12, 0)
    assert s.due(now) == [], "第一次只取基准"
    eyes.processes["python.exe"] = 1
    (due,) = s.due(now + timedelta(seconds=2))
    assert due.id == "e" and "python" in due.why and due.scheduled == (now + timedelta(seconds=2)).timestamp()
    state.mark_run("idea-review", "e", due.scheduled)                      # 导演会做的
    eyes.processes["python.exe"] = 3
    assert s.due(now + timedelta(seconds=10)) == [], "触发后重取基准"
    eyes.processes["python.exe"] = 1
    assert s.due(now + timedelta(seconds=20)) == [], "10 分钟内不再触发"
    eyes.processes["python.exe"] = 3
    s.due(now + timedelta(seconds=EVENT_DEBOUNCE + 10))
    eyes.processes["python.exe"] = 1
    assert s.due(now + timedelta(seconds=EVENT_DEBOUNCE + 20)), "过了防抖时间又能触发"


def test_file_change_and_quiet_defers_inline(tmp_path):
    eyes = FakeSensors()
    eyes.files["D:/x"] = {"exists": True, "size": 1, "mtime": 1}
    store, state, s = sched(tmp_path, ev("file_change", path="D:/x"), eyes)
    now = T(2026, 11, 2, 12, 0)
    s.due(now)
    eyes.files["D:/x"] = {"exists": True, "size": 2, "mtime": 2}
    assert s.due(now + timedelta(seconds=2), quiet=True) == [], "inline 在勿扰时不给"
    assert len(s.due(now + timedelta(seconds=3))) == 1, "退出勿扰后补上（事件没丢）"
    eyes.files["D:/x"] = {"exists": True, "size": 3, "mtime": 3}
    assert len(s.due(now + timedelta(seconds=4))) == 1


def test_task_run_event_is_not_deferred_when_quiet(tmp_path):
    eyes = FakeSensors()
    store, state, s = sched(tmp_path, ev("process_exit", run="task", name="python"), eyes)
    now = T(2026, 11, 2, 12, 0)
    s.due(now)
    eyes.processes["python.exe"] = 0
    assert len(s.due(now + timedelta(seconds=2), quiet=True, away=True)) == 1


def test_internal_events_and_debounce(tmp_path):
    store, state, s = sched(tmp_path, ev("task_done"), FakeSensors())
    now = T(2026, 11, 2, 12, 0)
    assert s.due(now) == [] and s.due(now, events=("reflect",)) == []
    (due,) = s.due(now, events=("task_done",))
    state.mark_run("idea-review", "e", due.scheduled)
    assert s.due(now + timedelta(seconds=30), events=("task_done",)) == []
    assert s.due(now + timedelta(seconds=EVENT_DEBOUNCE + 1), events=("task_done",))


def test_without_a_watcher_watch_events_are_ignored(tmp_path):
    store = HabitStore(tmp_path / "ws", git=lambda: None)
    store.write("idea-review", SKILL, hab(ev("process_exit", name="python")), "x", kind="create")
    s = HabitScheduler(store, HabitState(tmp_path / "s.json"))
    assert s.due(T(2026, 11, 2, 12, 0)) == []


# -- 导演 --------------------------------------------------------------------

def event_rig(tmp_path, trigger, reply):
    rig = Rig(tmp_path, reply, hab(trigger))
    eyes = FakeSensors()
    rig.director.habit_scheduler.watcher = Watcher(eyes, clock=lambda: 0.0)
    return rig, eyes


def test_director_speaks_on_process_exit_with_the_reason(tmp_path):
    rig, eyes = event_rig(tmp_path, ev("process_exit", name="python"), {"say": "……测试跑完了。"})
    rig.tick()                                   # 基准
    eyes.processes["python.exe"] = 0
    rig.tick()
    rig.settle()
    obs = json.loads(rig.client.calls[0][-1]["content"])
    assert "python" in obs["habit"]["why"] and obs["habit"]["trigger_id"] == "e"
    assert rig.said == ["……测试跑完了。"]


def test_task_done_and_reflect_events_reach_habits(tmp_path):
    rig, _ = event_rig(tmp_path, ev("task_done"), {"say": "任务完了"})
    rig.director.task_finished("好了", True)
    rig.tick()
    rig.settle()
    assert "任务完了" in rig.said
    rig2, _ = event_rig(tmp_path / "b", ev("reflect"), {"say": "反思了"})
    rig2.director._habit_events.add("reflect")
    rig2.tick()
    rig2.settle()
    assert "反思了" in rig2.said
