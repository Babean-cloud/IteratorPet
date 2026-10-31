"""心事用的"眼睛"：不只看前台窗口。全部本地、ctypes，工作线程里也能调。

* ``list_windows``：所有看得见的顶层窗口（含后台的），隐私窗口只给"（隐私窗口）"；
* ``capture_window``：用 ``PrintWindow`` 截某个窗口——被挡住、在后台也能截（最小化的不行）；
* ``process_count`` / ``file_status``：进程还在不在、文件变没变。

隐私：黑名单（``perception.DEFAULT_BLACKLIST``）命中的窗口不给标题、不截图。文件只看大小和修改时间，
不读内容。截图只在本机认字，图不出本机。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from .perception import DEFAULT_BLACKLIST, _process_name


@dataclass
class Win:
    hwnd: int
    pid: int
    app: str
    title: str
    private: bool
    minimized: bool

    def label(self) -> str:
        app = self.app[:-4] if self.app.lower().endswith(".exe") else self.app
        return "（隐私窗口）" if self.private else f"{app}：{self.title[:80]}"


BLACKLIST = DEFAULT_BLACKLIST          # 桌宠启动时换成 config.perception_blacklist


def _is_private(app: str, title: str, blacklist) -> bool:
    haystack = f"{app} {title}".lower()
    return any(str(word).lower() in haystack for word in blacklist)


def list_windows(blacklist=None) -> list[Win]:
    """看得见、有标题、没被系统藏起来（UWP 的 cloaked）的顶层窗口，按 Z 序（前台在前）。自己的窗口不算。"""
    if sys.platform != "win32":
        return []
    blacklist = BLACKLIST if blacklist is None else blacklist
    user32 = ctypes.windll.user32
    dwm = ctypes.windll.dwmapi
    own = os.getpid()
    found: list[Win] = []
    names: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):     # GW_OWNER：弹窗、工具条不算
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own or buf.value in ("Program Manager",):
            return True
        if pid.value not in names:
            names[pid.value] = _process_name(pid.value)
        app = names[pid.value]
        found.append(Win(int(hwnd), int(pid.value), app, buf.value,
                         _is_private(app, buf.value, blacklist), bool(user32.IsIconic(hwnd))))
        return True

    user32.EnumWindows(visit, 0)
    return found


def find_window(query: str, windows: list[Win] | None = None) -> Win | None:
    """按标题 / 程序名里包含 ``query``（不分大小写）找窗口，前台优先。隐私窗口找不到。"""
    query = (query or "").strip().lower()
    if not query:
        return None
    for win in windows if windows is not None else list_windows():
        if not win.private and (query in win.title.lower() or query in win.app.lower()):
            return win
    return None


def capture_window(hwnd: int):
    """``PrintWindow`` 截整个窗口（后台 / 被挡住也行）→ PIL Image；最小化、太小、失败返回 None。"""
    if sys.platform != "win32":
        return None
    from PIL import Image

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    if user32.IsIconic(hwnd):
        return None
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 50 or height < 50 or width * height > 40_000_000:
        return None
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, 2):          # PW_RENDERFULLCONTENT：浏览器 / Electron 也能截
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
            return None
        image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB")
        if image.getextrema() in (((0, 0), (0, 0), (0, 0)),):
            return None                                         # 全黑：硬件加速的窗口截不到
        return image
    finally:
        gdi32.SelectObject(memory_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def read_window(query: str, limit: int = 1500) -> tuple[Win | None, str]:
    """找窗口 → 截图 → 本地认字。返回 (窗口, 文字)。"""
    from .perception import ocr_image

    win = find_window(query)
    if win is None:
        return None, ""
    image = capture_window(win.hwnd)
    if image is None:
        return win, ""
    return win, ocr_image(image, limit=limit)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def process_count(name: str) -> int:
    """叫这个名字的进程有几个（``python`` 和 ``python.exe`` 都行，不分大小写）。"""
    if sys.platform != "win32":
        return 0
    name = (name or "").strip().lower()
    if not name:
        return 0
    if not name.endswith(".exe"):
        name += ".exe"
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)                   # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return 0
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    count = 0
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == name:
                count += 1
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return count


def file_status(path: str) -> dict:
    """只看在不在、多大、什么时候改的，不读内容。"""
    try:
        target = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
        stat = target.stat()
    except (OSError, ValueError):
        return {"exists": False}
    return {"exists": True, "size": stat.st_size, "mtime": round(stat.st_mtime, 1),
            "modified_ago_s": int(time.time() - stat.st_mtime), "folder": target.is_dir()}
