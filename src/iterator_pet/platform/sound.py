"""音效：原版迭代器说话的"哔哔啵啵"、字幕投影的轻响、机械臂伺服、底座滑动、电光、放电、读珍珠、换角色。

声音从用户本机的游戏里取（``assets_extractor.ensure_sounds``，第一次在后台线程解，之后读缓存），
不随仓库分发；没装游戏就安静着。只看仿真画出来的状态（``RenderState``），不改仿真一个字。

* 说话：一句台词刚出现时放一次 PebblesChat / MoonChat（原作 SS_AI_Talk / SL_AI_Talk），
  字幕逐字出来时每几个字一声很轻的 beep（原作投影文字的 SS_AI_Text）；
* 动作：机械臂关节动得越快伺服声越响（SS_AI_Arm_Joint_LOOP），底座沿轨道滑动时一段伺服（Base_Move）；
  光环电光新亮起来时偶尔一声（Halo_Connection_Light_Up，限频）；放电赶鼠标：蓄力时电流声渐强，
  放出去那一下 Zapper_Zap；
* 其他：读珍珠的每一句前一声 Data_Bit，换角色荡出去时 Exit_Work_Mode。

音量 = 游戏 sounds.txt 里的音量 × 总音量 × 这一类的开关。全屏勿扰 / 暂停时静音。
"""

from __future__ import annotations

import math
import random
import threading
import time
from pathlib import Path

LOOP_KEYS = ("arm", "base", "electric")
TEXT_EVERY_CHARS = 4          # 字幕每几个字一声 beep
HALO_MIN_GAP = 1.6            # 电光声最多多久一次（20 根电光，不限频就是一片噼啪）
ARM_DEAD_ZONE = 1.5           # 关节每帧平均位移（仿真像素）低于这个不响
ARM_FULL_SPEED = 8.0          # 超出死区这么多，伺服声算满


class SoundBoard:
    def __init__(self, config, install: str | None = None, cache_dir: Path | None = None, extract=None):
        self.config = config
        self.install = install
        self.cache_dir = cache_dir
        self._extract = extract
        self.sounds: dict[str, list[dict]] = {}
        self.effects: dict[str, list] = {}       # 名字 → [(QSoundEffect, 游戏音量)]
        self.loops: dict[str, tuple] = {}         # 名字 → (QSoundEffect, 游戏音量)
        self.mixer = None                         # 真的载入了才有（mixer.py）
        self.output = None
        self._levels = {key: 0.0 for key in LOOP_KEYS}
        self._pending: dict | None = None
        self._loading = False
        self.muted = False
        self.error: str | None = None
        self._created_at = time.monotonic()
        # 上一帧看到的东西：判断"刚开始说一句""电光刚亮"
        self._line = None
        self._chars = 0
        self._joints = None
        self._base_point = None
        self._sparks: list[float] = []
        self._kill_phase = "idle"
        self._last_halo = 0.0

    # -- 设置 -----------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "sound_enabled", True))

    @property
    def volume(self) -> float:
        return max(0.0, min(1.0, float(getattr(self.config, "sound_volume", 0.6))))

    def _allowed(self, key: str) -> bool:
        if not self.enabled or self.muted:
            return False
        if key in ("talk_pebbles", "talk_moon", "text", "databit"):
            return bool(getattr(self.config, "sound_speech", True))
        return bool(getattr(self.config, "sound_actions", True))

    # -- 载入：解包在后台线程，QSoundEffect 只能在主线程建 ----------------------------
    def start(self) -> None:
        if self._loading or self.effects:
            return
        self._loading = True

        def work():
            try:
                extract = self._extract
                if extract is None:
                    from ..assets_extractor import ensure_sounds as extract
                self._pending = extract(self.install, self.cache_dir) or {}
            except Exception as exc:                  # noqa: BLE001 —— 没声音不该拦住桌宠
                self.error = f"{type(exc).__name__}: {exc}"
                self._pending = {}

        threading.Thread(target=work, daemon=True).start()

    def _load(self, sounds: dict) -> None:
        # 所有音效混成一条流（mixer.py）：每个音效各开一条 QSoundEffect 流，跑一阵会全部哑掉
        from ..assets_extractor import sound_dir
        from .mixer import Clip, Mixer, Output

        folder = sound_dir(self.cache_dir)
        self.sounds = sounds
        self.mixer = Mixer()
        self.output = Output(self.mixer)
        for key, clips in sounds.items():
            loaded = []
            for clip in clips:
                effect = Clip(self.mixer, folder / clip["file"], loop=key in LOOP_KEYS)
                loaded.append((effect, float(clip.get("vol", 1.0))))
            if not loaded:
                continue
            if key in LOOP_KEYS:
                self.loops[key] = loaded[0]
            else:
                self.effects[key] = loaded

    def save_diagnostics(self) -> Path:
        """On-demand snapshot only; never poll audio devices from the frame loop."""
        import json
        import os
        from datetime import datetime
        from PySide6.QtCore import qVersion
        from ..config import config_dir

        rows = []
        for key, clips in list(self.effects.items()) + [(k, [v]) for k, v in self.loops.items()]:
            for effect, volume in clips:
                rows.append({"key": key, "file": Path(effect.source().toLocalFile()).name,
                             "status": effect.status().name, "playing": effect.isPlaying(),
                             "muted": effect.isMuted(), "volume": effect.volume(),
                             "loops_remaining": effect.loopsRemaining()})
        data = {"time": datetime.now().astimezone().isoformat(), "pid": os.getpid(),
                "uptime_seconds": round(time.monotonic() - self._created_at, 1),
                "qt": qVersion(), "enabled": self.enabled, "muted": self.muted,
                "volume": self.volume, "speech": getattr(self.config, "sound_speech", True),
                "actions": getattr(self.config, "sound_actions", True), "error": self.error,
                "output": self.output.describe() if self.output is not None else None,
                "effects": rows}
        folder = config_dir() / "audio_diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @property
    def ready(self) -> bool:
        return bool(self.effects or self.loops)

    # -- 播放 -----------------------------------------------------------------
    def play(self, key: str, gain: float = 1.0) -> bool:
        if not self._allowed(key) or key not in self.effects:
            return False
        effect, volume = random.choice(self.effects[key])
        effect.setVolume(max(0.0, min(1.0, volume * self.volume * gain)))
        effect.play()
        return True

    def _set_loop(self, key: str, level: float) -> None:
        loop = self.loops.get(key)
        if loop is None:
            return
        effect, volume = loop
        level = level if self._allowed(key) else 0.0
        # 平滑：起得快、落得慢，伺服声不会一跳一跳
        current = self._levels[key]
        current += (level - current) * (0.35 if level > current else 0.12)
        self._levels[key] = current
        if current < 0.03:
            if effect.isPlaying():
                effect.stop()
            return
        effect.setVolume(max(0.0, min(1.0, volume * self.volume * current)))
        if not effect.isPlaying():
            effect.play()

    def silence(self) -> None:
        self._base_point = None
        for key in LOOP_KEYS:
            self._levels[key] = 0.0
            loop = self.loops.get(key)
            if loop is not None and loop[0].isPlaying():
                loop[0].stop()

    # -- 每帧（主线程）：看画出来的状态决定响什么 -----------------------------------
    def update(self, state, dialogue, character: str, reading_pearl: bool = False) -> None:
        if self._pending is not None:
            sounds, self._pending = self._pending, None
            self._loading = False
            if sounds:
                self._load(sounds)
        if not self.ready or state is None:
            return
        if self.muted or not self.enabled:
            self.silence()
            return
        self._speech(dialogue, character, reading_pearl)
        self._motion(state)

    def _speech(self, dialogue, character: str, reading_pearl: bool) -> None:
        text = getattr(dialogue, "text", "") or ""
        if not text:
            self._line, self._chars = None, 0
            return
        line = (id(dialogue), text, getattr(dialogue, "total", 0))
        visible = dialogue.visible_chars() if hasattr(dialogue, "visible_chars") else len(text)
        if line != self._line or visible < self._chars:
            self._line, self._chars = line, visible
            if reading_pearl:
                self.play("databit")
            self.play("talk_moon" if character == "past_moon" else "talk_pebbles")
            return
        if visible - self._chars >= TEXT_EVERY_CHARS and visible < len(text):
            self._chars = visible
            self.play("text", gain=0.45)
        elif visible >= len(text):
            self._chars = visible

    def _motion(self, state) -> None:
        joints = list(getattr(state, "joints", []) or [])
        if self._joints is not None and len(self._joints) == len(joints) and joints:
            moved = sum(math.dist(a, b) for a, b in zip(joints, self._joints)) / len(joints)
            # 慢悠悠地漂时不响（一直嗡嗡会烦），真正甩起来才响；平方曲线让小动作更轻
            level = min(1.0, max(0.0, (moved - ARM_DEAD_ZONE) / ARM_FULL_SPEED))
            self._set_loop("arm", level * level)
        self._joints = joints
        # base_moving is the controller's intent (also True while a held carriage
        # has already reached its target). Only audible motion should drive sound.
        point = getattr(state, "base_point", None)
        moved = (point is not None and self._base_point is not None
                 and math.dist(point, self._base_point) > 1e-4)
        self._base_point = tuple(point) if point is not None else None
        self._set_loop("base", 0.8 if moved else 0.0)

        sparks = [float(s[2]) for s in getattr(state, "halo_sparks", []) or []]
        now = time.monotonic()
        if len(sparks) == len(self._sparks) and now - self._last_halo >= HALO_MIN_GAP:
            if any(b > 0.6 and a < 0.2 for a, b in zip(self._sparks, sparks)):
                self._last_halo = now
                self.play("halo", gain=0.6)
        self._sparks = sparks

        phase = getattr(state, "kill_phase", "idle")
        charge = float(getattr(state, "kill_fac", 0.0) or 0.0)
        self._set_loop("electric", min(1.0, charge * 1.2) if phase in ("warning", "charging") else 0.0)
        if phase == "flash" and self._kill_phase != "flash":
            self.play("zap")
        self._kill_phase = phase
