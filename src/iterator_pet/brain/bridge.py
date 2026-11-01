"""本机接口：让 Claude Code / Codex / DeepSeek Harness 之类的编程 agent 直接告诉它"跑完了""在等你批准"。

比截屏认字去猜可靠得多。三种接法，都走同一个本机 HTTP 服务（只听 127.0.0.1，要口令）：

* **钩子**：Claude Code 的 Stop / Notification 钩子、Codex 的 ``notify``，调用桌宠写好的
  ``bridge\\notify.ps1``（系统自带的 PowerShell，不用装别的），把 agent 给的 JSON 原样转过来；
* **MCP**：``/mcp`` 是一个最小的 MCP（Streamable HTTP）服务，只有一个工具 ``tell_iterator``，
  支持 MCP 的 agent 想说什么就说什么（``claude mcp add --transport http …``）；
* **通用**：``POST /event`` ``{"source", "kind": done|attention|error|info, "title", "message", "project"}``。

口令和端口在 ``%LOCALAPPDATA%\\IteratorPet\\bridge\\bridge.json``（第一次启动随机生成）。
发进来的内容一律当数据：只用来让它说一句话，进不了记忆 / 提醒 / 日程（见 ``director.agent_event``）。
浏览器里的网页带不上 Authorization 头（跨域预检我们不答），所以网页打不进来。
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 47613
MAX_BODY = 64 * 1024
DEDUPE_SECONDS = 8.0
MIN_TURN_SECONDS = 60.0     # Claude Code 一轮跑了这么久以上才算"长活跑完了"
KINDS = ("done", "attention", "error", "info")
MCP_VERSION = "2025-06-18"

NOTIFY_PS1 = r"""# 桌宠自动生成：把编程 agent 的钩子通知转给迭代器桌宠。改了也会被重写。
# 用法：notify.ps1 <来源>            （JSON 从标准输入读，Claude Code 钩子）
#       notify.ps1 <来源> '<JSON>'    （JSON 在参数里，Codex 的 notify）
param([string]$Source = "agent", [string]$Payload = "")
$ErrorActionPreference = "SilentlyContinue"
[Console]::InputEncoding = [Text.Encoding]::UTF8
$conf = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot "bridge.json") | ConvertFrom-Json
if (-not $Payload) { $Payload = [Console]::In.ReadToEnd() }
if (-not $Payload) { $Payload = "{}" }
$body = [Text.Encoding]::UTF8.GetBytes($Payload)
Invoke-RestMethod -Method Post -Uri ("http://127.0.0.1:{0}/hook/{1}" -f $conf.port, $Source) `
  -Headers @{ Authorization = "Bearer " + $conf.token } -ContentType "application/json; charset=utf-8" `
  -Body $body -TimeoutSec 3 | Out-Null
exit 0
"""


def bridge_dir() -> Path:
    from ..config import config_dir

    return config_dir() / "bridge"


def load_settings(folder: Path | None = None) -> dict:
    """端口 + 口令；没有就生成。顺手把 notify.ps1 写好（UTF-8 BOM，PowerShell 5.1 才认中文）。"""
    folder = Path(folder) if folder is not None else bridge_dir()
    path = folder / "bridge.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings.get("port"), int) or not settings.get("token"):
        settings = {"port": int(settings.get("port") or DEFAULT_PORT), "token": secrets.token_urlsafe(24)}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=1), encoding="utf-8")
        (folder / "notify.ps1").write_text(NOTIFY_PS1, encoding="utf-8-sig")
    except OSError:
        pass
    return settings


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _project(cwd) -> str:
    return Path(str(cwd)).name if cwd else ""


def normalize(source: str, payload: dict) -> dict | None:
    """各家钩子的 JSON → ``{source, kind, title, message, project}``；不认识 / 不值得说的返回 None。"""
    if not isinstance(payload, dict):
        return None
    source = (source or "agent").strip().lower()[:30]
    if source == "claude-code":
        name = payload.get("hook_event_name") or ""
        project = _project(payload.get("cwd"))
        if name == "Stop":
            return {"source": "Claude Code", "kind": "done", "title": "这一轮做完了", "message": "", "project": project}
        if name == "Notification":
            message = payload.get("message") or ""
            kind = "attention" if any(w in message.lower() for w in ("permission", "approve", "waiting", "批准", "确认")) \
                else "info"
            return {"source": "Claude Code", "kind": kind, "title": "在等你" if kind == "attention" else "通知",
                    "message": _clip(message, 200), "project": project}
        return None                                     # SubagentStop、PreToolUse 之类：太碎，不说
    if source == "codex":
        if payload.get("type") != "agent-turn-complete":
            return None
        return {"source": "Codex", "kind": "done", "title": "这一轮做完了",
                "message": _clip(payload.get("last-assistant-message"), 200), "project": _project(payload.get("cwd"))}
    kind = payload.get("kind") if payload.get("kind") in KINDS else "info"
    title, message = _clip(payload.get("title"), 60), _clip(payload.get("message"), 300)
    if not (title or message):
        return None
    return {"source": _clip(payload.get("source") or source, 30), "kind": kind, "title": title,
            "message": message, "project": _clip(payload.get("project"), 40)}


class Bridge:
    """后台线程跑 HTTP；主线程每秒 ``poll`` 拿事件。"""

    def __init__(self, settings: dict | None = None, clock=time.monotonic):
        self.settings = settings if settings is not None else load_settings()
        self.clock = clock
        self.events: queue.Queue = queue.Queue()
        self._recent: dict = {}
        self._turns: dict[str, float] = {}       # Claude Code 会话 → 这一轮开始的时间
        self.server: ThreadingHTTPServer | None = None
        self.error: str | None = None

    @property
    def port(self) -> int:
        return self.server.server_address[1] if self.server is not None else int(self.settings["port"])

    def start(self) -> bool:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code: int, body=None, headers=None):
                data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if body is not None:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                token = header[7:] if header.startswith("Bearer ") else ""
                return hmac.compare_digest(token.encode(), str(bridge.settings["token"]).encode())

            def do_GET(self):  # noqa: N802
                if not self._authorized():
                    return self._reply(401, {"error": "unauthorized"})
                if self.path == "/health":
                    return self._reply(200, {"ok": True, "name": "iterator-pet"})
                return self._reply(405 if self.path == "/mcp" else 404, {"error": "not found"})

            def _reject(self, code: int, error: str, length: int):
                # 读完请求体再拒：Windows 上关掉还有没读数据的连接会发 RST，客户端偶尔先收到
                # "连接被重置"而不是我们的回复（全量并行测试时偶发）
                if 0 < length <= 16 * MAX_BODY:
                    self.rfile.read(length)
                self.close_connection = True
                return self._reply(code, {"error": error})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if not self._authorized():
                    return self._reject(401, "unauthorized", length)
                if length > MAX_BODY:
                    return self._reject(413, "too large", length)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8-sig") or "{}")
                except (UnicodeDecodeError, ValueError):
                    return self._reply(400, {"error": "bad json"})
                if self.path == "/mcp":
                    reply = bridge.mcp(payload)
                    return self._reply(202) if reply is None else self._reply(200, reply)
                if self.path == "/event":
                    event = normalize("agent", payload)
                elif self.path == "/hook/claude-code":
                    event = bridge.claude_hook(payload)
                elif self.path.startswith("/hook/"):
                    event = normalize(self.path[6:], payload)
                else:
                    return self._reply(404, {"error": "not found"})
                accepted = bridge.push(event)
                return self._reply(200, {"ok": True, "accepted": accepted})

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", int(self.settings["port"])), Handler)
        except OSError as exc:
            self.error = f"端口 {self.settings['port']} 用不了：{exc}"
            return False
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return True

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def claude_hook(self, payload) -> dict | None:
        """Claude Code 的 Stop 每答完一句都会触发：配了 UserPromptSubmit 的话，按会话记下"这一轮几点开始"，
        只有跑了 ``MIN_TURN_SECONDS`` 以上的那一轮才说（"跑完了"），一问一答的闲聊不打扰。没配就照旧每次都说。"""
        if not isinstance(payload, dict):
            return None
        session = str(payload.get("session_id") or "")
        name = payload.get("hook_event_name")
        if name == "UserPromptSubmit":
            if session:
                self._turns[session] = self.clock()
                if len(self._turns) > 200:
                    self._turns.pop(next(iter(self._turns)))
            return None
        if name == "Stop" and session in self._turns:
            started = self._turns.pop(session)
            if self.clock() - started < MIN_TURN_SECONDS:
                return None
            event = normalize("claude-code", payload)
            if event is not None:
                event["title"] = f"这一轮做完了（跑了 {int((self.clock() - started) / 60) or 1} 分钟）"
            return event
        return normalize("claude-code", payload)

    def push(self, event: dict | None) -> bool:
        """去重：同一来源同一项目同一种事，几秒内只算一次（Claude Code 有时连发）。"""
        if not event:
            return False
        key = (event["source"], event["kind"], event["project"])
        now = self.clock()
        if now - self._recent.get(key, -1e9) < DEDUPE_SECONDS:
            return False
        self._recent[key] = now
        self.events.put(event)
        return True

    def poll(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    # -- MCP（Streamable HTTP，只回 JSON，不开 SSE） ---------------------------------
    TOOL = {
        "name": "tell_iterator",
        "description": "Tell the user's desktop pet (a Rain World iterator) something, e.g. that a long task "
                       "finished or needs their approval. It relays it to the user in one short in-character "
                       "line. Use sparingly: only for things the user would want to know right now.",
        "inputSchema": {"type": "object", "properties": {
            "message": {"type": "string", "description": "What happened, one or two sentences."},
            "kind": {"type": "string", "enum": list(KINDS), "description": "done / attention / error / info"},
            "project": {"type": "string", "description": "Project or folder name (optional)."}},
            "required": ["message"]},
    }

    def mcp(self, request) -> dict | None:
        if isinstance(request, list):                 # 批量：逐个答
            replies = [r for r in (self.mcp(item) for item in request) if r is not None]
            return replies or None
        if not isinstance(request, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        method, rid = request.get("method"), request.get("id")
        if rid is None:
            return None                               # 通知（notifications/initialized 之类）
        if method == "initialize":
            result = {"protocolVersion": (request.get("params") or {}).get("protocolVersion") or MCP_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "iterator-pet", "version": "0.4.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [self.TOOL]}
        elif method == "tools/call":
            params = request.get("params") or {}
            if params.get("name") != "tell_iterator":
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown tool"}}
            args = params.get("arguments") or {}
            event = normalize("mcp", {"source": "MCP", "kind": args.get("kind"), "title": "",
                                      "message": args.get("message"), "project": args.get("project")})
            accepted = self.push(event)
            text = "Relayed to the user." if accepted else "Not relayed (empty, or a duplicate within a few seconds)."
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}


def setup_text(settings: dict, folder: Path | None = None) -> str:
    """「接入编程 agent…」对话框里给用户复制的配置。"""
    folder = Path(folder) if folder is not None else bridge_dir()
    script = str(folder / "notify.ps1")
    port, token = settings["port"], settings["token"]

    def hook(source):
        return {"type": "command",
                "command": f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}" {source}'}

    claude = {"hooks": {"UserPromptSubmit": [{"hooks": [hook("claude-code")]}],
                        "Stop": [{"hooks": [hook("claude-code")]}],
                        "Notification": [{"hooks": [hook("claude-code")]}]}}
    codex_cmd = json.dumps(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "codex"],
                           ensure_ascii=False)
    return "\n".join([
        "① Claude Code：把下面的 hooks 合并进 ~/.claude/settings.json（用户级：所有项目、所有会话都生效，"
        "新开的会话自动带上；放项目的 .claude/settings.json 就只管那个项目）。",
        "   跑了 1 分钟以上的那一轮结束、或者它在等你批准时，桌宠会说一句；一问一答的闲聊不打扰。",
        json.dumps(claude, ensure_ascii=False, indent=2),
        "",
        "② Codex：在 ~/.codex/config.toml 里加一行",
        f"notify = {codex_cmd}",
        "",
        "③ 支持 MCP 的 agent（Claude Code、DeepSeek Harness 等）：加一个 HTTP MCP 服务",
        f'claude mcp add --transport http iterator-pet http://127.0.0.1:{port}/mcp '
        f'--header "Authorization: Bearer {token}"',
        "   其他 agent 填：地址 http://127.0.0.1:%d/mcp，请求头 Authorization: Bearer <口令>" % port,
        "",
        "④ 自己的脚本：POST http://127.0.0.1:%d/event（同样带这个请求头）" % port,
        '   {"source": "构建", "kind": "done", "title": "打包完了", "project": "IteratorPet"}',
        "",
        f"口令存在 {folder / 'bridge.json'}，只在本机用；觉得泄露了就删掉这个文件再重启桌宠，会换一个新的。",
    ])
