import sys
from dataclasses import dataclass
from PySide6.QtCore import QRect, QPoint
from PySide6.QtGui import QGuiApplication, QScreen

# 任务栏自动隐藏时 availableGeometry == 整块显示器：一个置顶窗口正好盖满显示器，Windows 和 NVIDIA 覆盖层
# 都会把它当成"全屏程序"——任务栏碰底边不再弹出、按 Win 时 NVIDIA 弹 Alt+Z 提示（用户 2026-11-01 实测）。
# 在任务栏那条边上让出几像素，它就不再是全屏窗口。
FULLSCREEN_GAP = 2


@dataclass(frozen=True)
class WorkspaceSnapshot:
    geometry: QRect
    available: QRect
    dpr: float
    name: str


def taskbar_edge() -> str:
    """任务栏在哪条边（left / top / right / bottom）；查不到按底边。"""
    if sys.platform != "win32":
        return "bottom"
    try:
        import ctypes
        from ctypes import wintypes

        class APPBARDATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                        ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]

        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        if ctypes.windll.shell32.SHAppBarMessage(0x00000005, ctypes.byref(data)):    # ABM_GETTASKBARPOS
            return ("left", "top", "right", "bottom")[data.uEdge] if data.uEdge < 4 else "bottom"
    except (OSError, AttributeError, ValueError):
        pass
    return "bottom"


def pet_rect(snapshot: WorkspaceSnapshot, edge: str | None = None) -> QRect:
    """桌宠窗口的矩形：可用区域；如果它等于整块显示器（任务栏自动隐藏），在任务栏那边让出 FULLSCREEN_GAP。"""
    rect = QRect(snapshot.available)
    if rect != snapshot.geometry:
        return rect
    gap = FULLSCREEN_GAP
    edge = edge or taskbar_edge()
    if edge == "top":
        return rect.adjusted(0, gap, 0, 0)
    if edge == "left":
        return rect.adjusted(gap, 0, 0, 0)
    if edge == "right":
        return rect.adjusted(0, 0, -gap, 0)
    return rect.adjusted(0, 0, 0, -gap)


class ScreenWorkspace:
    def __init__(self, screen: QScreen | None = None):
        self.screen = screen or QGuiApplication.primaryScreen()

    def snapshot(self) -> WorkspaceSnapshot:
        s = self.screen or QGuiApplication.primaryScreen()
        return WorkspaceSnapshot(s.geometry(), s.availableGeometry(), s.devicePixelRatio(), s.name())

    def refresh_primary(self) -> WorkspaceSnapshot:
        self.screen = QGuiApplication.primaryScreen()
        return self.snapshot()
