"""PyInstaller 的入口脚本（onedir 打包用）。

`启动.exe` 的行为全部在 `iterator_pet.launcher` 里：找游戏目录 → 初始化素材 →
以无开发者面板的形式启动。这里只负责把异常留住 —— noconsole 打包没有 stderr，
不留痕的话只能看到"双击没反应"。

**不弹任何窗**（用户明确要求）：崩溃只写日志 + stderr。
日志写到**当前** `%LOCALAPPDATA%`（测试会用临时目录覆盖它），不硬编码 home。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "IteratorPet"


def _log(text: str) -> Path | None:
    try:
        base = _log_dir()
        base.mkdir(parents=True, exist_ok=True)
        target = base / "launcher-crash.log"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n\n")
        return target
    except OSError:
        return None


def _selftest(out: Path) -> int:
    """``启动.exe --selftest 结果.txt``：在打好的包里试一遍容易漏数据文件的模块，结果写进文件
    （noconsole 没有 stdout）。PyInstaller 漏收数据文件时构建照样成功，只有真跑才知道。"""
    import tempfile

    rows = []

    def check(name, fn):
        try:
            rows.append(f"ok   {name}: {fn()}")
        except BaseException as exc:                     # noqa: BLE001
            rows.append(f"FAIL {name}: {type(exc).__name__}: {exc}")

    def pinyin():
        from pypinyin import lazy_pinyin
        return "".join(lazy_pinyin("多媒体"))

    def places():
        from iterator_pet.brain.places import match
        hits = match("莫莫的课件", {"MM课": {"path": "D:/x"}}, {"MM": {"meaning": "MM课"}})
        return hits[:1]

    def docs():
        import docx
        from iterator_pet.brain.docs import read_document     # 走桌宠真正用的那条路（包里没 magika，用替身）
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.docx"
            d = docx.Document()
            d.add_paragraph("迭代器自检")
            d.save(path)
            return read_document(path).strip()[:20]

    def audio():
        from PySide6.QtMultimedia import QAudioSink, QMediaDevices  # noqa: F401
        from iterator_pet.platform.mixer import Mixer
        return Mixer().mix(4).shape

    def habits():
        # 习惯系统：模块都能导入；在临时工作区写一个最小习惯、读回、撤回一次。有 git 走 git，没有走 .history，两条都试
        from iterator_pet.brain.habits import author, runner, scheduler        # noqa: F401
        from iterator_pet.brain.habits.store import HabitStore, find_git
        skill = ("---\nname: selftest\ndescription: 自检\nmetadata:\n  iteratorpet-title: 自检\n---\n"
                 "## 做法\n记到 notes/a.md\n")
        config = {"schema": 1, "triggers": [{"id": "t", "type": "chat", "when": "x", "run": "inline"}]}
        modes = []
        for label, git in (("git", find_git), ("no-git", lambda: None)):
            if label == "git" and find_git() is None:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                store = HabitStore(Path(tmp) / "ws", git=git)
                store.write("selftest", skill, config, "原话", kind="create")
                store.write("selftest", skill + "\n第二版\n", config, "改", kind="update")
                back = store.revert("selftest")
                assert "第二版" not in back.body and store.has_git == (label == "git"), label
                modes.append(label)
        return "+".join(modes)

    def grants():
        # 全部文件和授权表都放临时目录；不读取真实配置或用户工作区。
        from iterator_pet.brain.habits.grants import GrantStore
        from iterator_pet.brain.habits.files import HabitFiles

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "ws"
            folder = base / "granted"
            root.mkdir()
            folder.mkdir()
            store = GrantStore(base / "grants.json")
            store.add(folder)
            files = HabitFiles(root, store)
            target = folder / "selftest.txt"
            files.write(str(target), "旧版本")
            files.write(str(target), "新版本")
            backups = list((root / ".history/grants").rglob("*.bak"))
            assert target.read_text(encoding="utf-8") == "新版本"
            assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "旧版本"
            try:
                import send2trash   # noqa: F401
                mode = "Send2Trash"
            except ImportError:
                mode = ".trash"
            files.delete(str(target))
            assert not target.exists()
            if mode == ".trash":
                copies = list((root / ".trash/grants").rglob("selftest.txt"))
                assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == "新版本"
            return "授权+覆盖备份+删除(" + mode + ")"

    check("grants", grants)
    check("habits", habits)
    check("pypinyin", pinyin)
    check("places", places)
    check("markitdown+docx", docs)
    check("audio", audio)
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return 0 if all(r.startswith("ok") for r in rows) else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        raise SystemExit(_selftest(Path(sys.argv[2])))
    try:
        from iterator_pet.launcher import main

        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        detail = traceback.format_exc()
        where = _log("启动失败:\n" + detail)
        try:
            print(detail, file=sys.stderr)
        except Exception:
            pass
        if where is not None:
            raise SystemExit(f"启动失败，日志：{where}")
        raise
