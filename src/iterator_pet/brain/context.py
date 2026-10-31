"""一个角色的"工作上下文"：``characters\\<角色>\\context.json``。

原来这些都只在内存里，每次启动清零——反思的计时也跟着清零，必须连续开满 30 分钟才会触发，
所以实际上几乎从没反思过。现在存盘，重启接着算：
* ``turns``：最近几轮原始请求/回复（观察已去掉大段屏幕文字），让它记得刚才在想什么；
* ``activity``：活动日志，反思时回顾；``since_reflect`` 自上次反思以来记了几条；
* ``active_seconds``：自上次反思以来**用户在场**的累计秒数（离开时不算）；
* ``reflections`` / ``last_reflect``：一共反思了几次、上一次是什么时候（时间戳）；
* ``mood`` / ``thought``：重启后心情不突变。
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from .mindlog import MindLog

TURNS_KEEP = 4                 # 批量截断后留几轮（一轮 = 一问一答）
TURNS_MAX = 12                 # 涨到几轮时一次砍回 TURNS_KEEP：前缀在两次截断之间只追加，缓存才吃得上
ACTIVITY_KEEP = 80


class Context:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else None
        self.turns: deque[dict] = deque()
        self.activity: deque[str] = deque(maxlen=ACTIVITY_KEEP)
        self.since_reflect = 0
        self.active_seconds = 0.0
        self.reflections = 0
        self.last_reflect = 0.0
        self.mood = "平静"
        self.thought = ""
        self.remarks: list[dict] = []    # 主动说过的话和你的反应（remarks.py）
        self.talk_scale = 1.0            # 开口间隔倍率：嫌烦变大、被回应变小
        self.relation: dict = {}         # 关系阶段的进度（relation.py）
        self.energy = 0.8                # 精力（relation.py）
        # 念头记录（mindlog.py）：单独一个文件，只给界面回看，不进 prompt
        self.mind = MindLog(self.path.with_name("mind.json") if self.path is not None else None)
        self.load()

    def load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        turns = [m for m in raw.get("turns") or []
                 if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                 and isinstance(m.get("content"), str)]
        if turns and turns[0]["role"] == "assistant":
            turns = turns[1:]                      # 必须以 user 开头，一问一答
        self.turns.extend(turns)
        self._trim_turns()
        self.activity.extend(str(a) for a in raw.get("activity") or [])
        self.mood = str(raw.get("mood") or self.mood)[:12]
        self.thought = str(raw.get("thought") or "")[:120]
        self.remarks = [r for r in raw.get("remarks") or [] if isinstance(r, dict) and "t" in r][-20:]
        self.relation = raw.get("relation") if isinstance(raw.get("relation"), dict) else {}
        try:
            self.since_reflect = int(raw.get("since_reflect", 0))
            self.active_seconds = float(raw.get("active_seconds", 0.0))
            self.reflections = int(raw.get("reflections", 0))
            self.last_reflect = float(raw.get("last_reflect", 0.0))
            self.talk_scale = min(3.0, max(0.6, float(raw.get("talk_scale", 1.0))))
            self.energy = min(1.0, max(0.0, float(raw.get("energy", 0.8))))
        except (TypeError, ValueError):
            pass

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "mood": self.mood,
            "thought": self.thought,
            "reflections": self.reflections,
            "last_reflect": self.last_reflect,
            "active_seconds": round(self.active_seconds, 1),
            "since_reflect": self.since_reflect,
            "talk_scale": round(self.talk_scale, 3),
            "energy": round(self.energy, 3),
            "relation": self.relation,
            "remarks": self.remarks,
            "activity": list(self.activity),
            "turns": list(self.turns),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def clear(self) -> None:
        """清空记忆时一起清：最近几轮、活动日志、心情。反思次数留着（只是个计数）。"""
        self.turns.clear()
        self.activity.clear()
        self.since_reflect = 0
        self.active_seconds = 0.0
        self.mood, self.thought = "平静", ""
        self.remarks, self.talk_scale, self.relation = [], 1.0, {}
        self.mind.clear()
        self.save()

    def add_turn(self, user: str, assistant: str) -> None:
        self.turns.append({"role": "user", "content": user})
        self.turns.append({"role": "assistant", "content": assistant})
        self._trim_turns()

    def _trim_turns(self) -> None:
        """只追加、批量截断：不再每轮挤掉最老的一轮（那样每次请求的前缀都不同，整段缓存全废）。"""
        if len(self.turns) > TURNS_MAX * 2:
            for _ in range(len(self.turns) - TURNS_KEEP * 2):
                self.turns.popleft()
