"""测试别往用户真实的 %LOCALAPPDATA%\\IteratorPet 里写东西（launcher.log 里曾混进测试的 "boom" / "断网"）。"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.getbasetemp() / "appdata"))
