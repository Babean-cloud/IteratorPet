"""HabitAuthor 和导演里的 habit_edit：创建、校验重试、反问不写文件、防投毒、撤回。"""

import json
import time

import pytest

from iterator_pet.brain.director import Director
from iterator_pet.brain.habits import author as A
from iterator_pet.brain.habits.store import HabitStore
from iterator_pet.simulation.state import SimulationState

WIDE = (0.0, 0.0, 1138.0, 608.0)

SKILL = """---
name: idea-review
description: 用户随口说出自己感兴趣的话题时记下来；每晚回顾。
metadata:
  iteratorpet-title: 想法回顾
---
## 记下想法
追加到 notes/ideas.md。

## 睡前回顾
22:30 左右告诉用户今天记了几条。
"""

HABIT = {"schema": 1,
         "triggers": [{"id": "capture", "type": "chat", "when": "用户随口说出想法", "run": "inline"},
                      {"id": "review", "type": "time", "at": "22:30", "days": "daily", "run": "inline"}]}


def reply(skill=SKILL, habit=HABIT, summary="记想法并每晚回顾", questions=()):
    return json.dumps({"skill_md": skill, "habit_json": habit, "change_summary": summary,
                       "questions": list(questions)}, ensure_ascii=False)


class FakeClient:
    """按脚本回；记下每次收到的 messages，好检查作者看到了什么。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append(json.loads(json.dumps(messages)))
        if not self.replies:
            raise AssertionError("多余的请求")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {"prompt_tokens": 10, "completion_tokens": 5}


# -- author() ----------------------------------------------------------------

def test_author_creates():
    client = FakeClient(reply())
    result = A.author(client, "pebbles", "帮我记想法", None, name="")
    assert result.ok and result.name == "idea-review" and result.change_summary == "记想法并每晚回顾"
    assert result.habit_json["budget"]["run_steps"] == 20 and len(result.usages) == 1


def test_author_input_is_only_the_users_words_and_files():
    """H9：给作者的只有原话、原话历史、现有文件、格式说明——没有屏幕、网页、notes。"""
    client = FakeClient(reply())
    existing = {"name": "idea-review", "skill_md": SKILL, "habit_json": HABIT, "requests": "## 2026-11-02 21:14 · 创建\n旧原话"}
    A.author(client, "pebbles", "加个联网", existing)
    system, user = client.calls[0]
    assert system["role"] == "system" and user["role"] == "user"
    payload = json.loads(user["content"])
    assert set(payload) == {"user_said", "request_history", "existing_skill_md", "existing_habit_json", "task"}
    assert payload["user_said"] == "加个联网" and "旧原话" in payload["request_history"]


def test_author_renames_to_a_free_name():
    client = FakeClient(reply())
    result = A.author(client, "pebbles", "x", None, name="", taken={"idea-review"})
    assert result.ok and result.name == "idea-review-2"
    assert "name: idea-review-2" in result.skill_md


def test_author_update_keeps_name():
    other = SKILL.replace("name: idea-review", "name: something-else")
    result = A.author(FakeClient(reply(other)), "pebbles", "x", {"name": "idea-review", "skill_md": SKILL, "habit_json": HABIT, "requests": ""})
    assert result.ok and result.name == "idea-review" and "name: idea-review\n" in result.skill_md


def test_author_retries_once_with_the_error_then_succeeds():
    bad = dict(HABIT, permissions={"write": "everything"})
    client = FakeClient(reply(habit=bad), reply())
    result = A.author(client, "pebbles", "x", None)
    assert result.ok and len(client.calls) == 2 and len(result.usages) == 2
    assert "permissions.write" in client.calls[1][-1]["content"], "校验错误要原样回传给模型"


def test_author_gives_up_after_one_retry():
    bad = dict(HABIT, permissions={"write": "everything"})
    client = FakeClient(reply(habit=bad), reply(habit=bad))
    result = A.author(client, "pebbles", "x", None)
    assert not result.ok and "permissions.write" in result.error and len(client.calls) == 2


def test_author_non_json_and_network_error():
    assert not A.author(FakeClient("随便聊聊", "还是随便聊聊"), "pebbles", "x", None).ok
    result = A.author(FakeClient(RuntimeError("网络错误")), "pebbles", "x", None)
    assert not result.ok and "网络错误" in result.error


def test_author_question():
    result = A.author(FakeClient(reply(questions=["每天几点提醒？"])), "pebbles", "x", None)
    assert result.ok and result.questions == ["每天几点提醒？"] and not result.skill_md


# -- 导演 --------------------------------------------------------------------

class World:
    def __init__(self, tmp_path, *replies):
        self.client = FakeClient(*replies)
        self.store = HabitStore(tmp_path / "ws", git=lambda: None)
        self.director = Director(self.client, idle_fn=lambda: 0.0, habits=self.store)
        self.sim = SimulationState(WIDE)
        self.said = []
        self.sim.say = lambda text: self.said.append(text) or True

    def apply(self, action, reason="chat", user_text="帮我记想法"):
        self.director.apply(self.sim, action, reason, user_text=user_text if reason == "chat" else None)

    def settle(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            self.director._drain_habits(self.sim)
            if not self.director._habit_busy and self.director._habit_results.empty():
                break
            time.sleep(0.01)


CREATE = {"habit_edit": {"op": "create", "brief": "记想法"}}


def test_create_via_chat(tmp_path):
    w = World(tmp_path, reply())
    w.apply(CREATE)
    w.settle()
    habit = w.store.get("idea-review")
    assert habit.title == "想法回顾" and "帮我记想法" in w.store.requests("idea-review")
    assert len(w.said) == 1 and "建好了" in w.said[0] and "改回去" in w.said[0]


def test_habit_edit_dropped_when_not_from_the_user(tmp_path):
    """§10 第 7 条：屏幕上的话把它叫醒（自主，非 chat），回复里的 habit_edit 被丢弃。"""
    w = World(tmp_path)
    for reason in ("window", "screen", "idle", "agent", "task_done", "reflect"):
        w.apply(CREATE, reason=reason)
    w.apply(CREATE, reason="chat", user_text=None)       # 有 chat 原因但没有用户原话
    w.settle()
    assert w.client.calls == [] and w.store.list() == [] and not (tmp_path / "ws").exists()


def test_failure_says_so_and_writes_nothing(tmp_path):
    bad = dict(HABIT, permissions={"write": "everything"})
    w = World(tmp_path, reply(habit=bad), reply(habit=bad))
    w.apply(CREATE)
    w.settle()
    assert w.store.list() == [] and len(w.said) == 1 and "没弄成" in w.said[0]


def test_question_writes_nothing_then_answer_is_merged(tmp_path):
    w = World(tmp_path, reply(questions=["每天几点提醒？"]), reply())
    w.apply(CREATE, user_text="每天提醒我喝水")
    w.settle()
    assert w.said == ["每天几点提醒？"] and w.store.list() == []
    w.apply(CREATE, user_text="十点")
    w.settle()
    sent = json.loads(w.client.calls[1][1]["content"])["user_said"]
    assert "每天提醒我喝水" in sent and "十点" in sent
    assert w.store.list(), "回答之后才写"


def test_update_revert_pause_delete(tmp_path):
    v2 = SKILL.replace("追加到 notes/ideas.md。", "追加到 notes/ideas.md。\n\n## 备资料\n上网搜。")
    w = World(tmp_path, reply(), reply(v2, dict(HABIT, permissions={"web": True}), "加了联网备资料"))
    w.apply(CREATE)
    w.settle()
    w.said.clear()
    w.apply({"habit_edit": {"op": "update", "name": "idea-review", "brief": "联网"}}, user_text="收到关键词你上网备资料")
    w.settle()
    assert w.store.get("idea-review").config["permissions"]["web"] is True and "改好了" in w.said[0]
    sent = json.loads(w.client.calls[1][1]["content"])
    assert "帮我记想法" in sent["request_history"], "改的时候作者看得到之前的全部原话"
    w.apply({"habit_edit": {"op": "revert", "name": "idea-review"}}, user_text="改回去")
    assert w.store.get("idea-review").config["permissions"]["web"] is False and "改回去了" in w.said[-1]
    w.apply({"habit_edit": {"op": "pause", "name": "想法回顾"}}, user_text="先停了")
    assert not w.store.get("idea-review").enabled
    w.apply({"habit_edit": {"op": "resume"}}, user_text="恢复")                # 只有一个习惯时可以不写名字
    assert w.store.get("idea-review").enabled
    w.apply({"habit_edit": {"op": "delete", "name": "idea-review"}}, user_text="删了吧")
    assert w.store.list() == [] and (tmp_path / "ws" / ".trash").is_dir()


def test_unknown_habit_is_reported(tmp_path):
    w = World(tmp_path)
    w.apply({"habit_edit": {"op": "update", "name": "nope"}})
    w.apply({"habit_edit": {"op": "revert", "name": "nope"}})
    assert len(w.said) == 2 and all("习惯" in s for s in w.said) and w.client.calls == []


def test_suggest_only_after_a_task(tmp_path):
    w = World(tmp_path)
    w.apply({"habit_suggest": "这种整理以后要不要做成习惯？"}, reason="task_done")
    w.apply({"habit_suggest": "这种整理以后要不要做成习惯？"}, reason="window")
    assert w.said == ["这种整理以后要不要做成习惯？"]


def test_without_a_store_habit_fields_are_ignored():
    director = Director(FakeClient(), idle_fn=lambda: 0.0)
    sim = SimulationState(WIDE)
    director.apply(sim, CREATE, "chat", user_text="x")
    assert director.habits is None
