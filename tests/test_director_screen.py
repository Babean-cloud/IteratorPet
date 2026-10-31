from datetime import datetime
from types import SimpleNamespace

from iterator_pet.brain.director import Director
from iterator_pet.simulation.state import SimulationState


def test_screen_question_requests_fresh_full_capture():
    requested = []
    director = Director.__new__(Director)
    director.perception = SimpleNamespace(request_full=lambda: requested.append(True))
    director.clock = lambda: 100.0
    director.wall = datetime.now
    director._pending_chat = []
    director.log = lambda _text: None
    director.remarks = SimpleNamespace(on_user_chat=lambda *_args: None)
    director.relation = SimpleNamespace(on_talk=lambda: None)
    director.chatlog = None

    director.say_to("请复述弹窗里的诊断地址")
    assert requested == [True]
    assert director._screen_chat_at == 100.0
    director.say_to("你好")
    assert requested == [True]


def test_prepared_screen_image_reaches_chat_request_without_second_capture():
    director = Director(client=object(), clock=lambda: 100.0, idle_fn=lambda: 0.0)
    launched = []
    director._start = lambda sim, reason, text, **kwargs: launched.append((reason, text, kwargs))
    director.say_to("请看屏幕", image=b"jpeg", screen_capture_attempted=True)
    sim = SimulationState((0.0, 0.0, 1138.0, 608.0))
    director.tick(sim)
    assert launched[0] == ("chat", "请看屏幕",
                           {"image": b"jpeg", "screen_capture_attempted": True})
