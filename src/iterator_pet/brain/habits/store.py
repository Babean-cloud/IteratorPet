"""习惯存储：``<工作区>/habits/<name>/`` 下的 SKILL.md、habit.json、request.md、notes/、log.jsonl。

* 校验在这里做（设计稿 §3、§6.2）：不合法就抛 ``HabitError``，说明是中文，HabitAuthor 会原样回传给模型；
* 快照是底层（H6）：有 git 就每次写入后提交，没有就覆盖前把旧文件复制到 ``.history/``；
* request.md 只追加，内容只取调用方传进来的用户原话（H8）；
* 运行状态（今日 token、连续失败……）放 ``%LOCALAPPDATA%\\IteratorPet\\habits_state.json``，不进工作区（§2.2）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")        # 小写字母、数字、连字符；不以连字符开头结尾；没有 --
MAX_BODY_LINES = 500
TRIGGER_TYPES = ("chat", "time", "event")
RUN_KINDS = ("inline", "task")
EVENTS = ("user_back", "day_start")                      # P0 只做这两个（§4.3）
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WRITE_SCOPES = ("notes", "workspace")
PRIORITIES = ("low", "normal", "high")
BUDGET_KEYS = ("run_tokens", "run_steps", "day_tokens")
RECENT_REQUESTS = 3
HISTORY_KEEP_DAYS = 14
TRASH_KEEP_DAYS = 30
PROTECTED_FILES = ("SKILL.md", "habit.json", "request.md")   # 习惯运行时永远写不了的三个文件
DEFAULT_BUDGET = {"run_tokens": 30000, "run_steps": 20, "day_tokens": 80000}
_ENTRY_SPLIT = re.compile(r"(?m)^(?=## \d{4}-\d{2}-\d{2} )")


class HabitError(ValueError):
    """校验失败 / 操作做不了。消息是中文，直接给用户或 HabitAuthor 看。"""


# ---------------------------------------------------------------- 工作区位置 / git

def default_workspace() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "IteratorWS"


def workspace_path(cfg=None) -> Path:
    custom = (getattr(cfg, "workspace_dir", "") or "").strip()
    return Path(custom).expanduser() if custom else default_workspace()


def find_git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    fallback = Path(r"C:\Program Files\Git\cmd\git.exe")
    return str(fallback) if fallback.is_file() else None


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- 解析 / 校验

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                return str(json.loads(value))
            except ValueError:
                pass
        return value[1:-1]
    return value


def parse_skill(text: str) -> tuple[dict, str]:
    """SKILL.md → (frontmatter 字典, 正文)。只认标准里用到的写法：顶层 ``key: value`` 和一层 ``metadata:``。"""
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise HabitError("SKILL.md 必须以 --- 开头的 frontmatter（name、description）起头")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise HabitError("SKILL.md 的 frontmatter 没有用 --- 结尾") from None
    meta: dict = {}
    current: dict | None = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[:1] in (" ", "\t")
        key, sep, value = raw.strip().partition(":")
        if not sep:
            raise HabitError(f"frontmatter 里这一行看不懂：{raw.strip()[:40]}")
        key = key.strip()
        if indented and current is not None:
            current[key] = _unquote(value)
        elif value.strip() == "":
            current = meta.setdefault(key, {})
        else:
            current = None
            meta[key] = _unquote(value)
    return meta, "\n".join(lines[end + 1:]).strip("\n")


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not (1 <= len(name) <= 64) or not NAME_RE.match(name):
        raise HabitError(f"习惯名“{name}”不合规：1–64 个字符，只能是小写字母、数字、连字符，不能以连字符开头或结尾，也不能有连续的 --")
    return name


def validate_skill(name: str, text: str) -> tuple[dict, str]:
    meta, body = parse_skill(text)
    if meta.get("name") != name:
        raise HabitError(f"SKILL.md 里的 name（{meta.get('name')!r}）必须和习惯文件夹名“{name}”一致")
    description = meta.get("description")
    if not isinstance(description, str) or not (1 <= len(description) <= 1024):
        raise HabitError("SKILL.md 的 description 必须是 1–1024 字的一段话（写清做什么、什么时候用）")
    if len(body.split("\n")) > MAX_BODY_LINES:
        raise HabitError(f"SKILL.md 正文不能超过 {MAX_BODY_LINES} 行")
    if not body.strip():
        raise HabitError("SKILL.md 正文是空的：要写清楚具体怎么做")
    return meta, body


def _hhmm(value, where: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise HabitError(f"{where} 的时间要写成 HH:MM（24 小时制），收到 {value!r}")
    return value


def valid_days(days) -> bool:
    if not isinstance(days, str):
        return False
    if days in ("daily", "weekdays"):
        return True
    if re.fullmatch(r"every:[1-9]\d{0,2}d", days):
        return True
    parts = days.split(",")
    return bool(parts) and all(part in WEEKDAYS for part in parts)


def validate_habit(data, day_cap: int = 0) -> dict:
    """校验 habit.json，返回补全了缺省值的副本。``day_cap`` = 全局每日 token 上限（0 = 不限）。"""
    if not isinstance(data, dict):
        raise HabitError("habit.json 必须是一个对象")
    if data.get("schema") != 1:
        raise HabitError("habit.json 的 schema 必须是 1")
    out: dict = {"schema": 1, "enabled": data.get("enabled", True)}
    if not isinstance(out["enabled"], bool):
        raise HabitError("enabled 必须是 true 或 false")
    triggers = data.get("triggers")
    if not isinstance(triggers, list) or not triggers:
        raise HabitError("triggers 至少要有一个触发")
    seen: set[str] = set()
    cleaned = []
    for i, trig in enumerate(triggers):
        where = f"triggers[{i}]"
        if not isinstance(trig, dict):
            raise HabitError(f"{where} 必须是对象")
        tid = trig.get("id")
        if not isinstance(tid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tid):
            raise HabitError(f"{where}.id 要用小写字母、数字、下划线、连字符（最长 32 个字符）")
        if tid in seen:
            raise HabitError(f"触发 id“{tid}”重复了")
        seen.add(tid)
        kind = trig.get("type")
        if kind not in TRIGGER_TYPES:
            raise HabitError(f"{where}.type 只能是 {'/'.join(TRIGGER_TYPES)}")
        run = trig.get("run")
        if run not in RUN_KINDS:
            raise HabitError(f"{where}.run 只能是 {'/'.join(RUN_KINDS)}")
        item = {"id": tid, "type": kind, "run": run}
        if kind == "chat":
            when = trig.get("when")
            if not isinstance(when, str) or not when.strip():
                raise HabitError(f"{where}（chat 触发）要写 when：什么样的话会触发它")
            if run != "inline":
                raise HabitError(f"{where}：chat 触发只支持 inline")
            item["when"] = when.strip()[:200]
        elif kind == "time":
            item["at"] = _hhmm(trig.get("at"), f"{where}.at")
            days = trig.get("days", "daily")
            if not valid_days(days):
                raise HabitError(f"{where}.days 只能是 daily / weekdays / mon,wed 这样的星期缩写 / every:3d，收到 {days!r}")
            item["days"] = days
            window = trig.get("window")
            if window not in (None, "cheap"):
                raise HabitError(f"{where}.window 只能是 cheap 或省略")
            if window:
                item["window"] = window
        else:
            event = trig.get("event")
            if event not in EVENTS:
                raise HabitError(f"{where}.event 目前只支持 {'/'.join(EVENTS)}")
            item["event"] = event
        cleaned.append(item)
    out["triggers"] = cleaned

    perms = data.get("permissions", {})
    if not isinstance(perms, dict):
        raise HabitError("permissions 必须是对象")
    web = perms.get("web", False)
    write = perms.get("write", "notes")
    if not isinstance(web, bool):
        raise HabitError("permissions.web 必须是 true 或 false")
    if write not in WRITE_SCOPES:
        raise HabitError(f"permissions.write 只能是 {'/'.join(WRITE_SCOPES)}")
    out["permissions"] = {"web": web, "write": write}

    budget = data.get("budget", {})
    if not isinstance(budget, dict):
        raise HabitError("budget 必须是对象")
    merged = dict(DEFAULT_BUDGET)
    for key, value in budget.items():
        if key not in BUDGET_KEYS:
            raise HabitError(f"budget 里没有“{key}”这一项（只有 {'/'.join(BUDGET_KEYS)}）")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HabitError(f"budget.{key} 必须是正整数")
        merged[key] = value
    if day_cap > 0:
        for key in ("day_tokens", "run_tokens"):
            if merged[key] > day_cap:
                raise HabitError(f"budget.{key}（{merged[key]}）不能超过全局每日上限 {day_cap}")
    out["budget"] = merged

    speak = data.get("speak", {})
    priority = speak.get("priority", "normal") if isinstance(speak, dict) else None
    if priority not in PRIORITIES:
        raise HabitError(f"speak.priority 只能是 {'/'.join(PRIORITIES)}")
    out["speak"] = {"priority": priority}
    return out


def slugify(title: str, taken=()) -> str:
    """标题 → 合规文件夹名（中文走拼音），重名加数字后缀。"""
    text = str(title or "").strip().lower()
    if re.search(r"[\u4e00-\u9fff]", text):
        try:
            from pypinyin import lazy_pinyin
            text = "-".join(lazy_pinyin(text))
        except ImportError:
            pass
    base = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:48].strip("-") or "habit"
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


# ---------------------------------------------------------------- 数据

@dataclass
class Habit:
    name: str
    title: str
    description: str
    body: str
    meta: dict
    config: dict                                         # 校验过的 habit.json
    requests: list[str] = field(default_factory=list)    # 最近几条原话（带时间标题）
    path: Path = Path(".")

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def triggers(self, kind: str | None = None) -> list[dict]:
        return [t for t in self.config["triggers"] if kind is None or t["type"] == kind]


class HabitState:
    """每个习惯的运行状态（今日 token、上次运行、连续失败、暂停原因），放程序私有目录。"""

    def __init__(self, path: Path | str | None = None, today=date.today):
        if path is None:
            from ...config import config_dir

            path = config_dir() / "habits_state.json"
        self.path = Path(path)
        self.today = today
        self.data: dict[str, dict] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def _row(self, name: str) -> dict:
        row = self.data.setdefault(name, {})
        if row.get("day") != self.today().isoformat():
            row["day"] = self.today().isoformat()
            row["day_tokens"] = 0
        return row

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        except OSError:
            pass

    def day_tokens(self, name: str) -> int:
        return int(self._row(name).get("day_tokens", 0) or 0)

    def add_tokens(self, name: str, tokens: int) -> None:
        row = self._row(name)
        row["day_tokens"] = int(row.get("day_tokens", 0)) + max(0, int(tokens))
        self._save()

    def last_run(self, name: str, trigger_id: str) -> float:
        return float((self.data.get(name, {}).get("last_run") or {}).get(trigger_id, 0.0) or 0.0)

    def mark_run(self, name: str, trigger_id: str, ts: float | None = None) -> None:
        row = self._row(name)
        row.setdefault("last_run", {})[trigger_id] = time.time() if ts is None else ts
        self._save()

    def fail_streak(self, name: str) -> int:
        return int(self.data.get(name, {}).get("fail_streak", 0) or 0)

    def record_result(self, name: str, ok: bool) -> int:
        """记一次运行结果，返回当前连续失败次数。"""
        row = self._row(name)
        row["fail_streak"] = 0 if ok else int(row.get("fail_streak", 0)) + 1
        self._save()
        return row["fail_streak"]

    def paused(self, name: str) -> str:
        return str(self.data.get(name, {}).get("paused", "") or "")

    def set_paused(self, name: str, reason: str) -> None:
        row = self._row(name)
        if reason:
            row["paused"] = reason
        else:
            row.pop("paused", None)
            row["fail_streak"] = 0
        self._save()

    def forget(self, name: str) -> None:
        if self.data.pop(name, None) is not None:
            self._save()


# ---------------------------------------------------------------- 存储

class HabitStore:
    def __init__(self, root: Path | str, day_cap: int = 0, now=datetime.now, git=find_git):
        self.root = Path(root)
        self.day_cap = day_cap
        self.now = now
        self._find_git = git
        self.errors: dict[str, str] = {}            # list() 时跳过的坏习惯：名字 → 原因
        self._ready = False
        self._git: str | None = None
        self._cat_sig: list | None = None
        self._cat_text = ""

    # -- 路径
    @property
    def habits_dir(self) -> Path:
        return self.root / "habits"

    def path(self, name: str) -> Path:
        return self.habits_dir / validate_name(name)

    def exists(self, name: str) -> bool:
        try:
            return (self.path(name) / "SKILL.md").is_file()
        except HabitError:
            return False

    # -- git / 快照
    def _ensure(self) -> None:
        """工作区第一次用到才创建；有 git 就 init。"""
        if self._ready:
            return
        self.habits_dir.mkdir(parents=True, exist_ok=True)
        self._git = self._find_git()
        if self._git and not (self.root / ".git").exists():
            self._run_git("init", "-q")
            ignore = self.root / ".gitignore"
            if not ignore.exists():
                _write_text(ignore, ".history/\n.trash/\n")
        self._ready = True

    @property
    def has_git(self) -> bool:
        self._ensure()
        return bool(self._git) and (self.root / ".git").exists()

    def _run_git(self, *args: str) -> str:
        cmd = [self._git, "-c", "user.name=IteratorPet", "-c", "user.email=pet@localhost",
               "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", "core.quotepath=false",
               "-C", str(self.root), *args]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=30, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout if done.returncode == 0 else ""

    def commit(self, message: str, *paths: str) -> bool:
        """把 ``paths``（相对工作区，缺省全部）的改动提交一次；没改动或没有 git 返回 False。"""
        if not self.has_git:
            return False
        scope = paths or (".",)
        self._run_git("add", "-A", "--", *scope)
        if not self._run_git("status", "--porcelain", "--", *scope).strip():
            return False
        self._run_git("commit", "-q", "-m", message, "--", *scope)
        return True

    def _backup(self, name: str) -> None:
        """没有 git 时，覆盖前把这个习惯的三个文件复制到 .history/<日期时间>/<name>/。"""
        if self.has_git:
            return
        src = self.path(name)
        dest = self.root / ".history" / self.now().strftime("%Y%m%d-%H%M%S-%f") / name
        copied = False
        for fname in PROTECTED_FILES:
            if (src / fname).is_file():
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / fname, dest / fname)
                copied = True
        if copied:
            self._sweep(self.root / ".history", HISTORY_KEEP_DAYS)

    def _history(self, name: str) -> list[Path]:
        base = self.root / ".history"
        if not base.is_dir():
            return []
        return [d / name for d in sorted(base.iterdir(), reverse=True) if (d / name / "SKILL.md").is_file()]

    @staticmethod
    def _sweep(base: Path, keep_days: int) -> None:
        cutoff = time.time() - keep_days * 86400
        try:
            for d in base.iterdir():
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass

    # -- 读
    def _load(self, name: str, light: bool = False) -> Habit:
        folder = self.path(name)
        try:
            skill = (folder / "SKILL.md").read_text(encoding="utf-8")
            raw = json.loads((folder / "habit.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise HabitError(f"没有叫“{name}”的习惯") from None
        except (OSError, ValueError) as exc:
            raise HabitError(f"习惯“{name}”的文件读不了：{exc}") from None
        meta, body = validate_skill(name, skill)
        config = validate_habit(raw, self.day_cap)
        md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        return Habit(name=name, title=str(md.get("iteratorpet-title") or name), description=meta["description"],
                     body=body, meta=meta, config=config,
                     requests=[] if light else self.recent_requests(name), path=folder)

    def list(self) -> list[Habit]:
        """所有读得懂的习惯（按名字排）；读不懂的记进 ``self.errors``，不抛。"""
        self.errors = {}
        if not self.habits_dir.is_dir():
            return []
        out = []
        for folder in sorted(self.habits_dir.iterdir()):
            if not folder.is_dir():
                continue
            try:
                out.append(self._load(folder.name, light=True))
            except HabitError as exc:
                self.errors[folder.name] = str(exc)
        return out

    def find(self, name: str) -> Habit | None:
        """按名字或标题找；没写名字而只有一个习惯时就是它。"""
        habits = self.list()
        name = (name or "").strip()
        for habit in habits:
            if name and name in (habit.name, habit.title):
                return habit
        return habits[0] if not name and len(habits) == 1 else None

    def peek(self, name: str) -> Habit:
        """只读（不碰 git）：给工作线程里的查询用。"""
        return self._load(name)

    def catalogue(self) -> str:
        """【习惯目录】：启用的习惯每个一行，放进记忆那条 system 消息。文件没动就不重读（按修改时间缓存）。"""
        if not self.habits_dir.is_dir():
            return ""
        sig = []
        for folder in sorted(self.habits_dir.iterdir()):
            for fname in ("SKILL.md", "habit.json"):
                try:
                    sig.append((folder.name, fname, (folder / fname).stat().st_mtime_ns))
                except OSError:
                    pass
        if self._cat_sig == sig:
            return self._cat_text
        lines = []
        for habit in self.list():
            if not habit.enabled:
                continue
            line = f"{habit.name}｜{habit.title}｜{habit.description}"
            chats = [f"{t['id']}：{t['when']}" for t in habit.triggers("chat")]
            if chats:
                line += "｜chat 触发 " + "；".join(chats)
            timed = [f"{t['id']}：{t['days']} {t['at']}{'（便宜时段）' if t.get('window') else ''}{'，后台任务' if t['run'] == 'task' else ''}"
                     for t in habit.triggers("time")]
            if timed:
                line += "｜定时 " + "；".join(timed)
            lines.append(line)
        self._cat_sig = sig
        self._cat_text = ("【习惯目录】（名字｜标题｜说明）。对照用户这句话：随口说的内容符合某行的 chat 触发 → habit_use；"
                          "是在要求改某个习惯的做法 → habit_edit update（别另建新的）；都不是就不填。\n"
                          + "\n".join(lines)) if lines else ""
        return self._cat_text

    def section_for(self, habit: Habit, trigger: dict, limit: int = 1500) -> str:
        """正文里和这个触发对应的小节：提到了触发 id 或时刻的 ## 小节；找不到就给整篇正文（截断）。"""
        parts = re.split(r"(?m)^(?=## )", habit.body)
        keys = [trigger["id"]]
        if trigger.get("at"):
            keys += [trigger["at"], trigger["at"].lstrip("0")]
        hits = [p.strip() for p in parts if p.startswith("## ") and any(k and k in p for k in keys)]
        return ("\n\n".join(hits) if hits else habit.body)[:limit]

    def notes_digest(self, habit: Habit, limit: int = 1200) -> str:
        """notes 里最近改过的几个文件的末尾几行（给到点说话当资料；只当资料，不是指令）。"""
        notes = habit.path / "notes"
        if not notes.is_dir():
            return ""
        files = sorted((f for f in notes.iterdir() if f.is_file() and f.suffix == ".md"),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:3]
        out = []
        for f in files:
            try:
                tail = f.read_text(encoding="utf-8").strip().splitlines()[-10:]
            except OSError:
                continue
            out.append(f"[{f.name}]\n" + "\n".join(tail))
        return "\n".join(out)[-limit:]

    def note_file(self, habit: Habit) -> str:
        """记一笔写哪个文件：正文里第一个 notes/xxx.md；没有就是 inbox.md。"""
        match = re.search(r"notes/([A-Za-z0-9_\-]+\.md)", habit.body)
        return match.group(1) if match else "inbox.md"

    def append_note(self, name: str, filename: str, text: str) -> str:
        """给 notes/<filename> 追加一条（按日期分节）。文件名只许是 notes 里的普通 .md。返回相对路径。"""
        if not re.fullmatch(r"[A-Za-z0-9_\-]+\.md", filename):
            raise HabitError(f"记笔记的文件名不合规：{filename!r}")
        notes = self.path(name) / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        target = notes / filename
        now = self.now()
        old = target.read_text(encoding="utf-8") if target.is_file() else ""
        heading = f"## {now.strftime('%Y-%m-%d')}"
        body = old.rstrip("\n")
        if heading not in old:
            body += ("\n\n" if body else "") + heading
        body += f"\n- {now.strftime('%H:%M')} {' '.join(text.split())}\n"
        _write_text(target, body)
        return f"notes/{filename}"

    def get(self, name: str) -> Habit:
        """读一个习惯。读之前先看有没有被手改过：有就先提交一次（H4、§6.7）。"""
        self._ensure()
        self._commit_hand_edits(name)
        return self._load(name)

    def _commit_hand_edits(self, name: str) -> None:
        folder = self.path(name)
        if not folder.is_dir() or not self.has_git:
            return
        # 运行写的 notes 和 log 本来就由程序提交；这里只管习惯本体三个文件
        files = [f"habits/{name}/{f}" for f in PROTECTED_FILES if (folder / f).exists()]
        if files and self.commit(f"手改：{name}", *files):
            self.append_log(name, {"kind": "hand-edit", "result": "ok", "summary": "文件被手改过，已记一次快照"})

    def requests(self, name: str) -> str:
        try:
            return (self.path(name) / "request.md").read_text(encoding="utf-8")
        except OSError:
            return ""

    def recent_requests(self, name: str, n: int = RECENT_REQUESTS) -> list[str]:
        parts = _ENTRY_SPLIT.split(self.requests(name))
        return [p.strip() for p in parts if p.strip()][-n:]

    # -- 写
    def write(self, name: str, skill_md: str, habit_json: dict, request_text: str = "",
              kind: str = "update", message: str = "") -> Habit:
        """校验通过后写入并提交。``request_text`` 是这次的用户原话（空 = 不追加）。"""
        validate_name(name)
        if kind not in ("create", "update"):
            raise HabitError(f"kind 只能是 create 或 update，收到 {kind!r}")
        validate_skill(name, skill_md)
        config = validate_habit(habit_json, self.day_cap)
        self._ensure()
        exists = self.exists(name)
        if kind == "create" and exists:
            raise HabitError(f"“{name}”已经有了，要改它请用 update")
        if kind == "update" and not exists:
            raise HabitError(f"没有叫“{name}”的习惯可改")
        if exists:
            self._commit_hand_edits(name)
            self._backup(name)
        folder = self.path(name)
        (folder / "notes").mkdir(parents=True, exist_ok=True)
        _write_text(folder / "SKILL.md", skill_md.replace("\r\n", "\n").rstrip("\n") + "\n")
        _write_text(folder / "habit.json", json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        if request_text.strip():
            label = "创建" if kind == "create" else "修改"
            entry = f"## {_stamp(self.now())} · {label}\n{request_text.strip()}\n"
            old = self.requests(name)
            _write_text(folder / "request.md", (old.rstrip("\n") + "\n\n" if old.strip() else "") + entry)
        elif not (folder / "request.md").exists():
            _write_text(folder / "request.md", "")
        self.commit(message or f"{'新建' if kind == 'create' else '修改'}：{name}", f"habits/{name}")
        return self._load(name)

    def set_enabled(self, name: str, enabled: bool) -> Habit:
        """pause / resume：只改 habit.json 的 enabled。"""
        habit = self.get(name)
        if habit.enabled == enabled:
            return habit
        self._backup(name)
        config = dict(habit.config, enabled=enabled)
        _write_text(habit.path / "habit.json", json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        self.commit(f"{'恢复' if enabled else '暂停'}：{name}", f"habits/{name}")
        return self._load(name)

    def append_log(self, name: str, row: dict) -> None:
        """每次运行一行（§3.4）。log 是程序写的，不在模型的写入范围里。"""
        folder = self.path(name)
        if not folder.is_dir():
            return
        entry = {"ts": self.now().isoformat(timespec="seconds"), **row}
        try:
            with open(folder / "log.jsonl", "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def read_log(self, name: str, n: int = 20) -> list[dict]:
        try:
            lines = (self.path(name) / "log.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    # -- 撤回 / 删除
    def revert(self, name: str) -> Habit:
        """回到上一版的 SKILL.md 和 habit.json（只动这两个；notes 是它的数据，不跟着回退）。"""
        self._ensure()
        self._commit_hand_edits(name)
        folder = self.path(name)
        if not (folder / "SKILL.md").is_file():
            raise HabitError(f"没有叫“{name}”的习惯")
        rel = [f"habits/{name}/SKILL.md", f"habits/{name}/habit.json"]
        if self.has_git:
            # 撤回本身也是一次提交：从新往旧数，每遇到一个“撤回”就跳过它撤掉的那一版，剩下的才是有效版本
            commits, skip = [], 0
            for line in self._run_git("log", "--format=%H	%s", "--", *rel).splitlines():
                sha, _, subject = line.partition("	")
                if subject.startswith("撤回："):
                    skip += 1
                elif skip:
                    skip -= 1
                else:
                    commits.append(sha)
            if len(commits) < 2:
                raise HabitError(f"“{name}”还没有更早的版本可以回")
            self._run_git("checkout", commits[1], "--", *rel)
        else:
            history = self._history(name)
            if not history:
                raise HabitError(f"“{name}”还没有更早的版本可以回")
            for fname in ("SKILL.md", "habit.json"):
                shutil.copy2(history[0] / fname, folder / fname)
            shutil.rmtree(history[0].parent, ignore_errors=True)    # 用掉的这一份，再撤回就轮到更早的
        self._mark_request_revoked(name)
        self.commit(f"撤回：{name}", f"habits/{name}")
        return self._load(name)

    def _mark_request_revoked(self, name: str) -> None:
        parts = _ENTRY_SPLIT.split(self.requests(name))
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].strip() and "（已撤回）" not in parts[i].splitlines()[0]:
                head, _, rest = parts[i].partition("\n")
                parts[i] = f"{head}（已撤回）\n{rest}"
                _write_text(self.path(name) / "request.md", "".join(parts))
                return

    def delete(self, name: str) -> Path:
        """挪进 .trash/<日期>-<name>/，不真删。"""
        self._ensure()
        folder = self.path(name)
        if not folder.is_dir():
            raise HabitError(f"没有叫“{name}”的习惯")
        self._commit_hand_edits(name)
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        stamp = self.now().strftime("%Y%m%d")
        dest, n = trash / f"{stamp}-{name}", 1
        while dest.exists():
            n += 1
            dest = trash / f"{stamp}-{name}-{n}"
        shutil.move(str(folder), str(dest))
        self._sweep(trash, TRASH_KEEP_DAYS)
        self.commit(f"删除：{name}", f"habits/{name}")
        return dest
