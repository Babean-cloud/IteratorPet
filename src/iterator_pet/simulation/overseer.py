"""监视者的行为：什么时候钻出来、从哪个墙洞钻、悬在哪、什么时候缩回去（dev/监视者/overseer-design.md §3.1）。

纯数据，不碰 Qt。坐标和距离都用调用方给的那套像素（桌面上是屏幕像素；仿真像素的话把常量乘上缩放再传）。

状态机：hidden → emerging → hovering → retracting → hidden。
``extended``（0 缩在墙里，1 全伸出）就是渲染层 ``OverseerGraphics.set_pose(extended=…)`` 要的量，
钻出 / 缩回只是把它在 0↔1 之间匀速走，触须会自己收回到身体上。

墙洞每次钻出时重新挑，约束（任何一条放不下就换相邻边，都不行取总体最优的）：
离滑车、离机械臂折线和缆线、离人偶头都要留距离——效果图里监视者正好钻在机械臂扫过的地方，只避滑车不够。
"""

from __future__ import annotations

import math
import random

TICK = 1.0 / 40.0                       # 和游戏、人偶仿真同频

EMERGE_TICKS = 24                       # ~0.6 s
RETRACT_TICKS = 20                      # ~0.5 s
IDLE_TICKS = 40 * 6                     # 没有投影要显示后再悬多久才缩回

CARRIAGE_CLEAR = 420.0                  # 墙洞离滑车（手臂伸开也够不着）
ARM_CLEAR = 130.0                       # 墙洞 / 悬停点离机械臂折线、缆线
HEAD_CLEAR = 200.0                      # 悬停点离人偶头
HOVER_DEPTH = (60.0, 85.0)              # 悬停点离墙洞往屏幕里多远（用户 2026-11-01：缩小后柄拉得太长，跟着缩）
HOVER_SWAY = 30.0                       # 沿墙方向的随机偏移
WALL_MARGIN = 80.0                      # 墙洞离边角至少这么远
TRIES = 40

EDGES = ("top", "bottom", "left", "right")
INWARD = {"top": (0.0, 1.0), "bottom": (0.0, -1.0), "left": (1.0, 0.0), "right": (-1.0, 0.0)}
ADJACENT = {"top": ("left", "right"), "bottom": ("left", "right"),
            "left": ("top", "bottom"), "right": ("top", "bottom")}

HIDDEN, EMERGING, HOVERING, RETRACTING = "hidden", "emerging", "hovering", "retracting"


def nearest_edge(point: tuple[float, float], bounds: tuple[float, float, float, float]) -> str:
    """点离哪条屏幕边最近（滑车所在的那一面）。bounds = (x, y, w, h)。"""
    x, y, w, h = bounds
    d = {"top": point[1] - y, "bottom": y + h - point[1], "left": point[0] - x, "right": x + w - point[0]}
    return min(d, key=d.get)


def dist_to_polyline(p: tuple[float, float], line) -> float:
    """点到折线的最短距离；折线只有一个点就是点距。"""
    pts = list(line)
    if not pts:
        return math.inf
    if len(pts) == 1:
        return math.dist(p, pts[0])
    best = math.inf
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
        best = min(best, math.dist(p, (a[0] + dx * t, a[1] + dy * t)))
    return best


def _candidate(edge: str, bounds, rng: random.Random):
    x, y, w, h = bounds
    inward = INWARD[edge]
    if edge in ("top", "bottom"):
        hole = (rng.uniform(x + WALL_MARGIN, x + w - WALL_MARGIN), y if edge == "top" else y + h)
        sway = (rng.uniform(-HOVER_SWAY, HOVER_SWAY), 0.0)
    else:
        hole = (x if edge == "left" else x + w, rng.uniform(y + WALL_MARGIN, y + h - WALL_MARGIN))
        sway = (0.0, rng.uniform(-HOVER_SWAY, HOVER_SWAY))
    depth = rng.uniform(*HOVER_DEPTH)
    body = (hole[0] + inward[0] * depth + sway[0], hole[1] + inward[1] * depth + sway[1])
    return hole, body


def _shortfall(hole, body, carriage, head, obstacles) -> float:
    """离各个约束还差多少（全满足 = 0）。取各项缺口里的最大值，好比较"哪个候选最不坏"。"""
    gap = max(0.0, CARRIAGE_CLEAR - math.dist(hole, carriage))
    gap = max(gap, HEAD_CLEAR - math.dist(body, head))
    for line in obstacles:
        gap = max(gap, ARM_CLEAR - dist_to_polyline(hole, line), ARM_CLEAR - dist_to_polyline(body, line))
    return gap


SIDE_ORDER = ("right", "left", "top")   # 第二只监视者：没被占的侧面，右边优先；底边留给输入法 / 终端


def pick_hole(bounds, carriage, head, obstacles, rng: random.Random, prefer=None, avoid=()):
    """挑钻出的墙洞。返回 (edge, hole, body, inward)；body 是悬停点。

    ``obstacles`` 是一组折线（机械臂关节、缆线节点），``carriage`` 是滑车位置。
    默认先在滑车所在那条边上试，放不下再试相邻两条，最后一条边也不行就取缺口最小的候选。
    ``prefer`` 给了就按它的顺序试，跳过 ``avoid`` 里的边（第二只监视者用）。
    """
    home = nearest_edge(carriage, bounds)
    edges = (home, *ADJACENT[home]) if prefer is None else prefer
    edges = [e for e in edges if e not in avoid] or [home]
    best = None
    for edge in edges:
        for _ in range(TRIES):
            hole, body = _candidate(edge, bounds, rng)
            short = _shortfall(hole, body, carriage, head, obstacles)
            if short <= 0.0:
                return edge, hole, body, INWARD[edge]
            if best is None or short < best[0]:
                best = (short, edge, hole, body)
    _, edge, hole, body = best
    return edge, hole, body, INWARD[edge]


class OverseerSim:
    def __init__(self, seed: int = 11, prefer=None):
        self.rng = random.Random(seed)
        self.prefer = prefer          # None = 滑车那一面；第二只传 SIDE_ORDER
        self.avoid: tuple = ()        # 钻出时避开的边（另一只占着的）
        self.phase = HIDDEN
        self.extended = 0.0
        self.edge = "top"
        self.hole = (0.0, 0.0)
        self.body = (0.0, 0.0)
        self.inward = (0.0, 1.0)
        self.wanted = False
        self._timer = 0               # emerging / retracting 的进度，hovering 的空闲计数

    # -- 外部接口 ------------------------------------------------------
    def want(self, wanted: bool) -> None:
        """有投影要显示就传 True；传 False 后悬停够 IDLE_TICKS 才缩回。"""
        self.wanted = wanted
        if wanted and self.phase == HOVERING:
            self._timer = 0

    def retract_now(self) -> None:
        """被拖拽 / 全屏勿扰 / 换角色时：立刻开始缩回（hidden 时无事发生）。"""
        self.wanted = False
        if self.phase in (EMERGING, HOVERING):
            self._begin_retract()

    @property
    def visible(self) -> bool:
        return self.phase != HIDDEN

    # -- 步进 ------------------------------------------------------------
    def step(self, bounds, carriage, head, obstacles=()) -> None:
        """每 tick 一次。bounds / carriage / head / obstacles 只在要钻出时用来挑墙洞。"""
        if self.phase == HIDDEN:
            if self.wanted:
                self.edge, self.hole, self.body, self.inward = pick_hole(
                    bounds, carriage, head, list(obstacles), self.rng, self.prefer, self.avoid)
                self.phase, self._timer = EMERGING, 0
        elif self.phase == EMERGING:
            self._timer += 1
            self.extended = min(1.0, self._timer / EMERGE_TICKS)
            if self._timer >= EMERGE_TICKS:
                self.phase, self._timer = HOVERING, 0
        elif self.phase == HOVERING:
            self._timer = 0 if self.wanted else self._timer + 1
            if self._timer >= IDLE_TICKS:
                self._begin_retract()
        elif self.phase == RETRACTING:
            self._timer += 1
            self.extended = max(0.0, 1.0 - self._timer / RETRACT_TICKS)
            if self._timer >= RETRACT_TICKS:
                self.phase, self._timer = HIDDEN, 0
                # 缩回途中又有投影来了：下一 tick 在 HIDDEN 里重新挑洞钻出

    def _begin_retract(self) -> None:
        # 从当前 extended 往回走，所以 timer 按已伸出的比例起算
        self.phase = RETRACTING
        self._timer = round((1.0 - self.extended) * RETRACT_TICKS)

    # -- 渲染要的快照 ----------------------------------------------------
    def snapshot(self) -> dict:
        return {"phase": self.phase, "extended": self.extended, "hole": self.hole,
                "body": self.body, "inward": self.inward, "edge": self.edge}
