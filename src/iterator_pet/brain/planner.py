"""日程：模型一次给一段"接下来几分钟做什么"，本地一步步演，不再每个动作问一次模型。

步骤（``do`` 字段）：
* ``say``      {text, when: "now" | "cursor_near"}  —— cursor_near 最多等 60 s，等不到就算了
* ``go``       {x, y} 或 {to: "cursor"}             —— 飘过去，到了（或 20 s 超时）进下一步
* ``park``     {side: left/right/top/bottom/free}   —— 设地盘（持久，见 SimulationState.set_territory）
* ``look``     {at: "cursor" | {x, y}, s}
* ``follow``   {s}  —— 跟着光标
* ``meditate`` {s}  —— 原地发呆
* ``sleep``    {s}  —— 原地低头（导演看到在睡就不主动请求）
* ``wait``     {s}  —— 自由漫游
"""

from __future__ import annotations

import time

from ..simulation import constants as C

MAX_STEPS = 6
DURATION_RANGE = (2.0, 300.0)
SAY_WAIT_LIMIT = 60.0
GO_TIMEOUT = 20.0
SAY_MAX_CHARS = 40
SIDES = ("left", "right", "top", "bottom", "free")


def _seconds(step: dict, default: float) -> float:
    try:
        value = float(step.get("s", default))
    except (TypeError, ValueError):
        value = default
    return min(DURATION_RANGE[1], max(DURATION_RANGE[0], value))


def _point(sim, value) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        fx = min(1.0, max(0.0, float(value.get("x"))))
        fy = min(1.0, max(0.0, float(value.get("y"))))
    except (TypeError, ValueError):
        return None
    x, y, w, h = sim.bounds
    return (x + fx * w, y + fy * h)


class Planner:
    def __init__(self, clock=time.monotonic, on_territory=None, say_fn=None):
        self.clock = clock
        self.on_territory = on_territory
        # 说话走导演的闸门（话量 / 勿扰）；没有导演时直接进气泡
        self.say_fn = say_fn
        self.steps: list[dict] = []
        self.current: dict | None = None
        self._started = 0.0
        self._next_follow = 0.0

    # -- 状态 -----------------------------------------------------------------
    @property
    def idle(self) -> bool:
        return self.current is None and not self.steps

    @property
    def sleeping(self) -> bool:
        return self.current is not None and self.current.get("do") == "sleep"

    @property
    def waiting_for_cursor(self) -> bool:
        step = self.current
        return step is not None and step.get("do") == "say" and step.get("when") == "cursor_near"

    STEP_NAMES = {"say": "等着开口", "go": "飘过去", "park": "挪窝", "look": "张望",
                  "follow": "跟着光标", "meditate": "发呆", "sleep": "打盹", "wait": "闲逛"}

    def label(self) -> str:
        if self.current is None:
            return "（没有安排）"
        kind = self.current.get("do")
        rest = [self.STEP_NAMES.get(s.get("do"), s.get("do")) for s in self.steps]
        text = self.STEP_NAMES.get(kind, str(kind))
        return text + (" → " + " → ".join(rest) if rest else "")

    # -- 输入 -----------------------------------------------------------------
    def load(self, steps, sim=None) -> None:
        """换上新日程（旧的作废）。"""
        if sim is not None and self.current is not None:
            sim.clear_directive()
        self.steps = [s for s in (steps or []) if isinstance(s, dict) and s.get("do")][:MAX_STEPS]
        self.current = None

    def clear(self, sim=None) -> None:
        self.load([], sim)

    # -- 每 tick（主线程） -----------------------------------------------------
    def tick(self, sim) -> None:
        if sim.dragging or sim.kill.active:
            return
        now = self.clock()
        if self.current is None:
            if not self.steps:
                return
            self.current = self.steps.pop(0)
            self._started = now
            if not self._begin(sim, self.current, now):
                self.current = None
            return
        if self._finished(sim, self.current, now):
            sim.clear_directive()
            self.current = None

    def _begin(self, sim, step: dict, now: float) -> bool:
        """开始一步。返回 False = 这一步瞬时完成（或无效），直接进下一步。"""
        kind = step.get("do")
        if kind == "say":
            if step.get("when") == "cursor_near":
                return True                          # 在 _finished 里等光标
            self._say(sim, step)
            return False
        if kind == "park":
            side = step.get("side")
            if side not in SIDES:
                return False
            sim.set_territory(None if side == "free" else side,
                              step.get("lo", 0.1), step.get("hi", 0.9))
            if self.on_territory is not None:
                self.on_territory(None if side == "free" else side)
            return False
        ticks = int(_seconds(step, 20.0) * C.GAME_HZ)
        if kind == "go":
            goal = sim.point_near_cursor() if step.get("to") == "cursor" else _point(sim, step)
            if goal is None:
                return False
            sim.direct(goal, "cursor" if step.get("to") == "cursor" else None,
                       int(GO_TIMEOUT * C.GAME_HZ))
            return True
        if kind == "look":
            at = step.get("at")
            look = "cursor" if at == "cursor" else _point(sim, at)
            if look is None:
                return False
            sim.direct(None, look, ticks, roam=True)
            return True
        if kind in ("meditate", "sleep"):
            head = sim.creature.head
            look = (head[0], head[1] + (60.0 if kind == "sleep" else -60.0))
            sim.direct(None, look, ticks)
            return True
        if kind == "follow":
            self._next_follow = now
            return True
        if kind == "wait":
            sim.clear_directive()
            return True
        return False

    def _finished(self, sim, step: dict, now: float) -> bool:
        kind = step.get("do")
        elapsed = now - self._started
        if kind == "say":
            near = (sim.cursor is not None
                    and C.dist(sim.cursor, sim.creature.chunk0) < 160.0)
            if near:
                self._say(sim, step)
                return True
            return elapsed >= SAY_WAIT_LIMIT
        if kind == "go":
            arrived = sim.wander.progress >= 1.0 and sim.last_body_speed < 1.0
            return arrived or not sim.directive_active or elapsed >= GO_TIMEOUT
        if kind == "follow":
            if now >= self._next_follow and sim.cursor is not None:
                goal = sim.point_near_cursor()
                if goal is not None and C.dist(goal, sim.wander.next) > 40.0:
                    sim.direct(goal, "cursor", int(3 * C.GAME_HZ))
                self._next_follow = now + 1.5
            return elapsed >= _seconds(step, 20.0)
        if kind in ("look", "meditate", "sleep"):
            return not sim.directive_active or elapsed >= _seconds(step, 20.0)
        return elapsed >= _seconds(step, 20.0)

    def _say(self, sim, step: dict) -> None:
        text = step.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        if self.say_fn is not None:
            self.say_fn(sim, text)
        else:
            sim.say(text.strip()[:SAY_MAX_CHARS])
