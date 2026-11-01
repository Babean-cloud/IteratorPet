"""更新提示：版本比较、后台查、查不到时安静。"""

from __future__ import annotations

import re
import time
from pathlib import Path

from iterator_pet import __version__
from iterator_pet.platform import updates


def test_version_matches_pyproject():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1) == __version__


def test_newer():
    assert updates.newer("v0.6.0", "0.5.0") and updates.newer("v0.5.1", "0.5.0")
    assert not updates.newer("v0.5.0", "0.5.0") and not updates.newer("v0.4.9", "0.5.0")
    assert not updates.newer("nightly", "0.5.0"), "认不出的 tag 不算新版"
    assert updates.newer("v1.0.0-beta", "0.5.0")


def _wait(checker):
    deadline = time.time() + 2
    while not checker.poll()[0] and time.time() < deadline:
        time.sleep(0.01)
    return checker.poll()


def test_checker_reports_only_newer():
    up = updates.UpdateChecker("0.5.0", fetch=lambda: "v0.6.0")
    up.start()
    assert _wait(up) == (True, "v0.6.0")
    same = updates.UpdateChecker("0.5.0", fetch=lambda: "v0.5.0")
    same.start()
    assert _wait(same) == (True, None)
    offline = updates.UpdateChecker("0.5.0", fetch=lambda: None)
    offline.start()
    assert _wait(offline) == (True, None)
