"""监视者（Overseer）+ 全息投影的**静态效果图**。不改桌宠任何行为，只画图看风格。

    $env:PYTHONPATH = "src"; python tools/overseer_mockup.py      →  docs/overseer-mockup-*.png

监视者本体用 ``iterator_pet.rendering.overseer``（逐条移植自游戏 ``OverseerGraphics`` / ``Mycelium``）；
全息照 ``Hologram.shader``：
单色、按噪声左右错动、扫描线式闪缺。这里是静态一帧，错动 / 闪缺取一个"刚投出来"的样子。
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen,  # noqa: E402
                           QRadialGradient)
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from iterator_pet.config import load  # noqa: E402
from iterator_pet.rendering.compose import Compositor  # noqa: E402
from iterator_pet.rendering.overseer import MAIN_COLORS as COLORS, OverseerGraphics  # noqa: E402
from iterator_pet.simulation.state import SimulationState  # noqa: E402

PHYSICS_DT = 1 / 40
W, H, ZOOM = 1280, 720, 1.5
OVERSEER_ZOOM = 2.5          # 监视者比人偶放大一点，眼睛（游戏里直径 10 px）才看得清
UI_FONT = "Microsoft YaHei UI"
MONO = "Cascadia Mono"


def qcolor(rgb, a=1.0, mul=1.0):
    c = QColor.fromRgbF(min(1, rgb[0] * mul), min(1, rgb[1] * mul), min(1, rgb[2] * mul))
    c.setAlphaF(a)
    return c


def ui_symbol(name: str):
    """从本机游戏的 uiSprites 图集切一个符号（勾 / 叉 / 珍珠），切不到返回 None。"""
    try:
        from iterator_pet.assets_extractor import _crop, _load_ui_atlas

        install = load().install or None
        atlas, frames = _load_ui_atlas(Path(install)) if install else (None, None)
        image = _crop(atlas, frames, name) if atlas is not None else None
        if image is None:
            return None
        import io

        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return QImage.fromData(buffer.getvalue(), "PNG")
    except Exception:  # noqa: BLE001
        return None


def scene(character: str, seed: int):
    sim = SimulationState((0.0, 0.0, W / ZOOM, H / ZOOM), character=character, seed=seed)
    for _ in range(int(7 / PHYSICS_DT)):
        sim.step(PHYSICS_DT)
    state = sim.interpolate(1.0)
    state.halo_sparks = []          # 静态一帧里电光会被拍成横贯全屏的长弧，效果图里先不画
    state.halo_white = 0.0          # 光环"闪白"那一下在静帧里像个实心圆盘
    image = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#15121a"))
    painter = QPainter(image)
    # 假装是桌面：一个暗色的编辑器窗口
    painter.fillRect(QRectF(60, 50, 760, 470), QColor("#1f1d24"))
    painter.fillRect(QRectF(60, 50, 760, 28), QColor("#2a2731"))
    painter.setPen(QColor("#5b5766"))
    painter.setFont(QFont(MONO, 10))
    for i, line in enumerate(["def build(out_dir):", "    args = [sys.executable, '-m', 'PyInstaller',",
                              "    subprocess.run(args, check=True)", "", "# TODO: prune Qt runtime"]):
        painter.drawText(QPointF(80, 108 + i * 20), line)
    low = QImage(math.ceil(W / ZOOM), math.ceil(H / ZOOM), QImage.Format.Format_ARGB32_Premultiplied)
    low.fill(Qt.GlobalColor.transparent)
    Compositor().render(low, state, 1.0, antialias=False, shadow=True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    painter.drawImage(QRectF(0, 0, low.width() * ZOOM, low.height() * ZOOM), low, QRectF(low.rect()))
    painter.end()
    base = state.base_point
    return image, (base[0] * ZOOM, base[1] * ZOOM), (state.head[0] * ZOOM, state.head[1] * ZOOM)


def edge_of(point):
    x, y = point
    d = {"top": y, "bottom": H - y, "left": x, "right": W - x}
    return min(d, key=d.get)


def anchor_away_from(base, edge, rng):
    """监视者钻出来的墙洞：同一条边上随机挑，离滑车至少 420 px（手臂伸开也够不着）；都不够就取最远的。"""
    cands = []
    for _ in range(50):
        if edge in ("top", "bottom"):
            p = (rng.uniform(80, W - 80), 0.0 if edge == "top" else float(H))
        else:
            p = (0.0 if edge == "left" else float(W), rng.uniform(80, H - 80))
        if math.dist(p, base) > 420:
            return p
        cands.append(p)
    return max(cands, key=lambda q: math.dist(q, base))


def draw_overseer(p: QPainter, anchor, body, inward, color, look, seed):
    """照游戏 OverseerGraphics 画（见 rendering/overseer.py）：低分辨率画好再按 OVERSEER_ZOOM 最近邻放大。"""
    g = OverseerGraphics(color, size=0.85, seed=seed)
    z = OVERSEER_ZOOM
    root = ((anchor[0] - inward[0] * 8) / z, (anchor[1] - inward[1] * 8) / z)   # 根在屏幕外一点 = 墙里
    for _ in range(80):                                     # 跑两秒让触须摆好
        g.set_pose((body[0] / z, body[1] / z), root, inward, (look[0] / z, look[1] / z),
                   extended=1.0, facing=0.55)
        g.update()
    g.draw_glow(p, z)
    layer = QImage(math.ceil(W / z), math.ceil(H / z), QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(Qt.GlobalColor.transparent)
    lp = QPainter(layer)
    lp.setRenderHint(QPainter.RenderHint.Antialiasing)
    g.draw(lp)
    lp.end()
    p.save()
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    p.drawImage(QRectF(0, 0, layer.width() * z, layer.height() * z), layer, QRectF(layer.rect()))
    p.restore()


def hologram(width: int, lines: list[tuple], color, symbols=()) -> QImage:
    """全息面板：单色、字发光；之后再整体加错动和扫描线。lines: (文字, 字体, 透明度, 是否警告)。"""
    rows = []
    for text, font, alpha, warn in lines:
        fm = QFontMetrics(font)
        rows.append((text, font, alpha, warn, fm.height() + 4))
    height = sum(r[4] for r in rows) + 28 + (54 if symbols else 0)
    img = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    # 很淡的投影底 + 四角括线（原作全息没有框，这里只给角标，保证中文压在花桌面上也读得清）
    p.fillRect(QRectF(0, 0, width, height), qcolor(color, 0.07))
    p.setPen(QPen(qcolor(color, 0.8), 1.4))
    for (x, y, dx, dy) in ((0, 0, 1, 1), (width, 0, -1, 1), (0, height, 1, -1), (width, height, -1, -1)):
        p.drawLine(QPointF(x, y), QPointF(x + dx * 14, y))
        p.drawLine(QPointF(x, y), QPointF(x, y + dy * 14))
    y = 14
    for text, font, alpha, warn, h in rows:
        c = (1.0, 0.35, 0.25) if warn else color
        p.setFont(font)
        p.setPen(qcolor(c, 0.25 * alpha))                 # 发光：错开一像素再画一遍
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            p.drawText(QRectF(16 + dx, y + dy, width - 32, h), Qt.AlignmentFlag.AlignLeft, text)
        p.setPen(qcolor(c, alpha, 1.15))
        p.drawText(QRectF(16, y, width - 32, h), Qt.AlignmentFlag.AlignLeft, text)
        y += h
    if symbols:
        x = width - 16 - len(symbols) * 52
        for sym, label, hot in symbols:
            rect = QRectF(x, y + 8, 40, 40)
            p.setPen(QPen(qcolor(color, 0.9 if hot else 0.45), 1.4))
            p.setBrush(qcolor(color, 0.22 if hot else 0.06))
            p.drawEllipse(rect)
            if sym is not None:
                tinted = sym.convertToFormat(QImage.Format.Format_ARGB32)
                pp = QPainter(tinted)
                pp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                pp.fillRect(tinted.rect(), qcolor(color, 1.0, 1.15))
                pp.end()
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                s = 22
                p.drawImage(QRectF(rect.center().x() - s / 2, rect.center().y() - s / 2, s, s), tinted)
            else:
                p.setPen(QPen(qcolor(color, 1.0), 2.4))
                c = rect.center()
                p.drawLine(QPointF(c.x() - 7, c.y() - 7), QPointF(c.x() + 7, c.y() + 7))
                p.drawLine(QPointF(c.x() + 7, c.y() - 7), QPointF(c.x() - 7, c.y() + 7))
            p.setPen(qcolor(color, 0.7))
            p.setFont(QFont(UI_FONT, 8))
            p.drawText(QRectF(rect.x() - 10, rect.bottom() + 1, 60, 14), Qt.AlignmentFlag.AlignHCenter, label)
            x += 52
    p.end()
    return img


def holo_effect(img: QImage, rng, jitter_rows=(), scan=3) -> QImage:
    """Hologram.shader 的观感：几段行左右错开、隔行压暗、零星缺行。"""
    out = QImage(img.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    y = 0
    while y < img.height():
        h = rng.randint(2, 9)
        dx = 0
        if any(a <= y < b for a, b in jitter_rows):
            dx = rng.choice([-6, -4, -3, 3, 4, 7])
        if rng.random() < 0.035:
            y += h
            continue                                   # 闪缺
        p.drawImage(QPointF(dx, y), img, QRectF(0, y, img.width(), h))
        y += h
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    for sy in range(0, out.height(), scan):
        p.fillRect(QRectF(0, sy, out.width(), 1), QColor(0, 0, 0, 70))
    p.end()
    return out


def place_panel(w, h, body, head, inward) -> QRectF:
    """投影放在监视者往屏幕里那一侧，挑离人偶最远、又不出屏幕的位置。"""
    candidates = []
    for side in (-1, 1):
        if inward[1] != 0:              # 上 / 下边：投影在它上方或下方，左右错开
            x = body[0] + side * 30 - (w if side < 0 else 0)
            y = body[1] + inward[1] * 30 - (h if inward[1] < 0 else 0)
        else:                           # 左 / 右边：投影在它旁边，上下错开
            x = body[0] + inward[0] * 30 - (w if inward[0] < 0 else 0)
            y = body[1] + side * 20 - (h if side < 0 else 0)
        x = min(max(x, 16), W - w - 16)
        y = min(max(y, 16), H - h - 40)
        candidates.append(QRectF(x, y, w, h))
    return max(candidates, key=lambda r: math.dist((r.center().x(), r.center().y()), head))


def beam(p: QPainter, eye, rect: QRectF, color):
    """监视者的眼到投影两角的光束（原作投影时眼睛和全息之间有一道淡光）。"""
    path = QPainterPath(QPointF(*eye))
    path.lineTo(rect.topLeft())
    path.lineTo(rect.bottomLeft())
    path.closeSubpath()
    near = rect.left() > eye[0]
    if not near:
        path = QPainterPath(QPointF(*eye))
        path.lineTo(rect.topRight())
        path.lineTo(rect.bottomRight())
        path.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(qcolor(color, 0.06))
    p.drawPath(path)


def compose(character: str, seed: int, out: Path, build_panel, caption: str):
    rng = random.Random(seed)
    image, base, head = scene(character, seed)
    color = COLORS[character]
    edge = edge_of(base)
    anchor = anchor_away_from(base, edge, rng)
    inward = {"top": (0, 1), "bottom": (0, -1), "left": (1, 0), "right": (-1, 0)}[edge]
    body = (anchor[0] + inward[0] * 120 + (rng.uniform(-30, 30) if inward[0] == 0 else 0),
            anchor[1] + inward[1] * 120 + (rng.uniform(-30, 30) if inward[1] == 0 else 0))
    panel = holo_effect(build_panel(color), rng, jitter_rows=[(2, 12)])
    rect = place_panel(panel.width(), panel.height(), body, head, inward)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    beam(p, body, rect, color)
    draw_overseer(p, anchor, body, inward, color, (rect.center().x(), rect.center().y()), seed)
    p.drawImage(rect.topLeft(), panel)
    p.setPen(QColor(200, 200, 210, 160))
    p.setFont(QFont(UI_FONT, 10))
    p.drawText(QRectF(14, H - 30, W - 28, 22), Qt.AlignmentFlag.AlignLeft, caption)
    p.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out))
    print("saved", out, "edge", edge, "carriage", [round(v) for v in base], "hole", [round(v) for v in anchor])


def approval_panel(color):
    check, cross = ui_symbol("Menu_Symbol_CheckBox"), None
    title = QFont(UI_FONT, 12, QFont.Weight.DemiBold)
    body = QFont(UI_FONT, 11)
    mono = QFont(MONO, 10)
    return hologram(470, [
        ("要运行一条命令", title, 1.0, False),
        ("为了：统计构建日志里的报错次数", body, 0.85, False),
        ("python tools/count_errors.py 接活测试\\构建日志.log", mono, 1.0, False),
        ("会运行脚本", body, 0.95, True),
    ], color, symbols=[(cross, "拒绝", False), (check, "允许", True)])


def thought_panel(color):
    title = QFont(UI_FONT, 12, QFont.Weight.DemiBold)
    body = QFont(UI_FONT, 11)
    return hologram(430, [
        ("读完了：构建日志.log", title, 1.0, False),
        ("打包两次都失败，停在 packaging/build.py 第 68 行：", body, 0.95, False),
        ("找不到 assets/icon.ico。图标被挪走了，", body, 0.95, False),
        ("或者路径还是旧的。把它放回去再打一次就好。", body, 0.75, False),
        ("▼  鼠标停住暂停滚动 · 点一下展开全文", QFont(UI_FONT, 8), 0.55, False),
    ], color)


def closeup(out: Path, zoom=6):
    """特写：同一只监视者三种视线（侧看 / 半侧 / 正对镜头）+ 钻出一半，两种颜色各一排。"""
    cols = [("侧看", 0.0, 1.0), ("半侧", 0.5, 1.0), ("正对（投影时）", 1.0, 1.0), ("钻出一半", 0.3, 0.45)]
    cw, ch = 70, 64
    img = QImage(cw * len(cols) * zoom, ch * 2 * zoom, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#15121a"))
    p = QPainter(img)
    for row, name in enumerate(("pebbles", "past_moon")):
        for col, (label, facing, ext) in enumerate(cols):
            ox, oy = col * cw, row * ch
            g = OverseerGraphics(COLORS[name], size=0.85, seed=5 + col)
            head, root = (ox + 38, oy + 30), (ox - 2, oy + ch - 4)
            for _ in range(80):
                g.set_pose(head, root, (0.6, -1), (ox + cw + 200, oy + 10), extended=ext, facing=facing)
                g.update()
            p.save()
            p.setClipRect(QRectF(ox * zoom, oy * zoom, cw * zoom, ch * zoom))
            g.draw_glow(p, zoom)
            layer = QImage(img.width() // zoom, img.height() // zoom, QImage.Format.Format_ARGB32_Premultiplied)
            layer.fill(Qt.GlobalColor.transparent)
            lp = QPainter(layer)
            lp.setRenderHint(QPainter.RenderHint.Antialiasing)
            g.draw(lp)
            lp.end()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            p.drawImage(QRectF(0, 0, img.width(), img.height()), layer, QRectF(layer.rect()))
            p.restore()
            p.setPen(QColor(200, 200, 210, 170))
            p.setFont(QFont(UI_FONT, 11))
            p.drawText(QRectF(ox * zoom + 8, (oy + ch) * zoom - 28, cw * zoom, 24), Qt.AlignmentFlag.AlignLeft, label)
    p.end()
    image_out = out
    img.save(str(image_out))
    print("saved", image_out)


if __name__ == "__main__":
    docs = ROOT / "docs"
    closeup(docs / "overseer-closeup.png")
    compose("pebbles", 3, docs / "overseer-mockup-pebbles.png", approval_panel,
            "效果图 · 五块卵石的监视者（游戏里写死的青绿）从滑车所在那条边、避开滑车处钻出来，投出命令批准")
    compose("past_moon", 11, docs / "overseer-mockup-moon.png", thought_panel,
            "效果图 · 仰望皓月的监视者（游戏里写死的黄，同 Iggy）投出长结果，慢速上滚")

