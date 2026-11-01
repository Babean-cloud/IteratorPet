"""习惯目录、chat 触发的“记一笔”、need 工具 habit。"""

import json
from datetime import datetime

from iterator_pet.brain.chat_tools import ChatTools, needs
from iterator_pet.brain.director import Director
from iterator_pet.brain.habits.store import HabitState, HabitStore
from iterator_pet.simulation.state import SimulationState

from test_habit_author import HABIT, SKILL, FakeClient

WIDE = (0.0, 0.0, 1138.0, 608.0)


def make(tmp_path):
    store = HabitStore(tmp_path / "ws", git=lambda: None, now=lambda: datetime(2026, 11, 2, 21, 14))
    store.write("idea-review", SKILL, HABIT, "帮我记想法", kind="create")
    state = HabitState(tmp_path / "state.json")
    director = Director(FakeClient(), idle_fn=lambda: 0.0, habits=store, habit_state=state,
                        wall=lambda: datetime(2026, 11, 2, 21, 14))
    sim = SimulationState(WIDE)
    sim.say = lambda text: True
    return store, state, director, sim


USE = {"habit_use": {"name": "idea-review", "trigger": "capture", "input": "桌宠的记忆能不能用 git 管"}}


def test_use_appends_to_notes_without_a_request(tmp_path):
    store, state, director, sim = make(tmp_path)
    director.apply(sim, USE, "chat", user_text="突然想到，桌宠的记忆能不能用 git 管")
    director.apply(sim, USE, "chat", user_text="x")
    note = (tmp_path / "ws" / "habits" / "idea-review" / "notes" / "ideas.md").read_text(encoding="utf-8")
    assert note.count("## 2026-11-02") == 1 and note.count("- 21:14 桌宠的记忆能不能用 git 管") == 2
    log = store.read_log("idea-review")
    assert len(log) == 2 and log[0]["trigger"] == "capture" and log[0]["files"] == ["notes/ideas.md"]
    assert state.last_run("idea-review", "capture") > 0
    assert director.client.calls == [], "不额外请求模型"


def test_use_defaults_to_inbox_and_falls_back_to_user_text(tmp_path):
    store, _, director, sim = make(tmp_path)
    store.write("idea-review", SKILL.replace("notes/ideas.md", "笔记本"), HABIT, "", kind="update")
    director.apply(sim, {"habit_use": {"name": "idea-review", "trigger": "capture"}}, "chat", user_text="原话本身")
    assert "原话本身" in (tmp_path / "ws" / "habits" / "idea-review" / "notes" / "inbox.md").read_text(encoding="utf-8")


def test_use_is_ignored_when_it_does_not_fit(tmp_path):
    store, _, director, sim = make(tmp_path)
    notes = tmp_path / "ws" / "habits" / "idea-review" / "notes"
    director.apply(sim, USE, "window")                                             # 不是用户亲口说的
    director.apply(sim, {"habit_use": {"name": "nope", "trigger": "capture"}}, "chat", user_text="x")
    director.apply(sim, {"habit_use": {"name": "idea-review", "trigger": "review"}}, "chat", user_text="x")   # time 触发
    store.set_enabled("idea-review", False)
    director.apply(sim, USE, "chat", user_text="x")                                # 停了的习惯
    assert not (notes / "ideas.md").exists()


def test_note_filename_cannot_escape_notes(tmp_path):
    store, *_ = make(tmp_path)
    import pytest
    from iterator_pet.brain.habits.store import HabitError
    for bad in ("../SKILL.md", "a/b.md", "x.txt", ""):
        with pytest.raises(HabitError):
            store.append_note("idea-review", bad, "x")


def test_catalogue_lists_enabled_habits_and_caches(tmp_path):
    store, *_ = make(tmp_path)
    text = store.catalogue()
    assert text.startswith("【习惯目录】") and "idea-review｜想法回顾｜" in text
    assert "chat 触发 capture：用户随口说出想法" in text and "定时 review：daily 22:30" in text
    assert store.catalogue() is text, "文件没动就不重读"
    store.set_enabled("idea-review", False)
    assert store.catalogue() == ""


def test_catalogue_goes_into_the_memory_message(tmp_path):
    store, _, director, sim = make(tmp_path)
    captured = {}
    director._run = lambda reason, messages, *a, **k: captured.update(messages=messages)
    director._start(sim, "idle", None)
    systems = [m["content"] for m in captured["messages"] if m["role"] == "system"]
    assert len(systems) == 2 and "【习惯目录】" in systems[1] and "idea-review" in systems[1]
    assert "idea-review" not in systems[0], "第一条 system（人设 + 格式）不变"


def test_habit_tool(tmp_path):
    store, *_ = make(tmp_path)
    assert needs({"need": {"tool": "habit", "name": "idea-review"}})
    tools = ChatTools(habits=store)
    results, _ = tools.run([{"tool": "habit", "name": "想法回顾"}])
    out = results["habit"]
    assert "## 记下想法" in out and '"schema": 1' in out and "帮我记想法" in out
    assert tools.run([{"tool": "habit", "name": "nope"}])[0]["habit"] == "没有这个习惯。"
    assert ChatTools().run([{"tool": "habit", "name": "x"}])[0]["habit"] == "没有习惯可查。"
