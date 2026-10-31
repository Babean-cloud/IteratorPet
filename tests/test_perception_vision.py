import io
import queue
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageGrab

from iterator_pet.brain import perception
from iterator_pet.brain import director as director_module
from iterator_pet.brain.director import Director
from iterator_pet.config import Config
from iterator_pet.platform import pet_window


def test_screen_vision_is_off_by_default():
    assert Config().screen_vision == "off"


def test_explicit_full_mode_captures_foreground_window_in_memory(monkeypatch):
    eyes = perception.Perception()
    eyes.own_pid = 99
    eyes.window = perception.WindowInfo(1, "editor", "code", (0, 0, 960, 640), False, 0.0)
    monkeypatch.setattr(perception, "foreground", lambda: (1, 10, "editor", "code", (0, 0, 960, 640)))
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: Image.new("RGB", (960, 640), "red"))
    payload = eyes.capture_vision("full", (1, "code"))
    assert payload is not None and payload.startswith(b"\xff\xd8")
    assert Image.open(io.BytesIO(payload)).size == (960, 640)


def test_private_window_never_captures_for_vision(monkeypatch):
    eyes = perception.Perception()
    eyes.window = perception.WindowInfo(1, "bank", "bank", (0, 0, 960, 640), True, 0.0)
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: 1 / 0)
    assert eyes.capture_vision("full", (1, "bank")) is None


def test_chat_window_in_foreground_captures_previous_external_window(monkeypatch):
    eyes = perception.Perception()
    eyes.own_pid = 99
    eyes._own_window_active = True  # 上一轮 poll 看见的是自己的输入界面
    eyes.window = perception.WindowInfo(1, "editor", "code", (0, 0, 960, 640), False, 0.0)
    monkeypatch.setattr(perception, "foreground", lambda: (2, 99, "IteratorPet", "聊天",
                                                          (200, 100, 700, 500)))
    monkeypatch.setattr(perception, "_window_details", lambda _hwnd:
                        (1, 10, "editor", "code", (0, 0, 960, 640)))
    grabs = []

    def grab(**kwargs):
        grabs.append(kwargs)
        return Image.new("RGB", (960, 640), "blue")

    monkeypatch.setattr(ImageGrab, "grab", grab)
    assert eyes.capture_vision("full", (1, "code")) is None
    payload = eyes.capture_vision("full", (1, "code"), own_ui_hidden=True)
    assert payload is not None and grabs == [{"bbox": (0, 0, 960, 640), "all_screens": True}]


def test_other_external_window_in_foreground_never_captures_stale_target(monkeypatch):
    eyes = perception.Perception()
    eyes.own_pid = 99
    eyes.window = perception.WindowInfo(1, "editor", "code", (0, 0, 960, 640), False, 0.0)
    monkeypatch.setattr(perception, "foreground", lambda: (3, 11, "browser", "news",
                                                          (0, 0, 960, 640)))
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: 1 / 0)
    assert eyes.capture_vision("full", (1, "code")) is None


def test_roi_vision_crops_changed_region(monkeypatch):
    eyes = perception.Perception()
    eyes.own_pid = 99
    eyes.window = perception.WindowInfo(1, "editor", "code", (0, 0, 960, 640), False, 0.0)
    before = Image.new("RGB", (960, 640), "black")
    after = before.copy()
    ImageDraw.Draw(after).rectangle((400, 180, 620, 420), fill="white")
    from iterator_pet.brain.attention import thumbnail

    eyes._frames[(1, "code")] = thumbnail(before)
    monkeypatch.setattr(perception, "foreground", lambda: (1, 10, "editor", "code", (0, 0, 960, 640)))
    monkeypatch.setattr(ImageGrab, "grab", lambda **_kw: after.copy())
    payload = eyes.capture_vision("roi", (1, "code"))
    assert payload is not None
    width, height = Image.open(io.BytesIO(payload)).size
    assert width < 960 and height < 640


def test_screen_capture_callable_goes_to_vision_request():
    calls = []

    class Client:
        def complete(self, messages, max_tokens, model=None):
            calls.append((messages, model))
            return '{"say":"看到了"}', {}

    director = Director.__new__(Director)
    director.client = Client()
    director.vision_model = "vision"
    director._results = queue.Queue()
    director._busy = True
    director._run("chat", [{"role": "user", "content": "请看屏幕"}], "{}", None,
                  image=lambda: b"\xff\xd8example")
    assert calls[0][1] == "vision"
    assert calls[0][0][-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "本轮附有当前窗口截图" in calls[0][0][-1]["content"][0]["text"]
    assert not director._busy


def test_precaptured_jpeg_bytes_go_to_vision_request_without_calling_them():
    calls = []

    class Client:
        def complete(self, messages, max_tokens, model=None):
            calls.append((messages, model))
            return '{"say":"我看见了"}', {}

    director = Director.__new__(Director)
    director.client = Client()
    director.vision_model = "vision"
    director._results = queue.Queue()
    director._busy = True
    director._run("chat", [{"role": "user", "content": "请看屏幕"}], "{}", None,
                  image=b"\xff\xd8jpeg payload")
    assert len(calls) == 1 and calls[0][1] == "vision"
    assert calls[0][0][-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert director._results.get_nowait()[3] is None


def test_full_capture_is_scheduled_only_for_explicit_screen_request(monkeypatch):
    calls = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            calls.append(args)

        def start(self):
            pass

    monkeypatch.setattr(director_module.threading, "Thread", FakeThread)
    eyes = SimpleNamespace(window=perception.WindowInfo(1, "editor", "code",
                                                        (0, 0, 960, 640), False, 0.0),
                           capture_vision=lambda mode, key: b"image")
    brain = Director(client=object(), perception=eyes)
    brain.vision_model = "vision"
    brain.observe = lambda *_args: {}
    sim = SimpleNamespace(appearance=SimpleNamespace(key="pebbles"))

    brain._start(sim, "chat", "请看屏幕")
    assert calls[-1][4] is None           # 默认关闭时连截图任务也不安排
    brain.screen_vision_mode = "full"
    brain._start(sim, "chat", "你好")
    assert calls[-1][4] is None
    brain._start(sim, "chat", "请看屏幕")
    assert callable(calls[-1][4])
    brain._start(sim, "chat", "不要看屏幕，只回答问题")
    assert calls[-1][4] is None
    brain._start(sim, "chat", "帮我截个屏")
    assert callable(calls[-1][4])
    brain._start(sim, "chat", "请解释截图功能怎么用")
    assert calls[-1][4] is None
    brain._start(sim, "idle", None)
    assert calls[-1][4] is None


def test_chat_ui_is_hidden_during_screen_capture_and_restored(monkeypatch):
    events = []

    class Chat:
        def isVisible(self):
            return True

        def hide(self):
            events.append("hide")

        def open(self):
            events.append("open")

    class Thread:
        def __init__(self, target, daemon, name):
            self.target = target

        def start(self):
            self.target()

    eyes = SimpleNamespace(window=perception.WindowInfo(1, "editor", "code",
                                                        (0, 0, 960, 640), False, 0.0),
                           capture_vision=lambda mode, key, own_ui_hidden: (
                               events.append((mode, key, own_ui_hidden)) or b"jpeg"))
    brain = SimpleNamespace(perception=eyes, screen_vision_mode="full",
                            say_to=lambda text, image, screen_capture_attempted:
                                events.append((text, image, screen_capture_attempted)))
    pet = SimpleNamespace(director=brain, _chat_window=Chat())
    monkeypatch.setattr(pet_window.QTimer, "singleShot", lambda _ms, callback: callback())
    monkeypatch.setattr(pet_window.threading, "Thread", Thread)
    monkeypatch.setattr(director_module, "_log", lambda _line: None)

    pet_window.PetWindow.send_screen_chat(pet, "请看屏幕")
    assert events == ["hide", ("full", (1, "code"), True), "open",
                      ("请看屏幕", b"jpeg", True)]
