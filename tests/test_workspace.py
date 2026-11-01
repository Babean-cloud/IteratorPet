from PySide6.QtCore import QRect

from iterator_pet.platform.workspace import WorkspaceSnapshot, pet_rect


def test_pet_window_never_covers_the_whole_monitor():
    """任务栏自动隐藏时可用区域 = 整块显示器：窗口要在任务栏那边让出几像素，不然 Windows / NVIDIA 当它是全屏游戏。"""
    full = QRect(0, 0, 2560, 1440)
    auto_hide = WorkspaceSnapshot(full, full, 1.5, "x")
    assert pet_rect(auto_hide, "bottom") == QRect(0, 0, 2560, 1438)
    assert pet_rect(auto_hide, "left") == QRect(2, 0, 2558, 1440)
    normal = WorkspaceSnapshot(full, QRect(0, 0, 2560, 1392), 1.5, "x")
    assert pet_rect(normal) == QRect(0, 0, 2560, 1392)            # 任务栏常驻：照旧用可用区域
