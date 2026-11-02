"""斜杠命令：快捷输入条和对话窗口共用，照 CLI 的样子来。

不以 ``/`` 开头的就是普通聊天，交给模型；以 ``/`` 开头的在本地解析，能本地办的（定时提醒、
换角色、停任务……）不经过模型。这里只管**解析**，执行在 ``platform/quick_bar.py``。

    /work -r 去读一下 docs 里的说明，列个大纲      只读（默认）
    /work -w -C D:\\notes 把会议记录整理成 md      可写，换任务文件夹
    /remind 25m 休息一下       /remind 21:30 开会       /remind 1h20m 收衣服
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class CommandError(ValueError):
    """用户打错了：消息直接给人看。"""


@dataclass(frozen=True)
class Spec:
    name: str
    usage: str
    help: str
    aliases: tuple[str, ...] = ()
    flags: tuple[tuple[str, str], ...] = ()          # (写法, 说明)
    takes_value: tuple[str, ...] = ()                # 后面跟一个值的 flag


COMMANDS = (
    Spec("work", "/work [-r|-w|-d] [-n] [-C 文件夹] 要做的事", "让它去干一件长活（读文件、整理、跑命令）",
         aliases=("w", "do"),
         flags=(("-r", "只读（默认）"), ("-w", "可写任务文件夹"), ("-d", "DSH 沙箱，只读"), ("-n", "允许这个任务联网查资料（-d 不支持）"),
                ("-C", "换任务文件夹，比如 -C D:\\notes")),
         takes_value=("-C", "--dir")),
    Spec("remind", "/remind 25m|1h20m|21:30 内容", "定时提醒，本地计时，不花 token", aliases=("r",)),
    Spec("watch", "/watch 要等的事", "心事：它自己想办法盯着，等到了告诉你（要开 AI）"),
    Spec("unwatch", "/unwatch", "放下所有心事，不盯了"),
    Spec("stop", "/stop", "停掉正在跑的任务"),
    Spec("status", "/status", "任务、提醒、盯着的事"),
    Spec("pearl", "/pearl [路径或网址]", "递珍珠；不写就递剪贴板（文字、文件、截图、链接）", aliases=("p",)),
    Spec("pearls", "/pearls", "列出收着的珍珠（带编号，1 是最近的）"),
    Spec("drop", "/drop [编号…|all]", "扔掉珍珠；不写就扔最近那颗，可以写几个编号：/drop 2 5", aliases=("toss",)),
    Spec("give", "/give [编号] [捎的话]", "把珍珠转给另一位迭代器；不写编号就是最近那颗"),
    Spec("habits", "/habits", "列出它养成的习惯"),
    Spec("grants", "/grants [revoke 路径]", "查看文件夹授权、撤回授权"),
    Spec("habit", "/habit 名字 | /habit 名字 改成… | /habit pause|resume|revert|delete 名字", "看详情；直接修改 / 暂停 / 恢复 / 撤回 / 移进回收站"),
    Spec("places", "/places", "列出它记下的地点和别名（用户确认过的）"),
    Spec("forget", "/forget 名字", "忘掉一个地点或别名"),
    Spec("char", "/char [卵石|月]", "换角色；不写就换成另一个", aliases=("swap",)),
    Spec("park", "/park 左|右|上|下|自由", "去某条边待着"),
    Spec("reflect", "/reflect", "现在反思一次"),
    Spec("chat", "/chat", "打开完整的对话窗口"),
    Spec("help", "/help", "列出所有命令", aliases=("?",)),
)
_BY_NAME = {alias: spec for spec in COMMANDS for alias in (spec.name, *spec.aliases)}

HABIT_OPS = ("pause", "resume", "revert", "delete")
CHARACTER_WORDS = {"pebbles": "pebbles", "卵石": "pebbles", "五块卵石": "pebbles", "fp": "pebbles",
                   "moon": "past_moon", "月": "past_moon", "月亮": "past_moon", "皓月": "past_moon",
                   "仰望皓月": "past_moon", "lttm": "past_moon", "past_moon": "past_moon"}
SIDE_WORDS = {"left": "left", "左": "left", "左边": "left", "right": "right", "右": "right", "右边": "right",
              "top": "top", "上": "top", "上边": "top", "bottom": "bottom", "下": "bottom", "下边": "bottom",
              "free": None, "自由": None, "满屏": None}


@dataclass
class Command:
    name: str
    flags: dict = field(default_factory=dict)
    text: str = ""


def parse(line: str) -> Command | None:
    """``None`` = 普通聊天。命令名不认识 / 写法不对抛 ``CommandError``。"""
    line = (line or "").strip()
    if not line.startswith("/") or line.startswith("//"):
        return None
    head, _, rest = line[1:].partition(" ")
    spec = _BY_NAME.get(head.lower())
    if spec is None:
        near = [s.name for s in COMMANDS if s.name.startswith(head.lower()[:2])] if head else []
        hint = f"，是不是 /{near[0]}？" if near else "。/help 看全部命令。"
        raise CommandError(f"没有 /{head} 这个命令{hint}")
    flags, text = _split_flags(spec, rest.strip())
    command = Command(spec.name, flags, text)
    _check(command)
    return command


def _split_flags(spec: Spec, rest: str) -> tuple[dict, str]:
    """开头连着的 ``-x`` 是开关，第一个不是 ``-`` 开头的词起都算正文（原样保留空格）。"""
    flags: dict = {}
    known = {flag for flag, _ in spec.flags} | set(spec.takes_value)
    while rest.startswith("-"):
        match = re.match(r'(-{1,2}[A-Za-z]+)(?:\s+|$)', rest)
        if not match:
            break
        flag = match.group(1)
        if flag not in known:
            raise CommandError(f"/{spec.name} 不认识 {flag}。用法：{spec.usage}")
        rest = rest[match.end():]
        if flag in spec.takes_value:
            value = re.match(r'"([^"]*)"\s*|(\S+)\s*', rest)
            if not value:
                raise CommandError(f"{flag} 后面要跟一个值。用法：{spec.usage}")
            flags["C" if flag == "--dir" else flag.lstrip("-")] = value.group(1) or value.group(2)
            rest = rest[value.end():]
        else:
            flags[flag.lstrip("-")] = True
    return flags, rest.strip()


def _check(command: Command) -> None:
    if command.name == "work":
        modes = [m for m in ("r", "w", "d") if command.flags.get(m)]
        if len(modes) > 1:
            raise CommandError("-r / -w / -d 只能选一个。")
        if command.flags.get("n") and command.flags.get("d"):
            raise CommandError("-d（DSH 沙箱）不能联网。")
        if not command.text:
            raise CommandError("要它做什么？比如：/work -r 去读一下 README，列个大纲")
    elif command.name == "remind":
        when, _, text = command.text.partition(" ")
        if parse_when(when) is None or not text.strip():
            raise CommandError("写法：/remind 25m 休息一下　或　/remind 21:30 开会")
    elif command.name == "drop" and command.text and command.text.lower() not in ("all", "全部") \
            and not re.fullmatch(r"\d+([\s,，]+\d+)*", command.text):
        raise CommandError("写法：/drop（最近那颗）、/drop 3、/drop 2 5 或 /drop all。编号看 /pearls")
    elif command.name == "watch" and not command.text:
        raise CommandError("要等什么？比如：/watch 等终端里的测试跑完")
    elif command.name == "forget" and not command.text:
        raise CommandError("忘掉哪个？写名字，比如：/forget MM。名字看 /places")
    elif command.name == "grants" and command.text and (not command.text.startswith("revoke ") or not command.text[7:].strip()):
        raise CommandError("写法：/grants 或 /grants revoke 文件夹路径")
    elif command.name == "habit":
        word, _, name = command.text.partition(" ")
        if not command.text:
            raise CommandError("看哪个习惯？写名字，比如：/habit idea-review。名字看 /habits")
        if word in HABIT_OPS and not name.strip():
            raise CommandError(f"{word} 哪个习惯？写法：/habit {word} 名字。名字看 /habits")
        if " 改成" in command.text:
            habit_name, _, change = command.text.partition(" 改成")
            if not habit_name.strip() or not change.strip():
                raise CommandError("写法：/habit 名字 改成……")
    elif command.name == "char" and command.text and character_of(command.text) is None:
        raise CommandError("只有 卵石 / 月 两个。")
    elif command.name == "park" and command.text.strip().lower() not in SIDE_WORDS:
        raise CommandError("写法：/park 左|右|上|下|自由")


def work_mode(command: Command) -> str:
    return "workspace-write" if command.flags.get("w") else ("dsh" if command.flags.get("d") else "read-only")


def parse_when(word: str) -> dict | None:
    """``25m`` / ``1h20m`` / ``90s`` / ``半小时`` 之类 → ``{"in_min": …}``；``21:30`` → ``{"at": "21:30"}``。"""
    word = (word or "").strip().lower().replace("：", ":")
    if re.fullmatch(r"\d{1,2}:\d{2}", word):
        hour, minute = (int(x) for x in word.split(":"))
        return {"at": word} if hour < 24 and minute < 60 else None
    if word in ("半小时", "半个小时"):
        return {"in_min": 30}
    units = {"h": 60, "小时": 60, "个小时": 60, "m": 1, "min": 1, "分": 1, "分钟": 1, "s": 1 / 60, "秒": 1 / 60}
    parts = re.findall(r"(\d+(?:\.\d+)?)(个小时|小时|分钟|min|h|m|s|分|秒)", word)
    if not parts or "".join(n + u for n, u in parts) != word:
        return None
    minutes = sum(float(n) * units[u] for n, u in parts)
    return {"in_min": minutes} if 0 < minutes <= 24 * 60 else None


def character_of(word: str) -> str | None:
    return CHARACTER_WORDS.get((word or "").strip().lower())


def complete(line: str) -> list[Spec]:
    """输入条的补全：还在敲命令名时，列出匹配的命令。"""
    line = line or ""
    if not line.startswith("/") or " " in line:
        return []
    head = line[1:].lower()
    return [spec for spec in COMMANDS
            if spec.name.startswith(head) or any(a.startswith(head) for a in spec.aliases if head)]


def spec_for(line: str) -> Spec | None:
    """已经敲完命令名（后面有空格）时，给出它的用法提示。"""
    if not (line or "").startswith("/") or " " not in line:
        return None
    return _BY_NAME.get(line[1:].split(" ", 1)[0].lower())


def help_lines() -> list[str]:
    return [f"{spec.usage}　{spec.help}" for spec in COMMANDS]
