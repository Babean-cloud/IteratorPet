from iterator_pet.brain import perception, window_events


def test_popup_filter_accepts_foreground_dialog_from_other_process():
    dialog = window_events.WindowMeta(2, 20, 0, "#32770", True, True)
    menu = window_events.WindowMeta(2, 10, 0, "#32768", True, True)
    assert window_events.is_popup_candidate(dialog, 2, 10, 99)
    assert not window_events.is_popup_candidate(dialog, 1, 10, 99)
    assert not window_events.is_popup_candidate(menu, 2, 10, 99)


def test_show_event_is_debounced_and_expires():
    now = [100.0]
    watcher = window_events.PopupWatcher(clock=lambda: now[0])
    watcher.record(2)
    assert watcher.ready() is None
    now[0] = 100.31
    assert watcher.ready() == 2
    watcher.record(3)
    now[0] = 106.0
    assert watcher.ready() is None


def test_popup_bypasses_settle_and_min_gap(monkeypatch):
    now = [100.0]
    eyes = perception.Perception(clock=lambda: now[0])
    eyes.own_pid = 99
    eyes.window = perception.WindowInfo(1, "App", "main", (0, 0, 800, 600), False, 50.0)
    eyes._last_ocr_at = 100.0
    eyes._ocr = {(2, "dialog"): (100.0, "old")}
    monkeypatch.setattr(perception, "foreground", lambda: (2, 10, "App", "dialog", (10, 10, 410, 310)))
    monkeypatch.setattr(window_events, "inspect_window", lambda hwnd:
                        window_events.WindowMeta(hwnd, 10, 0, "#32770" if hwnd == 2 else "App", True, True))
    eyes.popup_watcher.record(2)
    now[0] = 100.4
    eyes.poll()
    assert eyes._popup_ready
    launched = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            launched.append(args)

        def start(self):
            pass

    monkeypatch.setattr(perception.threading, "Thread", FakeThread)
    assert eyes.maybe_ocr(now[0])
    assert launched[0][-1] is True
    assert not eyes._popup_ready


def test_old_popup_does_not_force_a_different_window():
    eyes = perception.Perception(clock=lambda: 110.0)
    eyes.window = perception.WindowInfo(3, "Other", "other", (0, 0, 800, 600), False, 109.0)
    eyes._view_since = 109.0
    eyes._popup_ready = True
    eyes._popup_target = (2, 100.0)
    assert not eyes.maybe_ocr()
    assert not eyes._popup_ready


def test_foreground_event_starts_new_window_after_short_settle(monkeypatch):
    now = [100.0]
    eyes = perception.Perception(clock=lambda: now[0])
    eyes.own_pid = 99
    eyes.window = perception.WindowInfo(1, "App", "old", (0, 0, 800, 600), False, 50.0)
    eyes._last_ocr_at = 100.0
    monkeypatch.setattr(perception, "foreground", lambda: (2, 10, "Other", "new", (0, 0, 800, 600)))
    eyes.popup_watcher.record_foreground(2)
    now[0] = 100.31
    assert eyes.has_ready_window_event()
    eyes.poll()
    assert not eyes.has_ready_window_event()
    assert not eyes.window_read_due(102.9)
    assert eyes.window_read_due(103.31)

    launched = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            launched.append(args)

        def start(self):
            pass

    monkeypatch.setattr(perception.threading, "Thread", FakeThread)
    assert eyes.maybe_ocr(103.31)
    assert launched[0][-1] is True
