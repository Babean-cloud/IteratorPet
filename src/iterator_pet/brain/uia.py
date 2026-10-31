"""只读当前前台窗口的 UI Automation 焦点控件；失败就交给 OCR。"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class FocusInfo:
    rect: tuple[int, int, int, int]
    text: str


_uia_disabled = False


def _pattern_text(element, api) -> str:
    """编辑区优先取 TextPattern，再取 Value；普通按钮取 Name。"""
    for pattern_id, interface in ((api.UIA_TextPatternId, api.IUIAutomationTextPattern),
                                  (api.UIA_ValuePatternId, api.IUIAutomationValuePattern)):
        try:
            pattern = element.GetCurrentPattern(pattern_id).QueryInterface(interface)
            value = (pattern.DocumentRange.GetText(1000) if pattern_id == api.UIA_TextPatternId
                     else pattern.CurrentValue)
            if value and value.strip():
                return value[:1000]
        except Exception:                         # noqa: BLE001
            pass
    return (element.CurrentName or "")[:160]


def _query_focus(hwnd: int, window_rect: tuple[int, int, int, int]) -> FocusInfo | None:
    # UIA 会向别的进程发 COM 请求，必须留在没有窗口的后台 MTA 线程。
    sys.coinit_flags = 0
    import comtypes.client
    import comtypes.gen.UIAutomationClient as api

    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, 0)
    if hr not in (0, 1):
        return None
    try:
        automation = comtypes.client.CreateObject(api.CUIAutomation, interface=api.IUIAutomation)
        focus = automation.GetFocusedElement()
        if not focus:
            return None
        if focus.CurrentIsPassword:
            return None
        # 焦点可能是没有 HWND 的内部控件，沿 UIA 父级找到前台顶层窗口。
        element = focus
        matched = False
        for _ in range(16):
            native = int(element.CurrentNativeWindowHandle or 0)
            if native and (native == hwnd or ctypes.windll.user32.GetAncestor(native, 2) == hwnd):
                matched = True
                break
            element = automation.ControlViewWalker.GetParentElement(element)
            if not element:
                break
        if not matched:
            return None
        bounds = focus.CurrentBoundingRectangle
        left, top, right, bottom = window_rect
        box = (max(left, bounds.left), max(top, bounds.top),
               min(right, bounds.right), min(bottom, bounds.bottom))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        return FocusInfo(box, _pattern_text(focus, api))
    finally:
        ole32.CoUninitialize()


def focus_snapshot(hwnd: int, window_rect: tuple[int, int, int, int]) -> FocusInfo | None:
    """最多等 1.5 秒；遇到卡住的 UIA 提供方，本进程后续直接走 OCR。"""
    global _uia_disabled
    if sys.platform != "win32" or _uia_disabled:
        return None
    result = [None]

    def work():
        try:
            result[0] = _query_focus(hwnd, window_rect)
        except Exception:                         # noqa: BLE001
            pass

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(1.5)
    if thread.is_alive():
        _uia_disabled = True
        return None
    return result[0]
