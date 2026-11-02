"""task 类习惯：工具与写入范围、联网权限、预算（单次 / 当天 / 全局）、连续失败、与委托任务排队。"""

import json
import time

import pytest

from iterator_pet.brain.habits import runner as R
from iterator_pet.brain.habits.runner import HabitRunner, WriteDenied, check_write
from iterator_pet.brain.habits.store import HabitState, HabitStore

from test_habit_author import HABIT, SKILL
from test_habit_scheduler import DIGEST, REVIEW, Rig, T, hab


def call(name, **args):
    return {"id": "c" + name, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def msg(*calls):
    return {"content": "", "tool_calls": list(calls)}


class ToolClient:
    """按脚本回；记下每次给了哪些工具、问了什么。"""

    def __init__(self, *script, tokens=100):
        self.script, self.tokens = list(script), tokens
        self.tool_names, self.messages = [], []

    def complete_tools(self, messages, tools, max_tokens=0):
        self.tool_names.append([t["function"]["name"] for t in tools])
        self.messages.append(json.loads(json.dumps(messages)))
        item = self.script.pop(0) if self.script else msg(call("finish", summary="没有更多了"))
        if isinstance(item, Exception):
            raise item
        return item, {"prompt_tokens": self.tokens, "completion_tokens": 0}


class FakeWeb:
    can_search = True

    def __init__(self):
        self.queries = []

    def search(self, q):
        self.queries.append(q)
        return "1. git 管记忆\n   https://example.com/git"

    def fetch(self, url):
        return "页面正文"


def run_to_end(runner):
    deadline = time.time() + 5
    while time.time() < deadline:
        done = runner.take_finished()
        if done:
            return done[0]
        time.sleep(0.01)
    raise AssertionError("没跑完")


def build(tmp_path, client, habit=None, web=None):
    store = HabitStore(tmp_path / "ws", git=lambda: None)
    h = store.write("idea-review", SKILL, habit or hab(dict(DIGEST), REVIEW), "帮我记想法", kind="create")
    state = HabitState(tmp_path / "state.json")
    runner = HabitRunner(lambda: client, store, state, web_fn=lambda: web)
    return store, state, runner, h


NOTES = "habits/idea-review/notes"


# -- 写入范围 ----------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "habits/idea-review/SKILL.md", "habits/idea-review/habit.json", "habits/idea-review/request.md",
    "habits/idea-review/log.jsonl", "habits/idea-review/skill.MD", "habits/idea-review/SKILL.md.",
    "habits/idea-review/SKILL.md ", "habits/idea-review/notes/../SKILL.md", "habits/idea-review/SKILL.md::$DATA",
    "habits/idea-review/notes/request.md", ".git/config", ".history/x.md", ".gitignore", "../outside.md",
    "C:/Windows/x.md", "", "habits/other/notes/x.md", "habits/idea-review/x.md", "notes.md",
])
def test_write_denied_in_notes_scope(tmp_path, rel):
    store, *_ = build(tmp_path, ToolClient())
    (tmp_path / "ws" / ".git").mkdir(exist_ok=True)
    with pytest.raises(WriteDenied):
        check_write(store.root, "idea-review", "notes", rel)


def test_write_allowed_in_notes(tmp_path):
    store, *_ = build(tmp_path, ToolClient())
    ok = check_write(store.root, "idea-review", "notes", f"{NOTES}/digest-2026-11-02.md")
    assert ok.name == "digest-2026-11-02.md"
    check_write(store.root, "idea-review", "notes", f"{NOTES}/sub/dir/x.md")


@pytest.mark.parametrize("rel", ["habits/idea-review/SKILL.md", "habits/other/habit.json", "x/request.md",
                                 "habits/idea-review/log.jsonl", ".git/HEAD", ".trash/a.md"])
def test_protected_even_with_workspace_scope(tmp_path, rel):
    store, *_ = build(tmp_path, ToolClient())
    with pytest.raises(WriteDenied):
        check_write(store.root, "idea-review", "workspace", rel)
    assert check_write(store.root, "idea-review", "workspace", "reports/weekly.md").name == "weekly.md"


def test_junction_or_symlink_cannot_escape(tmp_path):
    store, *_ = build(tmp_path, ToolClient())
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "ws" / "habits" / "idea-review" / "notes" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("建不了符号链接")
    with pytest.raises(WriteDenied):
        check_write(store.root, "idea-review", "workspace", f"{NOTES}/link/x.md")


# -- 运行 --------------------------------------------------------------------

def test_run_searches_writes_notes_and_finishes(tmp_path):
    web = FakeWeb()
    client = ToolClient(msg(call("web_search", query="git 管记忆")),
                        msg(call("write_file", path="notes/digest.md", content="# 资料\nhttps://example.com/git")),
                        msg(call("finish", summary="备好了一条资料", say="资料备好了。")))
    store, state, runner, habit = build(tmp_path, client, hab(dict(DIGEST), REVIEW, ) | {"permissions": {"web": True, "write": "notes"}}, web)
    habit = store.get("idea-review")
    run = runner.start(habit, habit.config["triggers"][0], "pebbles")
    assert runner.busy and runner.start(habit, habit.config["triggers"][0]) is None, "同时只跑一个"
    done = run_to_end(runner)
    assert done.state == "done" and done.say == "资料备好了。" and done.steps == 3 and done.tokens == 300
    assert done.files == ["habits/idea-review/notes/digest.md"]
    assert (tmp_path / "ws" / NOTES / "digest.md").read_text(encoding="utf-8").startswith("# 资料")
    assert web.queries == ["git 管记忆"]
    assert "web_search" in client.tool_names[0] and "run_command" not in client.tool_names[0]
    system = client.messages[0][0]["content"]
    assert "## 记下想法" in system and "帮我记想法" in system and "五块卵石" in system
    assert "SKILL.md" in system


def test_no_web_tools_without_permission(tmp_path):
    client = ToolClient(msg(call("web_search", query="x")), msg(call("finish", summary="好")))
    store, state, runner, habit = build(tmp_path, client, web=FakeWeb())
    habit = store.get("idea-review")
    runner.start(habit, habit.config["triggers"][0])
    run_to_end(runner)
    assert not any(n.startswith("web_") for names in client.tool_names for n in names)
    assert "没有这个工具" in client.messages[1][-1]["content"]


def test_cannot_touch_the_habit_itself(tmp_path):
    client = ToolClient(
        msg(call("write_file", path="habits/idea-review/SKILL.md", content="被改了")),
        msg(call("write_file", path="notes/../habit.json", content="{}")),
        msg(call("write_file", path="notes/ok.md", content="好")),
        msg(call("finish", summary="好")))
    store, state, runner, habit = build(tmp_path, client)
    habit = store.get("idea-review")
    before = {n: (habit.path / n).read_bytes() for n in ("SKILL.md", "habit.json", "request.md")}
    runner.start(habit, habit.config["triggers"][0])
    done = run_to_end(runner)
    assert {n: (habit.path / n).read_bytes() for n in before} == before
    assert done.files == ["habits/idea-review/notes/ok.md"]
    assert client.messages[1][-1]["content"].startswith("拒绝")


def test_reads_whole_workspace_but_not_outside(tmp_path):
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "secret.txt").write_text("外面的", encoding="utf-8")
    client = ToolClient(msg(call("list_dir", path="habits")), msg(call("read_file", path="../secret.txt")),
                        msg(call("read_file", path="habits/idea-review/request.md")), msg(call("finish", summary="好")))
    store, state, runner, habit = build(tmp_path, client)
    runner.start(store.get("idea-review"), REVIEW | {"run": "task"})
    run_to_end(runner)
    outs = [m[-1]["content"] for m in client.messages[1:]]
    assert "idea-review/" in outs[0] and outs[1].startswith("拒绝越界") and "帮我记想法" in outs[2]


# -- 预算 --------------------------------------------------------------------

def test_step_limit_stops_and_keeps_written_files(tmp_path):
    habit = hab(dict(DIGEST)) | {"budget": {"run_steps": 2}}
    client = ToolClient(msg(call("write_file", path="notes/a.md", content="a")),
                        msg(call("write_file", path="notes/b.md", content="b")),
                        msg(call("write_file", path="notes/c.md", content="c")))
    store, state, runner, _ = build(tmp_path, client, habit)
    runner.start(store.get("idea-review"), habit["triggers"][0])
    done = run_to_end(runner)
    assert done.state == "over_budget" and done.steps == 2 and len(client.messages) == 2
    assert (tmp_path / "ws" / NOTES / "a.md").exists() and not (tmp_path / "ws" / NOTES / "c.md").exists()


def test_run_token_limit(tmp_path):
    habit = hab(dict(DIGEST)) | {"budget": {"run_tokens": 150}}
    client = ToolClient(*[msg(call("write_file", path=f"notes/{i}.md", content="x")) for i in range(5)], tokens=100)
    store, state, runner, _ = build(tmp_path, client, habit)
    runner.start(store.get("idea-review"), habit["triggers"][0])
    done = run_to_end(runner)
    assert done.state == "over_budget" and done.steps == 2 and done.tokens == 200


def test_day_budget_stops_midway(tmp_path):
    """§10 第 8 条：day_tokens 很小，跑到一半停下，已写的保留。"""
    habit = hab(dict(DIGEST)) | {"budget": {"day_tokens": 250}}
    client = ToolClient(*[msg(call("write_file", path=f"notes/{i}.md", content="x")) for i in range(5)], tokens=100)
    store, state, runner, _ = build(tmp_path, client, habit)
    state.add_tokens("idea-review", 100)                           # 今天已经花了一点
    runner.start(store.get("idea-review"), habit["triggers"][0])
    done = run_to_end(runner)
    assert done.state == "over_budget" and "今天" in done.summary and done.steps == 2
    assert (tmp_path / "ws" / NOTES / "0.md").exists()


def test_failure_and_no_key(tmp_path):
    client = ToolClient(RuntimeError("网络错误: 断网"))
    store, state, runner, _ = build(tmp_path, client)
    habit = store.get("idea-review")
    runner.start(habit, habit.config["triggers"][0])
    done = run_to_end(runner)
    assert done.state == "failed" and "断网" in done.error
    runner.client_fn = lambda: None
    assert runner.start(habit, habit.config["triggers"][0]) is None


# -- 导演接线 ----------------------------------------------------------------

def rigged(tmp_path, client, habit=None):
    rig = Rig(tmp_path, {"say": "x"}, habit or hab(dict(DIGEST, at="02:00")))
    rig.director.habit_runner = HabitRunner(lambda: client, rig.store, rig.state)
    return rig


def fire(rig, day=3, hour=2, minute=1):
    rig.wall["t"] = T(2026, 11, day, hour, minute)
    rig.tick()


def drain(rig, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        rig.tick(0.1)
        if not rig.director.habit_runner.busy and not rig.director.habit_runner._done:
            return
        time.sleep(0.01)


def test_director_runs_the_task_logs_commits_and_speaks(tmp_path):
    client = ToolClient(msg(call("write_file", path="notes/digest.md", content="资料")),
                        msg(call("finish", summary="备好了", say="……资料备好了。")))
    rig = rigged(tmp_path, client)
    fire(rig)
    drain(rig)
    rig.tick(0.1)
    (row,) = rig.store.read_log("idea-review")
    assert row["kind"] == "task" and row["result"] == "done" and row["tokens"] == 200
    assert row["files"] == ["habits/idea-review/notes/digest.md"] and row["trigger"] == "digest"
    assert rig.state.day_tokens("idea-review") == 200 and rig.state.fail_streak("idea-review") == 0
    assert rig.said == ["……资料备好了。"]
    fire(rig, hour=2, minute=30)
    drain(rig)
    assert len(client.messages) == 2, "同一个点只跑一次"


def test_task_waits_for_errand_task_and_quiet_holds_the_say(tmp_path):
    client = ToolClient(msg(call("finish", summary="好", say="跑完了")))
    rig = rigged(tmp_path, client)
    busy = {"v": True}
    rig.director.task_busy_fn = lambda: busy["v"]
    fire(rig)
    rig.tick()
    assert client.messages == [] and rig.director._habit_tasks, "委托任务优先，习惯任务排队"
    busy["v"] = False
    rig.director.quiet = True                                      # 你在全屏：任务照跑，话先攒着
    rig.tick()
    drain(rig)
    assert len(client.messages) == 1 and rig.said == []
    rig.director.quiet = False
    rig.tick()
    assert rig.said == ["跑完了"]


def test_over_budget_run_is_logged_and_not_rerun_today(tmp_path):
    habit = hab(dict(DIGEST, at="02:00")) | {"budget": {"day_tokens": 150}}
    client = ToolClient(*[msg(call("write_file", path=f"notes/{i}.md", content="x")) for i in range(6)], tokens=100)
    rig = rigged(tmp_path, client, habit)
    fire(rig)
    drain(rig)
    (row,) = rig.store.read_log("idea-review")
    assert row["result"] == "over_budget" and row["steps"] == 2
    assert (tmp_path / "ws" / NOTES / "0.md").exists()
    # 同一天里再到点（比如又加了一个触发）也不再跑
    rig.director._habit_tasks.append(type("D", (), {"name": "idea-review", "id": "digest", "run": "task",
                                                    "trigger": habit["triggers"][0]})())
    rig.tick()
    rows = rig.store.read_log("idea-review")
    assert rows[-1]["result"] == "over_budget" and "今天" in rows[-1]["summary"] and len(client.messages) == 2


def test_three_failed_runs_pause_the_habit(tmp_path):
    client = ToolClient(*[RuntimeError("坏了")] * 3)
    rig = rigged(tmp_path, client, hab(dict(DIGEST, at="02:00")))
    for day in (3, 4, 5):
        fire(rig, day=day)
        drain(rig)
        rig.clock.now += 100
    rig.tick()
    assert "连续失败" in rig.state.paused("idea-review")
    assert [r["result"] for r in rig.store.read_log("idea-review")] == ["failed"] * 3
    assert any("停了" in s for s in rig.said)


def test_global_budget_pauses_model_tasks(tmp_path):
    class Usage:
        def today_tokens(self):
            return 99

        def record(self, *a, **k):
            pass

        def note_say(self):
            pass

    client = ToolClient()
    rig = rigged(tmp_path, client)
    rig.director.usage, rig.director.daily_token_cap = Usage(), 10
    fire(rig)
    rig.tick()
    assert client.messages == [] and not rig.director._habit_tasks
    assert len(rig.said) == 1 and "额度" in rig.said[0]


def test_notes_text_cannot_be_sent_to_the_web(tmp_path):
    secret = "我的私人想法：下个月要和房东谈搬家的事情并且不能让任何人知道这件事，包括我的同事和家人还有隔壁邻居"
    web = FakeWeb()
    client = ToolClient(
        msg(call("read_file", path="habits/idea-review/notes/ideas.md")),
        msg(call("web_search", query="搜一下 " + secret[:45])),
        msg(call("web_fetch", url="https://example.com/?q=" + __import__("urllib.parse").parse.quote(secret[:45]))),
        msg(call("web_search", query="房东 搬家 谈判技巧")),
        msg(call("finish", summary="好")))
    habit = hab(dict(DIGEST)) | {"permissions": {"web": True, "write": "notes"}}
    store, state, runner, _ = build(tmp_path, client, habit, web)
    (tmp_path / "ws" / NOTES / "ideas.md").write_text(secret, encoding="utf-8")
    runner.start(store.get("idea-review"), habit["triggers"][0])
    run_to_end(runner)
    outs = [m[-1]["content"] for m in client.messages[1:]]
    assert outs[1].startswith("拒绝") and "notes" in outs[1] and outs[2].startswith("拒绝")
    assert web.queries == ["房东 搬家 谈判技巧"], "概括过的关键词可以发"


def test_leaks_notes_unit():
    src = ["abcdefghij" * 6]
    assert R.leaks_notes(src, "x" + "abcdefghij" * 4 + "y")
    assert R.leaks_notes(src, "abcdefghij abcdefghij\nabcdefghij abcdefghij"), "忽略空白"
    assert not R.leaks_notes(src, "abcdefghij" * 3), "不到 40 个字不算"
    assert not R.leaks_notes([], "abcdefghij" * 6)
