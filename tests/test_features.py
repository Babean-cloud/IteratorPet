"""第三轮功能：委托任务（假 dsh）、对话记录、像素风布局、分层合成、任务结果进导演。

不联网、不调真 dsh：任务用一个假的"dsh"（python 脚本）按 ``--json`` 的格式吐事件，
包括真 dsh 的那个毛病——发完 ``final`` 不退出。
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from iterator_pet.brain.chatlog import ChatLog
from iterator_pet.brain.tasks import TaskRunner

FAKE_DSH = r"""
import json, os, sys, time
task = sys.stdin.read()
print(json.dumps({"type": "session", "sessionId": "session-fake", "cwd": os.getcwd()}), flush=True)
print(json.dumps({"type": "tool_call", "tool": "fs.list"}), flush=True)
mode = os.environ.get("DSH_PERMISSION_MODE")
key = "yes" if os.environ.get("DEEPSEEK_API_KEY") else "no"
if "hang" in task:
    time.sleep(60)
print(json.dumps({"type": "final", "text": f"done:{task}|{mode}|key={key}"}), flush=True)
time.sleep(60)   # 真 dsh：stdin 是管道时发完 final 不退
"""


def _runner(tmp_path, timeout=30.0, key="sk-x"):
    script = tmp_path / "fake_dsh.py"
    script.write_text(FAKE_DSH, encoding="utf-8")
    return TaskRunner(None, api_key_fn=lambda: key, timeout=timeout,
                      command_fn=lambda task: [sys.executable, str(script)])


def _wait(task, limit=20.0):
    start = time.time()
    while not task.finished and time.time() - start < limit:
        time.sleep(0.05)


def test_task_runs_in_workspace_with_mode_and_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    runner = _runner(tmp_path)
    work = tmp_path / "ws"
    task = runner.start("列一下文件", "read-only", str(work))
    assert runner.busy and work.is_dir()
    _wait(task)
    assert task.state == "done", task.error
    assert task.result == "done:列一下文件|read-only|key=yes"
    assert task.session_id == "session-fake"
    assert any("fs.list" in p for p in task.progress)
    assert not runner.busy


def test_task_rejects_full_access_and_parallel_runs(tmp_path):
    runner = _runner(tmp_path)
    with pytest.raises(ValueError):
        runner.start("x", "danger-full-access", str(tmp_path))
    task = runner.start("hang", "workspace-write", str(tmp_path))
    with pytest.raises(RuntimeError):
        runner.start("second", "read-only", str(tmp_path))
    runner.cancel()
    _wait(task)
    assert task.state == "cancelled"


def test_task_timeout_kills_it(tmp_path):
    runner = _runner(tmp_path, timeout=1.0)
    task = runner.start("hang", "read-only", str(tmp_path))
    _wait(task, 15.0)
    assert task.state == "timeout"


def test_parse_hotkey():
    from iterator_pet.platform.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, parse_hotkey

    assert parse_hotkey("Ctrl+Alt+I") == (MOD_CONTROL | MOD_ALT, ord("I"))
    assert parse_hotkey("ctrl + shift + f9") == (MOD_CONTROL | MOD_SHIFT, 0x78)
    assert parse_hotkey("Alt+Space") == (MOD_ALT, 0x20)
    assert parse_hotkey("I") is None, "必须带修饰键"
    assert parse_hotkey("Ctrl+Banana") is None


def test_chatlog_persists_and_versions(tmp_path):
    log = ChatLog(tmp_path / "chat.jsonl", now=lambda: 1000.0)
    log.add("user", "你好")
    log.add("pet", "又是你。")
    assert log.version == 2
    again = ChatLog(tmp_path / "chat.jsonl")
    assert [e["who"] for e in again.entries] == ["user", "pet"]
    assert json.loads((tmp_path / "chat.jsonl").read_text(encoding="utf-8").splitlines()[1])["text"] == "又是你。"


def test_task_result_reaches_director_once(tmp_path):
    from datetime import datetime

    from iterator_pet.brain.director import Director
    from iterator_pet.simulation.state import SimulationState

    class Client:
        calls = []

        def complete(self, messages, **_):
            self.calls.append(messages)
            return json.dumps({"say": "算完了。"}), {}

    sim = SimulationState((0.0, 0.0, 776.0, 415.0))
    sim.pool_lines = False
    clock = {"t": 1000.0}
    director = Director(Client(), clock=lambda: clock["t"], idle_fn=lambda: 0.0,
                        wall=lambda: datetime(2026, 10, 30, 14, 0))
    director.task_finished("找到 12 个文件", ok=True)
    clock["t"] += 10
    director.tick(sim)
    deadline = time.time() + 2
    while director.busy and time.time() < deadline:
        time.sleep(0.01)
    director.tick(sim)
    first = json.loads(Client.calls[0][-1]["content"])
    assert first["task_result"] == "完成：找到 12 个文件"
    assert sim.dialogue.text == "算完了。", "任务汇报不受话量闸门限制"
    assert "task_result" not in director.observe(sim, "idle"), "只附加一次"


def test_pixel_layout_and_compositor():
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    from iterator_pet.rendering.canvas import PetCanvas
    from iterator_pet.rendering.compose import Compositor
    from iterator_pet.simulation.state import SimulationState

    app = QApplication.instance() or QApplication([])  # noqa: F841
    sim = SimulationState((0.0, 0.0, 400.0, 300.0), warmup=40)
    canvas = PetCanvas(sim, scale=1.5)
    assert canvas.pixel_layout() is None, "默认关"
    canvas.pixel_art = True
    ratio = canvas.devicePixelRatioF() or 1.0
    assert canvas.pixel_layout() == (1.0, 1.5 * ratio)
    canvas.pixel_snap = True
    low, block = canvas.pixel_layout()
    assert block == round(1.5 * ratio) and abs(low * block - 1.5 * ratio) < 1e-9
    canvas.scale = 0.6
    if 0.6 * ratio < 1.25:
        assert canvas.pixel_layout() is None, "便携档不做像素风"

    state = sim.interpolate(1.0)
    plain = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
    shaded = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
    for image, shadow in ((plain, False), (shaded, True)):
        image.fill(0)
        Compositor().render(image, state, 1.0, antialias=False, shadow=shadow)

    def opaque(image):
        return sum(1 for y in range(0, 300, 3) for x in range(0, 400, 3)
                   if image.pixelColor(x, y).alpha() > 0)

    assert opaque(shaded) > opaque(plain), "落影和柔光让更多像素有颜色"
