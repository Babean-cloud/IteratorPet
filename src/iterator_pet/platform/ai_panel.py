"""「AI 状态…」面板：上面四张数字卡（余额 / 今日用量 / 缓存命中 / 估算花费，两个角色共用），
下面"此刻"（心情、日程、它在想、它看到的）和当前角色的"记忆"（好感度、小目标、反思、日记、
最近经历摘要、记住的事）。「现在反思」手动触发一次反思。

只在右键打开时才看；余额只在打开 / 点「刷新余额」时查一次（后台线程，不卡 UI）。
键值行放在真的网格里（键列定宽），所有值左边对齐在同一条竖线上。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from .chat_window import soft_wrap

from ..brain.director import REFLECT_FULL
from ..brain.usage import UsageTracker
from .ui_theme import (
    GAP, GAP_S, NAMES, ConfirmDialog, FramedWindow, accent_for, button_row, label, section, spaced,
)

KEY_WIDTH = 64


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _affinity_word(value: float) -> str:
    """好感度条从中间往两边读不直观，配一个词。"""
    for limit, word in ((-0.5, "厌烦"), (-0.15, "提防"), (0.15, "生疏"), (0.5, "熟悉"), (2.0, "亲近")):
        if value < limit:
            return word
    return "亲近"


class _Card(QFrame):
    """一张数字卡：小标题 / 大数字 / 一行注脚（可选细进度条），四张等高。"""

    def __init__(self, caption: str, bar: bool = False):
        super().__init__()
        self.setObjectName("card")
        self.setMinimumHeight(104)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)
        layout.addWidget(label(spaced(caption), "cardCaption", wrap=False))
        layout.addSpacing(2)
        self.value = label("—", "bigNumber", wrap=False)
        layout.addWidget(self.value)
        layout.addStretch(1)
        # 注脚会变长（今日用量按对话 / 任务 / 心事分开之后），不许换行的话整张卡把面板撑得比窗口还宽，
        # 右边「放下」之类的按钮被挤到窗口外面点不到（用户实测）。允许折行、宽度跟着网格走。
        self.note = label("", "cardNote", wrap=True)
        self.note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.note)
        self.bar = None
        if bar:
            self.bar = QProgressBar()
            self.bar.setRange(0, 1000)
            self.bar.setFixedHeight(4)
            layout.addSpacing(6)
            layout.addWidget(self.bar)


class _Rows(QWidget):
    """一组键值行：键列定宽、灰；值可换行；空值整行隐藏。"""

    def __init__(self):
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(GAP)
        self.grid.setVerticalSpacing(GAP_S)
        self.grid.setColumnMinimumWidth(0, KEY_WIDTH)
        self.grid.setColumnStretch(1, 1)
        self._rows: dict[str, tuple] = {}

    def add(self, name: str, key: str, widget: QWidget | None = None):
        index = len(self._rows)
        k = label(key, "key", wrap=False)
        k.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        value = widget or label("", "value", selectable=True)
        value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.grid.addWidget(k, index, 0, Qt.AlignmentFlag.AlignTop)
        self.grid.addWidget(value, index, 1)
        self._rows[name] = (k, value)
        return value

    def show_text(self, name: str, text: str | None) -> None:
        key, value = self._rows[name]
        visible = bool(text)
        if visible:
            value.setText(soft_wrap(text))
        key.setVisible(visible)
        value.setVisible(visible)


class AIPanel(FramedWindow):
    def __init__(self, window):
        self.window = window
        character = getattr(window, "character", "pebbles")
        self.accent = accent_for(character)
        self.name = NAMES.get(character, "迭代器")
        super().__init__(spaced(f"{self.name} · 状态"), self.accent)
        self.setFixedWidth(560)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.balance = _Card("账户余额")
        self.today = _Card("今日用量", bar=True)
        self.cache = _Card("缓存命中")
        self.cost = _Card("估算花费")
        grid = QGridLayout()
        grid.setSpacing(GAP_S)
        for i, card in enumerate((self.balance, self.today, self.cache, self.cost)):
            grid.addWidget(card, i // 2, i % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        # 卡片以下放进滚动区：记忆、日记越攒越长，整窗会比屏幕还高，底下的按钮和珍珠就够不着了
        self.content = QWidget()
        page = QVBoxLayout(self.content)
        page.setContentsMargins(0, 0, 10, 0)
        page.setSpacing(self.body.spacing())
        page.addLayout(grid)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.content)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.body.addWidget(self.scroll, 1)

        # 分节默认只展开"此刻"，其余折叠，点标题行展开；条数 / 摘要写在标题右边
        self.now = _Rows()                         # 此刻：一眼要看的
        for name, key in (("state", "状态"), ("mood", "心情"), ("thought", "它在想"),
                          ("sees", "它看到"), ("error", "错误")):
            self.now.add(name, key)
        page.addWidget(_group("此刻", self.now, open_=True))

        self.rel = _Rows()                         # 关系与日程：平时不用看
        for name, key in (("relation", "关系"), ("energy", "精力"), ("talk", "搭话"),
                          ("plan", "日程"), ("reminders", "提醒")):
            self.rel.add(name, key)
        self.rel_group = _group("关系与日程", self.rel)
        page.addWidget(self.rel_group)

        self.eyes = _Rows()                        # 屏幕识别：识别出的字、本轮计数（perception.stats）
        self.eyes.add("ocr", "识别")
        self.eyes.add("stats", "本轮计数")
        page.addWidget(_group("屏幕识别", self.eyes))

        self.mind = _Rows()                        # 念头回看：最近 10 条，新的在前（brain/mindlog.py）
        self.mind.add("log", "最近的念头")
        self.mind_group = _group("念头回看", self.mind)
        page.addWidget(self.mind_group)

        self.errand_list = _ErrandList(window)
        self.errand_group = _group("心事", self.errand_list)
        page.addWidget(self.errand_group)

        self.pearl_list = _PearlList(window)       # 放在记忆前面：要删珍珠不用往下翻一大段
        self.pearl_group = _group("珍珠", self.pearl_list)
        page.addWidget(self.pearl_group)

        self.mem = _Rows()
        affinity = QWidget()
        affinity_row = QHBoxLayout(affinity)
        affinity_row.setContentsMargins(0, 0, 0, 0)
        affinity_row.setSpacing(GAP)
        self.affinity = QProgressBar()
        self.affinity.setRange(-100, 100)
        self.affinity.setFixedHeight(4)
        self.affinity_value = label("", "value", wrap=False)
        affinity_row.addWidget(self.affinity, 1, Qt.AlignmentFlag.AlignVCenter)
        affinity_row.addWidget(self.affinity_value)
        self.mem.add("affinity", "好感度", affinity)
        for name, key in (("goal", "小目标"), ("reflect", "反思"), ("diary", "日记"),
                          ("summary", "最近经历"), ("facts", "记住的事"), ("places", "地点簿")):
            self.mem.add(name, key)
        self.mem_group = _group("记忆", self.mem)
        page.addWidget(self.mem_group)
        page.addStretch(1)

        refresh = QPushButton("刷新余额")
        refresh.clicked.connect(self.fetch_balance)
        folder = QPushButton("数据文件夹")
        folder.clicked.connect(self._open_folder)
        self.reflect = QPushButton("现在反思")
        self.reflect.clicked.connect(self._reflect_now)
        clear = QPushButton("清空记忆")
        clear.setObjectName("danger")
        clear.clicked.connect(self._clear_memory)
        self.body.addSpacing(4)
        self.body.addLayout(button_row(clear, left=(refresh, folder, self.reflect)))

        self._balance = "…"
        self._balance_note = "查询中"
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self.refresh()
        self.fetch_balance()

    # -- 余额（后台线程，结果写字符串，定时器刷出来） -------------------------
    def fetch_balance(self) -> None:
        client = self.window.llm_client()
        if client is None:
            self._balance, self._balance_note = "—", "还没有 API Key"
            return
        self._balance, self._balance_note = "…", "查询中"

        def work():
            try:
                info = client.balance()
                if info["items"]:
                    currency, amount = info["items"][0]
                    symbol = "¥" if currency == "CNY" else ("$" if currency == "USD" else "")
                    self._balance = f"{symbol}{amount}"
                    self._balance_note = currency if info["available"] else "余额不足，接口不可用"
                else:
                    self._balance, self._balance_note = "?", "没有返回余额"
            except Exception as exc:                 # noqa: BLE001
                self._balance, self._balance_note = "—", f"查询失败：{str(exc)[:40]}"

        threading.Thread(target=work, daemon=True).start()

    # -- 刷新 ------------------------------------------------------------------
    def refresh(self) -> None:
        win = self.window
        cfg = win.config
        usage: UsageTracker = win.usage
        prices = (cfg.llm_price_hit, cfg.llm_price_miss, cfg.llm_price_out)
        today = usage.today_row()
        total = usage.total
        cap = int(cfg.llm_daily_token_cap)
        used = usage.today_tokens()

        self.balance.value.setText(self._balance)
        self.balance.note.setText(self._balance_note)
        self.today.value.setText(_fmt_tokens(used))
        sources = usage.today_by_source()
        tracked = sum(row["prompt"] + row["completion"] for row in sources.values())
        parts = [f"{today['calls']} 次请求"]
        for key, name in (("dialogue", "对话"), ("task", "任务"), ("errand", "心事")):
            row = sources.get(key)
            if row:
                parts.append(f"{name} {_fmt_tokens(row['prompt'] + row['completion'])}")
        if used > tracked:
            parts.append(f"旧记录 {_fmt_tokens(used - tracked)}")
        parts.append(f"上限 {_fmt_tokens(cap) if cap > 0 else '不限'}")
        self.today.note.setText(" · ".join(parts))
        if self.today.bar is not None:
            self.today.bar.setValue(int(1000 * min(1.0, used / cap)) if cap > 0 else 0)
        rate = today["cache_hit"] / today["prompt"] * 100.0 if today["prompt"] else 0.0
        self.cache.value.setText(f"{rate:.0f}%" if today["prompt"] else "—")
        self.cache.note.setText("命中部分按缓存价计费")
        self.cost.value.setText(f"¥{UsageTracker.cost(today, prices):.3f}")
        self.cost.note.setText(f"累计 ¥{UsageTracker.cost(total, prices):.2f} · "
                               f"{_fmt_tokens(total['prompt'] + total['completion'])}")

        rows, rel = self.now, self.rel
        director = win.director
        if director is None:
            rows.show_text("state", "AI 驱动没开")
            for name in ("mood", "thought", "sees", "error"):
                rows.show_text(name, None)
            for name in ("relation", "energy", "talk", "plan", "reminders"):
                rel.show_text(name, None)
            self.eyes.show_text("ocr", None)
            self.eyes.show_text("stats", None)
            self.mind.show_text("log", None)
            self.rel_group.set_hint("")
            self.mind_group.set_hint("")
        else:
            st = director.status()
            if st["busy"]:
                state = "思考中…"
            elif st["quiet"]:
                state = "全屏勿扰"
            elif st["away"]:
                state = "你不在，打盹中"
            else:
                state = "醒着"
            if st["over_budget"]:
                state += " · 今日额度用完，只回应你说话"
            rows.show_text("state", state)
            from ..brain.relation import energy_word

            rel.show_text("relation", st["relation"] + (f" · 下一阶段还要：{st['relation_next']}"
                                                        if st.get("relation_next") else " · 已经最亲近了"))
            rel.show_text("energy", f"{st['energy']:.2f} · {energy_word(st['energy'])}")
            remarks = st.get("remarks")
            pace = "话少些" if st["talk_scale"] > 1.15 else ("话多些" if st["talk_scale"] < 0.9 else "照常")
            rel.show_text("talk", (f"{remarks['stats']} · 它学到的：{pace}（间隔 ×{st['talk_scale']:.2f}）")
                          if remarks else None)
            rel.show_text("plan", st["plan"])
            rel.show_text("reminders", "\n".join(st["reminders"]) if st["reminders"] else None)
            self.rel_group.set_hint(f"{st['relation']} · 精力 {st['energy']:.2f}")
            rows.show_text("mood", st["mood"])
            rows.show_text("thought", st["thought"])
            self.mind.show_text("log", "\n".join(
                f"{datetime.fromtimestamp(m['ts']).strftime('%H:%M')} {m['mood']} · {m['thought']}"
                for m in st["mind_log"]) or None)
            self.mind_group.set_hint(f"{len(st['mind_log'])} 条" if st["mind_log"] else "")
            window_info = st["window"]
            rows.show_text("sees", f"{window_info['app']} — {window_info['title'] or '…'}"
                                   f"（{window_info['minutes']} 分钟）" if window_info else None)
            self.eyes.show_text("ocr", (st["ocr"][:140] + "…") if st["ocr"] else None)
            ps = st.get("perception_stats")
            self.eyes.show_text("stats", (f"OCR {ps['ocr_n']} 次 / {ps['ocr_s']:.0f}s · observe {ps['observe_n']} 次 · "
                                          f"屏幕字 {ps['screen_chars']}（每小时清零）") if ps else None)
            rows.show_text("error", st["last_error"][:160] if st["last_error"] else
                           (st["ocr_error"][:160] if st["ocr_error"] else None))

        memory = win.memory
        self.affinity.setValue(int(memory.affinity * 100))
        self.affinity_value.setText(f"{memory.affinity:+.2f} · {_affinity_word(memory.affinity)}")
        self.mem.show_text("goal", memory.goal or None)
        self.mem.show_text("reflect", self._reflect_text())
        self.mem.show_text("diary", "\n".join(f"{d['date']}  {d['text']}" for d in memory.diary[-3:])
                           if memory.diary else "还没写过 · 反思时会写")
        self.mem.show_text("summary", memory.summary or "还没有：聊得多了，滑出去的旧对话会并成一段摘要")
        self.mem.show_text("facts", "\n".join(f"· {fact}" for fact in memory.profile[-6:])
                           + (f"\n（共 {len(memory.profile)} 条）" if len(memory.profile) > 6 else "")
                           or None)
        spots = [f"{n} → {e['path']}" for n, e in memory.places.items()] \
            + [f"{n} = {e['meaning']}" for n, e in memory.aliases.items()]
        self.mem.show_text("places", ("\n".join(f"· {x}" for x in spots[:6])
                                      + (f"\n（共 {len(spots)} 条）" if len(spots) > 6 else "")
                                      + "\n删除：/forget 名字") if spots else "还没有：用户确认过的地点和缩写才会记在这里")
        self.reflect.setEnabled(director is not None and not director.status()["reflect_pending"])
        self.pearl_list.refresh()
        self.errand_list.refresh()
        errands = getattr(win, "errands", None)
        self.errand_group.set_hint(f"{len(errands.items)} 件" if errands is not None and errands.items else "")
        pearls = getattr(win, "pearls", None)
        self.pearl_group.set_hint(f"{len(pearls.items)} 颗" if pearls is not None and pearls.items else "")
        self.mem_group.set_hint(f"好感 {memory.affinity:+.2f} · {_affinity_word(memory.affinity)}"
                                f" · 记住 {len(memory.profile)} 条")
        self._fit_screen()

    def _fit_screen(self) -> None:
        """滚动区按内容长高，但整窗不超过屏幕（留出任务栏和一点边）；超出的部分在里面滚。"""
        screen = self.screen()
        if screen is None:
            return
        area = screen.availableGeometry()
        chrome = self.height() - self.scroll.height()          # 标题栏、按钮、投影留白
        want = self.content.sizeHint().height()
        height = max(160, min(want, area.height() - chrome - 24))
        if height != self.scroll.height():
            self.scroll.setFixedHeight(height)
            self.adjustSize()
        if self.isVisible():
            geo = self.frameGeometry()
            x = min(max(geo.x(), area.left()), area.right() - geo.width())
            y = min(max(geo.y(), area.top()), area.bottom() - geo.height())
            if (x, y) != (geo.x(), geo.y()):
                self.move(x, y)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._fit_screen()
        self._sync_screen_mask()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._sync_screen_mask()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_screen_mask()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._clear_screen_mask()
        super().hideEvent(event)

    def _screen_rect(self) -> tuple[int, int, int, int]:
        """用和截图相同的 Win32 坐标，避免高 DPI 时 Qt 几何坐标偏移。"""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(int(self.winId())), ctypes.byref(rect)):
                return rect.left, rect.top, rect.right, rect.bottom
        geo = self.frameGeometry()
        return geo.left(), geo.top(), geo.right() + 1, geo.bottom() + 1

    def _sync_screen_mask(self) -> None:
        director = getattr(self.window, "director", None)
        perception = getattr(director, "perception", None)
        if perception is not None and self.isVisible():
            perception.set_own_overlay(self._screen_rect())

    def _clear_screen_mask(self) -> None:
        director = getattr(self.window, "director", None)
        perception = getattr(director, "perception", None)
        if perception is not None:
            perception.set_own_overlay(None)

    def _reflect_text(self) -> str:
        ctx = self.window.context
        last = (datetime.fromtimestamp(ctx.last_reflect).strftime("%m-%d %H:%M")
                if ctx.last_reflect else "还没有过")
        director = self.window.director
        if director is None:
            nxt = "AI 驱动没开"
        elif director.status()["reflect_pending"]:
            nxt = "马上"
        else:
            st = director.status()
            nxt = (f"约 {st['reflect_in_minutes']} 分钟后（你在电脑前的时间）"
                   f"，或活动再攒 {max(0, REFLECT_FULL - st['activity_since_reflect'])} 条")
        return f"共 {ctx.reflections} 次 · 上次 {last} · 下次 {nxt}"

    # -- 按钮 ------------------------------------------------------------------
    def _open_folder(self) -> None:
        from ..config import config_dir

        try:
            os.startfile(str(config_dir()))          # noqa: S606 —— Windows 资源管理器
        except (OSError, AttributeError):
            pass

    def _reflect_now(self) -> None:
        if self.window.director is not None:
            self.window.director.reflect_now()
            self.refresh()

    def _clear_memory(self) -> None:
        dialog = ConfirmDialog(spaced("清空记忆"),
                               f"确定清空{self.name}记住的所有事、日记、最近经历、对话记录和珍珠吗？"
                               "另一个角色不受影响。这一步撤不回来。",
                               self.accent, confirm="清空", danger=True, parent=self)
        if dialog.ask():
            self.window.clear_memory()
            self.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        self._clear_screen_mask()
        self.window._ai_panel = None
        super().closeEvent(event)


class _PearlList(QWidget):
    """最近几颗珍珠：色点 + 标题 + 它记下的一句话，右边「再读」「扔掉」。只在增删改后重建。"""

    SHOWN = 6

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(GAP_S)
        self._key = None

    def refresh(self) -> None:
        store = self.window.pearls
        key = (id(store), store.version, len(store.items))
        if key == self._key:
            return
        self._key = key
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not store.items:
            self.layout_.addWidget(label("还没有。把文件或一段文字拖到它身上，或者在对话窗口点「递珍珠」。",
                                         "muted"))
            return
        from ..brain.pearls import kind_color, kind_label

        for pearl in reversed(store.recent(self.SHOWN)):
            row = QHBoxLayout()
            row.setSpacing(GAP_S)
            r, g, b = kind_color(pearl["kind"])
            dot = label("●", "value", wrap=False)
            dot.setStyleSheet(f"color: rgb({r}, {g}, {b});")
            text = label(soft_wrap(f"{pearl['title']}（{kind_label(pearl['kind'])}）\n"
                                   f"{pearl['summary'] or '还没读'}"), "value", selectable=True)
            text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            again = QPushButton("再读")
            again.clicked.connect(lambda _=False, pid=pearl["id"]: self.window.reread_pearl(pid))
            again.setEnabled(self.window.director is not None)
            drop = QPushButton("扔掉")
            drop.setObjectName("danger")
            drop.clicked.connect(lambda _=False, pid=pearl["id"]: self.window.remove_pearl(pid))
            row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text, 1)
            row.addWidget(again, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(drop, 0, Qt.AlignmentFlag.AlignTop)
            holder = QWidget()
            holder.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self.layout_.addWidget(holder)
        if len(store.items) > self.SHOWN:
            self.layout_.addWidget(label(f"一共 {len(store.items)} 颗，这里只列最近 {self.SHOWN} 颗；"
                                         "全部的在右键「珍珠」里，或者 /pearls、/drop 编号。", "muted"))


class _ErrandList(QWidget):
    """在等的事：要等什么、它打算怎么盯（它自己写的）、盯哪些信号、最近几次醒来看到了什么，右边「放下」。"""

    STATES = {"planning": "正在想怎么盯", "waiting": "盯着", "woken": "醒了，正在看"}

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(GAP_S)
        self._key = None

    def refresh(self) -> None:
        from ..brain.errands import MAX_WAKES, describe_trigger

        errands = getattr(self.window, "errands", None)
        items = errands.items if errands is not None else []
        key = (errands.version if errands is not None else 0,
               tuple((e["id"], e["status"], e.get("wakes")) for e in items))
        if key == self._key:
            return
        self._key = key
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not items:
            hint = ("没有。对它说\"……好了告诉我\"，或者 /watch 要等的事。"
                    if self.window.config.perception != "off" else "「读屏幕」关着，心事不办。")
            self.layout_.addWidget(label(hint, "muted"))
            return
        for errand in items:
            until = datetime.fromtimestamp(errand["deadline"]).strftime("%H:%M")
            how = "、".join(describe_trigger(t) for t in errand["triggers"]) or "定时看一眼"
            next_check = (datetime.fromtimestamp(errand["next_check"]).strftime("%H:%M")
                          if errand.get("next_check") and errand["status"] == "waiting" else "—")
            lines = [errand["goal"],
                     f"{self.STATES.get(errand['status'], errand['status'])} · 信号：{how}",
                     f"下次看 {next_check} · 最多等到 {until} · 醒过 {errand['wakes']}/{MAX_WAKES} 次"]
            if errand.get("notes"):
                lines.append(f"它的打算：{errand['notes']}")
            for entry in (errand.get("history") or [])[-3:]:
                lines.append(f"· {entry}")
            text = label(soft_wrap("\n".join(lines)), "value", selectable=True)
            text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            drop = QPushButton("放下")
            drop.setObjectName("danger")
            drop.clicked.connect(lambda _=False, eid=errand["id"]: self.window.drop_errand(eid))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(GAP_S)
            row.addWidget(text, 1)
            row.addWidget(drop, 0, Qt.AlignmentFlag.AlignTop)
            holder = QWidget()
            holder.setLayout(row)
            self.layout_.addWidget(holder)


_OPEN: dict[str, bool] = {}        # 各分节的展开状态：面板关了再开还记得（只在本次运行内，不写盘）


class _Group(QWidget):
    """可折叠的分节：点标题行展开 / 收起，标题右边可以带一句提示（条数之类）。"""

    def __init__(self, title: str, content: QWidget, open_by_default: bool):
        super().__init__()
        self.title, self.content, self.hint = title, content, ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GAP_S + 2)
        self.head = section(spaced(title))
        self.head_label = self.head.findChild(QLabel)
        self.head.setCursor(Qt.CursorShape.PointingHandCursor)
        self.head.mousePressEvent = self._press
        layout.addWidget(self.head)
        layout.addWidget(content)
        self.set_open(_OPEN.get(title, open_by_default))

    def _press(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_open(not self._open)

    def set_open(self, value: bool) -> None:
        self._open = _OPEN[self.title] = value
        self.content.setVisible(value)
        self._retitle()

    def set_hint(self, text: str) -> None:
        if text != self.hint:
            self.hint = text
            self._retitle()

    def _retitle(self) -> None:
        self.head_label.setText(("▾ " if self._open else "▸ ") + spaced(self.title)
                                + (f"   {self.hint}" if self.hint else ""))


def _group(title: str, content: QWidget, open_: bool = False) -> _Group:
    return _Group(title, content, open_)
