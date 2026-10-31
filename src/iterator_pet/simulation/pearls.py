"""收着的珍珠在人偶周围随意漂；人偶走了，它们各自慢半拍地跟上去；正在读的那颗飘到头前面、亮起来。

纯仿真（仿真像素，每 tick 一步），不碰 Qt。思路借了游戏里神经元飞虫（OracleSwarmer）的那套：
**有一个慢慢转向的朝向 ``heading``，沿朝向推一把，再乘摩擦**——所以轨迹是弯的、会加减速，
不像弹簧那样直来直去。每颗珍珠还有自己的"性格"：

* ``lag``     跟人偶的迟滞：自己的锚点按这个比例追人偶，越小越"慢半拍"；
* ``agility`` 推力（加速度），``top`` 最高速度：有的轻快，有的懒洋洋；
* ``home``    相对锚点的落脚点，每隔几秒随机换一个（椭圆环带里），所以不是绕固定轨道转圈；
* ``wobble``  朝向每 tick 随机抖一点，轨迹不会是完美的弧。

离落脚点远（人偶刚飘走）时推力和限速都放大，赶回来；到了就只剩小幅游荡。
"""

from __future__ import annotations

import math
import random

MAX_SHOWN = 5
RING_INNER, RING_OUTER = 20.0, 46.0     # 落脚点的环带半径（仿真像素）
RING_SQUASH = 0.72                      # 竖向压扁一点，像绕着身体
HOME_EVERY = (90, 260)                  # 多少 tick 换一次落脚点
FRICTION = 0.93
TURN = 0.075                            # 朝向每 tick 最多转多少弧度
WOBBLE = 0.22
SEPARATION = 9.0                        # 两颗珍珠靠这么近就互相推开
READ_AHEAD = 13.0                       # 读的时候在头前面多远
GLOW_RATE = 0.05


class PearlOrbit:
    def __init__(self, seed: int = 7):
        self.items: list[dict] = []
        self.reading: str | None = None
        self.rng = random.Random(seed)

    def _new(self, pearl_id: str, start: tuple[float, float]) -> dict:
        r = self.rng
        return {
            "id": pearl_id, "pos": start, "vel": (0.0, 0.0), "glow": 1.0,
            "anchor": start, "heading": r.uniform(0, 2 * math.pi),
            "lag": r.uniform(0.025, 0.07), "agility": r.uniform(0.07, 0.13),
            "top": r.uniform(0.9, 1.6), "home": self._pick_home(), "timer": r.randint(*HOME_EVERY),
        }

    def _pick_home(self) -> tuple[float, float]:
        angle = self.rng.uniform(0, 2 * math.pi)
        radius = math.sqrt(self.rng.uniform(RING_INNER ** 2, RING_OUTER ** 2))
        return math.cos(angle) * radius, math.sin(angle) * radius * RING_SQUASH

    def set_pearls(self, pearls: list[tuple[str, tuple[int, int, int]]],
                   spawn: tuple[float, float] | None = None,
                   center: tuple[float, float] | None = None) -> None:
        """换成这一组（最近的几颗）。已经在的保留位置和性格；新来的从 ``spawn``（松手处）出现。"""
        old = {p["id"]: p for p in self.items}
        fresh = []
        for pearl_id, color in pearls[-MAX_SHOWN:]:
            item = old.get(pearl_id) or self._new(pearl_id, spawn or center or (0.0, 0.0))
            if center is not None and pearl_id not in old:
                item["anchor"] = center
            item["color"] = color
            fresh.append(item)
        self.items = fresh

    def step(self, center: tuple[float, float], head: tuple[float, float],
             look_dir: tuple[float, float]) -> None:
        for p in self.items:
            reading = p["id"] == self.reading
            # 各自慢半拍的锚点：人偶走了，锚点按自己的 lag 追，珍珠再追锚点 —— 两级延迟
            ax, ay = p["anchor"]
            p["anchor"] = (ax + (center[0] - ax) * p["lag"], ay + (center[1] - ay) * p["lag"])
            p["timer"] -= 1
            if p["timer"] <= 0:
                p["home"] = self._pick_home()
                p["timer"] = self.rng.randint(*HOME_EVERY)
            if reading:
                target = (head[0] + look_dir[0] * READ_AHEAD, head[1] + look_dir[1] * READ_AHEAD + 6.0)
            else:
                target = (p["anchor"][0] + p["home"][0], p["anchor"][1] + p["home"][1])
            dx, dy = target[0] - p["pos"][0], target[1] - p["pos"][1]
            dist = math.hypot(dx, dy)
            # 朝向慢慢转向目标，外加随机抖动（离得越近抖得越明显 = 在落脚点附近游荡）
            want = math.atan2(dy, dx)
            turn = (want - p["heading"] + math.pi) % (2 * math.pi) - math.pi
            p["heading"] += max(-TURN, min(TURN, turn)) + self.rng.uniform(-WOBBLE, WOBBLE) * (
                0.25 if reading else 1.0) * min(1.0, 12.0 / (dist + 4.0))
            urgency = min(3.0, 0.35 + dist / 28.0)        # 离得远就使劲赶
            if reading:
                urgency = min(4.0, 0.6 + dist / 14.0)
            push = p["agility"] * urgency
            vx = p["vel"][0] * FRICTION + math.cos(p["heading"]) * push
            vy = p["vel"][1] * FRICTION + math.sin(p["heading"]) * push
            if reading:                                    # 读的那颗要稳稳停在手边，不绕圈
                pull = 0.03 * min(1.0, 12.0 / max(dist, 1e-6))   # 拉力封顶，免得起步猛地一拽
                vx, vy = vx * 0.8 + dx * pull, vy * 0.8 + dy * pull
            top = p["top"] * max(1.0, urgency)
            speed = math.hypot(vx, vy)
            if speed > top:                                # 超速时缓刹（每 tick 最多减 8%），不硬截
                k = max(top / speed, 0.92)
                vx, vy = vx * k, vy * k
            p["vel"] = (vx, vy)
        # 互相推开一点，别叠成一颗
        for i, a in enumerate(self.items):
            for b in self.items[i + 1:]:
                dx, dy = b["pos"][0] - a["pos"][0], b["pos"][1] - a["pos"][1]
                d = math.hypot(dx, dy)
                if 1e-6 < d < SEPARATION:
                    f = (SEPARATION - d) / SEPARATION * 0.15
                    a["vel"] = (a["vel"][0] - dx / d * f, a["vel"][1] - dy / d * f)
                    b["vel"] = (b["vel"][0] + dx / d * f, b["vel"][1] + dy / d * f)
        for p in self.items:
            p["pos"] = (p["pos"][0] + p["vel"][0], p["pos"][1] + p["vel"][1])
            goal = 1.0 if p["id"] == self.reading else 0.25
            p["glow"] += (goal - p["glow"]) * GLOW_RATE

    def snapshot(self) -> tuple:
        """给 RenderState：((x, y), (r, g, b), glow) 元组。"""
        return tuple((p["pos"], p["color"], round(p["glow"], 3)) for p in self.items)
