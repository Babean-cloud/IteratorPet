"""本机接口：钩子、MCP、通用事件；只认口令。"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from iterator_pet.brain.bridge import Bridge, load_settings, normalize, setup_text


@pytest.fixture()
def bridge(tmp_path):
    settings = load_settings(tmp_path)
    settings["port"] = 0                                 # 随机端口，不和正在跑的桌宠抢
    server = Bridge(settings)
    assert server.start()
    yield server
    server.stop()


def _post(bridge, path, payload, token=None, raw=None):
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{bridge.port}{path}", data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {token or bridge.settings['token']}"})
    try:
        with urllib.request.urlopen(request, timeout=3) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_settings_persist_and_write_script(tmp_path):
    first = load_settings(tmp_path)
    assert len(first["token"]) >= 24 and first["port"] == 47613
    assert load_settings(tmp_path)["token"] == first["token"]
    assert (tmp_path / "notify.ps1").read_bytes().startswith(b"\xef\xbb\xbf"), "UTF-8 BOM：PowerShell 5.1 才认"
    text = setup_text(first, tmp_path)
    assert '"Stop"' in text and "notify = [" in text and "--transport http" in text


def test_normalize_hooks():
    assert normalize("claude-code", {"hook_event_name": "Stop", "cwd": "E:/code/IteratorPet"}) == {
        "source": "Claude Code", "kind": "done", "title": "这一轮做完了", "message": "", "project": "IteratorPet"}
    ask = normalize("claude-code", {"hook_event_name": "Notification", "message": "Claude needs your permission to use Bash"})
    assert ask["kind"] == "attention"
    assert normalize("claude-code", {"hook_event_name": "PreToolUse"}) is None
    codex = normalize("codex", {"type": "agent-turn-complete", "last-assistant-message": "Done. " * 100})
    assert codex["kind"] == "done" and len(codex["message"]) <= 201
    assert normalize("agent", {"kind": "weird", "title": "打包完了"})["kind"] == "info"
    assert normalize("agent", {}) is None


def test_http_requires_token_and_dedupes(bridge):
    assert _post(bridge, "/event", {"title": "x"}, token="wrong")[0] == 401
    assert _post(bridge, "/event", None, raw=b"not json")[0] == 400
    status, body = _post(bridge, "/hook/claude-code", {"hook_event_name": "Stop", "cwd": "D:/proj"})
    assert status == 200 and body["accepted"]
    assert not _post(bridge, "/hook/claude-code", {"hook_event_name": "Stop", "cwd": "D:/proj"})[1]["accepted"], \
        "几秒内连发只算一次"
    assert _post(bridge, "/event", {"title": "大" * 100000})[0] == 413
    events = bridge.poll()
    assert [e["project"] for e in events] == ["proj"]


def test_short_claude_turns_are_quiet(tmp_path):
    now = {"t": 0.0}
    server = Bridge(load_settings(tmp_path), clock=lambda: now["t"])
    hook = {"session_id": "s1", "cwd": "D:/proj"}
    assert server.claude_hook(dict(hook, hook_event_name="UserPromptSubmit")) is None
    now["t"] = 20.0
    assert server.claude_hook(dict(hook, hook_event_name="Stop")) is None, "一问一答的闲聊不打扰"
    server.claude_hook(dict(hook, hook_event_name="UserPromptSubmit"))
    now["t"] = 20.0 + 300
    event = server.claude_hook(dict(hook, hook_event_name="Stop"))
    assert event["kind"] == "done" and "5 分钟" in event["title"]
    assert server.claude_hook(dict(hook, session_id="old", hook_event_name="Stop"))["kind"] == "done", \
        "没配 UserPromptSubmit 的老配置照旧每次都说"


def test_mcp_roundtrip(bridge):
    status, init = _post(bridge, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                          "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    assert status == 200 and init["result"]["serverInfo"]["name"] == "iterator-pet"
    assert _post(bridge, "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    tools = _post(bridge, "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]
    assert tools["result"]["tools"][0]["name"] == "tell_iterator"
    call = _post(bridge, "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "tell_iterator", "arguments": {"message": "构建完成", "kind": "done", "project": "pet"}}})[1]
    assert call["result"]["content"][0]["text"] == "Relayed to the user."
    assert bridge.poll()[0] == {"source": "MCP", "kind": "done", "title": "", "message": "构建完成", "project": "pet"}
    assert _post(bridge, "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "nope"})[1]["error"]["code"] == -32601


@pytest.mark.skipif(shutil.which("powershell") is None, reason="要 Windows PowerShell")
def test_notify_script_end_to_end(tmp_path):
    settings = load_settings(tmp_path)
    settings["port"] = 0
    server = Bridge(settings)
    assert server.start()
    try:
        settings["port"] = server.port
        (tmp_path / "bridge.json").write_text(json.dumps(settings), encoding="utf-8")
        payload = json.dumps({"hook_event_name": "Stop", "cwd": "C:/代码/迭代器"}, ensure_ascii=False)
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(tmp_path / "notify.ps1"), "claude-code"], input=payload.encode("utf-8"),
                       timeout=30, check=True)
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(tmp_path / "notify.ps1"), "codex",
                        json.dumps({"type": "agent-turn-complete", "last-assistant-message": "全部改完了"},
                                   ensure_ascii=False)], timeout=30, check=True)
        deadline = time.time() + 3
        events = []
        while len(events) < 2 and time.time() < deadline:
            events += server.poll()
            time.sleep(0.05)
        assert [e["source"] for e in events] == ["Claude Code", "Codex"]
        assert events[0]["project"] == "迭代器", "中文路径经 stdin 也不乱码"
        assert events[1]["message"] == "全部改完了"
    finally:
        server.stop()
