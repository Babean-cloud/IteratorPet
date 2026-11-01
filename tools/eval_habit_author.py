"""HabitAuthor 离线评测：用真模型把“想法回顾”的三次演变（v1 创建 → v2 联网备资料 → v3 挪到夜里）走一遍。

只用合成的用户原话（设计稿 §10），在临时文件夹里建习惯，不碰真实工作区。API Key 在进程内经 ``config.llm_api_key`` 读取，
不打印。用法（项目根）::

    $env:PYTHONPATH='src'
    .\\.venv\\Scripts\\python.exe tools/eval_habit_author.py [-n 2]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from iterator_pet.brain.habits import author
from iterator_pet.brain.habits.store import HabitError, HabitStore

STEPS = (
    ("create", "我是不是会蹦几句自己感兴趣的话题和想法，你帮我记下来，每天给我回顾一下"),
    ("update", "上次说的提几句想法那件事，收到关键词后你要自己上网准备一下资料供我回顾，每天晚上睡前提醒我看"),
    ("update", "查资料跑任务的时间定在晚上，因为晚上 token 便宜"),
)


def real_client():
    from iterator_pet.brain.client import ChatClient
    from iterator_pet.config import llm_api_key, load

    cfg = load()
    key = llm_api_key(cfg)
    if not key:
        sys.exit("没有配置 API Key")
    return ChatClient(key, cfg.llm_base_url, cfg.llm_model, timeout=90.0)


def brief(habit) -> str:
    rows = []
    for t in habit.config["triggers"]:
        extra = {k: v for k, v in t.items() if k not in ("id", "type", "run")}
        rows.append(f"    {t['id']}: {t['type']}/{t['run']} {json.dumps(extra, ensure_ascii=False)}")
    perms = habit.config["permissions"]
    return "\n".join(rows) + f"\n    permissions={perms}  budget={habit.config['budget']}"


def run_chain(client, tmp: Path) -> None:
    store = HabitStore(tmp / "ws", git=lambda: None)
    name = ""
    for i, (op, text) in enumerate(STEPS, start=1):
        existing = None
        if op == "update":
            habit = store.get(name)
            existing = {"name": name, "skill_md": (habit.path / "SKILL.md").read_text(encoding="utf-8"),
                        "habit_json": habit.config, "requests": store.requests(name)}
        result = author.author(client, "pebbles", text, existing, name=name,
                               taken=[h.name for h in store.list()], day_cap=10_000_000)
        tokens = sum((u or {}).get("total_tokens", 0) for u in result.usages)
        print(f"-- v{i} {op}：{text[:30]}…  token {tokens}")
        if not result.ok:
            print("   ✗ 失败：", result.error)
            return
        if result.questions:
            print("   ? 反问：", result.questions)
            return
        try:
            habit = store.write(result.name, result.skill_md, result.habit_json, text, kind=op)
        except HabitError as exc:
            print("   ✗ 写入失败：", exc)
            return
        name = habit.name
        print(f"   ✓ {habit.name}（{habit.title}）{result.change_summary}\n{brief(habit)}")
    print("   正文行数：", len(store.get(name).body.splitlines()), " request.md 条数：", len(store.recent_requests(name, 9)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--repeat", type=int, default=1)
    args = ap.parse_args()
    client = real_client()
    for run in range(args.repeat):
        print(f"==== 第 {run + 1} 次")
        with tempfile.TemporaryDirectory() as tmp:
            run_chain(client, Path(tmp))


if __name__ == "__main__":
    main()
