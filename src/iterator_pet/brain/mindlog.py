"""念头记录：``characters\\<角色>\\mind.json``，最近 ``KEEP`` 条（心情 + 念头）。

只给界面回看用（AI 面板"念头回看"）。**不进任何 prompt、不进对话记录 / 记忆摘要 / recall**：
念头是模型写给自己的、"用户看不到"的话，混进对话里它会开始演给人看。
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

KEEP = 50


class MindLog:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else None
        self.items: deque[dict] = deque(maxlen=KEEP)
        if self.path is not None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.items.extend(r for r in raw if isinstance(r, dict) and r.get("thought"))
            except (OSError, ValueError, TypeError):
                pass

    def add(self, ts: float, mood: str, thought: str) -> bool:
        """记一条；和上一条念头相同就不重复记。返回是否记了。"""
        thought = thought.strip()
        if not thought or (self.items and self.items[-1]["thought"] == thought):
            return False
        self.items.append({"ts": round(ts, 1), "mood": mood, "thought": thought})
        self.save()
        return True

    def recent(self, count: int = 10) -> list[dict]:
        return list(self.items)[-count:][::-1]          # 新的在前

    def clear(self) -> None:
        self.items.clear()
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.items), ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass
