"""出错时它会用自己的口吻说一句（Key 失效、余额用完、断网），不静默退回台词池。"""

from __future__ import annotations

from iterator_pet.brain.director import ERROR_LINES, ERROR_REPEAT, Director, error_kind
from iterator_pet.simulation.state import SimulationState


class NoClient:
    def complete(self, *a, **k):
        raise AssertionError("不该请求")


def test_error_kinds():
    assert error_kind("HTTP 401: invalid key") == "auth"
    assert error_kind('HTTP 402: {"error":{"message":"Insufficient Balance"}}') == "balance"
    assert error_kind("网络错误: <urlopen error timed out>") == "network"
    assert error_kind("HTTP 503: busy") == "server"
    assert error_kind("ValueError: x") == "other"


def test_autonomous_error_said_once_then_quiet_but_chat_always_answered():
    now = {"t": 1000.0}
    director = Director(NoClient(), clock=lambda: now["t"], idle_fn=lambda: 0.0)
    sim = SimulationState((0.0, 0.0, 800.0, 600.0), warmup=0)
    sim.pool_lines = False
    director._results.put(("idle", None, None, "HTTP 402: Insufficient Balance", "b", None, None))
    director._drain_results(sim)
    assert sim.dialogue.text == ERROR_LINES["balance"]
    sim.cancel_dialogue()
    now["t"] += 60
    director._results.put(("idle", None, None, "HTTP 402: Insufficient Balance", "b", None, None))
    director._drain_results(sim)
    assert not sim.dialogue.text, "同一类错误两小时内不重复唠叨"
    director._results.put(("chat", None, None, "HTTP 402: Insufficient Balance", "b", None, "在吗"))
    director._drain_results(sim)
    assert sim.dialogue.text == ERROR_LINES["balance"], "用户在等回复：一定说"
    sim.cancel_dialogue()
    now["t"] += ERROR_REPEAT + 1
    director._results.put(("idle", None, None, "HTTP 401: bad", "b", None, None))
    director._drain_results(sim)
    assert sim.dialogue.text == ERROR_LINES["auth"]
