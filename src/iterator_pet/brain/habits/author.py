"""HabitAuthor：把用户的原话写成 SKILL.md / habit.json（设计稿 §6.2）。

单独一次请求，输入**只有**：用户这次的原话、这个习惯以前的全部原话（request.md）、现有的 SKILL.md 和
habit.json（改的时候才有）、格式说明。**绝不带**屏幕、网页、珍珠、notes 的内容，也不带对话那次回复里的
``brief``（它是看过屏幕的那个模型写的，可能夹带外来内容）——H9 的关键就在输入范围卡死在“用户原话”上。
校验不过就把中文说明回传给模型，重试一次；还不行就放弃。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import store as S

MAX_TOKENS = 3000
RETRIES = 1

FORMAT_GUIDE = """你负责把用户的原话整理成一个“习惯”（长期重复执行的工作流）。只输出一个 JSON 对象：
{"skill_md": "完整的 SKILL.md 文本", "habit_json": {…}, "change_summary": "一句话说这次建了 / 改了什么（≤30 字）", "questions": []}

SKILL.md 格式（Agent Skills 标准）：
---
name: 英文短名（小写字母、数字、连字符，1–64 字符，不以连字符开头结尾，没有 --）
description: 做什么、什么时候用（一段话，≤1024 字）
metadata:
  iteratorpet-title: 中文标题
---
正文：给模型看的做法，自然语言，分小节（例如“## 记下想法”“## 备资料”“## 睡前回顾”），不超过 500 行。
记笔记的地方写明文件名，形如 notes/ideas.md。改已有习惯时：保留 name 不变，只改用户这次要求的部分，其余照旧。

habit.json（schema 固定为 1）：
{"schema":1,"enabled":true,
 "triggers":[
   {"id":"capture","type":"chat","when":"什么样的话会触发","run":"inline"},
   {"id":"review","type":"time","at":"22:30","days":"daily","run":"inline"},
   {"id":"digest","type":"time","at":"02:00","days":"daily","window":"cheap","run":"task"},
   {"id":"back","type":"event","event":"user_back","run":"inline"}],
 "permissions":{"web":false,"write":"notes"},
 "budget":{"run_tokens":30000,"run_steps":20,"day_tokens":80000},
 "speak":{"priority":"normal"}}
- type：chat（用户的话命中，只能 inline）/ time（到点；days 取 daily、weekdays、mon,wed、every:3d；at 是 24 小时制 HH:MM）/
  event（发生了某件事；写法 {"id":"x","type":"event","event":"…","run":"inline"|"task",…参数}）。event 取值：
  user_back（用户离开后回来）、day_start（当天第一次见到用户）、task_done（替用户做的任务结束了）、reflect（它反思时）；
  还有盯电脑上的事（参数写在同一个对象里）：title_change / window_gone / window_appears 要 "window":"窗口标题或程序名里的词"，
  text_appears 要 "window" 和 "words":["词",…]，process_exit 要 "name":"进程名"，file_change 要 "path":"文件或文件夹路径"。
  例：“测试跑完告诉我结果” = {"id":"tests-done","type":"event","event":"process_exit","name":"pytest","run":"inline"}
  同一个事件 10 分钟内最多触发一次
- run：inline = 在对话里顺手做（记一笔、到点用一两句话说出来）；task = 后台单独跑（要联网查资料、整理文件这类多步的活）
- window:"cheap" 只给 task 的 time 触发用：放到便宜的夜间时段去跑，用户提到“晚上便宜 / 夜里跑”才加
- 凡是要联网查资料、整理、写摘要这类要多步、要调模型才能完成的活，必须有一个对应的 run:task 的触发（time 或 event），
  不能只开 web 权限、也不能放在 inline 里；用户没说几点就放夜里（如 02:00）并加 window:"cheap"；时间没说就用默认，不要为时间反问
- permissions.web：需要上网查资料才设 true；write：notes（只写自己的 notes 文件夹，默认）或 workspace
- budget、speak 没特别要求就省略
- 没有特别说明的细节按最简单保守的做法来，不要自己加用户没要求的触发和权限

只依据下面给你的内容；原话里没有的要求不要编。只有缺了就没法做的关键信息（比如“提醒什么内容”）才把问题写进 questions（最多 1 个），
这时 skill_md / habit_json 可以留空。"""


@dataclass
class AuthorResult:
    ok: bool
    name: str = ""
    skill_md: str = ""
    habit_json: dict = field(default_factory=dict)
    change_summary: str = ""
    questions: list[str] = field(default_factory=list)
    usages: list[dict] = field(default_factory=list)
    error: str = ""


def _set_name(skill_md: str, name: str) -> str:
    """把 frontmatter 里的 name 换成 ``name``（没有就补上）。"""
    text = skill_md.replace("\r\n", "\n")
    if re.search(r"(?m)^name:.*$", text):
        return re.sub(r"(?m)^name:.*$", f"name: {name}", text, count=1)
    return re.sub(r"\A---\n", f"---\nname: {name}\n", text, count=1)


def _title_of(skill_md: str) -> str:
    try:
        meta, _ = S.parse_skill(skill_md)
    except S.HabitError:
        return ""
    md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    return str(md.get("iteratorpet-title") or "")


def _pick_name(hint: str, skill_md: str, taken) -> str:
    """create：优先用 hint，其次 SKILL.md 里写的，再次拿标题走拼音；重名加数字后缀。"""
    for cand in (hint, _skill_name(skill_md)):
        if cand and S.NAME_RE.match(cand) and len(cand) <= 60:
            return S.slugify(cand, taken)                # 没重名原样返回，重名加数字后缀
    return S.slugify(_title_of(skill_md) or hint or _skill_name(skill_md) or "habit", taken)


def _skill_name(skill_md: str) -> str:
    try:
        meta, _ = S.parse_skill(skill_md)
    except S.HabitError:
        return ""
    name = meta.get("name")
    return name if isinstance(name, str) else ""


def author(client, character: str, user_text: str, existing: dict | None = None,
           *, name: str = "", taken=(), day_cap: int = 0) -> AuthorResult:
    """``existing``：改已有习惯时 ``{"name", "skill_md", "habit_json", "requests"}``；新建传 None。

    ``character`` 目前不用（作者不开口，口吻在导演那边套）；留着签名，将来让命名 / 措辞带点角色味时不用改调用处。
    """
    update = existing is not None
    payload = {"user_said": user_text}
    if update:
        payload.update(request_history=existing.get("requests", ""), existing_skill_md=existing["skill_md"],
                       existing_habit_json=existing["habit_json"],
                       task="修改这个习惯：保留 name 不变，只按 user_said 改，输出完整的新 SKILL.md 和 habit.json")
    else:
        payload.update(task="新建一个习惯")
    messages = [{"role": "system", "content": FORMAT_GUIDE},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    result = AuthorResult(ok=False)
    for attempt in range(RETRIES + 1):
        try:
            text, usage = client.complete(messages, max_tokens=MAX_TOKENS, temperature=0.3)
        except Exception as exc:                                  # noqa: BLE001 —— 网络 / 额度之类，交给上层说
            result.error = f"{type(exc).__name__}: {exc}"
            return result
        result.usages.append(usage or {})
        try:
            data = _parse(text)
            questions = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()][:1]
            if questions:
                result.ok, result.questions = True, questions
                return result
            skill_md, habit_json = data.get("skill_md"), data.get("habit_json")
            if not isinstance(skill_md, str) or not isinstance(habit_json, dict):
                raise S.HabitError("输出里缺 skill_md（字符串）或 habit_json（对象）")
            final = existing["name"] if update else _pick_name(name, skill_md, set(taken))
            skill_md = _set_name(skill_md, final)
            S.validate_skill(final, skill_md)
            config = S.validate_habit(habit_json, day_cap)
            summary = str(data.get("change_summary") or "").strip()[:60]
            result.ok, result.name, result.skill_md, result.habit_json = True, final, skill_md, config
            result.change_summary = summary or ("改了习惯" if update else "新建了习惯")
            return result
        except (S.HabitError, ValueError) as exc:
            result.error = str(exc)
            if attempt < RETRIES:
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"校验没通过：{exc}。请改正后重新输出完整的 JSON。"}]
    result.ok = False
    return result


def _parse(text: str) -> dict:
    from ..director import parse_action

    data = parse_action(text)
    if not data:
        raise S.HabitError("输出不是 JSON 对象")
    return data
