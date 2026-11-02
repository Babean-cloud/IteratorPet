"""快捷输入条：快捷键（默认 Ctrl+Alt+I）呼出，弹在人偶上方，一行字，回车就走。

像终端一样用：直接打字是跟它聊天（回答在人偶的气泡里）；``/`` 开头是命令（``brain/commands.py``），
敲 ``/`` 会列出命令，Tab 补全，↑↓ 翻历史，Esc 收起。``/chat`` 或 Ctrl+Enter 打开完整对话窗口。
``//`` 开头 = 想说的话本身以 / 开头。
"""

from __future__ import annotations

import json
import os

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QListWidget, QVBoxLayout, QWidget

from ..brain import commands as cmd
from .ui_theme import FAINT, GAP_S, MEDIUM_GREY, NAMES, RED_HOVER, SHADOW, _paint_frame

HISTORY_KEEP = 50
USE_HOLO_SKIN = False                          # 输入条做成监视者投影的样式（用户 2026-11-01 关掉了）
HOLO_MARGIN = 16                             # 全息皮肤的内边距（没有阴影，只留给括线和呼吸）


def execute(pet, line: str) -> tuple[bool, str]:
    """跑一行输入。返回 (是否还要留着给人看, 要显示的话)。快捷输入条和对话窗口共用。"""
    line = (line or "").strip()
    if not line:
        return True, ""
    try:
        command = cmd.parse(line)
    except cmd.CommandError as exc:
        return True, f"✗ {exc}"
    if command is None:
        return _chat(pet, line[1:] if line.startswith("//") else line)
    return globals()[f"_do_{command.name}"](pet, command)


def _chat(pet, text: str) -> tuple[bool, str]:
    if pet.director is None:
        pet.set_llm_enabled(True)
        if pet.director is None:
            return True, "✗ AI 没开（右键 → AI），现在只能用本地命令，/help 看看。"
    if (hasattr(pet, "send_screen_chat")
            and getattr(pet.director, "wants_screen_vision", lambda _text: False)(text)):
        pet.send_screen_chat(text)
    else:
        pet.director.say_to(text)
    return False, ""


def _do_work(pet, command) -> tuple[bool, str]:
    mode = cmd.work_mode(command)
    if mode == "dsh" and not pet.tasks.available:
        return True, "✗ 没找到 DeepSeek Harness，-d 用不了。"
    folder = command.flags.get("C") or pet.task_workspace()
    folder = os.path.expandvars(os.path.expanduser(folder.strip('"')))
    if not os.path.isdir(folder):
        return True, f"✗ 没有这个文件夹：{folder}"
    if not pet.start_task(command.text, mode, folder, web=bool(command.flags.get("n"))):
        return True, "✗ " + (pet.chatlog.entries[-1]["text"] if pet.chatlog.entries else "没能开始。")
    return False, ""


def _do_remind(pet, command) -> tuple[bool, str]:
    when, _, text = command.text.partition(" ")
    spec = dict(cmd.parse_when(when), text=text.strip())
    if not pet.reminders.add_spec(spec):
        return True, "✗ 这个时间定不了。"
    due = pet.reminders.describe(limit=50)
    pet.chatlog.add("system", f"定了提醒：{when} {text.strip()}")
    return True, "✓ 定好了。还没到点的：\n" + "\n".join(due)


def _do_watch(pet, command) -> tuple[bool, str]:
    if pet.director is None:
        pet.set_llm_enabled(True)
    if pet.director is None or pet.director.errands is None:
        return True, "✗ 心事要开着 AI，而且「读屏幕」不能是关的。"
    pet.chatlog.add("user", f"/watch {command.text}")
    if pet.director.add_errand(command.text) is None:
        return True, "这件事已经在盯了。"
    pet.simulation.say("……知道了，我看着。")
    return False, ""


def _do_unwatch(pet, command) -> tuple[bool, str]:
    count = pet.errands.cancel_all()
    return True, f"✓ 放下了 {count} 件心事。" if count else "现在没有心事。"


def _do_stop(pet, command) -> tuple[bool, str]:
    if not pet.task_busy:
        return True, "现在没有任务在跑。"
    pet.cancel_task()
    return True, "✓ 叫停了。"


def _do_status(pet, command) -> tuple[bool, str]:
    lines = []
    task = pet.current_task
    if task is not None and not task.finished:
        lines.append(f"任务（{task.label}）：{task.text[:40]}")
        if task.approval is not None:
            lines.append("　等你批准一条命令 → /chat")
    elif task is not None:
        lines.append(f"上一个任务：{task.state}，{task.text[:30]}")
    lines += (pet.reminders.describe(limit=10) + pet.errands.describe()) or ["没有提醒，也没有心事。"]
    lines.append(f"AI：{'开着' if pet.director is not None else '没开'}　角色：{NAMES.get(pet.character, pet.character)}")
    return True, "\n".join(lines)


def _habit_store(pet):
    return getattr(pet, "habits", None)


def _do_habits(pet, command) -> tuple[bool, str]:
    store = _habit_store(pet)
    if store is None:
        return True, "✗ 习惯系统没启动。"
    habits = store.list()
    where = f"工作区：{store.root}"
    if not habits and not store.errors:
        return True, "还没有习惯。跟它说“帮我……”，比如“我随口说的想法你帮我记下来，每天回顾”。\n" + where
    state = getattr(pet, "habit_state", None)
    lines = []
    for habit in habits:
        flag = "" if habit.enabled else "（已暂停）"
        if state is not None and state.paused(habit.name):
            flag = f"（自动暂停：{state.paused(habit.name)}）"
        lines.append(f"{habit.name}｜{habit.title}{flag}　{habit.description[:40]}")
    lines += [f"{name}｜读不了：{why[:40]}" for name, why in store.errors.items()]
    return True, "\n".join(lines + ["/habit 名字 看详情", where])


def _trigger_text(trigger: dict) -> str:
    if trigger["type"] == "chat":
        return f"{trigger['id']}：聊到“{trigger['when']}”时（{trigger['run']}）"
    if trigger["type"] == "time":
        cheap = "，挪到便宜时段" if trigger.get("window") == "cheap" else ""
        return f"{trigger['id']}：{trigger['days']} {trigger['at']}{cheap}（{trigger['run']}）"
    return f"{trigger['id']}：事件 {trigger['event']}（{trigger['run']}）"


def _do_habit(pet, command) -> tuple[bool, str]:
    from datetime import datetime

    from ..brain.habits.store import HabitError

    store = _habit_store(pet)
    if store is None:
        return True, "✗ 习惯系统没启动。"
    state = getattr(pet, "habit_state", None)
    word, _, rest = command.text.partition(" ")
    op, name = (word, rest.strip()) if word in cmd.HABIT_OPS else ("", command.text.strip())
    habit = store.find(name)
    if habit is None:
        return True, f"✗ 没有叫“{name}”的习惯。/habits 看名字。"
    if op:
        try:
            if op == "pause":
                store.set_enabled(habit.name, False)
            elif op == "resume":
                store.set_enabled(habit.name, True)
                if state is not None:
                    state.set_paused(habit.name, "")
            else:
                store.revert(habit.name)
        except HabitError as exc:
            return True, f"✗ {exc}"
        return True, f"✓ 「{habit.title}」{ {'pause': '暂停了', 'resume': '恢复了', 'revert': '撤回到上一版了'}[op] }。"
    lines = [f"{habit.title}（{habit.name}）{'' if habit.enabled else '　已暂停'}", habit.description]
    lines.append(f"文件：{habit.path}")
    lines += ["　" + _trigger_text(t) for t in habit.config["triggers"]]
    perms = habit.config["permissions"]
    lines.append(f"权限：{'可联网' if perms['web'] else '不联网'}，写 {perms['write']}")
    if state is not None:
        if state.paused(habit.name):
            lines.append(f"自动暂停：{state.paused(habit.name)}（/habit resume {habit.name} 恢复）")
        runs = [(t["id"], state.last_run(habit.name, t["id"])) for t in habit.config["triggers"]]
        seen = [f"{tid} {datetime.fromtimestamp(ts):%m-%d %H:%M}" for tid, ts in runs if ts]
        lines.append("上次触发：" + ("；".join(seen) if seen else "还没有"))
        lines.append(f"今天花了 {state.day_tokens(habit.name)} / {habit.config['budget']['day_tokens']} token")
    for row in store.read_log(habit.name, 3):
        lines.append(f"　{row.get('ts', '')[5:16].replace('T', ' ')} {row.get('trigger', '')} {row.get('result', '')} {str(row.get('summary', ''))[:30]}")
    return True, "\n".join(lines)


def _do_pearl(pet, command) -> tuple[bool, str]:
    path = command.text.strip().strip('"')
    if path:
        return (False, "") if pet.give_pearl(path=os.path.expandvars(path)) else (True, "✗ 读不了这个文件。")
    mime = QApplication.clipboard().mimeData()
    paths, text = pet.pearl_payload(mime)
    if paths:
        pet.drop_files(paths[:3])
        return False, ""
    if text.strip():
        pet.give_pearl(text=text)
        return False, ""
    return True, "✗ 剪贴板里没有文字或文件。"


def _numbered_pearls(pet) -> list[dict]:
    """1 = 最近递的那颗（和右键「珍珠」菜单的编号一致）。"""
    return list(reversed(pet.pearls.items))


def _do_pearls(pet, command) -> tuple[bool, str]:
    from ..brain.pearls import kind_label

    pearls = _numbered_pearls(pet)
    if not pearls:
        return True, "还没有珍珠。"
    lines = [f"{n}. {p['title'][:30]}（{kind_label(p['kind'])}）{(p['summary'] or '还没读')[:30]}"
             for n, p in enumerate(pearls[:20], start=1)]
    if len(pearls) > 20:
        lines.append(f"……一共 {len(pearls)} 颗")
    return True, "\n".join(lines + ["/drop 编号 扔掉"])


def _do_drop(pet, command) -> tuple[bool, str]:
    import re

    pearls = _numbered_pearls(pet)
    if not pearls:
        return True, "还没有珍珠。"
    if command.text.lower() in ("all", "全部"):
        count = pet.remove_all_pearls()
        return True, f"✓ 扔掉了 {count} 颗。" if count else "没扔。"
    numbers = [int(n) for n in re.findall(r"\d+", command.text)] or [1]
    bad = [n for n in numbers if not 1 <= n <= len(pearls)]
    if bad:
        return True, f"✗ 没有第 {bad[0]} 颗（一共 {len(pearls)} 颗，/pearls 看编号）"
    chosen = [pearls[n - 1] for n in dict.fromkeys(numbers)]
    for pearl in chosen:
        pet.remove_pearl(pearl["id"])
    return True, "✓ 扔掉了：" + "、".join(f"「{p['title'][:20]}」" for p in chosen)


def _do_give(pet, command) -> tuple[bool, str]:
    import re

    pearls = _numbered_pearls(pet)
    if not pearls:
        return True, "还没有珍珠可以转。"
    match = re.match(r"(\d+)\s*(.*)$", command.text, re.S)
    number, note = (int(match.group(1)), match.group(2)) if match else (1, command.text)
    if not 1 <= number <= len(pearls):
        return True, f"✗ 没有第 {number} 颗（一共 {len(pearls)} 颗，/pearls 看编号）"
    pearl = pearls[number - 1]
    receiver = NAMES.get(pet.other_character, "另一位")
    if pet.hand_over_pearl(pearl["id"], note) is None:
        return True, "✗ 没转成。"
    return True, f"✓「{pearl['title'][:20]}」转给{receiver}了，下次切到{receiver}时会读。"


def _do_places(pet, command) -> tuple[bool, str]:
    memory = pet.memory
    lines = [f"{name} → {e['path']}　（{e.get('confirmed', '')}）" for name, e in memory.places.items()]
    lines += [f"{name} = {e['meaning']}　（{e.get('confirmed', '')}）" for name, e in memory.aliases.items()]
    return True, "\n".join(lines) if lines else "还没有记下地点或别名：聊到的时候它会先问你，你确认了才记。"


def _do_forget(pet, command) -> tuple[bool, str]:
    if not pet.memory.forget_place(command.text):
        return True, f"✗ 没有叫「{command.text}」的地点或别名。/places 看全部。"
    pet.memory.save()
    return True, f"✓ 忘掉「{command.text}」了。"


def _do_char(pet, command) -> tuple[bool, str]:
    target = cmd.character_of(command.text) if command.text else (
        "past_moon" if pet.character == "pebbles" else "pebbles")
    if target == pet.character:
        return True, f"现在就是{NAMES.get(target)}。"
    pet.set_character(target)
    return False, ""


def _do_park(pet, command) -> tuple[bool, str]:
    pet.set_territory(cmd.SIDE_WORDS[command.text.strip().lower()])
    return False, ""


def _do_reflect(pet, command) -> tuple[bool, str]:
    if pet.director is None:
        return True, "✗ AI 没开，没法反思。"
    pet.director.reflect_now()
    return True, "✓ 马上反思一次（右键 → AI 状态 里能看到结果）。"


def _do_chat(pet, command) -> tuple[bool, str]:
    pet.open_chat()
    return False, ""


def _do_help(pet, command) -> tuple[bool, str]:
    return True, "\n".join(cmd.help_lines() + ["直接打字 = 跟它聊天　Tab 补全　↑↓ 历史　Ctrl+Enter 完整窗口　Esc 收起"])


class QuickBar(QWidget):
    WIDTH = 560

    def __init__(self, pet):
        super().__init__(None)
        self.pet = pet
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SHADOW + 12, SHADOW + 12, SHADOW + 12, SHADOW + 12)
        layout.setSpacing(GAP_S)
        self.input = QLineEdit()
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._update_hints)
        self.input.returnPressed.connect(self._run)
        self.menu = QListWidget()
        self.menu.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.menu.itemClicked.connect(lambda item: self._accept_completion(self.menu.row(item)))
        self.menu.hide()
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hint.hide()
        layout.addWidget(self.input)
        layout.addWidget(self.menu)
        layout.addWidget(self.hint)
        self.setFixedWidth(self.WIDTH + 2 * SHADOW)
        self._matches: list[cmd.Spec] = []
        self._history = self._load_history()
        self._cursor = len(self._history)
        self.holo = False                         # 监视者投影皮肤（第三期）：透明底、全息色字，位置贴着监视者
        self._holo_color = (0.447, 0.902, 0.769)

    # -- 皮肤 -------------------------------------------------------------------
    def apply_skin(self) -> None:
        """全息皮肤（背景透明、监视者主色的字）或原来的窗口样式。

        用户 2026-11-01：输入条一律用原来的弹窗样式，不再做成监视者投影（``USE_HOLO_SKIN`` 留着以后想恢复）。"""
        from ..rendering.overseer import MAIN_COLORS

        holo = USE_HOLO_SKIN and bool(getattr(self.pet, "overseer_enabled", False))
        self._holo_color = MAIN_COLORS.get(self.pet.character, MAIN_COLORS["pebbles"])
        self.holo = holo
        margin = HOLO_MARGIN if holo else SHADOW + 12
        self.layout().setContentsMargins(margin, margin, margin, margin)
        self.setFixedWidth(self.WIDTH + 2 * (HOLO_MARGIN if holo else SHADOW))
        self.setStyleSheet(self._holo_style() if holo else "")
        if not holo:
            self.setWindowOpacity(1.0)
        self.update()

    def _holo_style(self) -> str:
        r, g, b = (int(min(1.0, c * 1.15) * 255) for c in self._holo_color)
        solid, dim = f"rgb({r},{g},{b})", lambda a: f"rgba({r},{g},{b},{a})"
        return (
            f'QWidget {{ font-family: "Segoe UI", "Microsoft YaHei UI", "Noto Sans SC"; }}'
            f"QLineEdit {{ background: transparent; border: none; border-bottom: 1px solid {dim(0.55)}; color: {solid};"
            f" font-size: 15px; padding: 5px 2px; selection-background-color: {dim(0.35)}; }}"
            f"QListWidget {{ background: transparent; border: none; color: {dim(0.85)}; font-size: 13px; outline: 0; }}"
            f"QListWidget::item {{ padding: 4px 6px; }}"
            f"QListWidget::item:selected {{ background: {dim(0.18)}; color: {solid}; }}"
            f"QLabel {{ background: transparent; color: {dim(0.7)}; font-size: 13px; }}")

    def dock(self, top_left: QPoint, extended: float) -> None:
        """贴到监视者给的位置；监视者还在钻出来时先半透明，免得它还没到字就先亮着。"""
        if self.pos() != top_left:
            self.move(top_left)
        self.setWindowOpacity(0.15 + 0.85 * max(0.0, min(1.0, extended)))

    # -- 显示 -------------------------------------------------------------------
    def popup(self) -> None:
        from .ui_theme import accent_for

        self.apply_skin()
        name = NAMES.get(self.pet.character, "迭代器")
        self.input.setPlaceholderText(f"对{name}说点什么，或者 / 看命令")
        if not self.holo:
            self.input.setStyleSheet(f"QLineEdit {{ border: 1px solid {accent_for(self.pet.character)}; }}")
        else:
            self.input.setStyleSheet("")
            self.setWindowOpacity(0.15)
        self._show_hint("")
        self._update_hints(self.input.text())
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        self.input.selectAll()

    def _place(self) -> None:
        """人偶头顶上方（放不下就放下面），横向夹在屏幕里。"""
        state = getattr(self.pet, "_drawn_state", None)
        screen = self.pet.screen().availableGeometry()
        if state is None:
            x, y = screen.center().x(), screen.bottom() - 200
        else:
            x = self.pet.x() + state.head[0] * self.pet.scale
            y = self.pet.y() + state.head[1] * self.pet.scale
        top = int(y - 90 * self.pet.scale / 2 - self.height())
        if top < screen.top():
            top = int(y + 60)
        left = int(min(max(x - self.width() / 2, screen.left()), screen.right() - self.width()))
        self.move(QPoint(left, min(top, screen.bottom() - self.height())))

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self.holo:
            _paint_frame(self)
            return
        from ..rendering.hologram import draw_backdrop

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        inset = HOLO_MARGIN / 2
        draw_backdrop(p, self.width() - 2 * inset, self.height() - 2 * inset, self._holo_color, inset, inset)
        p.end()

    def _show_hint(self, text: str) -> None:
        self.hint.setText(text)
        if self.holo:                             # 全息皮肤：错误用红，其余沿用皮肤里的色（不单独设 color）
            self.hint.setStyleSheet("color: rgb(255,89,64);" if text.startswith("✗") else "")
            self.hint.setVisible(bool(text))
            QTimer.singleShot(0, self._refit)
            return
        color = RED_HOVER if text.startswith("✗") else MEDIUM_GREY
        self.hint.setStyleSheet(f"color: {color};")
        self.hint.setVisible(bool(text))
        QTimer.singleShot(0, self._refit)

    def _refit(self) -> None:
        bottom = self.geometry().bottom()
        self.layout().activate()
        self.resize(self.width(), self.layout().totalHeightForWidth(self.width()))
        if self.isVisible() and self.y() + self.height() > self.screen().availableGeometry().bottom():
            self.move(self.x(), bottom - self.height())

    def _update_hints(self, text: str) -> None:
        self._matches = cmd.complete(text)
        self.menu.clear()
        for spec in self._matches:
            self.menu.addItem(f"/{spec.name}　{spec.help}")
        if self._matches:
            self.menu.setCurrentRow(0)
            rows = min(len(self._matches), 10)
            self.menu.setFixedHeight(rows * self.menu.sizeHintForRow(0) + 2 * self.menu.frameWidth() + 8)
        self.menu.setVisible(bool(self._matches))
        spec = cmd.spec_for(text)
        if spec is not None:                      # 敲完命令名：下面给出用法
            flags = "　".join(f"{f} {d}" for f, d in spec.flags)
            self._show_hint(spec.usage + (f"\n{flags}" if flags else ""))
            if not self.holo:
                self.hint.setStyleSheet(f"color: {FAINT};")
        elif text:                                # 接着打字：上一条结果收掉
            self._show_hint("")
        QTimer.singleShot(0, self._refit)

    def _accept_completion(self, row: int) -> None:
        if 0 <= row < len(self._matches):
            self.input.setText(f"/{self._matches[row].name} ")
            self.input.setFocus()

    # -- 键盘 -------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key, mods = event.key(), event.modifiers()
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods & Qt.KeyboardModifier.ControlModifier:
                text = self.input.text().strip()
                self.hide()
                self.pet.open_chat()
                if text and self.pet._chat_window is not None:
                    self.pet._chat_window.input.setText(text)
                return True
            if key == Qt.Key.Key_Tab:
                self._accept_completion(self.menu.currentRow())
                return True
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                step = -1 if key == Qt.Key.Key_Up else 1
                if self._matches:
                    row = (self.menu.currentRow() + step) % len(self._matches)
                    self.menu.setCurrentRow(row)
                else:
                    self._browse_history(step)
                return True
        return super().eventFilter(obj, event)

    def _browse_history(self, step: int) -> None:
        if not self._history:
            return
        self._cursor = max(0, min(len(self._history), self._cursor + step))
        self.input.setText(self._history[self._cursor] if self._cursor < len(self._history) else "")

    def event(self, event) -> bool:  # noqa: N802
        # 点到别处就收起（像开始菜单 / 输入法候选框）
        if event.type() == QEvent.Type.WindowDeactivate:
            QTimer.singleShot(0, self.hide)
        return super().event(event)

    # -- 执行 -------------------------------------------------------------------
    def _run(self) -> None:
        line = self.input.text().strip()
        if not line:
            return
        if self._matches and " " not in line and cmd.complete(line) and line[1:] not in {
                n for s in self._matches for n in (s.name, *s.aliases)}:
            self._accept_completion(self.menu.currentRow())   # 敲了半个命令名就回车：先补全
            return
        self._remember(line)
        keep, message = execute(self.pet, line)
        if keep:
            if not message.startswith("✗"):
                self.input.clear()
            self._show_hint(message)
        else:
            self.input.clear()
            self.hide()

    def _remember(self, line: str) -> None:
        if not self._history or self._history[-1] != line:
            self._history.append(line)
        del self._history[:-HISTORY_KEEP]
        self._cursor = len(self._history)
        try:
            path = self._history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._history, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _history_path():
        from ..config import config_dir

        return config_dir() / "quick_history.json"

    def _load_history(self) -> list[str]:
        try:
            data = json.loads(self._history_path().read_text(encoding="utf-8"))
            return [str(x) for x in data][-HISTORY_KEEP:] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []
