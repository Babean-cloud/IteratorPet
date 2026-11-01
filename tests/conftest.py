"""测试别往用户真实的 %LOCALAPPDATA%\\IteratorPet 里写东西（launcher.log 里曾混进测试的 "boom" / "断网"）。

另外：``SimulationState`` 默认要预热 320 tick 的机械臂 / 缆线物理（约 2 s），全量测试里几百次重复预热
占了大半时间。同样参数预热出来的状态完全一样（种子固定），所以第一次预热完存一份，之后深拷贝。
"""

import copy
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

# 整个测试进程只建一个 QApplication（它也是 QGuiApplication）：有的文件先建了 QGuiApplication，
# 后面要 QWidget 的测试就建不了窗口，只能跳过。并行跑时每个 worker 各一个。
_APP = QApplication.instance() or QApplication([])

from iterator_pet.simulation import state as _state

_warm: dict = {}
_original_init = _state.SimulationState.__init__


def _cached_init(self, bounds, inset=26.0, seed=1234, warmup=None, character="pebbles", start=None):
    if warmup is not None and warmup <= 0:
        return _original_init(self, bounds, inset, seed, warmup, character, start)
    key = (tuple(bounds), inset, seed, warmup, character, None if start is None else tuple(start))
    if key not in _warm:
        _original_init(self, bounds, inset, seed, warmup, character, start)
        _warm[key] = copy.deepcopy(self)
        return
    cached = _warm[key]
    # memo 把缓存对象映射到 self：子对象里回指 sim 的绑定方法（比如 wander.clamp）也改指新对象
    self.__dict__.update(copy.deepcopy(cached.__dict__, {id(cached): self}))


_state.SimulationState.__init__ = _cached_init


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.getbasetemp() / "appdata"))
