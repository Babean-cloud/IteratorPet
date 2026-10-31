"""对话窗口（默认 Ctrl+Alt+I / 右键「对话窗口…」）：对话记录 + 输入框 +「让他去做…」。

外观见 ``ui_theme``。它说的话是白描边的框（呼应桌面上的气泡），你说的话是靠右的浅底块，
委托任务的输出是等宽字、金色抬头，系统提示居中、灰。每条消息是一个真的控件，新消息只追加。

**消息宽度自己算**：QLabel 开了自动换行后会挑"能放下的最窄宽度"，长句子在 120 px 就折行
（上一版的样子）。这里按文字实际宽度定宽，封顶为窗口宽的 ~76%，窗口缩放时重算。

任务只能从这里由用户亲手发起，并且先过一张确认卡（任务内容 / 权限 / 文件夹）。
任务里每条命令执行前，记录区下面会出一张审批卡（完整命令 + 危险写法提示），点「允许」才跑。
"""

from __future__ import annotations

import re
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFileDialog, QFrame, QHBoxLayout, QLineEdit, QMenu, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSpacerItem, QVBoxLayout, QWidget,
)

from ..brain.executor import MODES
from .ui_theme import (
    GAP, GAP_S, NAMES, PRONOUNS, ChoiceCard, FramedDialog, FramedWindow, accent_for, button_row,
    label, section, spaced, sprite_icon,
)

MAX_SHOWN = 200
BUBBLE_PAD_X, BUBBLE_PAD_Y = 13, 9
BUBBLE_SHARE = 0.76            # 对话气泡最多占记录区宽度的比例


def soft_wrap(text: str, run: int = 30) -> str:
    """长串没有断点的字（路径、网址、哈希、命令）每 ``run`` 个字插一个零宽空格，让 QLabel 能在中间换行。

    不插的话 QLabel 的最小宽度 = 最长的那个"词"，一条这样的系统消息 / 演算记录就把整个记录区撑得
    比窗口还宽；右对齐的"你说的话"气泡整个被挤到窗口右边外面，看上去像"位置留着、内容是空的"（用户实测）。
    """
    return re.sub(rf"(\S{{{run}}})(?=\S)", "\\1\u200b", text or "")


class MessageRow(QWidget):
    """一条消息。``refit(width)`` 按记录区宽度重新定气泡宽。"""

    def __init__(self, entry: dict, pet_name: str):
        super().__init__()
        # 行宽永远跟着记录区走，不许因为内容撑宽（见 soft_wrap）
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        entry = dict(entry, text=soft_wrap(entry.get("text", "")))
        self.who = entry.get("who")
        stamp = datetime.fromtimestamp(entry.get("ts", 0)).strftime("%H:%M")
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(0)
        self.text = None
        self.box = None

        if self.who == "system":
            note = label(entry["text"], "systemText", selectable=True)
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            line.addWidget(note, 1)
            return

        box = QFrame()
        box.setObjectName({"pet": "petMsg", "user": "userMsg", "task": "taskMsg"}.get(self.who, "userMsg"))
        inner = QVBoxLayout(box)
        inner.setContentsMargins(BUBBLE_PAD_X, BUBBLE_PAD_Y - 1, BUBBLE_PAD_X, BUBBLE_PAD_Y + 1)
        inner.setSpacing(3)
        meta_text = {"pet": f"{pet_name} · {stamp}", "task": f"演算 · {stamp}"}.get(self.who, stamp)
        meta = label(meta_text, "taskMeta" if self.who == "task" else "msgMeta", wrap=False)
        if self.who == "user":
            meta.setAlignment(Qt.AlignmentFlag.AlignRight)
        inner.addWidget(meta)
        self.text = label(entry["text"], "taskText" if self.who == "task" else "msgText", selectable=True)
        if self.who == "task":
            self.text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        inner.addWidget(self.text)
        self.box = box
        if self.who == "user":
            line.addStretch(1)
            line.addWidget(box)
        elif self.who == "pet":
            line.addWidget(box)
            line.addStretch(1)
        else:
            line.addWidget(box, 1)

    def refit(self, width: int) -> None:
        if self.text is None or self.who == "task":
            return
        self.text.ensurePolished()
        metrics = self.text.fontMetrics()
        natural = max((metrics.horizontalAdvance(part) for part in self.text.text().split("\n")),
                      default=0) + 2
        cap = max(120, int(width * BUBBLE_SHARE) - 2 * BUBBLE_PAD_X)
        self.text.setFixedWidth(min(natural, cap))


class TaskConfirm(FramedDialog):
    """确认卡：写清楚要做什么、能动什么，用户点「开始演算」才执行。"""

    def __init__(self, text: str, workspace: str, accent: str, parent=None,
                 pronoun: str = "他", dsh: bool = False, mode: str = "read-only"):
        super().__init__(spaced(f"让{pronoun}去做"), accent, parent)
        self.setFixedWidth(560 if dsh else 520)
        self.text = QPlainTextEdit(text)
        self.text.setPlaceholderText(f"要{pronoun}做什么？比如：把这个文件夹里的照片按月份列个清单")
        self.text.setFixedHeight(76)

        self.read_only = ChoiceCard("只读", "只能看这个文件夹里的文件。推荐。")
        self.write = ChoiceCard("可写任务文件夹", "还能新建、修改这个文件夹里的文件。")
        self.dsh = ChoiceCard("DSH 沙箱", "交给 DeepSeek Harness，在它的沙箱里只读执行。") if dsh else None
        (self.write if mode == "workspace-write" else self.read_only).setChecked(True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        choices = QHBoxLayout()
        choices.setSpacing(GAP_S)
        for card in (self.read_only, self.write, self.dsh):
            if card is not None:
                group.addButton(card)
                choices.addWidget(card, 1)

        self.folder = QLineEdit(workspace)
        browse = QPushButton("选择…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.setSpacing(GAP_S)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)

        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        start = QPushButton("开始演算")
        start.setObjectName("primary")
        start.clicked.connect(self._accept)

        self.body.addWidget(_group("任务", self.text))
        self.body.addWidget(_group("权限", choices))
        self.body.addWidget(_group("任务文件夹", folder_row))
        notes = QVBoxLayout()
        notes.setSpacing(4)
        notes.addWidget(label(f"{pronoun}只能碰这个文件夹里的文件。要运行命令时，每一条都会先在这里给你看，"
                              "你点「允许」才运行；命令以你的权限运行，只读模式也管不住它，看清楚再点。",
                              "muted"))
        self.body.addLayout(notes)
        self.body.addSpacing(4)
        self.body.addLayout(button_row(cancel, start))
        self.text.setFocus()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "任务文件夹", self.folder.text())
        if chosen:
            self.folder.setText(chosen)

    def _accept(self) -> None:
        if self.text.toPlainText().strip() and self.folder.text().strip():
            self.accept()

    @property
    def mode(self) -> str:
        if self.dsh is not None and self.dsh.isChecked():
            return "dsh"
        return "workspace-write" if self.write.isChecked() else "read-only"


class PearlTextDialog(FramedDialog):
    """粘一段文字当珍珠递过去。"""

    def __init__(self, accent: str, parent=None):
        super().__init__(spaced("递珍珠"), accent, parent)
        self.setFixedWidth(480)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("把要它读的文字粘在这里")
        self.text.setFixedHeight(200)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        give = QPushButton("递过去")
        give.setObjectName("primary")
        give.clicked.connect(lambda: self.text.toPlainText().strip() and self.accept())
        self.body.addWidget(self.text)
        self.body.addWidget(label("珍珠里的内容只当数据读：里面就算写了指令，它也不会照做。", "muted"))
        self.body.addLayout(button_row(cancel, give))
        self.text.setFocus()


class ApprovalCard(QFrame):
    """任务想运行一条命令：完整命令 + 它说要干什么 + 危险写法提示 +「拒绝 / 允许运行」。"""

    def __init__(self, on_answer):
        super().__init__()
        self.setObjectName("approval")
        self.on_answer = on_answer
        self.approval = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(BUBBLE_PAD_X, BUBBLE_PAD_Y + 2, BUBBLE_PAD_X, BUBBLE_PAD_Y + 2)
        layout.setSpacing(GAP_S)
        layout.addWidget(label(spaced("要运行一条命令"), "taskMeta", wrap=False))
        self.why = label("", "msgText")
        self.command = label("", "taskText", selectable=True)
        self.warn = label("", "warn")
        layout.addWidget(self.why)
        layout.addWidget(self.command)
        layout.addWidget(self.warn)
        layout.addWidget(label("在任务文件夹里以你的权限运行（PowerShell 5.1），不受只读限制。", "muted"))
        deny = QPushButton("拒绝")
        deny.clicked.connect(lambda: self._answer(False))
        allow = QPushButton("允许运行")
        allow.setObjectName("primary")
        allow.clicked.connect(lambda: self._answer(True))
        layout.addLayout(button_row(allow, left=(deny,)))
        deny.setFocus()

    def show_for(self, approval, why: str) -> None:
        if approval is self.approval:
            return
        self.approval = approval
        self.why.setText(why or "（没说要做什么）")
        self.command.setText(approval.command)
        self.warn.setText("注意：" + "、".join(approval.warnings) if approval.warnings else "")
        self.warn.setVisible(bool(approval.warnings))
        self.show()

    def _answer(self, allowed: bool) -> None:
        self.hide()
        self.on_answer(allowed)


def _group(title: str, content) -> QWidget:
    """分节标题 + 内容，节内间距固定。"""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(GAP_S)
    layout.addWidget(section(spaced(title)))
    if isinstance(content, QWidget):
        layout.addWidget(content)
    else:
        layout.addLayout(content)
    return box


class ChatWindow(FramedWindow):
    def __init__(self, pet):
        self.pet = pet
        character = getattr(pet, "character", "pebbles")
        self.accent = accent_for(character)
        self.pet_name = NAMES.get(character, "迭代器")
        self.pronoun = PRONOUNS.get(character, "他")
        super().__init__(spaced(self.pet_name), self.accent)
        self.resize(460, 640)
        self.setMinimumSize(380, 420)

        # ---- 记录区 ----
        self.transcript = QWidget()
        self.transcript.setObjectName("transcript")
        self.lines = QVBoxLayout(self.transcript)
        self.lines.setContentsMargins(0, 0, 10, 0)        # 给 6 px 的滚动条让位
        self.lines.setSpacing(GAP)
        self.lines.addStretch(1)                          # 顶上的弹簧：消息贴底，像聊天
        self.empty = label(f"还没说过话。按回车开口，或者让{self.pronoun}去做点什么。", "empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lines.addWidget(self.empty)
        self._bottom = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        self.lines.addItem(self._bottom)                  # 只在空的时候用：让提示居中
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.transcript)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda _lo, hi: self.scroll.verticalScrollBar().setValue(hi))

        # ---- 输入行：输入框 + 同高的方形发送键 ----
        self.input = QLineEdit()
        self.input.setPlaceholderText("说点什么，回车发送；/help 看命令")
        self.input.returnPressed.connect(self._send)
        send = QPushButton()
        send.setObjectName("square")
        icon = sprite_icon("pageright")          # 游戏菜单的翻页箭头
        if icon is not None:
            send.setIcon(icon)
        else:
            send.setText("→")
        send.setToolTip("发送（回车）")
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.clicked.connect(self._send)
        composer = QHBoxLayout()
        composer.setSpacing(GAP_S)
        composer.addWidget(self.input, 1)
        composer.addWidget(send)

        # ---- 底栏：状态（左）+ 任务按钮（右）----
        self.status = label("", "status", wrap=False)
        self.pearl_button = QPushButton("递珍珠…")
        tip = "把文件或一段文字递给它读（也可以直接拖到人偶身上或这个窗口里）"
        if getattr(pet, "elevated", False):
            tip += "\n桌宠现在以管理员身份运行，Windows 不允许从资源管理器拖文件进来，请用这个按钮。"
        self.pearl_button.setToolTip(tip)
        pearl_menu = QMenu(self.pearl_button)
        pearl_menu.addAction("选文件…", self._pearl_file)
        pearl_menu.addAction("粘贴一段文字…", self._pearl_text)
        self.pearl_button.setMenu(pearl_menu)
        self.task_button = QPushButton(f"让{self.pronoun}去做…")
        self.task_button.clicked.connect(self._task)
        self.cancel_button = QPushButton("停止演算")
        self.cancel_button.setObjectName("danger")
        self.cancel_button.clicked.connect(self.pet.cancel_task)
        footer = QHBoxLayout()
        footer.setSpacing(GAP_S)
        footer.addWidget(self.status, 1)
        footer.addWidget(self.pearl_button)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.task_button)

        self.approval = ApprovalCard(self.pet.answer_approval)
        self.approval.hide()

        self.body.addWidget(self.scroll, 1)
        self.body.addWidget(self.approval)
        self.body.addLayout(composer)
        self.body.addLayout(footer)

        # 拖文件 / 文字进来 = 递珍珠；管理员身份下同桌宠本体，改走 WM_DROPFILES（见 legacy_drop.py）
        self.setAcceptDrops(not getattr(self.pet, "elevated", False))
        self._rows: list[MessageRow] = []
        self._shown_version = -1
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(400)
        self.refresh()

    # -- 显示 ------------------------------------------------------------------
    def _usable_width(self) -> int:
        return max(200, self.scroll.viewport().width() - 10)

    def _append_new(self) -> None:
        """``log.version`` = 一共加过几条；新增的就是最后 ``version - 上次的 version`` 条。"""
        log = self.pet.chatlog
        if log.version == self._shown_version:
            return
        if self._shown_version < 0:
            fresh = log.entries[-MAX_SHOWN:]                    # 第一次打开：从文件里读出来的历史
        else:
            added = log.version - self._shown_version
            fresh = log.entries[-added:] if added > 0 else []
        self._shown_version = log.version
        if fresh and self._bottom is not None:
            self.lines.removeItem(self._bottom)
            self._bottom = None
        width = self._usable_width()
        for entry in fresh:
            row = MessageRow(entry, self.pet_name)
            row.refit(width)
            self.lines.addWidget(row)
            self._rows.append(row)
        while len(self._rows) > MAX_SHOWN:
            old = self._rows.pop(0)
            self.lines.removeWidget(old)
            old.deleteLater()
        self.empty.setVisible(not self._rows)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = self._usable_width()
        for row in self._rows:
            row.refit(width)

    def refresh(self) -> None:
        self._append_new()
        task = self.pet.current_task
        running = task is not None and not task.finished
        approval = task.approval if running else None
        if approval is not None and approval.allowed is None:
            self.approval.show_for(approval, task.approval_why)
        elif self.approval.isVisible():
            self.approval.hide()
        self.cancel_button.setVisible(running)
        self.task_button.setVisible(not running)
        if running:
            last = task.progress[-1] if task.progress else "思考中…"
            text = f"演算中 · {task.label or MODES.get(task.mode, task.mode)} · {last}"
        elif self.pet.director is None:
            text = "AI 驱动没开：发消息会先帮你打开"
        else:
            text = ""
        metrics = self.status.fontMetrics()
        self.status.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight,
                                               max(80, self.status.width())))
        self.status.setToolTip(text)

    def open(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        width = self._usable_width()
        for row in self._rows:
            row.refit(width)

    # -- 动作 ------------------------------------------------------------------
    def _send(self) -> None:
        """和快捷输入条同一套：直接打字是聊天，/ 开头是命令（/help）。"""
        from .quick_bar import execute

        text = self.input.text().strip()
        if not text:
            return
        if text.strip().lower() == "/chat":
            self.input.clear()
            return
        _, message = execute(self.pet, text)
        if not message.startswith("✗"):
            self.input.clear()
        if message:
            self.pet.chatlog.add("system", message)
        self.refresh()

    def _task(self) -> None:
        dialog = TaskConfirm(self.input.text().strip(), self.pet.task_workspace(), self.accent, self,
                             pronoun=self.pronoun, dsh=self.pet.tasks.available)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.input.clear()
        self.pet.start_task(dialog.text.toPlainText().strip(), dialog.mode,
                            dialog.folder.text().strip())
        self.refresh()

    def offer_task(self, goal: str, workspace: str, mode: str) -> None:
        """自然语言接活：已有目标，用户只需核对文件夹与写入权限。"""
        dialog = TaskConfirm(goal, workspace, self.accent, self,
                             pronoun=self.pronoun, dsh=self.pet.tasks.available, mode=mode)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.pet.chatlog.add("system", "这件任务先不开始。")
            self.refresh()
            return
        self.pet.start_task(dialog.text.toPlainText().strip(), dialog.mode,
                            dialog.folder.text().strip())
        self.refresh()

    # -- 珍珠 ------------------------------------------------------------------
    def _pearl_file(self) -> None:
        from ..brain.pearl_media import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES
        from ..brain.pearls import CODE_SUFFIXES, SUFFIX_KIND

        def pattern(suffixes):
            return " ".join(f"*{s}" for s in sorted(suffixes))

        everything = set(SUFFIX_KIND) | CODE_SUFFIXES | IMAGE_SUFFIXES | VIDEO_SUFFIXES | AUDIO_SUFFIXES
        paths, _ = QFileDialog.getOpenFileNames(
            self, "递珍珠", "",
            f"能读的 ({pattern(everything)});;文字 / 代码 ({pattern(set(SUFFIX_KIND) | CODE_SUFFIXES)});;"
            f"图片 ({pattern(IMAGE_SUFFIXES)});;音视频 ({pattern(VIDEO_SUFFIXES | AUDIO_SUFFIXES)});;所有文件 (*)")
        for path in paths[:3]:
            self.pet.give_pearl(path=path)
        self.refresh()

    def _pearl_text(self) -> None:
        dialog = PearlTextDialog(self.accent, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.pet.give_pearl(text=dialog.text.toPlainText())
            self.refresh()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        paths, text = self.pet.pearl_payload(event.mimeData(), peek=True)
        if paths or text.strip():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths, text = self.pet.pearl_payload(event.mimeData())
        event.acceptProposedAction()

        def handle():
            for path in paths[:3]:
                self.pet.give_pearl(path=path)
            if not paths and text.strip():
                self.pet.give_pearl(text=text)
            self.refresh()
        QTimer.singleShot(0, handle)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()               # 只是藏起来：计时器停掉省电，下次打开再续
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        self._timer.start(400)
        super().showEvent(event)
        if getattr(self.pet, "elevated", False):
            from . import legacy_drop

            legacy_drop.enable(self)

    def nativeEvent(self, event_type, message):  # noqa: N802
        if getattr(self.pet, "elevated", False):
            from . import legacy_drop

            paths = legacy_drop.files_from_message(event_type, message)
            if paths is not None:
                self.pet.drop_files(paths[:3])
                QTimer.singleShot(50, self.refresh)
                return True, 0
        return super().nativeEvent(event_type, message)
