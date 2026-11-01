"""更新提示：启动后一天最多查一次 GitHub 的最新 Release，有新版就说一句、托盘里多一项"去下载"。

只发一个匿名的 GET 到 api.github.com（不带任何本机信息）；``config.update_check`` 可关。
仓库还没公开时 API 返回 404，当作"没有新版"，安静处理。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request

REPO = "Babean-cloud/IteratorPet"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/latest"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 24 * 3600.0


def parse(tag: str) -> tuple[int, ...]:
    """``v0.5.0`` / ``0.5`` / ``v1.2.3-beta`` → (0, 5, 0)；认不出返回 ()。"""
    nums = re.findall(r"\d+", (tag or "").split("-")[0])
    return tuple(int(n) for n in nums[:3])


def newer(tag: str, current: str) -> bool:
    latest, mine = parse(tag), parse(current)
    return bool(latest) and latest > mine


def fetch_latest(timeout: float = 8.0) -> str | None:
    """最新 Release 的 tag；查不到（断网、仓库私有、没有 Release）返回 None。"""
    from ..brain.client import _ssl_context

    request = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "IteratorPet"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:                                   # noqa: BLE001 —— 查不到就当没有
        return None
    tag = body.get("tag_name") if isinstance(body, dict) else None
    return tag if isinstance(tag, str) else None


class UpdateChecker:
    """后台线程查一次；主线程用 ``poll()`` 取结果（只取一次）。"""

    def __init__(self, current: str, fetch=fetch_latest):
        self.current = current
        self._fetch = fetch
        self._result: str | None = None
        self._done = False

    def start(self) -> None:
        def work():
            tag = self._fetch()
            self._result = tag if tag and newer(tag, self.current) else None
            self._done = True

        threading.Thread(target=work, daemon=True).start()

    def poll(self) -> tuple[bool, str | None]:
        """(查完了没有, 新版本 tag 或 None)。"""
        return self._done, self._result
