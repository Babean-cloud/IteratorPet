"""两个输入框共用的斜杠命令补全。只读本地目录与已记下的名字。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..brain import commands as cmd
from ..config import config_dir


@dataclass(frozen=True)
class Suggestion:
    text: str                       # 按 Tab 后放进输入框的整行
    label: str                      # 候选列表里显示的说明


class CompletionProvider:
    def __init__(self, pet):
        self.pet = pet
        self._habit_stamp = None
        self._habits: list[tuple[str, str, str, str]] = []

    def refresh(self) -> None:
        self._habit_stamp = None

    def _habit_rows(self) -> list[tuple[str, str, str, str]]:
        store = getattr(self.pet, "habits", None)
        if store is None:
            return []
        folder = store.habits_dir
        try:
            stamp = (str(folder), folder.stat().st_mtime_ns)
        except OSError:
            stamp = (str(folder), 0)
        if stamp != self._habit_stamp:
            from pypinyin import Style, lazy_pinyin

            self._habits = []
            for habit in store.list():
                title = habit.title
                self._habits.append((habit.name, title,
                                     "".join(lazy_pinyin(title)).casefold(),
                                     "".join(lazy_pinyin(title, style=Style.FIRST_LETTER)).casefold()))
            self._habit_stamp = stamp
        return self._habits

    @staticmethod
    def _names(prefix: str, lead: str, rows: list[tuple[str, str, str, str]]) -> list[Suggestion]:
        key = prefix.casefold()
        found = []
        for name, title, pinyin, initials in rows:
            if not key or any(part.startswith(key) for part in (name.casefold(), title.casefold(), pinyin, initials)):
                found.append(Suggestion(lead + name, f"{name}　{title[:40]}"))
        return found[:30]

    def _habit(self, line: str, rest: str) -> list[Suggestion]:
        head = line.split(" ", 1)[0]
        op, space, tail = rest.partition(" ")
        if space and op in cmd.HABIT_OPS:
            return self._names(tail, f"{head} {op} ", self._habit_rows())
        operations = [Suggestion(f"{head} {name} ", f"{name}　操作习惯")
                      for name in cmd.HABIT_OPS if name.startswith(rest.casefold())]
        if space:
            return []                 # 普通习惯名不含空格；有空格时只补操作后的名字
        names = self._names(rest, head + " ", self._habit_rows())
        return (names + operations if rest else operations + names)[:30]

    def _forget(self, line: str, rest: str) -> list[Suggestion]:
        memory = getattr(self.pet, "memory", None)
        if memory is None:
            return []
        head = line.split(" ", 1)[0]
        entries = [(name, row.get("path", "")) for name, row in memory.places.items()]
        entries += [(name, row.get("meaning", "")) for name, row in memory.aliases.items()]
        return [Suggestion(f"{head} {name}", f"{name}　{detail[:48]}") for name, detail in entries
                if name.casefold().startswith(rest.casefold())][:30]

    def _grants(self, line: str, rest: str) -> list[Suggestion]:
        head = line.split(" ", 1)[0]
        if not rest.startswith("revoke "):
            return [Suggestion(head + " revoke ", "revoke　撤回授权")] if "revoke".startswith(rest.casefold()) else []
        grants = getattr(self.pet, "grants", None)
        if grants is None:
            return []
        prefix = rest[7:].strip().strip('"').casefold()
        found = []
        for row in grants.list():
            path = row["path"]
            if path.casefold().startswith(prefix) or Path(path).name.casefold().startswith(prefix):
                quoted = f'"{path}"' if " " in path else path
                found.append(Suggestion(f"{head} revoke {quoted}", f"{path}　{'有效' if row['active'] else '已过期'}"))
        return found[:30]

    @staticmethod
    def _paths(lead: str, raw: str, *, folders: bool) -> list[Suggestion]:
        raw = raw.strip()
        if not raw or raw.endswith('"') or ('"' in raw[1:] if raw.startswith('"') else '"' in raw):
            return []
        unquoted = raw[1:] if raw.startswith('"') else raw
        if not (os.path.isabs(unquoted) or unquoted.startswith(("~", ".")) or "%" in unquoted):
            return []
        expanded = os.path.expandvars(os.path.expanduser(unquoted))
        path = Path(expanded)
        parent = path if unquoted.endswith(("\\", "/")) else path.parent
        prefix = "" if unquoted.endswith(("\\", "/")) else path.name.casefold()
        try:
            private = os.path.normcase(os.path.realpath(config_dir()))
            here = os.path.normcase(os.path.realpath(parent))
            if here == private or here.startswith(private + os.sep):
                return []
            entries = [entry for entry in parent.iterdir() if not entry.name.startswith(".")
                       and entry.name.casefold().startswith(prefix) and (not folders or entry.is_dir())]
        except OSError:
            return []
        found = []
        for entry in sorted(entries, key=lambda p: p.name.casefold())[:30]:
            value = str(entry)
            quoted = f'"{value}"' if " " in value else value
            found.append(Suggestion(lead + quoted + (" " if folders else ""),
                                    entry.name + ("/" if entry.is_dir() else "")))
        return found

    def complete(self, line: str) -> list[Suggestion]:
        if not line.startswith("/") or line.startswith("//"):
            return []
        commands = cmd.complete(line)
        if commands:
            return [Suggestion(f"/{spec.name} ", f"/{spec.name}　{spec.help}") for spec in commands]
        spec = cmd.spec_for(line)
        if spec is None:
            return []
        rest = line.split(" ", 1)[1]
        if spec.name == "habit":
            found = self._habit(line, rest)
        elif spec.name == "forget":
            found = self._forget(line, rest)
        elif spec.name == "grants":
            found = self._grants(line, rest)
        elif spec.name == "pearl":
            found = self._paths(line.split(" ", 1)[0] + " ", rest, folders=False)
        elif spec.name == "work":
            match = re.match(r"^((?:-[rwdn]\s+)*(?:-C|--dir)\s+)(.*)$", rest, re.I)
            found = self._paths(line.split(" ", 1)[0] + " " + match.group(1), match.group(2), folders=True) if match else []
        else:
            found = []
        return [item for item in found if item.text != line][:30]
