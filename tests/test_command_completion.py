"""斜杠命令的动态候选与两个输入框的 Tab 填充。"""

from types import SimpleNamespace

from PySide6.QtCore import Qt, QRect
from PySide6.QtTest import QTest

from iterator_pet.platform.command_completion import CompletionProvider


class Habits:
    def __init__(self, root):
        self.habits_dir = root / "habits"
        self.habits_dir.mkdir()
        self.items = [SimpleNamespace(name="idea-review", title="想法回顾"),
                      SimpleNamespace(name="daily-report", title="每日简报")]
        self.reads = 0

    def list(self):
        self.reads += 1
        return self.items


def pet_for(tmp_path):
    habits = Habits(tmp_path)
    folder = tmp_path / "My Folder"
    folder.mkdir()
    memory = SimpleNamespace(places={"MM": {"path": str(folder)}}, aliases={"多媒体": {"meaning": "MM"}})
    grants = SimpleNamespace(list=lambda: [{"path": str(folder), "active": True}])
    return SimpleNamespace(habits=habits, memory=memory, grants=grants, character="pebbles",
                           overseer_enabled=False, scale=1.0, _drawn_state=None,
                           screen=lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1920, 1080)),
                           x=lambda: 0, y=lambda: 0, director=None,
                           elevated=False, cancel_task=lambda: None, answer_approval=lambda *_: None,
                           current_task=None, chatlog=SimpleNamespace(version=0, entries=[]))


def test_habit_names_operations_and_pinyin_initials(tmp_path):
    pet = pet_for(tmp_path)
    provider = CompletionProvider(pet)
    assert [s.text for s in provider.complete("/hab")] == ["/habits ", "/habit "]
    assert [s.text for s in provider.complete("/habit i")] == ["/habit idea-review"]
    assert provider.complete("/habit d")[0].text == "/habit daily-report"
    assert [s.text for s in provider.complete("/habit x")] == ["/habit idea-review"]
    assert [s.text for s in provider.complete("/habit pause d")] == ["/habit pause daily-report"]
    assert any(s.text == "/habit delete " for s in provider.complete("/habit de"))
    assert pet.habits.reads == 1               # 打字时不反复解析全部习惯文件
    pet.habits.items.append(SimpleNamespace(name="new-habit", title="新习惯"))
    provider.refresh()
    assert [s.text for s in provider.complete("/habit n")] == ["/habit new-habit"]


def test_places_grants_and_existing_paths(tmp_path):
    pet = pet_for(tmp_path)
    provider = CompletionProvider(pet)
    assert [s.text for s in provider.complete("/forget M")] == ["/forget MM"]
    assert [s.text for s in provider.complete("/grants r")] == ["/grants revoke "]
    assert [s.text for s in provider.complete("/grants revoke My")] == [f'/grants revoke "{tmp_path / "My Folder"}"']
    report = tmp_path / "report.txt"
    report.write_text("x", encoding="utf-8")
    assert [s.text for s in provider.complete("/pearl " + str(tmp_path / "rep"))] == ["/pearl " + str(report)]
    assert [s.text for s in provider.complete("/work -w -C " + str(tmp_path / "My"))] == [
        f'/work -w -C "{tmp_path / "My Folder"}" ']
    assert provider.complete("普通聊天") == []


def test_quick_bar_tab_fills_habit_name(tmp_path):
    from iterator_pet.platform.quick_bar import QuickBar

    bar = QuickBar(pet_for(tmp_path))
    bar.popup()
    bar.input.setText("/habit i")
    assert bar.menu.count() == 1 and "idea-review" in bar.menu.item(0).text()
    QTest.keyClick(bar.input, Qt.Key.Key_Tab)
    assert bar.input.text() == "/habit idea-review"
    bar.hide()


def test_chat_window_tab_fills_habit_name(tmp_path):
    from iterator_pet.platform.chat_window import ChatWindow

    panel = ChatWindow(pet_for(tmp_path))
    panel.open()
    panel.input.setText("/habit pause i")
    assert panel.completion_menu.count() == 1
    QTest.keyClick(panel.input, Qt.Key.Key_Tab)
    assert panel.input.text() == "/habit pause idea-review"
    panel.close()

