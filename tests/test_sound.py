"""原版音效：sounds.txt 解析、什么时候响什么、开关和静音。不出声（假的音效对象）。"""

from __future__ import annotations

from types import SimpleNamespace

from iterator_pet.assets_extractor import parse_sounds_txt
from iterator_pet.platform.sound import SoundBoard
from iterator_pet.simulation.dialogue import DialogueState

SOUNDS_TXT = """//	AI Oracles
SS_AI_Talk_1 : PebblesChat1/vol=0.7
SS_AI_Text : beep4A/vol=0.5, beep4B/vol=0.5
Electricity_Loop : electricityLoop/minVol=0.6/maxVol=1.1
Gate_Electric_Background_LOOP/PLAYALL/dopplerFac=0 : ominousMachineA/vol=0.6, machineDOORS/vol=0.5
Plain : thing
"""


def test_parse_sounds_txt():
    table = parse_sounds_txt(SOUNDS_TXT)
    assert table["SS_AI_Talk_1"] == [("PebblesChat1", 0.7)]
    assert table["SS_AI_Text"] == [("beep4A", 0.5), ("beep4B", 0.5)]
    assert table["Electricity_Loop"] == [("electricityLoop", 0.85)]
    assert table["Gate_Electric_Background_LOOP"][1] == ("machineDOORS", 0.5)
    assert table["Plain"] == [("thing", 1.0)]


class FakeEffect:
    def __init__(self):
        self.plays, self.volume, self.playing = 0, 0.0, False
        self.error = False
        self.deleted = False

    def setVolume(self, v):  # noqa: N802
        self.volume = v

    def play(self):
        self.plays += 1
        self.playing = True

    def stop(self):
        self.playing = False

    def isPlaying(self):  # noqa: N802
        return self.playing

    def status(self):
        return SimpleNamespace(name="Error" if self.error else "Ready")

    def setSource(self, source):  # noqa: N802
        self.source = source

    def deleteLater(self):  # noqa: N802
        self.deleted = True


def _board(**config):
    cfg = SimpleNamespace(sound_enabled=True, sound_speech=True, sound_actions=True, sound_volume=0.5, **config)
    board = SoundBoard(cfg)
    board.effects = {key: [(FakeEffect(), 0.8)] for key in
                     ("talk_pebbles", "talk_moon", "text", "halo", "zap", "databit", "swap")}
    board.loops = {key: (FakeEffect(), 0.6) for key in ("arm", "base", "electric")}
    return board


def _state(**kw):
    base = dict(joints=[(0.0, 0.0), (10.0, 0.0)], base_point=(0.0, 0.0), base_moving=False, halo_sparks=[], kill_phase="idle", kill_fac=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _plays(board, key):
    return board.effects[key][0][0].plays


def test_speech_chatter_then_glyph_beeps():
    board = _board()
    dialogue = DialogueState()
    dialogue.start("……你又来了。这次带了什么东西过来。")
    for _ in range(200):
        board.update(_state(), dialogue, "pebbles")
        dialogue.step()
    assert _plays(board, "talk_pebbles") == 1 and _plays(board, "talk_moon") == 0, "一句话开头一声"
    assert 2 <= _plays(board, "text") <= 6, "字幕每几个字一声轻响"
    assert board.effects["talk_pebbles"][0][0].volume == 0.8 * 0.5
    dialogue.start("……你又来了。这次带了什么东西过来。")       # 同一句再说一遍也算新的一句
    board.update(_state(), dialogue, "past_moon", reading_pearl=True)
    assert _plays(board, "talk_moon") == 1 and _plays(board, "databit") == 1


def test_motion_loops_and_zap():
    board = _board()
    board.update(_state(), DialogueState(), "pebbles")
    for i in range(10):
        board.update(_state(joints=[(0.0, 0.0), (10.0 + 12 * (i + 1), 0.0)],
                            base_point=(float(i + 1), 0.0), base_moving=True), DialogueState(), "pebbles")
    arm, base = board.loops["arm"][0], board.loops["base"][0]
    assert arm.playing and base.playing and arm.volume > 0
    for _ in range(40):
        board.update(_state(), DialogueState(), "pebbles")
    assert not arm.playing and not base.playing, "停下来就不响"
    board.update(_state(kill_phase="charging", kill_fac=0.9), DialogueState(), "pebbles")
    board.update(_state(kill_phase="charging", kill_fac=0.9), DialogueState(), "pebbles")
    assert board.loops["electric"][0].playing
    board.update(_state(kill_phase="flash"), DialogueState(), "pebbles")
    board.update(_state(kill_phase="flash"), DialogueState(), "pebbles")
    assert _plays(board, "zap") == 1, "放出去那一下只响一次"


def test_stationary_carriage_fades_out_even_when_controller_wants_to_move():
    board = _board()
    dialogue = DialogueState()
    for i in range(20):
        board.update(_state(base_point=(float(i), 0.0), base_moving=True), dialogue, "pebbles")
    base = board.loops["base"][0]
    assert base.playing
    for _ in range(40):
        board.update(_state(base_point=(19.0, 0.0), base_moving=True), dialogue, "pebbles")
    assert not base.playing
    plays = base.plays
    for _ in range(100):
        board.update(_state(base_point=(19.0, 0.0), base_moving=True), dialogue, "pebbles")
    assert base.plays == plays
    board.update(_state(base_point=(20.0, 0.0), base_moving=True), dialogue, "pebbles")
    assert base.playing


def test_halo_spark_is_rate_limited():
    board = _board()
    off = [((0, 0), (0, 0), 0.0)] * 20
    on = [((0, 0), (0, 0), 1.0)] + off[1:]
    for _ in range(5):
        board.update(_state(halo_sparks=off), DialogueState(), "pebbles")
        board.update(_state(halo_sparks=on), DialogueState(), "pebbles")
    assert _plays(board, "halo") == 1


def test_switches_and_mute():
    board = _board(sound_speech_override=None)
    board.config.sound_speech = False
    dialogue = DialogueState()
    dialogue.start("……")
    board.update(_state(), dialogue, "pebbles")
    assert _plays(board, "talk_pebbles") == 0, "关了说话声"
    assert board.play("zap"), "动作声照常"
    board.muted = True
    assert not board.play("zap")
    board.loops["arm"][0].playing = True
    board.update(_state(), dialogue, "pebbles")
    assert not board.loops["arm"][0].playing, "静音时循环声停掉"
    board.muted, board.config.sound_enabled = False, False
    assert not board.play("swap")


def test_no_game_means_silent_board(tmp_path):
    board = SoundBoard(SimpleNamespace(), install=str(tmp_path), cache_dir=tmp_path,
                       extract=lambda install, cache: {})
    board.start()
    import time

    deadline = time.time() + 2
    while board._pending is None and time.time() < deadline:
        time.sleep(0.01)
    board.update(_state(), DialogueState(), "pebbles")
    assert not board.ready and not board.play("talk_pebbles")


def test_frame_update_does_not_poll_audio(monkeypatch):
    from iterator_pet.platform import sound

    board = _board()

    def unexpected(*args):
        raise AssertionError("Audio polling/rebuild must not run in the animation tick")

    for clips in board.effects.values():
        for effect, _ in clips:
            monkeypatch.setattr(effect, "status", unexpected)
    monkeypatch.setattr(sound.time, "monotonic", lambda: 3600.0)
    dialogue = DialogueState()
    dialogue.start("测试")
    board.update(_state(), dialogue, "pebbles")
    assert _plays(board, "talk_pebbles") == 1


def test_audio_diagnostics_with_real_qt_objects(tmp_path, monkeypatch):
    import json
    from PySide6.QtWidgets import QApplication
    from PySide6.QtMultimedia import QSoundEffect

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    board = SoundBoard(SimpleNamespace(sound_enabled=True, llm_api_key="must-not-be-exported"))
    effect = QSoundEffect()
    board.effects = {"test": [(effect, 1.0)]}
    path = board.save_diagnostics()
    content = path.read_text(encoding="utf-8")
    data = json.loads(content)
    assert data["enabled"] is True and data["uptime_seconds"] >= 0
    assert data["effects"][0]["status"] == "Null"
    assert "must-not-be-exported" not in content
