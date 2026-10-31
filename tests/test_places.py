"""地点簿与别名：只记用户确认过的、本地模糊匹配（含拼音）、task 的 workspace 可以填地点名。"""

from __future__ import annotations

import json
from datetime import datetime

from iterator_pet.brain.chatlog import ChatLog
from iterator_pet.brain.context import Context
from iterator_pet.brain.director import Director
from iterator_pet.brain.memory import Memory
from iterator_pet.brain.places import match, score
from iterator_pet.simulation.state import SimulationState


def test_score_handles_abbreviation_pinyin_and_unrelated():
    assert score("MM", "看看MM里的课件") == 100
    assert score("MM", "看看莫莫的课件") >= 80, "笔误：读音首字母对得上缩写"
    assert score("多媒体", "多美体的课件") >= 80
    assert score("MM", "今天吃什么") < 72 and score("毕设", "毕业设计") < 72


def test_match_returns_top_three_sorted():
    places = {"MM课": {"path": "D:/c/MM"}, "毕设": {"path": "D:/c/bs"}}
    aliases = {"MM": {"meaning": "MM课"}}
    found = match("看看莫莫的课件", places, aliases)
    assert found and found[0]["name"] == "MM" and found[0]["kind"] == "alias" and len(found) <= 3
    assert match("今天吃什么", places, aliases) == []


def test_memory_only_stores_real_folders_and_persists(tmp_path):
    memory = Memory(tmp_path / "m.json")
    folder = tmp_path / "MM"
    folder.mkdir()
    assert not memory.add_place("MM课", str(tmp_path / "不存在")), "模型猜的路径记不进来"
    assert memory.add_place("MM课", str(folder))
    assert memory.add_alias("MM", "MM课")
    assert not memory.add_alias("MM", "MM课"), "重复不算新增"
    memory.save()
    again = Memory(tmp_path / "m.json")
    assert again.places["MM课"]["path"] == str(folder) and again.places["MM课"]["confirmed"]
    assert again.aliases["MM"]["meaning"] == "MM课"
    assert again.resolve_place("MM") == str(folder) and again.resolve_place("mm课") == str(folder)
    assert again.forget_place("mm") and "MM" not in again.aliases
    again.clear()
    assert not again.places


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete(self, messages, **_):
        self.calls.append(messages)
        return json.dumps(self.reply, ensure_ascii=False), {}


def _director(tmp_path, reply, on_task=None):
    now = {"t": 1000.0}
    director = Director(FakeClient(reply), clock=lambda: now["t"], idle_fn=lambda: 0.0,
                        wall=lambda: datetime(2026, 11, 1, 15, 0), memory=Memory(tmp_path / "m.json"),
                        chatlog=ChatLog(tmp_path / "c.jsonl"), context=Context(None),
                        on_task_request=on_task)
    return director, now


def test_candidates_in_observation_but_not_in_history(tmp_path):
    folder = tmp_path / "MM"
    folder.mkdir()
    director, _ = _director(tmp_path, {"say": "你是说 MM 课吗？"})
    director.memory.add_place("MM课", str(folder))
    director.memory.add_alias("MM", "MM课")
    sim = SimulationState((0.0, 0.0, 776.0, 415.0))
    sim.pool_lines = False
    obs = director.observe(sim, "chat", "看看莫莫的课件")
    director._start(sim, "chat", "看看莫莫的课件")
    import time
    for _ in range(100):
        if not director.busy:
            break
        time.sleep(0.02)
    sent = json.loads(director.client.calls[0][-1]["content"])
    assert sent["place_candidates"][0]["name"] == "MM"
    director._drain_results(sim)
    assert "place_candidates" not in json.loads(director.context.turns[0]["content"])


def test_learn_only_on_user_turn_and_task_workspace_resolves(tmp_path):
    folder = tmp_path / "MM"
    folder.mkdir()
    proposals = []
    director, _ = _director(tmp_path, {}, on_task=lambda p, u: proposals.append(p))
    sim = SimulationState((0.0, 0.0, 776.0, 415.0))
    sim.pool_lines = False
    learn = {"learn": {"place": "MM课", "path": str(folder), "alias": "MM", "meaning": "MM课"}}
    director.apply(sim, dict(learn), "screen")
    assert not director.memory.places, "看屏幕时看到的不记"
    director.apply(sim, dict(learn), "chat", user_text="对，MM就是多媒体那门课，在这个文件夹")
    assert director.memory.places["MM课"]["path"] == str(folder) and "MM" in director.memory.aliases
    director.apply(sim, {"task": {"goal": "列大纲", "mode": "read-only", "workspace": "MM"}},
                   "chat", user_text="看看MM里的课件")
    assert proposals[-1]["workspace"] == str(folder), "别名 → 地点 → 路径，由本地换算"
