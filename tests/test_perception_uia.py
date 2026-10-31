from types import SimpleNamespace

from iterator_pet.brain import uia


def test_uia_prefers_text_pattern_over_name():
    class Pattern:
        DocumentRange = SimpleNamespace(GetText=lambda _limit: "document text")

    class Element:
        CurrentName = "button name"

        def GetCurrentPattern(self, pattern_id):
            if pattern_id == 1:
                return SimpleNamespace(QueryInterface=lambda _interface: Pattern())
            raise RuntimeError("pattern missing")

    api = SimpleNamespace(UIA_TextPatternId=1, IUIAutomationTextPattern=object,
                          UIA_ValuePatternId=2, IUIAutomationValuePattern=object)
    assert uia._pattern_text(Element(), api) == "document text"


def test_uia_without_pattern_uses_short_control_name():
    class Element:
        CurrentName = "OK"

        def GetCurrentPattern(self, _pattern_id):
            raise RuntimeError("pattern missing")

    api = SimpleNamespace(UIA_TextPatternId=1, IUIAutomationTextPattern=object,
                          UIA_ValuePatternId=2, IUIAutomationValuePattern=object)
    assert uia._pattern_text(Element(), api) == "OK"
