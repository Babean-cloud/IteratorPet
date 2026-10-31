"""委托任务探针：同一个"可写 + pwsh"任务，换不同的调用方式各跑一次，看哪种能跑通。

用法（在项目根目录、你自己的终端里）：

    $env:PYTHONPATH="src"; .\\.venv\\Scripts\\python tools\\dsh_probe.py

第一轮（四种控制台方式）全军覆没，连继承终端都失败，而手敲 dsh 却成功——所以这轮换的是
**怎么调 dsh、任务和输出怎么走**（brain/tasks.WIRINGS）：去掉 cmd 那一层、任务改成命令行参数、
stdin 接 NUL、输出写文件不走管道。最后一行是"最像手敲"的组合（继承终端 + 参数 + 文件）。

* 用桌宠配置里的 DeepSeek Key（或环境变量 DEEPSEEK_API_KEY），每种一次请求，共 5 次；
* 工作目录是任务文件夹下的临时子目录 ``_probe``，跑完删掉；
* 最后打印一张表，告诉你 ``config.json`` 里 ``dsh_wiring`` / ``dsh_launch`` 该设成什么。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iterator_pet.brain.tasks import TaskRunner, default_workspace, find_dsh  # noqa: E402
from iterator_pet.config import llm_api_key, load  # noqa: E402

TASK = ("用 pwsh 工具运行这条命令：Write-Output probe-ok 。"
        "只用 pwsh，不要用文件工具。最后只回复命令的原始输出；如果 pwsh 报错，回复退出码。")

COMBOS = (
    ("cmd-stdin", "hidden-console"),       # 现在的做法（对照）
    ("exe-stdin", "hidden-console"),       # 去掉 cmd
    ("exe-arg", "hidden-console"),         # 再把任务改成参数、stdin = NUL
    ("exe-arg-file", "hidden-console"),    # 再把输出改成写文件
    ("exe-arg-file", "inherit"),           # 最像在终端里手敲
)


def main() -> int:
    cfg = load()
    dsh = find_dsh(getattr(cfg, "dsh_path", ""))
    key = llm_api_key(cfg)
    if not dsh:
        print("没找到 dsh（DeepSeek Harness）。")
        return 1
    if not key:
        print("没有 DeepSeek Key：先在桌宠里设置，或设环境变量 DEEPSEEK_API_KEY。")
        return 1
    base = Path(getattr(cfg, "task_workspace", "") or default_workspace()) / "_probe"
    results = []
    for wiring, launch in COMBOS:
        name = f"{wiring} + {launch}"
        work = base / f"{wiring}-{launch}"
        work.mkdir(parents=True, exist_ok=True)
        runner = TaskRunner(dsh, api_key_fn=lambda: key, launch=launch, wiring=wiring, timeout=240)
        print(f"[{name}] 跑中…", flush=True)
        started = time.time()
        task = runner.start(TASK, "workspace-write", str(work))
        while not task.finished:
            time.sleep(0.5)
        text = (task.result or task.error).strip().replace("\n", " ")
        ok = "probe-ok" in text and "3221225794" not in text and "C0000142" not in text.upper()
        results.append((wiring, launch, ok, round(time.time() - started), text[:80]))
        print(f"[{name}] {'通过' if ok else '失败'}（{results[-1][3]} s）：{text[:80]}", flush=True)
    shutil.rmtree(base, ignore_errors=True)

    print("\n调用方式          控制台            结果   耗时  摘要")
    for wiring, launch, ok, secs, text in results:
        print(f"{wiring:17s} {launch:17s} {'通过' if ok else '失败'}   {secs:>3}s  {text}")
    good = [(w, l) for w, l, ok, _, _ in results if ok and l != "inherit"]
    if good:
        wiring, launch = good[-1] if good[-1][0] == "exe-arg-file" else good[0]
        print(f'\n建议：config.json 里设 "dsh_wiring": "{wiring}", "dsh_launch": "{launch}"，然后重启桌宠。')
    elif any(ok for _, l, ok, _, _ in results if l == "inherit"):
        print("\n只有继承终端的组合能跑：需要给桌宠做伪终端（ConPTY）启动，把这张表发给开发者。")
    else:
        print("\n全部失败：把这张表发给开发者。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
