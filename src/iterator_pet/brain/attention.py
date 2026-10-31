"""注意力截图的便宜判定；只处理内存里的缩略图，不负责截图或 OCR。"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageStat

THUMB_SIZE = (96, 64)
TILES = (12, 8)
BASE_INTERVAL = 15.0
MAX_INTERVAL = 300.0


def thumbnail(image: Image.Image) -> Image.Image:
    """把窗口缩成固定大小的灰度图。"""
    return image.convert("L").resize(THUMB_SIZE, Image.Resampling.BILINEAR)


def dhash(image: Image.Image) -> int:
    """64 位感知指纹；相同值表示缩略图的横向明暗关系没变。"""
    small = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    pixels = small.tobytes()
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return value


@dataclass(frozen=True)
class FrameChange:
    heat: tuple[float, ...]
    volatility: tuple[float, ...]
    important_tiles: tuple[tuple[int, int], ...]
    fingerprint: int

    @property
    def changed(self) -> bool:
        return bool(self.important_tiles)


def compare_frames(previous: Image.Image, current: Image.Image,
                   volatility: tuple[float, ...] | None = None) -> FrameChange:
    """每格测变化；长期闪动的格子逐渐降权。输入图不会被修改。"""
    before = thumbnail(previous)
    after = thumbnail(current)
    diff = ImageChops.difference(before, after)
    old = volatility or (0.0,) * (TILES[0] * TILES[1])
    if len(old) != TILES[0] * TILES[1]:
        raise ValueError("volatility 格子数不对")
    heat = []
    updated = []
    important = []
    for y in range(TILES[1]):
        for x in range(TILES[0]):
            index = y * TILES[0] + x
            tile = diff.crop((x * 8, y * 8, x * 8 + 8, y * 8 + 8))
            value = ImageStat.Stat(tile).mean[0]
            heat.append(value)
            if value >= 12 and value > old[index] * 1.7:
                important.append((x, y))
            updated.append(old[index] * 0.65 + value * 0.35)
    return FrameChange(tuple(heat), tuple(updated), tuple(important), dhash(after))


def next_interval(current: float, *, stable: bool = False,
                  changed: bool = False) -> float:
    """稳定时逐步延长 L2 间隔；真变化时回到基础间隔。"""
    if changed:
        return BASE_INTERVAL
    if stable:
        return min(MAX_INTERVAL, max(BASE_INTERVAL, current) * 1.6)
    return max(BASE_INTERVAL, min(MAX_INTERVAL, current))


def regions_from_tiles(tiles: tuple[tuple[int, int], ...], size: tuple[int, int]
                       ) -> list[tuple[int, int, int, int]]:
    """相邻的变化格合成区域，四周外扩一格；最多取两块。"""
    remaining = set(tiles)
    groups = []
    while remaining:
        group = {remaining.pop()}
        frontier = list(group)
        while frontier:
            x, y = frontier.pop()
            neighbors = {(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
            for neighbor in neighbors & remaining:
                remaining.remove(neighbor)
                group.add(neighbor)
                frontier.append(neighbor)
        groups.append(group)
    width, height = size
    boxes = []
    for group in sorted(groups, key=len, reverse=True)[:2]:
        x0 = max(0, min(x for x, _ in group) - 1)
        y0 = max(0, min(y for _, y in group) - 1)
        x1 = min(TILES[0], max(x for x, _ in group) + 2)
        y1 = min(TILES[1], max(y for _, y in group) + 2)
        boxes.append((x0 * width // TILES[0], y0 * height // TILES[1],
                      (x1 * width + TILES[0] - 1) // TILES[0],
                      (y1 * height + TILES[1] - 1) // TILES[1]))
    return boxes
