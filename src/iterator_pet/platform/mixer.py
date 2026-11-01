"""一条混音流代替 25 个 QSoundEffect（失声修复，用户 2026-11-01）。

之前每个音效各开一条系统音频流。实测跑一阵子就全没声了：诊断里每个音效都是 Ready、正在播，
别的程序照常有声，换耳机、换扬声器都会复现——是 Qt 那些流自己哑了，而我们看不见。

现在自己混音：所有音效在 numpy 里叠成一条 44.1 kHz 立体声，只开一条 ``QAudioSink`` 往里推。
这条流由我们看着：

* 1 秒推不进去（设备不消费）→ 重开；
* 系统默认输出设备变了 → 跟过去重开；
* 出错 → 重开；
* 安静了一会儿、且距上次打开超过 ``REFRESH_SEC`` → 悄悄重开一次（没在响，听不出来）。
  流"在消费但没声"这种情况看不见，只能定期换新的兜底。

``Clip`` 对外长得像 QSoundEffect（setVolume / play / stop / isPlaying / status / source），
``SoundBoard`` 的逻辑和测试都不用动。
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np

RATE = 44100
CHANNELS = 2
FRAME_BYTES = CHANNELS * 2              # int16
PUMP_MS = 20                            # 多久往流里推一次
AHEAD_MS = 100                          # 流里最多压多少还没放的声音（= 延迟上限）
BUFFER_MS = 250
STALL_SEC = 1.0                         # 这么久一个字节都推不进去 = 流卡死了
REFRESH_SEC = 120.0                     # 安静时多久换一条新流
QUIET_SEC = 2.0                         # 没声音多久算"安静"
RETRY_SEC = 2.0                         # 重开失败后多久再试


def load_wav(path: Path, rate: int = RATE) -> np.ndarray:
    """读成 (帧数, 2) 的 float32，-1..1；单声道复制成两路，采样率不对就线性插值过去。"""
    with wave.open(str(path), "rb") as w:
        ch, width, src_rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        data = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        data = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的采样宽度 {width}")
    data = data.reshape(-1, ch)
    data = np.repeat(data[:, :1], 2, 1) if ch == 1 else data[:, :2]
    if src_rate != rate and len(data) > 1:
        m = max(1, int(round(len(data) * rate / src_rate)))
        x = np.linspace(0, len(data) - 1, m)
        data = np.stack([np.interp(x, np.arange(len(data)), data[:, c]) for c in range(2)], 1)
    return np.ascontiguousarray(data, np.float32)


class _Status:
    def __init__(self, name: str):
        self.name = name


class Clip:
    """一个音效：数据 + 播放位置 + 音量。接口照 QSoundEffect 的子集。"""

    def __init__(self, mixer: "Mixer", path: Path, loop: bool = False):
        self.mixer, self.path, self.loop = mixer, Path(path), loop
        try:
            self.data = load_wav(self.path)
            self._status = "Ready"
        except (OSError, EOFError, ValueError, wave.Error):
            self.data = np.zeros((0, 2), np.float32)
            self._status = "Error"
        self.pos = 0
        self._volume = 1.0

    def setVolume(self, volume: float) -> None:  # noqa: N802
        self._volume = max(0.0, min(1.0, float(volume)))

    def volume(self) -> float:
        return self._volume

    def play(self) -> None:
        """和 QSoundEffect 一样：在放的话从头再来。"""
        if len(self.data):
            self.pos = 0
            self.mixer.active.add(self)

    def stop(self) -> None:
        self.mixer.active.discard(self)

    def isPlaying(self) -> bool:  # noqa: N802
        return self in self.mixer.active

    def isMuted(self) -> bool:  # noqa: N802
        return False

    def loopsRemaining(self) -> int:  # noqa: N802
        return -2 if self.loop and self.isPlaying() else int(self.isPlaying())

    def status(self) -> _Status:
        return _Status(self._status)

    def source(self):
        from PySide6.QtCore import QUrl

        return QUrl.fromLocalFile(str(self.path))


class Mixer:
    def __init__(self):
        self.active: set[Clip] = set()
        self.last_sound = 0.0                    # 最近一次混进非零声音的时刻（monotonic）

    def mix(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), np.float32)
        for clip in list(self.active):
            data, n, vol = clip.data, len(clip.data), clip._volume
            if clip.loop:
                out += data[(clip.pos + np.arange(frames)) % n] * vol
                clip.pos = (clip.pos + frames) % n
            else:
                chunk = data[clip.pos:clip.pos + frames]
                out[:len(chunk)] += chunk * vol
                clip.pos += len(chunk)
                if clip.pos >= n:
                    self.active.discard(clip)
        if self.active:
            self.last_sound = time.monotonic()
        return out

    @staticmethod
    def to_pcm(samples: np.ndarray) -> bytes:
        return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class Output:
    """唯一的那条音频流 + 看门狗。主线程里跑（QTimer）。"""

    def __init__(self, mixer: Mixer):
        from PySide6.QtCore import QTimer
        from PySide6.QtMultimedia import QMediaDevices

        self.mixer = mixer
        self.sink = None
        self.io = None
        self.device_name = ""
        self.device_id = b""
        self.opened_at = 0.0
        self._last_write = 0.0
        self._next_try = 0.0
        self.events: list[str] = []              # 重开记录（诊断用，最多留 30 条）
        self._devices = QMediaDevices()
        self._devices.audioOutputsChanged.connect(self._devices_changed)
        self._timer = QTimer()
        self._timer.setInterval(PUMP_MS)
        self._timer.timeout.connect(self.pump)
        self.open()
        self._timer.start()

    def _devices_changed(self) -> None:
        """设备列表变了（启动时 Qt 也会发一次）：默认输出真的换了才重开。"""
        from PySide6.QtMultimedia import QMediaDevices

        if QMediaDevices.defaultAudioOutput().id() != self.device_id or self.sink is None:
            self.reopen("默认输出设备变了")

    def _log(self, text: str) -> None:
        self.events.append(time.strftime("%H:%M:%S ") + text)
        del self.events[:-30]

    def open(self) -> bool:
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

        self.close()
        now = time.monotonic()
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            self._next_try = now + RETRY_SEC
            self._log("没有可用的输出设备")
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        sink = QAudioSink(device, fmt)
        sink.setBufferSize(int(RATE * BUFFER_MS / 1000) * FRAME_BYTES)
        io = sink.start()
        if io is None:
            self._next_try = now + RETRY_SEC
            self._log(f"打不开 {device.description()}")
            return False
        self.sink, self.io, self.device_name, self.device_id = sink, io, device.description(), device.id()
        self.opened_at = self._last_write = now
        self._log(f"打开 {self.device_name}")
        return True

    def close(self) -> None:
        if self.sink is not None:
            self.sink.stop()
            self.sink.deleteLater()
        self.sink = self.io = None

    def reopen(self, reason: str) -> None:
        self._log("重开：" + reason)
        self.open()

    def pump(self) -> None:
        from PySide6.QtMultimedia import QtAudio

        now = time.monotonic()
        sink = self.sink
        if sink is None:
            if now >= self._next_try:
                self.open()
            return
        if sink.error() != QtAudio.Error.NoError:
            self.reopen(f"出错 {sink.error().name}")
            return
        if now - self._last_write > STALL_SEC:
            self.reopen("流卡住了（1 秒推不进去）")
            return
        if (not self.mixer.active and now - self.mixer.last_sound > QUIET_SEC
                and now - self.opened_at > REFRESH_SEC):
            self.reopen("定期换新（安静时）")
            return
        free = sink.bytesFree()
        queued = sink.bufferSize() - free
        want = int(RATE * AHEAD_MS / 1000) * FRAME_BYTES - queued
        frames = min(free, want) // FRAME_BYTES
        if frames <= 0:
            return
        written = self.io.write(Mixer.to_pcm(self.mixer.mix(frames)))
        if written and written > 0:
            self._last_write = now

    def describe(self) -> dict:
        sink = self.sink
        return {"device": self.device_name, "open": sink is not None,
                "state": sink.state().name if sink is not None else None,
                "error": sink.error().name if sink is not None else None,
                "opened_seconds_ago": round(time.monotonic() - self.opened_at, 1),
                "playing": len(self.mixer.active), "events": list(self.events)}
