"""设置窗口：表单值写回 config、填错的数字不改、能立刻生效的推给桌宠。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from iterator_pet.config import Config  # noqa: E402
from iterator_pet.platform import settings_window as S  # noqa: E402


def test_apply_values_parses_and_reports_bad():
    cfg = Config()
    changed, bad = S.apply_values(cfg, {
        "llm_talkativeness": 1.7, "llm_daily_token_cap": "abc", "perception": "title",
        "perception_blacklist": "微信， 1Password ,", "screen_vision": "everything",
        "llm_model": " deepseek-x ", "auto_quiet": False, "_autostart": True, "nope": 1})
    assert cfg.llm_talkativeness == 1.0 and cfg.perception == "title" and cfg.llm_model == "deepseek-x"
    assert cfg.perception_blacklist == ["微信", "1Password"] and cfg.auto_quiet is False
    assert cfg.llm_daily_token_cap == Config().llm_daily_token_cap and cfg.screen_vision == "off"
    assert set(bad) == {"每日 token 上限", "看图（图片会发出本机）"}
    assert "nope" not in changed and "_autostart" not in changed
    assert S.apply_values(cfg, {"perception": "title"}) == ([], []), "没变就不算改动"


def test_fields_match_config():
    names = {f.name for f in Config.__dataclass_fields__.values()}
    for field in S.FIELDS:
        assert field.key.startswith("_") or field.key.split(".")[0] in names, field.key


def test_window_round_trip(monkeypatch):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr("iterator_pet.platform.autostart.enabled", lambda: False)
    applied = []

    class Win:
        config = Config()
        character = "pebbles"

        def apply_settings(self, changed):
            applied.append(changed)
            return "chat_hotkey" in changed

    win = Win()
    panel = S.SettingsWindow(win)
    values = panel.values()
    assert values["llm_talkativeness"] == 0.5 and values["_autostart"] is False
    panel.editors["chat_hotkey"][1].setText("Ctrl+Alt+K")
    panel.save()
    assert win.config.chat_hotkey == "Ctrl+Alt+K" and applied == [["chat_hotkey"]]
    assert "重启后生效" in panel.status.text()


def test_habit_settings():
    cfg = Config()
    changed, bad = S.apply_values(cfg, {"cheap_hours": "00:30-08:30", "habit_default_budget.day_tokens": "120000",
                                        "workspace_dir": "D:/ws"})
    assert set(changed) == {"cheap_hours", "habit_default_budget.day_tokens", "workspace_dir"} and not bad
    assert cfg.habit_default_budget == {"run_tokens": 30000, "run_steps": 20, "day_tokens": 120000}
    changed, bad = S.apply_values(cfg, {"cheap_hours": "半夜", "habit_default_budget.day_tokens": "abc"})
    assert not changed and set(bad) == {"便宜时段", "每个习惯每天 token"} and cfg.cheap_hours == "00:30-08:30"
    assert S.apply_values(cfg, {"cheap_hours": ""})[0] == ["cheap_hours"], "清空 = 准点跑"


def test_workspace_field_shows_live_path_without_freezing_default(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr("iterator_pet.platform.autostart.enabled", lambda: False)
    active = tmp_path / "IteratorWS"
    win = SimpleNamespace(config=Config(), character="pebbles", habits=SimpleNamespace(root=active),
                          apply_settings=lambda changed: False)
    panel = S.SettingsWindow(win)
    assert panel.values()["workspace_dir"] == str(active)
    panel.save()
    assert win.config.workspace_dir == ""
