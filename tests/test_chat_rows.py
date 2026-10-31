"""对话窗口：一条很长、没有断点的消息不许把记录区撑宽（不然右对齐的"你说的话"整个被挤出窗口外）。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from iterator_pet.platform.chat_window import MessageRow, soft_wrap  # noqa: E402


def test_soft_wrap_only_touches_long_runs():
    assert soft_wrap("你好 hello") == "你好 hello"
    wrapped = soft_wrap("x" * 70)
    assert wrapped.replace("​", "") == "x" * 70 and wrapped.count("​") == 2


def test_long_rows_do_not_widen_the_transcript():
    app = QApplication.instance() or QApplication([])
    for who in ("system", "task", "user", "pet"):
        row = MessageRow({"who": who, "ts": 0, "text": "a" * 400}, "五块卵石")
        row.refit(400)
        assert row.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
        assert row.minimumSizeHint().width() < 600 or row.minimumWidth() == 0, who
    assert app is not None
