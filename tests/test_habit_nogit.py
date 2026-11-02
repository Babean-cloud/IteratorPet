"""A6：没有 git 时靠 sha256 发现手改。"""

from iterator_pet.brain.habits.store import HabitState, HabitStore

from test_habit_store import CONFIG, SKILL


def make(tmp_path):
    state = HabitState(tmp_path / "state.json")
    store = HabitStore(tmp_path / "ws", git=lambda: None, state=state)
    store.write("idea-review", SKILL, CONFIG, "x", kind="create")
    return state, store, tmp_path / "ws" / "habits" / "idea-review" / "SKILL.md"


def test_hand_edit_detected_by_hash(tmp_path):
    state, store, skill = make(tmp_path)
    assert state.hashes("idea-review").keys() == {"SKILL.md", "habit.json"}
    store.get("idea-review")
    assert not (tmp_path / "ws" / ".history").exists(), "没改过就不存快照"
    skill.write_text(SKILL.replace("追加到 notes/ideas.md。", "我手改的一句。"), encoding="utf-8")
    assert "我手改的一句" in store.get("idea-review").body
    (snap,) = list((tmp_path / "ws" / ".history").iterdir())
    assert "我手改的一句" in (snap / "idea-review" / "SKILL.md").read_text(encoding="utf-8")
    assert any(r.get("kind") == "hand-edit" for r in store.read_log("idea-review"))
    store.get("idea-review")
    assert len(list((tmp_path / "ws" / ".history").iterdir())) == 1, "同一处手改只记一次"


def test_write_after_hand_edit_does_not_double_snapshot(tmp_path):
    state, store, skill = make(tmp_path)
    skill.write_text(SKILL.replace("追加到 notes/ideas.md。", "我手改的一句。"), encoding="utf-8")
    store.write("idea-review", SKILL.replace("追加到 notes/ideas.md。", "我手改的一句。\n新增。"), CONFIG, "改", kind="update")
    assert len(list((tmp_path / "ws" / ".history").iterdir())) == 1
    back = store.revert("idea-review")
    assert "新增" not in back.body and "我手改的一句" in back.body


def test_revert_right_after_hand_edit_skips_the_fresh_snapshot(tmp_path):
    state, store, skill = make(tmp_path)
    store.write("idea-review", SKILL + "\nv2\n", CONFIG, "v2", kind="update")
    skill.write_text(SKILL + "\nv2\n手改\n", encoding="utf-8")
    back = store.revert("idea-review")
    assert "手改" not in back.body and "v2" not in back.body


def test_without_state_nothing_changes(tmp_path):
    store = HabitStore(tmp_path / "ws", git=lambda: None)
    store.write("idea-review", SKILL, CONFIG, "x", kind="create")
    skill = tmp_path / "ws" / "habits" / "idea-review" / "SKILL.md"
    skill.write_text(SKILL + "\n手改\n", encoding="utf-8")
    store.get("idea-review")
    assert not (tmp_path / "ws" / ".history").exists()
