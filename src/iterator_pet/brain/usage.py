"""Token 用量记账：按天 + 累计，存 ``%LOCALAPPDATA%\\IteratorPet\\usage.json``。

字段直接取自 DeepSeek 的 ``usage``：``prompt_tokens`` / ``completion_tokens`` /
``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``（别家接口没有后两个就当全未命中）。
金额只是**估算**（单价在 config 里，可能过时）；真余额以面板里查到的 ``/user/balance`` 为准。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

KEYS = ("calls", "prompt", "completion", "cache_hit", "cache_miss")
KEEP_DAYS = 60


def _empty() -> dict:
    return {key: 0 for key in KEYS}


class UsageTracker:
    def __init__(self, path: Path | str | None = None, today=date.today):
        if path is None:
            from ..config import config_dir

            path = config_dir() / "usage.json"
        self.path = Path(path)
        self.today = today
        self.days: dict[str, dict] = {}
        self.total = _empty()
        self.by_source: dict[str, dict[str, dict]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for day, row in (raw.get("days") or {}).items():
            if isinstance(row, dict):
                self.days[str(day)] = {key: int(row.get(key, 0) or 0) for key in KEYS}
        total = raw.get("total")
        if isinstance(total, dict):
            self.total = {key: int(total.get(key, 0) or 0) for key in KEYS}
        for day, sources in (raw.get("by_source") or {}).items():
            if isinstance(sources, dict):
                self.by_source[str(day)] = {
                    str(name): {key: int(row.get(key, 0) or 0) for key in KEYS}
                    for name, row in sources.items() if isinstance(row, dict)}

    def save(self) -> None:
        for old in sorted(self.days)[:-KEEP_DAYS]:
            del self.days[old]
            self.by_source.pop(old, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"days": self.days, "total": self.total,
                                             "by_source": self.by_source}, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass

    def record(self, usage: dict | None, source: str = "other") -> None:
        usage = usage or {}
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        miss = int(usage.get("prompt_cache_miss_tokens", prompt - hit) or 0)
        row = {
            "calls": 1,
            "prompt": prompt,
            "completion": int(usage.get("completion_tokens", 0) or 0),
            "cache_hit": hit,
            "cache_miss": max(0, miss),
        }
        day = self.days.setdefault(self.today().isoformat(), _empty())
        source_day = self.by_source.setdefault(self.today().isoformat(), {}).setdefault(source, _empty())
        for key, value in row.items():
            day[key] += value
            self.total[key] += value
            source_day[key] += value
        self.save()

    def today_row(self) -> dict:
        return dict(self.days.get(self.today().isoformat(), _empty()))

    def today_tokens(self) -> int:
        row = self.today_row()
        return row["prompt"] + row["completion"]

    def today_by_source(self) -> dict[str, dict]:
        return {name: dict(row) for name, row in self.by_source.get(self.today().isoformat(), {}).items()}

    @staticmethod
    def cost(row: dict, prices: tuple[float, float, float]) -> float:
        """估算花费。``prices`` = (输入命中, 输入未命中, 输出)，单位：元 / 百万 token。"""
        hit, miss, out = prices
        return (row["cache_hit"] * hit + row["cache_miss"] * miss + row["completion"] * out) / 1e6
