"""管理员身份运行时的拖文件退路：老式 WM_DROPFILES。

桌宠以管理员身份跑（比如从管理员终端启动）时，Windows 的 UIPI 不许普通权限的资源管理器
跨完整性级别调用我们的 OLE 拖放目标（``RegisterDragDrop``），``dragEnterEvent`` 一次都不会来
（用户实测：接球圈有反应，``拖入`` 一条都没有）。

老式的 ``DragAcceptFiles`` + ``WM_DROPFILES`` 走的是窗口消息，可以用 ``ChangeWindowMessageFilterEx``
单独放行。前提是窗口上**不能**同时注册着 OLE 目标（资源管理器看到 OLE 目标就走 OLE，然后被拦），
所以管理员模式下关掉 Qt 的 ``setAcceptDrops``，改走这条。只认文件，拖文字这条路过不来。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

WM_DROPFILES = 0x0233
WM_COPYDATA = 0x004A
WM_COPYGLOBALDATA = 0x0049                    # 未公开，资源管理器跨进程搬 HDROP 用
MSGFLT_ALLOW = 1


def enable(window) -> bool:
    """给窗口打开 WM_DROPFILES 并放行跨完整性级别的那几条消息。可重复调用。"""
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(window.winId())
        user32 = ctypes.windll.user32
        user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
        for msg in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):
            user32.ChangeWindowMessageFilterEx(hwnd, msg, MSGFLT_ALLOW, None)
        ctypes.windll.ole32.RevokeDragDrop(wintypes.HWND(hwnd))   # 万一 Qt 已经注册过 OLE 目标
        ctypes.windll.shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
        return True
    except Exception:
        return False


def dropped_files(hdrop) -> list[str]:
    """从 HDROP 里取出文件路径，并释放它。"""
    shell32 = ctypes.windll.shell32
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = [ctypes.c_void_p]
    paths = []
    try:
        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        for index in range(min(count, 16)):
            size = shell32.DragQueryFileW(hdrop, index, None, 0) + 1
            buffer = ctypes.create_unicode_buffer(size)
            shell32.DragQueryFileW(hdrop, index, buffer, size)
            paths.append(buffer.value)
    finally:
        shell32.DragFinish(hdrop)
    return paths


def files_from_message(event_type, message) -> list[str] | None:
    """``nativeEvent`` 用：是 WM_DROPFILES 就返回文件列表（已释放 HDROP），否则 None。"""
    if sys.platform != "win32" or bytes(event_type) != b"windows_generic_MSG":
        return None
    msg = wintypes.MSG.from_address(int(message))
    if msg.message != WM_DROPFILES:
        return None
    return dropped_files(msg.wParam)
