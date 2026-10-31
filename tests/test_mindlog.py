from iterator_pet.brain.context import Context
from iterator_pet.brain.mindlog import KEEP, MindLog
from iterator_pet.platform.projections import TPS, mind_ttl


def test_mind_ttl_scales_with_length_and_is_capped():
    assert mind_ttl("平静", "嗯") == 6 * TPS, "短的不短于原来的 6 s"
    assert mind_ttl("无聊", "字" * 60) > 6 * TPS
    assert mind_ttl("无聊", "字" * 120) == 15 * TPS, "封顶"


def test_mindlog_dedupes_persists_and_caps(tmp_path):
    path = tmp_path / "mind.json"
    log = MindLog(path)
    assert log.add(1.0, "平静", "他又在改桌宠")
    assert not log.add(2.0, "平静", "他又在改桌宠"), "同一条念头不重复记"
    assert not log.add(3.0, "平静", "  ")
    for i in range(KEEP + 5):
        log.add(10.0 + i, "无聊", f"念头{i}")
    assert len(log.items) == KEEP
    assert MindLog(path).recent(1)[0]["thought"] == f"念头{KEEP + 4}", "重启后还在，新的在前"


def test_mind_log_lives_beside_context_and_never_in_turns(tmp_path):
    ctx = Context(tmp_path / "context.json")
    ctx.mind.add(1.0, "平静", "秘密念头")
    ctx.save()
    assert (tmp_path / "mind.json").is_file()
    assert "秘密念头" not in (tmp_path / "context.json").read_text(encoding="utf-8"), "不混进上下文文件"
    ctx.clear()
    assert ctx.mind.recent() == [], "清记忆时一起清"
