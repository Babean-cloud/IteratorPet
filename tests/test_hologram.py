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


def _alive(img: QImage) -> int:
    """图里还留着的不透明度总和，隔点采样（暗底整块淡入、字被溶出来，两样都算进去）。"""
    return sum(img.pixelColor(x, y).alpha() for y in range(0, img.height(), 2) for x in range(0, img.width(), 2))


def test_backdrop_is_never_punched_through_once_settled():
    """溶解只挖字和边，不挖暗底：白色桌面上不该透出一闪一闪的白块（用户 2026-11-01）。"""
    _app()
    holo = H.Hologram(H.render_panel(300, [H.Line("监视者" * 20)], COLOR), seed=3, color=COLOR)
    img = holo.frame(H.STRONG_TICKS + 40)[0]
    w, h = img.width(), img.height()
    inside = [img.pixelColor(x, y).alpha() for y in range(20, h - 20, 3) for x in range(20, w - 20, 3)]
    assert min(inside) >= int(255 * H.BACKING_ALPHA) - 2


def test_frame_dissolves_in_and_out_instead_of_shifting_rows():
    _app()
    holo = H.Hologram(H.render_panel(300, [H.Line("监视者" * 20)], COLOR), seed=3)
    start = _alive(holo.frame(0)[0])
    mid = _alive(holo.frame(H.STRONG_TICKS // 2)[0])
    settled = _alive(holo.frame(H.STRONG_TICKS + 5)[0])
    assert start == 0 and 0 < mid < settled                                    # 一点点溶出来
    assert holo.frame(100, leaving=0)[1] == 1.0
    leaving = _alive(holo.frame(100, leaving=H.STRONG_TICKS // 2)[0])
    assert 0 < leaving < settled                                               # 溶没的过程
    assert holo.frame(100, leaving=H.STRONG_TICKS)[1] == 0.0
    assert holo.frame(100)[0] == holo.frame(100)[0]                            # 同一 tick 同一画面（可复现）


def test_dissolve_mask_is_sparse_in_the_middle_dense_at_the_edge_and_empty_at_zero_alpha():
    mask = H._Dissolve(480, 300)
    kept = mask.keep(1.0, 1.0)
    assert kept.shape == (300, 480)
    centre, rim = kept[90:210, 120:360], kept[:12]
    assert centre.mean() > 0.97                                                # 中间几乎不丢，字读得清
    assert rim.mean() < centre.mean() - 0.05                                   # 边上破洞最密
    assert not mask.keep(1.0, 0.0).any()
    half = mask.keep(1.0, 0.5)
    assert half.mean() < kept.mean()                                           # 淡入中丢得更多


def test_dissolve_drifts_continuously_with_time():
    mask = H._Dissolve(480, 300)
    a, b, c = mask.keep(2.0, 1.0), mask.keep(2.0 + 1 / 40, 1.0), mask.keep(2.0 + 0.2, 1.0)
    step, later = (a != b).mean(), (a != c).mean()
    assert 0 < step < 0.12                                                     # 每个 tick 只有一小部分像素变，不是整屏重洗
    assert later > 0                                                           # 噪声在滚（滚一整张噪声图要 0.5 s，所以别拿整数秒比）
    assert (mask.keep(2.0, 1.0) == a).all()                                    # 同一时刻可复现


def test_noise_matches_the_game_statistics():
    n = H._noise()
    assert n.shape == (H.NOISE_N, H.NOISE_N) and abs(float(n.mean()) - 0.5) < 0.02 and 0.15 < float(n.std()) < 0.25


def test_edge_drift_is_smooth():
    steps = [H._drift(t / 40, 3, 0.8) for t in range(400)]
    jumps = [abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(steps, steps[1:])]
    assert max(jumps) < 0.2 and max(abs(s[0]) for s in steps) <= 0.8             # 每 tick 挪不到 0.2 px：没有跳变


def test_place_panel_stays_on_screen_and_away_from_head():
    for inward, body in (((0, 1), (600, 120)), ((0, -1), (600, 600)), ((1, 0), (120, 360)), ((-1, 0), (1160, 360))):
        rect = H.place_panel(470, 200, body, (640, 360), inward, (1280, 720))
        assert rect.left() >= 16 and rect.top() >= 16
        assert rect.right() <= 1280 - 16 and rect.bottom() <= 720 - 40
