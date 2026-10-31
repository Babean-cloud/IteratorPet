"""Read-only extraction of the handful of base sprites an iterator is built from.

Rain World's iterator has no character sheet. ``OracleGraphics`` composes the
whole puppet out of a few generic atlas elements (``Circle20``, ``pixel``,
``CentipedeSegment``, ``LizardScaleA1`` ...) plus vertex colours. So all we need
is those few sprites, cut from the user's own installation and cached locally.

Nothing is redistributed: the cache lives under the user's app-data folder and
is regenerated from their game files. When no installation is present we fall
back to procedurally generated equivalents so tests still run anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_INSTALL = Path(r"F:\steam\steamapps\common\Rain World")
PALETTE_NAME = "palette26"

# Elements used by OracleGraphics / ArmJointGraphics / UbilicalCord / Gown.
SPRITE_NAMES = (
    "Circle20",
    "pixel",
    "CentipedeSegment",
    "MirosLegSmallPart",
    "deerEyeB",
    "LizardScaleA1",
    "haloGlyph-1",
    "KarmaPetal",
    # 数据珍珠（DataPearl.InitiateSprites：本体 JetFishEyeA + 高光 tinyStar + 一层柔光）
    "JetFishEyeA",
    "tinyStar",
)


def cache_root() -> Path:
    import os

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "IteratorPet" / "cache"


@dataclass
class ExtractReport:
    ok: bool
    source: str
    cache_dir: Path
    sprites: dict[str, Path] = field(default_factory=dict)
    palette_path: Path | None = None
    colors: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"source      : {self.source}",
            f"cache       : {self.cache_dir}",
            f"sprites     : {len(self.sprites)}",
            f"palette     : {self.palette_path}",
        ]
        for key, value in self.colors.items():
            lines.append(f"  {key:18s} {value}")
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# generated fallbacks (used when the game is absent)
# --------------------------------------------------------------------------
def _generated(name: str):
    """Produce a pixel-art equivalent of each primitive, drawn at 8x then downsampled."""
    from PIL import Image, ImageDraw

    scale = 8

    def canvas(w: int, h: int):
        img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
        return img, ImageDraw.Draw(img)

    if name == "Circle20":
        img, d = canvas(20, 20)
        d.ellipse((0, 0, 20 * scale - 1, 20 * scale - 1), fill=(255, 255, 255, 255))
        size = (20, 20)
    elif name == "pixel":
        img = Image.new("RGBA", (1, 1), (255, 255, 255, 255))
        size = (1, 1)
    elif name == "CentipedeSegment":
        img, d = canvas(18, 15)
        d.rounded_rectangle((0, 2 * scale, 18 * scale - 1, 13 * scale), radius=6 * scale, fill=(255, 255, 255, 255))
        size = (18, 15)
    elif name == "MirosLegSmallPart":
        img, d = canvas(4, 25)
        d.rounded_rectangle((0, 0, 4 * scale - 1, 25 * scale - 1), radius=2 * scale, fill=(255, 255, 255, 255))
        size = (4, 25)
    elif name == "deerEyeB":
        img, d = canvas(5, 5)
        d.ellipse((0, 0, 5 * scale - 1, 5 * scale - 1), fill=(255, 255, 255, 255))
        size = (5, 5)
    elif name == "LizardScaleA1":
        img, d = canvas(4, 14)
        d.polygon(
            [(2 * scale, 0), (4 * scale, 3 * scale), (3 * scale, 14 * scale), (1 * scale, 14 * scale), (0, 3 * scale)],
            fill=(255, 255, 255, 255),
        )
        size = (4, 14)
    elif name == "haloGlyph-1":
        img, d = canvas(6, 6)
        d.regular_polygon((3 * scale, 3 * scale, 3 * scale), 4, rotation=45, fill=(255, 255, 255, 255))
        size = (6, 6)
    elif name == "KarmaPetal":
        img, d = canvas(6, 20)
        d.ellipse((0, 0, 6 * scale - 1, 20 * scale - 1), fill=(255, 255, 255, 255))
        size = (6, 20)
    elif name == "JetFishEyeA":                     # 珍珠本体（DataPearl.InitiateSprites）
        img, d = canvas(6, 6)
        d.ellipse((0, 0, 6 * scale - 1, 6 * scale - 1), fill=(255, 255, 255, 255))
        size = (6, 6)
    elif name == "tinyStar":                        # 珍珠高光
        img, d = canvas(3, 3)
        d.rectangle((scale, 0, 2 * scale - 1, 3 * scale - 1), fill=(255, 255, 255, 255))
        d.rectangle((0, scale, 3 * scale - 1, 2 * scale - 1), fill=(255, 255, 255, 255))
        size = (3, 3)
    else:  # pragma: no cover - defensive
        img = Image.new("RGBA", (4, 4), (255, 255, 255, 255))
        size = (4, 4)
    return img.resize(size, Image.LANCZOS)


def _generated_palette():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (32, 16), (0, 0, 0))
    d = ImageDraw.Draw(img)
    # Palette 26 (SS_AI) headline colours, sampled from the real palette layout.
    rows = {
        0: (0, 0, 0),
        1: (176, 0, 255),
        2: (255, 49, 22),
        3: (255, 35, 0),
        5: (31, 7, 0),
    }
    for y, colour in rows.items():
        for x in range(32):
            d.point((x, y), fill=colour)
    return img


# --------------------------------------------------------------------------
# extraction from the local installation
# --------------------------------------------------------------------------
def _load_atlas(install: Path):
    """Return (atlas image, frame table) from resources.assets, or None."""
    assets = install / "RainWorld_Data" / "resources.assets"
    if not assets.is_file():
        return None, None, f"missing {assets}"
    try:
        import UnityPy
    except ImportError:
        return None, None, "UnityPy is not installed"

    env = UnityPy.load(str(assets))
    atlas_image = None
    frame_table = None
    for obj in env.objects:
        try:
            name = obj.peek_name()
        except Exception:
            continue
        if obj.type.name == "Texture2D" and name == "rainWorld":
            atlas_image = obj.read().image
        elif obj.type.name == "TextAsset" and name == "rainWorld":
            payload = obj.read().m_Script
            text = payload if isinstance(payload, str) else bytes(payload).decode("utf-8", errors="replace")
            frame_table = json.loads(text).get("frames", {})
    if atlas_image is None:
        return None, None, "rainWorld texture not found"
    if frame_table is None:
        return None, None, "rainWorld frame table not found"
    return atlas_image, frame_table, ""


def _crop(atlas, frame_table, name: str):
    entry = frame_table.get(f"{name}.png")
    if not entry:
        return None
    frame = entry["frame"]
    box = (frame["x"], frame["y"], frame["x"] + frame["w"], frame["y"] + frame["h"])
    return atlas.crop(box)


def _load_palette(install: Path):
    path = install / "RainWorld_Data" / "StreamingAssets" / "palettes" / f"{PALETTE_NAME}.png"
    if not path.is_file():
        return None
    from PIL import Image

    return Image.open(path).convert("RGB")


def _palette_colors(palette) -> dict[str, tuple[int, int, int]]:
    if palette is None:
        return {}
    width, height = palette.size
    sample = lambda x, y: palette.getpixel((min(x, width - 1), min(y, height - 1)))
    return {
        "black": sample(0, 0),
        "metal_blend": sample(5, 5),
        "arm_base_b": sample(5, 1),
        "arm_highlight_b": sample(5, 2),
        "cloth_hot": sample(5, 2),
        "cloth_warm": sample(5, 3),
        "cloth_deep": sample(3, 5),
    }


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def extract(install: Path = DEFAULT_INSTALL, cache_dir: Path | None = None, force: bool = False) -> ExtractReport:
    """Cut the required sprites out of the local game into the user cache."""
    cache = Path(cache_dir) if cache_dir else cache_root()
    sprite_dir = cache / "sprites"
    sprite_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    atlas, frames, reason = _load_atlas(Path(install))
    palette = _load_palette(Path(install))
    source = "game" if atlas is not None else "generated"
    if atlas is None:
        notes.append(f"using generated primitives ({reason})")

    sprites: dict[str, Path] = {}
    for name in SPRITE_NAMES:
        image = _crop(atlas, frames, name) if atlas is not None else None
        if image is None:
            if atlas is not None:
                notes.append(f"{name}: not in atlas, generated instead")
            image = _generated(name)
        target = sprite_dir / f"{name}.png"
        if force or not target.is_file():
            image.save(target)
        sprites[name] = target

    palette_path = cache / f"{PALETTE_NAME}.png"
    if palette is None:
        palette = _generated_palette()
        if "generated" not in source:
            notes.append("palette26 missing, generated instead")
    if force or not palette_path.is_file():
        palette.save(palette_path)

    colors = _palette_colors(palette)
    manifest = {
        "source": source,
        "install": str(install),
        "sprites": {k: v.name for k, v in sprites.items()},
        "palette": palette_path.name,
        "colors": {k: list(v) for k, v in colors.items()},
        "notes": notes,
    }
    (cache / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return ExtractReport(
        ok=True,
        source=source,
        cache_dir=cache,
        sprites=sprites,
        palette_path=palette_path,
        colors=colors,
        notes=notes,
    )


def ensure_sprites(cache_dir: Path | None = None, install: Path | None = None) -> ExtractReport:
    """Return the cache, extracting only when it is incomplete.

    ``install`` 为空时退回 ``DEFAULT_INSTALL``（开发机）；发布版由 `launcher.py`
    把自动探测到的目录传进来。
    """
    cache = Path(cache_dir) if cache_dir else cache_root()
    manifest_path = cache / "manifest.json"
    complete = manifest_path.is_file() and all(
        (cache / "sprites" / f"{name}.png").is_file() for name in SPRITE_NAMES
    )
    if complete:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return ExtractReport(
            ok=True,
            source=data.get("source", "cache"),
            cache_dir=cache,
            sprites={n: cache / "sprites" / f"{n}.png" for n in SPRITE_NAMES},
            palette_path=cache / f"{PALETTE_NAME}.png",
            colors={k: tuple(v) for k, v in data.get("colors", {}).items()},
            notes=list(data.get("notes", [])),
        )
    return extract(Path(install) if install else DEFAULT_INSTALL, cache_dir=cache)


# --------------------------------------------------------------------------
# 界面用的菜单图元（uiSprites 图集）：对话窗口 / 面板 / 菜单照游戏菜单的样子画
# --------------------------------------------------------------------------
# 名字 → 用途：listDivider 是两头渐隐的分隔线；CheckBox 是勾；Arrow / pageright 是箭头；
# InfoI 是"i"。和人偶图元一样只从用户本机的游戏里切，存进本地缓存，不随仓库分发。
UI_SPRITE_NAMES = ("listDivider", "Menu_Symbol_CheckBox", "Menu_Symbol_Arrow",
                   "pageright", "pageleft", "Menu_InfoI")


def ui_dir(cache_dir: Path | None = None) -> Path:
    return (Path(cache_dir) if cache_dir else cache_root()) / "ui"


def _generated_ui(name: str):
    """没有游戏时的替身：形状对、观感接近就行。"""
    from PIL import Image, ImageDraw

    if name == "listDivider":
        img = Image.new("RGBA", (140, 2), (0, 0, 0, 0))
        for x in range(140):
            alpha = int(255 * max(0.0, 1.0 - abs(x - 69.5) / 70.0) ** 0.6)
            img.putpixel((x, 0), (255, 255, 255, alpha))
            img.putpixel((x, 1), (255, 255, 255, alpha))
        return img
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    if name == "Menu_Symbol_CheckBox":
        d.line([(2, 8), (6, 12), (14, 3)], fill=white, width=2)
    elif name == "Menu_Symbol_Arrow":
        d.polygon([(8, 4), (14, 11), (2, 11)], fill=white)
    elif name in ("pageright", "pageleft"):
        d.polygon([(2, 6), (9, 6), (9, 2), (15, 8), (9, 14), (9, 10), (2, 10)], fill=white)
        if name == "pageleft":
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
    else:
        d.rectangle((7, 2, 9, 4), fill=white)
        d.rectangle((7, 6, 9, 14), fill=white)
    return img


def _load_ui_atlas(install: Path):
    assets = install / "RainWorld_Data" / "resources.assets"
    if not assets.is_file():
        return None, None
    try:
        import UnityPy
    except ImportError:
        return None, None
    image = table = None
    for obj in UnityPy.load(str(assets)).objects:
        try:
            name = obj.peek_name()
        except Exception:
            continue
        if name != "uiSprites":
            continue
        if obj.type.name == "Texture2D":
            image = obj.read().image
        elif obj.type.name == "TextAsset":
            payload = obj.read().m_Script
            text = payload if isinstance(payload, str) else bytes(payload).decode("utf-8", errors="replace")
            table = json.loads(text).get("frames", {})
    return image, table


def ensure_ui_sprites(install: Path | None = None, cache_dir: Path | None = None) -> dict[str, Path]:
    """缓存里没有就切一次（另附一张朝右的小箭头，给菜单的子菜单标记用）。"""
    target = ui_dir(cache_dir)
    wanted = {name: target / f"{name}.png" for name in UI_SPRITE_NAMES}
    wanted["arrow_right"] = target / "arrow_right.png"
    if all(path.is_file() for path in wanted.values()):
        return wanted
    target.mkdir(parents=True, exist_ok=True)
    atlas, frames = _load_ui_atlas(Path(install) if install else DEFAULT_INSTALL)
    for name in UI_SPRITE_NAMES:
        image = _crop(atlas, frames, name) if atlas is not None else None
        (image or _generated_ui(name)).save(wanted[name])
    from PIL import Image

    Image.open(wanted["Menu_Symbol_Arrow"]).rotate(-90, expand=True).save(wanted["arrow_right"])
    return wanted


# --------------------------------------------------------------------------
# 音效：迭代器说话的"哔哔啵啵"、机械臂伺服、放电……和图元一样只从用户本机的游戏里取，不随仓库分发。
# 游戏的 StreamingAssets/loadedsoundeffects/*.wav 全是 0 字节的占位，真东西在 AssetBundles/loadedsoundeffects
# 包里（约 1 GB、1600 多段）；哪个事件用哪段、多大音量写在 StreamingAssets/soundeffects/sounds.txt。
# --------------------------------------------------------------------------
SOUND_IDS = {
    # 桌宠里的名字: 游戏 sounds.txt 里的事件
    "talk_pebbles": ("SS_AI_Talk_1", "SS_AI_Talk_2", "SS_AI_Talk_3", "SS_AI_Talk_4", "SS_AI_Talk_5"),
    "talk_moon": ("SL_AI_Talk_1", "SL_AI_Talk_2", "SL_AI_Talk_3", "SL_AI_Talk_4", "SL_AI_Talk_5"),
    "text": ("SS_AI_Text",),
    "arm": ("SS_AI_Arm_Joint_LOOP",),
    "base": ("SS_AI_Base_Move_LOOP",),
    "halo": ("SS_AI_Halo_Connection_Light_Up",),
    "zap": ("Zapper_Zap",),
    "electric": ("Electricity_Loop",),
    "databit": ("Data_Bit",),
    "swap": ("SS_AI_Exit_Work_Mode",),
}
# sounds.txt 读不到时的后备（照 v1.9 的 sounds.txt 抄的）
SOUND_FALLBACK = {
    "SS_AI_Talk_1": [("PebblesChat1", 0.7)], "SS_AI_Talk_2": [("PebblesChat2", 0.6)],
    "SS_AI_Talk_3": [("PebblesChat3", 0.7)], "SS_AI_Talk_4": [("PebblesChat4", 0.7)],
    "SS_AI_Talk_5": [("PebblesChat5", 0.7)],
    **{f"SL_AI_Talk_{i}": [(f"MoonChat{i}", 0.5)] for i in range(1, 6)},
    "SS_AI_Text": [("beep4A", 0.5), ("beep4B", 0.5)],
    "SS_AI_Arm_Joint_LOOP": [("ServoA", 0.6)], "SS_AI_Base_Move_LOOP": [("ServoC", 0.4)],
    "SS_AI_Halo_Connection_Light_Up": [("MyceliaSparkA", 0.4)], "Zapper_Zap": [("Arc", 0.6)],
    "Electricity_Loop": [("electricityLoop", 0.85)], "Data_Bit": [("DataBit", 0.25)],
    "SS_AI_Exit_Work_Mode": [("GravityOff", 0.6)],
}


def sound_dir(cache_dir: Path | None = None) -> Path:
    return (Path(cache_dir) if cache_dir else cache_root()) / "sounds"


def parse_sounds_txt(text: str) -> dict[str, list[tuple[str, float]]]:
    """``SS_AI_Talk_1 : PebblesChat1/vol=0.7`` → ``{"SS_AI_Talk_1": [("PebblesChat1", 0.7)]}``。
    ``minVol/maxVol`` 取平均；没写音量就是 1。"""
    import re

    table: dict[str, list[tuple[str, float]]] = {}
    for line in text.splitlines():
        if ":" not in line or line.lstrip().startswith("//"):
            continue
        head, _, tail = line.partition(":")
        sound_id = head.strip().split("/")[0]
        clips = []
        for part in tail.split(","):
            fields = part.strip().split("/")
            if not fields[0]:
                continue
            opts = dict(f.split("=", 1) for f in fields[1:] if "=" in f)
            try:
                if "vol" in opts:
                    volume = float(opts["vol"])
                elif "minVol" in opts and "maxVol" in opts:
                    volume = (float(opts["minVol"]) + float(opts["maxVol"])) / 2
                else:
                    volume = 1.0
            except ValueError:
                volume = 1.0
            clips.append((fields[0].strip(), round(volume, 3)))
        if sound_id and clips and re.fullmatch(r"[A-Za-z0-9_]+", sound_id):
            table[sound_id] = clips
    return table


def ensure_sounds(install: Path | None = None, cache_dir: Path | None = None) -> dict:
    """缓存里没有就从游戏里解一次（UnityPy 解 AudioClip → wav）。返回清单
    ``{名字: [{"file", "vol"}, ...]}``；没有游戏 / 解不出来就是空清单（桌宠就安静着）。"""
    import re

    target = sound_dir(cache_dir)
    manifest = target / "sounds.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("version") == 1 and all((target / c["file"]).is_file()
                                            for clips in data["sounds"].values() for c in clips):
            return data["sounds"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    install = Path(install) if install else DEFAULT_INSTALL
    streaming = install / "RainWorld_Data" / "StreamingAssets"
    bundle = streaming / "AssetBundles" / "loadedsoundeffects"
    if not bundle.is_file():
        return {}
    try:
        table = parse_sounds_txt((streaming / "soundeffects" / "sounds.txt").read_text(encoding="utf-8",
                                                                                       errors="replace"))
    except OSError:
        table = {}
    wanted: dict[str, list[tuple[str, float]]] = {}
    for key, ids in SOUND_IDS.items():
        wanted[key] = [clip for sound_id in ids for clip in (table.get(sound_id) or SOUND_FALLBACK.get(sound_id, []))]
    bases = {base for clips in wanted.values() for base, _ in clips}
    try:
        import UnityPy
    except ImportError:
        return {}
    env = UnityPy.load(str(bundle))
    found: dict[str, list] = {base: [] for base in bases}
    for obj in env.objects:
        if obj.type.name != "AudioClip":
            continue
        try:
            name = obj.peek_name()
        except Exception:
            continue
        match = re.fullmatch(r"(.+?)(?:_\d+)?", name or "")
        for base in (name, match.group(1) if match else None):
            if base in found:
                found[base].append((name, obj))
                break
    target.mkdir(parents=True, exist_ok=True)
    sounds: dict[str, list[dict]] = {}
    for key, clips in wanted.items():
        entries = []
        for base, volume in clips:
            for name, obj in sorted(found.get(base, []), key=lambda item: item[0])[:8]:
                try:
                    samples = obj.read().samples
                except Exception:
                    continue
                if not samples:
                    continue
                file = f"{name}.wav"
                (target / file).write_bytes(next(iter(samples.values())))
                entries.append({"file": file, "vol": round(volume, 3)})
        if entries:
            sounds[key] = entries
    manifest.write_text(json.dumps({"version": 1, "sounds": sounds}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    return sounds


def prepare(install: Path | None = None) -> ExtractReport | None:
    """启动时该调的那一个入口：缓存齐了立刻返回，缺了才切素材。

    **只能在启动器/这里提取**：绘制路径绝不能触发 UnityPy 解包——早先
    ``rendering/sprites._raw`` 就在 paintEvent 里调了 ``ensure_sprites``，
    全新环境第一次绘制会解整个 resources.assets，失败时直接“启动失败”。
    """
    try:
        report = ensure_sprites(install=Path(install) if install else None)
    except Exception:  # 切素材失败不该拦住桌宠：用占位图元继续
        report = None
    try:
        ensure_ui_sprites(install=Path(install) if install else None)
    except Exception:  # 界面图元缺了只是退回 QSS 画的样子
        pass
    return report


def sprite_path(name: str, cache_dir: Path | None = None) -> Path:
    cache = Path(cache_dir) if cache_dir else cache_root()
    return cache / "sprites" / f"{name}.png"
