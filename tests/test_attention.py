from PIL import Image, ImageDraw

from iterator_pet.brain.attention import compare_frames, dhash, next_interval, regions_from_tiles, thumbnail


def _frame(box=None):
    image = Image.new("RGB", (960, 640), "black")
    if box:
        ImageDraw.Draw(image).rectangle(box, fill="white")
    return image


def test_continuous_flicker_loses_priority():
    dark = _frame()
    bright = _frame((0, 0, 79, 79))
    volatility = None
    for before, after in [(dark, bright), (bright, dark)] * 5:
        result = compare_frames(before, after, volatility)
        volatility = result.volatility
    assert not result.changed
    assert result.heat[0] > 12


def test_sudden_region_change_is_important():
    before = _frame()
    after = _frame((480, 240, 639, 399))
    result = compare_frames(before, after)
    assert result.changed
    assert (6, 3) in result.important_tiles
    assert result.fingerprint != dhash(thumbnail(before))


def test_stable_scene_increases_interval():
    interval = 15.0
    for _ in range(8):
        interval = next_interval(interval, stable=True)
    assert interval > 15.0
    assert next_interval(interval, changed=True) == 15.0
    for _ in range(20):
        interval = next_interval(interval, stable=True)
    assert interval == 300.0


def test_changed_tiles_become_expanded_region():
    assert regions_from_tiles(((6, 3), (7, 3)), (960, 640)) == [(400, 160, 720, 400)]
