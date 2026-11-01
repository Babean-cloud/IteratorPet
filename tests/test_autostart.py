"""开机自启：只写当前用户的 Run 键；关掉时删值。不碰真注册表（假 winreg）。"""

from __future__ import annotations

from iterator_pet.platform import autostart


class FakeReg:
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = object(), 2, 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, *a):  # noqa: N802
        assert root is self.HKEY_CURRENT_USER and path == autostart.RUN_KEY
        reg = self

        class Key:
            def __enter__(self):
                return reg

            def __exit__(self, *exc):
                return False
        return Key()

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], 1

    def SetValueEx(self, key, name, _r, _t, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError
        del self.values[name]


def test_toggle(monkeypatch):
    reg = FakeReg()
    monkeypatch.setattr(autostart, "_winreg", lambda: reg)
    assert not autostart.enabled()
    assert autostart.set_enabled(True) and "iterator_pet.launcher" in reg.values["IteratorPet"]
    assert not autostart.set_enabled(False) and "IteratorPet" not in reg.values
    assert not autostart.set_enabled(False), "已经关着再关不报错"


def test_frozen_registers_exe(monkeypatch):
    monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
    monkeypatch.setattr(autostart.sys, "executable", r"C:\Games\迭代器\启动.exe")
    assert autostart.command() == '"C:\Games\迭代器\启动.exe"'
