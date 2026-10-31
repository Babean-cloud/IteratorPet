"""记忆：``characters\\<角色>\\memory.json``（见 ``profiles.py``），纯本地，可以直接打开改。

都不用向量库（桌宠用不着，塞一份短档案进 prompt 更省也更稳）：
* **档案** ``profile``：关于用户的事实，每条一句。模型回复里带 ``remember`` 就追加；
  超过 ``PROFILE_SOFT_CAP`` 条时在下一次请求里让模型顺手 ``profile_rewrite`` 压缩。
* **日记** ``diary``：反思时写，只把最近几条带进 prompt。
* **关系** ``affinity``：-1..1 的好感度，模型每次可以微调 ±0.1。
* **最近经历** ``summary``：滑出 prompt 里对话窗口的旧对话，攒够一批就让模型并进这段摘要
  （``summarized_ts`` = 已经并进去的最后一条对话的时间），见 ``director.FOLD_BATCH``。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PROFILE_SOFT_CAP = 24
PROFILE_HARD_CAP = 40
DIARY_KEEP = 300
DIARY_IN_PROMPT = 5
DIARY_EVERY_SECONDS = 2 * 3600.0
SUMMARY_MAX = 400
CHAT_IN_PROMPT_CHARS = 2400


class Memory:
    def __init__(self, path: Path | str | None = None, now=datetime.now):
        if path is None:
            from ..config import config_dir

            path = config_dir() / "memory.json"
        self.path = Path(path)
        self.now = now
        self.profile: list[str] = []
        self.diary: list[dict] = []
        self.affinity = 0.0
        self.goal = ""
        self.summary = ""
        self.summarized_ts = 0.0
        self.last_diary_ts = 0.0
        self.events_since_diary = 0
        self.load()

    # -- 文件 -----------------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        self.profile = [str(x) for x in raw.get("profile") or [] if str(x).strip()]
        self.diary = [d for d in raw.get("diary") or []
                      if isinstance(d, dict) and d.get("text")]
        self.goal = str(raw.get("goal") or "")[:80]
        self.summary = str(raw.get("summary") or "")[:SUMMARY_MAX]
        try:
            self.affinity = max(-1.0, min(1.0, float(raw.get("affinity", 0.0))))
            self.last_diary_ts = float(raw.get("last_diary_ts", 0.0))
            self.summarized_ts = float(raw.get("summarized_ts", 0.0))
        except (TypeError, ValueError):
            pass

    def save(self) -> None:
        payload = {
            "profile": self.profile,
            "affinity": round(self.affinity, 3),
            "goal": self.goal,
            "summary": self.summary,
            "summarized_ts": self.summarized_ts,
            "last_diary_ts": self.last_diary_ts,
            "diary": self.diary[-DIARY_KEEP:],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass

    def clear(self) -> None:
        self.profile, self.diary, self.affinity, self.goal = [], [], 0.0, ""
        self.summary, self.summarized_ts = "", 0.0
        self.last_diary_ts = 0.0
        self.save()

    def set_summary(self, text, upto_ts: float) -> bool:
        text = (text or "").strip()[:SUMMARY_MAX] if isinstance(text, str) else ""
        if not text:
            return False
        self.summary = text
        self.summarized_ts = max(self.summarized_ts, float(upto_ts))
        return True

    # -- 写入（来自模型输出） ----------------------------------------------------
    def remember(self, fact: str) -> bool:
        fact = (fact or "").strip()[:80]
        if not fact or fact in self.profile:
            return False
        self.profile.append(fact)
        del self.profile[:-PROFILE_HARD_CAP]
        return True

    def rewrite_profile(self, facts) -> bool:
        if not isinstance(facts, list):
            return False
        cleaned = [str(f).strip()[:80] for f in facts if str(f).strip()]
        if not cleaned:
            return False
        self.profile = cleaned[:PROFILE_SOFT_CAP]
        return True

    def write_diary(self, text: str) -> bool:
        text = (text or "").strip()[:120]
        if not text:
            return False
        stamp = self.now()
        self.diary.append({"date": stamp.strftime("%Y-%m-%d %H:%M"), "text": text})
        self.last_diary_ts = stamp.timestamp()
        self.events_since_diary = 0
        return True

    def set_goal(self, goal: str) -> bool:
        goal = (goal or "").strip()[:80]
        if goal == self.goal:
            return False
        self.goal = goal
        return True

    def nudge_affinity(self, delta) -> None:
        try:
            delta = max(-0.1, min(0.1, float(delta)))
        except (TypeError, ValueError):
            return
        self.affinity = max(-1.0, min(1.0, self.affinity + delta))

    # -- 读出（进 prompt） ------------------------------------------------------
    @property
    def wants_rewrite(self) -> bool:
        return len(self.profile) > PROFILE_SOFT_CAP

    @property
    def wants_diary(self) -> bool:
        stale = self.now().timestamp() - self.last_diary_ts >= DIARY_EVERY_SECONDS
        return stale and self.events_since_diary >= 8

    def prompt_block(self, talkativeness: float = 0.5, chat_lines: list[str] | None = None,
                     pearl_lines: list[str] | None = None) -> str:
        """记忆，作为第二条 system 消息。档案/日记/摘要很少变；对话记录只在有人说话时变，
        所以两次对话之间的自主请求前缀不变，照样吃缓存。"""
        lines = ["【你的记忆】"]
        talk = ("少说话（有具体的事才开口，大约十来分钟一句）" if talkativeness < 0.34 else
                ("话可以多一点（有东西可说时一两分钟一句也行）" if talkativeness > 0.66
                 else "适度说话（有具体的东西可说时，两三分钟一句正好）"))
        lines.append(f"这个人希望你：{talk}")
        if self.goal:
            lines.append(f"你现在的小目标：{self.goal}")
        lines.append(f"对这个人的好感度：{self.affinity:+.2f}（-1 厌烦 … +1 亲近）")
        if self.profile:
            lines.append("你知道的关于这个人的事：" + "；".join(self.profile))
        else:
            lines.append("你还不太了解这个人。")
        if self.diary:
            recent = self.diary[-DIARY_IN_PROMPT:]
            lines.append("你最近的日记：" + " | ".join(f"{d['date']} {d['text']}" for d in recent))
        if self.summary:
            lines.append("最近经历（摘要）：" + self.summary)
        if pearl_lines:
            lines.append("这个人递给你、你收着的珍珠：" + "；".join(pearl_lines))
        if chat_lines:
            kept, used = [], 0
            for line in reversed(chat_lines):           # 从最新往回装，装满为止
                used += len(line)
                if used > CHAT_IN_PROMPT_CHARS and kept:
                    break
                kept.append(line)
            lines.append("【最近和这个人的对话】")
            lines.extend(reversed(kept))
        return "\n".join(lines)
