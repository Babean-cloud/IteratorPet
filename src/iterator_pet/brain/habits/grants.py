"""用户给习惯任务的文件夹授权。表只由程序写，模型只能提出请求。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ...config import config_dir


class GrantError(ValueError):
    pass


def inside(path: str | Path, folder: str | Path) -> bool:
    """比较真实路径，软链接不能借父目录的授权越界。"""
    target = os.path.normcase(os.path.realpath(path))
    base = os.path.normcase(os.path.realpath(folder))
    return target == base or target.startswith(base.rstrip("\\/") + os.sep)


def protected(path: str | Path, *, new_file: bool = False) -> str:
    """不能授权、或不能在外部新建的路径。返回原因，空串表示可用。"""
    target = Path(os.path.realpath(path))
    if not new_file and target == Path(target.anchor):
        return "不能授权整个盘符"
    home = os.environ.get("USERPROFILE") or str(Path.home())
    if not new_file and inside(home, target):
        return "不能授权用户目录或它的上级"
    roots = [os.environ.get(key) for key in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    roots.append(str(config_dir()))          # 授权表、配置、私有运行状态永远由程序自己管
    roots += [str(Path(p) / "Microsoft/Windows/Start Menu/Programs/Startup") for p in
              (os.environ.get("APPDATA"), os.environ.get("PROGRAMDATA")) if p]
    for root in filter(None, roots):
        if inside(target, root) or (not new_file and inside(root, target)):
            return "系统或开机启动位置不能授权或写入"
    if new_file:
        if any(part.startswith(".") for part in target.parts[1:]):
            return "不能新建隐藏文件"
        if target.name.lower().endswith("profile.ps1"):
            return "不能新建 PowerShell 配置文件"
        if any(inside(target, p.strip('"')) for p in os.environ.get("PATH", "").split(os.pathsep)
               if p and Path(p.strip('"')).is_dir()):
            return "PATH 目录不能新建文件"
        if any(inside(target, p) for p in
               (os.environ.get("APPDATA", "") + "/Microsoft/Windows/Start Menu/Programs/Startup",
                os.environ.get("PROGRAMDATA", "") + "/Microsoft/Windows/Start Menu/Programs/Startup") if p):
            return "开机启动位置不能新建文件"
    return ""


class GrantStore:
    def __init__(self, path: str | Path | None = None, config=None, now=datetime.now):
        self.path = Path(path) if path is not None else config_dir() / "grants.json"
        self.config = config
        self.now = now
        self.rows: list[dict] = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self.rows = [row for row in raw if isinstance(row, dict) and row.get("tier") in ("3d", "30d", "forever")
                             and isinstance(row.get("path"), str)]
        except (OSError, ValueError):
            pass

    def _days(self, name: str, default: int) -> int:
        return max(1, int(getattr(self.config, name, default)))

    def expires(self, row: dict) -> datetime | None:
        if row["tier"] == "forever":
            return None
        try:
            last = datetime.fromisoformat(row["last_used"])
        except (KeyError, TypeError, ValueError):
            return datetime.min
        days = self._days("grants_long_days", 30) if row["tier"] == "30d" else self._days("grants_short_days", 3)
        return last + timedelta(days=days)

    def list(self) -> list[dict]:
        return [{**row, "expires_at": (self.expires(row).isoformat(timespec="minutes") if self.expires(row) else None),
                 "active": self.expires(row) is None or self.expires(row) > self.now()} for row in self.rows]

    def covering(self, path: str | Path) -> dict | None:
        choices = [row for row in self.rows if inside(path, row["path"])
                   and (self.expires(row) is None or self.expires(row) > self.now())]
        return max(choices, key=lambda row: self.expires(row) or datetime.max, default=None)

    def expired_covering(self, path: str | Path) -> bool:
        return any(inside(path, row["path"]) and self.expires(row) is not None
                   and self.expires(row) <= self.now() for row in self.rows)

    def add(self, path: str | Path, source: str = "chat", *, forever: bool = False) -> dict:
        folder = Path(os.path.realpath(path))
        if not folder.is_dir():
            raise GrantError("授权路径必须是已经存在的文件夹")
        reason = protected(folder)
        if reason:
            raise GrantError(reason)
        stamp = self.now().isoformat(timespec="seconds")
        row = next((r for r in self.rows if os.path.normcase(r["path"]) == os.path.normcase(str(folder))), None)
        if row is None:
            row = {"path": str(folder), "granted_at": stamp, "last_used": stamp,
                   "tier": "forever" if forever else "3d", "source": source, "used_days": []}
            self.rows.append(row)
        else:
            if self.expires(row) is not None and self.expires(row) <= self.now():
                row.update(granted_at=stamp, used_days=[], tier="3d")
            row.update(last_used=stamp, tier="forever" if forever else row["tier"], source=source)
        self._save()
        return row

    def use(self, path: str | Path) -> bool:
        row = self.covering(path)
        if row is None:
            return False
        now = self.now()
        row["last_used"] = now.isoformat(timespec="seconds")
        window = self._days("grants_upgrade_window_days", 7)
        days = set(row.get("used_days") or [])
        days.add(now.date().isoformat())
        row["used_days"] = sorted(day for day in days if day > (now.date() - timedelta(days=window)).isoformat())
        if row["tier"] == "3d" and len(row["used_days"]) >= 3:
            row["tier"] = "30d"
        self._save()
        return True

    def revoke(self, path: str | Path) -> bool:
        key = os.path.normcase(os.path.realpath(path))
        old = len(self.rows)
        self.rows = [row for row in self.rows if os.path.normcase(os.path.realpath(row["path"])) != key]
        if len(self.rows) != old:
            self._save()
            return True
        return False

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        os.replace(temp, self.path)

