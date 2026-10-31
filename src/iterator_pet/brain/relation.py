"""关系阶段 + 精力：熟了才肯多说，累了话就短。

**关系阶段**（只升不降）：要同时满足"认识了几天、聊过几句、好感度"三条才往上走一级。每一级在记忆消息里
多放开一点能说的东西——卵石一开始对禁忌实验、对月闭口不谈，熟了才松口一点，最后偶尔漏出愧疚；
月一开始是礼貌的主人，熟了才说起对卵石的担心、对自己结构的隐约不安。好感度跌到很低时阶段不降，但会写明
"最近关系很僵"，它会收回去。升级那一刻给一个 ``relation_up`` 事件，让它用一句话不着痕迹地表现出来。

**精力**（0~1）：你在电脑前时慢慢掉，深夜、电脑很忙时掉得快；你离开 / 它打盹时回。进观察，不进人设。
对卵石就是原作里那个长期过劳的状态。

都存在 ``context.json``（``relation`` / ``energy``），按角色分开。
"""

from __future__ import annotations

STAGES = ("陌生", "认得", "熟悉", "信任", "亲近")
# 升到第 i 级要：认识天数、你对它说过的话的句数、好感度
THRESHOLDS = (None, (0.5, 10, -0.2), (3, 40, 0.15), (7, 120, 0.4), (21, 300, 0.65))
STRAINED = -0.3

OPENNESS = {
    "pebbles": (
        "你们还不熟。你把这个人当闯进房间的东西：不谈自己的过去，不提仰望皓月，不提身上的腐化；被问到就冷冷打断或者转开。",
        "你们算是认得了。你会提到自己在做\"很重要的计算\"、大难题、对先祖一走了之的不屑，但实验和月仍然一个字不提。",
        "你们挺熟了。你偶尔承认有过\"一个没成功的方案\"，会抱怨身体里那些增生让你难受；提到月只说\"隔壁那位\"，马上岔开。",
        "你信任这个人了。你能断断续续说出那个禁忌实验的大概：抽走了大量的水、拖垮了月、自己也染上了腐化；"
        "但嘴上仍坚持那是计算上的必然。",
        "你们很亲近了。极少数时候（比如深夜、你很累的时候）你会不经意漏出对月的愧疚、对自己处境的疲惫，"
        "说完马上收回去，绝不承认。你不会说\"我把你当朋友\"，但你会只对这个人说这些。",
    ),
    "past_moon": (
        "你们还不熟。你是礼貌的主人：问候、好奇、讲讲眼前的小事，不谈自己的心事。",
        "你们算是认得了。你愿意讲先祖的旧事、你读过的珍珠里的故事。",
        "你们挺熟了。你会说起五块卵石：他太急、太骄傲，最近总不回你的消息，你很担心他。",
        "你信任这个人了。你会说出隐约的不安：卵石那边在抽很多的水，你自己的结构偶尔有说不清的不对劲。",
        "你们很亲近了。你把这个人当成可以托付心事的朋友：会谈你对大难题、对\"结束\"的看法，"
        "也会轻声说起你害怕有一天自己会坍塌、会想不起很多事——但你并不知道将来会怎样。",
    ),
}


class Relation:
    def __init__(self, context, memory=None, wall_ts=None):
        self.context = context
        self.memory = memory
        state = context.relation if isinstance(getattr(context, "relation", None), dict) else {}
        context.relation = state
        state.setdefault("stage", 0)
        state.setdefault("talks", 0)
        state.setdefault("since", wall_ts)            # 第一次见面（wall 时间戳）

    @property
    def state(self) -> dict:
        return self.context.relation

    @property
    def stage(self) -> int:
        return int(self.state.get("stage", 0))

    @property
    def affinity(self) -> float:
        return float(getattr(self.memory, "affinity", 0.0) or 0.0)

    def on_talk(self) -> None:
        self.state["talks"] = int(self.state.get("talks", 0)) + 1

    def days(self, now_ts: float) -> float:
        since = self.state.get("since")
        if since is None:
            self.state["since"] = since = now_ts
        return max(0.0, (now_ts - float(since)) / 86400.0)

    def next_needs(self, now_ts: float) -> str | None:
        if self.stage + 1 >= len(STAGES):
            return None
        days, talks, affinity = THRESHOLDS[self.stage + 1]
        wants = []
        if self.days(now_ts) < days:
            wants.append(f"再认识 {days - self.days(now_ts):.1f} 天")
        if self.state["talks"] < talks:
            wants.append(f"再聊 {talks - self.state['talks']} 句")
        if self.affinity < affinity:
            wants.append(f"好感度到 {affinity:+.2f}")
        return "、".join(wants) or "马上"

    def update(self, now_ts: float) -> bool:
        """够条件就升一级（一次只升一级）。返回是否刚升。"""
        if self.stage + 1 >= len(STAGES):
            return False
        days, talks, affinity = THRESHOLDS[self.stage + 1]
        if self.days(now_ts) >= days and self.state["talks"] >= talks and self.affinity >= affinity:
            self.state["stage"] = self.stage + 1
            return True
        return False

    def prompt_text(self, character: str) -> str:
        lines = OPENNESS.get(character, OPENNESS["pebbles"])
        text = f"【你们的关系：{STAGES[self.stage]}】" + lines[self.stage]
        if self.affinity <= STRAINED:
            text += "不过最近你们的关系很僵：你收回去了，话更少、更冷，那些心事一个字都不会说。"
        return text

    def brief(self) -> str:
        return STAGES[self.stage] + ("（最近很僵）" if self.affinity <= STRAINED else "")


# -- 精力 -------------------------------------------------------------------------------
DRAIN_PER_HOUR = 0.12
RECOVER_PER_HOUR = 0.6


def step_energy(energy: float, dt: float, present: bool, late: bool, busy_cpu: bool, sleeping: bool) -> float:
    if not present or sleeping:
        energy += RECOVER_PER_HOUR * dt / 3600.0
    else:
        energy -= DRAIN_PER_HOUR * (2.0 if late else 1.0) * (1.5 if busy_cpu else 1.0) * dt / 3600.0
    return max(0.05, min(1.0, energy))


def energy_word(energy: float) -> str:
    for limit, word in ((0.2, "累坏了"), (0.4, "很累"), (0.6, "有点累"), (0.85, "还行")):
        if energy < limit:
            return word
    return "精神"
