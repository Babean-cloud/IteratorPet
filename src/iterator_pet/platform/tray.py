"""图标绘制 + 托盘菜单。

托盘**图标本体**改用 `platform/win_tray.py`（Win32 `Shell_NotifyIconW`）：`QSystemTrayIcon`
在这个环境里打包后注册不上（连最小 exe 都复现）。这里只留两件 Qt 该管的事：
1. `make_icon()` —— 给窗口/任务栏用的 QIcon；
2. `tray_menu()` —— 右键托盘时弹出的菜单。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu

from ..config import SIZE_PRESETS, save
from ..rendering.theme import DEFAULT_PALETTE

TRAY_ICON_NAME = "tray.png"
ICON_SIZE = 64


def _asset_path(name: str) -> Path:
    """包里 / PyInstaller 解包目录里找 assets。"""
    if hasattr(sys, "_MEIPASS"):                      # 打包后
        return Path(sys._MEIPASS) / "iterator_pet" / "assets" / name
    return Path(__file__).resolve().parent.parent / "assets" / name


def _portrait_pixmap(size: int) -> QPixmap | None:
    path = _asset_path(TRAY_ICON_NAME)
    if not path.is_file():
        return None
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return None
    return pixmap.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _draw_fallback(painter: QPainter, size: int) -> None:
    """没有 assets 时的手绘图标（旧版那套：袍子 + 头 + 眼睛）。"""
    p = DEFAULT_PALETTE
    grad = QLinearGradient(0.0, size * 0.35, 0.0, size)
    grad.setColorAt(0.0, QColor(p.robe_light))
    grad.setColorAt(0.55, QColor(p.robe_mid))
    grad.setColorAt(1.0, QColor(p.robe_dark))
    painter.setBrush(grad)
    painter.setPen(QPen(QColor("#5c2b3b"), max(1.0, size * 0.02)))
    painter.drawRoundedRect(
        QRectF(size * 0.30, size * 0.34, size * 0.40, size * 0.50), size * 0.16, size * 0.16
    )
    painter.setBrush(QColor(p.head_mid))
    painter.setPen(QPen(QColor("#6d3345"), max(1.0, size * 0.02)))
    painter.drawRoundedRect(
        QRectF(size * 0.31, size * 0.10, size * 0.38, size * 0.28), size * 0.14, size * 0.14
    )
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(p.eye))
    painter.drawEllipse(QPointF(size * 0.42, size * 0.24), size * 0.045, size * 0.055)
    painter.drawEllipse(QPointF(size * 0.58, size * 0.24), size * 0.045, size * 0.055)


def make_icon(size: int = ICON_SIZE) -> QIcon:
    """单张 pixmap 的 QIcon（窗口图标用）。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    portrait = _portrait_pixmap(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    if portrait is not None:
        painter.drawPixmap((size - portrait.width()) // 2, (size - portrait.height()) // 2, portrait)
    else:
        _draw_fallback(painter, size)
    painter.end()
    return QIcon(pixmap)


TERRITORY_LABELS = (("", "自由活动"), ("left", "左边"), ("right", "右边"),
                    ("top", "上边"), ("bottom", "下边"))
PERCEPTION_LABELS = (("off", "关"), ("title", "只看窗口标题"), ("ocr", "标题 + 本地文字识别"))


PEARLS_IN_MENU = 10


def add_pearl_menu(menu: QMenu, window) -> None:
    """「珍珠」子菜单：最近几颗，每颗可以再读 / 扔掉；最后一项全部扔掉。不用再翻状态面板。"""
    from ..brain.pearls import kind_label
    from .ui_theme import NAMES

    store = getattr(window, "pearls", None)
    pearl_menu = menu.addMenu("珍珠")
    if store is None or not store.items:
        empty = QAction("还没有（把文件、图、链接拖到它身上）", pearl_menu)
        empty.setEnabled(False)
        pearl_menu.addAction(empty)
        return
    for number, pearl in enumerate(reversed(store.recent(PEARLS_IN_MENU)), start=1):
        title = pearl["title"] if len(pearl["title"]) <= 24 else pearl["title"][:23] + "…"
        gift = pearl.get("gift")
        origin = f"，{gift['from']}转来的" if gift else ""
        one = pearl_menu.addMenu(f"{number}. {title}（{kind_label(pearl['kind'])}{origin}）")
        again = QAction("再读一遍", one)
        again.setEnabled(getattr(window, "director", None) is not None)
        again.triggered.connect(lambda _=False, pid=pearl["id"]: window.reread_pearl(pid))
        one.addAction(again)
        give = QAction(f"转给{NAMES.get(window.other_character, '另一位')}…", one)
        give.triggered.connect(lambda _=False, pid=pearl["id"]: window.ask_hand_over(pid))
        one.addAction(give)
        drop = QAction("扔掉", one)
        drop.triggered.connect(lambda _=False, pid=pearl["id"]: window.remove_pearl(pid))
        one.addAction(drop)
    if len(store.items) > PEARLS_IN_MENU:
        more = QAction(f"（一共 {len(store.items)} 颗，/pearls 看全部）", pearl_menu)
        more.setEnabled(False)
        pearl_menu.addAction(more)
    pearl_menu.addSeparator()
    drop_all = QAction("全部扔掉…", pearl_menu)
    drop_all.triggered.connect(window.remove_all_pearls)
    pearl_menu.addAction(drop_all)


def add_ai_actions(menu: QMenu, window) -> None:
    """AI 相关的几项 + 「待在」。右键人偶和托盘共用一份，免得两边不一致。"""
    action_talk = QAction(f"说句话 / 命令…（{getattr(window.config, 'chat_hotkey', 'Ctrl+Alt+I')}）", menu)
    action_talk.triggered.connect(window.open_quick_bar)
    menu.addAction(action_talk)

    action_chat = QAction("对话窗口…", menu)
    action_chat.triggered.connect(window.open_chat)
    menu.addAction(action_chat)

    stay_menu = menu.addMenu("待在")
    current = getattr(window, "territory", "")
    for key, label in TERRITORY_LABELS:
        action = QAction(label, stay_menu)
        action.setCheckable(True)
        action.setChecked(current == key)
        action.triggered.connect(
            lambda _checked=False, side=key: window.set_territory(side or None))
        stay_menu.addAction(action)

    add_pearl_menu(menu, window)

    action_quiet = QAction("今天别说话", menu)
    action_quiet.setCheckable(True)
    action_quiet.setChecked(bool(getattr(window, "quiet_today", False)))
    action_quiet.triggered.connect(lambda checked: window.set_quiet_today(bool(checked)))
    menu.addAction(action_quiet)

    action_panel = QAction("AI 状态…", menu)
    action_panel.triggered.connect(window.show_ai_panel)
    menu.addAction(action_panel)

    # 不常动的开关收进一个子菜单，右键菜单别拉成一长条
    ai_menu = menu.addMenu("AI")
    action_llm = QAction("AI 驱动（DeepSeek）", ai_menu)
    action_llm.setCheckable(True)
    action_llm.setChecked(bool(getattr(window, "llm_enabled", False)))
    action_llm.triggered.connect(lambda checked: window.set_llm_enabled(bool(checked)))
    ai_menu.addAction(action_llm)

    sense_menu = ai_menu.addMenu("读屏幕")
    level = getattr(window.config, "perception", "ocr")
    for key, label in PERCEPTION_LABELS:
        action = QAction(label, sense_menu)
        action.setCheckable(True)
        action.setChecked(level == key)
        action.triggered.connect(lambda _checked=False, name=key: window.set_perception(name))
        sense_menu.addAction(action)

    vision_menu = ai_menu.addMenu("看图模型（图片会发出本机）")
    vision_mode = getattr(window.config, "screen_vision", "off")
    for key, label in (("off", "关闭（默认）"),
                       ("roi", "智能裁图（明确要求看屏幕时）"),
                       ("full", "直接整窗截图（明确要求看屏幕时）")):
        action = QAction(label, vision_menu)
        action.setCheckable(True)
        action.setChecked(vision_mode == key)
        action.triggered.connect(lambda _checked=False, name=key: window.set_screen_vision(name))
        vision_menu.addAction(action)

    action_llm_cfg = QAction("API Key…", ai_menu)
    action_llm_cfg.triggered.connect(window.configure_llm)
    ai_menu.addAction(action_llm_cfg)
    action_bridge = QAction("接入编程 agent…", ai_menu)
    action_bridge.triggered.connect(window.show_bridge_setup)
    ai_menu.addAction(action_bridge)
    menu.addSeparator()


VIEW_LABELS = (("pixel_art", "像素风"), ("pixel_snap", "像素对齐（不闪，像素略大/小）"),
               ("drop_shadow", "光影（柔光 + 落影）"), ("show_pearls", "珍珠（收着的绕着它漂）"),
               ("overseer_enabled", "监视者投影（关掉就用窗口）"))


def add_view_actions(menu: QMenu, window) -> None:
    view_menu = menu.addMenu("画面")
    for key, label in VIEW_LABELS:
        action = QAction(label, view_menu)
        action.setCheckable(True)
        action.setChecked(bool(getattr(window, key, False)))
        action.triggered.connect(
            lambda checked, name=key: window.set_view_option(name, bool(checked)))
        view_menu.addAction(action)
    add_sound_actions(menu, window)


SOUND_LABELS = (("sound_enabled", "声音"), ("sound_speech", "说话声（哔哔啵啵 + 字幕）"),
                ("sound_actions", "动作声（机械臂、底座、电光、放电）"))
SOUND_VOLUMES = (("轻", 0.3), ("中", 0.6), ("响", 0.9))


def add_sound_actions(menu: QMenu, window) -> None:
    """「声音」：原版音效（从你本机的游戏里取）。没装游戏时整个菜单灰掉。"""
    config = window.config
    sound_menu = menu.addMenu("声音")
    board = getattr(window, "sound", None)
    if board is not None:
        def diagnose():
            from PySide6.QtWidgets import QMessageBox
            try:
                path = board.save_diagnostics()
            except Exception as exc:
                QMessageBox.warning(window, "音频诊断", f"保存失败：{exc}")
                return
            QMessageBox.information(window, "音频诊断", f"已保存：\n{path}")

        diagnostic = QAction("保存音频诊断…", sound_menu)
        diagnostic.triggered.connect(diagnose)
        sound_menu.addAction(diagnostic)
    if board is not None and not board.ready:
        hint = QAction("正在从游戏里取声音…" if board._loading else "没找到游戏的音效", sound_menu)
        hint.setEnabled(False)
        sound_menu.addAction(hint)
        sound_menu.addSeparator()
    for key, label in SOUND_LABELS:
        action = QAction(label, sound_menu)
        action.setCheckable(True)
        action.setChecked(bool(getattr(config, key, True)))
        action.setEnabled(key == "sound_enabled" or bool(getattr(config, "sound_enabled", True)))
        action.triggered.connect(lambda checked, name=key: window.set_sound_option(name, bool(checked)))
        sound_menu.addAction(action)
    sound_menu.addSeparator()
    current = float(getattr(config, "sound_volume", 0.6))
    for label, value in SOUND_VOLUMES:
        action = QAction(f"音量：{label}", sound_menu)
        action.setCheckable(True)
        action.setChecked(abs(current - value) < 0.05)
        action.triggered.connect(lambda _=False, v=value: window.set_sound_option("sound_volume", v))
        sound_menu.addAction(action)


def tray_menu(window, on_dev_panel=None) -> QMenu:
    """右键托盘时弹出的菜单（不持有引用：每次现建，避免状态过期）。"""
    menu = QMenu()
    add_ai_actions(menu, window)
    action_pause = QAction("继续" if getattr(window, "paused", False) else "暂停", menu)
    action_pause.triggered.connect(window.toggle_pause)
    menu.addAction(action_pause)

    action_click = QAction("鼠标穿透", menu)
    action_click.setCheckable(True)
    action_click.setChecked(bool(getattr(window, "click_through", False)))
    action_click.triggered.connect(window.toggle_click_through)
    menu.addAction(action_click)

    from . import autostart

    action_boot = QAction("开机自启", menu)
    action_boot.setCheckable(True)
    action_boot.setChecked(autostart.enabled())
    action_boot.triggered.connect(lambda checked: action_boot.setChecked(autostart.set_enabled(bool(checked))))
    menu.addAction(action_boot)

    menu.addSeparator()
    action_dev = QAction("开发者面板", menu)
    action_dev.setCheckable(True)
    action_dev.setEnabled(on_dev_panel is not None)
    if on_dev_panel is not None:
        action_dev.triggered.connect(lambda: action_dev.setChecked(bool(on_dev_panel())))
    menu.addAction(action_dev)

    menu.addSeparator()
    action_cursor = QAction("光标特效（杀鼠标时）", menu)
    action_cursor.setCheckable(True)
    action_cursor.setChecked(bool(getattr(window.config, "cursor_fx", True)))
    def _toggle_cursor(checked: bool) -> None:
        window.config.cursor_fx = bool(checked)
        from . import cursorfx
        cursorfx.set_enabled(bool(checked))
        save(window.config)
    action_cursor.triggered.connect(_toggle_cursor)
    menu.addAction(action_cursor)

    add_view_actions(menu, window)
    size_menu = menu.addMenu("尺寸")
    current_size = getattr(window, "size_preset", "full")
    for key, (factor, label) in SIZE_PRESETS.items():
        action_size = QAction(label, size_menu)
        action_size.setCheckable(True)
        action_size.setChecked(current_size == key)
        action_size.triggered.connect(lambda _checked=False, name=key: window.set_size(name))
        size_menu.addAction(action_size)

    spark_menu = menu.addMenu("电光频率")
    current_rate = getattr(window, "spark_rate", "eco")
    for key, label in (("off", "无"), ("eco", "节能"), ("computing", "computing（1.5×）")):
        action_rate = QAction(label, spark_menu)
        action_rate.setCheckable(True)
        action_rate.setChecked(current_rate == key)
        action_rate.triggered.connect(
            lambda _checked=False, name=key: window.set_spark_rate(name)
        )
        spark_menu.addAction(action_rate)

    character_menu = menu.addMenu("角色")
    current_character = getattr(window, "character", "pebbles")
    for key, label in (("pebbles", "五块卵石"), ("past_moon", "仰望皓月")):
        action_character = QAction(label, character_menu)
        action_character.setCheckable(True)
        action_character.setChecked(current_character == key)
        action_character.triggered.connect(
            lambda _checked=False, name=key: window.set_character(name)
        )
        character_menu.addAction(action_character)

    action_dialogue = QAction("对话", menu)
    action_dialogue.setCheckable(True)
    action_dialogue.setChecked(bool(getattr(window.config, "dialogue_enabled", True)))
    action_dialogue.triggered.connect(
        lambda checked: window.set_dialogue_enabled(bool(checked))
    )
    menu.addAction(action_dialogue)

    menu.addSeparator()
    action_quit = QAction("退出", menu)
    action_quit.triggered.connect(window.close)
    menu.addAction(action_quit)
    return menu
