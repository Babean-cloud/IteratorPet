"""Full-screen transparent overlay window that hosts the pet."""

from __future__ import annotations

import ctypes
import math
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QFileDialog

from ..config import SIZE_PRESETS, Config, llm_api_key, load, save, scale_for
from ..rendering.hit import hit_test, in_drop_zone
from ..rendering.canvas import PetCanvas
from ..rendering.hologram import Line
from ..simulation.state import SimulationState
from .fixed_loop import DEFAULT_MAX_SUBSTEPS, GAME_DT, FixedStepLoop
from .workspace import ScreenWorkspace, pet_rect
from . import cursorfx, windows_overlay
from .projections import PRI_INPUT, PRI_NOTICE, STATUS_TTL, ConfirmOffer, ProjectionQueue, ProjectionSource


# 没开 AI 时，编程 agent 的通知用这几句转达
AGENT_LINES = {
    "done": "……{project} 那边跑完了。",
    "attention": "……{project} 在等你点头。",
    "error": "……{project} 出错了，去看看。",
    "info": "……{project} 有消息。",
}

# 把珍珠托用户转给另一位时说的话（本地，不花 token）
GIFT_LINES = {
    "pebbles": ("……拿去给她。别说是我想起来的。", "……转给月。看不看是她的事。",
                "……这颗给她。你跑一趟。", "……给隔壁的。我这里放不下了。"),
    "past_moon": ("请把这颗带给他吧……希望他会看。", "麻烦你转交给他，谢谢。",
                  "这颗……也许他会喜欢。拜托你了。", "替我交给卵石吧，别让他知道我在担心。"),
}
CALM_FPS = 60            # 平常自己待着。试过 30：比 40 TPS 的仿真还低，帧和 tick 对不齐，用户实测"有点卡"；
                         # 局部重画以后 60 帧的代价已经小很多，省 CPU 主要靠局部重画和离开时降速
AWAY_FPS = 8             # 用户离开了：没人看
AWAY_TIME_SCALE = 0.25   # 离开时仿真按 1/4 速走（物理开销跟着降到 1/4）


def _drop_log(message: str) -> None:
    """递珍珠的排障记录（launcher.log 里搜 [drop]）。"""
    try:
        from ..config import config_dir

        with (config_dir() / "launcher.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[drop] {time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _elevated() -> bool:
    """以管理员身份运行时，Windows（UIPI）不许普通权限的资源管理器把文件拖进来。"""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def cursor_sim_point(
    global_pos,
    origin,
    screen_rect,
    scale: float,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float] | None:
    """Map a global cursor position into SIMULATION coordinates, or ``None``.

    The simulation runs in game pixels and ``scale`` maps those onto screen
    pixels (``rendering/canvas.py``), so a screen offset has to be DIVIDED by
    ``scale`` before ``SimulationState.set_cursor`` sees it. Feeding screen
    pixels straight in -- which this used to do -- inflates every cursor
    distance by ``scale`` (1.5x on this machine): ``SCORE_TALK_RANGE`` only
    caught a cursor that was physically 1.5x closer than intended, and
    ``investigate`` aimed at ``cursor + 150 px`` measured in the wrong unit.

    Containment is tested against the SCREEN, not the window. The pet window is
    ``availableGeometry()``, so the taskbar strip (48 px tall here) read as "no
    cursor at all" even with the mouse sitting in it; that forced the selector
    into its no-cursor branch on every trip past the bottom edge. The point is
    clamped into the chamber, which is where the game would put it as well.
    """
    # QMouseEvent 给的是 QPointF，QRect.contains 只吃 QPoint → 统一成分数坐标再比
    if screen_rect is not None and not screen_rect.contains(
        global_pos.toPoint() if hasattr(global_pos, "toPoint") else global_pos
    ):
        return None
    zoom = max(1e-6, float(scale))
    x = (global_pos.x() - origin.x()) / zoom
    y = (global_pos.y() - origin.y()) / zoom
    bx, by, bw, bh = bounds
    return (min(max(x, bx), bx + bw), min(max(y, by), by + bh))


class PetWindow(PetCanvas):
    closed = Signal()

    def __init__(self, simulation: SimulationState, config: Config, parent=None, sprites=None):
        # Rain World's native cadence: every oracle constant is per 40 Hz tick.
        self._loop = FixedStepLoop(dt=GAME_DT, max_substeps=DEFAULT_MAX_SUBSTEPS)
        super().__init__(
            simulation,
            alpha_provider=lambda: self._loop.alpha,
            parent=parent,
            scale=config.scale,
            sprites=sprites,
        )
        self.config = config
        self.dialogue_enabled = bool(getattr(config, 'dialogue_enabled', True))
        self.pixel_art = bool(getattr(config, "pixel_art", True))
        self.pixel_snap = bool(getattr(config, "pixel_snap", False))
        self.drop_shadow = bool(getattr(config, "drop_shadow", True))
        self.show_pearls = bool(getattr(config, "show_pearls", True))
        self.overseer_enabled = getattr(config, "ui_style", "overseer") != "windows"
        self.elevated = _elevated()
        # 文件 / 文字拖到人偶身上 = 递珍珠。管理员身份下 OLE 拖放被 UIPI 拦，改走老式 WM_DROPFILES（只认文件）
        self.setAcceptDrops(not self.elevated)
        if self.elevated:
            _drop_log("桌宠以管理员身份运行：改用 WM_DROPFILES 接文件（拖文字进不来）")
        self.click_through = bool(config.click_through)
        self._swap_to: str | None = None         # 正在荡出去、等着换成的角色
        # 另一位转来的珍珠：启动 / 换过来后等开场白说完再读（_tick_gifts）
        self._gift_wait_until = time.perf_counter() + 8.0
        self._gifts_queued: set[str] = set()
        self.errand_marks = 0
        self.errand_busy = False
        self._paused = False
        self._press_global = None
        self._cursor_fx_fired = False
        self._press_at = 0.0
        self._last = time.perf_counter()
        self._origin = (0.0, 0.0)

        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if config.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setWindowTitle("Iterator Pet")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        workspace = ScreenWorkspace()
        snapshot = workspace.snapshot()
        geo = pet_rect(snapshot)          # 任务栏自动隐藏时让出 2 px，免得被当成全屏程序
        self._origin = (float(geo.x()), float(geo.y()))
        self.setGeometry(geo)
        zoom = max(0.3, min(4.0, float(config.scale)))
        # 心情的"屏幕速度"要有 scale 才能算（仿真 px/tick → 屏幕 px/s）
        self.simulation.scale = zoom
        self.simulation.resize(
            (0.0, 0.0, float(geo.width()) / zoom, float(geo.height()) / zoom)
        )
        # 地盘（「待在」菜单 / 模型的 park）跨重启保留；不依赖 AI
        self.simulation.set_territory(getattr(config, "territory", "") or None)
        # LLM 导演（托盘「AI 驱动」）；没开或没 Key 时为 None，行为与原版完全一致。
        # 用量和记忆不随开关重建：关掉 AI 也能在面板里看累计。
        # 记忆 / 对话记录 / 上下文按角色分开（profiles.py）；用量、提醒两个角色共用。
        from ..brain.errands import Errands
        from ..brain.executor import AgentRunner
        from ..brain.profiles import migrate_legacy
        from ..brain.reminders import Reminders
        from ..brain.tasks import TaskRunner, find_dsh
        from ..brain.usage import UsageTracker

        migrate_legacy()
        self.usage = UsageTracker()
        self.reminders = Reminders()
        self.errands = Errands()                 # 心事：和提醒一样两个角色共用
        from ..brain.pearl_media import PearlMaker

        self.pearl_maker = PearlMaker(config)    # 图 / 网页 / 音视频珍珠在后台做
        from .sound import SoundBoard

        self.projections = ProjectionQueue()                  # 监视者投影（projections.py）
        self._pressed_button: str | None = None
        self._confirm_offer = None
        self._projection_source = ProjectionSource(
            self.projections, on_answer=self.answer_approval, on_expand=lambda: self.open_chat(),
            scroll_cps=float(getattr(config, "overseer_scroll_speed", 32.0)))
        self.sound = SoundBoard(config, getattr(config, "install", "") or None)   # 原版音效（sound.py）
        self.sound.start()
        self.bridge = None                       # 编程 agent 的本机接口（bridge.py）
        if getattr(config, "bridge_enabled", True):
            from ..brain.bridge import Bridge

            bridge = Bridge()
            if bridge.start():
                self.bridge = bridge
            _drop_log(f"本机接口：{'127.0.0.1:%d' % bridge.port if self.bridge else bridge.error}")
        self._load_character_data()
        # 委托任务：默认自带执行器（executor.py）；装了 DeepSeek Harness 的话多一档"DSH 沙箱只读"
        self.agent = AgentRunner(lambda: self.llm_client(timeout=90.0),
                                 auto_read_only=bool(getattr(config, "auto_approve_readonly", True)),
                                 web=self._web_tools,
                                 auto_all=lambda: bool(getattr(self.config, "auto_approve_all", False)))
        self.tasks = TaskRunner(find_dsh(getattr(config, "dsh_path", "")),
                                api_key_fn=lambda: llm_api_key(self.config),
                                launch=getattr(config, "dsh_launch", "hidden-console"),
                                wiring=getattr(config, "dsh_wiring", "cmd-stdin"))
        self._runner = self.agent
        self._task_reported = None
        self._task_log_seen = 0
        self._approval_seen = None
        self.director = None
        self._ai_panel = None
        self._chat_window = None
        self._setup_brain()

        # 每秒重提一次 topmost：WS_EX_TOPMOST 只保证"在置顶带里"，带内的前后顺序会被
        # 别的置顶窗口（聊天/IDE/浏览器）抢走，人偶就被挡住。SWP_NOACTIVATE 不抢焦点。
        self._top_timer = QTimer(self)
        self._top_timer.setInterval(1000)
        self._top_timer.timeout.connect(self._keep_on_top)
        if config.always_on_top:
            self._top_timer.start()

        interval = max(4, int(1000 / max(15, config.fps)))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(interval)

        # 系统状态（全屏勿扰 / CPU 联动光环 / 电量）：每秒一次，全本地
        from .system_status import CpuMeter

        self._cpu = CpuMeter()
        self._fullscreen_quiet = False
        self._fullscreen_votes = 0
        self._halo_rate = getattr(config, "spark_rate", "eco")
        self._battery_warned = False
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._poll_system)
        self._status_timer.start()

    # -- lifecycle ---------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        windows_overlay.prevent_activation(self)
        # 默认整窗穿透；_update_hit_passthrough 在光标移到人偶/滑车上时再打开
        windows_overlay.set_click_through(self, True)
        self._interactive = False
        if self.elevated:
            from . import legacy_drop

            _drop_log(f"WM_DROPFILES {'已打开' if legacy_drop.enable(self) else '打开失败'}")

    def nativeEvent(self, event_type, message):  # noqa: N802
        if self.elevated:
            from . import legacy_drop

            paths = legacy_drop.files_from_message(event_type, message)
            if paths is not None:
                _drop_log(f"放下（WM_DROPFILES）：{len(paths)} 个文件")
                self.drop_files(paths, self._sim_point(QCursor.pos()))
                return True, 0
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 退出时收掉还在跑的委托任务：dsh → Electron → pwsh 是一棵进程树，不收会留孤儿
        for runner in (getattr(self, "tasks", None), getattr(self, "agent", None)):
            if runner is not None and runner.busy:
                runner.cancel()
        self._remember_position()
        if getattr(self, "bridge", None) is not None:
            self.bridge.stop()
        if getattr(self, "director", None) is not None:
            if self.director.perception is not None:
                self.director.perception.stop_events()
            self.director.save()                 # 最近几轮、反思进度：下次启动接着来
        self.simulation.end_drag(fling=False)
        self.simulation.kill.cancel()
        cursorfx.release()
        self._timer.stop()
        # NO save() HERE. Every mutator (toggle_click_through / toggle_rail)
        # already persists, so closing wrote nothing new -- but a TOOL
        # that builds a window from a default ``Config`` and then closes it
        # overwrote the user's real config. That happened: ``dev/tools/smoke_window.py``
        # reset ``scale`` from the user's 1.5 to the 2.2 default, which silently
        # changed the chamber size and made the pet 1.5x bigger on screen.
        self.closed.emit()
        super().closeEvent(event)

    # -- per-frame ---------------------------------------------------------
    def _tick(self) -> None:
        now = time.perf_counter()
        elapsed = now - self._last
        self._last = now
        if self._idle_mode() == "away":
            # 用户离开了（键鼠 10 分钟没动、没有投影、没在拖）：没人看，仿真放慢到 1/4 速（慢动作），
            # 帧率降到 AWAY_FPS。导演用的是自己的时钟，提醒 / 心事照常按真实时间走
            elapsed *= AWAY_TIME_SCALE
        if not self._paused and elapsed > 0.0:
            # The cursor must be sampled once per SIMULATION TICK, not once per
            # frame. ``advance`` can run up to 8 sub-steps for one frame, and the
            # behaviour gates are per tick: sampling per frame made every repeat
            # tick read a speed of exactly 0, which shuts ``investigate`` and
            # opens ``talk`` -- measured through the real simulation with a cursor
            # moving 3 px/frame: 1 tick/frame -> investigate 24 %, 2+ ticks/frame
            # -> investigate 2 % and talk starts while the mouse is MOVING. Worse,
            # every segment length is even, so the parity of boundary ticks is
            # conserved and a boundary that lands on a duplicate sample stays
            # duplicate forever.
            self._loop.advance(elapsed, self._step_once)
            if self._swap_to is not None and self.simulation.swap_ready:
                self._finish_swap()                 # 人偶出屏了：这一刻换角色
        self._track_cursor()
        self._update_hit_passthrough()
        director = self.director
        errands = director.errands if director is not None else None
        self.errand_marks = len(errands.items) if errands is not None else 0
        self.errand_busy = bool(director is not None and director.errand_busy)
        eyes = director.perception if director is not None else None
        # 暂停 / 全屏勿扰时不画：帧缓存不重画，圈会冻在半亮
        shown = hasattr(eyes, "reading_level") and not self._paused and not self._fullscreen_quiet
        self.screen_level = eyes.reading_level() if shown else 0.0
        self.screen_found = eyes.found_level() if shown else 0.0
        self._tick_gifts()
        self.sound.update(self._drawn_state, self.simulation.dialogue, self.character,
                          reading_pearl=self.simulation.pearls.reading is not None)
        self._retime()
        self._request_paint()

    # -- 省 CPU：按情况降帧率、只重画变了的那块（用户 2026-11-01：平时就占满一个核心） ------------
    def _idle_mode(self) -> str:
        """active = 用户在跟它互动（拖拽 / 投影在 / 杀鼠标 / 换角色 / 光标在附近），按配置帧率；
        calm = 平常自己待着，CALM_FPS；away = 用户离开了，AWAY_FPS + 慢动作仿真。"""
        sim = self.simulation
        busy = (getattr(sim, "dragging", False) or sim.swap_phase is not None
                or getattr(getattr(sim, "kill", None), "phase", "idle") != "idle"
                or self.overseer.visible or self.overseer2.visible)
        if busy:
            return "active"
        director = getattr(self, "director", None)
        if director is not None and director.away:
            return "away"
        cursor, head = getattr(sim, "cursor", None), sim.creature.chunk0
        if cursor is not None and math.dist(cursor, head) < 260.0:
            return "active"
        return "calm"

    def _retime(self) -> None:
        mode = self._idle_mode()
        fps = {"active": self.config.fps, "calm": min(self.config.fps, CALM_FPS),
               "away": AWAY_FPS}[mode]
        interval = max(4, int(1000 / max(1, fps)))
        if self._timer.isActive() and self._timer.interval() != interval:
            self._timer.setInterval(interval)

    def _request_paint(self) -> None:
        """帧缓存按仿真 tick 记（``_frame_stamp``），仿真 40 TPS、帧率 60：每 3 帧里有 1 帧和上一帧一模一样，
        照样整窗交给系统合成一遍。人偶这层没变、又没有每帧都在动的监视者 / 字幕时，这一帧直接跳过。

        试过只重画变了的矩形（``update(rect)``）：透明分层窗口局部提交时边缘会闪出矩形框（用户 2026-11-01 实测），
        省得也不多，已撤回，一律整窗。"""
        if (self._frame_stamp() == self._frame_key and not self.overseer.visible
                and not self.overseer2.visible and not self.simulation.dialogue.text):
            return
        self.update()

    def _update_hit_passthrough(self) -> None:
        """只有滑车和人偶（头/身体）能点，其余地方一律让鼠标穿过去。

        Windows 只让**完全透明**的像素穿透；柔光、头后暗晕、落影、放电白光都是半透明的，
        一大片地方就点不到下面的窗口了（用户实测）。所以整窗默认 WS_EX_TRANSPARENT，
        光标移到能抓的部位上（或正在拖）时才临时关掉。用户开了「鼠标穿透」则一直穿透。
        """
        if self.click_through:
            return
        over = getattr(self.simulation, "dragging", False)
        catching = False
        if not over and self.overseer.button_at(self._local_cursor()) is not None:
            over = True                      # 投影的按钮 / 长文面板：关掉穿透才点得到
        if not over:
            point = self._sim_point(QCursor.pos())
            over = hit_test(self._drawn_state, point, self.scale) is not None
            # 左键按着从别处拖东西过来（递珍珠）：人偶周围一大圈都接，人偶转头看着你手里的东西
            if (not over and sys.platform == "win32" and self._left_button_down()
                    and in_drop_zone(self._drawn_state, point, self.scale)):
                over = catching = True
        if catching:
            self.simulation.direct(None, look="cursor", ticks=6)
        if catching != getattr(self, "_catching", False):
            self._catching = catching
            _drop_log(f"接球圈 {'进入' if catching else '离开'}")
        if over != getattr(self, "_interactive", None):
            self._interactive = over
            windows_overlay.set_click_through(self, not over)

    def _step_once(self, dt: float) -> None:
        """One 40 TPS tick: look at the pointer, then step the physics."""
        if getattr(self.simulation, "dragging", False):
            # 必须是**每 tick**采一次（不是每帧）：一帧能跑 1-8 个 tick，重复采样会让
            # 第 2..N 个 tick 拿到同一个点（和 AGENTS §6.2 的"每帧采光标"同类缺陷）。
            point = self._sim_point(QCursor.pos())
            if point is not None:
                self.simulation.drag_to(point)
            if not self._left_button_down():
                self.simulation.end_drag()
        else:
            self._track_cursor()
        self.simulation.step(dt)
        self._step_overseer()
        director = getattr(self, "director", None)
        if director is not None:
            director.tick(self.simulation)
        cursorfx.tick()
        # 杀鼠标的 flash 段：换忙光标 0.3 s（档 2）。用"进 flash 的第一 tick"当触发沿
        if getattr(self.simulation, "kill", None) is not None:
            if self.simulation.kill.phase == "flash":
                if not self._cursor_fx_fired and getattr(self.config, "cursor_fx", True):
                    anchor = self._drawn_state.kill_anchor if self._drawn_state else None
                    if anchor is not None:
                        geo = self.geometry()
                        sx = geo.x() + anchor[0] * self.scale
                        sy = geo.y() + anchor[1] * self.scale
                        # 掉到工作区底部（留 2 px 免得压到任务栏边沿）
                        cursorfx.start(sx, sy, geo.y() + geo.height() - 2.0)
                self._cursor_fx_fired = True
            else:
                self._cursor_fx_fired = False

    def _step_overseer(self) -> None:
        """监视者每 tick 一步。人偶被拖拽 / 全屏勿扰 / 换角色时让它立刻缩回（投影内容之后再出来）。"""
        state = self.simulation.interpolate(1.0)
        quiet = (getattr(self.simulation, "dragging", False) or self._fullscreen_quiet
                 or self.simulation.swap_phase is not None)
        director = getattr(self, "director", None)
        chat = getattr(self, "_chat_window", None)
        queue = self.projections
        self.overseer.set_cursor(self._local_cursor())
        queue.step(paused=self.overseer.panel_hovered)           # 鼠标停在面板上：倒计时和滚动都停
        if not self.overseer_enabled:
            # 配置 ui_style = windows：不投影，在场的缩回去；"已见"照样更新，免得重新打开时补投一堆旧的
            self._projection_source.observe(
                mood=director.mood if director is not None else "",
                thought=director.thought if director is not None else "",
                task=self.current_task, quiet=True)
            queue.clear()
            self._projection_source.forget_approval()      # 中途又打开时，还在等的批准要重新投出来
            self.overseer.clear()
            self.overseer.step(state, self.scale, (self.width(), self.height()), quiet=True)
            second = getattr(self, "overseer2", None)
            if second is not None:
                second.clear()
                second.step(state, self.scale, (self.width(), self.height()), quiet=True)
            PetWindow._sync_own_ui(self, director, None)
            return
        self._projection_source.observe(
            mood=director.mood if director is not None else "",
            thought=director.thought if director is not None else "",
            task=self.current_task,
            quiet=bool(chat is not None and chat.isVisible()),      # 对话窗口开着就不用再投一遍
        )
        bar = getattr(self, "_quick_bar", None)
        bar_open = bool(bar is not None and bar.isVisible() and bar.holo)
        if queue.has("input") and not bar_open:
            queue.withdraw("input")                            # 输入条收起了（Esc / 发送 / 点别处）
        current = queue.current
        if bar_open and queue.has("input") and (current is None or current.kind != "input"):
            bar.hide()                                         # 被批准 / 确认这类更高优先级的投影顶掉
        elif current is not None and current.kind == "input":
            self.overseer.show(current.serial, [], reserve=(bar.width(), bar.height()))
            rect = self.overseer.reserved_rect
            if rect is not None:
                bar.dock(self.mapToGlobal(QPoint(int(rect.x()), int(rect.y()))), self.overseer.extended)
        elif current is not None:
            self.overseer.show(current.serial, current.lines, buttons=current.buttons, width=current.width,
                               revision=current.revision, scroll_text=current.scroll_text,
                               scroll_cps=current.scroll_cps)
        else:
            self.overseer.clear()
        cursor = state.cursor
        look = (cursor[0] * self.scale, cursor[1] * self.scale) if cursor is not None else None
        self.overseer.step(state, self.scale, (self.width(), self.height()), look=look, quiet=quiet)
        second = getattr(self, "overseer2", None)                # 测试里的假窗口可能没有第二只
        if second is not None:
            extra = queue.second
            if extra is not None:
                second.show(extra.serial, extra.lines, width=extra.width, revision=extra.revision)
            else:
                second.clear()
            first = self.overseer.sim
            second.step(state, self.scale, (self.width(), self.height()), look=look, quiet=quiet,
                        avoid=(first.edge,) if first.visible else ())
        PetWindow._sync_own_ui(self, director, current)   # 测试里 self 是假对象

    def _sync_own_ui(self, director, current) -> None:
        """告诉读屏幕：自家气泡 / 监视者 / 投影在哪、刚说了什么，截图时盖掉，认出来也丢掉。"""
        perception = getattr(director, "perception", None) if director is not None else None
        if perception is None or not hasattr(self, "own_regions"):
            return
        perception.set_own_regions(self.own_regions())
        said = self.simulation.dialogue.text
        if said and said != getattr(self, "_noted_say", None):
            self._noted_say = said
            perception.note_own_text(said)
        if current is not None and current.serial != getattr(self, "_noted_projection", None):
            self._noted_projection = current.serial
            for line in current.lines:
                perception.note_own_text(getattr(line, "text", ""))
            perception.note_own_text(current.scroll_text or "")

    def _local_cursor(self) -> tuple[float, float]:
        """光标在本窗口里的位置（屏幕像素）：投影的按钮 / 悬停都按这个算。"""
        p = self.mapFromGlobal(QCursor.pos())
        return float(p.x()), float(p.y())

    def _track_cursor(self) -> None:
        """Feed the global cursor position to the puppet so it can look at it."""
        screen = self.screen()
        self.simulation.set_cursor(
            cursor_sim_point(
                QCursor.pos(),
                self.pos(),
                screen.geometry() if screen is not None else None,
                self.scale,
                self.simulation.bounds,
            )
        )

    def _keep_on_top(self) -> None:
        # AI 面板 / 对话窗口开着时不抢 z 序，不然每秒一次把它们压到全屏透明窗口下面
        if getattr(self, "_ai_panel", None) is not None:
            return
        chat = getattr(self, "_chat_window", None)
        if chat is not None and chat.isVisible():
            return
        if self.config.always_on_top:
            windows_overlay.keep_on_top(self)

    # -- mouse drag (P0) ----------------------------------------------------
    def _sim_point(self, global_pos) -> tuple[float, float] | None:
        screen = self.screen()
        return cursor_sim_point(
            global_pos,
            self.pos(),
            screen.geometry() if screen is not None else None,
            self.scale,
            self.simulation.bounds,
        )

    def _left_button_down(self) -> bool:
        """Windows 下查物理按键状态：光标移出窗口后 Qt 收不到 release，靠它兜底。"""
        if sys.platform != "win32":
            return True
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception:
            return True

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        # 穿透模式收不到事件（这里只是兜底）；暂停时禁止拖拽 —— 暂停不推进 tick，
        # drag_point 永远不会被消费，画面上人偶不会动（审计 P0-6）。
        if self.click_through or self._paused:
            event.ignore()
            return
        # 投影按钮优先：只记下按下的是哪个，松开时还在同一个按钮里才算点击（防误触，批准类尤其要紧）
        pressed = self.overseer.button_at((event.position().x(), event.position().y()))
        if pressed is not None:
            self._pressed_button = pressed
            event.accept()
            return
        point = self._sim_point(event.globalPosition())
        kind = hit_test(self._drawn_state, point, self.scale)
        if kind is None:
            event.ignore()
            return
        # 头/躯干都是拖 chunk0（头被 _weld 焊在 chunk0 上）；底座走 P1 的沿轨道拖拽
        mode = "base" if kind == "base" else "body"
        if not self.simulation.begin_drag(mode, point):
            event.ignore()
            return
        self._press_global = event.globalPosition()
        self._press_at = time.perf_counter()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        pressed, self._pressed_button = self._pressed_button, None
        if pressed is not None:
            if self.overseer.button_at((event.position().x(), event.position().y())) == pressed:
                self.projections.click(pressed)
            event.accept()
            return
        self.simulation.end_drag()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # 拖拽位置由 _step_once 里**每 tick** 采一次 QCursor.pos()；这里只兜住
        # "按下后还没到下一个 tick"的那一下移动。
        if getattr(self.simulation, "dragging", False):
            point = self._sim_point(event.globalPosition())
            if point is not None:
                self.simulation.drag_to(point)
        super().mouseMoveEvent(event)

    def trigger_kill(self) -> None:
        """调试入口：把心情直接拉到闸值以下，下一 tick 就会强制打断并进仪式。"""
        from ..simulation import constants as C
        self.simulation.temper.value = C.TEMPER_KILL_GATE - 0.01

    # -- size ---------------------------------------------------------------
    @property
    def size_preset(self) -> str:
        return getattr(self.config, "size", "full")

    def set_size(self, key: str) -> None:
        """切换尺寸档（托盘菜单）：portable 40% / youth 75% / full 100% / pro 150%。

        换尺寸 = 换渲染倍率 = 换**仿真腔室**的大小，所以整个仿真要按新腔室重建
        （rail / wander / 缆线 / 光环锚点全在仿真坐标里，就地缩放会留下旧坐标）。
        重建是一次性的（构造时自带 320 tick 预热），代价就是一下轻微的"重摆"。
        """
        if key not in SIZE_PRESETS:
            return
        self.config.size = key
        self.config.scale = scale_for(key)
        self.apply_scale()
        save(self.config)

    def apply_scale(self) -> None:
        """按当前 config.scale 重建腔室与仿真。"""
        zoom = max(0.3, min(4.0, float(self.config.scale)))
        self.scale = zoom
        geo = self.geometry()
        bounds = (0.0, 0.0, float(geo.width()) / zoom, float(geo.height()) / zoom)
        self.simulation.end_drag(fling=False)   # 拖拽中切尺寸：先结束旧会话（不甩）
        carried_temper = self.simulation.temper.value
        seed = getattr(self.simulation, "seed", 1234)
        # **不能带预热同步构造**：构造里的 320 tick 预热随腔室面积膨胀得非常厉害
        # （1138x608 约 1.6 s，40% 档的 2845x1520 要 11.7 s —— 这就是"切尺寸卡一下"）。
        # warmup=0 只要 0.4 ms，之后让 40 TPS 的固定步长循环自然把关节/缆线跑稳，
        # 用户看到的是一秒左右"刚站住"的过渡，而不是整个界面冻住。
        old = self.simulation
        ox, oy, ow, oh = old.bounds
        here = ((old.creature.chunk0[0] - ox) / max(1.0, ow), (old.creature.chunk0[1] - oy) / max(1.0, oh))
        self.simulation = SimulationState(bounds, seed=seed, warmup=0, character=self.character,
                                          start=(here[0] * bounds[2], here[1] * bounds[3]))
        self.simulation.scale = zoom
        # 心情跨尺寸保留（否则换一下大小就"消气"），仪式状态机复位（不许留半个仪式）
        self.simulation.temper.value = carried_temper
        self.simulation.pool_lines = self.director is None
        self.simulation.set_territory(getattr(self.config, "territory", "") or None)
        # README「已知 BUG：改了尺寸之后电光需要重新设置」：新仿真的光环是默认档，补回来
        self.simulation.halo.set_spark_rate(getattr(self, "_halo_rate", self.spark_rate))
        self._sync_pearls()                      # 新仿真里珍珠从身上重新飘出来
        if self._swap_to is not None:            # 荡到一半换了尺寸：新仿真里没有动画状态，直接换
            self._finish_swap(animate=False)
        if self.director is not None:
            self.director.planner.clear()
        cursorfx.release()
        self._last = time.perf_counter()
        self.update()

    # -- controls ----------------------------------------------------------
    def toggle_pause(self) -> None:
        if not self._paused:
            self.simulation.end_drag(fling=False)   # 进入暂停：结束拖拽（暂停不消费 drag_point）
            self.simulation.kill.cancel()           # 暂停时取消仪式，否则白闪会冻在屏幕上
            self.simulation.cancel_dialogue()       # 暂停时收起气泡（审计 A2：tick 不动会冻在半截）
        cursorfx.release()
        self._paused = not self._paused
        self.sound.muted = self._paused or getattr(self, "_fullscreen_quiet", False)
        if self._paused:
            self.sound.silence()
            self._loop.pause()
        else:
            self._loop.resume()
            self._last = time.perf_counter()
        self.update()

    @property
    def paused(self) -> bool:
        return self._paused

    def set_spark_rate(self, name: str) -> None:
        """切换电光频率档位（off / eco / computing），并写回配置。"""
        self.simulation.halo.set_spark_rate(name)
        self._halo_rate = name
        self.config.spark_rate = name
        save(self.config)

    @property
    def spark_rate(self) -> str:
        return getattr(self.config, "spark_rate", "eco")

    # -- dialogue -----------------------------------------------------------
    def set_dialogue_enabled(self, enabled: bool) -> None:
        """托盘「对话」开关。关掉立刻收起当前这句。"""
        self.dialogue_enabled = bool(enabled)
        self.config.dialogue_enabled = bool(enabled)
        if not enabled:
            self.simulation.cancel_dialogue()
        self.update()
        save(self.config)

    @property
    def quiet_today(self) -> bool:
        return time.time() < float(getattr(self.config, "quiet_until", 0.0) or 0.0)

    def set_quiet_today(self, on: bool) -> None:
        """「今天别说话」：到明天 0 点前不主动开口（你找它、提醒、任务结果照常）。"""
        from datetime import datetime, timedelta

        until = 0.0
        if on:
            tomorrow = datetime.now().date() + timedelta(days=1)
            until = datetime.combine(tomorrow, datetime.min.time()).timestamp()
        self.config.quiet_until = until
        save(self.config)
        if self.director is not None:
            self.director.mute_until = until
            self.director.log("用户让你今天别说话" if on else "用户说你可以说话了")
            self.director._say(self.simulation, "……行，今天我不出声了。" if on else "……那我就接着说了。", direct=True)

    # -- 系统状态 ---------------------------------------------------------------
    def _poll_system(self) -> None:
        from . import system_status

        self._collect_pearls()
        self._collect_agent_events()
        if self.director is None:                # 没开 AI 时提醒也要响（/remind 是纯本地的）；开着时导演自己响
            for text in self.reminders.pop_due():
                self.chatlog.add("system", f"提醒：{text}")
                self.simulation.say(text)

        fullscreen = bool(getattr(self.config, "auto_quiet", True)
                          and system_status.foreground_is_fullscreen())
        # 连续两次读数一致才切换：单次误读不至于让桌宠一闪一闪
        if fullscreen != self._fullscreen_quiet:
            self._fullscreen_votes += 1
            if self._fullscreen_votes >= 2:
                self._fullscreen_votes = 0
                self._set_fullscreen_quiet(fullscreen)
        else:
            self._fullscreen_votes = 0
        if self._fullscreen_quiet:
            return

        self._check_task()
        cpu = self._cpu.sample()
        user_rate = getattr(self.config, "spark_rate", "eco")
        if self.task_busy and user_rate != "off":
            # 替你干活的时候光环全速演算
            cpu = max(cpu, 1.0)
        if getattr(self.config, "cpu_halo", True) and user_rate != "off":
            # 迟滞：60% 进 computing，45% 才退，免得在门槛上来回闪
            if cpu > 0.6:
                rate = "computing"
            elif cpu < 0.45:
                rate = user_rate
            else:
                rate = self._halo_rate
        else:
            rate = user_rate
        if rate != self._halo_rate:
            self._halo_rate = rate
            self.simulation.halo.set_spark_rate(rate)

        power = system_status.battery()
        director = self.director
        if director is not None:
            director.system = {"cpu": round(cpu, 2),
                               "battery": None if power is None else power[0],
                               "charging": None if power is None else power[1]}
        if power is not None:
            percent, charging = power
            if not charging and percent <= 20 and not self._battery_warned:
                self._battery_warned = True
                if director is not None:
                    director.bump("battery")
            elif charging or percent > 30:
                self._battery_warned = False

    def _set_fullscreen_quiet(self, quiet: bool) -> None:
        """全屏程序（游戏 / 视频 / 演示）时整个藏起来、停掉仿真；退出全屏再出来。

        没有"退到边上"：透明窗口盖在全屏视频/游戏上面，哪怕只占一角也挡东西，
        而且停掉 40 TPS 的仿真打游戏时一点 CPU 都不占。
        """
        self._fullscreen_quiet = quiet
        self.sound.muted = quiet or self._paused
        if quiet:
            self.sound.silence()
            self.simulation.end_drag(fling=False)
            self.simulation.cancel_dialogue()
            self._timer.stop()
            self.hide()
        else:
            self._last = time.perf_counter()
            self._timer.start(max(4, int(1000 / max(15, self.config.fps))))
            self.show()
        if self.director is not None:
            self.director.quiet = quiet
        # 勿扰时把全局快捷键让出来：游戏里同一组合键得能用
        for name in ("_hotkey", "_chat_hotkey"):
            hotkey = getattr(self, name, None)
            if hotkey is not None:
                (hotkey.suspend if quiet else hotkey.resume)()

    # -- 对话窗口 / 委托任务 ------------------------------------------------------
    def open_quick_bar(self) -> None:
        """快捷键（默认 Ctrl+Alt+I）：人偶头顶弹一行输入条，聊天或 / 命令。再按一次收起。"""
        from .quick_bar import QuickBar

        if getattr(self, "_quick_bar", None) is None:
            self._quick_bar = QuickBar(self)
        if self._quick_bar.isVisible():
            self._quick_bar.hide()
        else:
            if self.overseer_enabled:
                # 监视者出来给输入框"投影"：真正的输入框还是 QuickBar 这个窗口（输入法要在真窗口里才正常），
                # 先弹在人偶头顶，下一 tick 起 _step_overseer 把它贴到监视者选好的位置
                self.projections.post("input", PRI_INPUT, [], ttl=None)
            self._quick_bar.popup()

    def open_chat(self) -> None:
        """右键「对话窗口…」/ 输入条里的 /chat 或 Ctrl+Enter。"""
        from .chat_window import ChatWindow

        if self._chat_window is None:
            self._chat_window = ChatWindow(self)
            geo = self.geometry()
            self._chat_window.move(geo.right() - self._chat_window.width() - 24,
                                   geo.bottom() - self._chat_window.height() - 24)
        self._chat_window.open()

    def send_screen_chat(self, text: str) -> None:
        """读屏请求：先收起输入界面，后台抓屏后再发问；整个过程不落截图文件。"""
        director = self.director
        if director is None or director.perception is None:
            return
        eyes = director.perception
        target = eyes.window
        if target is None or target.private:
            director.say_to(text, screen_capture_attempted=True)
            return
        key, mode = (target.hwnd, target.title), director.screen_vision_mode
        chat = self._chat_window if self._chat_window is not None and self._chat_window.isVisible() else None
        if chat is not None:
            chat.hide()
        result = {"ready": False, "image": None}
        started = time.monotonic()

        def check() -> None:
            if not result["ready"] and time.monotonic() - started < 3.0:
                QTimer.singleShot(40, check)
                return
            if chat is not None:
                chat.open()
            if self.director is director:
                from ..brain.director import _log

                image = result["image"] if result["ready"] else None
                _log(f"屏幕看图：{'已截 JPEG ' + str(len(image)) + ' bytes' if image else '未取得截图'}"
                     f"（{mode}）")
                director.say_to(text, image=image,
                                screen_capture_attempted=True)

        def begin() -> None:
            def capture() -> None:
                try:
                    result["image"] = eyes.capture_vision(mode, key, own_ui_hidden=True)
                except Exception:                       # noqa: BLE001
                    result["image"] = None
                finally:
                    result["ready"] = True

            threading.Thread(target=capture, daemon=True, name="IteratorPet-ScreenVision").start()
            check()

        QTimer.singleShot(180, begin)

    def _web_tools(self):
        """执行器的联网工具（读网页一直可用；搜索要配 ``search_*``）。是否给某个任务，由确认卡上的「联网」决定。"""
        from ..brain.web import WebTools

        return WebTools(self.config.search_provider, self.config.search_api_key, self.config.search_base_url)

    def task_workspace(self) -> str:
        from ..brain.tasks import default_workspace

        return getattr(self.config, "task_workspace", "") or str(default_workspace())

    @property
    def current_task(self):
        return self._runner.current

    @property
    def task_busy(self) -> bool:
        return self.agent.busy or self.tasks.busy

    def start_task(self, text: str, mode: str, workspace: str, web: bool = False) -> bool:
        """只由用户亲手发起：对话窗口的确认卡，或快捷输入条的 ``/work``——导演（自主的那个模型）没有这条路。

        ``mode``：read-only / workspace-write 走自带执行器；dsh = DeepSeek Harness 只读沙箱。
        """
        from ..brain.executor import MODES

        if not llm_api_key(self.config):
            self.chatlog.add("system", "还没设置 DeepSeek API Key（右键 → AI → API Key…）。")
            return False
        if self.task_busy:
            self.chatlog.add("system", "已经有一个任务在跑（/stop 停掉）。")
            return False
        runner = self.tasks if mode == "dsh" else self.agent
        try:
            task = (runner.start(text, "read-only", workspace) if mode == "dsh"
                    else runner.start(text, mode, workspace, web=web))
        except Exception as exc:                      # noqa: BLE001
            self.chatlog.add("system", f"没能开始：{exc}")
            return False
        task.character, task.label = self.character, MODES[mode]
        self._runner = runner
        self._task_log_seen = 0
        if workspace != self.task_workspace():
            self.config.task_workspace = workspace
            save(self.config)
        if self.director is not None:
            self.director.task_workspace = workspace
        online = "，可联网" if getattr(task, "web", False) else ""
        self.chatlog.add("task", f"开始演算（{MODES[mode]}{online}，{workspace}）：{text}")
        if self.director is not None:
            self.director.log(f"你开始替用户演算：{text[:40]}")
        self.simulation.say("……开始演算。")
        return True

    def _natural_task(self, proposal: dict, user_text: str) -> None:
        """模型只选任务；应用核对范围并决定直接只读或展示确认卡。"""
        if self.task_busy:
            self.chatlog.add("system", "上一件任务还在进行；可以等它结束，或用 /stop 停下。")
            return
        goal, mode = proposal["goal"], proposal["mode"]
        known = str(Path(self.task_workspace()).resolve())
        suggested = proposal.get("workspace")
        # 模型不能凭空扩大范围；新路径只作为确认卡的预填值。
        requested = str(suggested).strip() if isinstance(suggested, str) else ""
        same_scope = not requested or Path(requested).resolve() == Path(known)
        if mode == "read-only" and same_scope and Path(known).is_dir():
            if self.start_task(goal, mode, known) and not self.overseer_enabled:
                self.open_chat()                      # 监视者投影开着时进度走投影，不自己弹窗
            return

        folder = requested or known
        self.chatlog.add("system", "请核对这件任务的文件夹和权限，再决定是否开始。")

        def show_proposal():
            if self.task_busy:
                return
            if self.overseer_enabled:
                self._offer_projection(goal, folder, mode)
                return
            self.open_chat()
            self._chat_window.offer_task(goal, folder, mode)

        QTimer.singleShot(0, show_proposal)

    def _offer_projection(self, goal: str, folder: str, mode: str) -> None:
        """接活确认走监视者投影：模式 / 文件夹在投影上选，勾才开始（和确认卡同一套规则，没有默认动作）。"""
        def start(g: str, m: str, f: str) -> None:
            if not self.start_task(g, m, f, web=self._confirm_offer.web):
                self.projections.post("notice", PRI_NOTICE, [Line("没能开始", "title"),
                                      Line("原因记在对话窗口里（右键 → 对话窗口）。")], ttl=STATUS_TTL)

        def cancel() -> None:
            self.chatlog.add("system", "这件任务先不开始。")
            self.projections.post("notice", PRI_NOTICE, [Line("已取消", "title"), Line("这件任务先不开始。")],
                                  ttl=STATUS_TTL)

        def browse(current: str) -> str | None:
            chosen = QFileDialog.getExistingDirectory(None, "任务文件夹", current)
            return chosen or None

        self._confirm_offer = ConfirmOffer(self.projections, goal, folder, mode, dsh=self.tasks.available,
                                           on_start=start, on_cancel=cancel, browse=browse)
        self._confirm_offer.post()

    def cancel_task(self) -> None:
        self._runner.cancel()

    def answer_approval(self, allowed: bool) -> None:
        """对话窗口里命令确认的「允许 / 拒绝」。"""
        if self._runner is self.agent:
            self.agent.answer(allowed)

    def _task_chatlog(self, task):
        """任务记进接活那个角色的对话记录（中途切了角色也不串）。"""
        if not task.character or task.character == self.character:
            return self.chatlog
        from ..brain.chatlog import ChatLog
        from ..brain.profiles import character_dir

        return ChatLog(character_dir(task.character) / "chat.jsonl")

    def _check_task(self) -> None:
        task = self.current_task
        if task is None:
            return
        while task.usage:                             # 执行器的请求也记进用量
            self.usage.record(task.usage.pop(0), source="task")
        if len(task.log) > self._task_log_seen:
            log = self._task_chatlog(task)
            for note in task.log[self._task_log_seen:]:
                log.add("task", note)
            self._task_log_seen = len(task.log)
        approval = task.approval
        if approval is not None and approval is not self._approval_seen:
            self._approval_seen = approval            # 要批准命令：监视者投影关着才把对话窗口叫出来
            if not self.overseer_enabled:
                self.open_chat()
            self.simulation.say("……这一步要你点头。")
        if not task.finished or task is self._task_reported:
            return
        self._task_reported = task
        minutes = max(1, int((time.time() - task.started) / 60))
        log = self._task_chatlog(task)
        if task.state == "done":
            log.add("task", task.result)
        else:
            labels = {"cancelled": "已停止", "timeout": "超时", "failed": "失败"}
            log.add("task", f"{labels.get(task.state, task.state)}（{minutes} 分钟）："
                            f"{task.error or '没有结果'}")
        if task.state == "cancelled" or log is not self.chatlog:
            return
        if self.director is not None:
            self.director.task_finished(task.result or task.error, task.state == "done")
        else:
            self.simulation.say("演算完成。" if task.state == "done" else "……演算失败了。")

    # -- 画面 -------------------------------------------------------------------
    SOUND_OPTIONS = ("sound_enabled", "sound_speech", "sound_actions")

    def set_sound_option(self, name: str, value) -> None:
        """「声音」菜单：总开关 / 说话声 / 动作声 / 音量。立刻生效并写回配置。"""
        if name == "sound_volume":
            value = max(0.0, min(1.0, float(value)))
        elif name not in self.SOUND_OPTIONS:
            return
        else:
            value = bool(value)
        setattr(self.config, name, value)
        if not self.sound.enabled:
            self.sound.silence()
        save(self.config)

    VIEW_OPTIONS = ("pixel_art", "pixel_snap", "drop_shadow", "show_pearls")

    def set_view_option(self, name: str, value: bool) -> None:
        """「画面」菜单：像素风 / 像素对齐 / 光影 / 监视者投影。立刻生效并写回配置。"""
        if name == "overseer_enabled":              # 配置里存的是 ui_style 字符串，不是布尔
            self.overseer_enabled = bool(value)
            self.config.ui_style = "overseer" if value else "windows"
            save(self.config)
            return
        if name not in self.VIEW_OPTIONS:
            return
        setattr(self, name, bool(value))
        setattr(self.config, name, bool(value))
        self._frame_key = None
        self.update()
        save(self.config)

    # -- LLM 导演 -------------------------------------------------------------
    def _setup_brain(self) -> None:
        """按 config 建/拆导演。拆掉时清掉残留指令，台词池接回来。"""
        from ..brain.director import Director
        from ..brain.perception import DEFAULT_BLACKLIST, Perception

        if self.director is not None:
            if self.director.perception is not None:
                self.director.perception.stop_events()
            self.director.planner.clear(self.simulation)
            self.director.save()
        self.director = None
        client = self.llm_client() if getattr(self.config, "llm_enabled", False) else None
        if client is not None:
            cfg = self.config
            perception = Perception(cfg.perception,
                                    cfg.perception_blacklist or DEFAULT_BLACKLIST)
            if self._ai_panel is not None and self._ai_panel.isVisible():
                perception.set_own_overlay(self._ai_panel._screen_rect())
            perception.start_events()
            self.director = Director(
                client,
                idle_seconds=cfg.llm_plan_refresh_seconds,
                perception=perception,
                memory=self.memory,
                usage=self.usage,
                reminders=self.reminders,
                daily_token_cap=cfg.llm_daily_token_cap,
                on_territory=self._remember_territory,
                talkativeness=cfg.llm_talkativeness,
                chatlog=self.chatlog,
                context=self.context,
                pearls=self.pearls,
                on_task_request=self._natural_task,
                task_workspace=self.task_workspace(),
                # 心事要看别的窗口：「读屏幕」关掉时一并不办
                errands=self.errands if cfg.perception != "off" else None,
            )
            from ..brain import sensors

            sensors.BLACKLIST = tuple(cfg.perception_blacklist or DEFAULT_BLACKLIST)
            self.director.vision_model = getattr(cfg, "llm_vision_model", "") or None
            self.director.screen_vision_mode = getattr(cfg, "screen_vision", "off")
            self.director.mute_until = float(getattr(cfg, "quiet_until", 0.0) or 0.0)
        self.simulation.pool_lines = self.director is None
        self.simulation.pending_events.clear()
        if self.director is None:
            self.simulation.clear_directive()

    def llm_client(self, timeout: float = 20.0):
        """按当前配置建一个客户端；没 Key 返回 None。"""
        from ..brain.client import ChatClient, LLMError

        key = llm_api_key(self.config)
        if not key:
            return None
        try:
            return ChatClient(key, self.config.llm_base_url, self.config.llm_model, timeout=timeout)
        except LLMError:
            return None

    def _load_character_data(self) -> None:
        """当前角色的记忆 / 对话记录 / 上下文。切换角色时整套换掉，像切换对话。"""
        from ..brain.chatlog import ChatLog
        from ..brain.context import Context
        from ..brain.memory import Memory
        from ..brain.pearls import PearlStore
        from ..brain.profiles import character_dir

        folder = character_dir(self.character)
        self.memory = Memory(folder / "memory.json")
        self.chatlog = ChatLog(folder / "chat.jsonl")
        self.context = Context(folder / "context.json")
        self.pearls = PearlStore(folder / "pearls")
        self.simulation.pearls.reading = None
        self._sync_pearls()

    # -- 珍珠 -------------------------------------------------------------------
    def _sync_pearls(self, spawn=None, include: str | None = None) -> None:
        """把最近几颗交给仿真去漂；``include`` = 正在读的那颗（可能不在最近几颗里）。"""
        from ..brain.pearls import kind_color
        from ..simulation.pearls import MAX_SHOWN

        shown = self.pearls.recent(MAX_SHOWN)
        extra = self.pearls.get(include) if include else None
        if extra is not None and extra not in shown:
            shown = shown[1:] + [extra] if len(shown) >= MAX_SHOWN else shown + [extra]
        self.simulation.pearls.set_pearls([(p["id"], kind_color(p["kind"])) for p in shown],
                                          spawn=spawn, center=self.simulation.creature.chunk0)

    def give_pearl(self, path: str | None = None, text: str | None = None, spawn=None) -> bool:
        """递珍珠：拖到人偶身上、拖进对话窗口、对话窗口的「递珍珠」、/pearl。

        一段文字马上收；文件 / 网址 / 图 / 音视频交给后台的 ``PearlMaker``（截帧、下载、认字都慢），
        做好了在 ``_collect_pearls`` 里收下再读。``path`` 也可以是网址。
        """
        from ..brain.pearl_media import Draft, is_url
        from ..brain.pearls import PearlError, from_text

        if path is None and text is not None and is_url(text):
            path, text = text.strip(), None
        if path is None:
            try:
                title, kind, body = from_text(text or "")
            except PearlError as exc:
                self.chatlog.add("system", f"这颗珍珠读不了：{exc}")
                return False
            self._accept_pearl(Draft(title, kind, body, "直接递来的文字"), spawn)
            return True
        if self.director is None:
            self.set_llm_enabled(True)               # 没 Key 会先弹设置框；取消了就先收着
        self.pearl_maker.start(str(path), extra=spawn)
        self.simulation.say("……让我看看。")
        return True

    def _collect_pearls(self) -> None:
        """后台做好的珍珠：收下、绕着飘、读。"""
        for draft, error, spawn in self.pearl_maker.poll():
            if draft is None:
                self.chatlog.add("system", f"这颗珍珠读不了：{error}")
                _drop_log(f"珍珠没做成：{error[:200]}")
                self.simulation.say("……这不是我能读的东西。")
                continue
            self._accept_pearl(draft, spawn)

    def _collect_agent_events(self) -> None:
        """编程 agent 发来的通知：记进对话记录；开着 AI 让它用自己的口吻说，没开就说一句固定的。"""
        if self.bridge is None:
            return
        for event in self.bridge.poll():
            where = f"（{event['project']}）" if event.get("project") else ""
            detail = event.get("message") or event.get("title") or ""
            self.chatlog.add("system", f"{event['source']}{where}：{detail or event['kind']}")
            if self.director is not None:
                self.director.agent_event(event)
            else:
                line = AGENT_LINES.get(event["kind"], AGENT_LINES["info"]).format(
                    project=event.get("project") or event["source"])
                self.simulation.say(line)

    def show_bridge_setup(self) -> None:
        """右键「AI → 接入编程 agent…」：给出可以直接复制的配置。"""
        from ..brain.bridge import bridge_dir, setup_text
        from .ui_theme import accent_for, spaced

        if self.bridge is None:
            text = "本机接口没开（config.json 的 bridge_enabled），或者端口被占用了，看 launcher.log 的 [drop] 记录。"
        else:
            text = setup_text(self.bridge.settings, bridge_dir())
        from .ui_theme import CopyDialog

        self._top_timer.stop()
        try:
            CopyDialog(spaced("接入编程 agent"), text, accent_for(self.character)).exec()
        finally:
            if self.config.always_on_top:
                self._top_timer.start()

    def _accept_pearl(self, draft, spawn=None) -> None:
        from ..brain.pearls import kind_label

        if self.director is None and draft.kind == "note":
            self.set_llm_enabled(True)
        pearl = self.pearls.add(draft.title, draft.kind, draft.source, draft.text, image=draft.image)
        self._sync_pearls(spawn=spawn)
        extra = "，带一张图" if pearl.get("image") else ""
        self.chatlog.add("system", f"递了一颗珍珠「{draft.title}」（{kind_label(draft.kind)}，"
                                   f"{len(draft.text)} 字{extra}）")
        if self.director is not None:
            self.director.read_pearl(pearl["id"])
        else:
            self.simulation.say("……先收着。")

    def reread_pearl(self, pearl_id: str) -> None:
        if self.director is None or self.pearls.get(pearl_id) is None:
            return
        self._sync_pearls(include=pearl_id)
        self.director.read_pearl(pearl_id)

    def remove_pearl(self, pearl_id: str) -> bool:
        pearl = self.pearls.get(pearl_id)
        if pearl is None or not self.pearls.remove(pearl_id):
            return False
        if self.simulation.pearls.reading == pearl_id:
            self.simulation.pearls.reading = None
        self._sync_pearls()
        self.chatlog.add("system", f"扔掉了珍珠「{pearl['title']}」")
        return True

    # -- 珍珠传话 ----------------------------------------------------------------
    @property
    def other_character(self) -> str:
        return "past_moon" if self.character == "pebbles" else "pebbles"

    def hand_over_pearl(self, pearl_id: str, user_note: str = "") -> dict | None:
        """把一颗珍珠托用户转给另一位迭代器（右键「珍珠 → 转给…」/ ``/give``）。

        珍珠连正文、图、它读后的话一起搬到对方的 ``pearls\\`` 里，标成"转来的、没读"；
        对方下次被切出来、开场白说完之后会读它，并且知道是谁给的（``gift``）。
        """
        import random

        from ..brain.chatlog import ChatLog
        from ..brain.pearls import PearlStore
        from ..brain.profiles import character_dir
        from .ui_theme import NAMES

        other = self.other_character
        giver = NAMES.get(self.character, self.character)
        receiver = NAMES.get(other, other)
        title = (self.pearls.get(pearl_id) or {}).get("title")
        moved = self.pearls.hand_over(pearl_id, PearlStore(character_dir(other) / "pearls"),
                                      giver, self.character, user_note)
        if moved is None:
            return None
        if self.simulation.pearls.reading == pearl_id:
            self.simulation.pearls.reading = None
        self._sync_pearls()
        note = f"，捎了句话：{user_note.strip()}" if user_note.strip() else ""
        self.chatlog.add("system", f"把珍珠「{title}」转给了{receiver}{note}")
        ChatLog(character_dir(other) / "chat.jsonl").add("system", f"{giver}托这个人转来一颗珍珠「{title}」{note}")
        if self.director is not None:
            self.director.log(f"你把珍珠「{title}」托这个人转给了{receiver}")
        self.simulation.say(random.choice(GIFT_LINES.get(self.character, GIFT_LINES["pebbles"])))
        return moved

    def ask_hand_over(self, pearl_id: str) -> None:
        """右键「珍珠 → 转给…」：先问一句要不要捎话（可以不写），取消就不转。"""
        from .ui_theme import NAMES

        receiver = NAMES.get(self.other_character, "另一位")
        note = self._ask_text(f"转给{receiver}", f"要捎句话给{receiver}吗？（可以不写，直接确定）")
        if note is not None:
            self.hand_over_pearl(pearl_id, note)

    def _tick_gifts(self) -> None:
        """转来的珍珠：等开场白说完、手上没别的事，再一颗一颗读。"""
        director = self.director
        if (director is None or self._swap_to is not None or time.perf_counter() < self._gift_wait_until
                or director.busy or director.reading or self.simulation.dialogue.text):
            return
        for pearl in self.pearls.unread_gifts():
            if pearl["id"] not in self._gifts_queued:
                self._gifts_queued.add(pearl["id"])
                self._sync_pearls(include=pearl["id"])
                director.read_pearl(pearl["id"])
                self._gift_wait_until = time.perf_counter() + 2.0
                return

    def _remember_position(self) -> None:
        """退出时记下人偶在屏幕上的位置（比例），下次从这里出来。换角色动画中途退出就不记（在屏幕外）。"""
        sim = self.simulation
        if sim.swap_phase is not None:
            return
        x, y, w, h = sim.bounds
        cx, cy = sim.creature.chunk0
        self.config.last_position = [round((cx - x) / max(1.0, w), 4), round((cy - y) / max(1.0, h), 4)]
        save(self.config)

    def drop_errand(self, errand_id: str) -> None:
        """状态面板「放下」：这件心事不盯了。"""
        errand = self.errands.finish(errand_id)
        if errand is None:
            return
        if self.director is not None and self.director.watcher is not None:
            self.director.watcher.reset(errand_id)
        self.chatlog.add("system", f"放下了心事「{errand['goal']}」")

    def remove_all_pearls(self, confirm: bool = True) -> int:
        """右键「珍珠 → 全部扔掉…」/ ``/drop all``。只扔当前角色的。"""
        count = len(self.pearls.items)
        if not count:
            return 0
        if confirm:
            from .ui_theme import ConfirmDialog, NAMES, accent_for, spaced

            dialog = ConfirmDialog(spaced("扔掉珍珠"),
                                   f"把{NAMES.get(self.character, '它')}收着的 {count} 颗珍珠全部扔掉吗？撤不回来。",
                                   accent_for(self.character), confirm="全部扔掉", danger=True)
            if not dialog.ask():
                return 0
        self.pearls.clear()
        self.simulation.pearls.reading = None
        self._sync_pearls()
        self.chatlog.add("system", f"扔掉了全部 {count} 颗珍珠")
        return count

    @staticmethod
    def pearl_payload(mime, peek: bool = False) -> tuple[list[str], str]:
        """(本地文件路径或网址, 文字)。剪贴板 / 拖进来的图（截图、浏览器里的图）先存成临时 png；
        ``peek``（拖着经过时）只判断有没有东西，不存图。"""
        from ..brain.pearl_media import is_url

        paths = []
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    paths.append(url.toLocalFile())
                elif url.scheme() in ("http", "https"):
                    paths.append(url.toString())
        if not paths and mime.hasImage() and peek:
            return ["（图）"], ""
        if not paths and mime.hasImage():
            import tempfile
            from pathlib import Path

            from PySide6.QtGui import QImage

            image = QImage(mime.imageData())
            if not image.isNull():
                target = Path(tempfile.gettempdir()) / f"剪贴板图片-{time.strftime('%H%M%S')}.png"
                if image.save(str(target)):
                    paths.append(str(target))
        text = mime.text() if (not paths and mime.hasText()) else ""
        if is_url(text):
            paths, text = [text.strip()], ""
        return paths, text

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        paths, text = self.pearl_payload(event.mimeData(), peek=True)
        ok = bool(paths or text.strip())
        _drop_log(f"拖入 {'接受' if ok else '拒绝'}：{len(paths)} 个文件，"
                  f"格式 {event.mimeData().formats()[:4]}")
        if ok:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths, text = self.pearl_payload(event.mimeData())
        spawn = self._sim_point(QCursor.pos())
        _drop_log(f"放下：{len(paths)} 个文件 / {len(text)} 字")
        event.acceptProposedAction()
        self.drop_files(paths, spawn, text)

    def drop_files(self, paths, spawn=None, text: str = "") -> None:
        # 放下这一刻资源管理器还在等返回：真正的处理（可能弹 Key 设置框）放到下一轮事件循环
        def handle():
            if paths:
                for path in paths[:3]:
                    self.give_pearl(path=path, spawn=spawn)
            elif text.strip():
                self.give_pearl(text=text, spawn=spawn)
        QTimer.singleShot(0, handle)

    def clear_memory(self) -> None:
        """面板「清空记忆」：只清当前角色的记忆、上下文、对话记录和珍珠。"""
        self.memory.clear()
        self.context.clear()
        self.chatlog.clear()
        self.pearls.clear()
        self.simulation.pearls.reading = None
        self._sync_pearls()
        if self.director is not None:
            self._setup_brain()                  # 正在路上的请求作废，免得把旧东西写回来

    # -- 地盘 -------------------------------------------------------------------
    def _remember_territory(self, side: str | None) -> None:
        self.config.territory = side or ""
        save(self.config)

    def set_territory(self, side: str | None) -> None:
        """「待在」菜单：left / right / top / bottom / None（满屏自由）。"""
        if self.director is not None:
            self.director.planner.clear(self.simulation)
        self.simulation.set_territory(side)
        self._remember_territory(side)

    @property
    def territory(self) -> str:
        return getattr(self.config, "territory", "") or ""

    def set_perception(self, level: str) -> None:
        if level not in ("off", "title", "ocr"):
            return
        self.config.perception = level
        if self.director is not None and self.director.perception is not None:
            perception = self.director.perception
            perception.level = level
            if level == "ocr":
                perception.start_events()
            else:
                perception.stop_events()
        save(self.config)

    def set_screen_vision(self, mode: str) -> None:
        if mode not in ("off", "roi", "full"):
            return
        self.config.screen_vision = mode
        if self.director is not None:
            self.director.screen_vision_mode = mode
        save(self.config)

    def show_ai_panel(self) -> None:
        from .ai_panel import AIPanel

        if self._ai_panel is None:
            self._ai_panel = AIPanel(self)
        self._ai_panel.show()
        self._ai_panel.raise_()

    @property
    def llm_enabled(self) -> bool:
        return self.director is not None

    def set_llm_enabled(self, enabled: bool) -> None:
        """托盘「AI 驱动」开关。没 Key 时先弹设置框。"""
        if enabled and not llm_api_key(self.config):
            if not self.configure_llm():
                return
        self.config.llm_enabled = bool(enabled)
        self._setup_brain()
        save(self.config)

    def _ask_text(self, title: str, label: str, text: str = "", password: bool = False):
        """弹一个置顶输入框。期间停掉 keep-on-top，否则每秒一次的 SWP_TOPMOST 会把它盖住。"""
        from .ui_theme import TextPrompt, accent_for, spaced

        dialog = TextPrompt(spaced(title), label, accent_for(self.character), text=text,
                            password=password)
        self._top_timer.stop()
        try:
            return dialog.value()
        finally:
            if self.config.always_on_top:
                self._top_timer.start()

    def configure_llm(self) -> bool:
        """设置 DeepSeek API Key（写进 config.json；也可以只用环境变量）。"""
        value = self._ask_text(
            "AI 设置",
            "DeepSeek API Key（留空 = 清除；也可以设环境变量 DEEPSEEK_API_KEY）：",
            self.config.llm_api_key,
            password=True,
        )
        if value is None:
            return False
        self.config.llm_api_key = value.strip()
        save(self.config)
        if self.director is not None or getattr(self.config, "llm_enabled", False):
            self._setup_brain()
        return bool(llm_api_key(self.config))

    # -- character ----------------------------------------------------------
    @property
    def character(self) -> str:
        return getattr(self.config, "character", "pebbles")

    def set_character(self, key: str) -> None:
        """切换角色（托盘「角色」菜单）。外观参数在 simulation/appearance.py。

        动画：人偶连着机械臂荡出屏幕 → 看不见的那一刻换外观和数据 → 荡回来，新角色说一句
        开场白（``state.begin_swap`` / ``_finish_swap``）。拖拽 / 仪式中荡不了就直接换。
        外观：**仿真一行不动**，不重建 SimulationState，只把 appearance 换掉。
        大脑：像切换对话——当前角色的上下文先存盘，整套记忆 / 对话记录 / 上下文换成另一个
        角色的，导演重建（路上的请求作废、日程清空）。用量和提醒两边共用。
        """
        from ..simulation.appearance import CHARACTERS

        if key not in CHARACTERS or key == self.character or getattr(self, "_swap_to", None):
            return
        self.simulation.end_drag(fling=False)
        self.simulation.kill.cancel()
        self.simulation.cancel_dialogue()        # 换角色不许串台词（审计 A3）
        if self.director is not None:
            self.director.save()
            self.director.planner.clear(self.simulation)
        for name in ("_chat_window", "_ai_panel"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.close()
                setattr(self, name, None)
        if not self._paused and self.simulation.begin_swap():
            # 荡出去的这几秒：旧导演停掉、台词池也别插嘴
            self.sound.play("swap")                # 原作迭代器退出工作模式的那一声
            self._swap_to = key
            self.director = None
            self.simulation.pool_lines = False
            return
        self._swap_to = key
        self._finish_swap(animate=False)

    def _finish_swap(self, animate: bool = True) -> None:
        """人偶已经在屏幕外了（或者不做动画）：真正换角色。"""
        from ..simulation.appearance import CHARACTERS

        key, self._swap_to = self._swap_to, None
        if key is None:
            return
        self.config.character = key
        self._load_character_data()
        self._setup_brain()
        self._gift_wait_until = time.perf_counter() + 7.0   # 先荡回来、说完开场白
        self._gifts_queued = set()
        if animate:
            self.simulation.swap_back(CHARACTERS[key])   # 荡回来；到了发 arrive → 开场白
        else:
            self.simulation.appearance = CHARACTERS[key]
            if self.director is not None:
                self.director.bump("arrive")
            else:
                from ..simulation.constants import ARRIVE_LINES

                line = self.simulation.lines.next_line(key, "arrive")
                self.simulation.say(line or ARRIVE_LINES[key][0])
        # 界面强调色跟角色走：菜单立刻换；已打开的对话窗口 / 面板关掉重开时换
        from .ui_theme import apply_theme

        apply_theme(QApplication.instance(), key)
        self.update()
        save(self.config)

    def toggle_click_through(self) -> None:
        self.simulation.end_drag(fling=False)
        self.simulation.kill.cancel()          # 穿透后就收不到事件了，先结束拖拽
        cursorfx.release()
        self.click_through = not self.click_through
        self._apply_click_through()
        self.config.click_through = self.click_through
        save(self.config)

    def _apply_click_through(self) -> None:
        applied = windows_overlay.set_click_through(self, True)
        # Qt-level fallback (also keeps behaviour sane on non-Windows).
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self.click_through)
        self._click_through_native = applied
        self._interactive = None              # 关掉全穿透后下一帧按光标位置重新判

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_pause()
        elif key == Qt.Key.Key_Escape:
            self.close()
        elif key == Qt.Key.Key_C:
            self.toggle_click_through()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """右键菜单：托盘图标被 Windows 折进溢出区时，这是最直接的退出入口。"""
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QMenu

        from .tray import add_ai_actions

        menu = QMenu(self)
        add_ai_actions(menu, self)

        action_pause = QAction("继续" if self._paused else "暂停", menu)
        action_pause.triggered.connect(self.toggle_pause)
        menu.addAction(action_pause)

        action_click = QAction("鼠标穿透", menu)
        action_click.setCheckable(True)
        action_click.setChecked(bool(self.click_through))
        action_click.triggered.connect(self.toggle_click_through)
        menu.addAction(action_click)

        action_auto = QAction("自动批准命令（危险的仍会问）", menu)
        action_auto.setCheckable(True)
        action_auto.setChecked(bool(getattr(self.config, "auto_approve_all", False)))

        def _toggle_auto(checked: bool) -> None:
            self.config.auto_approve_all = bool(checked)
            save(self.config)

        action_auto.triggered.connect(_toggle_auto)
        menu.addAction(action_auto)

        menu.addSeparator()
        action_cursor = QAction("光标特效（杀鼠标时）", menu)
        action_cursor.setCheckable(True)
        action_cursor.setChecked(bool(getattr(self.config, "cursor_fx", True)))

        def _toggle_cursor(checked: bool) -> None:
            self.config.cursor_fx = bool(checked)
            cursorfx.set_enabled(bool(checked))
            save(self.config)

        action_cursor.triggered.connect(_toggle_cursor)
        menu.addAction(action_cursor)

        action_kill = QAction("杀鼠标（调试）", menu)
        action_kill.triggered.connect(self.trigger_kill)
        menu.addAction(action_kill)

        from .tray import add_view_actions

        add_view_actions(menu, self)
        size_menu = menu.addMenu("尺寸")
        for key, (factor, label) in SIZE_PRESETS.items():
            action_size = QAction(label, size_menu)
            action_size.setCheckable(True)
            action_size.setChecked(self.size_preset == key)
            action_size.triggered.connect(
                lambda _checked=False, name=key: self.set_size(name)
            )
            size_menu.addAction(action_size)

        spark_menu = menu.addMenu("电光频率")
        for key, label in (("off", "无"), ("eco", "节能"), ("computing", "computing（1.5×）")):
            action_rate = QAction(label, spark_menu)
            action_rate.setCheckable(True)
            action_rate.setChecked(self.spark_rate == key)
            action_rate.triggered.connect(
                lambda _checked=False, name=key: self.set_spark_rate(name)
            )
            spark_menu.addAction(action_rate)

        character_menu = menu.addMenu("角色")
        for key, label in (("pebbles", "五块卵石"), ("past_moon", "仰望皓月")):
            action_character = QAction(label, character_menu)
            action_character.setCheckable(True)
            action_character.setChecked(self.character == key)
            action_character.triggered.connect(
                lambda _checked=False, name=key: self.set_character(name)
            )
            character_menu.addAction(action_character)

        action_dialogue = QAction("对话", menu)
        action_dialogue.setCheckable(True)
        action_dialogue.setChecked(bool(self.dialogue_enabled))
        action_dialogue.triggered.connect(
            lambda checked: self.set_dialogue_enabled(bool(checked))
        )
        menu.addAction(action_dialogue)

        menu.addSeparator()
        action_quit = QAction("退出", menu)
        action_quit.triggered.connect(self.close)
        menu.addAction(action_quit)

        menu.exec(event.globalPos())
        event.accept()


def saved_position(cfg, bounds) -> tuple[float, float] | None:
    """config.last_position 存的是屏幕比例（换尺寸、换分辨率也落在同一片区域）→ 仿真坐标。"""
    raw = getattr(cfg, "last_position", None)
    if not (isinstance(raw, (list, tuple)) and len(raw) == 2):
        return None
    try:
        fx, fy = (min(1.0, max(0.0, float(v))) for v in raw)
    except (TypeError, ValueError):
        return None
    x, y, w, h = bounds
    return (x + fx * w, y + fy * h)


def create_window(config: Config | None = None, sprites=None) -> PetWindow:
    app = QApplication.instance() or QApplication([])
    cfg = config or load()
    workspace = ScreenWorkspace()
    snapshot = workspace.snapshot()
    geo = pet_rect(snapshot)          # 任务栏自动隐藏时让出 2 px，免得被当成全屏程序
    zoom = max(0.3, min(4.0, float(cfg.scale)))
    bounds = (0.0, 0.0, float(geo.width()) / zoom, float(geo.height()) / zoom)
    sim = SimulationState(
        bounds,
        character=getattr(cfg, "character", "pebbles"),
        start=saved_position(cfg, bounds),
    )
    window = PetWindow(sim, cfg, sprites=sprites)
    window.setGeometry(geo)
    return window
