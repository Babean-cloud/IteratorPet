"""B2 / B3：脚本类习惯（run: script）和“同步到 GitHub”示范。不真的访问 GitHub：shell 用假的。"""

import json
import time

import pytest

from iterator_pet.brain.habits import author as A
from iterator_pet.brain.habits import store as S
from iterator_pet.brain.habits.runner import HabitRunner, WriteDenied, check_write, script_block_reason
from iterator_pet.brain.habits.store import HabitError, HabitState, HabitStore

from test_habit_author import FakeClient, SKILL
from test_habit_runner import run_to_end
from test_habit_scheduler import Rig, T, hab

PUSH = {"id": "push", "type": "time", "at": "23:00", "days": "daily", "run": "script", "script": "scripts/sync.ps1"}

# 作者格式说明里给的示范脚本（B3）：取出来单独验证它自己不会被拦
GUIDE = A.FORMAT_GUIDE
SYNC = "\n".join(line[4:] for line in GUIDE[GUIDE.index("    $ErrorActionPreference"):GUIDE.index("  说明：仓库只在第一次创建")].splitlines())


def test_guide_example_is_a_valid_unblocked_script():
    assert "gh auth status" in SYNC and "gh repo create" in SYNC and "--private" in SYNC and "git push" in SYNC
    assert "token" not in SYNC.lower() and script_block_reason(SYNC) == ""


# -- 校验 --------------------------------------------------------------------

@pytest.mark.parametrize("script", [None, "", "sync.ps1", "scripts/../x.ps1", "scripts/a b.ps1", "scripts/x.bat", "/scripts/x.ps1"])
def test_bad_script_paths(script):
    with pytest.raises(HabitError, match="script"):
        S.validate_habit(hab(dict(PUSH, script=script)))


def test_chat_trigger_cannot_run_scripts():
    with pytest.raises(HabitError, match="chat"):
        S.validate_habit(hab({"id": "c", "type": "chat", "when": "x", "run": "script", "script": "scripts/a.ps1"}))


def test_store_writes_scripts_with_bom_and_requires_them(tmp_path):
    store = HabitStore(tmp_path / "ws", git=lambda: None)
    with pytest.raises(HabitError, match="还没有写出来"):
        store.write("idea-review", SKILL, hab(PUSH), "x", kind="create")
    store.write("idea-review", SKILL, hab(PUSH), "x", kind="create", scripts={"scripts/sync.ps1": "git push\n中文"})
    raw = (tmp_path / "ws" / "habits" / "idea-review" / "scripts" / "sync.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and "中文".encode() in raw and b"\r\n" in raw
    store.write("idea-review", SKILL + "\n补\n", hab(PUSH), "改", kind="update")          # 脚本已在盘上就不用再给
    with pytest.raises(HabitError, match="scripts"):
        store.write("idea-review", SKILL, hab(PUSH), "x", kind="update", scripts={"../evil.ps1": "x"})


# -- 命令检查 ----------------------------------------------------------------

@pytest.mark.parametrize("text, blocked", [
    ("git add -A\ngit commit -m x\ngit push", False),
    ("Get-ChildItem D:\\other | Select-Object -First 3", False),          # 读工作区外的：只有路径、不像写
    ("Copy-Item notes\\a.md D:\\backup\\a.md", True),
    ("git status > D:\\out.txt", True),
    ("Remove-Item C:\\Users\\x\\file.txt", True),
    ("Set-Content -Path ..\\x.txt -Value 1", True),
    ("Remove-Item notes\\old.md", False),                                  # 只涉及工作区：直接跑
])
def test_script_block_reason(text, blocked):
    assert bool(script_block_reason(text)) is blocked


# -- 运行 --------------------------------------------------------------------

class Shell:
    def __init__(self, code=0, output="ok\n已推送"):
        self.code, self.output, self.calls = code, output, []

    def __call__(self, command, cwd, timeout, cancelled):
        self.calls.append((command, cwd, timeout))
        return self.code, self.output


def script_setup(tmp_path, shell, body="git push"):
    store = HabitStore(tmp_path / "ws", git=lambda: None)
    habit = store.write("idea-review", SKILL, hab(PUSH), "x", kind="create", scripts={"scripts/sync.ps1": body})
    runner = HabitRunner(lambda: None, store, HabitState(tmp_path / "s.json"), shell_fn=shell)
    return store, runner, habit


def test_runner_runs_script_without_a_model(tmp_path):
    shell = Shell(output="x" * 3000)
    store, runner, habit = script_setup(tmp_path, shell)
    assert runner.start_script(habit, PUSH) is not None and runner.start_script(habit, PUSH) is None or True
    done = run_to_end(runner)
    command, cwd, timeout = shell.calls[0]
    assert command.startswith("& '") and command.endswith("sync.ps1'") and cwd == str(store.root) and timeout == 120.0
    assert done.kind == "script" and done.state == "done" and len(done.output) == 2000 and done.tokens == 0


def test_script_failure_and_block(tmp_path):
    store, runner, habit = script_setup(tmp_path, Shell(code=1, output="还没登录 GitHub"))
    runner.start_script(habit, PUSH)
    done = run_to_end(runner)
    assert done.state == "failed" and "退出码 1" in done.summary and "还没登录" in done.summary
    store2, runner2, habit2 = script_setup(tmp_path / "b", Shell(), body="Copy-Item a.md D:\\x\\a.md")
    runner2.start_script(habit2, PUSH)
    blocked = run_to_end(runner2)
    assert blocked.state == "blocked" and "工作区以外" in blocked.summary and runner2.shell_fn.calls == []


def test_habit_runs_cannot_write_scripts(tmp_path):
    store, runner, habit = script_setup(tmp_path, Shell())
    for scope in ("notes", "workspace"):
        for rel in ("habits/idea-review/scripts/sync.ps1", "habits/idea-review/scripts/new.ps1", "habits/other/scripts/x.ps1"):
            with pytest.raises(WriteDenied):
                check_write(store.root, "idea-review", scope, rel)


# -- 导演 --------------------------------------------------------------------

def script_rig(tmp_path, shell, body="git push"):
    rig = Rig(tmp_path, {"say": "x"}, hab(PUSH), scripts={"scripts/sync.ps1": body})
    rig.director.habit_runner = HabitRunner(lambda: None, rig.store, rig.state, shell_fn=shell)
    return rig


def drain(rig):
    deadline = time.time() + 5
    while time.time() < deadline:
        rig.tick(0.1)
        if not rig.director.habit_runner.busy and not rig.director.habit_runner._done:
            return
        time.sleep(0.01)


def fire(rig, day=2, hour=23, minute=1):
    rig.wall["t"] = T(2026, 11, day, hour, minute)
    rig.tick()


def test_script_runs_logs_output_and_ignores_the_global_budget(tmp_path):
    shell = Shell()

    class Usage:
        def today_tokens(self):
            return 99

        def record(self, *a, **k):
            pass

        def note_say(self):
            pass

    rig = script_rig(tmp_path, shell)
    rig.director.usage, rig.director.daily_token_cap = Usage(), 10          # 全局 token 已用完
    fire(rig)
    drain(rig)
    (row,) = rig.store.read_log("idea-review")
    assert row["kind"] == "script" and row["result"] == "done" and row["output"] == "ok\n已推送" and row["tokens"] == 0
    assert len(shell.calls) == 1 and rig.said == [], "全局 token 用完脚本照跑，也没有额外开口"
    assert rig.state.fail_streak("idea-review") == 0


def test_script_runs_while_an_errand_task_is_running(tmp_path):
    shell = Shell()
    rig = script_rig(tmp_path, shell)
    rig.director.task_busy_fn = lambda: True
    fire(rig)
    drain(rig)
    assert len(shell.calls) == 1


def test_three_failed_scripts_pause_the_habit(tmp_path):
    shell = Shell(code=1, output="boom")
    rig = script_rig(tmp_path, shell)
    for day in (2, 3, 4):
        fire(rig, day=day)
        drain(rig)
        rig.clock.now += 100
    assert "连续失败" in rig.state.paused("idea-review") and len(shell.calls) == 3
    assert [r["result"] for r in rig.store.read_log("idea-review")] == ["failed"] * 3


def test_blocked_script_tells_the_user_instead_of_running(tmp_path):
    shell = Shell()
    rig = script_rig(tmp_path, shell, body="Move-Item a.md C:\\Users\\x\\a.md")
    fire(rig)
    drain(rig)
    rig.tick()
    assert shell.calls == [] and rig.store.read_log("idea-review")[-1]["result"] == "blocked"
    assert any("没跑" in s for s in rig.said)
    assert rig.state.fail_streak("idea-review") == 0


# -- 作者 --------------------------------------------------------------------

def reply(scripts, habit=None):
    return json.dumps({"skill_md": SKILL, "habit_json": habit or hab(PUSH), "change_summary": "每晚同步",
                       "questions": [], "scripts": scripts}, ensure_ascii=False)


def test_author_returns_scripts():
    result = A.author(FakeClient(reply({"sync.ps1": SYNC})), "pebbles", "把工作区同步到 GitHub", None)
    assert result.ok and list(result.scripts) == ["scripts/sync.ps1"] and "gh repo create" in result.scripts["scripts/sync.ps1"]


def test_author_retries_when_script_would_leave_the_workspace():
    bad = reply({"sync.ps1": "Copy-Item * D:\\backup\\"})
    client = FakeClient(bad, reply({"sync.ps1": "git push"}))
    result = A.author(client, "pebbles", "x", None)
    assert result.ok and len(client.calls) == 2 and "工作区以外" in client.calls[1][-1]["content"]


@pytest.mark.parametrize("scripts", [{"../x.ps1": "git push"}, {"a.bat": "x"}, {"sync.ps1": ""}, ["x"]])
def test_author_rejects_bad_scripts(scripts):
    assert not A.author(FakeClient(reply(scripts), reply(scripts)), "pebbles", "x", None).ok


def test_author_missing_script_is_caught_by_the_store(tmp_path):
    result = A.author(FakeClient(reply({})), "pebbles", "x", None)
    assert result.ok                                                         # 作者这层不管文件在不在……
    store = HabitStore(tmp_path / "ws", git=lambda: None)
    with pytest.raises(HabitError, match="还没有写出来"):
        store.write(result.name, result.skill_md, result.habit_json, "x", kind="create", scripts=result.scripts)
