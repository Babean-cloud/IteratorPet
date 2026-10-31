"""监听前台切换与新顶层窗口；回调只记 HWND，读屏仍由导演线程决定。"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

EVENT_OBJECT_SHOW = 0x8002
EVENT_SYSTEM_FOREGROUND = 0x0003
WM_QUIT = 0x0012


@dataclass(frozen=True)
class WindowMeta:
    hwnd: int
    pid: int
    owner: int
    class_name: str
    visible: bool
    top_level: bool


def inspect_window(hwnd: int) -> WindowMeta | None:
    """只取判断弹窗所需的 Win32 元数据，不读窗口内容。"""
    if sys.platform != "win32" or not hwnd:
        return None
    user32 = ctypes.windll.user32
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = (ctypes.c_void_p, wintypes.UINT)
    user32.GetWindow.restype = ctypes.c_void_p
    user32.GetWindow.argtypes = (ctypes.c_void_p, wintypes.UINT)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    name = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(ctypes.c_void_p(hwnd), name, len(name))
    return WindowMeta(hwnd, int(pid.value), int(user32.GetWindow(ctypes.c_void_p(hwnd), 4) or 0),
                      name.value, bool(user32.IsWindowVisible(ctypes.c_void_p(hwnd))),
                      int(user32.GetAncestor(ctypes.c_void_p(hwnd), 2) or 0) == hwnd)


def is_popup_candidate(meta: WindowMeta | None, foreground_hwnd: int,
                       previous_pid: int, own_pid: int) -> bool:
    """只响应当前前台的新顶层窗口；普通菜单、别的程序弹窗不算。"""
    return bool(meta and meta.hwnd == foreground_hwnd and meta.top_level and meta.visible
                and meta.class_name != "#32768"
                and meta.pid != own_pid
                and (meta.class_name == "#32770" or
                     (previous_pid and meta.pid == previous_pid)))


class PopupWatcher:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._latest: tuple[int, float] | None = None
        self._foreground: tuple[int, float] | None = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._stopping = False

    def record(self, hwnd: int) -> None:
        """WinEvent 回调只做这一步；重复 SHOW 被合并并防抖。"""
        with self._lock:
            self._latest = (hwnd, self.clock())

    def record_foreground(self, hwnd: int) -> None:
        with self._lock:
            self._foreground = (hwnd, self.clock())

    def foreground_ready(self) -> bool:
        with self._lock:
            return bool(self._foreground and self.clock() - self._foreground[1] >= 0.3)

    def take_foreground(self) -> int | None:
        with self._lock:
            if not self._foreground or self.clock() - self._foreground[1] < 0.3:
                return None
            hwnd, _at = self._foreground
            self._foreground = None
            return hwnd

    def ready(self) -> int | None:
        with self._lock:
            event = self._latest
            if event is None:
                return None
            age = self.clock() - event[1]
            if age < 0.3:
                return None
            self._latest = None
        return event[0] if age <= 5.0 else None

    def start(self) -> None:
        if sys.platform != "win32" or (self._thread is not None and self._thread.is_alive()):
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="IteratorPet-WinEvent")
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        thread_id = self._thread_id
        if thread_id and sys.platform == "win32":
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        if self._thread is None or not self._thread.is_alive():
            self._thread = None
            self._thread_id = 0
        with self._lock:
            self._latest = None
            self._foreground = None

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = (ctypes.c_void_p, wintypes.UINT)
        user32.GetClassNameW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
        callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

        def on_event(_hook, _event, hwnd, object_id, child_id, _thread, _time):
            if _event == EVENT_SYSTEM_FOREGROUND:
                if hwnd:
                    self.record_foreground(int(hwnd))
                return
            if hwnd and object_id == 0 and child_id == 0 and user32.GetAncestor(hwnd, 2) == hwnd:
                name = ctypes.create_unicode_buffer(32)
                user32.GetClassNameW(hwnd, name, len(name))
                if name.value != "#32768":       # 菜单 SHOW 很频繁，不当弹窗
                    self.record(int(hwnd))

        callback = callback_type(on_event)
        user32.SetWinEventHook.restype = ctypes.c_void_p
        user32.SetWinEventHook.argtypes = (wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                           callback_type, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
        user32.UnhookWinEvent.argtypes = (ctypes.c_void_p,)
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # 建立本线程的消息队列
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if self._stopping:
            return
        hooks = [user32.SetWinEventHook(event, event, None, callback, 0, 0, 0x0002)
                 for event in (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_SHOW)]
        hooks = [hook for hook in hooks if hook]
        if not hooks:
            self._thread_id = 0
            return
        try:
            while not self._stopping and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for hook in hooks:
                user32.UnhookWinEvent(hook)
            self._thread_id = 0
