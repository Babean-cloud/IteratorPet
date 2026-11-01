"""开机自启：写 ``HKCU\Software\Microsoft\Windows\CurrentVersion\Run``（只影响当前用户，不要管理员）。

发布版登记 ``启动.exe`` 本身；从源码跑时登记 ``pythonw -m iterator_pet.launcher``（要求包已 ``pip install -e .``）。
启动器本来就不弹窗，所以开机自启就是静默启动。
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "IteratorPet"


def command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.is_file() else exe}" -m iterator_pet.launcher'


def _winreg():
    if sys.platform != "win32":
        return None
    import winreg

    return winreg


def enabled() -> bool:
    reg = _winreg()
    if reg is None:
        return False
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = reg.QueryValueEx(key, VALUE)
    except OSError:
        return False
    return bool(value)


def set_enabled(on: bool) -> bool:
    """开 / 关。返回操作后是否真的开着（写注册表失败时如实返回）。"""
    reg = _winreg()
    if reg is None:
        return False
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            if on:
                reg.SetValueEx(key, VALUE, 0, reg.REG_SZ, command())
            else:
                try:
                    reg.DeleteValue(key, VALUE)
                except FileNotFoundError:
                    pass
    except OSError:
        pass
    return enabled()
