"""系统状态：前台是不是全屏程序、CPU 忙不忙、电量。全走 ctypes，不加依赖，纯本地。

* 全屏：前台窗口铺满它所在的显示器（排除桌面本身 Progman/WorkerW 和我们自己）。
  游戏、全屏视频、幻灯片放映都是这个形状。
* CPU：``GetSystemTimes`` 两次采样之差；
* 电量：``GetSystemPowerStatus``（台式机没有电池 → None）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd"}


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


class _POWER(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def foreground_is_fullscreen(own_pid: int | None = None) -> bool:
    """两路判据取"或"：

    1. ``SHQueryUserNotificationState``——Windows 自己用来决定"现在别弹通知"的那个状态：
       3 = D3D 独占全屏，4 = 演示模式。**不认 2（QUNS_BUSY）**：我们自己这个置顶、铺满
       工作区的透明窗口一显示，Windows 就报 2（本机实测：前台不是我们，状态照样是 2），
       认它的话桌宠一出来就把自己藏掉——用户看到的就是"显示几秒就消失"；
    2. 前台窗口（不是我们自己）铺满显示器**且不是普通的最大化**——无边框全屏的游戏和
       浏览器全屏视频走这条（判据见函数末尾）。
    """
    if sys.platform != "win32":
        return False
    try:
        state = ctypes.c_int()
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            if state.value in (3, 4):
                return True
    except Exception:
        pass
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        # 浏览器全屏时前台常常是它的小浮窗（"按 F11/Esc 退出全屏"提示，本机实测 344×166），
        # 全屏的是浮窗的所有者——两个都看。
        owner = user32.GetAncestor(hwnd, 3)                   # GA_ROOTOWNER
        return any(_window_is_fullscreen(user32, h, own_pid)
                   for h in dict.fromkeys((hwnd, owner)) if h)
    except Exception:
        return False


def _window_is_fullscreen(user32, hwnd, own_pid) -> bool:
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == (own_pid if own_pid is not None else os.getpid()):
            return False
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in _SHELL_CLASSES:
            return False
        rect = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        monitor = user32.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        mon = info.rcMonitor
        covers = (rect.left <= mon.left and rect.top <= mon.top
                  and rect.right >= mon.right and rect.bottom >= mon.bottom)
        if not covers:
            return False
        # 最大化 ≠ 全屏：最大化的窗口四边会**超出**显示器一圈边框（本机 -7 px），全屏的
        # 正好贴齐。任务栏自动隐藏时工作区 = 整块显示器，只看"盖满"会把最大化的
        # 编辑器/浏览器当成全屏；而只看 IsZoomed 又会漏掉"最大化后再按 F 全屏"的视频。
        exact = (abs(rect.left - mon.left) <= 1 and abs(rect.top - mon.top) <= 1
                 and abs(rect.right - mon.right) <= 1 and abs(rect.bottom - mon.bottom) <= 1)
        return exact or not user32.IsZoomed(hwnd)
    except Exception:
        return False


class CpuMeter:
    """``GetSystemTimes`` 差分：两次 ``sample()`` 之间的平均占用 0..1。"""

    def __init__(self) -> None:
        self._last: tuple[int, int, int] | None = None
        self.value = 0.0

    @staticmethod
    def _times() -> tuple[int, int, int] | None:
        if sys.platform != "win32":
            return None
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                     ctypes.byref(user)):
            return None

        def as_int(ft) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        return as_int(idle), as_int(kernel), as_int(user)

    def sample(self) -> float:
        now = self._times()
        if now is None:
            return self.value
        if self._last is not None:
            idle = now[0] - self._last[0]
            total = (now[1] - self._last[1]) + (now[2] - self._last[2])  # kernel 含 idle
            if total > 0:
                busy = max(0.0, min(1.0, 1.0 - idle / total))
                self.value = 0.6 * self.value + 0.4 * busy             # 平滑一下，别一闪一闪
        self._last = now
        return self.value


def battery() -> tuple[int, bool] | None:
    """(电量百分比, 是否在充电)；没有电池返回 None。"""
    if sys.platform != "win32":
        return None
    status = _POWER()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    flag = status.BatteryFlag & 0xFF
    percent = status.BatteryLifePercent & 0xFF
    if flag == 128 or percent == 255:                                 # 无电池 / 未知
        return None
    return percent, status.ACLineStatus == 1
