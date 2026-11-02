"""B4：按日期回退（有 git 按提交时间，没有按 .history 的日期目录）。"""

import os
from datetime import datetime

import pytest

from iterator_pet.brain.habits import store as S
from iterator_pet.brain.habits.store import HabitError, HabitStore

from test_habit_store import CONFIG, SKILL

needs_git = pytest.mark.skipif(S.find_git() is None, reason="本机没有 git")


class Clock:
    t = datetime(2026, 11, 1, 10, 0)

    def __call__(self):
        return self.t


def stamp(clock, day, hour=12):
    clock.t = datetime(2026, 11, day, hour, 0)
    iso = clock.t.strftime("%Y-%m-%dT%H:%M:%S")
    os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = iso


def build(tmp_path, git, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-11-01T10:00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-11-01T10:00:00")
    clock = Clock()
    store = HabitStore(tmp_path / "ws", git=S.find_git if git else (lambda: None), now=clock)
    stamp(clock, 1)
    store.write("idea-review", SKILL, CONFIG, "v1 原话", kind="create")
    stamp(clock, 3)
    store.write("idea-review", SKILL + "\n第二版\n", CONFIG, "v2 原话", kind="update")
    stamp(clock, 5)
    store.write("idea-review", SKILL + "\n第二版\n第三版\n", CONFIG, "v3 原话", kind="update")
    return store, clock


@pytest.mark.parametrize("git", [True, False])
def test_revert_to_a_day(tmp_path, monkeypatch, git):
    if git and S.find_git() is None:
        pytest.skip("本机没有 git")
    store, clock = build(tmp_path, git, monkeypatch)
    stamp(clock, 6)
    back = store.revert("idea-review", to="2026-11-04")                 # 11-03 改过、11-05 又改：11-04 结束时是第二版
    assert "第二版" in back.body and "第三版" not in back.body
    text = store.requests("idea-review")
    assert "v3 原话" in text and "· 修改（已撤回）\nv3 原话" in text and "（已撤回）\nv2" not in text, "只标那天之后的原话"
    stamp(clock, 7)
    back = store.revert("idea-review", to="2026-11-01")
    assert "第二版" not in back.body


@pytest.mark.parametrize("git", [True, False])
def test_revert_to_day_errors(tmp_path, monkeypatch, git):
    if git and S.find_git() is None:
        pytest.skip("本机没有 git")
    store, clock = build(tmp_path, git, monkeypatch)
    stamp(clock, 6)
    with pytest.raises(HabitError, match="还没有它"):
        store.revert("idea-review", to="2026-10-20")
    with pytest.raises(HabitError, match="YYYY-MM-DD"):
        store.revert("idea-review", to="上周")
    with pytest.raises(HabitError, match="没有要回退"):
        store.revert("idea-review", to="2026-11-05")                    # 当天结束时就是现在这版
    with pytest.raises(HabitError, match="没有要回退"):
        store.revert("idea-review", to="2026-11-30")
    assert "第三版" in store.get("idea-review").body, "出错不改动"


def test_director_revert_with_date(tmp_path, monkeypatch):
    from test_habit_author import World

    store, clock = build(tmp_path / "x", False, monkeypatch)
    w = World(tmp_path / "w")
    w.store, w.director.habits = store, store
    w.apply({"habit_edit": {"op": "revert", "name": "idea-review", "to": "2026-11-02"}}, user_text="回到昨天那样")
    assert "第二版" not in store.get("idea-review").body and "第三版" not in store.get("idea-review").body
    assert w.said == ["……回到 2026-11-02 的样子了。"]
    w.said.clear()
    w.apply({"habit_edit": {"op": "revert", "name": "idea-review", "to": "上周"}}, user_text="回到上周")
    assert w.said == ["……没弄成。详情在对话窗口里。"]


def test_observation_has_today_for_date_math(tmp_path):
    from test_habit_author import World

    w = World(tmp_path)
    w.store.write("idea-review", SKILL, CONFIG, "x", kind="create")
    assert "date" in w.director.observe(w.sim, "chat", "回到上周的版本")
