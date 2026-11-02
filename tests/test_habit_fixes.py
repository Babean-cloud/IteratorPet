"""F 组真机修复：重复创建、排队告知、脚本回退、动态预算和明确命令。"""

import json
from types import SimpleNamespace

import pytest

from iterator_pet.brain.habits import store as S
from iterator_pet.brain.habits.store import HabitStore
from iterator_pet.platform.quick_bar import execute

from test_habit_author import CREATE, HABIT, SKILL, World, reply
from test_habit_revert_date import Clock, stamp


def test_duplicate_create_is_stopped_and_explicit_new_is_allowed(tmp_path):
    w = World(tmp_path, reply())
    w.store.write("idea-review", SKILL, HABIT, "帮我记想法", kind="create")
    notices = []
    w.director.on_habit_notice = lambda title, lines: notices.append((title, lines))
    w.apply(CREATE, user_text="帮我记想法")
    w.settle()
    assert w.client.calls == [] and len(w.store.list()) == 1
    assert w.director._last_habit_change["op"] == "exists"
    assert "要改它吗" in w.said[-1] and notices[-1][0] == "习惯已存在"
    w.apply(CREATE, user_text="另建一个新的，帮我记想法")
    w.settle()
    assert len(w.store.list()) == 2


def test_followup_after_duplicate_routes_to_update(tmp_path):
    w = World(tmp_path, reply())
    w.store.write("idea-review", SKILL, HABIT, "帮我记想法", kind="create")
    w.apply(CREATE, user_text="帮我记想法")
    w.settle()
    w.apply({}, user_text="改成每天睡前回顾")
    w.settle()
    assert len(w.client.calls) == 1
    assert "改成每天睡前回顾" in w.store.requests("idea-review")


def test_habit_notice_waits_until_dialogue_is_free(tmp_path):
    w = World(tmp_path, reply())
    notices = []
    w.director.on_habit_notice = lambda title, lines: notices.append((title, lines))
    w.sim.dialogue.text = "正在说话"
    w.apply(CREATE)
    w.settle()
    assert not w.said and notices[0][0] == "习惯已建好"
    assert len(notices[0][1]) == 3 and str(w.store.root) in notices[0][1][-1]
    w.sim.dialogue.text = ""
    w.director._drain_habit_runs(w.sim)
    assert "建好了" in w.said[-1]


def test_failed_notice_carries_reason(tmp_path):
    bad = dict(HABIT, permissions={"write": "everything"})
    w = World(tmp_path, reply(habit=bad), reply(habit=bad))
    notices = []
    w.director.on_habit_notice = lambda title, lines: notices.append((title, lines))
    w.apply(CREATE)
    w.settle()
    assert "permissions.write" in notices[0][1][0] and "没建成" in w.said[-1]


@pytest.mark.parametrize("git", [True, False])
def test_revert_restores_scripts_and_date_is_a_normal_version(tmp_path, monkeypatch, git):
    if git and S.find_git() is None:
        pytest.skip("本机没有 git")
    clock = Clock()
    store = HabitStore(tmp_path / "ws", git=S.find_git if git else (lambda: None), now=clock)
    config = dict(HABIT, triggers=[{"id": "push", "type": "time", "at": "23:00", "days": "daily",
                                    "run": "script", "script": "scripts/a.ps1"}])
    stamp(clock, 1)
    store.write("idea-review", SKILL, config, "v1", kind="create", scripts={"scripts/a.ps1": "v1"})
    stamp(clock, 3)
    store.write("idea-review", SKILL + "\nv2\n", config, "v2", kind="update",
                scripts={"scripts/a.ps1": "v2", "scripts/b.ps1": "extra"})
    stamp(clock, 5)
    store.write("idea-review", SKILL + "\nv3\n", config, "v3", kind="update", scripts={"scripts/a.ps1": "v3"})
    folder = store.path("idea-review")
    stamp(clock, 6)
    store.revert("idea-review")
    assert "v2" in (folder / "scripts" / "a.ps1").read_text(encoding="utf-8-sig")
    assert (folder / "scripts" / "b.ps1").is_file()
    stamp(clock, 7)
    store.revert("idea-review", to="2026-11-01")
    assert "v1" in (folder / "scripts" / "a.ps1").read_text(encoding="utf-8-sig")
    assert not (folder / "scripts" / "b.ps1").exists()
    stamp(clock, 8)
    store.revert("idea-review")
    assert "v2" in (folder / "scripts" / "a.ps1").read_text(encoding="utf-8-sig")
    if git:
        assert "回到日期：" in store._run_git("log", "--format=%s")


def test_budget_uses_current_defaults_unless_explicit(tmp_path):
    store = HabitStore(tmp_path / "ws", git=lambda: None, default_budget={"day_tokens": 5000})
    store.write("idea-review", SKILL, HABIT, "x", kind="create")
    path = store.path("idea-review") / "habit.json"
    assert "budget" not in json.loads(path.read_text(encoding="utf-8"))
    store.default_budget = S.clean_budget({"day_tokens": 7000})
    assert store.get("idea-review").config["budget"]["day_tokens"] == 7000
    store.set_enabled("idea-review", False)
    assert "budget" not in json.loads(path.read_text(encoding="utf-8"))
    store.write("idea-review", SKILL, dict(HABIT, budget={"run_steps": 5}), "改", kind="update")
    assert json.loads(path.read_text(encoding="utf-8"))["budget"] == {"run_steps": 5}
    assert store.get("idea-review").config["budget"]["day_tokens"] == 7000


def test_explicit_modify_and_delete_commands(tmp_path):
    w = World(tmp_path, reply())
    w.store.write("idea-review", SKILL, HABIT, "原话", kind="create")
    pet = SimpleNamespace(habits=w.store, habit_state=None, director=w.director, simulation=w.sim,
                          set_llm_enabled=lambda _: None)
    ok, msg = execute(pet, "/habit 想法回顾 改成每天 23:00 提醒")
    assert ok and "正在修改" in msg
    w.settle()
    sent = json.loads(w.client.calls[0][1]["content"])
    assert sent["user_said"] == "改成每天 23:00 提醒"
    assert "改成每天 23:00 提醒" in w.store.requests("idea-review")
    ok, msg = execute(pet, "/habit delete 想法回顾")
    assert ok and ".trash" in msg and w.store.list() == []


def test_settings_shows_active_workspace(tmp_path):
    from iterator_pet.config import Config
    from iterator_pet.platform.settings_window import FIELDS, SettingsWindow

    window = SimpleNamespace(config=Config(), habits=SimpleNamespace(root=tmp_path / "active"))
    field = next(field for field in FIELDS if field.key == "workspace_dir")
    assert SettingsWindow._value(SimpleNamespace(window=window), field) == str(tmp_path / "active")
