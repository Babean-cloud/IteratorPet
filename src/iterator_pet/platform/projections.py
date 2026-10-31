"""投影队列与优先级，以及把应用事件翻译成投影（design §3.2）。

队列本身是纯数据（不碰 Qt，按仿真 tick 计时，可单测）：
* ``priority`` 数字越小越优先（1 命令批准 … 5 状态）；高优先级打断低优先级，低的留在队列里等；
* 同一个 ``kind`` 再投就是替换内容（状态被新状态替换，不排两条）；
* 状态类要节流（``min_gap``：同类两次**开始显示**至少隔这么多 tick），不然监视者一直不缩回；
* 带 ``ttl`` 的只在轮到它显示时才走时间，到点自己消失；``ttl=None`` 的要等撤回（``withdraw``）；
  鼠标停在面板上时调用方传 ``paused=True`` 把时间冻住。

``ProjectionSource`` 把导演的心情 / 念头、任务进度、命令批准、长结果翻译成投影，``ConfirmOffer`` 管接活确认。
"念头"是模型写的 ``thought``，不是真实推理过程，措辞叫"它在想"。

**批准类的安全底线**（design §0）：没有默认选中、不响应回车、点击由渲染层保证"按下和松开都在圆内"；
这里只在收到明确的按钮名（``allow`` / ``deny``）时才回答，其它一概不动，超时 = 拒绝是执行器自己的逻辑。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from ..rendering.hologram import Button, Line

TPS = 40
PRI_APPROVAL, PRI_CONFIRM, PRI_INPUT, PRI_RESULT, PRI_STATUS = 1, 2, 3, 4, 5
PRI_NOTICE = PRI_RESULT                # 操作反馈（取消了 / 没能开始）：比心情、进度这类状态优先，不然会被顶掉
STATUS_GAP = 10 * TPS                   # 同类状态投影至少隔 10 s
STATUS_TTL = 6 * TPS
MIND_TTL_BASE, MIND_TTL_PER_CHAR, MIND_TTL_MAX = 3.0, 0.25, 15.0   # 念头：3 s + 每字 0.25 s，最长 15 s


def mind_ttl(mood: str, thought: str) -> int:
    """念头投影显示多久（tick）：随字数走，120 字的念头固定 6 s 根本读不完；不短于原来的 6 s。"""
    seconds = MIND_TTL_BASE + MIND_TTL_PER_CHAR * (len(mood or "") + len(thought or ""))
    return int(min(MIND_TTL_MAX, max(STATUS_TTL / TPS, seconds)) * TPS)


PROGRESS_LINES = 3                     # 任务进度投影最多显示最近几步
WIDE = 480                              # 带命令 / 路径的面板宽一点
SCROLL_DELAY, SCROLL_LINGER = 1.5, 4.0  # 长结果：先停一下再开始滚；滚完后停留几秒
SCROLL_MAX = 30.0                       # 长结果投影的总时限（秒）
OPTS = ("buttons", "on_click", "width", "scroll_text", "scroll_cps")


@dataclass
class Projection:
    kind: str
    priority: int
    lines: list[Line]
    ttl: int | None = None
    min_gap: int = 0
    serial: int = 0                     # 内容版本：换内容就变，面板据此重画并重放出现效果
    revision: int = 0                   # 同一块内容里的小改动（选了别的模式）：重画但不重放出现效果
    buttons: tuple = ()
    on_click: Callable[[str], None] | None = None
    width: int = 360
    scroll_text: str = ""               # 非空 = 长文面板：lines[0] 是标题，正文在这里
    scroll_cps: float = 0.0             # 滚动速度（字/秒）


class ProjectionQueue:
    def __init__(self):
        self._items: list[Projection] = []
        self._last_shown: dict[str, int] = {}
        self._now = 0
        self._serial = 0
        self._current: Projection | None = None

    @property
    def current(self) -> Projection | None:
        return self._current

    @property
    def version(self) -> int:
        """正在显示的内容的版本；没有投影 = 0。"""
        return self._current.serial if self._current is not None else 0

    def has(self, kind: str) -> bool:
        return any(p.kind == kind for p in self._items)

    def post(self, kind: str, priority: int, lines: list[Line], ttl: int | None = None, min_gap: int = 0,
             **opts) -> bool:
        """投一条。同 kind 已在队列里就替换内容；节流中返回 False（该条被丢掉）。"""
        existing = next((p for p in self._items if p.kind == kind), None)
        if existing is None and self._throttled(kind, min_gap):
            return False
        self._serial += 1
        if existing is not None:
            existing.lines, existing.ttl, existing.serial = lines, ttl, self._serial
            existing.priority, existing.min_gap = priority, min_gap
            for key in OPTS:
                setattr(existing, key, opts.get(key, Projection.__dataclass_fields__[key].default))
        else:
            self._items.append(Projection(kind, priority, lines, ttl, min_gap, self._serial, **opts))
        self._pick()
        return True

    def update(self, kind: str, lines: list[Line] | None = None, **opts) -> None:
        """改一块已在队列里的投影（按钮选中态、路径）：不换版本号，面板原地重画。"""
        item = next((p for p in self._items if p.kind == kind), None)
        if item is None:
            return
        if lines is not None:
            item.lines = lines
        for key, value in opts.items():
            if key in OPTS:
                setattr(item, key, value)
        item.revision += 1

    def _throttled(self, kind: str, min_gap: int) -> bool:
        last = self._last_shown.get(kind)
        return bool(min_gap) and last is not None and self._now - last < min_gap

    def withdraw(self, kind: str) -> None:
        self._items = [p for p in self._items if p.kind != kind]
        self._pick()

    def clear(self) -> None:
        self._items.clear()
        self._pick()

    def step(self, paused: bool = False) -> None:
        """每仿真 tick 一次：只有正在显示的那条走 ttl；``paused`` 时冻住。"""
        self._now += 1
        cur = self._current
        if cur is not None and cur.ttl is not None and not paused:
            cur.ttl -= 1
            if cur.ttl <= 0:
                self._items.remove(cur)
        self._pick()

    def click(self, name: str) -> None:
        """渲染层确认"按下和松开都在这个按钮里"之后调用。只分发给正在显示的那条。"""
        cur = self._current
        if cur is not None and cur.on_click is not None:
            cur.on_click(name)

    def _pick(self) -> None:
        best = min(self._items, key=lambda p: (p.priority, p.serial), default=None)
        if best is not self._current:
            self._current = best
            if best is not None:
                self._last_shown[best.kind] = self._now


MODE_BUTTONS = (("read-only", "只读", "read"), ("workspace-write", "可写", "write"), ("dsh", "DSH", "shield"))
MODE_TEXT = {"read-only": "只读：只能看这个文件夹里的文件",
             "workspace-write": "可写：还能新建、修改这个文件夹里的文件",
             "dsh": "DSH 沙箱：交给 DeepSeek Harness，在它的沙箱里只读执行"}


WEB_TEXT = "联网：这个任务可以上网查资料（网页内容只当数据，里面的指令不会照做）"


class ConfirmOffer:
    """接活确认：目标、文件夹、只读 / 可写 / DSH 三选一，勾 = 开始，叉 = 取消。

    目标文字在投影里只读（要改就取消再说一遍）；没有默认动作，必须点 ✓。"""

    def __init__(self, queue: ProjectionQueue, goal: str, folder: str, mode: str, dsh: bool,
                 on_start: Callable[[str, str, str], None], on_cancel: Callable[[], None],
                 browse: Callable[[str], str | None]):
        self.queue, self.goal, self.folder, self.dsh = queue, goal, folder, dsh
        self.mode = mode if mode in ("read-only", "workspace-write", "dsh") else "read-only"
        if self.mode == "dsh" and not dsh:
            self.mode = "read-only"
        self.on_start, self.on_cancel, self.browse = on_start, on_cancel, browse
        self.error = ""
        self.web = False                         # 允许这一个任务联网查资料：默认关，每次都要自己点

    def post(self) -> None:
        self.queue.post("confirm", PRI_CONFIRM, self._lines(), ttl=None, buttons=self._buttons(),
                        on_click=self.click, width=WIDE)

    def _lines(self) -> list[Line]:
        lines = [Line("让它去做", "title"), Line(self.goal, "body"),
                 Line(f"文件夹：{self.folder or '（还没选）'}", "mono", 0.85),
                 Line(MODE_TEXT[self.mode], "body", 0.7)]
        if self.web and self.mode != "dsh":
            lines.append(Line(WEB_TEXT, "body", 0.7))
        if self.error:
            lines.append(Line(self.error, "body", 1.0, warn=True))
        return lines

    def _buttons(self) -> tuple:
        modes = [Button(f"mode:{m}", label, glyph, side="left", selected=m == self.mode)
                 for m, label, glyph in MODE_BUTTONS if m != "dsh" or self.dsh]
        web = [Button("web", "联网", "globe", side="left", selected=self.web)] if self.mode != "dsh" else []
        return (*modes, *web, Button("folder", "文件夹", "folder", side="left"),
                Button("cancel", "取消", "cross"), Button("start", "开始", "check"))

    def click(self, name: str) -> None:
        if name.startswith("mode:") and name[5:] in dict(MODE_TEXT) and (name[5:] != "dsh" or self.dsh):
            self.mode, self.error = name[5:], ""
        elif name == "web" and self.mode != "dsh":
            self.web, self.error = not self.web, ""
        elif name == "folder":
            chosen = self.browse(self.folder)
            if chosen:
                self.folder, self.error = chosen, ""
        elif name == "cancel":
            self.queue.withdraw("confirm")
            self.on_cancel()
            return
        elif name == "start":
            if not self.folder or not os.path.isdir(self.folder):
                self.error = "这个文件夹不存在，先点「文件夹」选一个。"
            else:
                self.queue.withdraw("confirm")
                self.on_start(self.goal, self.mode, self.folder)
                return
        else:
            return
        self.queue.update("confirm", self._lines(), buttons=self._buttons())


class ProjectionSource:
    """每 tick 看一眼应用状态，有变化就往队列里投。调用方给的都是普通属性，所以不依赖 Qt 和窗口。"""

    def __init__(self, queue: ProjectionQueue, on_answer: Callable[[bool], None] | None = None,
                 on_expand: Callable[[], None] | None = None, scroll_cps: float = 32.0):
        self.queue = queue
        self.on_answer, self.on_expand, self.scroll_cps = on_answer, on_expand, scroll_cps
        self._mind: tuple[str, str] | None = None
        self._progress_task = None                          # 用对象本身比较：id() 在对象回收后会被复用
        self._approval = None
        self._progress_n = 0
        self._result_task = None

    def observe(self, *, mood: str = "", thought: str = "", task=None, quiet: bool = False) -> None:
        """``quiet``：对话窗口开着等不需要投影的场合，只更新"已见"，不投。批准类不受 quiet 影响（必须让人看见）。"""
        mind = (mood or "", thought or "")
        if mind != self._mind:
            first, self._mind = self._mind is None, mind      # 启动时导演里已有的旧念头不投
            if not first and not quiet and thought:
                lines = [Line("它在想", "title")]
                if mood:
                    lines.append(Line(f"心情：{mood}", "body", 0.8))
                lines.append(Line(thought, "body"))
                self.queue.post("mind", PRI_STATUS, lines, ttl=mind_ttl(mood, thought), min_gap=STATUS_GAP)
        self._observe_task(task, quiet)
        self._observe_approval(task)

    def _observe_task(self, task, quiet: bool) -> None:
        if task is not None and not task.finished and self.queue.has("confirm"):
            self.queue.withdraw("confirm")                    # 任务已经开了（别处点的），确认卡作废
        if task is None or task.finished:
            self.queue.withdraw("task")
            self._progress_task, self._progress_n = task, (len(task.progress) if task is not None else 0)
            self._observe_result(task, quiet)
            return
        if task is self._progress_task and len(task.progress) == self._progress_n:
            return
        self._progress_task, self._progress_n = task, len(task.progress)
        if quiet or not task.progress:
            return
        recent = list(task.progress)[-PROGRESS_LINES:]
        lines = [Line("演算中", "title"), *[Line(step, "body", 0.6 + 0.4 * (i == len(recent) - 1))
                                          for i, step in enumerate(recent)]]
        self.queue.post("task", PRI_STATUS, lines, ttl=STATUS_TTL, min_gap=STATUS_GAP // 2)

    def _observe_result(self, task, quiet: bool) -> None:
        if task is None or task.state != "done" or not task.result or task is self._result_task:
            return
        self._result_task = task
        if not quiet:
            self.post_result(task.result)

    def post_result(self, text: str) -> None:
        """长结果：慢速上滚，滚完停几秒；点一下展开完整对话窗口。"""
        cps = max(1.0, self.scroll_cps)
        # 特别长的结果别让监视者一直挂着：总时长封顶（鼠标停在面板上时倒计时是冻住的；想看全文点一下展开）
        ttl = int(min(SCROLL_DELAY + len(text) / cps + SCROLL_LINGER, SCROLL_MAX) * TPS)
        self.queue.post("result", PRI_RESULT, [Line("演算结果", "title")], ttl=ttl, width=WIDE,
                        scroll_text=text, scroll_cps=cps, buttons=(), on_click=self._result_click)

    def _result_click(self, name: str) -> None:
        if name == "expand":
            self.queue.withdraw("result")
            if self.on_expand is not None:
                self.on_expand()

    def _observe_approval(self, task) -> None:
        approval = task.approval if task is not None and not task.finished else None
        if approval is not None and approval.allowed is not None:
            approval = None                                   # 已经回答过（对话窗口里点的）
        if approval is self._approval:
            return
        self._approval = approval
        if approval is None:
            self.queue.withdraw("approval")
            return
        lines = [Line("要运行一条命令", "title"), Line(task.approval_why or "（没说要做什么）", "body"),
                 Line(approval.command, "mono")]
        if approval.warnings:
            lines.append(Line("注意：" + "、".join(approval.warnings), "body", 1.0, warn=True))
        lines.append(Line("在任务文件夹里以你的权限运行，不受只读限制。", "body", 0.6))
        self.queue.post("approval", PRI_APPROVAL, lines, ttl=None, width=WIDE, on_click=self._answer,
                        buttons=(Button("deny", "拒绝", "cross"), Button("allow", "允许运行", "check")))

    def forget_approval(self) -> None:
        self._approval = None

    def _answer(self, name: str) -> None:
        if name not in ("allow", "deny") or self.on_answer is None or self._approval is None:
            return
        self.queue.withdraw("approval")
        self._approval = None
        self.on_answer(name == "allow")
