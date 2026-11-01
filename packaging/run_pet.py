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
        from markitdown import MarkItDown
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.docx"
            d = docx.Document()
            d.add_paragraph("迭代器自检")
            d.save(path)
            return MarkItDown().convert(str(path)).text_content.strip()[:20]

    def audio():
        from PySide6.QtMultimedia import QAudioSink, QMediaDevices  # noqa: F401
        from iterator_pet.platform.mixer import Mixer
        return Mixer().mix(4).shape

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
