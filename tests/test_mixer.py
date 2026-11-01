"""自己混音的那条流：读 wav、叠加、循环、播完自动下场；看门狗在推不进去时重开。不出声。"""

from __future__ import annotations

import wave

import numpy as np

from iterator_pet.platform import mixer as M


def _wav(path, frames, rate=M.RATE, channels=2, value=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.full(frames * channels, value, "<i2").tobytes())
    return path


def test_load_wav_converts_mono_and_rate(tmp_path):
    data = M.load_wav(_wav(tmp_path / "a.wav", 480, rate=48000, channels=1))
    assert data.shape == (441, 2) and data.dtype == np.float32
    assert abs(float(data[0, 0]) - 8000 / 32768) < 1e-4


def test_one_shot_finishes_and_loop_wraps(tmp_path):
    mix = M.Mixer()
    shot = M.Clip(mix, _wav(tmp_path / "s.wav", 100))
    loop = M.Clip(mix, _wav(tmp_path / "l.wav", 30, value=-4000), loop=True)
    shot.setVolume(0.5)
    shot.play()
    loop.play()
    out = mix.mix(80)
    assert out.shape == (80, 2) and shot.isPlaying() and loop.isPlaying()
    assert abs(out[0, 0] - (0.5 * 8000 - 4000) / 32768) < 1e-4
    mix.mix(80)
    assert not shot.isPlaying() and loop.isPlaying(), "单次的放完自己下场，循环的一直绕"
    loop.stop()
    assert not mix.mix(10).any()


def test_broken_file_is_silent_not_fatal(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"nope")
    clip = M.Clip(M.Mixer(), bad)
    clip.play()
    assert clip.status().name == "Error" and not clip.isPlaying()


def test_pcm_is_clipped():
    pcm = np.frombuffer(M.Mixer.to_pcm(np.array([[2.0, -2.0]], np.float32)), "<i2")
    assert list(pcm) == [32767, -32767]


class FakeSink:
    def __init__(self):
        self.free, self.size = 4000, 4000

    def error(self):
        from PySide6.QtMultimedia import QtAudio
        return QtAudio.Error.NoError

    def bytesFree(self):  # noqa: N802
        return self.free

    def bufferSize(self):  # noqa: N802
        return self.size


class FakeIO:
    def __init__(self, sink):
        self.sink, self.chunks = sink, []

    def write(self, data):
        self.chunks.append(len(data))
        self.sink.free -= len(data)
        return len(data)


def _output(monkeypatch):
    out = M.Output.__new__(M.Output)
    out.mixer, out.events, out._next_try = M.Mixer(), [], 0.0
    out.sink = FakeSink()
    out.io = FakeIO(out.sink)
    now = [100.0]
    monkeypatch.setattr(M.time, "monotonic", lambda: now[0])
    out.opened_at = out._last_write = now[0]
    reasons = []
    monkeypatch.setattr(out, "reopen", lambda reason: reasons.append(reason))
    return out, now, reasons


def test_watchdog_reopens_when_device_stops_consuming(monkeypatch):
    out, now, reasons = _output(monkeypatch)
    out.pump()
    assert out.io.chunks and not reasons, "先把缓冲推到 AHEAD_MS"
    for _ in range(30):                          # 设备不消费：bytesFree 一直不涨
        now[0] += 0.05
        out.pump()
    assert reasons and "卡住" in reasons[0]


def test_quiet_refresh_only_when_silent(monkeypatch):
    out, now, reasons = _output(monkeypatch)
    now[0] += M.REFRESH_SEC + 1
    out._last_write = now[0]
    out.mixer.last_sound = now[0]                # 刚响过：不换
    out.pump()
    assert not reasons
    now[0] += M.QUIET_SEC + 0.1
    out._last_write = now[0]
    out.pump()
    assert reasons and "定期" in reasons[0]
