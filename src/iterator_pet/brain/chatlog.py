"""对话记录：气泡几秒就收起，这里留一份能翻回去看的。

``characters\\<角色>\\chat.jsonl``（见 ``profiles.py``），一行一条 ``{"ts", "who", "text"}``；
``who`` = user（你说的）/ pet（它说的）/ task（委托任务的进度与结果）/ system。
内存里只留最近 ``KEEP`` 条；文件留最近 ``FILE_KEEP`` 行，``search()`` 翻旧账就翻这个文件。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

KEEP = 300
FILE_KEEP = 6000
SPEAKERS = {"user": "这个人", "pet": "你"}

# 翻旧账用的"词"：中文按相邻两字切，英文/数字按词。太常见的两字词不算命中。
_CJK = re.compile(r"[一-鿿]+")
_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_COMMON = frozenset(
    "我们 你们 他们 这个 那个 什么 一个 就是 没有 不是 可以 知道 现在 时候 今天 怎么 还是 "
    "已经 觉得 因为 所以 但是 然后 如果 这样 那样 一下 有点 真的 自己 东西 好的 不要 不会 "
    "这么 那么 为什么 一点 还有 可能 应该 需要 的话 了吗 是不 不是 么呢".split()
)


def terms(text: str) -> set[str]:
    out = set()
    for run in _CJK.findall(text or ""):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    out.update(word.lower() for word in _WORD.findall(text or ""))
    return out - _COMMON


class ChatLog:
    def __init__(self, path: Path | str | None = None, now=time.time):
        if path is None:
            from ..config import config_dir

            path = config_dir() / "chat.jsonl"
        self.path = Path(path)
        self.now = now
        self.entries: list[dict] = []
        self.version = 0          # 每加一条 +1，窗口靠它判断要不要刷新
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-KEEP:]:
            entry = _parse(line)
            if entry is not None:
                self.entries.append(entry)
        if len(lines) > FILE_KEEP * 1.25:
            try:
                self.path.write_text("\n".join(lines[-FILE_KEEP:]) + "\n", encoding="utf-8")
            except OSError:
                pass

    def add(self, who: str, text: str) -> dict:
        entry = {"ts": round(self.now(), 1), "who": who, "text": str(text)}
        self.entries.append(entry)
        del self.entries[:-KEEP]
        self.version += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry

    def clear(self) -> None:
        self.entries = []
        self.version += 1
        try:
            self.path.unlink()
        except OSError:
            pass

    # -- 给导演用 -------------------------------------------------------------------
    def dialogue(self) -> list[dict]:
        """只要你来我往的那部分（不含任务输出和系统提示）。"""
        return [e for e in self.entries if e.get("who") in SPEAKERS]

    def search(self, query: str, before_ts: float, limit: int = 3, min_hits: int = 2) -> list[str]:
        """在 ``before_ts`` 之前的全部对话里找和 ``query`` 最相关的几段（带上下一句回应）。"""
        wanted = terms(query)
        if not wanted:
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        talk = [e for e in (_parse(line) for line in lines)
                if e is not None and e.get("who") in SPEAKERS and e.get("ts", 0) < before_ts]
        scored = []
        for i, entry in enumerate(talk):
            hits = len(wanted & terms(entry["text"]))
            if hits >= min_hits:
                scored.append((hits, entry["ts"], i))
        scored.sort(reverse=True)
        picked, used = [], set()
        for _hits, _ts, i in scored:
            if i in used or len(picked) >= limit:
                continue
            group = [talk[i]]
            if i + 1 < len(talk) and talk[i + 1]["who"] != talk[i]["who"]:
                group.append(talk[i + 1])
                used.add(i + 1)
            used.add(i)
            picked.append((talk[i]["ts"], " / ".join(line(e)[:120] for e in group)))
        return [text for _ts, text in sorted(picked)]


def line(entry: dict) -> str:
    """``[10-30 21:04] 这个人：……``"""
    stamp = datetime.fromtimestamp(entry.get("ts", 0)).strftime("%m-%d %H:%M")
    return f"[{stamp}] {SPEAKERS.get(entry.get('who'), entry.get('who'))}：{entry.get('text', '')}"


def _parse(raw: str) -> dict | None:
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) and entry.get("text") else None
