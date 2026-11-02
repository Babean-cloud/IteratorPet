"""A1：last_habit_change 观察 + “改回去”本地兜底。"""

import pytest

from iterator_pet.brain.director import UNDO_WORDS, Director
from iterator_pet.simulation.state import SimulationState

from test_habit_author import HABIT, SKILL, World


@pytest.mark.parametrize("text", ["改回去", "改回去吧", "撤销", "撤回", "不对，改回去", "算了，恢复原样", "还原", "回到之前的样子", "帮我改回去"])
def test_undo_phrases(text):
    assert UNDO_WORDS.match(text)


@pytest.mark.parametrize("text", ["改回去那个报错再看看", "这个怎么撤销", "把时间改一下", "晚上好", "不对"])
def test_not_undo_phrases(text):
    assert not UNDO_WORDS.match(text)


def made(tmp_path):
    w = World(tmp_path)
    w.store.write("idea-review", SKILL, HABIT, "v1", kind="create")
    w.store.write("idea-review", SKILL + "\n补一句\n", HABIT, "v2", kind="update")
    w.director._note_habit_change("idea-review", "想法回顾", "update", "补了一句")
    return w


def test_observation_carries_last_change_for_ten_minutes(tmp_path):
    w = made(tmp_path)
    clock = {"t": w.director.clock()}
    obs = w.director.observe(w.sim, "chat", "改回去")
    assert obs["last_habit_change"]["name"] == "idea-review" and obs["last_habit_change"]["op"] == "update"
    w.director._last_habit_change["at"] -= 700
    assert "last_habit_change" not in w.director.observe(w.sim, "idle")


def test_short_undo_reverts_without_the_model(tmp_path):
    w = made(tmp_path)
    w.apply({"say": "好"}, user_text="改回去")
    assert "补一句" not in w.store.get("idea-review").body and any("改回去了" in s for s in w.said)


def test_no_duplicate_when_model_already_reverted(tmp_path):
    w = made(tmp_path)
    w.apply({"habit_edit": {"op": "revert", "name": "idea-review"}}, user_text="改回去")
    assert [r for r in w.store.requests("idea-review").count("已撤回") * [1]] == [1], "只撤回了一层"


def test_fallback_limits(tmp_path):
    w = made(tmp_path)
    w.apply({}, user_text="改回去那个报错再看看")                   # 不是撤销用语
    w.apply({}, user_text="改回去", reason="window")                # 不是用户亲口说的
    assert "补一句" in w.store.get("idea-review").body
    w.director._last_habit_change["at"] -= 200                      # 超过 2 分钟
    w.apply({}, user_text="改回去")
    assert "补一句" in w.store.get("idea-review").body
    w.director._last_habit_change["at"] += 200
    w.director._last_habit_change["op"] = "revert"                  # 刚撤回过，不再兜底
    w.apply({}, user_text="改回去")
    assert "补一句" in w.store.get("idea-review").body
