"""LLM 导演层：不联网、不截屏，用假 client / 假时钟验证"意图 → 仿真"这条路。

* 模型输出解析容错；``direct`` 的目的地照样被夹进腔室和臂展；
* 地盘：park 之后人偶待在带子里、滑车锁到那条边；
* 日程在本地一步步执行（go / park / say cursor_near）；
* 显著度 / 表达欲 / 离开 / 每日额度 决定什么时候请求；
* 记忆、用量记账、OCR 文本清洗。
"""

from __future__ import annotations

import pytest
import json
import time
from datetime import date, datetime

from iterator_pet.brain.director import AUTO_GAP, ERROR_BACKOFF, Director, parse_action
from iterator_pet.brain.memory import PROFILE_SOFT_CAP, Memory
from iterator_pet.brain.perception import clean_ocr
from iterator_pet.brain.planner import Planner
from iterator_pet.brain.usage import UsageTracker
from iterator_pet.platform.fixed_loop import GAME_DT
from iterator_pet.simulation import constants as C
from iterator_pet.simulation.state import SimulationState

BOUNDS = (0.0, 0.0, 776.0, 415.0)
WIDE = (0.0, 0.0, 1138.0, 608.0)


class FakeClient:
    def __init__(self, reply: dict | str | Exception, usage: dict | None = None):
        self.reply = reply
        self.usage = usage or {"prompt_tokens": 1000, "completion_tokens": 80,
                               "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}
        self.calls: list[list[dict]] = []

    def complete(self, messages, **_kwargs):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)
        return text, self.usage


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _sim(bounds=BOUNDS) -> SimulationState:
    sim = SimulationState(bounds)
    sim.pool_lines = False
    return sim


def _director(tmp_path, reply, clock=None, idle=lambda: 0.0, **kwargs) -> Director:
    return Director(FakeClient(reply), clock=clock or Clock(), idle_fn=idle,
                    wall=lambda: datetime(2026, 10, 30, 14, 0),
                    memory=Memory(tmp_path / "memory.json"),
                    usage=UsageTracker(tmp_path / "usage.json"), **kwargs)


def _settle(director: Director, sim: SimulationState, timeout: float = 2.0) -> None:
    """等工作线程回来，再 tick 一次把结果落地。"""
    deadline = time.time() + timeout
    while director.busy and time.time() < deadline:
        time.sleep(0.005)
    director.tick(sim)


# -- 解析 --------------------------------------------------------------------
def test_parse_action_tolerates_wrapping():
    assert parse_action('{"say": "嗯"}') == {"say": "嗯"}
    assert parse_action('```json\n{"say": "嗯"}\n```') == {"say": "嗯"}
    assert parse_action("好的：{\"plan\": []} 完毕") == {"plan": []}
    assert parse_action("not json") == {}
    assert parse_action("[1, 2]") == {}


# -- 仿真接口 ------------------------------------------------------------------
def test_direct_reaches_a_clamped_goal_and_stays_inside_the_room():
    sim = _sim()
    goal = (sim.bounds[2] * 0.8, sim.bounds[3] * 0.3)
    assert sim.direct(goal, look=None, ticks=900)
    clamped = sim.wander.next
    x, y, w, h = sim.bounds
    for _ in range(800):
        sim.step(GAME_DT)
        bx, by = sim.creature.chunk0
        assert x - 1 <= bx <= x + w + 1 and y - 1 <= by <= y + h + 1
    # 横向到位；纵向会"挂"在目的地下方（原版漫游实测静止时也低 20–140 px）
    bx, by = sim.creature.chunk0
    assert abs(bx - clamped[0]) < 40.0
    assert -20.0 < by - clamped[1] < 150.0
    assert sim.wander.next == clamped, "指令期间漫游层不许自己换目的地"


def test_events_go_to_director_instead_of_pool():
    sim = _sim()
    for _ in range(5):
        sim.step(GAME_DT)
    assert sim.begin_drag("body", sim.creature.chunk0)
    sim.drag_to(sim.creature.chunk0)
    sim.step(GAME_DT)
    assert "drag" in sim.pending_events
    assert not sim.dialogue.text, "导演模式下不许再抽台词池"


def test_grabbing_cancels_directive():
    sim = _sim()
    sim.direct((100.0, 100.0), ticks=400)
    assert sim.begin_drag("body", sim.creature.chunk0)
    assert not sim.directive_active
    assert not sim.direct((200.0, 200.0)), "拖拽中不接指令"


@pytest.mark.slow
def test_territory_parks_puppet_and_carriage_on_that_side():
    for side in ("left", "right"):
        sim = SimulationState(WIDE)
        sim.set_territory(side)
        for _ in range(int(25 * C.GAME_HZ)):
            sim.step(GAME_DT)
        q, f0, f1 = sim._territory_frame_range()
        for _ in range(int(20 * C.GAME_HZ)):
            sim.step(GAME_DT)
            assert sim._in_territory(sim.creature.chunk0), side
        assert f0 - 0.03 <= sim.frame_pos <= f1 + 0.03, (side, sim.frame_pos, f0, f1)
        base_x = sim.rail.pose(sim.frame_pos).point[0]
        assert (base_x < 100) if side == "left" else (base_x > WIDE[2] - 100)
    sim.set_territory(None)
    assert sim.territory is None and sim.wander.region is None


# -- 日程 --------------------------------------------------------------------
def test_planner_runs_steps_locally():
    sim = _sim(WIDE)
    clock = Clock()
    parked = []
    planner = Planner(clock, on_territory=parked.append)
    planner.load([
        {"do": "park", "side": "left"},
        {"do": "say", "text": "到了。"},
        {"do": "say", "text": "你来啦", "when": "cursor_near"},
        {"do": "wait", "s": 5},
    ])
    planner.tick(sim)                         # park：瞬时
    assert sim.territory["side"] == "left" and parked == ["left"]
    planner.tick(sim)                         # say：瞬时
    assert sim.dialogue.text == "到了。"
    planner.tick(sim)                         # say cursor_near：开始等
    sim.cursor = None
    clock.now += 5
    planner.tick(sim)
    assert planner.current["do"] == "say", "光标不在附近就继续等"
    sim.cursor = sim.creature.chunk0
    planner.tick(sim)
    assert sim.dialogue.text == "你来啦"
    planner.tick(sim)                         # wait
    clock.now += 6
    planner.tick(sim)
    assert planner.idle


def test_planner_go_step_moves_and_finishes():
    sim = _sim()
    clock = Clock()
    planner = Planner(clock)
    planner.load([{"do": "go", "x": 0.3, "y": 0.4}])
    planner.tick(sim)
    assert sim.directive_active
    for _ in range(int(25 * C.GAME_HZ)):
        sim.step(GAME_DT)
        clock.now += GAME_DT
        planner.tick(sim)
        if planner.idle:
            break
    assert planner.idle


# -- 导演 --------------------------------------------------------------------
def test_chat_reply_applies_say_plan_memory_and_usage(tmp_path):
    sim = _sim()
    reply = {"say": "又是你。", "mood": "烦躁", "plan": [{"do": "meditate", "s": 30}],
             "remember": "这个人叫小林", "affinity": 0.05}
    director = _director(tmp_path, reply)
    director.say_to("我叫小林")
    director.tick(sim)
    _settle(director, sim)
    assert sim.dialogue.text == "又是你。"
    assert director.mood == "烦躁"
    assert director.memory.profile == ["这个人叫小林"]
    assert abs(director.memory.affinity - 0.05) < 1e-9
    assert director.usage.today_row()["calls"] == 1
    assert director.usage.today_row()["cache_hit"] == 900
    messages = director.client.calls[0]
    assert messages[0]["role"] == "system" and "五块卵石" in messages[0]["content"]
    assert messages[1]["role"] == "system" and "【你的记忆】" in messages[1]["content"]
    obs = json.loads(messages[-1]["content"])
    assert obs["user_said"] == "我叫小林"
    director.tick(sim)                        # 日程开始执行
    assert director.planner.current["do"] == "meditate"
    # 记忆写盘了
    assert "小林" in (tmp_path / "memory.json").read_text(encoding="utf-8")


def test_direct_chat_can_propose_work_without_a_second_model_call(tmp_path):
    sim = _sim()
    proposed = []
    director = _director(tmp_path, {
        "say": "我来看看。",
        "task": {"goal": "阅读日志并总结报错", "mode": "read-only", "workspace": None},
    }, on_task_request=lambda task, original: proposed.append((task, original)),
        task_workspace=str(tmp_path))
    director.say_to("读一下日志，找出报错原因")
    director.tick(sim)
    _settle(director, sim)
    assert len(director.client.calls) == 1
    assert proposed == [({"goal": "阅读日志并总结报错", "mode": "read-only",
                         "workspace": None}, "读一下日志，找出报错原因")]
    assert json.loads(director.client.calls[0][-1]["content"])["task_workspace"] == str(tmp_path)
    assert director.usage.today_by_source()["dialogue"]["calls"] == 1


def test_background_or_invalid_work_proposal_is_ignored(tmp_path):
    sim = _sim()
    proposed = []
    director = _director(tmp_path, {}, on_task_request=lambda *args: proposed.append(args))
    task = {"goal": "动电脑", "mode": "workspace-write"}
    director.apply(sim, {"task": task}, reason="idle")
    director.apply(sim, {"task": task}, reason="chat")
    director.apply(sim, {"task": {"goal": "动电脑", "mode": "unrestricted"}},
                   reason="chat", user_text="帮我动电脑")
    assert proposed == []


def test_history_drops_bulky_screen_text(tmp_path):
    sim = _sim()
    clock = Clock()
    director = _director(tmp_path, {"say": "嗯", "thought": "又来了"}, clock=clock)

    class Eyes:
        level = "ocr"

        def poll(self):
            return None, False

        def describe(self):
            return {"app": "Code", "title": "main.py", "minutes": 3}

        def ocr_text(self):
            return "很长的屏幕文字" * 50

    director.perception = Eyes()
    for text in ("一", "二"):
        director.say_to(text)
        clock.now += 10
        director.tick(sim)
        _settle(director, sim)
    second = director.client.calls[1]
    assert len(second) == 2 + 2 + 1
    past = json.loads(second[2]["content"])
    assert past["user_said"] == "一" and "screen_text" not in past, "历史不带大段屏幕文字"
    assert past["screen"]["app"] == "Code"
    assert "screen_text" in json.loads(second[-1]["content"]), "本轮观察带"
    assert "又来了" in second[3]["content"], "模型自己的 thought 留在历史里"


def test_talk_gate_limits_autonomous_lines_only(tmp_path):
    sim = _sim()
    clock = Clock()
    director = _director(tmp_path, {}, clock=clock, talkativeness=0.5)
    director.apply(sim, {"say": "自言自语一"}, reason="idle")
    assert sim.dialogue.text == "自言自语一"
    sim.cancel_dialogue()
    clock.now += 30
    director.apply(sim, {"say": "自言自语二"}, reason="window")
    assert not sim.dialogue.text, "话量闸门：自言自语之间至少隔一阵"
    director.apply(sim, {"say": "回你的话"}, reason="chat")
    assert sim.dialogue.text == "回你的话", "用户找它时不受限"
    sim.cancel_dialogue()
    clock.now += 200
    director.quiet = True
    director.apply(sim, {"say": "全屏时"}, reason="idle")
    assert not sim.dialogue.text, "勿扰时不自言自语"


def test_reflection_writes_diary_goal_and_facts(tmp_path):
    sim = _sim()
    clock = Clock()
    reply = {"diary": "今天被拖了两次。", "goal": "想让这个人早点睡",
             "remember": ["晚上写代码", "喜欢雨世界"], "mood": "疲惫"}
    director = _director(tmp_path, reply, clock=clock)
    director.planner.load([{"do": "wait", "s": 300}])
    for i in range(6):
        director.log(f"事件{i}")
    director.context.active_seconds = 1801     # 在场时间是累计的（单步封顶 10 s），直接给
    clock.now += AUTO_GAP + 1
    director.tick(sim)
    _settle(director, sim)
    obs = json.loads(director.client.calls[0][-1]["content"])
    assert "反思" in obs["event"] and len(obs["activity"]) == 6
    memory = director.memory
    assert memory.diary[-1]["text"] == "今天被拖了两次。"
    assert memory.goal == "想让这个人早点睡"
    assert memory.profile == ["晚上写代码", "喜欢雨世界"]
    assert "早点睡" in memory.prompt_block()


def test_reminders_fire_locally(tmp_path):
    from iterator_pet.brain.reminders import Reminders

    sim = _sim()
    clock = Clock()
    wall_now = {"t": 1_000_000.0}
    reminders = Reminders(tmp_path / "r.json", now=lambda: wall_now["t"],
                          wall=lambda: datetime(2026, 10, 30, 14, 0))
    director = _director(tmp_path, {"say": "好。", "remind": {"in_min": 30, "text": "该喝水了"}},
                         clock=clock, reminders=reminders)
    director.say_to("半小时后提醒我喝水")
    director.tick(sim)
    _settle(director, sim)
    assert reminders.items and reminders.items[0]["text"] == "该喝水了"
    assert Reminders(tmp_path / "r.json").items, "存盘了"
    sim.cancel_dialogue()
    wall_now["t"] += 31 * 60
    clock.now += 3
    director.tick(sim)
    assert sim.dialogue.text == "该喝水了" and not reminders.items
    assert reminders.due_from_spec({"at": "13:00"}) - wall_now["t"] == 23 * 3600


def test_config_upgrade_resets_conservative_defaults(tmp_path, monkeypatch):
    from iterator_pet import config as cfgmod

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = cfgmod.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"llm_daily_token_cap": 300000, "llm_plan_refresh_seconds": 180,
                                "llm_api_key": "k", "character": "past_moon"}), encoding="utf-8")
    cfg = cfgmod.load()
    assert cfg.llm_daily_token_cap == 10_000_000 and cfg.llm_plan_refresh_seconds == 75.0
    assert cfg.llm_api_key == "k" and cfg.character == "past_moon"
    cfg.llm_daily_token_cap = 500
    cfgmod.save(cfg)
    assert cfgmod.load().llm_daily_token_cap == 500, "升级后用户自己改的值要保留"


def test_low_salience_waits_urgent_fires(tmp_path):
    sim = _sim()
    clock = Clock()
    director = _director(tmp_path, {"say": None, "plan": [{"do": "wait", "s": 300}]}, clock=clock)
    director.planner.load([{"do": "wait", "s": 300}])
    clock.now += AUTO_GAP + 1
    director.bump("window")
    director.tick(sim)
    assert not director.client.calls, "换个窗口不值得打断日程"
    sim.pending_events.append("drag")
    director.tick(sim)
    _settle(director, sim)
    assert len(director.client.calls) == 1
    assert "拖" in json.loads(director.client.calls[0][-1]["content"])["event"]


def test_plan_done_and_desire_wake_it_up(tmp_path):
    sim = _sim()
    clock = Clock()
    director = _director(tmp_path, {"say": None, "plan": None}, clock=clock)
    clock.now += AUTO_GAP + 1
    director.tick(sim)
    assert not director.client.calls, "刚开始没什么想说的"
    clock.now += 7 * 60                       # 表达欲每分钟 +0.5
    director.tick(sim)
    _settle(director, sim)
    assert len(director.client.calls) == 1


def test_away_sleeps_without_requests_and_back_wakes(tmp_path):
    sim = _sim()
    clock = Clock()
    idle = {"s": 0.0}
    director = _director(tmp_path, {"say": "你回来了。"}, clock=clock, idle=lambda: idle["s"])
    idle["s"] = 1200.0
    for _ in range(5):
        clock.now += 600
        director.tick(sim)
    assert director.away and not director.client.calls
    director.tick(sim)
    assert director.planner.sleeping or director.planner.current["do"] == "sleep"
    idle["s"] = 1.0
    clock.now += 3
    director.tick(sim)
    _settle(director, sim)
    assert not director.away
    assert len(director.client.calls) == 1
    assert "回到" in json.loads(director.client.calls[0][-1]["content"])["event"]


def test_daily_cap_stops_autonomy_but_not_chat(tmp_path):
    sim = _sim()
    clock = Clock()
    director = _director(tmp_path, {"say": "嗯"}, clock=clock, daily_token_cap=500)
    director.usage.record({"prompt_tokens": 600, "completion_tokens": 0})
    assert director.over_budget
    clock.now += 3600
    sim.pending_events.append("drag")
    director.tick(sim)
    assert not director.client.calls
    director.say_to("在吗")
    director.tick(sim)
    _settle(director, sim)
    assert len(director.client.calls) == 1


def test_failure_falls_back_to_pool_and_backs_off(tmp_path):
    sim = _sim()
    clock = Clock()
    director = _director(tmp_path, RuntimeError("boom"), clock=clock)
    clock.now += 100.0
    sim.pending_events.append("drag")
    director.tick(sim)
    _settle(director, sim)
    assert director.last_error and "boom" in director.last_error
    if sim.dialogue.text:
        assert sim.dialogue.text in C.DEFAULT_LINES["characters"]["pebbles"]["drag"]
    calls = len(director.client.calls)
    clock.now += ERROR_BACKOFF - 1.0
    sim.pending_events.append("drag")
    director.tick(sim)
    assert len(director.client.calls) == calls, "退避期内不许再请求"


# -- 记忆 / 用量 / OCR ----------------------------------------------------------
def test_memory_flags_and_prompt_block(tmp_path):
    memory = Memory(tmp_path / "m.json", now=lambda: datetime(2026, 10, 30, 22, 0))
    assert memory.remember("喜欢猫") and not memory.remember("喜欢猫")
    for i in range(PROFILE_SOFT_CAP):
        memory.remember(f"事实{i}")
    assert memory.wants_rewrite
    assert memory.rewrite_profile(["喜欢猫", "晚睡"]) and not memory.wants_rewrite
    memory.events_since_diary = 10
    assert memory.wants_diary
    assert memory.write_diary("今天被拖了三次。") and not memory.wants_diary
    memory.save()
    again = Memory(tmp_path / "m.json")
    assert again.profile == ["喜欢猫", "晚睡"] and again.diary[-1]["text"] == "今天被拖了三次。"
    assert "晚睡" in again.prompt_block()


def test_usage_by_day_and_cost(tmp_path):
    today = {"d": date(2026, 10, 30)}
    usage = UsageTracker(tmp_path / "u.json", today=lambda: today["d"])
    usage.record({"prompt_tokens": 1000, "completion_tokens": 100,
                  "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200})
    today["d"] = date(2026, 10, 31)
    usage.record({"prompt_tokens": 500, "completion_tokens": 50})
    assert usage.today_row()["calls"] == 1 and usage.today_row()["cache_miss"] == 500
    assert usage.total["calls"] == 2 and usage.total["prompt"] == 1500
    cost = UsageTracker.cost({"cache_hit": 1_000_000, "cache_miss": 0, "completion": 0},
                             (0.2, 2.0, 3.0))
    assert abs(cost - 0.2) < 1e-9
    assert UsageTracker(tmp_path / "u.json").total["calls"] == 2


def test_usage_tracks_task_separately_and_loads_old_files(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"days": {"2026-10-30": {"calls": 3, "prompt": 300}},
                                "total": {"calls": 3, "prompt": 300}}), encoding="utf-8")
    usage = UsageTracker(path, today=lambda: date(2026, 10, 30))
    usage.record({"prompt_tokens": 50, "completion_tokens": 5}, source="task")
    assert usage.today_row()["calls"] == 4
    assert usage.today_by_source()["task"]["prompt"] == 50
    assert UsageTracker(path, today=lambda: date(2026, 10, 30)).today_by_source()["task"]["completion"] == 5


def test_clean_ocr_joins_cjk_and_drops_noise():
    text = clean_ocr(["迭 代 器 模 型 API 集 成", "x", "迭 代 器 模 型 API 集 成", "Hello  world"])
    assert text == "迭代器模型 API 集成 / Hello world"
