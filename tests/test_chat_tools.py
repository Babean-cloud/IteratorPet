"""对话主循环里的只读工具（harness 阶段 C）：need → 本地取 → 再问；最多两轮；先开口再说结论。"""

from __future__ import annotations

import json

from iterator_pet.brain.chat_tools import ChatTools, needs
from iterator_pet.brain.director import Director


class Script:
    """按顺序回放回复；记下每次发出去的 messages 和 model。"""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def complete(self, messages, model=None, **_):
        self.calls.append((messages, model))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return json.dumps(reply, ensure_ascii=False), {"total_tokens": 10}


class Eyes:
    level = "ocr"

    def describe(self):
        return {"app": "Code", "title": "main.py", "minutes": 3}

    def ocr_text(self, max_age=None):
        return "def f(:\n    return 1"

    def ocr_age(self):
        return 4.0


def _run(director, reason, tools, first=None):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": json.dumps(first or {"event": reason})}]
    director._run(reason, msgs, "brief", None, None, "那行怎么了" if reason == "chat" else None, tools)
    return director._results.get_nowait()


def test_needs_filters_unknown_and_caps():
    assert needs({"need": "read_screen"}) == [{"tool": "read_screen"}]
    got = needs({"need": [{"tool": "rm_rf"}, "recall", {"tool": "reminders"}, {"tool": "place"}]})
    assert [n["tool"] for n in got] == ["recall", "reminders"]
    assert needs({"need": None}) == [] and needs({}) == []


def test_chat_reads_screen_then_answers_and_says_first_half_now():
    client = Script({"say": "等等，我看看。", "need": [{"tool": "read_screen"}]},
                    {"say": "括号没配对：def f(: 那里。", "thought": "看到了"})
    director = Director(client)
    reason, action, usages, error, *_ = _run(director, "chat", ChatTools(perception=Eyes()))
    assert error is None and len(client.calls) == 2 and len(usages) == 2
    follow = json.loads(client.calls[1][0][-1]["content"])
    assert "def f(:" in follow["tool_results"]["read_screen"] and follow["you_already_said"] == "等等，我看看。"
    assert client.calls[1][0][-2]["role"] == "assistant", "接着第一轮的回复往下问（前缀不变，吃缓存）"
    assert action["say"].startswith("括号") and "need" not in action and action["checked"] == ["read_screen"]
    assert director._interim.get_nowait() == "等等，我看看。"


def test_autonomous_wake_does_not_blurt_the_first_half():
    client = Script({"say": "嗯？", "need": "reminders"}, {"say": None})
    director = Director(client)
    _run(director, "idle", ChatTools())
    assert director._interim.empty(), "没人找它时不先开口（不绕过话量闸门）"
    assert "没有提醒" in json.loads(client.calls[1][0][-1]["content"])["tool_results"]["reminders"]


def test_at_most_two_rounds_and_need_is_dropped():
    client = Script({"say": None, "need": "read_screen"})            # 一直想查
    director = Director(client)
    _, action, usages, *_ = _run(director, "chat", ChatTools(perception=Eyes()))
    assert len(client.calls) == 3 and len(usages) == 3
    assert "最后一次" in json.loads(client.calls[2][0][-1]["content"])["note"]
    assert "need" not in action


def test_look_screen_goes_to_vision_model_once():
    shots = []
    tools = ChatTools(perception=Eyes(), look=lambda: shots.append(1) or b"\xff\xd8jpeg")
    client = Script({"need": "look_screen"}, {"need": "look_screen"}, {"say": "是个折线图。"})
    director = Director(client)
    director.vision_model = "vision-x"
    _, action, *_ = _run(director, "chat", tools)
    messages, model = client.calls[1]
    assert model == "vision-x" and messages[-1]["content"][1]["type"] == "image_url"
    assert shots == [1], "一次被叫醒只截一张"
    assert "已经看过" in json.loads(client.calls[2][0][-1]["content"])["tool_results"]["look_screen"]
    assert all(isinstance(m["content"], str) for m in client.calls[2][0]), "后一轮用普通模型，不带图"
    assert client.calls[2][1] is None and action["say"] == "是个折线图。"


def test_no_vision_means_no_can_look(tmp_path):
    from iterator_pet.simulation.state import SimulationState

    director = Director(Script({"say": None}))
    tools = director._chat_tools(None)
    assert not tools.can_look
    assert "can_look" not in director.observe(SimulationState((0.0, 0.0, 800.0, 600.0)), "idle")


def test_private_window_is_not_read():
    class Private(Eyes):
        def describe(self):
            return {"app": "（隐私窗口）", "title": "", "minutes": 1}

    results, _ = ChatTools(perception=Private()).run([{"tool": "read_screen"}])
    assert "隐私" in results["read_screen"] and "def f" not in results["read_screen"]
