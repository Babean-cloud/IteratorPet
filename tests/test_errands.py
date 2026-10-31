"""心事：模型自己规划怎么盯（工具循环），本地便宜地盯梢，触发了再叫醒它判断。"""

from __future__ import annotations

import json
import time

from iterator_pet.brain.director import Director
from iterator_pet.brain.errands import Errands, Watcher, normalize_trigger
from iterator_pet.brain.sensors import Win
from iterator_pet.simulation.state import SimulationState

WIDE = (0.0, 0.0, 1138.0, 608.0)


class FakeSensors:
    """假的眼睛：窗口、进程、文件、窗口里的字都能随时改。"""

    def __init__(self):
        self.windows = [Win(1, 10, "WindowsTerminal.exe", "pytest - 终端", False, False),
                        Win(2, 11, "chrome.exe", "文档 - Chrome", False, False)]
        self.processes = {"python.exe": 2}
        self.files = {}
        self.text = {}

    def list_windows(self, blacklist=None):
        return list(self.windows)

    def find_window(self, query):
        return next((w for w in self.windows if query.lower() in (w.title + w.app).lower()), None)

    def read_window(self, query):
        win = self.find_window(query)
        return win, (self.text.get(win.hwnd, "") if win else "")

    def capture_window(self, hwnd):
        return None

    def process_count(self, name):
        return self.processes.get(name.lower() if name.lower().endswith(".exe") else name.lower() + ".exe", 0)

    def file_status(self, path):
        return self.files.get(path, {"exists": False})

    def retitle(self, hwnd, title):
        self.windows = [Win(w.hwnd, w.pid, w.app, title if w.hwnd == hwnd else w.title, w.private, w.minimized)
                        for w in self.windows]


def _call(name, **args):
    return {"content": "", "tool_calls": [{"id": f"c{name}", "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


class ScriptClient:
    """日常那条请求回固定 JSON；办心事的工具循环按脚本一步步回。"""

    def __init__(self, reply, script):
        self.reply, self.script = reply, list(script)
        self.tool_calls = []

    def complete(self, messages, **_):
        return json.dumps(self.reply, ensure_ascii=False), {}

    def complete_tools(self, messages, tools, **_):
        self.tool_calls.append(messages)
        return self.script.pop(0), {"total_tokens": 10}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _wait_errand(director, sim, clock):
    deadline = time.time() + 2
    while (director.busy or director._errand_busy) and time.time() < deadline:
        time.sleep(0.005)
    clock.now += 2.5
    director.tick(sim)


def test_errand_plans_with_tools_then_fires_on_local_trigger(tmp_path):
    clock = Clock()
    eyes = FakeSensors()
    errands = Errands(tmp_path / "e.json")
    client = ScriptClient(
        {"say": "……行，我看着。", "errand": "等终端里的测试跑完，告诉他结果"},
        [_call("list_windows"),
         _call("read_window", window="终端"),
         _call("wait", notes="测试在 Windows Terminal 里跑，标题会变；跑完会出现 passed / failed",
               triggers=[{"type": "title_change", "window": "终端"},
                         {"type": "text_appears", "window": "终端", "words": ["passed", "failed"]},
                         {"type": "bogus"}],
               check_in_min=10, say="标题一变我就知道。"),
         _call("read_window", window="终端"),
         _call("done", say="……测试跑完了，179 个全过。")])
    director = Director(client, clock=clock, idle_fn=lambda: 0.0, errands=errands, sensors=eyes)
    sim = SimulationState(WIDE)
    sim.pool_lines = False
    director.say_to("看到你跑完后提醒我一下")
    director.tick(sim)
    _wait_errand(director, sim, clock)           # 日常回复回来 → 记下心事
    assert errands.items[0]["goal"] == "等终端里的测试跑完，告诉他结果"
    _wait_errand(director, sim, clock)           # 办事的那一小段循环
    _wait_errand(director, sim, clock)
    errand = errands.items[0]
    assert errand["status"] == "waiting" and "Windows Terminal" in errand["notes"]
    assert [t["type"] for t in errand["triggers"]] == ["title_change", "text_appears"], "不认识的触发器丢掉"
    assert sim.dialogue.text == "标题一变我就知道。"
    first = json.loads(client.tool_calls[0][1]["content"])
    assert first["woken_by"] == "刚答应" and first["goal"].startswith("等终端")
    assert Errands(tmp_path / "e.json").items[0]["notes"], "存盘了，重启不丢"

    sim.cancel_dialogue()
    for _ in range(3):                            # 标题没变：不叫醒模型，不花 token
        clock.now += 2.5
        director.tick(sim)
    assert len(client.tool_calls) == 3
    eyes.retitle(1, "✓ 179 passed - 终端")
    clock.now += 2.5
    director.tick(sim)
    _wait_errand(director, sim, clock)
    woken = json.loads(client.tool_calls[3][1]["content"])
    assert "标题变了" in woken["woken_by"] and "Windows Terminal" in woken["your_notes"]
    assert sim.dialogue.text == "……测试跑完了，179 个全过。"
    assert not errands.items
    assert any("办完了" in e["text"] for e in director.chatlog.entries) if director.chatlog else True


def test_errand_due_and_final_turns_wait_into_give_up(tmp_path):
    clock = Clock()
    wall = {"t": 5000.0}
    errands = Errands(tmp_path / "e.json", now=lambda: wall["t"])
    errand = errands.add("等下载完")
    errands.plan(errand["id"], "看下载文件夹", [], check_in_min=3, deadline_min=10)
    client = ScriptClient({}, [_call("wait", notes="还在下", check_in_min=3),
                               _call("wait", notes="还在下", check_in_min=3)])
    director = Director(client, clock=clock, idle_fn=lambda: 0.0, errands=errands, sensors=FakeSensors())
    sim = SimulationState(WIDE)
    sim.pool_lines = False
    clock.now += 2.5
    director.tick(sim)
    assert not client.tool_calls, "复查时间没到"
    wall["t"] += 181
    clock.now += 2.5
    director.tick(sim)
    _wait_errand(director, sim, clock)
    assert json.loads(client.tool_calls[0][1]["content"])["woken_by"] == "到了你定的复查时间"
    assert errands.items and errands.items[0]["status"] == "waiting"
    assert "到了你定的复查时间" in errands.items[0]["history"][-1]
    wall["t"] += 600                              # 过了时限：最后一次，还说接着等也算放弃
    clock.now += 2.5
    director.tick(sim)
    _wait_errand(director, sim, clock)
    info = json.loads(client.tool_calls[1][1]["content"])
    assert info["final"] and info["woken_by"] == "已经过了时限"
    assert not errands.items and sim.dialogue.text == "……等不到了：等下载完"


def test_watcher_triggers():
    eyes = FakeSensors()
    eyes.files["D:/a.zip"] = {"exists": True, "size": 10, "mtime": 1.0}
    errand = {"id": "e1", "status": "waiting", "triggers": [
        normalize_trigger({"type": "window_gone", "window": "chrome"}),
        normalize_trigger({"type": "window_appears", "window": "安装完成"}),
        normalize_trigger({"type": "process_exit", "name": "python"}),
        normalize_trigger({"type": "file_change", "path": "D:/a.zip"})]}
    watcher = Watcher(eyes)
    assert watcher.poll([errand]) == [], "第一次只取基准"
    assert watcher.poll([errand]) == []
    eyes.processes["python.exe"] = 1
    (_, why), = watcher.poll([errand])
    assert "python 退出了（2 → 1 个）" in why
    assert watcher.poll([errand]) == [], "基准跟着更新，不会一直响"
    eyes.files["D:/a.zip"] = {"exists": True, "size": 99, "mtime": 2.0}
    assert "10 字节 → 99 字节" in watcher.poll([errand])[0][1]
    eyes.windows.append(Win(3, 12, "setup.exe", "安装完成", False, False))
    assert "出现了新窗口" in watcher.poll([errand])[0][1]
    eyes.windows = [w for w in eyes.windows if w.app != "chrome.exe"]
    assert "不见了" in watcher.poll([errand])[0][1]


def test_text_trigger_ignores_words_already_on_screen():
    eyes = FakeSensors()
    errand = {"id": "e1", "status": "waiting",
              "triggers": [normalize_trigger({"type": "text_appears", "window": "终端", "words": ["passed", "failed"]})]}
    watcher = Watcher(eyes)
    key = ("e1", 0, json.dumps(errand["triggers"][0], ensure_ascii=False, sort_keys=True))
    watcher._text[key] = "上一轮 12 passed"          # 后台认字的结果
    assert watcher.poll([errand]) == []
    watcher._text[key] = "上一轮 12 passed / 运行中"
    assert watcher.poll([errand]) == []
    watcher._text[key] = "1 failed, 178 passed"
    assert "failed" in watcher.poll([errand])[0][1]


def test_errands_survive_restart_and_resume(tmp_path):
    errands = Errands(tmp_path / "e.json", now=lambda: 100.0)
    errand = errands.add("等编译")
    assert errands.add("等编译") is None, "同一件事不重复"
    assert errand["status"] == "planning"
    again = Errands(tmp_path / "e.json", now=lambda: 200.0)
    assert again.items[0]["status"] == "waiting" and again.due()[0][1] == "到了你定的复查时间", \
        "退出时还没想好的：开起来马上再看一眼"
    assert "等编译" in again.describe()[0]
    assert again.cancel_all() == 1 and not Errands(tmp_path / "e.json").items


def test_errand_knows_how_long_the_screen_has_not_changed():
    from iterator_pet.brain import errand_agent

    eyes = FakeSensors()
    eyes.text[1] = "Claude 正在处理…"
    client = ScriptClient({}, [_call("read_window", window="终端"), _call("wait", notes="等", check_in_min=5)] * 2)
    first, _, _ = errand_agent.run(client, "pebbles", {}, sensors=eyes)
    second, _, _ = errand_agent.run(client, "pebbles", {}, sensors=eyes)
    assert first["_snapshot"] == second["_snapshot"], "读到的一样，指纹一样"
    errand = {"goal": "等", "created": 0.0, "deadline": 99999.0, "unchanged_since": 1000.0}
    assert errand_agent.brief(errand, "复查", 1000.0 + 20 * 60, None, False)["screen_unchanged_min"] == 20


def test_errand_field_ignored_without_store():
    director = Director(ScriptClient({}, []), idle_fn=lambda: 0.0)
    sim = SimulationState(WIDE)
    director.apply(sim, {"say": "好", "errand": "等测试"}, "chat")
    assert director.errands is None
