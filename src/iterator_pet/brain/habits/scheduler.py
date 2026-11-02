"""time / event 触发的判断（设计稿 §4.2、§4.3）：只算“哪些到点了”，不执行；执行和记账在导演里。

* ``days``：daily / weekdays / mon,wed / every:3d；
* ``window: cheap``：取 ``at`` 之后、落在 ``cheap_hours``（如 ``00:30-08:30``）里的第一个时刻；``cheap_hours`` 为空就准点；
* 错过了（桌宠没开 / 你不在）：``CATCH_UP`` 内补跑一次，超过就跳过并在 log 里记一笔；
* 新习惯 / 新触发第一次被看到时只记个起点，不会因为“今天这个点已经过了”立刻补跑；
* event 触发：user_back / day_start / task_done / reflect 是桌宠自己的事件；窗口、进程、文件之类的和心事共用 ``Watcher``；
  同一个事件 10 分钟内最多触发一次（day_start 一天一次）；
* 你在全屏 / 勿扰：task 照常，inline（要开口的）等到你退出再说。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .store import WATCH_EVENTS, WATCH_PARAMS

EVENT_DEBOUNCE = 600.0       # 同一个 event 触发两次之间至少隔这么久（防抖）
CATCH_UP = 12 * 3600.0
LATE_AFTER = 600.0            # 比预定时刻晚了这么久才算“补跑”
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class Due:
    name: str
    title: str
    trigger: dict
    scheduled: float          # 预定时刻（时间戳）；event 触发是触发那一刻
    late: bool = False
    why: str = ""            # event 触发的原因（Watcher 给的一句话）

    @property
    def run(self) -> str:
        return self.trigger["run"]

    @property
    def id(self) -> str:
        return self.trigger["id"]


def parse_cheap_hours(text: str) -> tuple[int, int] | None:
    """``"00:30-08:30"`` → (起, 止) 的分钟数；写错 / 空 / 起止相同都当没设。"""
    match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*[-~–—]\s*([01]?\d|2[0-3]):([0-5]\d)\s*", text or "")
    if not match:
        return None
    start = int(match[1]) * 60 + int(match[2])
    end = int(match[3]) * 60 + int(match[4])
    return None if start == end else (start, end)


def _in_window(minute: int, window: tuple[int, int]) -> bool:
    start, end = window
    return start <= minute < end if start < end else (minute >= start or minute < end)


def scheduled_at(day: date, at: str, window: str | None, cheap: tuple[int, int] | None) -> datetime:
    """``day`` 这一天的 ``at`` 触发，实际该在什么时刻跑。"""
    base = datetime.combine(day, datetime.min.time()) + timedelta(hours=int(at[:2]), minutes=int(at[3:]))
    if window != "cheap" or cheap is None:
        return base
    minute = base.hour * 60 + base.minute
    if _in_window(minute, cheap):
        return base
    # 离 at 最近的、之后的窗口起点（可能在第二天凌晨）
    start = cheap[0]
    nxt = datetime.combine(day, datetime.min.time()) + timedelta(minutes=start)
    return nxt if nxt > base else nxt + timedelta(days=1)


def day_matches(days: str, day: date, last_run: float | None) -> bool:
    if days == "daily":
        return True
    if days == "weekdays":
        return day.weekday() < 5
    if days.startswith("every:"):
        step = int(days[6:-1])
        return last_run is None or (day - datetime.fromtimestamp(last_run).date()).days >= step
    return DAY_NAMES[day.weekday()] in days.split(",")


class HabitScheduler:
    def __init__(self, store, state, cheap_hours: str = "", watcher=None):
        self.store = store
        self.state = state
        self._held: dict[str, Due] = {}      # 勿扰 / 你不在时触发的 inline 事件，等你回来再给
        self.watcher = watcher             # errands.Watcher（没给就不盯窗口 / 进程 / 文件）
        self.cheap = parse_cheap_hours(cheap_hours)

    def set_cheap_hours(self, text: str) -> None:
        self.cheap = parse_cheap_hours(text)

    def _active(self):
        for habit in self.store.list():
            if habit.enabled and not self.state.paused(habit.name):
                yield habit

    def due(self, now: datetime, quiet: bool = False, away: bool = False, events=()) -> list[Due]:
        """现在该跑哪些。``quiet`` / ``away`` 时 inline 的先不给（等你回来再说）；task 照给。
        ``events``：这一刻刚发生的内部事件（``user_back`` 等）。day_start 自己判断：你在场的这一天第一次。"""
        out: list[Due] = []
        stamp = now.timestamp()
        watch: dict[str, tuple] = {}
        for habit in self._active():
            for trig in habit.config["triggers"]:
                if trig["type"] == "time":
                    item = self._due_time(habit, trig, now, stamp)
                elif trig["type"] == "event" and trig["event"] in WATCH_EVENTS:
                    watch[f"habit:{habit.name}:{trig['id']}"] = (habit, trig)       # 下面一起交给 Watcher
                    continue
                elif trig["type"] == "event":
                    item = self._due_event(habit, trig, now, stamp, events, away)
                else:
                    continue
                if item is None:
                    continue
                if trig["run"] == "inline" and (quiet or away):
                    continue
                out.append(item)
        out += self._due_watch(watch, stamp, quiet, away)
        return out

    def _due_watch(self, watch: dict, stamp: float, quiet: bool, away: bool) -> list[Due]:
        if not watch or self.watcher is None:
            return []
        pseudo = [{"id": key, "status": "waiting",
                   "triggers": [{"type": t["event"], **{k: t[k] for k in WATCH_PARAMS if k in t}}]}
                  for key, (_, t) in watch.items()]
        out = []
        if not (quiet or away):
            out += [d for key, d in self._held.items() if key in watch]
            self._held.clear()
        for errand, why in self.watcher.poll(pseudo):            # 每次都 poll，基准才会跟着走；防抖在下面
            habit, trig = watch[errand["id"]]
            self.watcher.reset(errand["id"])                      # 触发过就重取基准：这类事可能再发生
            last = self.state.last_run(habit.name, trig["id"])
            if last and stamp - last < EVENT_DEBOUNCE:
                continue
            due = Due(habit.name, habit.title, trig, stamp, why=why)
            if trig["run"] == "inline" and (quiet or away):
                self._held[errand["id"]] = due
                continue
            out.append(due)
        return out

    def _due_time(self, habit, trig, now: datetime, stamp: float) -> Due | None:
        last = self.state.last_run(habit.name, trig["id"])
        if not last:
            self.state.mark_run(habit.name, trig["id"], stamp)       # 第一次见到：只记起点
            return None
        # 最近一个已经过了的预定时刻（cheap 可能把它推到第二天凌晨，所以多看两天）
        best = None
        for back in range(0, 3):
            day = now.date() - timedelta(days=back)
            if not day_matches(trig["days"], day, last):
                continue
            at = scheduled_at(day, trig["at"], trig.get("window"), self.cheap)
            if at <= now and (best is None or at > best):
                best = at
        if best is None or best.timestamp() <= last:
            return None
        age = stamp - best.timestamp()
        if age > CATCH_UP:
            self.state.mark_run(habit.name, trig["id"], stamp)
            self.store.append_log(habit.name, {"trigger": trig["id"], "kind": trig["run"], "result": "skipped",
                                               "summary": f"错过了 {best:%m-%d %H:%M} 的预定时刻（超过 12 小时），跳过"})
            return None
        return Due(habit.name, habit.title, trig, best.timestamp(), late=age > LATE_AFTER)

    def _due_event(self, habit, trig, now: datetime, stamp: float, events, away: bool) -> Due | None:
        event = trig["event"]
        if event in ("user_back", "task_done", "reflect"):
            if event not in events:
                return None
            last = self.state.last_run(habit.name, trig["id"])
            return None if last and stamp - last < EVENT_DEBOUNCE else Due(habit.name, habit.title, trig, stamp)
        if event == "day_start":
            if away:
                return None
            last = self.state.last_run(habit.name, trig["id"])
            if last and datetime.fromtimestamp(last).date() == now.date():
                return None
            return Due(habit.name, habit.title, trig, stamp)
        return None
