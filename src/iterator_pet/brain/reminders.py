"""提醒 / 番茄钟。模型只负责翻译，计时在本地。

"半小时后叫我喝水" → ``remind``，到点本地响。"等某件事发生了再告诉我"是心事，见 ``errands.py``。

存 ``%LOCALAPPDATA%\\IteratorPet\\reminders.json``（两个角色共用），重启不丢；过期没响的提醒
（比如关机期间到点）下次启动立刻响一次，超过 12 小时的就算了。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

STALE_SECONDS = 12 * 3600.0
MAX_ITEMS = 50


class Reminders:
    def __init__(self, path: Path | str | None = None, now=time.time, wall=datetime.now):
        if path is None:
            from ..config import config_dir

            path = config_dir() / "reminders.json"
        self.path = Path(path)
        self.now = now
        self.wall = wall
        self.items: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        # 0.3.0 存的是 {"timed": [...], "watches": [...]}；更早是一个列表。盯屏幕的旧条目不再要了
        timed = raw if isinstance(raw, list) else (raw.get("timed") or [] if isinstance(raw, dict) else [])
        self.items = [r for r in timed if isinstance(r, dict) and r.get("text")
                      and isinstance(r.get("due"), (int, float))]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"timed": self.items}, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass

    # -- 写入 -------------------------------------------------------------------
    def due_from_spec(self, spec: dict) -> float | None:
        """``{"in_min": 30}`` 或 ``{"at": "21:30"}`` → 到点的时间戳。"""
        try:
            if spec.get("in_min") is not None:
                minutes = float(spec["in_min"])
                if 0 < minutes <= 24 * 60:
                    return self.now() + minutes * 60.0
                return None
            if isinstance(spec.get("at"), str):
                hour, minute = (int(x) for x in spec["at"].strip().split(":")[:2])
                wall = self.wall()
                target = wall.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= wall:
                    target += timedelta(days=1)
                return self.now() + (target - wall).total_seconds()
        except (TypeError, ValueError):
            return None
        return None

    def add_spec(self, spec) -> bool:
        if not isinstance(spec, dict):
            return False
        text = str(spec.get("text") or "").strip()[:60]
        due = self.due_from_spec(spec)
        if not text or due is None:
            return False
        self.items.append({"due": due, "text": text})
        self.items.sort(key=lambda r: r["due"])
        del self.items[MAX_ITEMS:]
        self.save()
        return True

    def clear(self) -> None:
        self.items = []
        self.save()

    # -- 读出 -------------------------------------------------------------------
    def pop_due(self) -> list[str]:
        now = self.now()
        due = [r for r in self.items if r["due"] <= now]
        if not due:
            return []
        self.items = [r for r in self.items if r["due"] > now]
        self.save()
        return [r["text"] for r in due if now - r["due"] <= STALE_SECONDS]

    def describe(self, limit: int = 5) -> list[str]:
        """给模型 / 面板看：还没到点的提醒。"""
        return [f"{datetime.fromtimestamp(r['due']).strftime('%H:%M')} {r['text']}" for r in self.items[:limit]]
