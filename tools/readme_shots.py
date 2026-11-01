"""README 配图：用**现在的**渲染代码画几帧静态图（人偶、监视者、全息面板、顶部字幕都是桌宠里同一套代码）。

    $env:PYTHONPATH = "src"; python tools/readme_shots.py      →  docs/readme-*.png

不读任何游戏素材（人偶按移植的程序化画法画），图可以放进仓库。桌面背景是画出来的假窗口。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from iterator_pet.rendering import hologram as H  # noqa: E402
from iterator_pet.rendering.bubble import draw_bubble_world  # noqa: E402
from iterator_pet.rendering.compose import Compositor  # noqa: E402
from iterator_pet.rendering.overseer import MAIN_COLORS as COLORS, OverseerGraphics  # noqa: E402
from iterator_pet.rendering.overseer_layer import SIZE  # noqa: E402
from iterator_pet.simulation.state import SimulationState  # noqa: E402

DT = 1 / 40
W, HGT, ZOOM = 1280, 720, 1.5
OVERSEER_ZOOM = 2.5
UI, MONO = "Microsoft YaHei UI", "Cascadia Mono"
DOCS = ROOT / "docs"


def desktop(p: QPainter, light_doc: bool) -> None:
    """假桌面：深色壁纸 + 一个编辑器窗口 + （可选）一个白底文档窗口，看投影在深浅两种底上的样子。"""
    p.fillRect(QRectF(0, 0, W, HGT), QColor("#1b1922"))
    p.fillRect(QRectF(40, 40, 700, 430), QColor("#1f1d24"))
    p.fillRect(QRectF(40, 40, 700, 28), QColor("#2a2731"))
    p.setPen(QColor("#6b6776"))
    p.setFont(QFont(MONO, 10))
    code = ["def save(path, data):", "    with open(path, 'w') as f:", "        json.dump({'a': data['a']}, f)",
            "", "def test_save(tmp_path):", "    save(tmp_path / 'x.json', {'a': 1, 'b': 2})",
            "    assert load(tmp_path / 'x.json') == {'a': 1, 'b': 2}"]
    for i, line in enumerate(code):
        p.drawText(QPointF(60, 96 + i * 20), line)
    if light_doc:
        p.fillRect(QRectF(760, 120, 480, 520), QColor("#f4f3ef"))
        p.fillRect(QRectF(760, 120, 480, 28), QColor("#dedcd6"))
        p.setPen(QColor("#4a4a4a"))
        p.setFont(QFont(UI, 11))
        for i in range(7):
            p.drawText(QPointF(784, 180 + i * 26), "多媒体技术 第三讲 · 图像压缩与变换编码"[: 8 + (i * 7) % 12])


def pet(character: str, seed: int, settle: float = 7.0):
    sim = SimulationState((0.0, 0.0, W / ZOOM, HGT / ZOOM), character=character, seed=seed)
    for _ in range(int(settle / DT)):
        sim.step(DT)
    state = sim.interpolate(1.0)
    state.halo_sparks = []              # 静帧里电光会被拍成长弧
    state.halo_white = 0.0
    return sim, state


def draw_pet(p: QPainter, state) -> None:
    low = QImage(math.ceil(W / ZOOM), math.ceil(HGT / ZOOM), QImage.Format.Format_ARGB32_Premultiplied)
    low.fill(Qt.GlobalColor.transparent)
    Compositor().render(low, state, 1.0, antialias=False, shadow=True)
    p.save()
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    p.drawImage(QRectF(0, 0, low.width() * ZOOM, low.height() * ZOOM), low, QRectF(low.rect()))
    p.restore()


def overseer(p: QPainter, anchor, inward, body, look, color, seed) -> None:
    g = OverseerGraphics(color, size=SIZE, seed=seed)
    z = OVERSEER_ZOOM
    root = ((anchor[0] - inward[0] * 8) / z, (anchor[1] - inward[1] * 8) / z)
    for _ in range(80):
        g.set_pose((body[0] / z, body[1] / z), root, inward, (look[0] / z, look[1] / z), extended=1.0, facing=0.55)
        g.update()
    g.draw_glow(p, z)
    layer = QImage(math.ceil(W / z), math.ceil(HGT / z), QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(Qt.GlobalColor.transparent)
    lp = QPainter(layer)
    lp.setRenderHint(QPainter.RenderHint.Antialiasing)
    g.draw(lp)
    lp.end()
    p.save()
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    p.drawImage(QRectF(0, 0, layer.width() * z, layer.height() * z), layer, QRectF(layer.rect()))
    p.restore()


def panel(p: QPainter, holo: H.Hologram, rect: QRectF, eye, color, tick: int = 90) -> None:
    H.beam(p, eye, rect, color)
    image, _ = holo.frame(tick)
    p.drawImage(rect.topLeft(), image)


def subtitle(p: QPainter, sim, text: str) -> None:
    sim.say(text)
    for _ in range(len(text) * 3 + 10):          # 打字机把字全打出来
        sim.dialogue.step()
    view = sim.dialogue_view()
    draw_bubble_world(p, text=view["text"], size_fac=view["size_fac"], anchor_sim=view["anchor"], scale=ZOOM,
                      screen=(float(W), float(HGT)), below_sim=view.get("below"), top=True)


def shot_pebbles() -> None:
    """五块卵石：顶部字幕 + 右边钻出的监视者投出命令批准。"""
    color = COLORS["pebbles"]
    sim, state = pet("pebbles", 3)
    img = QImage(W, HGT, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    desktop(p, light_doc=False)
    draw_pet(p, state)
    buttons = [H.Button("deny", "拒绝", "cross"), H.Button("allow", "允许", "check", hot=True)]
    holo = H.Hologram(H.render_panel(470, [
        H.Line("要运行一条命令", "title", icon="triangle"),
        H.Line("为了：跑一遍 test_io 看看存盘的问题", "body", 0.85),
        H.Line("python -m pytest tests/test_io.py -q", "mono"),
        H.Line("会运行测试脚本（只读）", "body", 0.95)], color, buttons), seed=4, color=color)
    anchor, inward = (float(W), 300.0), (-1, 0)
    body = (W - 110.0, 300.0)
    rect = QRectF(body[0] - 40 - holo.image.width(), 330, holo.image.width(), holo.image.height())
    panel(p, holo, rect, body, color)
    overseer(p, anchor, inward, body, (rect.center().x(), rect.center().y()), color, 3)
    subtitle(p, sim, "存的时候 b 丢了，save 只写了一个字段。")
    p.end()
    img.save(str(DOCS / "readme-pebbles.png"))


def shot_moon() -> None:
    """仰望皓月：白底文档上，两只监视者——任务进度 + 看屏幕。"""
    color = COLORS["past_moon"]
    sim, state = pet("past_moon", 11)
    img = QImage(W, HGT, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    desktop(p, light_doc=True)
    draw_pet(p, state)
    task = H.Hologram(H.render_panel(420, [
        H.Line("演算中：整理 MM 课件讲义", "title", icon="spin"),
        H.Line("第 14 步 · 读完 第三讲.pptx（42 页）", "body", 0.9),
        H.Line("正在写 讲义/第三讲-变换编码.md", "body", 0.9),
        H.Line("联网：查了 2 个来源", "body", 0.7)], color), seed=7, color=color)
    look = H.Hologram(H.render_panel(360, [
        H.Line("在看：第三讲.pptx", "title", icon="eye"),
        H.Line("DCT 那页的公式有点挤，讲义里我拆成了两步。", "body", 0.9)], color), seed=9, color=color)
    body1, body2 = (W - 120.0, 420.0), (660.0, 130.0)
    r1 = QRectF(body1[0] - 40 - task.image.width(), 440, task.image.width(), task.image.height())
    r2 = QRectF(body2[0] - 40 - look.image.width(), 150, look.image.width(), look.image.height())
    panel(p, look, r2, body2, color)
    overseer(p, (body2[0], 0.0), (0, 1), body2, (r2.center().x(), r2.center().y()), color, 21)
    panel(p, task, r1, body1, color)
    overseer(p, (float(W), body1[1]), (-1, 0), body1, (r1.center().x(), r1.center().y()), color, 5)
    subtitle(p, sim, "讲义写到第二节了。你先去吃饭。")
    p.end()
    img.save(str(DOCS / "readme-moon.png"))


def shot_holo() -> None:
    """全息面板特写：同一块面板在深色和白色背景上（暗底不挖洞，白底上不闪白块）。"""
    color = COLORS["pebbles"]
    holo = H.Hologram(H.render_panel(400, [
        H.Line("任务完成", "title", icon="check"),
        H.Line("找到了：save() 只写了 'a'，漏了 'b'。", "body"),
        H.Line("改法写在对话窗口里。", "body", 0.8)], color), seed=2, color=color)
    w, h = holo.image.width(), holo.image.height()
    img = QImage(2 * w + 90, h + 60, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.fillRect(QRectF(0, 0, w + 45, img.height()), QColor("#16141c"))
    p.fillRect(QRectF(w + 45, 0, w + 45, img.height()), QColor("#f4f3ef"))
    frame, _ = holo.frame(120)
    p.drawImage(QPointF(30, 30), frame)
    p.drawImage(QPointF(w + 60, 30), frame)
    p.end()
    img.save(str(DOCS / "readme-hologram.png"))


if __name__ == "__main__":
    DOCS.mkdir(exist_ok=True)
    shot_pebbles()
    shot_moon()
    shot_holo()
    print("saved docs/readme-*.png")
