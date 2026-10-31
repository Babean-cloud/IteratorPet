"""地点簿的模糊匹配：用户的话里疑似地点 / 缩写的片段 → 最像的前 3 个候选（``place_candidates``）。

地点簿和别名存在 ``memory.json``（``Memory.places`` / ``Memory.aliases``）。这里只做**本地**匹配，
不联网、不花 token：rapidfuzz 比字形，pypinyin 比读音和首字母，所以"莫莫的课件"能对上缩写 ``MM``。
最后选哪个（或者要不要反问）由模型判断。
"""

from __future__ import annotations

import re

from pypinyin import Style, lazy_pinyin
from rapidfuzz import fuzz

THRESHOLD = 72.0
TOP = 3
_UNIT = re.compile(r"[A-Za-z0-9]+|[一-鿿]")


def _units(text: str) -> list[str]:
    """英文数字连成一个单位，汉字一字一个单位。"""
    return _UNIT.findall(text or "")


def _forms(units: list[str]) -> tuple[str, str, str]:
    raw = "".join(units).lower()
    pinyin = "".join(lazy_pinyin(units)).lower()
    initials = "".join(u[0] for u in lazy_pinyin(units, style=Style.FIRST_LETTER)).lower()
    return raw, pinyin, initials


def score(key: str, text: str) -> float:
    """``key``（地点名 / 缩写）在 ``text`` 里最像的一段的分数，0~100。"""
    key_units, text_units = _units(key), _units(text)
    if not key_units or not text_units:
        return 0.0
    k_raw, k_py, k_ini = _forms(key_units)
    ascii_key = all(u.isascii() for u in key_units)
    best = 0.0
    for size in {max(1, len(key_units) - 1), len(key_units), len(key_units) + 1}:
        for i in range(0, max(1, len(text_units) - size + 1)):
            window = text_units[i:i + size]
            w_raw, w_py, w_ini = _forms(window)
            if size == len(key_units) and w_raw == k_raw:
                return 100.0
            cur = max(fuzz.ratio(w_raw, k_raw), fuzz.ratio(w_py, k_py) * 0.95)
            # 缩写 MM ↔ 莫莫 / 默默：窗口全是汉字、长度一样、首字母一样
            if (ascii_key and len(k_raw) <= 4 and size == len(k_raw) and not any(u.isascii() for u in window)
                    and w_ini == k_raw):
                cur = max(cur, 85.0)
            # 很短的名字（两个字以内），字形差一点就很容易认错：要求更高
            if len(k_raw) <= 2 and cur < 85.0:
                cur = 0.0
            best = max(best, cur)
    return best


def match(text: str, places: dict, aliases: dict) -> list[dict]:
    """返回最多 ``TOP`` 个候选 ``{name, kind, to, score}``，按分数从高到低。"""
    found: list[dict] = []
    for kind, table, field in (("place", places, "path"), ("alias", aliases, "meaning")):
        for name, entry in table.items():
            s = score(name, text)
            if s >= THRESHOLD:
                found.append({"name": name, "kind": kind, "to": entry.get(field, ""), "score": int(s)})
    found.sort(key=lambda c: -c["score"])
    return found[:TOP]
