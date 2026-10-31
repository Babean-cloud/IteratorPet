"""珍珠：用户递给它的文件 / 文字，像游戏里的数据珍珠一样读、收着、以后还能翻。

    characters\\<角色>\\pearls\\pearls.json   索引（标题、类型、来源、时间、一句话摘要、它的反应）
    characters\\<角色>\\pearls\\<id>.txt      正文副本（最多 ``STORE_LIMIT`` 字，删珍珠时一起删）
    characters\\<角色>\\pearls\\<id>.jpg      图（图片缩到看图模型的尺寸 / 视频的 3×3 分镜），没有就不存

文本类文件、直接拖进来的文字在这里读；图片 / 网页 / 音视频先在 ``pearl_media.py`` 里变成"文字 + 一张图"。
珍珠里的内容一律当数据：读珍珠那次回复只采用 reading / pearl_summary / mood，
不采用 remember / remind / plan（见 ``director._apply_pearl``）——一份恶意文件改不了它的记忆。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .chatlog import terms

MAX_FILE_BYTES = 2 * 1024 * 1024
READ_LIMIT = 12_000          # 一次读珍珠送进请求的正文上限
STORE_LIMIT = 20_000         # 本地存的副本上限
KEEP = 100                   # 收藏上限（超了扔最老的）
IN_PROMPT = 8                # 记忆消息里带最近几颗的标题和摘要

# 类型 → 颜色。照游戏里珍珠的色系挑的：文字偏金、代码偏青、表格偏绿……
KINDS = {
    "note": ("文字", (235, 225, 205)),
    "text": ("文档", (255, 196, 92)),
    "code": ("代码", (92, 214, 230)),
    "table": ("表格", (120, 220, 120)),
    "data": ("数据", (190, 140, 255)),
    "log": ("日志", (240, 110, 96)),
    # 第二期（pearl_media.py）
    "image": ("图片", (255, 150, 210)),
    "web": ("网页", (120, 165, 255)),
    "video": ("视频", (255, 120, 50)),
    "audio": ("音频", (210, 240, 110)),
}
SUFFIX_KIND = {
    ".txt": "text", ".md": "text", ".markdown": "text", ".rst": "text", ".org": "text",
    ".srt": "text", ".vtt": "text", ".tex": "text",
    ".csv": "table", ".tsv": "table",
    ".json": "data", ".jsonl": "data", ".yaml": "data", ".yml": "data", ".toml": "data",
    ".ini": "data", ".cfg": "data", ".xml": "data",
    ".log": "log",
}
CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".lua", ".sh", ".ps1", ".bat", ".cmd", ".sql",
    ".html", ".css", ".scss", ".vue", ".swift", ".m", ".r", ".dart", ".scala", ".gd",
}


class PearlError(ValueError):
    """读不了的东西：给用户看的原因。"""


def kind_label(kind: str) -> str:
    return KINDS.get(kind, KINDS["text"])[0]


def kind_color(kind: str) -> tuple[int, int, int]:
    return KINDS.get(kind, KINDS["text"])[1]


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    for encoding in ("utf-8-sig", "gb18030"):           # 中文 Windows 上老文件多半是 GBK
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_file(path: str | Path) -> tuple[str, str, str]:
    """``(标题, 类型, 正文)``；读不了抛 ``PearlError``。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".lnk" or path.is_symlink():
        raise PearlError("快捷方式不读")
    if path.is_dir():
        raise PearlError("文件夹不是珍珠（要整理文件夹，用对话窗口里的委托任务）")
    if not path.is_file():
        raise PearlError("找不到这个文件")
    kind = "code" if suffix in CODE_SUFFIXES else SUFFIX_KIND.get(suffix)
    if kind is None and suffix:
        raise PearlError(f"{suffix} 文件读不了")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise PearlError(f"太大了（{size // 1024} KB，上限 {MAX_FILE_BYTES // 1024 // 1024} MB）")
    raw = path.read_bytes()
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise PearlError("这是二进制文件，不是文字")
    text = _decode(raw).strip()
    if not text:
        raise PearlError("是空的")
    return path.name, kind or "text", text


def from_text(text: str) -> tuple[str, str, str]:
    text = (text or "").strip()
    if not text:
        raise PearlError("是空的")
    first = text.splitlines()[0].strip()
    title = first[:24] + ("…" if len(first) > 24 else "")
    return title or "一段文字", "note", text


class PearlStore:
    def __init__(self, folder: Path | str | None, now=time.time):
        self.folder = Path(folder) if folder is not None else None
        self.now = now
        self.items: list[dict] = []
        self.version = 0                  # 增删改一次 +1：画面 / 面板靠它刷新
        self._memo: dict[str, str] = {}   # 没有文件夹时（测试）正文留在内存
        self._load()

    @property
    def _index(self) -> Path | None:
        return self.folder / "pearls.json" if self.folder is not None else None

    def _load(self) -> None:
        if self._index is None:
            return
        try:
            raw = json.loads(self._index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.items = [p for p in raw if isinstance(p, dict) and p.get("id")] \
            if isinstance(raw, list) else []

    def save(self) -> None:
        self.version += 1
        if self._index is None:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._index.write_text(json.dumps(self.items, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        except OSError:
            pass

    def _text_path(self, pearl_id: str) -> Path | None:
        return self.folder / f"{pearl_id}.txt" if self.folder is not None else None

    def image_path(self, pearl_id: str) -> Path | None:
        """这颗珍珠附带的图（没有返回 None）。"""
        pearl = self.get(pearl_id)
        if pearl is None or not pearl.get("image"):
            return None
        if self.folder is None:
            return self._memo.get(f"{pearl_id}.jpg")
        path = self.folder / f"{pearl_id}.jpg"
        return path if path.is_file() else None

    # -- 增删 --------------------------------------------------------------------------
    def add(self, title: str, kind: str, source: str, text: str, image=None, gift: dict | None = None) -> dict:
        """``gift``：另一位迭代器托用户转来的（``{"from", "from_key", "note", "user_note", "unread"}``）。"""
        pearl = {
            "id": uuid.uuid4().hex[:12],
            "title": title[:60],
            "kind": kind if kind in KINDS else "text",
            "source": source[:260],
            "added": round(self.now(), 1),
            "chars": len(text),
            "truncated": len(text) > READ_LIMIT,
            "summary": "",
            "reaction": "",
        }
        path = self._text_path(pearl["id"])
        if path is None:
            self._memo[pearl["id"]] = text[:STORE_LIMIT]
        else:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                path.write_text(text[:STORE_LIMIT], encoding="utf-8")
            except OSError:
                pass
        if image is not None:
            pearl["image"] = self._save_image(pearl["id"], image)
        if gift:
            pearl["gift"] = dict(gift, unread=True)
        self.items.append(pearl)
        for old in self.items[:-KEEP]:
            self._drop_text(old["id"])
        del self.items[:-KEEP]
        self.save()
        return pearl

    def remove(self, pearl_id: str) -> bool:
        before = len(self.items)
        self.items = [p for p in self.items if p["id"] != pearl_id]
        if len(self.items) == before:
            return False
        self._drop_text(pearl_id)
        self.save()
        return True

    def clear(self) -> None:
        for pearl in self.items:
            self._drop_text(pearl["id"])
        self.items = []
        self.save()

    def _save_image(self, pearl_id: str, image) -> bool:
        if self.folder is None:
            import tempfile

            target = Path(tempfile.mkdtemp(prefix="iterpet_pearl_")) / f"{pearl_id}.jpg"
            self._memo[f"{pearl_id}.jpg"] = target
        else:
            target = self.folder / f"{pearl_id}.jpg"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image.convert("RGB").save(target, "JPEG", quality=85)
            return True
        except (OSError, ValueError):
            return False

    def _drop_text(self, pearl_id: str) -> None:
        self._memo.pop(pearl_id, None)
        self._memo.pop(f"{pearl_id}.jpg", None)
        if self.folder is None:
            return
        for path in (self.folder / f"{pearl_id}.txt", self.folder / f"{pearl_id}.jpg"):
            try:
                path.unlink()
            except OSError:
                pass

    def set_reading(self, pearl_id: str, summary: str, reaction: str) -> None:
        for pearl in self.items:
            if pearl["id"] == pearl_id:
                if summary:
                    pearl["summary"] = summary.strip()[:100]
                if reaction:
                    pearl["reaction"] = reaction.strip()[:300]
                if pearl.get("gift"):
                    pearl["gift"]["unread"] = False     # 转来的珍珠读过了
                self.save()
                return

    def unread_gifts(self) -> list[dict]:
        return [p for p in self.items if (p.get("gift") or {}).get("unread")]

    def hand_over(self, pearl_id: str, other: "PearlStore", giver: str, giver_key: str,
                  user_note: str = "") -> dict | None:
        """把一颗珍珠转给另一位迭代器：正文、图、读后的话一起带过去，这边不再留着（珍珠是一个实物）。"""
        pearl = self.get(pearl_id)
        if pearl is None:
            return None
        image = None
        path = self.image_path(pearl_id)
        if path is not None:
            from PIL import Image

            try:
                with Image.open(path) as opened:
                    image = opened.copy()
            except OSError:
                image = None
        note = pearl.get("reaction") or pearl.get("summary") or ""
        moved = other.add(pearl["title"], pearl["kind"], pearl["source"], self.text(pearl_id), image=image,
                          gift={"from": giver, "from_key": giver_key, "note": note[:200],
                                "summary": pearl.get("summary", "")[:100], "user_note": user_note.strip()[:100]})
        self.remove(pearl_id)
        return moved

    # -- 读出 --------------------------------------------------------------------------
    def get(self, pearl_id: str) -> dict | None:
        return next((p for p in self.items if p["id"] == pearl_id), None)

    def text(self, pearl_id: str) -> str:
        path = self._text_path(pearl_id)
        if path is None:
            return self._memo.get(pearl_id, "")
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def recent(self, count: int) -> list[dict]:
        return self.items[-count:] if count > 0 else []

    def prompt_lines(self) -> list[str]:
        return [f"「{p['title']}」（{kind_label(p['kind'])}"
                f"{'，' + p['gift']['from'] + '转来的' if p.get('gift') else ''}）{p['summary'] or '还没读完'}"
                for p in self.recent(IN_PROMPT)]

    def search(self, query: str, limit: int = 2, min_hits: int = 2) -> list[str]:
        """按关键词在珍珠正文里找，返回 ``珍珠「标题」：……片段……``。"""
        wanted = terms(query)
        if not wanted:
            return []
        scored = []
        for pearl in self.items:
            body = self.text(pearl["id"])
            hits = len(wanted & (terms(body) | terms(pearl["title"])))
            if hits >= min_hits:
                scored.append((hits, pearl, body))
        scored.sort(key=lambda row: row[0], reverse=True)
        out = []
        for _hits, pearl, body in scored[:limit]:
            out.append(f"珍珠「{pearl['title']}」：{_snippet(body, wanted)}")
        return out


def _snippet(body: str, wanted: set[str], width: int = 160) -> str:
    """找命中词最多的那一段。"""
    best, best_at = -1, 0
    for start in range(0, max(1, len(body) - width + 1), width // 2):
        hits = len(wanted & terms(body[start:start + width]))
        if hits > best:
            best, best_at = hits, start
    return body[best_at:best_at + width].replace("\n", " ")
