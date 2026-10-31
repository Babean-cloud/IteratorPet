"""每个角色一套数据，切换角色 = 换一套上下文（像切换对话）。

    %LOCALAPPDATA%\\IteratorPet\\characters\\<角色>\\memory.json    记忆、日记、"最近经历"摘要
    %LOCALAPPDATA%\\IteratorPet\\characters\\<角色>\\chat.jsonl     对话记录
    %LOCALAPPDATA%\\IteratorPet\\characters\\<角色>\\context.json   最近几轮、活动日志、反思进度

两个角色共用的：API Key 和各项设置（config.json）、token 用量（usage.json，同一个账户）、
提醒（reminders.json，是用户给自己定的，到点由当前角色说）。

0.2.0 及以前只有一份 ``memory.json`` / ``chat.jsonl``，第一次启动时搬给五块卵石
（用户 2026-10-30 定的）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

LEGACY_OWNER = "pebbles"
PER_CHARACTER_FILES = ("memory.json", "chat.jsonl", "context.json")


def character_dir(key: str, base: Path | None = None) -> Path:
    if base is None:
        from ..config import config_dir

        base = config_dir()
    safe = "".join(ch for ch in key if ch.isalnum() or ch in "_-") or LEGACY_OWNER
    return Path(base) / "characters" / safe


def migrate_legacy(base: Path | None = None, owner: str = LEGACY_OWNER) -> list[str]:
    """把老版本根目录下的 memory.json / chat.jsonl 搬进 ``owner`` 的目录。

    目标已存在就不动（不覆盖新数据）；返回搬了哪些文件。
    """
    if base is None:
        from ..config import config_dir

        base = config_dir()
    moved = []
    target = character_dir(owner, base)
    for name in ("memory.json", "chat.jsonl"):
        old = Path(base) / name
        new = target / name
        if not old.is_file() or new.exists():
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            moved.append(name)
        except OSError:
            pass
    return moved
