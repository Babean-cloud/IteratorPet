"""第四轮：角色各自的上下文、反思存盘、对话窗口 / 摘要 / 翻旧账、自带的委托任务执行器。

不联网：导演用假 client，执行器用假 client + 假 shell。
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from iterator_pet.brain.chatlog import ChatLog
from iterator_pet.brain.context import TURNS_KEEP, TURNS_MAX, Context
from iterator_pet.brain.director import AUTO_GAP, CHAT_WINDOW, FOLD_BATCH, REFLECT_FULL, Director
from iterator_pet.brain.executor import (
    AgentRunner, OutsideWorkspace, command_warnings, resolve_inside, tool_specs,
)
from iterator_pet.brain.memory import Memory
from iterator_pet.brain.profiles import character_dir, migrate_legacy
from iterator_pet.brain.prompt import system_prompt
from iterator_pet.brain.usage import UsageTracker
from iterator_pet.simulation.state import SimulationState


class FakeClient:
    def __init__(self, reply: dict):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def complete(self, messages, **_kwargs):
        self.calls.append(messages)
        return json.dumps(self.reply, ensure_ascii=False), {}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _sim() -> SimulationState:
    sim = SimulationState((0.0, 0.0, 776.0, 415.0))
    sim.pool_lines = False
    return sim


def _brain(folder, reply, clock=None, **kwargs) -> Director:
    return Director(FakeClient(reply), clock=clock or Clock(), idle_fn=lambda: 0.0,
                    wall=lambda: datetime(2026, 10, 30, 14, 0),
                    memory=Memory(folder / "memory.json"),
                    chatlog=ChatLog(folder / "chat.jsonl"),
                    context=Context(folder / "context.json"),
                    usage=UsageTracker(folder / "usage.json"), **kwargs)


def _settle(director, sim, timeout=2.0):
    deadline = time.time() + timeout
    while director.busy and time.time() < deadline:
        time.sleep(0.005)
    director.tick(sim)


# -- 角色分开 ---------------------------------------------------------------------
def test_legacy_memory_moves_to_pebbles_without_overwriting(tmp_path):
    (tmp_path / "memory.json").write_text('{"profile": ["旧的"]}', encoding="utf-8")
    (tmp_path / "chat.jsonl").write_text('{"ts": 1, "who": "user", "text": "hi"}\n', encoding="utf-8")
    assert migrate_legacy(tmp_path) == ["memory.json", "chat.jsonl"]
    pebbles = character_dir("pebbles", tmp_path)
    assert Memory(pebbles / "memory.json").profile == ["旧的"]
    assert not (tmp_path / "memory.json").exists()
    assert not (character_dir("past_moon", tmp_path) / "memory.json").exists()
    (tmp_path / "memory.json").write_text('{"profile": ["更旧的"]}', encoding="utf-8")
    assert migrate_legacy(tmp_path) == [], "已经有新数据就不覆盖"
    assert Memory(pebbles / "memory.json").profile == ["旧的"]


def test_personas_know_each_other_but_not_each_others_memory():
    pebbles, moon = system_prompt("pebbles"), system_prompt("past_moon")
    assert "仰望皓月" in pebbles and "不知道" in pebbles
    assert "五块卵石" in moon and "不知道" in moon
    assert pebbles != moon


def test_each_character_has_its_own_context(tmp_path):
    sim = _sim()
    a = _brain(character_dir("pebbles", tmp_path), {"say": "……又是你。", "remember": "叫小林"})
    a.say_to("我叫小林")
    a.tick(sim)
    _settle(a, sim)
    a.save()
    b = _brain(character_dir("past_moon", tmp_path), {"say": "你好呀。"})
    sim.cancel_dialogue()
    b.say_to("你好")
    b.tick(sim)
    _settle(b, sim)
    memory_block = b.client.calls[0][1]["content"]
    assert "小林" not in memory_block, "皓月看不到卵石的记忆和对话"
    assert len(b.context.turns) == 2 and b.memory.profile == []
    again = _brain(character_dir("pebbles", tmp_path), {"say": "嗯。"})
    assert again.memory.profile == ["叫小林"]
    assert len(again.context.turns) == 2, "重启后最近几轮还在"


# -- 反思 ---------------------------------------------------------------------------
def test_reflection_progress_survives_restart(tmp_path):
    folder = tmp_path / "c"
    clock = Clock()
    director = _brain(folder, {"diary": "写了一篇。"}, clock=clock)
    sim = _sim()
    director.planner.load([{"do": "wait", "s": 3000}])
    for i in range(REFLECT_FULL - 1):
        director.log(f"事件{i}")
    for _ in range(30):                        # 在场时间按 tick 累计
        clock.now += 1.0
        director.tick(sim)
    director.save()
    assert not director.client.calls

    clock2 = Clock()
    again = _brain(folder, {"diary": "写了一篇。", "goal": "看着这个人"}, clock=clock2)
    assert again.context.since_reflect == REFLECT_FULL - 1
    assert again.context.active_seconds >= 29
    again.planner.load([{"do": "wait", "s": 3000}])
    again.log("最后一件")                       # 攒满
    clock2.now += AUTO_GAP + 1
    again.tick(sim)
    _settle(again, sim)
    obs = json.loads(again.client.calls[0][-1]["content"])
    assert "反思" in obs["event"] and len(obs["activity"]) == 40
    assert again.memory.diary[-1]["text"] == "写了一篇。"
    assert again.reflections == 1 and again.context.last_reflect > 0
    assert Context(folder / "context.json").reflections == 1


def test_reflect_now_ignores_budget_and_gap(tmp_path):
    director = _brain(tmp_path, {"diary": "被叫来反思。"}, daily_token_cap=1)
    director.usage.record({"prompt_tokens": 50, "completion_tokens": 0})
    assert director.over_budget
    sim = _sim()
    director.reflect_now()
    director.tick(sim)
    _settle(director, sim)
    assert "反思" in json.loads(director.client.calls[0][-1]["content"])["event"]
    assert not director.status()["reflect_pending"]


# -- 不忘事 ------------------------------------------------------------------------
def test_chat_survives_many_autonomous_turns(tmp_path):
    clock = Clock()
    director = _brain(tmp_path, {"say": None, "plan": None}, clock=clock)
    sim = _sim()
    director.chatlog.add("user", "我下周三要考试")
    director.chatlog.add("pet", "……那就别熬夜。")
    for _ in range(25):                        # 一大串自主请求
        clock.now += 300
        director.bump("drag")
        director.tick(sim)
        _settle(director, sim)
    last = director.client.calls[-1]
    assert "下周三要考试" in last[1]["content"], "对话在记忆消息里，自主请求挤不掉"
    assert len(last) <= 2 + TURNS_MAX * 2 + 1


def test_turns_only_append_then_trim_in_batch(tmp_path):
    """缓存：turns 涨到上限才一次砍回，两次截断之间每次请求的前缀都是上一次的延伸。"""
    ctx = Context(tmp_path / "c.json")
    for i in range(TURNS_MAX):
        ctx.add_turn(f"q{i}", f"a{i}")
    assert len(ctx.turns) == TURNS_MAX * 2
    before = list(ctx.turns)
    ctx.add_turn("q-new", "a-new")                 # 超限：一次砍回 TURNS_KEEP 轮
    assert len(ctx.turns) == TURNS_KEEP * 2
    assert list(ctx.turns)[-2:] == [{"role": "user", "content": "q-new"},
                                    {"role": "assistant", "content": "a-new"}]
    ctx2 = Context(tmp_path / "c2.json")
    for i in range(5):
        ctx2.add_turn(f"q{i}", f"a{i}")
        snapshot = list(ctx2.turns)
        ctx2.add_turn("x", "y")
        assert list(ctx2.turns)[:len(snapshot)] == snapshot, "未到上限时只追加，前缀不变"
        ctx2.turns.pop(); ctx2.turns.pop()
    assert before[0]["content"] == "q0"


def test_memory_block_is_static_across_affinity_changes(tmp_path):
    """好感度数值每次都变：必须在观察里，不能在记忆块里，否则每次改一下，后面的缓存全废。"""
    sim = _sim()
    clock = Clock()
    director = _brain(tmp_path, {"say": None, "affinity": 0.1}, clock=clock)
    blocks = []
    for _ in range(2):
        director.bump("drag")
        clock.now += 300
        director.tick(sim)
        _settle(director, sim)
        blocks.append(director.client.calls[-1][1]["content"])
    assert blocks[0] == blocks[1] and "好感度" not in blocks[0]
    assert "affinity" in json.loads(director.client.calls[-1][-1]["content"])


def test_old_dialogue_folds_into_summary(tmp_path):
    log = ChatLog(tmp_path / "chat.jsonl", now=iter(range(1, 10_000)).__next__)
    for i in range(CHAT_WINDOW + FOLD_BATCH + 5):
        log.add("user" if i % 2 == 0 else "pet", f"第{i}句")
    clock = Clock()
    director = _brain(tmp_path, {"say": "嗯。", "summary": "这个人说了很多句，从第0句到第14句。"},
                      clock=clock)
    director.chatlog = log
    sim = _sim()
    director.bump("drag")
    clock.now += 10
    director.tick(sim)
    _settle(director, sim)
    obs = json.loads(director.client.calls[0][-1]["content"])
    assert len(obs["fold"]) == FOLD_BATCH + 5 and "第0句" in obs["fold"][0]
    assert director.memory.summary.startswith("这个人说了很多句")
    assert director.memory.summarized_ts == log.dialogue()[FOLD_BATCH + 4]["ts"]
    director.bump("drag")
    clock.now += 10
    director.tick(sim)
    _settle(director, sim)
    second = director.client.calls[1]
    assert "fold" not in json.loads(second[-1]["content"]), "并过的不再并"
    assert "最近经历" in second[1]["content"]


def test_recall_finds_old_topics(tmp_path):
    stamps = iter(range(1, 10_000))
    log = ChatLog(tmp_path / "chat.jsonl", now=stamps.__next__)
    log.add("user", "我养了一只橘猫叫煤球")
    log.add("pet", "……随你。")
    for i in range(CHAT_WINDOW + 5):
        log.add("user", f"闲聊{i}")
    clock = Clock()
    director = _brain(tmp_path, {"say": "记得。"}, clock=clock)
    director.chatlog = log
    director.memory.summarized_ts = 1e9          # 不测摘要
    sim = _sim()
    director.say_to("你还记得煤球吗，那只橘猫")
    director.tick(sim)
    _settle(director, sim)
    obs = json.loads(director.client.calls[0][-1]["content"])
    assert obs["recall"] and "煤球" in obs["recall"][0] and "随你" in obs["recall"][0]
    brief = json.loads(director.context.turns[0]["content"])
    assert "recall" not in brief, "翻出来的旧账不进历史"


# -- 执行器 --------------------------------------------------------------------------
def test_resolve_inside_blocks_escapes(tmp_path):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    assert resolve_inside(root, "sub/a.txt") == (root / "sub" / "a.txt").resolve()
    for bad in ("../x.txt", str(tmp_path / "other.txt"), "sub/../../x"):
        with pytest.raises(OutsideWorkspace):
            resolve_inside(root, bad)


def test_command_warnings_flag_dangerous_forms():
    assert "会删除文件" in command_warnings("Remove-Item -Recurse .\\build")
    assert "会联网" in command_warnings("iwr https://example.com -OutFile a")
    assert "涉及任务文件夹以外的路径" in command_warnings("Get-ChildItem C:\\Users")
    assert command_warnings("Get-ChildItem | Measure-Object") == []


class ToolClient:
    """按剧本一轮轮吐 tool_calls。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def complete_tools(self, messages, tools, **_):
        self.seen.append([dict(m) for m in messages])
        self.tools = tools
        calls = self.script.pop(0)
        if isinstance(calls, str):
            return {"role": "assistant", "content": calls}, {"prompt_tokens": 10}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
            for i, (name, args) in enumerate(calls)]}, {"prompt_tokens": 10}


def _wait(cond, limit=5.0):
    deadline = time.time() + limit
    while not cond() and time.time() < deadline:
        time.sleep(0.01)
    return cond()


def test_agent_writes_runs_approved_commands_and_finishes(tmp_path):
    ran = []
    client = ToolClient([
        [("list_dir", {"path": "."})],
        [("write_file", {"path": "out/notes.txt", "content": "你好"})],
        [("read_file", {"path": "../secret.txt"})],
        [("run_command", {"command": "python count.py", "why": "看看有什么"})],
        [("run_command", {"command": "Remove-Item notes.txt", "why": "删掉"})],
        [("finish", {"summary": "写好了 out/notes.txt"})],
    ])
    runner = AgentRunner(lambda: client,
                         shell_fn=lambda cmd, cwd, timeout, cancelled: (ran.append(cmd), (0, "ok"))[1])
    task = runner.start("写个笔记", "workspace-write", str(tmp_path / "ws"))
    assert _wait(lambda: task.approval is not None)
    assert task.approval.command == "python count.py" and task.approval_why == "看看有什么"
    runner.answer(True)
    assert _wait(lambda: task.approval is not None and task.approval.command.startswith("Remove"))
    assert "会删除文件" in task.approval.warnings
    runner.answer(False)
    assert _wait(lambda: task.finished)
    assert task.state == "done" and task.result == "写好了 out/notes.txt"
    assert (tmp_path / "ws" / "out" / "notes.txt").read_text(encoding="utf-8") == "你好"
    assert ran == ["python count.py"], "被拒绝的命令不许跑"
    assert any("拒绝" in note for note in task.log) and any("运行" in note for note in task.log)
    tool_results = [m["content"] for m in client.seen[-1] if m["role"] == "tool"]
    assert any(r.startswith("拒绝") for r in tool_results), "越界读被拒"
    assert len(task.usage) == 6


def test_read_only_agent_has_no_write_tool(tmp_path):
    names = {spec["function"]["name"] for spec in tool_specs("read-only")}
    assert "write_file" not in names and "run_command" in names
    client = ToolClient([[("write_file", {"path": "a.txt", "content": "x"})], "做不到。"])
    runner = AgentRunner(lambda: client)
    task = runner.start("写点东西", "read-only", str(tmp_path))
    assert _wait(lambda: task.finished)
    assert not (tmp_path / "a.txt").exists()
    assert task.result == "做不到。"
    with pytest.raises(ValueError):
        runner.start("x", "danger-full-access", str(tmp_path))


def test_cancel_while_waiting_for_approval(tmp_path):
    client = ToolClient([[("run_command", {"command": "python check.py", "why": "看看"})]])
    runner = AgentRunner(lambda: client, shell_fn=lambda *a: pytest.fail("不该运行"))
    task = runner.start("看看", "read-only", str(tmp_path))
    assert _wait(lambda: task.approval is not None)
    runner.cancel()
    assert _wait(lambda: task.approval is None)
    assert task.state == "cancelled"


def test_energy_recovers_while_the_pet_was_offline(tmp_path):
    from iterator_pet.brain.context import Context

    clock = {"t": 1_000_000.0}
    ctx = Context(tmp_path / "context.json", now=lambda: clock["t"])
    ctx.energy = 0.1
    ctx.save()
    clock["t"] += 30 * 60                                   # 关了半小时：+0.3
    assert abs(Context(tmp_path / "context.json", now=lambda: clock["t"]).energy - 0.4) < 1e-6
    clock["t"] += 12 * 3600                                 # 关了一整晚：补满
    assert Context(tmp_path / "context.json", now=lambda: clock["t"]).energy == 1.0
