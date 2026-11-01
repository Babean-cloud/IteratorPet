"""对话主循环里的只读工具（harness 阶段 C）：模型觉得要先查一下，就在回复里填 ``need``，
本地取来结果放进下一条消息的 ``tool_results``，模型再给完整回复。一次被叫醒最多 ``MAX_ROUNDS`` 轮。

没用 function calling：主循环的输出是一整个 JSON 动作（json_mode），工具调用和它混在一起不稳；
"need" 字段只是 JSON 里多一项，system prompt 不变，缓存照样命中。

全部只读，跑在工作线程：读屏幕文字、截当前窗口给看图模型（要开了看图、窗口不是隐私窗口）、
翻旧对话和珍珠、看提醒 / 心事、查地点簿。动手的事照旧只能走 ``task``。
"""

from __future__ import annotations

import json

TOOLS = ("read_screen", "look_screen", "recall", "reminders", "place", "habit")
MAX_ROUNDS = 2              # 最多查几轮（加上最后那次回复，一次被叫醒最多 3 次请求）
MAX_PER_ROUND = 2           # 一轮最多几个工具
RESULT_CHARS = 2400         # 单个结果最多多少字（屏幕文字可能很长）


def needs(action: dict) -> list[dict]:
    """回复里要查的东西（规整过、去掉不认识的、最多 ``MAX_PER_ROUND`` 个）。"""
    raw = action.get("need")
    raw = [raw] if isinstance(raw, (dict, str)) else (raw if isinstance(raw, list) else [])
    out = []
    for item in raw:
        if isinstance(item, str):
            item = {"tool": item}
        if isinstance(item, dict) and item.get("tool") in TOOLS:
            out.append(item)
    return out[:MAX_PER_ROUND]


class ChatTools:
    """主线程里建（拿好引用和当前窗口），工作线程里调。"""

    def __init__(self, perception=None, chatlog=None, pearls=None, reminders=None, errands=None,
                 memory=None, look=None, habits=None):
        self.perception = perception
        self.chatlog = chatlog
        self.pearls = pearls
        self.reminders = reminders
        self.errands = errands
        self.memory = memory
        self.habits = habits                 # HabitStore（可以没有）
        self.look = look                     # () -> JPEG bytes | None；没开看图就是 None
        self.looked = False

    @property
    def can_look(self) -> bool:
        return self.look is not None and not self.looked

    def run(self, items: list[dict]) -> tuple[dict, bytes | None]:
        """跑一轮。返回 ({工具名: 结果文字}, 截图或 None)。"""
        results, image = {}, None
        for item in items:
            name = item["tool"]
            try:
                if name == "look_screen":
                    image, text = self._look()
                else:
                    text = getattr(self, "_" + name)(item)
            except Exception as exc:                     # noqa: BLE001 —— 查不到就说查不到
                text = f"出错了：{type(exc).__name__}"
            key = name if name not in results else f"{name}#{len(results)}"
            results[key] = str(text)[:RESULT_CHARS]
        return results, image

    def _read_screen(self, item) -> str:
        p = self.perception
        if p is None or getattr(p, "level", "off") == "off":
            return "看屏幕关着，读不了。"
        window = p.describe()
        if window is None:
            return "现在没有前台窗口。"
        if window.get("app") == "（隐私窗口）":
            return "前台是隐私窗口，不读。"
        text = p.ocr_text(max_age=None)
        age = getattr(p, "ocr_age", lambda: None)()
        head = f"{window['app']}：{window['title']}"
        if not text:
            return head + "\n（还没认出字）"
        return head + (f"（{int(age)} 秒前认的）" if age is not None else "") + "\n" + text

    def _look(self) -> tuple[bytes | None, str]:
        if not self.can_look:
            return None, "没开看图，或者这一次已经看过了。"
        self.looked = True
        image = self.look()
        if image is None:
            return None, "截图没取到（窗口换了，或者是隐私窗口）。"
        return image, "截好了，图附在这条消息里。"

    def _recall(self, item) -> str:
        query = str(item.get("query") or "").strip()[:80]
        if not query:
            return "recall 要给 query（关键词）。"
        found = []
        if self.chatlog is not None:
            found += self.chatlog.search(query, before_ts=float("inf"), limit=4, min_hits=1)
        if self.pearls is not None:
            found += self.pearls.search(query, min_hits=1)
        return "\n".join(found) if found else f"没翻到和「{query}」有关的。"

    def _reminders(self, item) -> str:
        rows = (self.reminders.describe(limit=20) if self.reminders is not None else [])
        rows += (self.errands.describe() if self.errands is not None else [])
        return "\n".join(rows) if rows else "没有提醒，也没有在盯的心事。"

    def _place(self, item) -> str:
        name = str(item.get("name") or "").strip()[:60]
        if not name:
            return "place 要给 name。"
        if self.memory is None:
            return "没有地点簿。"
        hits = self.memory.place_candidates(name)
        return str(hits) if hits else f"地点簿里没有像「{name}」的。"

    def _habit(self, item) -> str:
        if self.habits is None:
            return "没有习惯可查。"
        from .habits.store import HabitError

        found = self.habits.find(str(item.get("name") or "").strip()[:60])
        if found is None:
            return "没有这个习惯。"
        try:
            habit = self.habits.peek(found.name)         # 只读，不碰 git（这里在工作线程）
        except HabitError as exc:
            return f"这个习惯读不了：{exc}"
        return (f"【SKILL.md】\n{(habit.path / 'SKILL.md').read_text(encoding='utf-8')}\n"
                f"【habit.json】\n{json.dumps(habit.config, ensure_ascii=False)}\n"
                "【最近的原话】\n" + "\n".join(habit.requests))
