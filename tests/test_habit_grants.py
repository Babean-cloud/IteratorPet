"""P 组：授权期限、回退与习惯任务的文件边界。只碰 tmp_path。"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from iterator_pet.brain.habits.files import FileDenied, HabitFiles
from iterator_pet.brain.habits.grants import GrantError, GrantStore
from iterator_pet.brain.habits.runner import script_block_reason
from iterator_pet.brain.commands import parse
from iterator_pet.platform.quick_bar import execute


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 2, 10)

    def __call__(self):
        return self.value

    def later(self, days):
        self.value += timedelta(days=days)


def setup(tmp_path):
    clock = Clock()
    root = tmp_path / "ws"
    root.mkdir()
    folder = tmp_path / "external"
    folder.mkdir()
    grants = GrantStore(tmp_path / "grants.json", now=clock)
    return clock, root, folder, grants


def test_grant_expiry_renew_upgrade_and_persist(tmp_path):
    clock, _, folder, grants = setup(tmp_path)
    row = grants.add(folder)
    assert row["tier"] == "3d" and grants.covering(folder / "sub/x.txt") is row
    clock.later(2)
    assert grants.use(folder / "x.txt")
    clock.later(2)
    assert grants.covering(folder / "x.txt") is row
    assert grants.use(folder / "x.txt")
    clock.later(1)
    assert grants.use(folder / "x.txt") and row["tier"] == "30d"
    assert GrantStore(grants.path, now=clock).covering(folder / "x.txt") is not None
    clock.later(31)
    assert grants.covering(folder / "x.txt") is None


def test_overlap_uses_longer_expiry_and_forever(tmp_path):
    clock, _, folder, grants = setup(tmp_path)
    child = folder / "child"
    child.mkdir()
    parent = grants.add(folder)
    clock.later(1)
    nested = grants.add(child)
    assert grants.covering(child / "x.txt") is nested
    grants.add(child, forever=True)
    assert grants.covering(child / "x.txt") is nested and nested["tier"] == "forever"
    clock.later(4)
    assert grants.covering(folder / "x.txt") is None
    assert grants.covering(child / "x.txt") is nested
    assert grants.revoke(child) and not grants.revoke(child)
    assert grants.covering(child / "x.txt") is None and parent in grants.rows


def test_forbidden_roots_and_private_appdata(tmp_path, monkeypatch):
    _, _, folder, grants = setup(tmp_path)
    home = tmp_path / "home"
    win = tmp_path / "Windows"
    program = tmp_path / "Program Files"
    startup = tmp_path / "Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    for path in (home, win, program, startup):
        path.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("WINDIR", str(win))
    monkeypatch.setenv("ProgramFiles", str(program))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    for path in (home, win, program, startup, Path(folder.anchor)):
        with pytest.raises(GrantError):
            grants.add(path)


def test_backup_before_overwrite_and_expired_write_denied(tmp_path):
    clock, root, folder, grants = setup(tmp_path)
    grants.add(folder)
    files = HabitFiles(root, grants, now=clock)
    target = folder / "note.txt"
    target.write_text("旧内容", encoding="utf-8")
    files.write(str(target), "新内容")
    assert target.read_text(encoding="utf-8") == "新内容"
    backups = list((root / ".history/grants").rglob("*.bak"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "旧内容"
    clock.later(4)
    with pytest.raises(FileDenied, match="过期"):
        files.write(str(target), "不应写入")
    assert target.read_text(encoding="utf-8") == "新内容"


def test_delete_uses_recycle_bin_or_fallback(tmp_path, monkeypatch):
    clock, root, folder, grants = setup(tmp_path)
    grants.add(folder)
    files = HabitFiles(root, grants, now=clock)
    target = folder / "old.txt"
    target.write_text("可恢复", encoding="utf-8")
    calls = []
    monkeypatch.setitem(sys.modules, "send2trash", SimpleNamespace(send2trash=lambda path: (calls.append(path), Path(path).unlink())))
    files.delete(str(target))
    assert calls == [str(target)] and not target.exists()
    target.write_text("备份", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "send2trash", None)
    files.delete(str(target))
    trash = list((root / ".trash/grants").rglob("old.txt"))
    assert len(trash) == 1 and trash[0].read_text(encoding="utf-8") == "备份" and not target.exists()


def test_ungranted_new_file_renames_and_limits(tmp_path, monkeypatch):
    clock, root, folder, grants = setup(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    config = SimpleNamespace(grants_outside_daily_bytes=3)
    files = HabitFiles(root, grants, config, clock)
    target = other / "x.txt"
    target.write_text("原件", encoding="utf-8")
    created = files.write(str(target), "ab")
    assert created.name == "x (2).txt" and target.read_text(encoding="utf-8") == "原件"
    with pytest.raises(FileDenied, match="额度"):
        files.write(str(target), "cd")
    with pytest.raises(FileDenied, match="隐藏"):
        files.write(str(other / ".secret"), "a")
    monkeypatch.setenv("PATH", str(other))
    with pytest.raises(FileDenied, match="PATH"):
        files.write(str(other / "new.txt"), "a")


def test_script_grant_and_autonomous_reply_ignored(tmp_path):
    clock, root, folder, grants = setup(tmp_path)
    grants.add(folder)
    script = f'Set-Content "{folder / "x.txt"}" -Value ok'
    assert script_block_reason(script, root, grants) == ""
    assert script_block_reason(f'Set-Content "{tmp_path / "other.txt"}" -Value ok', root, grants)
    assert parse("/grants").name == "grants"
    pet = SimpleNamespace(grants=grants)
    assert str(folder) in execute(pet, "/grants")[1]
    assert "撤回" in execute(pet, f"/grants revoke {folder}")[1]

    from iterator_pet.brain.director import Director

    director = Director(object(), grants=grants, wall=clock)
    director._say = lambda *args, **kwargs: False
    director.log = lambda *args: None
    action = {"grant": {"path": str(folder), "forever": True}}
    director.apply(object(), action, reason="idle", user_text=f"{folder} 一直给你")
    assert not grants.rows
    director.apply(object(), action, reason="chat", user_text=f"{folder} 一直给你")
    assert grants.rows[0]["tier"] == "forever"


def test_model_cannot_expand_scope_or_invent_forever(tmp_path):
    clock, _, folder, grants = setup(tmp_path)
    child = folder / "child"
    child.mkdir()
    from iterator_pet.brain.director import Director

    director = Director(object(), grants=grants, wall=clock)
    director._say = lambda *args, **kwargs: False
    director.log = lambda *args: None
    action = {"grant": {"path": str(folder), "forever": True}}
    director.apply(object(), action, reason="chat", user_text=f"{child} 可以改")
    assert not grants.rows
    director.apply(object(), action, reason="chat", user_text=f"{folder} 我不是说一直给你")
    assert not grants.rows
    director.apply(object(), action, reason="chat", user_text=f"{folder} 可以改")
    assert grants.rows[0]["tier"] == "3d"


def test_habit_runner_grants_mode_reaches_external_file(tmp_path):
    from iterator_pet.brain.habits.runner import HabitRunner
    from iterator_pet.brain.habits.store import HabitState, HabitStore
    from test_habit_author import SKILL
    from test_habit_runner import ToolClient, call, msg, run_to_end
    from test_habit_scheduler import DIGEST, REVIEW, hab

    clock, root, folder, grants = setup(tmp_path)
    grants.add(folder)
    store = HabitStore(root, git=lambda: None)
    config = hab(dict(DIGEST), REVIEW)
    config["permissions"] = {"web": False, "write": "grants"}
    habit = store.write("idea-review", SKILL, config, "写授权文件夹", kind="create")
    target = folder / "report.txt"
    client = ToolClient(msg(call("write_file", path=str(target), content="完成")), msg(call("finish", summary="完成")))
    runner = HabitRunner(lambda: client, store, HabitState(tmp_path / "state.json"), grants=grants, wall=clock)
    runner.start(habit, DIGEST)
    result = run_to_end(runner)
    assert result.state == "done" and target.read_text(encoding="utf-8") == "完成"
    assert str(target) in result.files and "delete_file" in client.tool_names[0]


def test_settings_lists_and_revokes_grant(tmp_path, monkeypatch):
    from iterator_pet.config import Config
    from iterator_pet.platform.settings_window import SettingsWindow

    _, _, folder, grants = setup(tmp_path)
    grants.add(folder)
    monkeypatch.setattr("iterator_pet.platform.autostart.enabled", lambda: False)
    window = SimpleNamespace(config=Config(), character="pebbles", grants=grants)
    panel = SettingsWindow(window)
    assert panel.grant_picker.count() == 1 and panel.grant_picker.currentData() == str(folder)
    panel._revoke_grant()
    assert not grants.rows and panel.grant_picker.count() == 0
    panel.close()

