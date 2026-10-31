"""办心事的那一小段 agent 循环：看 → 想 → 决定（接着等 / 告诉对方 / 放弃）。

工作线程里跑（只做 HTTP 和只读的本地感知），结果交回主线程落地。每次醒来最多 ``MAX_STEPS`` 轮工具调用，
最后必须调 ``wait`` / ``done`` / ``give_up`` 之一。人设还是那个角色（system prompt 前缀不变，吃得到缓存）。
"""

from __future__ import annotations

import json
from datetime import datetime

from .errands import MAX_DEADLINE_MIN, TRIGGER_TYPES
from .prompt import PERSONAS

MAX_STEPS = 6

RULES = """
现在不是闲聊：这个人托付了你一件事（心事），要等某件事发生了再告诉对方。你可以调用工具看这台电脑上的情况，
最后必须调用 wait / done / give_up 之一结束这一次。

- 先弄清楚要等的东西在哪：list_windows 看有哪些窗口（包括后台的）；read_window 读某个窗口里的字
  （后台的也行，最小化的不行）；process_status / file_status 看进程和文件。
- 还没发生 → wait：
  * notes 写你的打算，给下次醒来的你看（要等什么、在哪个窗口、什么迹象算完成、上次看到的进度）；
  * triggers 挑最能说明"那件事发生了"的本地信号，程序会替你盯着，触发了才叫醒你：
    命令 / 程序跑完常常是窗口标题变了、或者窗口里出现"完成 / passed / error / 退出码"之类的字；
    下载完成是文件出现或不再变大；程序退出是进程没了；新弹窗是出现了某个窗口。拿不准就设两三个；
  * check_in_min：就算没触发也回来看一眼的间隔。快的事 2~5，慢的事 10~30；
  * deadline_min：最多等多久（从答应时算，默认 180，最多 720）。
- 已经发生了 → done，say 用你的口吻告诉对方结果（≤30 字，可以带一点具体信息，比如"179 个全过了"）。
- 根本找不到要等的东西、做不到，或者 final 为 true 了还没等到 → give_up，say 说明一句。
- screen_unchanged_min 是你读的那些窗口内容已经多少分钟没变过了。要等的事情如果正在进行，屏幕通常会变；
  超过 15 分钟一点没变、也看不到"正在运行 / 进度"的迹象，多半是已经停了、卡住了或者没额度了：
  用 done 告诉对方现在的样子（比如"那边半天没动静了，像是停了，你去看看"），别一直干等到时限。
- woken_by 是"刚答应"时，是第一次规划：wait 的 say 可以简短说一句你打算怎么盯（≤30 字），也可以留空。
  之后醒来如果还在等，say 一般留空，别打扰对方。
- 只说中文，不带角色名、不加引号、不用表情符号；不要复述隐私内容。
"""


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


def tool_specs() -> list[dict]:
    say = {"type": "string", "description": "对这个人说的一句话（≤30 字），不说就填空字符串"}
    trigger = {"type": "object", "properties": {
        "type": {"type": "string", "enum": list(TRIGGER_TYPES),
                 "description": "；".join(f"{k}：{v}" for k, v in TRIGGER_TYPES.items())},
        "window": {"type": "string", "description": "窗口标题或程序名里的一段字（title_change / window_gone / window_appears / text_appears）"},
        "words": {"type": "array", "items": {"type": "string"}, "description": "text_appears：要等的字，中英文都写"},
        "name": {"type": "string", "description": "process_exit：进程名，比如 python.exe"},
        "path": {"type": "string", "description": "file_change：文件或文件夹的完整路径"}},
        "required": ["type"]}
    return [
        _fn("list_windows", "列出现在打开着的窗口（前台的在最前）", {}),
        _fn("read_window", "截某个窗口认字（本地 OCR，可能有错字）",
            {"window": {"type": "string", "description": "窗口标题或程序名里的一段字"}}, ["window"]),
        _fn("process_status", "某个进程在不在、有几个", {"name": {"type": "string"}}, ["name"]),
        _fn("file_status", "某个文件 / 文件夹在不在、多大、多久前改过（不读内容）",
            {"path": {"type": "string"}}, ["path"]),
        _fn("wait", "还没发生：写下打算，设置触发器，接着等",
            {"notes": {"type": "string"}, "triggers": {"type": "array", "items": trigger},
             "check_in_min": {"type": "number"}, "deadline_min": {"type": "number"}, "say": say},
            ["notes", "check_in_min"]),
        _fn("done", "已经发生了：告诉对方", {"say": {"type": "string"}}, ["say"]),
        _fn("give_up", "等不到或做不到：说明一句", {"say": {"type": "string"}}, ["say"]),
    ]


def system_prompt(character: str) -> str:
    return PERSONAS.get(character, PERSONAS["pebbles"]) + "\n" + RULES.strip()


def brief(errand: dict, woken_by: str, now: float, foreground: dict | None, final: bool) -> dict:
    return {
        "goal": errand["goal"],
        "your_notes": errand.get("notes") or None,
        "history": errand.get("history") or [],
        "woken_by": woken_by,
        "now": datetime.fromtimestamp(now).strftime("%H:%M"),
        "waited_min": int((now - errand["created"]) / 60),
        "deadline_in_min": max(0, int((errand["deadline"] - now) / 60)),
        "max_deadline_min": MAX_DEADLINE_MIN,
        "foreground": foreground,
        "final": final,
        "screen_unchanged_min": (int((now - errand["unchanged_since"]) / 60)
                                 if errand.get("unchanged_since") else None),
    }


def _run_tool(name: str, args: dict, sensors) -> str:
    if name == "list_windows":
        windows = sensors.list_windows()[:30]
        return json.dumps([w.label() + ("（最小化）" if w.minimized else "") for w in windows], ensure_ascii=False)
    if name == "read_window":
        win, text = sensors.read_window(str(args.get("window") or ""))
        if win is None:
            return "没找到这个窗口（隐私窗口也找不到）。"
        if win.minimized:
            return f"{win.label()}：最小化了，读不到。"
        return f"{win.label()}\n{text or '（没认出字，可能截不到这个窗口）'}"
    if name == "process_status":
        count = sensors.process_count(str(args.get("name") or ""))
        return f"在运行，{count} 个" if count else "没在运行"
    if name == "file_status":
        return json.dumps(sensors.file_status(str(args.get("path") or "")), ensure_ascii=False)
    return f"没有 {name} 这个工具"


def run(client, character: str, info: dict, sensors=None, max_steps: int = MAX_STEPS) -> tuple[dict, list, list]:
    """跑一次。返回 (决定, usage 列表, 看过的东西)。决定：{"action": wait|done|give_up, ...参数}；
    读过窗口的话，决定里多一个 ``_snapshot``（读到的字的指纹），导演拿它算"多久没变了"。"""
    decision, usages, seen = _run(client, character, info, sensors, max_steps)
    reads = [s for s in seen if s.startswith("read_window(")]
    if reads:
        import hashlib

        decision["_snapshot"] = hashlib.sha1("\n".join(reads).encode("utf-8")).hexdigest()[:16]
    return decision, usages, seen


def _run(client, character: str, info: dict, sensors, max_steps: int) -> tuple[dict, list, list]:
    if sensors is None:
        from . import sensors as sensors_module

        sensors = sensors_module
    messages = [{"role": "system", "content": system_prompt(character)},
                {"role": "user", "content": json.dumps(info, ensure_ascii=False)}]
    usages, seen = [], []
    tools = tool_specs()
    for _ in range(max_steps):
        message, usage = client.complete_tools(messages, tools, max_tokens=800, temperature=0.4)
        usages.append(usage)
        calls = message.get("tool_calls") or []
        messages.append({"role": "assistant", "content": message.get("content") or "",
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            text = (message.get("content") or "").strip()
            return {"action": "wait", "say": text[:60] or None}, usages, seen
        for call in calls:
            name = (call.get("function") or {}).get("name", "")
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            args = args if isinstance(args, dict) else {}
            if name in ("wait", "done", "give_up"):
                return dict(args, action=name), usages, seen
            try:
                result = _run_tool(name, args, sensors)
            except Exception as exc:                       # noqa: BLE001
                result = f"出错了：{type(exc).__name__}"
            entry = f"{name}({json.dumps(args, ensure_ascii=False)[:60]}) → {result[:80]}"
            if name == "read_window":                  # 指纹按读到的全文算（前 80 字常常是不变的标题）
                import hashlib

                entry += f" #{hashlib.sha1(result.encode('utf-8')).hexdigest()[:10]}"
            seen.append(entry)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result[:3000]})
    messages.append({"role": "user", "content": "看够了，现在调用 wait / done / give_up 之一。"})
    message, usage = client.complete_tools(messages, tools, max_tokens=500, temperature=0.4)
    usages.append(usage)
    for call in message.get("tool_calls") or []:
        name = (call.get("function") or {}).get("name", "")
        if name in ("wait", "done", "give_up"):
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            return dict(args if isinstance(args, dict) else {}, action=name), usages, seen
    return {"action": "wait", "notes": "看了一圈还没想好", "check_in_min": 5}, usages, seen
