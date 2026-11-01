"""监视者（Overseer）的画法，照游戏 ``OverseerGraphics`` + ``CoralBrain.Mycelium`` 的 IL 逐条移植。

坐标是"游戏像素"、屏幕朝向（y 向下）；调用方用 ``painter.scale`` 放大到桌面上。
公式出处、和原作不一样的地方见 dev/notes/overseer-design.md §2.4。

用法::

    g = OverseerGraphics(color, size=0.8, seed=3)
    g.set_pose(head=(..), root=(..), root_dir=(0, 1), look=(..), extended=1.0, facing=0.6)
    for _ in range(60): g.update()          # 每 1/40 秒一帧，和游戏一样
    g.draw(painter)                         # 触须、身体、眼睛（不含柔光）
    g.draw_glow(painter, scale)             # FlatLight 柔光，画在放大后的画面上
"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPolygonF, QRadialGradient

# 游戏 OverseerGraphics.MainColor（按 ownerIterator，写死在 IL 里）
MAIN_COLORS = {
    "pebbles": (0.447, 0.902, 0.769),     # 0 / 默认：五块卵石
    "past_moon": (1.0, 0.8, 0.3),         # 1：仰望皓月（Iggy 同色）
    "moon": (1.0, 0.8, 0.3),
    "green": (0.0, 1.0, 0.0),             # 2
    "red": (1.0, 0.2, 0.0),               # 3
    "white": (0.9, 0.95, 1.0),            # 4
    "purple": (0.56, 0.27, 0.68),         # 5
}
# 房间调色板 (0,2) 那一格。桌面没有调色板，取一个接近暗色桌面的值
EARTH = (0.08, 0.07, 0.10)
SEGMENTS = 10


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_map(v, a, b, x, y):
    t = 0.0 if a == b else min(1.0, max(0.0, (v - a) / (b - a)))
    return lerp(x, y, t)


def inv_lerp(a, b, v):
    return 0.0 if a == b else min(1.0, max(0.0, (v - a) / (b - a)))


def v_add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def v_mul(a, s):
    return (a[0] * s, a[1] * s)


def v_len(a):
    return math.hypot(a[0], a[1])


def v_norm(a):
    n = v_len(a)
    return (a[0] / n, a[1] / n) if n > 1e-6 else (0.0, 0.0)


def v_lerp(a, b, t):
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))


def perp(a):
    return (-a[1], a[0])


def dir_vec(a, b):
    return v_norm(v_sub(b, a))


def c_lerp(a, b, t):
    return tuple(lerp(x, y, t) for x, y in zip(a, b))


def slerp3(a, b, t):
    """Unity Vector3.Slerp：方向按球面插值，长度线性插值。"""
    la, lb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    if la < 1e-6 or lb < 1e-6:
        return tuple(lerp(x, y, t) for x, y in zip(a, b))
    ua, ub = [x / la for x in a], [x / lb for x in b]
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(ua, ub))))
    omega = math.acos(dot)
    if omega < 1e-4:
        u = [lerp(x, y, t) for x, y in zip(ua, ub)]
    else:
        s = math.sin(omega)
        u = [(math.sin((1 - t) * omega) * x + math.sin(t * omega) * y) / s for x, y in zip(ua, ub)]
    length = lerp(la, lb, t)
    return tuple(x * length for x in u)


def bezier(a, ca, b, cb, t):
    """RWCustom.Custom.Bezier(A, cA, B, cB, t)：A→B 的三次贝塞尔。"""
    u = 1 - t
    return (u ** 3 * a[0] + 3 * u * u * t * ca[0] + 3 * u * t * t * cb[0] + t ** 3 * b[0],
            u ** 3 * a[1] + 3 * u * u * t * ca[1] + 3 * u * t * t * cb[1] + t ** 3 * b[1])


def qc(rgb, a=1.0):
    c = QColor.fromRgbF(*(min(1.0, max(0.0, v)) for v in rgb[:3]))
    c.setAlphaF(min(1.0, max(0.0, a)))
    return c


class Mycelium:
    """一根触须。points[i] = [位置, 上一帧位置, 速度]，和游戏 Vector2[n,3] 一致。"""

    def __init__(self, length: float, start):
        self.length = length
        n = max(2, min(20, int(max(length, lerp(length, 40, 0.5)) / 15)))
        self.points = [[start, start, (0.0, 0.0)] for _ in range(n)]
        self.con_rad = length / n

    def step(self):
        """Mycelium.Update 的 Phase1 / Phase4（Burst job，DLL 里没有 IL）。

        按游戏里其它 rope 的写法近似：先 pos += vel、vel *= 0.999，再沿绳把相邻点拉回 con_rad。
        """
        for p in self.points:
            p[1] = p[0]
            p[0] = v_add(p[0], p[2])
            p[2] = v_mul(p[2], 0.999)
        for _ in range(3):
            for i in range(1, len(self.points)):
                a, b = self.points[i - 1][0], self.points[i][0]
                d = v_len(v_sub(b, a))
                if d > self.con_rad and d > 1e-6:
                    fix = v_mul(v_sub(b, a), (d - self.con_rad) / d)
                    if i == 1:
                        self.points[i][0] = v_sub(b, fix)
                    else:
                        self.points[i - 1][0] = v_add(a, v_mul(fix, 0.5))
                        self.points[i][0] = v_sub(b, v_mul(fix, 0.5))
        for p in self.points[1:]:
            p[2] = v_lerp(p[2], v_sub(p[0], p[1]), 0.5)


class OverseerGraphics:
    def __init__(self, color, size: float = 0.8, seed: int = 0):
        rng = random.Random(seed)
        self.color = tuple(color)
        self.size = size
        self.extended = 1.0
        self.facing = 0.0                 # 0 = 按距离自动看镜头，1 = 正对镜头（对话 / 投影时）
        self.head = (0.0, 0.0)
        self.root = (0.0, 0.0)
        self.root_dir = (0.0, 1.0)
        self.look = (0.0, 0.0)
        self.use_dir = (0.0, 0.0, 1.0)
        count = rng.randint(2, 6)
        length = lerp_map(size, 0.5, 1.0, 55, 80)
        self.stuck_at = lerp_map(size, 0.5, 1.0, 0.5, 0.2)
        self.color_from = lerp_map(size, 0.5, 1.0, 0.5, 0.3)
        self.mycelia = [Mycelium(length, (0.0, 0.0)) for _ in range(count)]
        self.con_rad = self.mycelia[0].con_rad / 2      # myceliaConRad：监视者把触须每节收成一半
        # myceliaMovements[i] = [当前, 起点, 终点, 进度, 速度]；0 号管整体旋转，1 号管张开程度
        self.movements = [[rng.random(), 0.0, rng.random(), 0.0, 0.02] for _ in range(2)]
        self._rng = rng
        self._placed = False

    # ---- 姿态 ----------------------------------------------------------------
    def set_pose(self, head, root, root_dir, look, extended=1.0, facing=0.0):
        self.head, self.root, self.root_dir = head, root, v_norm(root_dir)
        self.look, self.extended, self.facing = look, extended, facing
        d = v_sub(look, head)
        dist = v_len(d)
        to_look = (*v_norm(d), 0.0)
        t = max(inv_lerp(600, 0, dist), facing)
        self.use_dir = tuple(x * extended for x in slerp3(to_look, (0.0, 0.0, 1.0), t))
        if not self._placed:
            start = self.connection_pos()
            for m in self.mycelia:
                for p in m.points:
                    p[0] = p[1] = start
            self._placed = True

    # ---- OverseerGraphics 的几个分段函数 ---------------------------------------
    def ext_of(self, f):
        return max(0.0, self.extended) ** lerp(0.1, 4, f)

    def total_length(self):
        return v_len(v_sub(self.head, self.root))

    def pos_of(self, f):
        ud = self.use_dir
        s = self.size
        far = bezier(self.head,
                     v_sub(self.head, v_mul((ud[0], ud[1]), 15 * s * self.extended)),
                     self.root,
                     v_add(self.root, v_mul(self.root_dir,
                                            max(40 * s, self.total_length() * 0.85) * self.extended)),
                     f)
        # 游戏缩回去时沿 PositionOfZipProg（钻进墙里的那段路）走；桌面上墙洞就是根部
        return v_lerp(self.root, far, self.ext_of(f))

    def rad_of(self, f):
        s = self.size
        k = lerp_map(self.total_length(), 20 * s, 90 * s, 1.75, 0.35)
        return lerp(1 + math.sin(f * math.pi), lerp(5, 0, f ** k), self.ext_of(f))

    def color_of(self, f):
        main, earth = self.color, EARTH
        neutral = tuple((1 + e) / 2 for e in earth)
        hidden = tuple((m + b + e * 8) / 10 for m, b, e in zip(main, (0, 0, 1), earth)) + (0.5,)
        shown = c_lerp(main, c_lerp(neutral, earth, f * f), 0.5) + (1.0,)
        return c_lerp(hidden, shown, self.ext_of(f))

    def connection_pos(self):
        return self.pos_of(self.stuck_at)

    # ---- OverseerGraphics.Update 里管触须的那一段 --------------------------------
    def update(self):
        rng = self._rng
        for i, mv in enumerate(self.movements):
            mv[0] = lerp(mv[1], mv[2], _s_curve(mv[3], 0.5))
            mv[3] = min(1.0, mv[3] + mv[4])
            if rng.random() < 1 / (20 if i else 80) and mv[3] == 1.0:
                mv[1], mv[3] = mv[0], 0.0
                if i == 0:
                    mv[2] = mv[0] + rng.random() ** 3 * 0.8 * (1 if rng.random() < 0.5 else -1)
                    mv[4] = 0.01 / max(1e-3, abs(mv[1] - mv[2]))
                else:
                    mv[2] = rng.random()
                    mv[4] = 0.02 / max(1e-3, abs(mv[1] - mv[2]))
        ud, s, ext = self.use_dir, self.size, self.extended
        ur_x, ur_y = -ud[1] / math.tau, ud[0] / math.tau
        spread = self.movements[1][0]
        n = len(self.mycelia)
        conn = self.connection_pos()
        for j, m in enumerate(self.mycelia):
            m.step()
            m.con_rad = self.con_rad * ext * lerp(1, 2 - s, self.facing)
            ang = j / n * 360
            ring = v_mul((math.sin(math.radians(ang)), -math.cos(math.radians(ang))), lerp(25, 15, spread) * s)
            local = (ring[0], ring[1], spread * -25 * s - lerp_map(s, 0.5, 1, 5, 0))
            target = v_add(self.head, self._mycelia_to_2d(local, ur_x, ur_y, self.movements[0][0]))
            to_target = dir_vec(self.head, target)
            push = slerp3((-ud[0], -ud[1], 0.0), (*to_target, 0.0), 0.5)
            last = len(m.points) - 1
            for k, p in enumerate(m.points):
                t = k / last
                vel = v_mul(p[2], 0.9 * ext)                       # 本体不动，bodyVel = 0
                vel = v_add(vel, v_mul((push[0], push[1]), 1 - t))
                vel = v_add(vel, v_mul(v_sub(target, p[0]), 0.05 * math.sin(t * math.pi)))
                vel = v_add(vel, v_mul((ud[0], ud[1]), t * t * 0.5))
                p[2] = vel
                p[0] = v_lerp(p[0], conn, 1 - ext)
            m.points[0][0] = conn
            m.points[0][2] = (0.0, 0.0)

    def _mycelia_to_2d(self, pos, ur_x, ur_y, ur_z):
        x, y, z = (c * self.extended for c in pos)
        a = ur_z * math.tau
        x, y = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
        a = ur_x * math.tau
        y, z = y * math.cos(a) - z * math.sin(a), y * math.sin(a) + z * math.cos(a)
        a = ur_y * math.tau
        z, x = z * math.cos(a) - x * math.sin(a), z * math.sin(a) + x * math.cos(a)
        return (x, y)

    # ---- DrawSprites -----------------------------------------------------------
    def draw_glow(self, p: QPainter, scale: float, offset=(0.0, 0.0)):
        """FlatLight：Futile_White（16px）× 3.125 ≈ 半径 25 游戏像素，MainColor，α = 0.25 × extended。"""
        c = QPointF(offset[0] + self.head[0] * scale, offset[1] + self.head[1] * scale)
        r = 25 * scale
        g = QRadialGradient(c, r)
        g.setColorAt(0.0, qc(self.color, 0.25 * self.extended))
        g.setColorAt(1.0, qc(self.color, 0.0))
        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, r, r)
        p.restore()

    def draw(self, p: QPainter):
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        for m in self.mycelia:
            self._draw_mycelium(p, m)
        self._draw_body(p)
        self._draw_eye(p)
        p.restore()

    def _draw_mycelium(self, p: QPainter, m: Mycelium):
        """Mycelium.DrawSprites：长条网格，每点 4 个顶点、半宽 0.5；UpdateColor 从 colorFrom 起渐暗到青黑（末端渐隐，见下）。"""
        prev = self.connection_pos()
        n_verts = len(m.points) * 4
        tip_dark = _hsl2rgb(0.4888889, 0.5, 0.2)
        base = self.color_of(self.stuck_at)
        w = 0.5
        pairs = []                                  # (左, 右, 顶点序号)，相邻两对之间就是网格里的一个四边形
        for i, pt in enumerate(m.points):
            cur = pt[0]
            d = dir_vec(cur, prev)
            n = perp(d)
            k = v_len(v_sub(prev, cur)) / 5
            a, b = v_sub(prev, v_mul(d, k)), v_add(cur, v_mul(d, k))
            pairs.append((v_sub(a, v_mul(n, w)), v_add(a, v_mul(n, w)), i * 4))
            pairs.append((v_sub(b, v_mul(n, w)), v_add(b, v_mul(n, w)), i * 4 + 2))
            prev = cur
        for (l0, r0, i0), (l1, r1, _) in zip(pairs, pairs[1:]):
            f = i0 / (n_verts - 1)
            col = c_lerp(base[:3], tip_dark, inv_lerp(self.color_from, 1, f))
            # 游戏把末两个顶点写成纯蓝，但那截在游戏里扎进墙 / 地形里看不见；桌面没有墙，
            # 露出来就是一截突兀的深蓝。改成最后 1/4 渐隐成透明（用户 2026-11-01）。
            alpha = base[3] * (1.0 - inv_lerp(0.75, 1.0, f))
            p.setBrush(qc(col, alpha))
            p.drawPolygon(QPolygonF([QPointF(*l0), QPointF(*r0), QPointF(*r1), QPointF(*l1)]))

    def _draw_body(self, p: QPainter):
        prev = v_add(self.pos_of(1), dir_vec(self.pos_of(0.999), self.pos_of(1)))
        prev_rad = self.rad_of(1)
        pairs = []
        for i in range(SEGMENTS):
            f = 1 - i / (SEGMENTS - 1)
            cur, rad = self.pos_of(f), self.rad_of(f)
            d = dir_vec(cur, prev)
            n = perp(d)
            k = v_len(v_sub(prev, cur)) / 5
            a = v_sub(prev, v_mul(d, k))
            b = v_add(cur, v_mul(d, k))
            pairs.append((v_sub(a, v_mul(n, (rad + prev_rad) * 0.5)), v_add(a, v_mul(n, (rad + prev_rad) * 0.5)), f))
            pairs.append((v_sub(b, v_mul(n, rad)), v_add(b, v_mul(n, rad)), f))
            prev, prev_rad = cur, rad
        for (l0, r0, f0), (l1, r1, f1) in zip(pairs, pairs[1:]):
            col = self.color_of((f0 + f1) / 2)
            p.setBrush(qc(col, col[3]))
            p.drawPolygon(QPolygonF([QPointF(*l0), QPointF(*r0), QPointF(*r1), QPointF(*l1)]))

    def _draw_eye(self, p: QPainter):
        ud, ext = self.use_dir, self.extended
        h = self.head
        r0 = self.rad_of(0)
        rot = math.degrees(math.atan2(ud[1], ud[0])) - 90 if (ud[0] or ud[1]) else 0.0
        z = ud[2]

        def ellipse(center, rx, ry, rgb, alpha=1.0):
            p.save()
            p.translate(*center)
            p.rotate(rot)                               # 本地 y 轴 = 视线方向，同 Futile 的 rotation
            p.setBrush(qc(rgb, alpha))
            p.drawEllipse(QPointF(0, 0), rx, ry)
            p.restore()

        bulb = self.color_of(0)
        ellipse(h, r0, r0 * lerp(1.2, 1, z), bulb[:3], bulb[3])
        white_c = v_add(h, v_mul((ud[0], ud[1]), 4 * ext))
        wx, wy = r0 * 0.7, r0 * lerp(0.35, 0.7, z)
        ellipse(white_c, wx, wy, c_lerp(self.color_of(0.75)[:3], (0, 0, 1), 0.5))
        ellipse(v_add(h, v_mul((ud[0], ud[1]), 2.5 * ext)), wx * 0.75, wy * 0.75, (1, 1, 1))   # InnerGlow 没设色 = 白
        flat = (ud[0], ud[1])
        bend = v_mul(v_norm(flat), math.sin(v_len(flat) * math.pi) * 2)
        pupil = v_add(v_add(h, v_mul(flat, 2 * ext)), bend)
        p.setBrush(QColor(0, 0, 0, 128))
        p.drawRect(int(round(pupil[0] - 0.5)), int(round(pupil[1] - 0.5)), 1, 1)   # "pixel" 精灵，1×1


def _s_curve(x, k):
    """RWCustom.Custom.SCurve。"""
    x = min(1.0, max(0.0, x))
    if x < 0.5:
        return (x * 2) ** (1 / k) * 0.5 if k else 0.0
    return 1 - ((1 - x) * 2) ** (1 / k) * 0.5 if k else 1.0


def _hsl2rgb(h, s, l):
    import colorsys
    return colorsys.hls_to_rgb(h, l, s)
