"""斜杠命令的解析（快捷输入条 / 对话窗口共用）。"""

import pytest

from iterator_pet.brain import commands as cmd


def test_plain_text_is_chat():
    assert cmd.parse("你好") is None
    assert cmd.parse("  ") is None
    assert cmd.parse("//斜杠开头的话") is None


def test_work_flags_and_text_keep_spaces():
    c = cmd.parse("/work -r 去读一下  README，列个大纲")
    assert c.name == "work" and c.flags == {"r": True}
    assert c.text == "去读一下  README，列个大纲"
    assert cmd.work_mode(c) == "read-only"
    assert cmd.work_mode(cmd.parse("/work 默认只读")) == "read-only"
    assert cmd.work_mode(cmd.parse("/w -w 写点东西")) == "workspace-write"
    assert cmd.work_mode(cmd.parse("/do -d 沙箱里看看")) == "dsh"


def test_work_folder_flag_with_quotes():
    c = cmd.parse('/work -w -C "D:\\my notes" 整理会议记录')
    assert c.flags == {"w": True, "C": "D:\\my notes"} and c.text == "整理会议记录"
    assert cmd.parse("/work --dir D:\\x 看看").flags == {"C": "D:\\x"}


@pytest.mark.parametrize("line", ["/work", "/work -r", "/work -r -w 两个都要", "/work -x 什么", "/work -C"])
def test_work_errors(line):
    with pytest.raises(cmd.CommandError):
        cmd.parse(line)


def test_unknown_command_suggests():
    with pytest.raises(cmd.CommandError, match="/work"):
        cmd.parse("/wo 干活")


@pytest.mark.parametrize("word,expected", [
    ("25m", {"in_min": 25}), ("1h20m", {"in_min": 80}), ("90s", {"in_min": 1.5}),
    ("半小时", {"in_min": 30}), ("10分钟", {"in_min": 10}), ("2小时", {"in_min": 120}),
    ("21:30", {"at": "21:30"}), ("7：05", {"at": "7:05"}),
    ("25", None), ("25x", None), ("25:00", None), ("0m", None), ("m", None),
])
def test_parse_when(word, expected):
    assert cmd.parse_when(word) == expected


def test_remind_needs_time_and_text():
    assert cmd.parse("/remind 25m 休息一下").text == "25m 休息一下"
    for bad in ("/remind 休息", "/remind 25m", "/r 明天 开会"):
        with pytest.raises(cmd.CommandError):
            cmd.parse(bad)


def test_char_and_park():
    assert cmd.parse("/char").text == ""
    assert cmd.character_of("月") == "past_moon" and cmd.character_of("卵石") == "pebbles"
    with pytest.raises(cmd.CommandError):
        cmd.parse("/char 猫")
    assert cmd.SIDE_WORDS[cmd.parse("/park 左").text] == "left"
    with pytest.raises(cmd.CommandError):
        cmd.parse("/park 中间")


def test_completion():
    assert [s.name for s in cmd.complete("/")] == [s.name for s in cmd.COMMANDS]
    assert [s.name for s in cmd.complete("/re")] == ["remind", "reflect"]
    assert cmd.complete("/work 读") == [] and cmd.complete("你好") == []
    assert cmd.spec_for("/w ").name == "work" and cmd.spec_for("/w") is None


def test_pearls_listing_and_drop(tmp_path):
    from iterator_pet.brain.pearls import PearlStore
    from iterator_pet.platform.quick_bar import execute

    class Pet:
        pearls = PearlStore(tmp_path / "p")
        removed_all = 0

        def remove_pearl(self, pid):
            return self.pearls.remove(pid)

        def remove_all_pearls(self):
            count = len(self.pearls.items)
            self.pearls.clear()
            return count

    pet = Pet()
    assert execute(pet, "/pearls")[1] == "还没有珍珠。"
    for title in ("一", "二", "三", "四"):
        pet.pearls.add(title, "note", "x", title)
    listing = execute(pet, "/pearls")[1]
    assert listing.splitlines()[0].startswith("1. 四"), "1 是最近递的"
    assert execute(pet, "/drop")[1] == "✓ 扔掉了：「四」"
    assert execute(pet, "/drop 1，3")[1] == "✓ 扔掉了：「三」、「一」"
    assert execute(pet, "/drop 9")[1].startswith("✗ 没有第 9 颗")
    with pytest.raises(cmd.CommandError):
        cmd.parse("/drop 最后那颗")
    assert execute(pet, "/drop all")[1] == "✓ 扔掉了 1 颗。" and not pet.pearls.items


def test_execute_local_commands(tmp_path):
    from iterator_pet.brain.chatlog import ChatLog
    from iterator_pet.brain.errands import Errands
    from iterator_pet.brain.reminders import Reminders
    from iterator_pet.platform.quick_bar import execute

    class Pet:
        director = None
        character = "pebbles"
        task_busy = False
        current_task = None
        reminders = Reminders(tmp_path / "r.json")
        errands = Errands(tmp_path / "e.json")
        chatlog = ChatLog(tmp_path / "c.jsonl")
        swapped = None
        parked = "unset"

        def set_llm_enabled(self, on):
            pass

        def set_character(self, key):
            self.swapped = key

        def set_territory(self, side):
            self.parked = side

    pet = Pet()
    keep, msg = execute(pet, "/remind 25m 喝水")
    assert keep and msg.startswith("✓") and "喝水" in msg and len(pet.reminders.items) == 1
    assert execute(pet, "/char") == (False, "") and pet.swapped == "past_moon"
    assert execute(pet, "/park 自由") == (False, "") and pet.parked is None
    assert execute(pet, "/stop")[1] == "现在没有任务在跑。"
    assert "AI：没开" in execute(pet, "/status")[1]
    assert execute(pet, "/nope")[1].startswith("✗")
    assert execute(pet, "随便聊聊")[1].startswith("✗ AI 没开")
    assert "/work" in execute(pet, "/help")[1]
    assert execute(pet, "/watch 等测试跑完")[1].startswith("✗"), "没开 AI 办不了心事"
    pet.errands.add("等下载完")
    assert "心事：等下载完" in execute(pet, "/status")[1]
    assert execute(pet, "/unwatch")[1] == "✓ 放下了 1 件心事。" and not pet.errands.items
