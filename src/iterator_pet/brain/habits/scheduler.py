"""time / event 触发的判断（设计稿 §4.2、§4.3）：只算“哪些到点了”，不执行；执行和记账在导演里。

* ``days``：daily / weekdays / mon,wed / every:3d；
* ``window: cheap``：取 ``at`` 之后、落在 ``cheap_hours``（如 ``00:30-08:30``）里的第一个时刻；``cheap_hours`` 为空就准点；
* 错过了（桌宠没开 / 你不在）：``CATCH_UP`` 内补跑一次，超过就跳过并在 log 里记一笔；
* 新习惯 / 新触发第一次被看到时只记个起点，不会因为“今天这个点已经过了”立刻补跑；
* 你在全屏 / 勿扰：task 照常，inline（要开口的）等到你退出再说。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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
    def __init__(self, store, state, cheap_hours: str = ""):
        self.store = store
        self.state = state
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
        for habit in self._active():
            for trig in habit.config["triggers"]:
                if trig["type"] == "time":
                    item = self._due_time(habit, trig, now, stamp)
                elif trig["type"] == "event":
                    item = self._due_event(habit, trig, now, stamp, events, away)
                else:
                    continue
                if item is None:
                    continue
                if trig["run"] == "inline" and (quiet or away):
                    continue
                out.append(item)
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
        if event == "user_back":
            return Due(habit.name, habit.title, trig, stamp) if "user_back" in events else None
        if event == "day_start":
            if away:
                return None
            last = self.state.last_run(habit.name, trig["id"])
            if last and datetime.fromtimestamp(last).date() == now.date():
                return None
            return Due(habit.name, habit.title, trig, stamp)
        return None
