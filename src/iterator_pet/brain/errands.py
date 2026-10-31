"""心事：用户托付的、"等某件事发生了再告诉我"的事。

不再写死"盯前台窗口的几个关键词"，而是照现在 agent 的通行做法分工：

* **模型规划和判断**（``errand_agent.py``）：答应下来以后，它先自己看（列窗口、读某个窗口的字、查进程 /
  文件），想清楚要等的东西在哪、什么信号说明发生了，写下打算（``notes``），再挑几个便宜的本地触发器，
  定一个复查时间；醒来时看自己当初的打算 + 现在的情况，决定告诉你、接着等，还是放弃。
* **本地只做便宜的盯梢**（``Watcher``）：窗口标题变了 / 窗口没了 / 出现了 / 某窗口里出现某些字 /
  进程退出 / 文件变了。触发了或到点了才叫醒模型，等待期间不花 token。

存 ``%LOCALAPPDATA%\\IteratorPet\\errands.json``（两个角色共用，和提醒一样），重启不丢；
触发器的"基准"（答应时窗口标题是什么、屏幕上已经有哪些字）只在内存里，重启后重新取。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

TRIGGER_TYPES = {
    "title_change": "某个窗口的标题变了（或者窗口没了）",
    "window_gone": "某个窗口关掉了",
    "window_appears": "出现了一个标题 / 程序名含某词的新窗口",
    "text_appears": "某个窗口里出现了某些字（本地认字，约 20 秒看一次）",
    "process_exit": "某个进程退出了（数量变少）",
    "file_change": "某个文件 / 文件夹变了（大小或修改时间）",
}
MAX_ACTIVE = 5
MAX_TRIGGERS = 4
DEFAULT_DEADLINE_MIN = 180
MAX_DEADLINE_MIN = 720
DEFAULT_CHECK_MIN = 5
MAX_WAKES = 12                 # 一件心事最多叫醒模型几次（每次是一小串工具调用）
TEXT_EVERY = 20.0              # text_appears：同一件心事多久认一次字
HISTORY_KEEP = 6


def normalize_trigger(spec) -> dict | None:
    if not isinstance(spec, dict) or spec.get("type") not in TRIGGER_TYPES:
        return None
    kind = spec["type"]
    out: dict = {"type": kind}
    if kind in ("title_change", "window_gone", "window_appears", "text_appears"):
        window = str(spec.get("window") or "").strip()[:60]
        if not window:
            return None
        out["window"] = window
    if kind == "text_appears":
        words = spec.get("words")
        words = [words] if isinstance(words, str) else (words if isinstance(words, list) else [])
        out["words"] = [w for w in (str(x).strip().lower()[:30] for x in words) if len(w) >= 2][:8]
        if not out["words"]:
            return None
    if kind == "process_exit":
        name = str(spec.get("name") or "").strip()[:60]
        if not name:
            return None
        out["name"] = name
    if kind == "file_change":
        path = str(spec.get("path") or "").strip()[:260]
        if not path:
            return None
        out["path"] = path
    return out


def describe_trigger(trigger: dict) -> str:
    kind = trigger["type"]
    if kind == "text_appears":
        return f"「{trigger['window']}」里出现 {'/'.join(trigger['words'])}"
    if kind == "process_exit":
        return f"进程 {trigger['name']} 退出"
    if kind == "file_change":
        return f"{trigger['path']} 变了"
    return {"title_change": "「{w}」标题变化", "window_gone": "「{w}」关掉",
            "window_appears": "出现「{w}」窗口"}[kind].format(w=trigger["window"])


class Errands:
    def __init__(self, path: Path | str | None = None, now=time.time):
        if path is None:
            from ..config import config_dir

            path = config_dir() / "errands.json"
        self.path = Path(path)
        self.now = now
        self.items: list[dict] = []
        self.version = 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [e for e in raw if isinstance(e, dict) and e.get("id") and e.get("goal")]
        except (OSError, ValueError, TypeError):
            pass
        for errand in self.items:              # 上次退出时正在想 / 刚被叫醒的：开起来马上再看一眼
            if errand.get("status") != "waiting":
                errand["status"], errand["next_check"] = "waiting", self.now()

    def save(self) -> None:
        self.version += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def add(self, goal: str, by: str = "") -> dict | None:
        goal = (goal or "").strip()[:120]
        if not goal or any(e["goal"] == goal for e in self.items):
            return None                      # 同一件事模型又说了一遍：不重复
        now = self.now()
        errand = {"id": uuid.uuid4().hex[:6], "goal": goal, "by": by, "created": now,
                  "status": "planning", "notes": "", "triggers": [], "next_check": None,
                  "deadline": now + DEFAULT_DEADLINE_MIN * 60.0, "wakes": 0, "history": []}
        self.items.append(errand)
        del self.items[:-MAX_ACTIVE]
        self.save()
        return errand

    def get(self, errand_id) -> dict | None:
        return next((e for e in self.items if e["id"] == errand_id), None)

    def plan(self, errand_id, notes: str = "", triggers=(), check_in_min=None, deadline_min=None) -> dict | None:
        """模型决定接着等：记下它的打算、触发器、下次复查时间。"""
        errand = self.get(errand_id)
        if errand is None:
            return None
        now = self.now()
        if isinstance(notes, str) and notes.strip():
            errand["notes"] = notes.strip()[:300]
        cleaned = [t for t in (normalize_trigger(s) for s in (triggers or [])) if t]
        if cleaned or not errand["triggers"]:
            errand["triggers"] = cleaned[:MAX_TRIGGERS]
        try:
            minutes = float(check_in_min) if check_in_min is not None else DEFAULT_CHECK_MIN
        except (TypeError, ValueError):
            minutes = DEFAULT_CHECK_MIN
        errand["next_check"] = now + max(1.0, min(60.0, minutes)) * 60.0
        if deadline_min is not None:
            try:
                errand["deadline"] = errand["created"] + max(5.0, min(MAX_DEADLINE_MIN, float(deadline_min))) * 60.0
            except (TypeError, ValueError):
                pass
        errand["status"] = "waiting"
        self.save()
        return errand

    def note(self, errand_id, text: str) -> None:
        errand = self.get(errand_id)
        if errand is not None:
            errand["history"].append(f"{time.strftime('%H:%M', time.localtime(self.now()))} {text[:120]}")
            del errand["history"][:-HISTORY_KEEP]
            self.save()

    def finish(self, errand_id) -> dict | None:
        errand = self.get(errand_id)
        if errand is not None:
            self.items.remove(errand)
            self.save()
        return errand

    def cancel_all(self) -> int:
        count = len(self.items)
        self.items = []
        self.save()
        return count

    def due(self) -> list[tuple[dict, str]]:
        """到了复查时间、或者过了时限的（等着的那些）。"""
        now = self.now()
        out = []
        for errand in self.items:
            if errand["status"] != "waiting":
                continue
            if now >= errand["deadline"]:
                out.append((errand, "已经过了时限"))
            elif errand["next_check"] is not None and now >= errand["next_check"]:
                out.append((errand, "到了你定的复查时间"))
        return out

    def describe(self) -> list[str]:
        out = []
        for e in self.items:
            until = time.strftime("%H:%M", time.localtime(e["deadline"]))
            how = "、".join(describe_trigger(t) for t in e["triggers"]) or "定时看一眼"
            state = "正在想怎么盯" if e["status"] == "planning" else f"盯着：{how}"
            out.append(f"心事：{e['goal']}（{state}，最多到 {until}）")
        return out


class Watcher:
    """本地盯梢：每 2 秒在主线程 ``poll`` 一次（列窗口、数进程、看文件都只要几毫秒）；
    ``text_appears`` 要截图认字，丢到后台线程，每件心事 ``TEXT_EVERY`` 秒最多一次。"""

    def __init__(self, sensors=None, clock=time.monotonic):
        if sensors is None:
            from . import sensors as sensors_module

            sensors = sensors_module
        self.sensors = sensors
        self.clock = clock
        self._base: dict = {}             # (errand id, 触发器下标, 触发器内容) -> 基准
        self._text: dict = {}             # 同上 -> 最近一次认出的字（后台线程写）
        self._text_at: dict = {}
        self._busy = False

    def reset(self, errand_id) -> None:
        """模型重新规划过：基准重取。"""
        for table in (self._base, self._text, self._text_at):
            for key in [k for k in table if k[0] == errand_id]:
                del table[key]

    def poll(self, errands: list[dict]) -> list[tuple[dict, str]]:
        armed = [e for e in errands if e["status"] == "waiting" and e["triggers"]]
        if not armed:
            return []
        windows = self.sensors.list_windows()
        fired = []
        for errand in armed:
            for index, trigger in enumerate(errand["triggers"]):
                key = (errand["id"], index, json.dumps(trigger, ensure_ascii=False, sort_keys=True))
                why = self._check(key, trigger, windows)
                if why:
                    fired.append((errand, why))
                    break
        return fired

    def _match(self, query: str, windows):
        query = query.lower()
        return [w for w in windows if not w.private and (query in w.title.lower() or query in w.app.lower())]

    def _check(self, key, trigger: dict, windows) -> str | None:
        kind = trigger["type"]
        base = self._base.get(key)
        if kind in ("title_change", "window_gone"):
            matches = self._match(trigger["window"], windows)
            if base is None:
                if matches:
                    self._base[key] = (matches[0].hwnd, matches[0].title)
                return None
            hwnd, title = base
            now = next((w for w in windows if w.hwnd == hwnd), None)
            if now is None:
                return f"「{trigger['window']}」窗口不见了（原来的标题：{title[:50]}）"
            if kind == "title_change" and now.title != title:
                return f"「{trigger['window']}」窗口的标题变了：{title[:50]} → {now.title[:50]}"
            return None
        if kind == "window_appears":
            hwnds = {w.hwnd for w in self._match(trigger["window"], windows)}
            if base is None:
                self._base[key] = hwnds
                return None
            new = [w for w in windows if w.hwnd in hwnds - base]
            return f"出现了新窗口：{new[0].label()}" if new else None
        if kind == "process_exit":
            count = self.sensors.process_count(trigger["name"])
            if base is None:
                self._base[key] = count
                return None if count else f"进程 {trigger['name']} 现在就没在运行"
            if count < base:
                self._base[key] = count
                return f"进程 {trigger['name']} 退出了（{base} → {count} 个）"
            return None
        if kind == "file_change":
            status = self.sensors.file_status(trigger["path"])
            stamp = (status.get("exists"), status.get("size"), status.get("mtime"))
            if base is None:
                self._base[key] = stamp
                return None
            return f"{trigger['path']} 变了（{_fmt_file(base)} → {_fmt_file(stamp)}）" if stamp != base else None
        if kind == "text_appears":
            return self._check_text(key, trigger, windows)
        return None

    def _check_text(self, key, trigger, windows) -> str | None:
        text = self._text.pop(key, None)
        if text is not None:
            present = {w for w in trigger["words"] if w in text.lower()}
            if key not in self._base:
                self._base[key] = present                 # 答应时屏幕上已经有的词不算
            else:
                new = present - self._base[key]
                if new:
                    return f"「{trigger['window']}」里出现了：{'、'.join(sorted(new))}"
        now = self.clock()
        if not self._busy and now - self._text_at.get(key, -1e9) >= TEXT_EVERY:
            target = self._match(trigger["window"], windows)
            if target and not target[0].minimized:
                self._text_at[key] = now
                self._busy = True
                threading.Thread(target=self._read, args=(key, target[0].hwnd), daemon=True).start()
        return None

    def _read(self, key, hwnd) -> None:
        try:
            from .perception import ocr_image

            image = self.sensors.capture_window(hwnd)
            self._text[key] = ocr_image(image, limit=4000) if image is not None else ""
        except Exception:                                  # noqa: BLE001
            self._text[key] = ""
        finally:
            self._busy = False


def _fmt_file(stamp) -> str:
    exists, size, _ = stamp
    return f"{size} 字节" if exists else "不存在"
