"""珍珠传话：一位迭代器托用户把珍珠转给另一位；对方切出来、开场白说完后读，并且知道是谁给的。"""

from __future__ import annotations

import json
import time

from PIL import Image

from iterator_pet.brain.director import Director
from iterator_pet.brain.pearls import PearlStore
from iterator_pet.simulation.state import SimulationState

WIDE = (0.0, 0.0, 1138.0, 608.0)


def test_hand_over_moves_text_image_and_reaction(tmp_path):
    pebbles = PearlStore(tmp_path / "pebbles")
    moon = PearlStore(tmp_path / "moon")
    pearl = pebbles.add("旧数据", "image", "a.png", "正文", image=Image.new("RGB", (20, 20), (0, 255, 0)))
    pebbles.set_reading(pearl["id"], "一张绿图", "……绿的。没什么好看。")
    moved = pebbles.hand_over(pearl["id"], moon, "五块卵石", "pebbles", "  他说你会懂  ")
    assert not pebbles.items, "珍珠是实物：转走了这边就没了"
    assert moved["gift"] == {"from": "五块卵石", "from_key": "pebbles", "note": "……绿的。没什么好看。",
                             "summary": "一张绿图", "user_note": "他说你会懂", "unread": True}
    assert moon.text(moved["id"]) == "正文" and moon.image_path(moved["id"]) is not None
    assert moon.unread_gifts() == [moved]
    assert "五块卵石转来的" in moon.prompt_lines()[0]
    moon.set_reading(moved["id"], "卵石给的绿图", "他还是老样子。")
    assert not moon.unread_gifts() and PearlStore(tmp_path / "moon").items[0]["gift"]["unread"] is False


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete(self, messages, **_):
        self.calls.append(messages)
        return json.dumps(self.reply, ensure_ascii=False), {}


def _settle(director, sim):
    deadline = time.time() + 2
    while director.busy and time.time() < deadline:
        time.sleep(0.005)
    director.tick(sim)


def test_receiver_greets_then_reads_knowing_the_giver(tmp_path):
    moon = PearlStore(tmp_path / "moon")
    gift = moon.add("实验记录", "text", "x.txt", "第 3 次……",
                    gift={"from": "五块卵石", "from_key": "pebbles", "note": "……失败了，又。", "user_note": "替他问好"})
    client = FakeClient({"say": "……你带了什么来？"})
    now = {"t": 1000.0}
    director = Director(client, idle_fn=lambda: 0.0, pearls=moon, clock=lambda: now["t"])
    sim = SimulationState(WIDE)
    sim.pool_lines = False
    sim.pending_events.append("arrive")
    now["t"] += 7
    director.tick(sim)
    _settle(director, sim)
    arrive = json.loads(client.calls[0][-1]["content"])
    assert arrive["gift_waiting"] == ["五块卵石转来的「实验记录」"], "开场白可以先提一句"

    client.reply = {"reading": ["是他的实验记录。", "……他还在试。"], "pearl_summary": "卵石的实验记录"}
    director.read_pearl(gift["id"])
    now["t"] += 7
    director.tick(sim)
    _settle(director, sim)
    obs = json.loads(client.calls[1][-1]["content"])
    assert obs["pearl"]["gift"] == {"from": "五块卵石", "note": "……失败了，又。", "user_note": "替他问好"}
    assert not moon.unread_gifts()
    assert any("五块卵石转来的珍珠" in line for line in director.activity)
