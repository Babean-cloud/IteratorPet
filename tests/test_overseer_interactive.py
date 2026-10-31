"""第二期：可交互投影。批准只能通过明确点击生效；点在圆外无效；超时 / 没点 = 不放行。"""

import os
from collections import deque
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402

from iterator_pet.platform import projections as P  # noqa: E402
from iterator_pet.rendering.hologram import STRONG_TICKS, button_rects  # noqa: E402
from iterator_pet.rendering.overseer_layer import OverseerLayer  # noqa: E402
from iterator_pet.simulation.appearance import PEBBLES  # noqa: E402

SIZE = (1280, 720)


def _state():
    return SimpleNamespace(base_point=(300.0, 0.0), head=(300.0, 200.0), joints=[(300.0, 0.0), (310.0, 100.0)],
                           cord=[(300.0, 200.0), (300.0, 250.0)], appearance=PEBBLES, cursor=None)


def _task(approval=None, state="running", result="", progress=()):
    return SimpleNamespace(progress=deque(progress), finished=state != "running", state=state, result=result,
                           approval=approval, approval_why="整理文件")


def _approval(allowed=None):
    return SimpleNamespace(command="Remove-Item .\\a.txt", warnings=["删除文件"], allowed=allowed)


def _source(answers):
    q = P.ProjectionQueue()
    return q, P.ProjectionSource(q, on_answer=answers.append, on_expand=lambda: answers.append("expand"))


# ---- 批准 -------------------------------------------------------------------------------------
def test_approval_is_projected_with_full_command_and_no_default():
    q, src = _source([])
    src.observe(task=_task(_approval()))
    cur = q.current
    assert cur.kind == "approval" and cur.priority == P.PRI_APPROVAL and cur.ttl is None
    texts = [line.text for line in cur.lines]
    assert "Remove-Item .\\a.txt" in texts and any("删除文件" in t for t in texts)
    assert [b.name for b in cur.buttons] == ["deny", "allow"]
    assert not any(b.selected or b.hot for b in cur.buttons)           # 没有默认选中


def test_approval_only_answers_on_explicit_button_names():
    answers = []
    q, src = _source(answers)
    src.observe(task=_task(_approval()))
    for junk in ("", "Enter", "expand", "mode:read-only", "ALLOW", "yes"):
        q.click(junk)
    assert answers == [] and q.current.kind == "approval"             # 乱七八糟的名字一律不放行
    q.click("allow")
    assert answers == [True] and q.current is None
    q.click("allow")                                                  # 已经答过，再点没有任何效果
    assert answers == [True]


def test_approval_deny_and_answered_elsewhere():
    answers = []
    q, src = _source(answers)
    approval = _approval()
    src.observe(task=_task(approval))
    q.click("deny")
    assert answers == [False]
    other = _approval()
    src.observe(task=_task(other))
    other.allowed = True                                               # 在对话窗口里答了
    src.observe(task=_task(other))
    assert q.current is None


def test_approval_outranks_status_and_does_not_expire():
    q, src = _source([])
    q.post("mind", P.PRI_STATUS, [], ttl=5)
    src.observe(task=_task(_approval()))
    for _ in range(P.TPS * 60):
        q.step()
    assert q.current.kind == "approval"                                # 没有"超时即允许"；超时拒绝是执行器的事


# ---- 接活确认 ----------------------------------------------------------------------------------
def _offer(tmp_path, log, mode="read-only", dsh=True, browse=None):
    q = P.ProjectionQueue()
    offer = P.ConfirmOffer(q, "整理照片", str(tmp_path), mode, dsh,
                           on_start=lambda g, m, f: log.append(("start", g, m, f)),
                           on_cancel=lambda: log.append("cancel"), browse=browse or (lambda cur: None))
    offer.post()
    return q, offer


def test_confirm_requires_explicit_start_and_reports_choices(tmp_path):
    log = []
    q, offer = _offer(tmp_path, log)
    assert log == [] and q.current.kind == "confirm"
    selected = [b.name for b in q.current.buttons if b.selected]
    assert selected == ["mode:read-only"]
    q.click("mode:workspace-write")
    assert [b.name for b in q.current.buttons if b.selected] == ["mode:workspace-write"]
    q.click("start")
    assert log == [("start", "整理照片", "workspace-write", str(tmp_path))] and q.current is None


def test_confirm_cancel_and_invalid_folder(tmp_path):
    log = []
    q, offer = _offer(tmp_path / "不存在", log)
    q.click("start")
    assert log == [] and q.current is not None and any(line.warn for line in q.current.lines)
    q.click("cancel")
    assert log == ["cancel"] and q.current is None


def test_confirm_browse_and_dsh_availability(tmp_path):
    log = []
    other = tmp_path / "b"
    other.mkdir()
    q, offer = _offer(tmp_path, log, dsh=False, browse=lambda cur: str(other))
    assert "mode:dsh" not in [b.name for b in q.current.buttons]
    q.click("mode:dsh")                                                # 没有 DSH 就点不进去
    assert offer.mode == "read-only"
    q.click("folder")
    q.click("start")
    assert log[0][3] == str(other)


def test_confirm_revision_changes_without_new_serial(tmp_path):
    q, _ = _offer(tmp_path, [])
    serial, revision = q.current.serial, q.current.revision
    q.click("mode:workspace-write")
    assert q.current.serial == serial and q.current.revision == revision + 1


def test_confirm_withdrawn_when_task_started_elsewhere(tmp_path):
    q, offer = _offer(tmp_path, [])
    src = P.ProjectionSource(q)
    src.observe(task=_task())
    assert q.current is None


# ---- 长结果 ------------------------------------------------------------------------------------
def test_result_projection_scrolls_and_expands_on_click():
    answers = []
    q, src = _source(answers)
    src.scroll_cps = 10.0
    src.observe(task=_task(state="done", result="结果" * 100))
    cur = q.current
    assert cur.kind == "result" and cur.scroll_text and cur.scroll_cps == 10.0
    assert cur.ttl >= int((P.SCROLL_DELAY + 20 + P.SCROLL_LINGER) * P.TPS) - 1
    src.observe(task=_task(state="done", result="结果" * 100))        # 同一个任务不重复投（换对象也算新任务，这里 id 不同）
    q.click("expand")
    assert answers == ["expand"] and q.current is None


def test_very_long_result_has_a_time_limit():
    q, src = _source([])
    src.observe(task=_task(state="done", result="字" * 20000))
    assert q.current.ttl == int(P.SCROLL_MAX * P.TPS)
    short = P.ProjectionQueue()
    P.ProjectionSource(short, scroll_cps=32.0).post_result("字" * 64)
    assert short.current.ttl == int((P.SCROLL_DELAY + 2 + P.SCROLL_LINGER) * P.TPS)       # 短的不受限


def test_result_not_projected_when_chat_open_and_paused_ttl():
    q, src = _source([])
    src.observe(task=_task(state="done", result="x"), quiet=True)
    assert q.current is None
    task = _task(state="done", result="y")
    src.observe(task=task)
    ttl = q.current.ttl
    q.step(paused=True)
    assert q.current.ttl == ttl                                        # 鼠标停着：倒计时冻住
    q.step()
    assert q.current.ttl == ttl - 1


# ---- 渲染层的按钮判定 ---------------------------------------------------------------------------
def _hovering_layer(buttons, **show):
    QGuiApplication.instance() or QGuiApplication([])
    layer = OverseerLayer()
    state = _state()
    layer.show(1, [P.Line("要运行一条命令", "title"), P.Line("Remove-Item a")], buttons=buttons, width=P.WIDE, **show)
    for _ in range(60 + STRONG_TICKS + 2):
        layer.step(state, 1.0, SIZE)
    assert layer.interactive
    return layer


def _center(layer, name):
    c = layer._content
    rect = layer._rect
    for b, r in zip(c.buttons, button_rects(int(rect.width()), int(rect.height()), c.buttons)):
        if b.name == name:
            return rect.x() + r.center().x(), rect.y() + r.center().y()
    raise AssertionError(name)


def test_button_hit_only_inside_circle_and_after_panel_settles():
    from iterator_pet.rendering.hologram import Button

    buttons = (Button("deny", "拒绝", "cross"), Button("allow", "允许运行", "check"))
    QGuiApplication.instance() or QGuiApplication([])
    fresh = OverseerLayer()
    fresh.show(1, [P.Line("x")], buttons=buttons)
    fresh.step(_state(), 1.0, SIZE)
    assert fresh.button_at((10.0, 10.0)) is None                       # 还没出来 / 还在错动：点不了
    layer = _hovering_layer(buttons)
    ax, ay = _center(layer, "allow")
    assert layer.button_at((ax, ay)) == "allow"
    assert layer.button_at((ax + 30, ay)) != "allow"                   # 圆外（半径 20）无效
    dx, dy = _center(layer, "deny")
    assert layer.button_at((dx, dy)) == "deny"
    assert layer.button_at((0.0, 0.0)) is None


def test_hover_rerenders_without_replaying_appearance():
    from iterator_pet.rendering.hologram import Button

    layer = _hovering_layer((Button("deny", "拒绝", "cross"), Button("allow", "允许运行", "check")))
    age, before = layer._age, layer._holo
    layer.set_cursor(_center(layer, "allow"))
    layer.step(_state(), 1.0, SIZE)
    assert layer._holo is not before and layer._age == age + 1         # 换了图（高亮），但没重置 age


def test_scroll_panel_scrolls_after_delay_and_pauses_on_hover():
    q, src = _source([])
    src.scroll_cps = 30.0
    body = "\n".join(f"第 {i} 行结果内容比较长一些" for i in range(60))
    src.observe(task=_task(state="done", result=body))
    cur = q.current
    QGuiApplication.instance() or QGuiApplication([])
    layer = OverseerLayer()
    state = _state()
    layer.show(cur.serial, cur.lines, width=cur.width, scroll_text=cur.scroll_text, scroll_cps=cur.scroll_cps)
    for _ in range(60 + STRONG_TICKS + 10):
        layer.step(state, 1.0, SIZE)
    assert layer._scroll_offset() == 0.0                               # 还在开头停留
    for _ in range(int(P.SCROLL_DELAY * 40) + 40):
        layer.step(state, 1.0, SIZE)
    moved = layer._scroll_offset()
    assert moved > 0
    rect = layer._rect
    layer.set_cursor((rect.center().x(), rect.center().y()))
    assert layer.panel_hovered and layer.button_at((rect.center().x(), rect.center().y())) == "expand"
    for _ in range(40):
        layer.step(state, 1.0, SIZE)
    assert layer._scroll_offset() == moved                             # 鼠标停着：不滚
