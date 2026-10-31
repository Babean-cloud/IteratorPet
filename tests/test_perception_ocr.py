import asyncio

from PIL import Image

from iterator_pet.brain import perception


def test_winrt_engine_is_initialized_once(monkeypatch):
    image = Image.new("RGB", (100, 40), "white")
    engine = object()
    calls = []

    def make_engine():
        calls.append("init")
        return engine

    async def recognize(_image, actual_engine):
        await asyncio.sleep(0)
        assert actual_engine is engine
        return ["hello"]

    monkeypatch.setattr(perception, "_ocr_engine", None)
    monkeypatch.setattr(perception, "_make_ocr_engine", make_engine)
    monkeypatch.setattr(perception, "_recognize_lines", recognize)
    assert perception.ocr_lines(image) == ["hello"]
    assert perception.ocr_lines(image) == ["hello"]
    assert calls == ["init"]
