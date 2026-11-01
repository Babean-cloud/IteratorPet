from PIL import Image, ImageDraw, ImageGrab
from types import SimpleNamespace

from iterator_pet.brain import perception, uia
from iterator_pet.platform.ai_panel import AIPanel


def test_own_foreground_does_not_capture_previous_window(monkeypatch):
    eyes = perception.Perception(clock=lambda: 100.0)
    eyes.window = perception.WindowInfo(10, "Notepad", "notes", (0, 0, 800, 600), False, 90.0)
    monkeypatch.setattr(perception, "foreground",
                        lambda: (20, eyes.own_pid, "IteratorPet", "AI 状态", (0, 0, 400, 300)))
    eyes.poll()
    assert eyes._own_window_active
    assert not eyes.maybe_ocr()
    monkeypatch.setattr(perception, "foreground",
                        lambda: (10, 123, "Notepad", "notes", (0, 0, 800, 600)))
    eyes.poll()
    assert not eyes._own_window_active


def test_ai_panel_rectangle_is_blank_before_ocr(monkeypatch):
    image = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(image).rectangle((100, 80, 249, 199), fill="black")
    eyes = perception.Perception(clock=lambda: 100.0)
    eyes.set_own_overlay((100, 80, 250, 200))
    monkeypatch.setattr(perception, "foreground", lambda: (10, 123, "Notepad", "notes", (0, 0, 400, 300)))
    monkeypatch.setattr(uia, "focus_snapshot", lambda *_args: None)
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: image.copy())
    seen = []

    def fake_ocr(crop):
        seen.append(crop.getpixel((150, 120)))
        return ["external document text"]

    monkeypatch.setattr(perception, "ocr_lines", fake_ocr)
    eyes._run_ocr((10, "notes"), (0, 0, 400, 300), 100.0)
    assert seen == [(255, 255, 255)]
    assert eyes._ocr[(10, "notes")][1] == "external document text"


def test_capture_is_discarded_if_panel_moves_during_grab(monkeypatch):
    eyes = perception.Perception(clock=lambda: 100.0)
    eyes.set_own_overlay((100, 80, 250, 200))
    monkeypatch.setattr(perception, "foreground", lambda: (10, 123, "Notepad", "notes", (0, 0, 400, 300)))
    monkeypatch.setattr(uia, "focus_snapshot", lambda *_args: None)

    def moving_grab(**_kw):
        eyes.set_own_overlay((110, 80, 260, 200))
        return Image.new("RGB", (400, 300), "white")

    monkeypatch.setattr(ImageGrab, "grab", moving_grab)
    eyes._run_ocr((10, "notes"), (0, 0, 400, 300), 100.0)
    assert (10, "notes") not in eyes._ocr
    assert eyes._last_ocr_at < 0


def test_moving_own_regions_are_blanked_with_padding_and_not_discarded(monkeypatch):
    image = Image.new("RGB", (400, 300), "black")
    eyes = perception.Perception(clock=lambda: 100.0)
    eyes.set_own_regions([(100, 100, 150, 130)])

    def grab(**_kw):
        eyes.set_own_regions([(300, 200, 340, 220)])          # 截图那一瞬监视者挪了位置
        return image.copy()

    monkeypatch.setattr(ImageGrab, "grab", grab)
    out = eyes._grab_masked((0, 0, 400, 300), eyes._overlay_version)
    assert out is not None                                     # 每帧都动的东西不让截图作废
    assert out.getpixel((95, 95)) == (255, 255, 255)           # 前后两处都盖白，且往外多盖一圈
    assert out.getpixel((320, 210)) == (255, 255, 255)
    assert out.getpixel((10, 10)) == (0, 0, 0)


def test_lines_matching_what_the_pet_just_said_are_dropped():
    eyes = perception.Perception(clock=lambda: 100.0)
    eyes.note_own_text("……别盯着我看。去写你的代码。")
    lines = ["别盯着我看。去写你的代", "def parse(row):", "测试 3 个通过"]
    assert eyes._drop_own(lines) == ["def parse(row):", "测试 3 个通过"]


def test_ai_panel_sends_its_geometry_to_perception():
    masks = []
    panel = SimpleNamespace(
        window=SimpleNamespace(director=SimpleNamespace(
            perception=SimpleNamespace(set_own_overlay=masks.append))),
        isVisible=lambda: True, _screen_rect=lambda: (10, 20, 110, 100))
    AIPanel._sync_screen_mask(panel)
    AIPanel._clear_screen_mask(panel)
    assert masks == [(10, 20, 110, 100), None]
