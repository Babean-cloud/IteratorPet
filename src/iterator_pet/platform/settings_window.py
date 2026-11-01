"""「设置…」窗口：把原来只能改 ``config.json`` 的高级项做成表单（用户 2026-11-02：门槛太高）。

表单由 ``FIELDS`` 驱动：加一项设置只要在表里加一行。保存时 ``apply_values`` 把表单值写回 config
（解析不了的数字保留原值并报出来），窗口层再按 ``LIVE`` 把能立刻生效的推给桌宠，其余重启后生效。
API Key 仍走右键「AI」→「API Key…」，这里不重复。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLineEdit, QPushButton, QScrollArea,
    QSlider, QWidget,
)

from .ui_theme import FramedWindow, accent_for, button_row, label, section, spaced


@dataclass(frozen=True)
class Field:
    section: str
    key: str              # config 字段名；以 _ 开头的是不进 config 的（比如开机自启写注册表）
    label: str
    kind: str             # text / secret / bool / int / float / choice / slider / list / folder
    options: tuple = ()   # choice：((值, 显示), ...)
    hint: str = ""


FIELDS = (
    Field("通用", "_autostart", "开机自启", "bool"),
    Field("通用", "update_check", "检查新版本", "bool", hint="一天最多查一次 GitHub，不带本机信息"),
    Field("通用", "chat_hotkey", "快捷输入条", "text", hint="例如 Ctrl+Alt+I（重启后生效）"),
    Field("通用", "auto_quiet", "全屏时自动安静", "bool"),
    Field("通用", "overseer_scroll_speed", "长结果滚动速度", "float", hint="字 / 秒"),
    Field("AI", "llm_base_url", "接口地址", "text", hint="任何 OpenAI 兼容接口"),
    Field("AI", "llm_model", "对话模型", "text"),
    Field("AI", "llm_vision_model", "看图模型", "text"),
    Field("AI", "llm_talkativeness", "话量", "slider", hint="左安静，右话多"),
    Field("AI", "llm_daily_token_cap", "每日 token 上限", "int", hint="0 = 不限"),
    Field("AI", "llm_price_hit", "单价：输入命中缓存", "float", hint="元 / 百万 token，只用来估算"),
    Field("AI", "llm_price_miss", "单价：输入未命中", "float"),
    Field("AI", "llm_price_out", "单价：输出", "float"),
    Field("读屏幕", "perception", "读屏幕", "choice",
          (("off", "关"), ("title", "只看窗口标题"), ("ocr", "本地文字识别"))),
    Field("读屏幕", "screen_vision", "看图（图片会发出本机）", "choice",
          (("off", "关闭"), ("roi", "智能裁图"), ("full", "整窗截图"))),
    Field("读屏幕", "perception_blacklist", "不看的窗口", "list", hint="程序名或标题关键词，逗号分隔；空 = 内置默认表"),
    Field("任务", "task_workspace", "默认任务文件夹", "folder"),
    Field("任务", "auto_approve_readonly", "只读命令不逐条批准", "bool"),
    Field("任务", "auto_approve_all", "自动批准命令（危险的仍会问）", "bool"),
    Field("联网", "search_provider", "搜索服务", "choice",
          (("", "不用（只能读网页）"), ("tavily", "Tavily"), ("brave", "Brave"), ("searxng", "SearXNG"))),
    Field("联网", "search_api_key", "搜索服务 Key", "secret"),
    Field("联网", "search_base_url", "SearXNG 地址", "text"),
    Field("外部工具", "ffmpeg_path", "ffmpeg", "text", hint="空 = 在 PATH 里找"),
    Field("外部工具", "ytdlp_path", "yt-dlp", "text"),
    Field("外部工具", "asr_base_url", "语音识别接口", "text", hint="OpenAI 兼容 /audio/transcriptions"),
    Field("外部工具", "asr_model", "语音识别模型", "text"),
    Field("外部工具", "asr_api_key", "语音识别 Key", "secret"),
    Field("外部工具", "bridge_enabled", "编程 agent 接口", "bool", hint="重启后生效"),
)

# 保存后能立刻推给桌宠的；其余重启后生效
LIVE = {"llm_talkativeness", "llm_daily_token_cap", "perception", "screen_vision", "task_workspace",
        "llm_base_url", "llm_model", "llm_vision_model", "auto_approve_readonly", "auto_approve_all",
        "llm_price_hit", "llm_price_miss", "llm_price_out", "update_check", "auto_quiet",
        "overseer_scroll_speed", "search_provider", "search_api_key", "search_base_url",
        "ffmpeg_path", "ytdlp_path", "asr_base_url", "asr_model", "asr_api_key", "perception_blacklist"}


def apply_values(cfg, values: dict) -> tuple[list[str], list[str]]:
    """把表单值写进 cfg。返回 (改了的字段, 填得不对没改的字段的显示名)。不认识的 key 忽略。"""
    changed, bad = [], []
    by_key = {f.key: f for f in FIELDS}
    for key, raw in values.items():
        field = by_key.get(key)
        if field is None or key.startswith("_"):
            continue
        old = getattr(cfg, key)
        try:
            if field.kind == "int":
                new = max(0, int(str(raw).strip() or 0))
            elif field.kind in ("float", "slider"):
                new = float(str(raw).strip())
                new = min(1.0, max(0.0, new)) if field.kind == "slider" else max(0.0, new)
            elif field.kind == "bool":
                new = bool(raw)
            elif field.kind == "list":
                new = [s.strip() for s in str(raw).replace("，", ",").split(",") if s.strip()]
            elif field.kind == "choice":
                new = str(raw)
                if new not in {value for value, _ in field.options}:
                    raise ValueError(new)
            else:
                new = str(raw).strip()
        except (TypeError, ValueError):
            bad.append(field.label)
            continue
        if new != old:
            setattr(cfg, key, new)
            changed.append(key)
    return changed, bad


class SettingsWindow(FramedWindow):
    def __init__(self, window):
        self.window = window
        super().__init__(spaced("设置"), accent_for(getattr(window, "character", "pebbles")))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFixedWidth(560)
        self.editors: dict[str, tuple[Field, QWidget]] = {}
        content = QWidget()
        page = QFormLayout(content)
        page.setContentsMargins(0, 0, 10, 0)
        page.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        current = None
        for field in FIELDS:
            if field.section != current:
                current = field.section
                page.addRow(section(current))
            editor = self._editor(field)
            self.editors[field.key] = (field, editor)
            page.addRow(field.label, editor)
            if field.hint:
                page.addRow("", label(field.hint, "muted"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setMinimumHeight(520)
        self.body.addWidget(scroll, 1)
        self.status = label("API Key 在右键「AI」→「API Key…」里改。", "muted")
        self.body.addWidget(self.status)
        save_button = QPushButton("保存")
        save_button.clicked.connect(self.save)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        self.body.addLayout(button_row(save_button, left=(close_button,)))

    def _value(self, field: Field):
        if field.key == "_autostart":
            from . import autostart

            return autostart.enabled()
        return getattr(self.window.config, field.key)

    def _editor(self, field: Field) -> QWidget:
        value = self._value(field)
        if field.kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(value))
            return box
        if field.kind == "choice":
            combo = QComboBox()
            for option, text in field.options:
                combo.addItem(text, option)
            index = combo.findData(value)
            combo.setCurrentIndex(max(0, index))
            return combo
        if field.kind == "slider":
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(int(round(float(value) * 100)))
            return slider
        edit = QLineEdit(", ".join(value) if field.kind == "list" else str(value))
        if field.kind == "secret":
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        if field.kind != "folder":
            return edit
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        pick = QPushButton("选…")

        def choose():
            folder = QFileDialog.getExistingDirectory(self, "选文件夹", edit.text())
            if folder:
                edit.setText(folder)

        pick.clicked.connect(choose)
        row.addWidget(pick)
        holder.edit = edit
        return holder

    def values(self) -> dict:
        out = {}
        for key, (field, editor) in self.editors.items():
            if isinstance(editor, QCheckBox):
                out[key] = editor.isChecked()
            elif isinstance(editor, QComboBox):
                out[key] = editor.currentData()
            elif isinstance(editor, QSlider):
                out[key] = editor.value() / 100.0
            else:
                out[key] = (getattr(editor, "edit", editor)).text()
        return out

    def save(self) -> None:
        values = self.values()
        from . import autostart

        boot = bool(values.pop("_autostart"))
        if boot != autostart.enabled():
            autostart.set_enabled(boot)
        changed, bad = apply_values(self.window.config, values)
        restart = self.window.apply_settings(changed)
        parts = ["已保存。"] if changed or not bad else []
        if bad:
            parts.append(f"这几项填得不对，没改：{'、'.join(bad)}。")
        if restart:
            parts.append("有几项重启后生效。")
        self.status.setText("".join(parts) or "没有改动。")
