"""习惯任务的文件落地：授权检查、覆盖前备份、回收站与外部新建限额。"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from ...config import config_dir
from ..executor import OutsideWorkspace, resolve_inside
from .grants import GrantStore, inside, protected


class FileDenied(ValueError):
    pass


class HabitFiles:
    def __init__(self, root: str | Path, grants: GrantStore | None = None, config=None, now=datetime.now):
        self.root = Path(os.path.realpath(root))
        self.grants = grants
        self.config = config
        self.now = now
        self.usage_path = config_dir() / "grants_outside_usage.json"

    def _target(self, path: str) -> tuple[Path, str]:
        raw = str(path or "").strip()
        if not raw:
            raise FileDenied("path 是空的")
        if ":" in raw[2:]:
            raise FileDenied("路径里不能带冒号")
        try:
            return resolve_inside(self.root, raw), "workspace"
        except OutsideWorkspace:
            pass
        if not os.path.isabs(raw):
            raise FileDenied("工作区外只能用完整路径")
        target = Path(os.path.realpath(raw))
        if self.grants is None:
            raise FileDenied("授权表没启动")
        if self.grants is not None and self.grants.covering(target) is not None:
            return target, "grant"
        if self.grants is not None and self.grants.expired_covering(target):
            raise FileDenied("这个文件夹的授权已过期")
        reason = protected(target, new_file=True)
        if reason:
            raise FileDenied(reason)
        if not target.parent.is_dir():
            raise FileDenied("外部新建文件要求父文件夹已存在")
        return target, "new"

    def _safe_path(self, path: Path) -> Path:
        # 去掉盘符冒号；其余路径按原目录层级保存，便于人工找回。
        drive = path.drive.replace(":", "") or "root"
        return Path(drive) / path.relative_to(path.anchor)

    def _backup(self, target: Path) -> None:
        stamp = self.now()
        base = self.root / ".history" / "grants"
        copy = base / stamp.strftime("%Y-%m-%d") / self._safe_path(target)
        copy = copy.with_name(copy.name + "." + stamp.strftime("%H%M%S%f") + ".bak")
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, copy)
        cutoff = stamp.date() - timedelta(days=14)
        for folder in base.iterdir():
            if folder.is_dir():
                try:
                    expired = datetime.strptime(folder.name, "%Y-%m-%d").date() < cutoff
                except ValueError:
                    expired = False
                if expired and inside(folder, base):
                    shutil.rmtree(folder)

    def _quota(self, size: int) -> None:
        limit = max(0, int(getattr(self.config, "grants_outside_daily_bytes", 1048576)))
        try:
            row = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            row = {}
        today = self.now().date().isoformat()
        used = int(row.get("bytes", 0)) if row.get("day") == today else 0
        if used + size > limit:
            raise FileDenied("今天在未授权文件夹新建文件的额度已用完")
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text(json.dumps({"day": today, "bytes": used + size}), encoding="utf-8", newline="\n")

    def write(self, path: str, content: str) -> Path:
        target, kind = self._target(path)
        if target.is_dir():
            raise FileDenied("这是个目录")
        if kind == "workspace":
            from .runner import check_write

            # grants 档在工作区内等同 workspace 档；习惯本体仍然受保护。
            target = check_write(self.root, "", "workspace", path)
        elif kind == "grant":
            if target.exists():
                if not target.is_file():
                    raise FileDenied("只能覆盖普通文件")
                self._backup(target)
        else:
            stem, suffix = target.stem, target.suffix
            candidate = target
            number = 2
            while candidate.exists():
                candidate = target.with_name(f"{stem} ({number}){suffix}")
                number += 1
            target = candidate
            self._quota(len(content.encode("utf-8")))
            with target.open("x", encoding="utf-8", newline="\n") as file:
                file.write(content)
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        if kind == "grant":
            self.grants.use(target)
        return target

    def delete(self, path: str) -> Path:
        target, kind = self._target(path)
        if kind == "new":
            raise FileDenied("未授权文件夹里的已有文件不能删除")
        if kind == "workspace":
            from .runner import check_write

            target = check_write(self.root, "", "workspace", path)
        if not target.is_file():
            raise FileDenied("只能删除已有的普通文件")
        try:
            from send2trash import send2trash

            send2trash(str(target))
        except ImportError:
            stamp = self.now()
            copy = self.root / ".trash" / "grants" / stamp.strftime("%Y-%m-%d_%H%M%S%f") / self._safe_path(target)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, copy)
            target.unlink()
        if kind == "grant":
            self.grants.use(target)
        return target

