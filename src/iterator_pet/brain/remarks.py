"""它主动说的话，你是什么反应：回了它 / 没理 / 嫌烦。用来让它学会"什么时候该开口、说什么"。

照主动式 agent 研究的思路（"不说话"也是一个选项，按反馈自我调整）：
* 每句**主动**说的话记一笔（用户找它、拖它时的回应不算）；
* ``REPLY_WINDOW`` 秒内你对它说话 = 回应；被拖、被惹毛、或者你说"闭嘴 / 安静 / 烦"= 嫌烦；
  过了窗口都没动静 = 没理；
* 嫌烦就把开口间隔拉长（最多 ×3），被回应就缩短一点（最少 ×0.6），慢慢回到 ×1；
* 最近几句和反应写进观察，模型能看到"上回那句被无视了"，别再说同类的话、别重复。

存在 ``context.json``（``remarks`` / ``talk_scale``），按角色分开。
"""

from __future__ import annotations

import re

REPLY_WINDOW = 120.0
DRAG_WINDOW = 30.0
KEEP = 20
SCALE_MIN, SCALE_MAX = 0.6, 3.0
ANNOYED = re.compile(r"闭嘴|安静|别说|别吵|吵死|好吵|烦死|别烦|少说|别打扰|shut ?up|be quiet", re.I)


class Remarks:
    def __init__(self, context):
        self.context = context
        if not isinstance(getattr(context, "remarks", None), list):
            context.remarks = []
        if not isinstance(getattr(context, "talk_scale", None), (int, float)):
            context.talk_scale = 1.0

    @property
    def items(self) -> list[dict]:
        return self.context.remarks

    @property
    def scale(self) -> float:
        return float(self.context.talk_scale)

    def _nudge(self, factor: float) -> None:
        self.context.talk_scale = max(SCALE_MIN, min(SCALE_MAX, self.scale * factor))

    def said(self, text: str, now: float, app: str = "") -> None:
        self.items.append({"t": round(now, 1), "text": text[:40], "app": app[:30], "reaction": None})
        del self.items[:-KEEP]

    def _pending(self, now: float, window: float) -> list[dict]:
        return [r for r in self.items if r["reaction"] is None and now - r["t"] <= window]

    def on_user_chat(self, text: str, now: float) -> None:
        if ANNOYED.search(text or ""):
            hit = self._pending(now, REPLY_WINDOW * 3) or self.items[-1:]
            for remark in hit:
                remark["reaction"] = "嫌烦"
            self._nudge(1.6)
            return
        hit = self._pending(now, REPLY_WINDOW)
        for remark in hit:
            remark["reaction"] = "回应"
        if hit:
            self._nudge(0.9)

    def on_drag(self, now: float) -> None:
        hit = self._pending(now, DRAG_WINDOW)
        for remark in hit:
            remark["reaction"] = "嫌烦"
        if hit:
            self._nudge(1.3)

    def settle(self, now: float, dt: float = 0.0) -> None:
        """过了窗口没动静的算没理；开口间隔每小时往 ×1 回一半。"""
        for remark in self.items:
            if remark["reaction"] is None and now - remark["t"] > REPLY_WINDOW:
                remark["reaction"] = "没理"
        if dt > 0:
            self.context.talk_scale = 1.0 + (self.scale - 1.0) * 0.5 ** (dt / 3600.0)

    def brief(self, now: float, wall_now: float | None = None) -> dict | None:
        """给模型看：最近几句主动搭话和反应，和一个统计。"""
        if not self.items:
            return None
        recent = self.items[-10:]
        counts = {k: sum(1 for r in recent if r["reaction"] == k) for k in ("回应", "没理", "嫌烦")}
        return {
            "stats": f"最近 {len(recent)} 次主动搭话：被回应 {counts['回应']}，没理 {counts['没理']}，嫌烦 {counts['嫌烦']}",
            "last": [f"{int((now - r['t']) / 60)} 分钟前「{r['text']}」→ {r['reaction'] or '还没反应'}"
                     for r in self.items[-3:]],
        }
