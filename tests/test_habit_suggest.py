"""B5：habit_suggest 被拒绝 / 没理的，30 天内同类不再提。"""

from datetime import datetime, timedelta

from iterator_pet.brain.habits.store import HabitState

from test_habit_author import World

ASK = "这种整理以后要不要做成习惯？"


def build(tmp_path):
    w = World(tmp_path)
    w.state = HabitState(tmp_path / "s.json")
    w.director.habit_state = w.state
    w.now = {"t": datetime(2026, 11, 2, 12, 0)}
    w.director.wall = lambda: w.now["t"]
    return w


def suggest(w, text=ASK):
    w.said.clear()
    w.apply({"habit_suggest": text}, reason="task_done")
    return list(w.said)


def test_refusal_blocks_similar_for_thirty_days(tmp_path):
    w = build(tmp_path)
    assert suggest(w) == [ASK]
    w.director.say_to("不用了")
    assert suggest(w) == [] and suggest(w, "这类整理要不要做成习惯呢？") == [], "相似的也不提"
    assert suggest(w, "每周五要不要自动备份工作区？") == ["每周五要不要自动备份工作区？"], "不同类的照提"
    w.now["t"] += timedelta(days=31)
    assert suggest(w) == [ASK]


def test_acceptance_does_not_block(tmp_path):
    w = build(tmp_path)
    suggest(w)
    w.director.say_to("好，做吧")
    assert suggest(w) == [ASK]


def test_ignoring_it_counts_as_a_no(tmp_path):
    w = build(tmp_path)
    suggest(w)
    w.director.clock = lambda t=w.director.clock(): t + 700        # 十分钟没人理
    w.director.tick(w.sim)
    assert suggest(w) == []


def test_other_answers_count_as_ignoring(tmp_path):
    w = build(tmp_path)
    suggest(w)
    w.director.say_to("晚上吃什么")
    assert suggest(w) == []
