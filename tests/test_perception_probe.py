from PIL import Image, ImageDraw, ImageGrab

from iterator_pet.brain import perception


def test_stable_probe_keeps_screen_text_fresh_without_ocr(monkeypatch):
    now = [100.0]
    frame = Image.new("RGB", (960, 640), "white")
    eyes = perception.Perception(clock=lambda: now[0])
    eyes.own_pid = 99
    eyes.window = perception.WindowInfo(1, "app", "title", (0, 0, 960, 640), False, 90.0)
    key = (1, "title")
    from iterator_pet.brain.attention import thumbnail

    eyes._frames[key] = thumbnail(frame)
    eyes._ocr[key] = (90.0, "unchanged text")
    monkeypatch.setattr(perception, "foreground", lambda: (1, 10, "app", "title", (0, 0, 960, 640)))
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: frame.copy())
    eyes._run_probe(key, eyes.window.rect, eyes._overlay_version)
    assert eyes._ocr[key] == (100.0, "unchanged text")
    assert eyes.stats["ocr_n"] == 0
    assert eyes._change_ready is None


def test_changed_probe_requests_early_read(monkeypatch):
    now = [100.0]
    before = Image.new("RGB", (960, 640), "black")
    after = before.copy()
    ImageDraw.Draw(after).rectangle((400, 200, 700, 420), fill="white")
    eyes = perception.Perception(clock=lambda: now[0])
    eyes.own_pid = 99
    eyes.window = perception.WindowInfo(1, "app", "title", (0, 0, 960, 640), False, 80.0)
    eyes._view_since = 80.0
    eyes._last_ocr_at = 80.0
    key = (1, "title")
    from iterator_pet.brain.attention import thumbnail

    eyes._frames[key] = thumbnail(before)
    eyes._ocr[key] = (80.0, "old text")
    monkeypatch.setattr(perception, "foreground", lambda: (1, 10, "app", "title", (0, 0, 960, 640)))
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: after.copy())
    eyes._run_probe(key, eyes.window.rect, eyes._overlay_version)
    assert eyes.change_read_due(now[0])

    launched = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            launched.append(args)

        def start(self):
            pass

    monkeypatch.setattr(perception.threading, "Thread", FakeThread)
    assert eyes.maybe_ocr(now[0])
    assert launched[0][-1] is False  # 局部变化先走 ROI，不强制整窗
    assert eyes._change_ready is None


def test_stable_thumbnail_still_gets_periodic_full_audit(monkeypatch):
    eyes = perception.Perception(clock=lambda: 400.0)
    eyes.window = perception.WindowInfo(1, "app", "title", (0, 0, 960, 640), False, 10.0)
    eyes._view_since = 10.0
    key = (1, "title")
    eyes._ocr[key] = (399.0, "possibly stale text")
    eyes._checked_at[key] = 100.0
    eyes._full_ocr_at[key] = 100.0
    eyes._intervals[key] = 300.0
    eyes._last_ocr_at = 100.0
    launched = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            launched.append(args)

        def start(self):
            pass

    monkeypatch.setattr(perception.threading, "Thread", FakeThread)
    assert eyes.maybe_ocr(400.0)
    assert launched[0][-1] is True
