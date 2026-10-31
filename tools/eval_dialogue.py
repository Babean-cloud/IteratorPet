"""对话离线评测：用真 Director 拼请求（和桌宠一模一样），发给真模型，记下回复和 token。

场景全是合成数据（``tools/eval_scenarios.json``），不碰真实记忆 / 对话记录 / 上下文。
API Key 由本脚本在进程内经 ``config.llm_api_key`` 读取，**不打印、不写进结果**。

用法（项目根）::

    $env:PYTHONPATH='src'
    .\\.venv\\Scripts\\python.exe tools/eval_dialogue.py --dry          # 不联网：只看每个场景的 prompt 大小
    .\\.venv\\Scripts\\python.exe tools/eval_dialogue.py                # 全部场景各跑一次
    .\\.venv\\Scripts\\python.exe tools/eval_dialogue.py -s err_popup -n 3   # 单个场景跑 3 次

结果追加到 ``tools/eval_runs/<时间>.jsonl``，一行一次（场景、回复、usage、prompt 字数）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from iterator_pet.brain.chatlog import ChatLog
from iterator_pet.brain.director import Director, parse_action
from iterator_pet.brain.memory import Memory
from iterator_pet.brain.perception import new_stats
from iterator_pet.simulation.state import SimulationState

HERE = Path(__file__).resolve().parent
SCENARIOS = HERE / "eval_scenarios.json"
RUNS = HERE / "eval_runs"
BOUNDS = (0.0, 0.0, 1138.0, 608.0)


class Eyes:
    """假感知：只给 observe() 用到的几样。"""

    def __init__(self, screen: dict | None):
        self.level = "ocr" if screen else "title"
        self.screen = screen or {}
        self.stats = new_stats()
        self.window = SimpleNamespace(hwnd=1, title=self.screen.get("title", ""), private=False)

    def describe(self):
        if not self.screen:
            return None
        return {"app": self.screen.get("app", ""), "title": self.screen.get("title", ""),
                "minutes": self.screen.get("minutes", 3)}

    def screen_delta(self, full: bool = False):
        text = self.screen.get("text")
        return {"screen_text": text} if text else {}


class Relay:
    """记下 Director 发出的 messages；有真客户端就转发，没有就回一个空动作。"""

    def __init__(self, real=None):
        self.real = real
        self.calls: list[dict] = []

    def complete(self, messages, **kwargs):
        entry = {"messages": messages, "kwargs": {k: v for k, v in kwargs.items() if k != "model"}}
        self.calls.append(entry)
        if self.real is None:
            return "{}", {}
        t0 = time.time()
        text, usage = self.real.complete(messages, **kwargs)
        entry["seconds"] = round(time.time() - t0, 2)
        return text, usage


def real_client():
    from iterator_pet.brain.client import ChatClient
    from iterator_pet.config import llm_api_key, load

    cfg = load()
    key = llm_api_key(cfg)
    if not key:
        sys.exit("没有配置 API Key（桌宠里设置过才能跑真模型）")
    return ChatClient(key, cfg.llm_base_url, cfg.llm_model, timeout=60.0)


def run_one(sc: dict, client, tmp: Path) -> dict:
    hour, minute = sc.get("time", [14, 0])
    now = {"t": 10_000.0}
    memory = Memory(tmp / "memory.json")
    memory.profile = list(sc.get("profile", []))
    memory.affinity = float(sc.get("affinity", 0.2))
    memory.diary = [{"date": "2026-10-31", "text": d} for d in sc.get("diary", [])]
    chatlog = ChatLog(tmp / "chat.jsonl", now=lambda: now["t"])
    for who, text in sc.get("history", []):
        chatlog.add(who, text)
        now["t"] += 30.0
    filler = sc.get("filler") or {}
    for i in range(int(filler.get("history", 0))):         # 合成的闲聊，撑到真实长度
        chatlog.add("user" if i % 2 == 0 else "pet", f"（合成对话第{i}句）" + "闲聊内容" * 3)
        now["t"] += 30.0
    relay = Relay(client)
    director = Director(relay, clock=lambda: now["t"], idle_fn=lambda: float(sc.get("idle_s", 5)),
                        wall=lambda: datetime(2026, 11, 1, hour, minute),
                        memory=memory, chatlog=chatlog, perception=Eyes(sc.get("screen")))
    context = director.context
    for i in range(int(filler.get("turns", 0))):           # 最近 N 轮原始请求/回复
        context.add_turn(json.dumps({"event": "屏幕有变化", "time": "15:%02d" % (i * 5 % 60), "relation": "认得"},
                                    ensure_ascii=False),
                         json.dumps({"thought": f"（合成念头{i}）", "say": None, "mood": "平静"}, ensure_ascii=False))
    if sc.get("last_spoke_min") is not None:
        director._last_auto_say = now["t"] - 60.0 * sc["last_spoke_min"]
    sim = SimulationState(BOUNDS, warmup=0, character=sc.get("character", "pebbles"))
    sim.pool_lines = False
    user_text = sc.get("user")
    if user_text:
        director.say_to(user_text)
        director._pending_chat.clear()
    director._start(sim, sc.get("reason", "chat" if user_text else "idle"), user_text)
    deadline = time.time() + 90
    while director.busy and time.time() < deadline:
        time.sleep(0.05)
    call = relay.calls[-1] if relay.calls else {"messages": []}
    raw = None
    usage: dict = {}
    while not director._results.empty():
        item = director._results.get()
        if item[3]:
            raw = {"error": item[3]}
        else:
            raw, usage = item[1], item[2] or {}
    sizes = [len(m["content"]) if isinstance(m["content"], str) else 0 for m in call["messages"]]
    return {"id": sc["id"], "reply": raw, "usage": usage, "seconds": call.get("seconds"),
            "prompt_chars": sizes, "expect": sc.get("expect", "")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="不联网，只看 prompt 大小")
    ap.add_argument("-s", "--scenario", action="append", help="只跑这些场景 id")
    ap.add_argument("-n", "--repeat", type=int, default=1)
    args = ap.parse_args()

    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] in args.scenario]
    client = None if args.dry else real_client()
    out = None
    if not args.dry:
        RUNS.mkdir(exist_ok=True)
        out = RUNS / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    totals = {"prompt": 0, "hit": 0, "completion": 0}
    for sc in scenarios:
        for _ in range(args.repeat):
            with tempfile.TemporaryDirectory() as tmp:
                res = run_one(sc, client, Path(tmp))
            u = res["usage"]
            totals["prompt"] += u.get("prompt_tokens", 0)
            totals["hit"] += u.get("prompt_cache_hit_tokens", 0)
            totals["completion"] += u.get("completion_tokens", 0)
            reply = res["reply"] or {}
            print(f"== {res['id']}  prompt字数 {sum(res['prompt_chars'])} {res['prompt_chars']}"
                  f"  token {u.get('prompt_tokens', '-')}/{u.get('completion_tokens', '-')}"
                  f"  命中 {u.get('prompt_cache_hit_tokens', '-')}  {res['seconds'] or ''}s")
            if not args.dry:
                for key in ("say", "thought", "mood", "plan", "remember", "remind", "errand", "task", "error"):
                    if reply.get(key) not in (None, "", []):
                        print(f"   {key}: {json.dumps(reply[key], ensure_ascii=False)[:200]}")
                print(f"   期望: {res['expect']}")
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(res, ensure_ascii=False) + "\n")
    if not args.dry:
        print(f"\n合计 prompt {totals['prompt']}（命中 {totals['hit']}），输出 {totals['completion']}；结果：{out}")


if __name__ == "__main__":
    main()
