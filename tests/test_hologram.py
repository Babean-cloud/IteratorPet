import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from iterator_pet.rendering import hologram as H  # noqa: E402

COLOR = (0.447, 0.902, 0.769)


def _app():
    return QGuiApplication.instance() or QGuiApplication([])


def _lit(img: QImage) -> int:
    return sum(1 for y in range(img.height()) for x in range(0, img.width(), 3) if img.pixelColor(x, y).alpha() > 0)


def test_panel_wraps_long_text_and_grows():
    _app()
    short = H.render_panel(300, [H.Line("你好")], COLOR)
    long = H.render_panel(300, [H.Line("很长的一段话" * 30)], COLOR)
    assert long.width() == 300 and long.height() > short.height() * 2
    assert _lit(long) > 0


def test_buttons_add_room_and_rects_fit_inside_panel():
    _app()
    plain = H.render_panel(400, [H.Line("要运行这条命令吗")], COLOR)
    buttons = [H.Button("deny", "拒绝", "cross"), H.Button("allow", "允许", "check")]
    with_buttons = H.render_panel(400, [H.Line("要运行这条命令吗")], COLOR, buttons)
    assert with_buttons.height() > plain.height()
    panel = QRectF(0, 0, with_buttons.width(), with_buttons.height())
    rects = H.button_rects(with_buttons.width(), with_buttons.height(), buttons)
    assert all(panel.contains(r) for r in rects) and not rects[0].intersects(rects[1])
    assert rects[0].left() < rects[1].left()                 # 右组按给定顺序从左到右


def test_left_and_right_groups_do_not_collide():
    _app()
    buttons = [H.Button("a", "只读", "read", side="left"), H.Button("b", "可写", "write", side="left"),
               H.Button("c", "夹", "folder", side="left"), H.Button("no", "取消", "cross"),
               H.Button("go", "开始", "check")]
    rects = H.button_rects(460, 200, buttons)
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert not a.intersects(b)
    assert rects[0].left() == H.PAD_X and rects[4].right() == 460 - H.PAD_X


def test_scroll_panel_scrolls_within_range():
    _app()
    body = "\n".join(f"第 {i} 行结果内容" for i in range(40))
    panel = H.ScrollPanel(360, H.Line("任务结果", "title"), body, "点一下展开", COLOR, view_lines=6)
    assert panel.max_offset > 0 and panel.image.height() < panel._body.height()
    a, _ = panel.frame(H.STRONG_TICKS + 1, offset=0.0)
    b, _ = panel.frame(H.STRONG_TICKS + 1, offset=60.0)
    assert a.size() == b.size() == panel.image.size() and a != b
    far, _ = panel.frame(H.STRONG_TICKS + 1, offset=10 ** 6)       # 超出范围夹到底，不崩
    assert far.size() == panel.image.size()
    short = H.ScrollPanel(360, H.Line("t", "title"), "一行", "h", COLOR, view_lines=6)
    assert short.max_offset == 0.0


def test_frame_animation_phases():
    _app()
    holo = H.Hologram(H.render_panel(300, [H.Line("监视者" * 20)], COLOR), seed=3)
    _, first = holo.frame(0)
    _, settled = holo.frame(H.STRONG_TICKS)
    assert first < 1.0 and settled == 1.0
    assert holo.frame(H.STRONG_TICKS)[0] is holo.frame(H.STRONG_TICKS + 50)[0]      # 平时是同一张缓存图
    assert holo.frame(5)[0] is holo.frame(5)[0]                                      # 强效果每步也缓存
    assert holo.frame(100, leaving=0)[1] == 1.0
    assert holo.frame(100, leaving=H.STRONG_TICKS)[1] == 0.0


def test_place_panel_stays_on_screen_and_away_from_head():
    for inward, body in (((0, 1), (600, 120)), ((0, -1), (600, 600)), ((1, 0), (120, 360)), ((-1, 0), (1160, 360))):
        rect = H.place_panel(470, 200, body, (640, 360), inward, (1280, 720))
        assert rect.left() >= 16 and rect.top() >= 16
        assert rect.right() <= 1280 - 16 and rect.bottom() <= 720 - 40
