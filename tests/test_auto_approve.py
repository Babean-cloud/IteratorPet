"""委托任务：一眼只读的命令自动放行，其余照旧逐条批准；同一条命令一次任务里只问一次。"""

import pytest

from iterator_pet.brain.executor import is_read_only_command


@pytest.mark.parametrize("command", [
    "Get-ChildItem",
    "Get-ChildItem -Recurse -File | Measure-Object",
    "(Get-ChildItem 接活测试).Count",
    "Select-String -Path *.log -Pattern ERROR | Select-Object -First 5",
    "gci | sort Length | ft Name, Length",
    "Get-Content 构建日志.log -Tail 20",
    "git status",
    "git log --oneline -5",
    "$n = (Get-ChildItem -File).Count; Write-Output $n",
    "Get-ChildItem | ForEach-Object { $_.Name }",
])
def test_read_only_commands_pass(command):
    assert is_read_only_command(command)


@pytest.mark.parametrize("command", [
    "Remove-Item b.txt",
    "Get-ChildItem | Remove-Item",
    "Get-ChildItem | ForEach-Object { Remove-Item $_ }",
    "Get-Content a.txt > b.txt",
    "Get-ChildItem | Out-File list.txt",
    "Set-Content a.txt hi",
    ".\\life.ps1",
    "powershell -File glider-test.ps1",
    "python run.py",
    "Get-Content C:\\Users\\babea\\secret.txt",
    "Get-Content ..\\other.txt",
    "Get-Content $env:USERPROFILE\\x",
    "(Get-Item a.txt).Delete()",
    "[IO.File]::WriteAllText('a', 'b')",
    "git push",
    "git commit -m x",
    "Invoke-WebRequest http://x",
    "Get-ChildItem; Stop-Process -Name x",
    "& notepad",
    "Get-Content $(Get-Item a)",
])
def test_everything_else_needs_approval(command):
    assert not is_read_only_command(command)


def test_runner_auto_runs_and_remembers_approvals(tmp_path):
    import threading

    from iterator_pet.brain.executor import AgentRunner
    from iterator_pet.brain.tasks import Task

    ran = []
    runner = AgentRunner(lambda: None, shell_fn=lambda cmd, cwd, timeout, cancelled: ran.append(cmd) or (0, "ok"),
                         approval_timeout=2.0)
    task = Task(text="t", mode="read-only", workspace=str(tmp_path))
    assert runner._command(task, "Get-ChildItem", "看看").startswith("退出码 0")
    assert task.approval is None and "只读，自动放行" in task.log[-2]

    def approve_soon():
        while task.approval is None:
            pass
        task.approval.allowed = True
        task.approval.event.set()

    threading.Thread(target=approve_soon, daemon=True).start()
    runner._command(task, "python run.py", "跑脚本")
    assert ran == ["Get-ChildItem", "python run.py"]
    runner._command(task, "python run.py", "再跑一次")
    assert ran[-1] == "python run.py" and "批准过" in task.log[-2], "同一条命令不再问"
