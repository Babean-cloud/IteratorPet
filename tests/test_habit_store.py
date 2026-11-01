"""习惯存储：创建、校验、只追加、撤回（有 git / 没有 git）、手改检测、删进 .trash。"""

import json
from datetime import datetime

import pytest

from iterator_pet.brain.habits import store as S
from iterator_pet.brain.habits.store import HabitError, HabitState, HabitStore

SKILL = """---
name: idea-review
description: 用户随口说出自己感兴趣的话题时记下来；每晚回顾。
metadata:
  iteratorpet-title: 想法回顾
  iteratorpet-version: "1"
---

# 想法回顾

## 记下想法
追加到 notes/ideas.md。
"""

CONFIG = {
    "schema": 1,
    "triggers": [
        {"id": "capture", "type": "chat", "when": "用户随口说出想法", "run": "inline"},
        {"id": "review", "type": "time", "at": "22:30", "days": "daily", "run": "inline"},
    ],
    "permissions": {"web": False, "write": "notes"},
}

HAS_GIT = S.find_git() is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="本机没有 git")


def make(tmp_path, git=True, day_cap=0):
    clock = iter(range(10_000))
    store = HabitStore(tmp_path / "ws", day_cap=day_cap, git=S.find_git if git else (lambda: None),
                       now=lambda: datetime(2026, 11, 2, 21, next(clock) % 60))
    return store


def create(store, text="帮我记想法"):
    return store.write("idea-review", SKILL, CONFIG, text, kind="create")


# -- 创建 / 读 ---------------------------------------------------------------

def test_create_writes_all_files_and_reads_back(tmp_path):
    store = make(tmp_path)
    assert not (tmp_path / "ws").exists(), "第一次用到才创建工作区"
    habit = create(store)
    folder = tmp_path / "ws" / "habits" / "idea-review"
    assert {p.name for p in folder.iterdir()} == {"SKILL.md", "habit.json", "request.md", "notes"}
    assert habit.title == "想法回顾" and habit.enabled
    assert habit.config["budget"] == S.DEFAULT_BUDGET and habit.config["permissions"] == {"web": False, "write": "notes"}
    assert "## 记下想法" in habit.body
    assert [h.name for h in store.list()] == ["idea-review"]
    assert store.get("idea-review").requests[-1].endswith("帮我记想法")
    assert "· 创建" in store.requests("idea-review")


def test_create_twice_and_update_missing_fail(tmp_path):
    store = make(tmp_path)
    create(store)
    with pytest.raises(HabitError, match="已经有了"):
        create(store)
    with pytest.raises(HabitError, match="没有叫"):
        store.write("other", SKILL.replace("idea-review", "other"), CONFIG, "x", kind="update")


# -- 校验 --------------------------------------------------------------------

@pytest.mark.parametrize("name", ["", "Idea", "-a", "a-", "a--b", "a_b", "中文", "x" * 65])
def test_bad_names(name):
    with pytest.raises(HabitError, match="不合规"):
        S.validate_name(name)


def test_skill_validation():
    with pytest.raises(HabitError, match="frontmatter"):
        S.validate_skill("a", "没有头")
    with pytest.raises(HabitError, match="必须和习惯文件夹名"):
        S.validate_skill("other", SKILL)
    with pytest.raises(HabitError, match="description"):
        S.validate_skill("idea-review", SKILL.replace("description: 用户随口", "description: " + "长" * 1100 + "用户随口"))
    with pytest.raises(HabitError, match="description"):
        S.validate_skill("idea-review", "---\nname: idea-review\n---\n正文")
    with pytest.raises(HabitError, match="500 行"):
        S.validate_skill("idea-review", SKILL + "行\n" * 600)
    with pytest.raises(HabitError, match="正文是空的"):
        S.validate_skill("idea-review", "---\nname: idea-review\ndescription: x\n---\n")


def _cfg(**over):
    cfg = json.loads(json.dumps(CONFIG))
    cfg.update(over)
    return cfg


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.update(schema=2), "schema"),
    (lambda c: c.update(triggers=[]), "至少"),
    (lambda c: c["triggers"][0].update(type="cron"), "type"),
    (lambda c: c["triggers"][0].update(run="later"), "run"),
    (lambda c: c["triggers"][0].pop("when"), "when"),
    (lambda c: c["triggers"][0].update(id="Bad Id"), "id"),
    (lambda c: c["triggers"][1].update(id="capture"), "重复"),
    (lambda c: c["triggers"][1].update(at="25:00"), "HH:MM"),
    (lambda c: c["triggers"][1].update(at="9:5"), "HH:MM"),
    (lambda c: c["triggers"][1].update(days="sometimes"), "days"),
    (lambda c: c["triggers"][1].update(window="night"), "window"),
    (lambda c: c["triggers"].append({"id": "e", "type": "event", "event": "reflect", "run": "task"}), "event"),
    (lambda c: c.update(permissions={"web": "yes"}), "web"),
    (lambda c: c.update(permissions={"write": "everything"}), "write"),
    (lambda c: c.update(budget={"run_tokens": -1}), "正整数"),
    (lambda c: c.update(budget={"forever": 1}), "没有"),
    (lambda c: c.update(budget={"day_tokens": 200000}), "全局每日上限"),
    (lambda c: c.update(speak={"priority": "shout"}), "priority"),
])
def test_habit_json_validation(mutate, message):
    cfg = _cfg()
    mutate(cfg)
    with pytest.raises(HabitError, match=message):
        S.validate_habit(cfg, day_cap=100000)


@pytest.mark.parametrize("days", ["daily", "weekdays", "mon,wed", "every:3d"])
def test_good_days(days):
    cfg = _cfg()
    cfg["triggers"][1]["days"] = days
    assert S.validate_habit(cfg)["triggers"][1]["days"] == days


def test_failed_validation_writes_nothing(tmp_path):
    store = make(tmp_path)
    bad = _cfg(permissions={"write": "everything"})
    with pytest.raises(HabitError):
        store.write("idea-review", SKILL, bad, "x", kind="create")
    assert store.list() == [] and not (tmp_path / "ws" / "habits" / "idea-review").exists()


def test_broken_habit_is_skipped_and_reported(tmp_path):
    store = make(tmp_path)
    create(store)
    (tmp_path / "ws" / "habits" / "idea-review" / "habit.json").write_text("{坏的", encoding="utf-8")
    assert store.list() == [] and "idea-review" in store.errors
    with pytest.raises(HabitError):
        store.get("idea-review")


# -- request.md 只追加 -------------------------------------------------------

def test_request_md_is_append_only(tmp_path):
    store = make(tmp_path)
    create(store, "第一句")
    store.write("idea-review", SKILL, CONFIG, "第二句", kind="update")
    store.write("idea-review", SKILL, CONFIG, "", kind="update")          # 空 = 不追加
    text = store.requests("idea-review")
    assert text.count("## 2026-11-02") == 2 and "第一句" in text and text.index("第一句") < text.index("第二句")
    assert len(store.recent_requests("idea-review", 1)) == 1 and "第二句" in store.recent_requests("idea-review", 1)[0]


# -- 撤回 --------------------------------------------------------------------

def _scenario_revert(store):
    create(store, "v1 原话")
    v2 = SKILL.replace("追加到 notes/ideas.md。", "追加到 notes/ideas.md。\n\n## 备资料\n上网。")
    store.write("idea-review", v2, _cfg(permissions={"web": True, "write": "notes"}), "v2 原话", kind="update")
    assert store.get("idea-review").config["permissions"]["web"] is True
    back = store.revert("idea-review")
    assert back.config["permissions"]["web"] is False and "备资料" not in back.body
    text = store.requests("idea-review")
    assert "v1 原话" in text and "v2 原话" in text, "原话历史必须完整"
    assert "· 修改（已撤回）" in text and "· 创建（已撤回）" not in text
    with pytest.raises(HabitError, match="更早"):
        store.revert("idea-review")                                      # 只剩 v1，回不动了


@needs_git
def test_revert_with_git(tmp_path):
    store = make(tmp_path, git=True)
    assert store.has_git
    _scenario_revert(store)
    assert (tmp_path / "ws" / ".git").is_dir()


def test_revert_without_git(tmp_path):
    store = make(tmp_path, git=False)
    assert not store.has_git
    _scenario_revert(store)
    assert not (tmp_path / "ws" / ".git").exists()
    assert (tmp_path / "ws" / ".history").is_dir()


def test_revert_keeps_notes(tmp_path):
    store = make(tmp_path, git=False)
    create(store)
    store.write("idea-review", SKILL + "\n补一行\n", CONFIG, "改", kind="update")
    (tmp_path / "ws" / "habits" / "idea-review" / "notes" / "ideas.md").write_text("想法", encoding="utf-8")
    store.revert("idea-review")
    assert (tmp_path / "ws" / "habits" / "idea-review" / "notes" / "ideas.md").read_text(encoding="utf-8") == "想法"


# -- 手改 --------------------------------------------------------------------

@needs_git
def test_hand_edit_is_committed_and_respected(tmp_path):
    store = make(tmp_path)
    create(store)
    skill = tmp_path / "ws" / "habits" / "idea-review" / "SKILL.md"
    skill.write_text(SKILL.replace("追加到 notes/ideas.md。", "我手改的一句。"), encoding="utf-8")
    habit = store.get("idea-review")
    assert "我手改的一句" in habit.body
    log = store._run_git("log", "--format=%s")
    assert "手改：idea-review" in log
    assert any(r.get("kind") == "hand-edit" for r in store.read_log("idea-review"))
    store.get("idea-review")                                              # 再读不会重复提交
    assert store._run_git("log", "--format=%s").count("手改：") == 1
    # 手改也是一版：撤回回到它之前
    assert "我手改的一句" not in store.revert("idea-review").body


# -- 暂停 / 删除 -------------------------------------------------------------

def test_pause_and_resume(tmp_path):
    store = make(tmp_path, git=False)
    create(store)
    assert store.set_enabled("idea-review", False).enabled is False
    assert store.set_enabled("idea-review", True).enabled is True


@pytest.mark.parametrize("git", [True, False])
def test_delete_moves_to_trash(tmp_path, git):
    if git and not HAS_GIT:
        pytest.skip("本机没有 git")
    store = make(tmp_path, git=git)
    create(store)
    dest = store.delete("idea-review")
    assert dest.parent.name == ".trash" and (dest / "SKILL.md").is_file()
    assert store.list() == [] and not store.exists("idea-review")
    with pytest.raises(HabitError):
        store.delete("idea-review")


def test_slugify():
    assert S.slugify("Idea Review") == "idea-review"
    assert S.slugify("想法回顾") == "xiang-fa-hui-gu"
    assert S.slugify("想法回顾", taken={"xiang-fa-hui-gu"}) == "xiang-fa-hui-gu-2"
    assert S.slugify("!!!") == "habit"
    S.validate_name(S.slugify("每天 22:30 提醒我喝水！"))


# -- 运行状态 ----------------------------------------------------------------

def test_state_lives_outside_workspace_and_rolls_over(tmp_path):
    day = ["2026-11-02"]
    from datetime import date
    state = HabitState(tmp_path / "state.json", today=lambda: date.fromisoformat(day[0]))
    state.add_tokens("a", 100)
    state.mark_run("a", "t", 5.0)
    assert state.record_result("a", False) == 1 and state.record_result("a", False) == 2
    state.set_paused("a", "连续失败")
    again = HabitState(tmp_path / "state.json", today=lambda: date.fromisoformat(day[0]))
    assert again.day_tokens("a") == 100 and again.last_run("a", "t") == 5.0
    assert again.fail_streak("a") == 2 and again.paused("a") == "连续失败"
    day[0] = "2026-11-03"
    assert again.day_tokens("a") == 0, "跨天清零"
    again.set_paused("a", "")
    assert again.paused("a") == "" and again.fail_streak("a") == 0
    assert HabitState().path.parent.name == "IteratorPet"                 # 默认走（被测试改过的）LOCALAPPDATA


def test_workspace_path(monkeypatch, tmp_path):
    class Cfg:
        workspace_dir = ""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert S.workspace_path(Cfg()) == tmp_path / "IteratorWS"
    Cfg.workspace_dir = str(tmp_path / "elsewhere")
    assert S.workspace_path(Cfg()) == tmp_path / "elsewhere"
