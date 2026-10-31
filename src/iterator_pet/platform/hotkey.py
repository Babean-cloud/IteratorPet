"""全局快捷键：Ctrl+Alt+Q 退出桌宠；对话窗口默认 Ctrl+Alt+I（config.chat_hotkey 可改）。

Ctrl+Alt+Space 被 Claude 桌面版占了（用户实测按下去弹的是 Claude），所以换掉了。

存在的理由：Windows 11 会把新应用的托盘图标**默认折叠进溢出区**（那个 `^`），而桌宠可以
开着「鼠标穿透」——两个条件一叠加，玩家就找不到任何能退出它的入口。托盘图标还在、通知也发了，
但一个**不依赖任何可见 UI** 的退出口是必要的兜底。

用 `RegisterHotKey(NULL, ...)`（线程消息）而不是键盘钩子：不注入、不拦截别人的按键，
注册失败（组合被占用）只是这条兜底失效，记一行日志。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes

HOTKEY_ID = 0x4950  # "IP"
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
VK_Q = 0x51
VK_SPACE = 0x20
CHAT_HOTKEY_ID = 0x4951
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN}
_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "`": 0xC0}


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """``"Ctrl+Alt+I"`` → (modifiers, vk)。至少要一个修饰键；认不出返回 None。"""
    parts = [p.strip().lower() for p in str(text or "").split("+") if p.strip()]
    if len(parts) < 2:
        return None
    modifiers = 0
    for part in parts[:-1]:
        if part not in _MODS:
            return None
        modifiers |= _MODS[part]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key in _KEYS:
        vk = _KEYS[key]
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        vk = 0x70 + int(key[1:]) - 1
    else:
        return None
    return modifiers, vk


class GlobalHotkey:
    """注册一个全局热键，触发时调用 ``callback``（在 Qt 主线程的事件循环里）。"""

    def __init__(self, callback, key: int = VK_Q, modifiers: int = MOD_CONTROL | MOD_ALT,
                 hotkey_id: int = HOTKEY_ID) -> None:
        self._callback = callback
        self._key = key
        self._modifiers = modifiers
        self._id = hotkey_id
        self._registered = False
        self._filter = None
        self._native = None

    @property
    def registered(self) -> bool:
        return self._registered

    def install(self, app) -> bool:
        """注册热键并挂上原生事件过滤器；返回是否成功。"""
        if self._registered or not hasattr(ctypes, "windll"):
            return False
        try:
            from PySide6.QtCore import QAbstractNativeEventFilter

            user32 = ctypes.windll.user32
            self._native = user32
            ok = bool(user32.RegisterHotKey(None, self._id, self._modifiers | MOD_NOREPEAT, self._key))
            if not ok:
                return False

            class _Filter(QAbstractNativeEventFilter):
                def __init__(self, owner):
                    super().__init__()
                    self.owner = owner

                def nativeEventFilter(self, event_type, message):  # noqa: N802
                    try:
                        msg = wintypes.MSG.from_address(int(message))
                    except Exception:
                        return False, 0
                    if msg.message == WM_HOTKEY and int(msg.wParam) == self.owner._id:
                        self.owner._callback()
                        return True, 0
                    return False, 0

            self._filter = _Filter(self)
            app.installNativeEventFilter(self._filter)
            self._registered = True
            return True
        except Exception:
            self._registered = False
            return False

    def suspend(self) -> None:
        """全屏勿扰时让出按键（游戏里同一组合键要能用）。过滤器留着，resume 再注册回来。"""
        if self._registered and self._native is not None:
            try:
                self._native.UnregisterHotKey(None, self._id)
            except Exception:
                pass
            self._suspended = True

    def resume(self) -> None:
        if getattr(self, "_suspended", False) and self._native is not None:
            try:
                self._native.RegisterHotKey(None, self._id, self._modifiers | MOD_NOREPEAT, self._key)
            except Exception:
                pass
            self._suspended = False

    def remove(self) -> None:
        if self._registered and self._native is not None:
            try:
                self._native.UnregisterHotKey(None, self._id)
            except Exception:
                pass
        self._registered = False
