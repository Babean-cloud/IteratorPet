from collections import deque
from types import SimpleNamespace

from iterator_pet.platform import projections as P
from iterator_pet.rendering.hologram import Line


def _lines(text="x"):
    return [Line(text)]


def _run(queue, ticks):
    for _ in range(ticks):
        queue.step()


def test_higher_priority_interrupts_and_lower_returns():
    q = P.ProjectionQueue()
    q.post("mind", P.PRI_STATUS, _lines("a"))
    q.post("approval", P.PRI_APPROVAL, _lines("b"))
    assert q.current.kind == "approval"
    q.withdraw("approval")
    assert q.current.kind == "mind"                      # 低的回队列，没丢
    q.post("task", P.PRI_STATUS, _lines("c"))
    assert q.current.kind == "mind"                      # 同优先级先来先投


def test_notice_outranks_mood_and_progress_even_if_they_came_first():
    q = P.ProjectionQueue()
    q.post("mind", P.PRI_STATUS, _lines("心情"), ttl=100)
    q.post("task", P.PRI_STATUS, _lines("进度"), ttl=100)
    q.post("notice", P.PRI_NOTICE, _lines("已取消"), ttl=100)
    assert q.current.kind == "notice"
    q.post("mind", P.PRI_STATUS, _lines("新心情"), ttl=100)         # 心情更新也顶不掉它
    assert q.current.kind == "notice"


def test_same_kind_replaces_and_bumps_version():
    q = P.ProjectionQueue()
    q.post("mind", P.PRI_STATUS, _lines("a"))
    first = q.version
    q.post("mind", P.PRI_STATUS, _lines("b"))
    assert q.version != first and q.current.lines[0].text == "b"
    assert len(q._items) == 1


def test_ttl_only_runs_while_shown():
    q = P.ProjectionQueue()
    q.post("mind", P.PRI_STATUS, _lines(), ttl=10)
    q.post("approval", P.PRI_APPROVAL, _lines())
    _run(q, 50)
    assert q.current.kind == "approval"                  # 被压着的 mind 没走时间
    q.withdraw("approval")
    assert q.current.kind == "mind"
    _run(q, 10)
    assert q.current is None and q.version == 0


def test_status_is_throttled():
    q = P.ProjectionQueue()
    assert q.post("mind", P.PRI_STATUS, _lines(), ttl=5, min_gap=100)
    _run(q, 6)
    assert q.current is None
    assert not q.post("mind", P.PRI_STATUS, _lines(), ttl=5, min_gap=100)     # 太快：丢掉
    _run(q, 100)
    assert q.post("mind", P.PRI_STATUS, _lines(), ttl=5, min_gap=100)


def _task(progress=(), finished=False):
    return SimpleNamespace(progress=deque(progress), finished=finished, state="done" if finished else "running",
                           result="", approval=None, approval_why="")


def test_source_ignores_stale_thought_then_projects_changes():
    q = P.ProjectionQueue()
    src = P.ProjectionSource(q)
    src.observe(mood="平静", thought="旧念头")
    assert q.current is None                              # 启动时已有的不投
    src.observe(mood="好奇", thought="它在看窗口")
    assert q.current.kind == "mind"
    assert any("它在看窗口" in line.text for line in q.current.lines)


def test_source_quiet_updates_seen_without_projecting():
    q = P.ProjectionQueue()
    src = P.ProjectionSource(q)
    src.observe(mood="", thought="a")
    src.observe(mood="", thought="b", quiet=True)
    assert q.current is None
    src.observe(mood="", thought="b")                     # 没变化，不补投
    assert q.current is None


def test_source_task_progress_shows_recent_steps_and_clears_on_finish():
    q = P.ProjectionQueue()
    src = P.ProjectionSource(q)
    task = _task(["看目录"])
    src.observe(task=task)
    assert q.current.kind == "task"
    task.progress.extend(["读 a.py", "读 b.py", "跑测试"])
    q._last_shown.clear()                                 # 绕过节流，只验内容
    src.observe(task=task)
    texts = [line.text for line in q.current.lines]
    assert "跑测试" in texts and "看目录" not in texts    # 只留最近几步
    task.finished = True
    src.observe(task=task)
    assert q.current is None
