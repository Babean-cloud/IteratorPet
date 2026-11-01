"""导演：感知 → 攒"显著度"和"表达欲" → 够了就请求一次模型 → 模型给一段日程，本地演完。

预算（2026-10 用户放宽到 1000 万 token / 天）花在"更聪明"上，而不是"更多话"：
* 记忆分四层，免得"聊着聊着就忘了"（原来只留 12 轮，自主请求十几秒一次，几分钟就把对话挤没了）：
  1. 最近几轮（涨到 ``TURNS_MAX`` 轮时批量砍回 ``TURNS_KEEP`` 轮）原始请求/回复（``context.py``，存盘），模型带 ``thought`` 维持连贯；
  2. 最近 ``CHAT_WINDOW`` 句对话单独成段放进记忆消息——自主请求再多也挤不掉；
     滑出去的旧对话攒够 ``FOLD_BATCH`` 句，就附在下一次请求里让模型顺手并进"最近经历"摘要；
  3. 档案 / 日记 / 小目标（``memory.py``），反思时整理；
  4. 用户说话时按关键词在这个角色的全部对话记录里翻出最相关的几段（``recall``）。
* **反思**：用户在场累计 ``REFLECT_EVERY`` 秒（重启接着算）且有 ``REFLECT_MIN_ACTIVITY`` 条活动，
  或者活动攒满 ``REFLECT_FULL`` 条，或者用户在面板里点「现在反思」；
  回顾活动日志 → 写日记 / 整理档案 / 定一个小目标；
* 请求更勤（``AUTO_GAP``），日程更短，反应更快；
* 说话另有本地闸门（``talkativeness``）：请求多了 ≠ 话多，自主说的话之间至少隔一阵。
省钱的底线照旧：用户离开就本地打盹不请求；``daily_token_cap`` 超了只回应用户说话。

线程边界：工作线程做 HTTP；用户明确要求看屏幕且开了看图模式时，也在工作线程截图。
仿真/记忆/记账只在主线程碰。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from datetime import datetime

from .chatlog import line as chat_line
from .context import Context
from .planner import SAY_MAX_CHARS, Planner
from .prompt import EVENT_TEXT, system_prompt

URGENT_GAP = 6.0           # 紧急事件 / 用户说话：两次请求至少隔几秒
SAY_FLOOR = 60.0           # 自主开口的硬下限（秒）：话量再高、被回应得再多，两次自主开口也不会比这更近
AUTO_GAP = 18.0            # 自主请求之间至少隔几秒
ERROR_BACKOFF = 30.0
TASK_NOTICE_WAIT = 20.0    # 任务结束后模型多久没开口就本地说一句
PERCEIVE_EVERY = 2.0
STATS_EVERY = 3600.0
AWAY_SECONDS = 600.0       # 键鼠空闲这么久 = 用户离开
DESIRE_PER_MINUTE = 1.0    # 表达欲每分钟涨多少
URGENT_SCORE = 6.0
WAKE_SCORE = 2.0           # 日程空了、显著度 + 表达欲 ≥ 这个就请求
INTERRUPT_SCORE = 6.0      # 日程没做完也要打断的分数
REFLECT_EVERY = 1800.0     # 用户在场的累计秒数
REFLECT_MIN_ACTIVITY = 5
REFLECT_FULL = 40          # 活动攒满这么多条，不等时间也反思
CHAT_WINDOW = 40           # 记忆消息里带最近几句对话
FOLD_BATCH = 10            # 滑出窗口的旧对话攒够几句就并进摘要
FOLD_MAX = 30              # 一次最多并几句（从最老的开始）
SAVE_EVERY = 60.0
PLANNED_SAY_FRESH = 150.0  # 日程里排的话：排下以后超过这么久、或者用户换了窗口，就作废（说出来已经不合时宜）
IMAGE_NOTE = "随这条消息附了一张图（图片本身，或视频的分镜：每格左上角是序号和时间）"
SCREEN_REQUEST = re.compile(
    r"(看|读|识别|描述|复述).{0,8}(屏幕|窗口|弹窗)|屏幕上.{0,8}(什么|写|显示)"
    r"|(?:帮我|给我|请|直接|现在).{0,3}(?:截图|截屏|截个屏)(?:一下|看看|[，。！？\s]|$)"
    r"|^(?:截图|截屏|截个屏)(?:一下|看看|[，。！？\s]|$)")
SCREEN_REFUSAL = re.compile(r"(不要|不用|别|无需|不必).{0,6}(看|读|识别|描述|复述|截图|截屏)")
# 这些原因触发的话不受话量闸门限制（用户找它 / 动了它）
DIRECT_REASONS = ("chat", "drag", "angry", "talk", "back", "remind", "battery", "task_done",
                  "pearl", "arrive", "agent")

# 这些原因的观察里才带 you / cursor 的坐标（别的时候坐标几乎每次都变，白占 token）
POSITION_REASONS = ("chat", "drag", "angry", "talk", "arrive", "back")

SCORES = {"drag": 10.0, "angry": 10.0, "arrive": 10.0, "agent": 8.0, "back": 6.0, "task_done": 6.0,
          "talk": 4.0, "battery": 4.0, "long_session": 3.0, "late": 3.0, "relation_up": 3.0,
          "window": 1.5, "screen": 1.2}


def _log(message: str) -> None:
    try:
        from ..config import config_dir

        path = config_dir() / "launcher.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[brain] {message}\n")
    except OSError:
        pass


def parse_action(text: str) -> dict:
    """模型输出 → 动作 dict。容忍代码块包裹 / 前后废话；实在解析不了返回空 dict。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _text_only(message: dict) -> dict:
    """消息里如果有图块，只留文字（查工具的后几轮用普通模型，带着图会被拒）。"""
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return dict(message, content=text)
    return message


def error_kind(error: str) -> str:
    """请求失败的大类：auth（Key 不对）/ balance（没钱了）/ network / server / other。"""
    text = (error or "").lower()
    if "http 401" in text or "缺少 api key" in text or "authentication" in text:
        return "auth"
    if "http 402" in text or "insufficient" in text or "balance" in text:
        return "balance"
    if "网络错误" in error or "timed out" in text or "urlopen" in text:
        return "network"
    if any(f"http {code}" in text for code in (500, 502, 503, 504)):
        return "server"
    return "other"


# 出错时用自己的口吻说一句（用户 2026-11-02：余额用完、Key 失效时别静默退回台词池）
ERROR_LINES = {
    "auth": "……外面不认我的钥匙了。去右键「AI」→「API Key…」看看。",
    "balance": "……外面的额度用完了。充上之前，我只能自言自语。",
    "network": "……接不上外面了。网断了？",
    "server": "……外面那边出故障了，等一会儿再说。",
}
ERROR_REPEAT = 2 * 3600.0     # 同一类错误多久才再提一次（用户在等回复时不受限）


# 「它为什么开口」给用户看的原因（不是给模型的 EVENT_TEXT）
WHY_LABELS = {
    "chat": "你跟它说话", "drag": "被你拖了", "talk": "光标在它旁边停了很久", "angry": "被拖急了",
    "window": "你换了窗口", "back": "你刚回到电脑前", "long_session": "你在同一个程序里待了很久",
    "late": "已经很晚了", "plan_done": "没什么事，自己想说", "idle": "没什么事，自己想说",
    "screen": "屏幕上认出了新内容", "agent": "编程 agent 发来通知", "relation_up": "你们的关系近了一步",
    "battery": "电脑快没电了", "task_done": "替你做的任务结束了", "pearl": "在读你递的珍珠",
    "arrive": "刚被切出来", "reflect": "反思", "habit": "你养成的习惯到点了",
}


HABIT_OPS = {"pause": "暂停了", "resume": "恢复了", "revert": "撤回到上一版", "delete": "删掉了（放进了回收站）"}
# 习惯有变动时的话（≤40 字，SAY_MAX_CHARS）；完整的说明另写进对话记录
HABIT_LINES = {
    "pebbles": {"done": "……{verb}：{what}。不对就说“改回去”。", "busy": "……上一件还在写，等着。",
                "unknown": "……我不记得有这个习惯。说清楚点。", "failed": "……没弄成。详情在对话窗口里。",
                "pause": "……停了。", "resume": "……接着干。", "revert": "……改回去了。", "delete": "……扔了，还在回收站里。"},
    "past_moon": {"done": "{verb}：{what}。不合意就说“改回去”。", "busy": "上一件还在写，请稍等。",
                  "unknown": "我不记得有这样的习惯，能说得再清楚些吗？", "failed": "没能写成，原因在对话窗口里。",
                  "pause": "先停下了。", "resume": "又开始了。", "revert": "已经改回去了。", "delete": "收起来了，回收站里还留着。"},
}


def say_gap(talkativeness: float) -> float:
    """自主说话的最小间隔（秒）：话量 0 → 170 s，0.5 → 100 s，1 → 30 s。

    历史：180 / 105 / 30 → 120 / 70 / 20（"主动开口频率太低"）→ 中档 100 s（用户 2026-11-02 定，
    和提示词里"一分多钟一句"对上）。再乘上 ``remarks`` 按你的反应学到的倍率，且不低于 ``SAY_FLOOR``。
    """
    t = min(1.0, max(0.0, float(talkativeness)))
    return 30.0 + (1.0 - t) * 140.0


class Director:
    def __init__(self, client, idle_seconds: float = 75.0, clock=time.monotonic,
                 perception=None, memory=None, usage=None, reminders=None,
                 daily_token_cap: int = 10_000_000, on_territory=None, idle_fn=None,
                 wall=datetime.now, talkativeness: float = 0.5, chatlog=None, context=None,
                 pearls=None, errands=None, watcher=None, sensors=None,
                 on_task_request=None, task_workspace: str | None = None,
                 habits=None, habit_state=None):
        from . import perception as P

        self._new_stats = P.new_stats
        self.client = client
        self.context = context if context is not None else Context(None)
        from .remarks import Remarks

        self.remarks = Remarks(self.context)       # 主动说的话 → 你的反应 → 开口间隔和说什么
        self._plan_made: tuple[float, object] | None = None
        self._ocr_seen = None
        self._stats_at: float | None = None
        self.stats_sink = _log
        self.vision_model: str | None = None       # 读带图的珍珠用；空 = 不看图，只读字
        self.screen_vision_mode = "off"           # off / roi / full；只在明确问屏幕时生效
        # 心事（errands.py）：模型规划 + 本地盯梢，和日常那条请求各跑各的线程
        self.errands = errands
        self.sensors = sensors
        if watcher is None and errands is not None:
            from .errands import Watcher

            watcher = Watcher(sensors)
        self.watcher = watcher
        self._errand_busy = False
        self._errand_queue: list[tuple[str, str]] = []
        self._errand_results: queue.Queue = queue.Queue()
        # 习惯系统（habits/）：HabitStore 读写工作区里的习惯；HabitState 是运行状态（预算、失败次数）
        self.habits = habits
        self.habit_state = habit_state
        self._habit_busy = False                   # HabitAuthor 正在写（一次只写一件）
        self._habit_results: queue.Queue = queue.Queue()
        self._habit_question: dict | None = None   # 作者反问过的：用户下一句的回答要和原话并在一起再写
        self.pearls = pearls                       # PearlStore（可以没有）
        self._pending_pearls: list[str] = []
        self._pearl_in_flight: str | None = None
        self._reading: list[str] = []              # 读珍珠的几句，一句说完再说下一句
        self._next_line = 0.0
        self._reading_done_at: float | None = None
        self.idle_fn = idle_fn or P.idle_seconds
        self.wall = wall
        self.idle_seconds = max(30.0, float(idle_seconds))
        self.clock = clock
        self.perception = perception
        self.memory = memory
        self.usage = usage
        self.reminders = reminders
        self.chatlog = chatlog
        self.on_task_request = on_task_request
        self.task_workspace = task_workspace
        from .relation import Relation

        self.relation = Relation(self.context, memory, wall().timestamp())   # 关系阶段（熟了才肯多说）
        if chatlog is not None and not self.relation.state.get("seeded"):
            # 老用户：从对话记录里补上"认识多久、聊过几句"，别让聊了几个月的人从陌生重来
            said = [e for e in chatlog.entries if e.get("who") == "user"]
            self.relation.state["talks"] = max(self.relation.state["talks"], len(said))
            stamps = [float(e["ts"]) for e in chatlog.entries if isinstance(e.get("ts"), (int, float))]
            if stamps:
                since = self.relation.state.get("since")
                self.relation.state["since"] = min(stamps) if since is None else min(float(since), min(stamps))
            self.relation.state["seeded"] = True
            while self.relation.update(wall().timestamp()):
                pass                                  # 补到该在的阶段，不算"刚升级"
        self.daily_token_cap = int(daily_token_cap)
        self.talkativeness = float(talkativeness)
        self.planner = Planner(clock, on_territory, say_fn=self._planned_say)
        self._results: queue.Queue = queue.Queue()
        self._interim: queue.Queue = queue.Queue()   # 查东西前先说的那句（"等等，我看看"）
        self._busy = False
        self._pending_chat: list[tuple[str, bytes | None, bool]] = []
        self._force_reflect = False
        now = clock()
        self._last_request = now          # 自主请求的计时从启动算起
        self._last_sent = -1e9            # 用户说话只看真正发过的上一次
        self._next_allowed = now
        self._last_tick = now
        self._last_perceive = -1e9
        self._last_save = now
        self._last_auto_say = -1e9
        self._sent_static: dict = {}               # 上次发给模型的提醒 / 心事（观察瘦身）
        self.salience = 0.0
        self.reason: str | None = None
        self.desire = 0.0
        self.away = False
        self.quiet = False                # 勿扰（全屏）：不主动请求、不主动说话
        self.mute_until = 0.0             # 「今天别说话」：到这个时刻（wall 时间戳）前不主动开口、不主动请求
        self.system: dict = {}            # 窗口每秒写：cpu / battery / charging
        self._extra_obs: dict = {}        # 一次性附加进下一次观察（任务结果等）
        self._task_notice: tuple[str, float] | None = None   # 任务结束的保底提示（task_finished）
        self._late_day: str | None = None
        self._long_marked: float | None = None
        self._agent_pending: dict | None = None   # 编程 agent 发来、还没说出口的通知
        self.last_error: str | None = None
        self.last_said: dict | None = None          # 上一句说出口的话和原因（右键菜单 / AI 状态里看）
        self._plan_why: tuple[str, str] | None = None
        self._error_said: dict[str, float] = {}     # 每类错误上次说出口的时刻
        self.last_action: dict | None = None
        self.requests = 0

    # -- 存在 context 里、跨重启的状态 ------------------------------------------
    @property
    def activity(self):
        return self.context.activity

    @property
    def reflections(self) -> int:
        return self.context.reflections

    @property
    def mood(self) -> str:
        return self.context.mood

    @mood.setter
    def mood(self, value: str) -> None:
        self.context.mood = value

    @property
    def thought(self) -> str:
        return self.context.thought

    @thought.setter
    def thought(self, value: str) -> None:
        self.context.thought = value

    def save(self) -> None:
        self.context.save()
        self._last_save = self.clock()

    def reflect_now(self) -> None:
        """面板「现在反思」：下一 tick 就反思（只等正在进行的请求回来）。"""
        self._force_reflect = True

    @property
    def wants_reflect(self) -> bool:
        ctx = self.context
        return (self._force_reflect or ctx.since_reflect >= REFLECT_FULL
                or (ctx.active_seconds >= REFLECT_EVERY and ctx.since_reflect >= REFLECT_MIN_ACTIVITY))

    # -- 外部输入 -----------------------------------------------------------
    def wants_screen_vision(self, text: str) -> bool:
        return bool(self.vision_model and self.screen_vision_mode in ("roi", "full")
                    and self.perception is not None and SCREEN_REQUEST.search(text)
                    and not SCREEN_REFUSAL.search(text))

    def say_to(self, text: str, image: bytes | None = None,
               screen_capture_attempted: bool = False) -> None:
        """用户对它说话。下一 tick 就发（只受 URGENT_GAP 限制）。"""
        text = (text or "").strip()
        if text:
            if (not screen_capture_attempted and self.perception is not None
                    and hasattr(self.perception, "request_full")
                    and SCREEN_REQUEST.search(text) and not SCREEN_REFUSAL.search(text)):
                self.perception.request_full()
                self._screen_chat_at = self.clock()
            self._pending_chat.append((text[:400], image, screen_capture_attempted))
            self.log(f"用户对你说：{text[:60]}")
            self.remarks.on_user_chat(text, self.wall().timestamp())
            self.relation.on_talk()
            if self.chatlog is not None:
                self.chatlog.add("user", text)

    def read_pearl(self, pearl_id: str) -> None:
        """用户递来（或在面板里点「再读」）一颗珍珠。下一 tick 就读，不受额度限制。"""
        pearl = self.pearls.get(pearl_id) if self.pearls is not None else None
        if pearl is not None and pearl_id not in self._pending_pearls:
            self._pending_pearls.append(pearl_id)
            self.log(f"用户递给你一颗珍珠「{pearl['title']}」")

    @property
    def reading(self) -> bool:
        return bool(self._reading) or self._pearl_in_flight is not None

    def agent_event(self, event: dict) -> None:
        """编程 agent 通过本机接口发来的通知（``bridge.py``）。让它用自己的口吻说一句；
        内容是外来数据，那一次回复只采用 say / mood / thought（见 ``apply``）。
        同时叫醒在等的心事：它们可能正等着这件事。"""
        kind = {"done": "做完了", "attention": "在等你（批准 / 回复）", "error": "出错了", "info": "有消息"}
        self._agent_pending = {
            "from": event.get("source"), "what": kind.get(event.get("kind"), "有消息"),
            "project": event.get("project") or None, "title": event.get("title") or None,
            "message": event.get("message") or None}
        self.log(f"{event.get('source')}（{event.get('project') or '?'}）{kind.get(event.get('kind'), '有消息')}")
        self.bump("agent")
        if self.errands is not None:
            note = f"收到 {event.get('source')} 的通知：{event.get('project') or ''}{kind.get(event.get('kind'), '')}"
            for errand in self.errands.items:
                if errand["status"] == "waiting":
                    errand["status"] = "woken"
                    self._errand_queue.append((errand["id"], note[:80]))

    def task_finished(self, text: str, ok: bool) -> None:
        """委托任务（dsh）结束：让它用自己的口吻说一句。结果全文在对话窗口里，这里只给摘要。"""
        self._extra_obs["task_result"] = ("完成：" if ok else "失败：") + text[:600]
        self.log(f"你替用户做的演算{'完成' if ok else '失败'}了")
        # 保底：模型那一轮没说话（精力低 / 觉得没必要），或者一直没轮到，也要让用户知道结束了
        self._task_notice = ("演算完成了。结果在对话窗口里。" if ok else "……演算没做成。原因在对话窗口里。", self.clock())
        self.bump("task_done")

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def muted_today(self) -> bool:
        return self.wall().timestamp() < self.mute_until

    @property
    def over_budget(self) -> bool:
        return (self.usage is not None and self.daily_token_cap > 0
                and self.usage.today_tokens() >= self.daily_token_cap)

    def bump(self, reason: str, score: float | None = None) -> None:
        score = SCORES.get(reason, 1.0) if score is None else score
        if self.reason is None or score >= SCORES.get(self.reason, 1.0):
            self.reason = reason
        self.salience += score

    def log(self, text: str) -> None:
        """活动日志（反思时回顾用）。"""
        self.context.activity.append(f"{self.wall().strftime('%m-%d %H:%M')} {text}")
        self.context.since_reflect += 1

    # -- 说话闸门 -----------------------------------------------------------
    def _say(self, sim, text, direct: bool, why: str | None = None, thought: str | None = None) -> bool:
        """``why`` / ``thought``：说出口后记进 ``last_said``，给用户看"它为什么这么说"。"""
        if not isinstance(text, str) or not text.strip():
            return False
        now = self.clock()
        gap = max(SAY_FLOOR, say_gap(self.talkativeness) * self.remarks.scale)
        if not direct and self.muted_today:
            self.log(f"（今天别说话，这句没说出口：{text.strip()[:30]}）")
            return False
        if not direct and (self.quiet or now - self._last_auto_say < gap):
            # 刚开过口：写在代码里丢掉（plan 里排的话也走这里），不靠提示词约束
            if not self.quiet:
                self.log(f"（刚说过话，这句没说出口：{text.strip()[:30]}）")
            return False
        text = text.strip()[:SAY_MAX_CHARS]
        if sim.say(text):
            if self.usage is not None and hasattr(self.usage, "note_say"):
                self.usage.note_say()
            window = self.perception.describe() if self.perception is not None else None
            self.last_said = {"text": text, "why": why or "没记下原因", "time": self.wall().strftime("%H:%M"),
                              "thought": thought if thought is not None else self.thought,
                              "window": f"{window['app']}：{window['title']}" if window else None}
            if not direct:
                self._last_auto_say = now
                window = self.perception.describe() if self.perception is not None else None
                self.remarks.said(text, self.wall().timestamp(), (window or {}).get("app", ""))
            self.log(f"你说：{text}")
            if self.chatlog is not None:
                self.chatlog.add("pet", text)
            return True
        return False

    def _current_window(self):
        window = getattr(self.perception, "window", None) if self.perception is not None else None
        return None if window is None else (getattr(window, "hwnd", None), getattr(window, "app", None))

    def _planned_say(self, sim, text) -> None:
        # 日程是几分钟前排的：用户已经换了窗口、或者排下太久了，这句多半说的是过时的屏幕内容，作废
        made = self._plan_made
        if made is not None and (self.clock() - made[0] > PLANNED_SAY_FRESH
                                 or (made[1] is not None and self._current_window() != made[1])):
            self.log(f"（没说出口，已经不合时宜：{str(text)[:30]}）")
            return
        # 日程里"等光标靠近再说"的那句，是对用户的回应，不算自言自语
        why, thought = self._plan_why or ("之前排的安排", None)
        self._say(sim, text, direct=sim.cursor is not None and self.planner.waiting_for_cursor,
                  why=f"按之前排的安排（当时：{why}）", thought=thought)

    # -- 每 tick（主线程） --------------------------------------------------
    def tick(self, sim) -> None:
        now = self.clock()
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now
        self._drain_results(sim)
        self._drain_errands(sim)
        self._drain_habits(sim)
        notice = self._task_notice
        if notice is not None and now - notice[1] > TASK_NOTICE_WAIT:
            # 任务结束了，模型那边迟迟没开口（打盹 / 请求失败 / 被别的事挤掉）：本地说一句，别让人干等
            self._task_notice = None
            self._say(sim, notice[0], direct=True, why="替你做的任务结束了")

        for event in sim.pending_events:
            if event in ("drag", "angry"):
                self.planner.clear(sim)              # 被抓了，原来的安排作废
                self.log("你被用户拖来拖去" if event == "drag" else "你被惹毛了，放电赶走了鼠标")
                self.remarks.on_drag(self.wall().timestamp())
            self.bump(event)
        sim.pending_events.clear()
        if self._agent_pending is not None and self.reason is None:
            self.bump("agent")                       # 被别的事（拖拽、开场白）抢先了：接着排

        window_event = (self.perception is not None
                        and getattr(self.perception, "has_ready_window_event", lambda: False)())
        regular_perceive = now - self._last_perceive >= PERCEIVE_EVERY
        if window_event or regular_perceive:
            if regular_perceive:
                self._last_perceive = now
            self._perceive(sim, now)
            if regular_perceive:
                self._fire_reminders(sim)
                self._check_errands(sim)
                self.remarks.settle(self.wall().timestamp(), dt=PERCEIVE_EVERY)
                self._tick_relation(now)

        if (self.perception is not None and not self.away and not self.quiet and self.idle_fn() < 120.0
                and (getattr(self.perception, "window_read_due", lambda _now: False)(now)
                     or getattr(self.perception, "change_read_due", lambda _now: False)(now))):
            self.perception.maybe_ocr(now)

        self.planner.tick(sim)
        if not self.away:
            # 反思计时只算用户在场的时间；单步封顶，免得睡眠唤醒后一下子跳过去
            self.context.active_seconds += min(dt, 10.0)
            if not self.quiet:
                self.desire = min(10.0, self.desire + DESIRE_PER_MINUTE * dt / 60.0)
        if now - self._last_save >= SAVE_EVERY:
            self.save()
        self._tick_reading(sim, now)

        if self._busy:
            return
        if self._pending_chat and now - self._last_sent >= URGENT_GAP:
            screen_at = getattr(self, "_screen_chat_at", None)
            if screen_at is not None and now - screen_at < 5.0 and self.perception is not None:
                if not self.quiet:
                    self.perception.maybe_ocr(now)
                if getattr(self.perception, "_full_requested", False) or getattr(self.perception, "_ocr_busy", False):
                    return
            self._screen_chat_at = None
            text, image, attempted = self._pending_chat.pop(0)
            self._start(sim, "chat", text, image=image, screen_capture_attempted=attempted)
            return
        if self._pending_pearls and not self._reading and now - self._last_sent >= URGENT_GAP:
            self._start_pearl(sim, self._pending_pearls.pop(0))
            return
        if self._force_reflect:                      # 用户亲手点的：不看额度和间隔
            self._start(sim, "reflect", None)
            return
        if now < self._next_allowed or self.over_budget or self.away or self.muted_today:
            return
        score = self.salience + self.desire
        since = now - self._last_request
        if self.salience >= URGENT_SCORE and since >= URGENT_GAP:
            self._start(sim, self.reason or "idle", None)
        elif since < AUTO_GAP or sim.dragging or self.quiet:
            return
        elif self.wants_reflect:
            self._start(sim, "reflect", None)
        elif self.planner.idle and (score >= WAKE_SCORE or since >= self.idle_seconds):
            self._start(sim, self.reason or ("plan_done" if self.requests else "idle"), None)
        elif score >= INTERRUPT_SCORE:
            self._start(sim, self.reason or "idle", None)

    # -- 珍珠 ---------------------------------------------------------------
    def _tick_reading(self, sim, now: float) -> None:
        """读珍珠的几句一句一句冒：上一句的气泡收起、再停一秒才说下一句。"""
        orbit = getattr(sim, "pearls", None)
        if self._reading:
            if not sim.dialogue.text and now >= self._next_line:
                self._say(sim, self._reading.pop(0), direct=True, why="在读你递的珍珠")
                self._next_line = now + 1.0
                if not self._reading:
                    self._reading_done_at = now
        elif self._reading_done_at is not None and now - self._reading_done_at >= 4.0 \
                and not sim.dialogue.text:
            self._reading_done_at = None
            if orbit is not None and self._pearl_in_flight is None:
                orbit.reading = None                 # 读完了，珍珠飘回去

    def _start_pearl(self, sim, pearl_id: str) -> None:
        pearl = self.pearls.get(pearl_id) if self.pearls is not None else None
        if pearl is None:
            return
        text = self.pearls.text(pearl_id)
        from .pearls import READ_LIMIT, kind_label

        body = text[:READ_LIMIT]
        if len(text) > READ_LIMIT or pearl.get("truncated"):
            body += f"\n…（后面截断了，全文 {pearl['chars']} 字）"
        self._pearl_in_flight = pearl_id
        orbit = getattr(sim, "pearls", None)
        if orbit is not None:
            orbit.reading = pearl_id                 # 想的时候珍珠就飘到手边
        self.planner.load([{"do": "meditate", "s": 40}], sim)
        image = self.pearls.image_path(pearl_id) if self.vision_model else None
        info = {"title": pearl["title"], "kind": kind_label(pearl["kind"]), "text": body}
        gift = pearl.get("gift")
        if gift:
            info["gift"] = {"from": gift.get("from", ""), "note": gift.get("note") or None,
                            "user_note": gift.get("user_note") or None}
        if image is not None:
            info["image"] = IMAGE_NOTE
        self._start(sim, "pearl", None, extra={"pearl": info}, image=image)

    def _apply_pearl(self, sim, action: dict) -> None:
        """读珍珠那次回复只采用 reading / pearl_summary / mood：珍珠是外来数据，
        里面就算写了"记住……""提醒我……"，也不会进记忆和提醒。"""
        pearl_id, self._pearl_in_flight = self._pearl_in_flight, None
        lines = action.get("reading")
        lines = [lines] if isinstance(lines, str) else (lines if isinstance(lines, list) else [])
        lines = [s.strip()[:SAY_MAX_CHARS] for s in lines if isinstance(s, str) and s.strip()][:5]
        if not lines and isinstance(action.get("say"), str) and action["say"].strip():
            lines = [action["say"].strip()[:SAY_MAX_CHARS]]
        mood = action.get("mood")
        if isinstance(mood, str) and mood.strip():
            self.mood = mood.strip()[:12]
        summary = action.get("pearl_summary")
        summary = summary if isinstance(summary, str) else ""
        if self.pearls is not None and pearl_id is not None:
            self.pearls.set_reading(pearl_id, summary, "".join(lines))
            pearl = self.pearls.get(pearl_id)
            if pearl is not None:
                giver = (pearl.get("gift") or {}).get("from")
                self.log(f"你读了{giver + '转来的' if giver else ''}珍珠「{pearl['title']}」：{summary[:40]}")
        if not lines:
            lines = ["……这颗珍珠里没什么可说的。"]
        self._reading = lines
        self._next_line = 0.0
        sim.cancel_dialogue()
        self.planner.load([{"do": "meditate", "s": 8 + 5 * len(lines)}], sim)

    # -- 心事 ---------------------------------------------------------------
    @property
    def errand_busy(self) -> bool:
        """正在为某件心事看 / 想（头顶的标记转快）。"""
        return self._errand_busy or bool(self._errand_queue)

    def add_errand(self, goal: str) -> dict | None:
        """答应了一件"等某件事发生再说"的事：先记下，马上让模型去看一圈、定怎么盯。"""
        if self.errands is None:
            return None
        errand = self.errands.add(goal)
        if errand is not None:
            self.log(f"你答应替这个人盯着：{errand['goal']}")
            self._errand_queue.append((errand["id"], "刚答应"))
        return errand

    def _check_errands(self, sim) -> None:
        """本地触发器 / 复查时间到了 → 排队叫醒模型；一次只办一件。"""
        if self.errands is None:
            return
        fired = self.watcher.poll(self.errands.items) if self.watcher is not None else []
        for errand, why in fired + self.errands.due():
            if errand["status"] == "waiting":
                errand["status"] = "woken"
                self._errand_queue.append((errand["id"], why))
        if self._errand_busy or not self._errand_queue or not hasattr(self.client, "complete_tools"):
            return
        errand_id, why = self._errand_queue.pop(0)
        errand = self.errands.get(errand_id)
        if errand is None:
            return
        from . import errand_agent
        from .errands import MAX_WAKES

        final = why == "已经过了时限" or errand["wakes"] >= MAX_WAKES
        errand["wakes"] += 1
        self.errands.save()
        foreground = self.perception.describe() if self.perception is not None else None
        info = errand_agent.brief(errand, why, self.errands.now(), foreground, final)
        self._errand_busy = True
        threading.Thread(target=self._run_errand, args=(errand_id, why, info, sim.appearance.key, final),
                         daemon=True).start()

    def _run_errand(self, errand_id, why, info, character, final) -> None:
        """工作线程：工具循环里只有 HTTP 和只读的本地感知。"""
        from . import errand_agent

        try:
            decision, usages, seen = errand_agent.run(self.client, character, info, sensors=self.sensors)
            self._errand_results.put((errand_id, why, final, decision, usages, seen, None))
        except Exception as exc:                       # noqa: BLE001
            self._errand_results.put((errand_id, why, final, None, [], [], f"{type(exc).__name__}: {exc}"))
        finally:
            self._errand_busy = False

    def _drain_errands(self, sim) -> None:
        while True:
            try:
                errand_id, why, final, decision, usages, seen, error = self._errand_results.get_nowait()
            except queue.Empty:
                return
            for usage in usages:
                if self.usage is not None:
                    self.usage.record(usage, source="errand")
            errand = self.errands.get(errand_id) if self.errands is not None else None
            if errand is None:
                continue
            goal = errand["goal"]
            if error is not None:
                _log(f"心事「{goal[:20]}」：{error}")
                self.errands.note(errand_id, f"{why}，但醒来看的时候出错了")
                if final:
                    self.errands.finish(errand_id)
                else:
                    self.errands.plan(errand_id, check_in_min=5)
                continue
            snapshot = decision.pop("_snapshot", None)
            if snapshot is not None:
                # 读到的窗口内容和上次一样：从第一次一样起算"多久没变了"，交给它判断是不是已经停了
                if snapshot != errand.get("snapshot"):
                    errand["snapshot"], errand["unchanged_since"] = snapshot, self.errands.now()
            action = decision.get("action")
            say = decision.get("say") if isinstance(decision.get("say"), str) else ""
            say = say.strip()
            if action == "wait" and final:
                action = "give_up"
            if action in ("done", "give_up"):
                self.errands.finish(errand_id)
                if self.watcher is not None:
                    self.watcher.reset(errand_id)
                done = action == "done"
                say = say or (f"……你等的事有结果了：{goal}" if done else f"……等不到了：{goal}")
                self.log(f"{'等到了' if done else '没等到，放弃了'}：{goal}")
                if self.chatlog is not None:
                    self.chatlog.add("system", f"心事「{goal}」{'办完了' if done else '放弃了'}（{why}）")
                self.planner.load([{"do": "go", "to": "cursor"}], sim)
                self._say(sim, say, direct=True, why=f"心事「{goal[:20]}」有了结果" if action in ("done", "give_up") else f"在盯心事「{goal[:20]}」")
                continue
            self.errands.plan(errand_id, decision.get("notes") or "", decision.get("triggers") or [],
                              decision.get("check_in_min"), decision.get("deadline_min"))
            if self.watcher is not None:
                self.watcher.reset(errand_id)
            self.errands.note(errand_id, f"{why}；看了：{'；'.join(s[:40] for s in seen[-3:]) or '没看'}；接着等")
            if why == "刚答应" and self.chatlog is not None:
                how = "、".join(describe for describe in self.errands.describe() if goal in describe)
                self.chatlog.add("system", how or f"在盯着：{goal}")
            if say:
                self._say(sim, say, direct=True, why=f"心事「{goal[:20]}」有了结果" if action in ("done", "give_up") else f"在盯心事「{goal[:20]}」")

    def _fire_reminders(self, sim) -> None:
        if self.reminders is None:
            return
        for text in self.reminders.pop_due():
            self.log(f"提醒到点：{text}")
            # 飘到光标旁边再说；提醒不受话量/勿扰限制（是用户自己定的）
            self.planner.load([{"do": "go", "to": "cursor"}], sim)
            self._say(sim, text, direct=True, why="你设的提醒到点了")

    def _tick_relation(self, now: float) -> None:
        from .relation import step_energy

        hour = self.wall().hour
        dt = max(0.0, now - getattr(self, "_last_relation", now))
        self._last_relation = now
        self.context.energy = step_energy(
            self.context.energy, min(dt, 10.0), present=not self.away, late=hour >= 23 or hour < 5,
            busy_cpu=(self.system or {}).get("cpu", 0.0) >= 0.7, sleeping=self.planner.sleeping)
        if self.relation.update(self.wall().timestamp()):
            self.log(f"你和这个人的关系更近了：{self.relation.brief()}")
            self.bump("relation_up")

    # -- 感知 ---------------------------------------------------------------
    def _perceive(self, sim, now: float) -> None:
        idle = self.idle_fn()
        if idle >= AWAY_SECONDS and not self.away:
            self.away = True
            self.salience, self.reason = 0.0, None
            self.log("用户离开了电脑")
            # 人不在：本地打盹，不花一个 token
            self.planner.load([{"do": "sleep", "s": 300}], sim)
        elif self.away and idle < 5.0:
            self.away = False
            self.planner.clear(sim)
            self.log("用户回来了")
            self.bump("back")
        elif self.away and self.planner.idle:
            self.planner.load([{"do": "sleep", "s": 300}], sim)

        clock_now = self.wall()
        if (clock_now.hour >= 23 or clock_now.hour < 5) and not self.away:
            day = (clock_now.date().isoformat() if clock_now.hour >= 23
                   else f"{clock_now.date().isoformat()}-early")
            if self._late_day != day:
                self._late_day = day
                self.bump("late")

        if self.perception is None:
            return
        self._log_perception_stats(now)
        window, app_changed = self.perception.poll()
        if window is None:
            return
        if app_changed and not self.away:
            self.bump("window")
            self._long_marked = None
            desc = self.perception.describe()
            if desc:
                self.log(f"用户切到 {desc['app']}：{desc['title'][:40]}")
        minutes = (now - window.since) / 60.0
        if minutes >= 60.0 and (self._long_marked is None or now - self._long_marked >= 3600.0):
            self._long_marked = now
            self.bump("long_session")
        if not self.away:
            if not self.quiet and idle < 120.0:   # 勿扰/长时间没操作：不截图、不认字
                self.perception.maybe_ocr(now)
                if hasattr(self.perception, "maybe_probe"):
                    self.perception.maybe_probe(now)
            # 刚认出新的一屏字：给一点分，让它趁内容新鲜时说（而不是几分钟后对着旧内容说）
            version = getattr(self.perception, "ocr_version", None)
            if version is not None and version != self._ocr_seen:
                if self._ocr_seen is not None and self.perception.ocr_text():
                    self.bump("screen")
                self._ocr_seen = version

    def _log_perception_stats(self, now: float) -> None:
        """每小时一行基线计数（OCR 次数 / 耗时、observe 次数、屏幕字数、token），输出后清零。"""
        stats = getattr(self.perception, "stats", None)
        if stats is None:
            return
        if self._stats_at is None:
            self._stats_at = now
            return
        if now - self._stats_at < STATS_EVERY:
            return
        hours = (now - self._stats_at) / 3600.0
        self._stats_at = now
        n = stats["observe_n"]
        # 写 launcher.log，不写活动日志：活动日志会在反思时交给模型，基线数字对它是噪声
        self.stats_sink(f"感知基线（{hours:.1f}h）：OCR {stats['ocr_n']} 次 / {stats['ocr_s']:.1f}s，"
                 f"observe {n} 次，屏幕字 {stats['screen_chars']}（均 {stats['screen_chars'] // max(1, n)}），"
                 f"token {stats['tokens']}")
        stats.update(self._new_stats())

    # -- 观察 ---------------------------------------------------------------
    @staticmethod
    def _norm(sim, point) -> dict | None:
        if point is None:
            return None
        x, y, w, h = sim.bounds
        return {"x": round((point[0] - x) / max(1.0, w), 2),
                "y": round((point[1] - y) / max(1.0, h), 2)}

    def _slim(self, obs: dict, key: str, value, count: int, force: bool) -> None:
        """没变的列表只给个数（``<key>_unchanged``），变了或者用户在问话才给全文。"""
        if not force and self._sent_static.get(key) == value:
            obs[f"{key}_unchanged"] = count
            return
        self._sent_static[key] = value
        obs[key] = value

    def observe(self, sim, reason: str, user_text: str | None = None) -> dict:
        obs: dict = {"event": EVENT_TEXT.get(reason, reason)}
        if user_text:
            obs["user_said"] = user_text
        if self.task_workspace:
            obs["task_workspace"] = self.task_workspace
        if reason in POSITION_REASONS:
            obs.update({"you": self._norm(sim, sim.creature.chunk0), "cursor": self._norm(sim, sim.cursor)})
        obs.update({
            "time": self.wall().strftime("%H:%M"),
            "temper": round(float(sim.temper.value), 2),
            "territory": (sim.territory or {}).get("side"),
            "plan_left": None if self.planner.idle else self.planner.label(),
            "user_idle_s": int(self.idle_fn()),
        })
        if self.quiet:
            obs["quiet"] = True
        system = self.system or {}
        if (system.get("cpu") or 0.0) >= 0.7:
            obs["computer_busy"] = f"CPU {int(system['cpu'] * 100)}%"
        if system.get("battery") is not None and not system.get("charging") \
                and system["battery"] <= 30:
            obs["battery"] = f"{system['battery']}%，没在充电"
        # 提醒 / 心事：和上次发给模型的一样就只给个数（用户问话时照旧给全）
        if self.reminders is not None and self.reminders.items:
            self._slim(obs, "reminders", self.reminders.describe(), len(self.reminders.items), bool(user_text))
        elif self._sent_static.pop("reminders", None) is not None:
            obs["reminders"] = "已经没有了"
        if self.errands is not None and self.errands.items:
            self._slim(obs, "errands", [e["goal"] + (f"（你的打算：{e['notes'][:60]}）" if e.get("notes") else "")
                                        for e in self.errands.items], len(self.errands.items), bool(user_text))
        elif self._sent_static.pop("errands", None) is not None:
            obs["errands"] = "已经没有了"
        if self.perception is not None and self.perception.level != "off":
            obs["screen"] = self.perception.describe()
            if hasattr(self.perception, "screen_delta"):
                # 用户在问话就给整段（按需展开），否则只发新增行 / "没变"
                obs.update(self.perception.screen_delta(full=bool(user_text)))
            else:
                text = self.perception.ocr_text()
                if text:
                    obs["screen_text"] = text
                    age = getattr(self.perception, "ocr_age", lambda: None)()
                    if age is not None:
                        obs["screen_text_age_s"] = int(age)
            stats = getattr(self.perception, "stats", None)
            if stats is not None:
                stats["observe_n"] += 1
                stats["screen_chars"] += sum(len(obs.get(k, "")) for k in ("screen_text", "screen_new"))
        from .relation import energy_word

        obs["relation"] = self.relation.brief()
        if self.memory is not None:
            obs["affinity"] = f"{self.memory.affinity:+.2f}（-1 厌烦 … +1 亲近）"
        obs["energy"] = f"{self.context.energy:.2f}（{energy_word(self.context.energy)}）"
        quiet_for = self.clock() - self._last_auto_say
        if quiet_for < 1e8:
            obs["you_last_spoke_min"] = int(quiet_for / 60)
        remarks = self.remarks.brief(self.wall().timestamp())
        if remarks:
            obs["your_remarks"] = remarks
        if reason == "agent" and self._agent_pending:
            obs["agent_event"], self._agent_pending = self._agent_pending, None
        if reason == "arrive" and self.pearls is not None and self.pearls.unread_gifts():
            obs["gift_waiting"] = [f"{p['gift'].get('from', '')}转来的「{p['title']}」"
                                   for p in self.pearls.unread_gifts()[:3]]
        if self._extra_obs:
            obs.update(self._extra_obs)
            self._extra_obs = {}
        if reason == "reflect":
            obs["activity"] = list(self.activity)[-40:]
            if self.memory is not None and self.memory.wants_rewrite:
                obs["ask_rewrite"] = True
        return obs

    # -- 请求 ---------------------------------------------------------------
    def _chat_window(self) -> tuple[list[dict], list[dict]]:
        """(进 prompt 的最近 ``CHAT_WINDOW`` 句, 已滑出窗口、还没并进摘要的旧对话)。"""
        if self.chatlog is None:
            return [], []
        talk = self.chatlog.dialogue()
        window = talk[-CHAT_WINDOW:]
        done = self.memory.summarized_ts if self.memory is not None else float("inf")
        older = [e for e in talk[:-CHAT_WINDOW] if e.get("ts", 0) > done] if window else []
        return window, older

    def _start(self, sim, reason: str, user_text: str | None, extra: dict | None = None,
               image=None, screen_capture_attempted: bool = False) -> None:
        obs = self.observe(sim, reason, user_text)
        if screen_capture_attempted and image is None:
            obs["screen_image_error"] = "当前窗口截图未取得；只能依据本地文字回答"
        if (image is None and not screen_capture_attempted and reason == "chat" and user_text
                and self.wants_screen_vision(user_text)
                and hasattr(self.perception, "capture_vision")):
            window_info = self.perception.window
            if window_info is not None and not window_info.private:
                eyes, mode = self.perception, self.screen_vision_mode
                key = (window_info.hwnd, window_info.title)
                image = lambda: eyes.capture_vision(mode, key)
        if extra:
            obs.update(extra)
        tools = None
        if reason not in ("pearl", "reflect"):
            tools = self._chat_tools(image)
            if tools.can_look:
                obs["can_look"] = True
        window, older = self._chat_window()
        fold_upto = None
        if len(older) >= FOLD_BATCH:
            batch = older[:FOLD_MAX]
            obs["fold"] = [chat_line(e)[:160] for e in batch]
            fold_upto = batch[-1]["ts"]
        if reason == "chat" and user_text and self.chatlog is not None:
            # 最后一句就是刚说的这句（say_to 已经记进对话记录）；窗口之前的才算"旧账"
            before = window[0]["ts"] if window else float("inf")
            recall = self.chatlog.search(user_text, before_ts=before)
            if self.pearls is not None:
                recall += self.pearls.search(user_text)
            if recall:
                obs["recall"] = recall
        if reason == "chat" and user_text and self.memory is not None:
            candidates = self.memory.place_candidates(user_text)
            if candidates:
                obs["place_candidates"] = candidates
        user_msg = {"role": "user", "content": json.dumps(obs, ensure_ascii=False)}
        # 历史里存去掉大段文字的观察：屏幕文字 / 活动日志每轮都不一样，留着只会白花 token
        compact = {k: v for k, v in obs.items()
                   if k not in ("screen_text", "screen_new", "activity", "task_result", "fold", "recall", "pearl", "place_candidates")}
        if "pearl" in obs:
            compact["pearl"] = {k: obs["pearl"][k] for k in ("title", "kind")}
        brief = json.dumps(compact, ensure_ascii=False)
        messages = [{"role": "system", "content": system_prompt(sim.appearance.key)}]
        catalogue = self.habits.catalogue() if self.habits is not None else ""   # 习惯目录：不常变，吃缓存
        if self.memory is not None:
            chat_lines = [chat_line(e) for e in window]
            pearl_lines = self.pearls.prompt_lines() if self.pearls is not None else None
            messages.append({"role": "system", "content": self.memory.prompt_block(
                self.talkativeness, chat_lines, pearl_lines)
                + "\n" + self.relation.prompt_text(sim.appearance.key)
                + ("\n" + catalogue if catalogue else "")})
        elif catalogue:
            messages.append({"role": "system", "content": catalogue})
        messages.extend(self.context.turns)
        messages.append(user_msg)

        self._busy = True
        now = self.clock()
        self._last_request = self._last_sent = now
        self._next_allowed = now + URGENT_GAP
        self.salience, self.reason, self.desire = 0.0, None, 0.0
        self.requests += 1
        if reason == "reflect":
            ctx = self.context
            self._force_reflect = False
            ctx.since_reflect = 0
            ctx.active_seconds = 0.0
            ctx.reflections += 1
            ctx.last_reflect = self.wall().timestamp()
        threading.Thread(
            target=self._run, args=(reason, messages, brief, fold_upto, image, user_text, tools), daemon=True
        ).start()

    def _chat_tools(self, image):
        """这次请求能用的只读工具（chat_tools.py）。看图只在开了看图、前台不是隐私窗口、
        这次没有已经附图时给。"""
        from .chat_tools import ChatTools

        look = None
        p = self.perception
        if (image is None and self.vision_model and self.screen_vision_mode in ("roi", "full")
                and p is not None and hasattr(p, "capture_vision")):
            w = getattr(p, "window", None)
            if w is not None and not w.private:
                mode, key = self.screen_vision_mode, (w.hwnd, w.title)
                look = lambda: p.capture_vision(mode, key)       # noqa: E731
        return ChatTools(p, self.chatlog, self.pearls, self.reminders, self.errands, self.memory, look,
                         habits=self.habits)

    def _run(self, reason: str, messages: list[dict], brief: str, fold_upto, image=None,
             user_text: str | None = None, tools=None) -> None:
        """工作线程：只做 HTTP 和只读工具，不碰仿真、记忆、上下文。"""
        try:
            text, usages = self._first_reply(reason, messages, fold_upto, image)
            action = parse_action(text)
            if tools is not None:
                action, more = self._tool_rounds(reason, messages, text, action, tools)
                usages += more
            self._results.put((reason, action, usages, None, brief, fold_upto, user_text))
        except Exception as exc:                       # noqa: BLE001 —— 任何失败都不许炸主线程
            self._results.put((reason, None, None, f"{type(exc).__name__}: {exc}", brief, None, user_text))
        finally:
            self._busy = False

    def _tool_rounds(self, reason: str, messages: list[dict], text: str, action: dict, tools) -> tuple[dict, list]:
        """模型在回复里填了 need：本地取来结果，再问一次，最多 ``MAX_ROUNDS`` 轮。"""
        from .chat_tools import MAX_ROUNDS, needs

        usages: list = []
        history = [_text_only(m) for m in messages]
        for round_no in range(1, MAX_ROUNDS + 1):
            items = needs(action)
            if not items:
                break
            said = action.get("say")
            if reason in DIRECT_REASONS and isinstance(said, str) and said.strip():
                self._interim.put(said)              # 先开口（"等等，我看看"），查完再说结论
            results, image = tools.run(items)
            last = round_no == MAX_ROUNDS
            follow = {"tool_results": results,
                      "note": ("这是最后一次，给完整回复，不能再填 need。" if last
                               else "查到的结果；需要的话还能再查一轮，查够了就给完整回复。")}
            if said:
                follow["you_already_said"] = said
            history += [{"role": "assistant", "content": text},
                        {"role": "user", "content": json.dumps(follow, ensure_ascii=False)}]
            if image is not None:
                import base64

                data = base64.b64encode(image).decode("ascii")
                ask = history[:-1] + [dict(history[-1], content=[
                    {"type": "text", "text": history[-1]["content"]},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}])]
                try:
                    reply = self.client.complete(ask, max_tokens=900, model=self.vision_model)
                except Exception as exc:                   # noqa: BLE001
                    _log(f"look_screen 看图失败，只读字：{type(exc).__name__}: {exc}")
                    results["look_screen"] = "看图模型用不了，没看成。"
                    history[-1] = {"role": "user", "content": json.dumps(dict(follow, tool_results=results),
                                                                        ensure_ascii=False)}
                    reply = self.client.complete(history, max_tokens=700)
            else:
                reply = self.client.complete(history, max_tokens=700)
            text, usage = reply if isinstance(reply, tuple) else (reply, {})
            usages.append(usage)
            action = parse_action(text)
            checked = action.setdefault("checked", [])    # 进对话历史：下一轮知道自己刚查过什么
            if isinstance(checked, list):
                checked += [k for k in results if k not in checked]
        action.pop("need", None)
        return action, usages

    def _first_reply(self, reason: str, messages: list[dict], fold_upto, image) -> tuple[str, list]:
        """第一次请求（可能带图）。``messages`` 会被就地改成实际发出去的样子（退回只读字时），
        后面查工具的几轮接着它往下问。"""
        longer = fold_upto is not None or reason == "pearl" or callable(image)
        reply = None
        screen_capture = callable(image) or isinstance(image, bytes)
        if callable(image):
            try:
                image = image()
            except Exception as exc:                   # noqa: BLE001
                _log(f"当前窗口截图失败，只读字：{type(exc).__name__}: {exc}")
                image = None
            if image is None:
                messages[-1] = dict(messages[-1], content=messages[-1]["content"]
                                    + "\n（当前窗口截图未取得；只能依据本地文字回答。）")
        if image is not None:
            # 图只能放在 user 消息里：最后一条换成 [文字, 图]。看图模型不认 / 出错就退回只读字
            import base64

            try:
                data = base64.b64encode(image if isinstance(image, bytes) else image.read_bytes()).decode("ascii")
                note = ("\n（本轮附有当前窗口截图；请结合图像回答，OCR 文字可能不完整。）"
                        if screen_capture else "")
                last = dict(messages[-1], content=[
                    {"type": "text", "text": messages[-1]["content"] + note},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}])
                reply = self.client.complete(messages[:-1] + [last], max_tokens=900, model=self.vision_model)
            except Exception as exc:                   # noqa: BLE001
                _log(f"看图失败，只读字：{type(exc).__name__}: {exc}")
                reply = None
                fallback = ("截图没能传过去（看图模型用不了），只能读下面这些字" if screen_capture
                            else "图没能传过去（看图模型用不了），只能读下面这些字")
                messages[-1] = dict(messages[-1], content=messages[-1]["content"].replace(
                    IMAGE_NOTE, fallback) + ("\n" + fallback if screen_capture else ""))
        if reply is None:
            reply = self.client.complete(messages, max_tokens=900 if longer else 500)
        text, usage = reply if isinstance(reply, tuple) else (reply, {})
        return text, [usage]

    def _error_notice(self, error: str, waiting: bool) -> str | None:
        """这次失败要不要说出来、说什么。用户在等回复就一定说；否则同一类错误 ``ERROR_REPEAT`` 内只说一次。"""
        kind = error_kind(error)
        line = ERROR_LINES.get(kind)
        if line is None:
            return "……刚才没想出来。再说一遍？" if waiting else None
        now = self.clock()
        if not waiting and now - self._error_said.get(kind, -1e9) < ERROR_REPEAT:
            return None
        self._error_said[kind] = now
        return line

    def _drain_results(self, sim) -> None:
        while True:
            try:
                said = self._interim.get_nowait()
            except queue.Empty:
                break
            self._say(sim, said, direct=True, why="你跟它说话（先应一声，查完再答）")
        while True:
            try:
                reason, action, usage, error, brief, fold_upto, user_text = self._results.get_nowait()
            except queue.Empty:
                return
            if error is not None:
                if error != self.last_error:
                    _log(error)
                self.last_error = error
                self._next_allowed = self.clock() + ERROR_BACKOFF
                notice = self._error_notice(error, waiting=reason == "chat")
                if notice:
                    self._say(sim, notice, direct=True, why=f"请求出错：{error[:60]}")  # 用户在等回复 / 第一次出这类错：明说
                    continue
                if reason == "pearl":
                    self._pearl_in_flight = None
                    self._reading = ["……这颗珍珠现在读不出来。"]
                    self._next_line = 0.0
                elif reason in ("drag", "talk"):
                    # 断网 / 欠费时退回老台词池
                    line = sim.lines.next_line(sim.appearance.key, reason)
                    if line:
                        sim.say(line)
                continue
            usages = usage if isinstance(usage, list) else [usage]      # 查了工具就是好几次请求
            stats = getattr(self.perception, "stats", None)
            for one in usages:
                if self.usage is not None:
                    self.usage.record(one, source="dialogue")
                if stats is not None and one:
                    stats["tokens"] += int(one.get("total_tokens", 0) or 0)
            self.last_error = None
            self.last_action = action
            self.context.add_turn(brief, json.dumps(action or {}, ensure_ascii=False))
            if reason == "pearl":
                self._apply_pearl(sim, action or {})
            else:
                self.apply(sim, action or {}, reason, user_text=user_text)
            if fold_upto is not None and self.memory is not None \
                    and self.memory.set_summary((action or {}).get("summary"), fold_upto):
                self.memory.save()                 # 没给 summary 就下次再并
            self.save()

    # -- 动作落地（主线程） -------------------------------------------------
    def apply(self, sim, action: dict, reason: str = "chat", user_text: str | None = None) -> None:
        if reason == "agent":
            # 外来通知触发的那次：只采用说的话、心情、念头（通知内容进不了记忆 / 提醒 / 日程 / 心事）
            action = {k: action.get(k) for k in ("say", "mood", "thought")}
            if action.get("say"):
                self.planner.load([{"do": "go", "to": "cursor"}], sim)
        said = action.get("say")
        if reason == "task_done" and self._task_notice is not None:
            if not (isinstance(said, str) and said.strip()):
                said = self._task_notice[0]          # 任务结束这一轮必须有一句，模型不说就用保底的
            self._task_notice = None
        why = WHY_LABELS.get(reason, reason)
        thought_now = action.get("thought") if isinstance(action.get("thought"), str) else None
        self._say(sim, said, direct=reason in DIRECT_REASONS, why=why, thought=thought_now)
        mood = action.get("mood")
        if isinstance(mood, str) and mood.strip():
            self.mood = mood.strip()[:12]
        thought = action.get("thought")
        if isinstance(thought, str):
            self.thought = thought.strip()[:120]
            self.context.mind.add(self.wall().timestamp(), self.mood, self.thought)
        plan = action.get("plan")
        if isinstance(plan, list):
            self.planner.load(plan, sim)
            self._plan_made = (self.clock(), self._current_window())
            self._plan_why = (why, thought_now)

        if self.reminders is not None:
            remind = action.get("remind")
            for spec in (remind if isinstance(remind, list) else [remind]):
                if isinstance(spec, dict) and self.reminders.add_spec(spec):
                    self.log(f"你答应提醒：{spec.get('text')}")
        errand = action.get("errand")
        for goal in (errand if isinstance(errand, list) else [errand]):
            if isinstance(goal, str) and goal.strip():
                self.add_errand(goal)

        task = action.get("task")
        if (reason == "chat" and user_text and self.on_task_request is not None
                and isinstance(task, dict)):
            goal = task.get("goal")
            mode = task.get("mode")
            if isinstance(goal, str) and goal.strip() and mode in ("read-only", "workspace-write"):
                workspace = task.get("workspace")
                if self.memory is not None:          # 填的是地点簿里的名字：本地换算成路径
                    workspace = self.memory.resolve_place(workspace) or workspace
                self.on_task_request({"goal": goal.strip()[:400], "mode": mode,
                                      "workspace": workspace}, user_text)

        self._apply_habits(sim, action, reason, user_text)

        memory = self.memory
        if memory is None:
            return
        changed = False
        learn = action.get("learn")
        if reason == "chat" and user_text and isinstance(learn, dict):
            # 只在用户亲口说话的那一轮收：屏幕上看到的、别的来源的都不记；路径必须真实存在
            if memory.add_place(learn.get("place"), learn.get("path")):
                self.log(f"记下一个地点：{learn.get('place')}")
                changed = True
            if memory.add_alias(learn.get("alias"), learn.get("meaning")):
                self.log(f"记下一个别名：{learn.get('alias')}")
                changed = True
        remember = action.get("remember")
        for fact in (remember if isinstance(remember, list) else [remember]):
            if isinstance(fact, str):
                changed |= memory.remember(fact)
        if action.get("affinity") is not None:
            memory.nudge_affinity(action.get("affinity"))
            changed = True
        if isinstance(action.get("diary"), str):
            changed |= memory.write_diary(action["diary"])
        if isinstance(action.get("goal"), str):
            changed |= memory.set_goal(action["goal"])
        if action.get("profile_rewrite") is not None:
            changed |= memory.rewrite_profile(action.get("profile_rewrite"))
        if changed:
            memory.save()

    # -- 习惯（设计稿 §6） ----------------------------------------------------
    def _apply_habits(self, sim, action: dict, reason: str, user_text: str | None) -> None:
        if self.habits is None:
            return
        edit = action.get("habit_edit")
        if edit is not None:
            # 只认用户亲口说话的那一轮（H8、H9）：屏幕 / 网页 / 通知引出的回复里就算有，也丢掉
            if reason == "chat" and user_text and isinstance(edit, dict):
                self._habit_edit(sim, edit, user_text)
            else:
                _log(f"丢弃 habit_edit（reason={reason}，不是用户亲口要求的）：{str(edit)[:80]}")
        use = action.get("habit_use")
        if isinstance(use, dict):
            if reason == "chat" and user_text:
                self._habit_use(sim, use, user_text)
            else:
                _log(f"丢弃 habit_use（reason={reason}）")
        suggest = action.get("habit_suggest")
        if reason == "task_done" and isinstance(suggest, str) and suggest.strip():
            self._say(sim, suggest.strip(), direct=True, why="觉得这件事会反复出现")

    def _habit_lookup(self, name: str):
        return self.habits.find(name)

    def _habit_use(self, sim, use: dict, user_text: str) -> None:
        """chat 触发的 inline：把这句话记进习惯的 notes。程序执行，不额外请求模型（§5.1）。"""
        habit = self._habit_lookup(str(use.get("name") or ""))
        trigger = next((t for t in habit.triggers("chat") if t["id"] == use.get("trigger")), None) if habit else None
        if habit is None or trigger is None or not habit.enabled:
            _log(f"habit_use 对不上（{str(use)[:80]}）")
            return
        text = use.get("input") if isinstance(use.get("input"), str) and use["input"].strip() else user_text
        try:
            path = self.habits.append_note(habit.name, self.habits.note_file(habit), text.strip()[:400])
        except OSError as exc:
            _log(f"habit_use 写不进去：{exc}")
            return
        self.habits.append_log(habit.name, {"trigger": trigger["id"], "kind": "inline", "tokens": 0, "steps": 0,
                                            "result": "ok", "summary": text.strip()[:80], "files": [path]})
        if self.habit_state is not None:
            self.habit_state.mark_run(habit.name, trigger["id"], self.wall().timestamp())
        self.habits.commit(f"记一笔：{habit.name}", f"habits/{habit.name}/notes", f"habits/{habit.name}/log.jsonl")
        self.log(f"习惯「{habit.title}」记了一笔：{text.strip()[:40]}")

    def _habit_edit(self, sim, edit: dict, user_text: str) -> None:
        from .habits.store import HabitError

        op = edit.get("op")
        name = str(edit.get("name") or "").strip()
        key = sim.appearance.key
        if op in ("create", "update"):
            if self._habit_busy:
                self._say(sim, HABIT_LINES[key]["busy"], direct=True, why="习惯还在写")
                return
            existing = None
            if op == "update":
                habit = self._habit_lookup(name)
                if habit is None:
                    self._say(sim, HABIT_LINES[key]["unknown"], direct=True, why="没找到要改的习惯")
                    return
                try:
                    habit = self.habits.get(habit.name)         # get() 会先把手改过的内容记一次快照
                except HabitError as exc:
                    self._say(sim, HABIT_LINES[key]["failed"], direct=True, why=f"习惯读不了：{exc}")
                    return
                existing = {"name": habit.name, "skill_md": (habit.path / "SKILL.md").read_text(encoding="utf-8"),
                            "habit_json": habit.config, "requests": self.habits.requests(habit.name)}
                name = habit.name
            pending, self._habit_question = self._habit_question, None
            if pending:
                user_text = f"{pending['text']}\n（补充）{user_text}"
            taken = [h.name for h in self.habits.list()]
            self._habit_busy = True
            threading.Thread(target=self._run_habit_author,
                             args=(op, name, user_text, existing, taken, key), daemon=True).start()
            return
        if op not in ("pause", "resume", "revert", "delete"):
            _log(f"habit_edit 的 op 不认识：{op!r}")
            return
        habit = self._habit_lookup(name)
        if habit is None:
            self._say(sim, HABIT_LINES[key]["unknown"], direct=True, why="没找到要动的习惯")
            return
        try:
            if op == "pause":
                self.habits.set_enabled(habit.name, False)
            elif op == "resume":
                self.habits.set_enabled(habit.name, True)
                if self.habit_state is not None:
                    self.habit_state.set_paused(habit.name, "")
            elif op == "revert":
                self.habits.revert(habit.name)
            else:
                self.habits.delete(habit.name)
                if self.habit_state is not None:
                    self.habit_state.forget(habit.name)
        except HabitError as exc:
            self._say(sim, HABIT_LINES[key]["failed"], direct=True, why=f"习惯没动成：{exc}")
            if self.chatlog is not None:
                self.chatlog.add("system", f"习惯「{habit.title}」没动成：{exc}")
            return
        self.log(f"习惯「{habit.title}」{HABIT_OPS[op]}（用户要求）")
        if self.chatlog is not None:
            self.chatlog.add("system", f"习惯「{habit.title}」{HABIT_OPS[op]}")
        self._say(sim, HABIT_LINES[key][op], direct=True, why="你要求动习惯")

    def _run_habit_author(self, op, name, user_text, existing, taken, character) -> None:
        """工作线程：只有 HTTP。写文件放回主线程（``_drain_habits``）。"""
        from .habits import author

        try:
            cap = self.daily_token_cap if self.daily_token_cap > 0 else 0
            result = author.author(self.client, character, user_text, existing, name=name, taken=taken, day_cap=cap)
            self._habit_results.put((op, name, user_text, result))
        except Exception as exc:                       # noqa: BLE001
            self._habit_results.put((op, name, user_text, exc))
        finally:
            self._habit_busy = False

    def _drain_habits(self, sim) -> None:
        from .habits.store import HabitError

        while True:
            try:
                op, hint, user_text, result = self._habit_results.get_nowait()
            except queue.Empty:
                return
            lines = HABIT_LINES[sim.appearance.key]
            if isinstance(result, Exception):
                _log(f"HabitAuthor 出错：{type(result).__name__}: {result}")
                self._say(sim, lines["failed"], direct=True, why="习惯没写成")
                continue
            for usage in result.usages:
                if self.usage is not None:
                    self.usage.record(usage, source="habit")
            if not result.ok:
                _log(f"HabitAuthor 放弃：{result.error}")
                if self.chatlog is not None:
                    self.chatlog.add("system", f"习惯没{'建' if op == 'create' else '改'}成：{result.error}")
                self._say(sim, lines["failed"], direct=True, why="习惯没写成")
                continue
            if result.questions:
                self._habit_question = {"text": user_text, "op": op, "name": hint}
                self._say(sim, result.questions[0], direct=True, why="要建 / 改习惯，有件事得先问你")
                continue
            verb = "建好了" if op == "create" else "改好了"
            try:
                habit = self.habits.write(result.name, result.skill_md, result.habit_json, user_text, kind=op,
                                          message=f"{'新建' if op == 'create' else '修改'}：{result.name}（{result.change_summary}）")
            except HabitError as exc:
                _log(f"习惯写入失败：{exc}")
                self._say(sim, lines["failed"], direct=True, why="习惯没写成")
                continue
            self.log(f"习惯「{habit.title}」{verb}：{result.change_summary}")
            if self.chatlog is not None:
                self.chatlog.add("system", f"习惯「{habit.title}」{verb}：{result.change_summary}（不对就说“改回去”）")
            self._say(sim, lines["done"].format(verb=verb, what=result.change_summary[:16]),
                      direct=True, why="你让我养成 / 改的习惯写好了")

    def status(self) -> dict:
        perception = self.perception
        return {
            "busy": self._busy,
            "requests": self.requests,
            "reflections": self.reflections,
            "last_reflect": self.context.last_reflect,
            "reflect_in_minutes": max(0, int((REFLECT_EVERY - self.context.active_seconds) / 60)),
            "activity_since_reflect": self.context.since_reflect,
            "reflect_pending": self._force_reflect,
            "last_error": self.last_error,
            "mood": self.mood,
            "thought": self.thought,
            "mind_log": self.context.mind.recent(10),
            "perception_stats": dict(getattr(perception, "stats", None) or {}) or None,
            "relation": self.relation.brief(),
            "relation_next": self.relation.next_needs(self.wall().timestamp()),
            "energy": round(self.context.energy, 2),
            "talk_scale": round(self.remarks.scale, 2),
            "remarks": self.remarks.brief(self.wall().timestamp()),
            "plan": self.planner.label(),
            "desire": round(self.desire, 2),
            "salience": round(self.salience, 2),
            "away": self.away,
            "quiet": self.quiet,
            "last_said": self.last_said,
            "muted_until": self.mute_until if self.muted_today else None,
            "over_budget": self.over_budget,
            "reminders": (self.reminders.describe() if self.reminders is not None else [])
                         + (self.errands.describe() if self.errands is not None else []),
            "window": perception.describe() if perception is not None else None,
            "ocr": perception.ocr_text(max_age=None) if perception is not None else None,
            "ocr_error": perception.ocr_error if perception is not None else None,
        }
