from PIL import Image, ImageDraw, ImageGrab

from iterator_pet.brain import perception
from iterator_pet.brain import uia


def test_short_buttons_survive_cleaning():
    assert perception.clean_ocr(["OK", "取消", "确定", "x", "1"]) == "OK / 取消 / 确定"


def test_roi_ocr_and_short_text_falls_back_to_full(monkeypatch):
    first = Image.new("RGB", (960, 640), "black")
    changed = first.copy()
    ImageDraw.Draw(changed).rectangle((480, 240, 639, 399), fill="white")
    shots = iter((first, changed))
    crops = []

    def fake_ocr(image):
        crops.append(image.size)
        return ["first full screen" if image.size == first.size else "OK"]

    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: next(shots))
    monkeypatch.setattr(perception, "ocr_lines", fake_ocr)
    monkeypatch.setattr(perception, "foreground", lambda: (1, 123, "test", "test", (0, 0, 960, 640)))
    monkeypatch.setattr(uia, "focus_snapshot", lambda *_args: None)
    eyes = perception.Perception(clock=lambda: 100.0)
    key = (1, "test")
    eyes._run_ocr(key, (0, 0, 960, 640), 100.0)
    eyes._run_ocr(key, (0, 0, 960, 640), 160.0)
    assert crops[0] == (960, 640)
    assert crops[1][0] < 960 and crops[1][1] < 640
    assert crops[2] == (960, 640)
    assert eyes.stats["ocr_n"] == 3


def test_focused_uia_text_skips_second_ocr(monkeypatch):
    image = Image.new("RGB", (960, 640), "white")
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: image)
    monkeypatch.setattr(perception, "ocr_lines", lambda _image: ["first full screen"])
    monkeypatch.setattr(perception, "foreground", lambda: (1, 123, "test", "test", (0, 0, 960, 640)))
    focus = uia.FocusInfo((100, 100, 500, 400), "Focused document has fresh and useful text")
    monkeypatch.setattr(uia, "focus_snapshot", lambda *_args: focus)
    eyes = perception.Perception(clock=lambda: 100.0)
    key = (1, "test")
    eyes._run_ocr(key, (0, 0, 960, 640), 100.0)
    eyes._run_ocr(key, (0, 0, 960, 640), 160.0)
    assert "Focused document" in eyes._ocr[key][1]
    assert eyes.stats["ocr_n"] == 1


def test_explicit_screen_request_forces_full_window(monkeypatch):
    image = Image.new("RGB", (960, 640), "white")
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: image)
    monkeypatch.setattr(perception, "ocr_lines", lambda crop: [f"size {crop.size}"])
    monkeypatch.setattr(perception, "foreground", lambda: (1, 123, "test", "test", (0, 0, 960, 640)))
    monkeypatch.setattr(uia, "focus_snapshot", lambda *_args: None)
    eyes = perception.Perception(clock=lambda: 100.0)
    eyes.window = perception.WindowInfo(1, "test", "test", (0, 0, 960, 640), False, 99.0)
    eyes._last_ocr_at = 99.0
    eyes._ocr = {(1, "test"): (99.0, "old text")}
    eyes.request_full()

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(perception.threading, "Thread", ImmediateThread)
    assert eyes.maybe_ocr()
    assert eyes._ocr[(1, "test")][1] == "size (960, 640)"
    assert not eyes._full_requested
