"""自然语言接活只自动启动已知范围的只读任务。"""

from pathlib import Path
from types import SimpleNamespace

from iterator_pet.platform import pet_window


def _window(folder, overseer=False):
    started, offered, opened, messages = [], [], [], []
    chat = SimpleNamespace(add=lambda who, text: messages.append((who, text)))
    window = SimpleNamespace(
        task_busy=False,
        overseer_enabled=overseer,
        _offer_projection=lambda *args: offered.append(args),
        task_workspace=lambda: str(folder),
        start_task=lambda goal, mode, workspace: started.append((goal, mode, workspace)) or True,
        open_chat=lambda: opened.append(True),
        chatlog=chat,
        _chat_window=SimpleNamespace(offer_task=lambda *args: offered.append(args)),
    )
    return window, started, offered, opened, messages


def test_known_read_only_workspace_starts_without_work_command(tmp_path):
    window, started, offered, opened, _ = _window(tmp_path)
    pet_window.PetWindow._natural_task(window, {
        "goal": "总结日志", "mode": "read-only", "workspace": None}, "读一下日志")
    assert started == [("总结日志", "read-only", str(Path(tmp_path).resolve()))]
    assert opened and not offered


def test_write_or_new_folder_needs_scope_confirmation(tmp_path, monkeypatch):
    window, started, offered, _, messages = _window(tmp_path)
    monkeypatch.setattr(pet_window, "QTimer", SimpleNamespace(singleShot=lambda delay, callback: callback()))
    other = tmp_path / "other"
    other.mkdir()
    pet_window.PetWindow._natural_task(window, {
        "goal": "整理文件", "mode": "workspace-write", "workspace": None}, "整理文件")
    pet_window.PetWindow._natural_task(window, {
        "goal": "查看另一目录", "mode": "read-only", "workspace": str(other)}, "查看另一目录")
    assert not started
    assert offered == [("整理文件", str(tmp_path), "workspace-write"),
                       ("查看另一目录", str(other), "read-only")]
    assert len(messages) == 2


def test_with_overseer_projection_nothing_opens_the_chat_window(tmp_path, monkeypatch):
    window, started, offered, opened, _ = _window(tmp_path, overseer=True)
    monkeypatch.setattr(pet_window, "QTimer", SimpleNamespace(singleShot=lambda delay, callback: callback()))
    pet_window.PetWindow._natural_task(window, {
        "goal": "总结日志", "mode": "read-only", "workspace": None}, "读一下日志")
    assert started and not opened                                  # 只读直接开工：进度走投影，不弹对话窗口
    pet_window.PetWindow._natural_task(window, {
        "goal": "整理文件", "mode": "workspace-write", "workspace": None}, "整理文件")
    assert not opened and offered == [("整理文件", str(tmp_path), "workspace-write")]    # 确认走投影
