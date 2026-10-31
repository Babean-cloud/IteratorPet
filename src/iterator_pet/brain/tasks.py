"""委托任务的 DSH 后端（0.3.0 起只用作「DSH 沙箱 · 只读」一档；默认走 ``executor.py``）。

把任务交给本机的 DeepSeek Harness（``dsh``）：

    dsh --profile headless --json "<任务>"      （工作目录 = 任务文件夹）

为什么这样安全（2026-10-30 实测 + 读 dsh-user-approval 的 README）：
* 沙箱由 ``DSH_PERMISSION_MODE`` 决定：``read-only`` 只能读；``workspace-write`` 只能写
  工作目录。``danger-full-access`` 桌宠**不提供**。
* headless 没有内置应答者：需要审批的操作一律 ``unavailable`` → **以拒绝方式关闭**。
  所以越出沙箱的事不会弹窗问谁，直接被拒。
* 任务**只能由用户亲手发起**（对话窗口「让他去做…」+ 确认卡）。导演（看屏幕、自主说话
  的那个模型）永远不能发起任务——屏幕上的文字可能是提示注入。

dsh 的 CLI 没有自己的 Key（实测报 ``MISSING_CREDENTIAL``），所以子进程环境里带上桌宠的
``DEEPSEEK_API_KEY``（只进子进程环境，不落盘、不进命令行）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

MODES = {"read-only": "只读", "workspace-write": "可写任务文件夹"}
DEFAULT_TIMEOUT = 15 * 60.0
_COMMON_DSH = (
    r"D:\DevelopTool\Deepseek Harness\resources\runtime\cli\bin\dsh.cmd",
    r"%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd",
    r"%ProgramFiles%\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd",
)


def find_dsh(configured: str = "") -> str | None:
    """配置 → PATH → 卸载信息里的安装目录 → 常见位置。"""
    candidates = []
    if configured:
        candidates.append(os.path.expandvars(configured))
    found = shutil.which("dsh")
    if found:
        candidates.append(found)
    candidates.extend(_registry_dsh())
    candidates.extend(os.path.expandvars(p) for p in _COMMON_DSH)
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


def _registry_dsh() -> list[str]:
    if sys.platform != "win32":
        return []
    out = []
    try:
        import winreg

        roots = ((winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"))
        for hive, key in roots:
            try:
                with winreg.OpenKey(hive, key) as base:
                    for i in range(winreg.QueryInfoKey(base)[0]):
                        try:
                            with winreg.OpenKey(base, winreg.EnumKey(base, i)) as sub:
                                name = winreg.QueryValueEx(sub, "DisplayName")[0]
                                if "deepseek harness" not in str(name).lower():
                                    continue
                                where = winreg.QueryValueEx(sub, "InstallLocation")[0]
                                out.append(str(Path(where) / "resources" / "runtime" / "cli"
                                               / "bin" / "dsh.cmd"))
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        pass
    return out


def pwsh_available() -> bool:
    """dsh 的命令工具只调 PowerShell 7（``pwsh``），不认系统自带的 5.1（powershell.exe）。

    没装时，任务里每条命令都在沙箱里以 0xC0000142 起不来（用户实测，真终端里也一样），
    而不是干净的"找不到命令"——所以在确认卡上提前说清楚。
    """
    if shutil.which("pwsh"):
        return True
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("LOCALAPPDATA", "")):
        if base and list(Path(base).glob("PowerShell/*/pwsh.exe")):
            return True
    return False


def default_workspace() -> Path:
    return Path(os.path.expanduser("~")) / "Documents" / "IteratorPet-workspace"


@dataclass
class Task:
    text: str
    mode: str
    workspace: str
    started: float = field(default_factory=time.time)
    state: str = "running"            # running / done / failed / cancelled / timeout
    result: str = ""
    error: str = ""
    session_id: str = ""
    progress: deque = field(default_factory=lambda: deque(maxlen=12))
    # 下面这些只有自带执行器（executor.py）用：
    character: str = ""               # 哪个角色接的活：结果记进它的对话记录
    label: str = ""                   # 状态栏显示的权限档（只读 / 可写 / DSH 沙箱）
    log: list = field(default_factory=list)       # 要进对话窗口的步骤（写了什么、跑了什么命令）
    usage: list = field(default_factory=list)     # 每次请求的 usage，主线程取走记账
    approval: object = None           # 正在等用户批准的命令（executor.Approval）
    approval_why: str = ""
    approved: set = field(default_factory=set)   # 这次任务里你批准过的命令：同一条不再问

    @property
    def finished(self) -> bool:
        return self.state != "running"


def _describe_event(event: dict) -> str | None:
    """把 ``--json`` 事件压成一行进度（只认识的类型才显示，其余忽略）。"""
    kind = event.get("type")
    if kind == "status":
        phase = event.get("phase")
        if phase == "turn_end":
            reason = event.get("reason") or {}
            if reason.get("kind") == "error":
                return f"出错：{(reason.get('error') or {}).get('message', '')[:120]}"
        return None
    for key in ("tool", "toolName", "name"):
        if isinstance(event.get(key), str) and kind and "tool" in str(kind):
            return f"{kind}: {event[key]}"
    if kind and "tool" in str(kind):
        return str(kind)
    return None


class TaskRunner:
    """一次只跑一个任务；``poll()`` 在主线程取进度。命令可注入（测试用假进程）。"""

    def __init__(self, dsh_path: str | None, api_key_fn=lambda: "", command_fn=None,
                 timeout: float = DEFAULT_TIMEOUT, launch: str = "hidden-console",
                 wiring: str = "cmd-stdin"):
        self.dsh_path = dsh_path
        self.launch = launch if launch in LAUNCH_MODES else "hidden-console"
        self.wiring = wiring if wiring in WIRINGS else "cmd-stdin"
        if self.wiring != "cmd-stdin" and dsh_path and direct_dsh(dsh_path) is None:
            self.wiring = "cmd-stdin"                 # 找不到 exe / cli.js：退回走 dsh.cmd
        self.api_key_fn = api_key_fn
        self.command_fn = command_fn or self._dsh_command
        self.timeout = timeout
        self.current: Task | None = None
        self.history: list[Task] = []
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        # 绑定方法每次取都是新对象，只能用 == 比（原来用 is not，永远为真：没装 dsh 也显示可用）
        return self.command_fn != self._dsh_command or bool(self.dsh_path)

    @property
    def busy(self) -> bool:
        return self.current is not None and not self.current.finished

    def _dsh_command(self, task: Task) -> list[str]:
        """``wiring`` 决定怎么调 dsh、任务文本怎么进去（见 ``WIRINGS``）。"""
        if self.wiring == "cmd-stdin" or not self.dsh_path:
            return ["cmd.exe", "/c", self.dsh_path, "--profile", "headless", "--json", "-"]
        exe, cli = direct_dsh(self.dsh_path)
        tail = ["-"] if self.wiring == "exe-stdin" else [task.text]
        return [str(exe), "--expose-internals", str(cli), "--profile", "headless", "--json", *tail]

    def start(self, text: str, mode: str, workspace: str) -> Task:
        if self.busy:
            raise RuntimeError("已经有一个任务在跑")
        if mode not in MODES:
            raise ValueError(f"不支持的权限：{mode}")
        if not self.available:
            raise RuntimeError("没找到 DeepSeek Harness（dsh）")
        Path(workspace).mkdir(parents=True, exist_ok=True)
        task = Task(text=text.strip(), mode=mode, workspace=str(workspace))
        self.current = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
            task = self.current
        if task is not None and not task.finished:
            task.state = "cancelled"
        if proc is not None and proc.poll() is None:
            _kill_tree(proc)

    # -- 工作线程 ------------------------------------------------------------------
    def _run(self, task: Task) -> None:
        env = dict(os.environ)
        env["DSH_PERMISSION_MODE"] = task.mode
        key = self.api_key_fn() or ""
        if key and not env.get("DEEPSEEK_API_KEY"):
            env["DEEPSEEK_API_KEY"] = key
        flags, startup = launch_flags(self.launch)
        final = ""
        wiring = self.wiring if self.command_fn == self._dsh_command else "cmd-stdin"
        via_stdin = wiring in ("cmd-stdin", "exe-stdin")
        to_file = wiring == "exe-arg-file"
        if wiring != "cmd-stdin":
            env["ELECTRON_RUN_AS_NODE"] = "1"         # dsh.cmd 里就是这么设的
        out_path = err_path = None
        try:
            if to_file:
                import tempfile

                out_fd, out_path = tempfile.mkstemp(prefix="iterpet_dsh_", suffix=".jsonl")
                err_fd, err_path = tempfile.mkstemp(prefix="iterpet_dsh_", suffix=".err")
                stdout, stderr_target = os.fdopen(out_fd, "wb"), os.fdopen(err_fd, "wb")
            else:
                stdout, stderr_target = subprocess.PIPE, subprocess.PIPE
            proc = subprocess.Popen(
                self.command_fn(task), cwd=task.workspace, env=env,
                stdin=subprocess.PIPE if via_stdin else subprocess.DEVNULL,
                stdout=stdout, stderr=stderr_target,
                creationflags=flags, startupinfo=startup,
            )
            if to_file:
                stdout.close()
                stderr_target.close()
            with self._lock:
                self._proc = proc
            if via_stdin:
                # 任务走 stdin（``-``）：不进命令行，免得引号/换行在 cmd 里出事
                proc.stdin.write(task.text.encode("utf-8"))
                proc.stdin.close()
            timer = threading.Timer(self.timeout, self._on_timeout, args=(task, proc))
            timer.daemon = True
            timer.start()
            lines = _tail_file(out_path, proc) if to_file else proc.stdout
            for raw in lines:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "session":
                    task.session_id = str(event.get("sessionId", ""))
                note = _describe_event(event)
                if note:
                    task.progress.append(note)
                if event.get("type") == "final":
                    # 实测：stdin 是管道时 dsh 发完 final 不退出（`< NUL` 才退）。
                    # final 就是完成信号：给 3 s 自己退，不退就收掉整棵进程树。
                    final = str(event.get("text") or "")
                    break
            if not to_file:
                # 管道模式下 dsh 发完 final 还会等我们把管道读完；关掉读端让它走
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                try:
                    proc.wait(timeout=15.0)
                except subprocess.TimeoutExpired:
                    pass                              # 强杀已发出，晚一点退也不影响结果
            timer.cancel()
            if to_file:
                stderr = Path(err_path).read_bytes().decode("utf-8", "replace").strip()
            elif proc.poll() is not None:
                stderr = proc.stderr.read().decode("utf-8", "replace").strip()
            else:
                stderr = ""                           # 还没退干净：别在这儿读阻塞住
            if task.state == "running":
                if final:
                    task.state, task.result = "done", final
                else:
                    task.state = "failed"
                    task.error = (list(task.progress)[-1:] or [stderr[-300:] or f"退出码 {proc.returncode}"])[0]
        except Exception as exc:                      # noqa: BLE001
            if task.state == "running":
                task.state, task.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._proc = None
            for path in (out_path, err_path):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            self.history.append(task)
            del self.history[:-20]

    def _on_timeout(self, task: Task, proc) -> None:
        if task.state == "running":
            task.state = "timeout"
            task.error = f"超过 {int(self.timeout / 60)} 分钟，已停止"
        _kill_tree(proc)


LAUNCH_MODES = ("hidden-console", "minimized-console", "no-window", "inherit")

# 怎么调 dsh、任务文本和输出怎么走。用户实测（2026-10-30）：四种控制台方式在"可写 + pwsh"下
# **全部**失败（连继承终端也失败），而在终端里手敲 ``dsh.cmd ... "任务"`` 却成功——剩下的差别是
# 管道：手敲时任务是命令行参数、stdin/stdout 是终端；我们是 stdin/stdout/stderr 全走匿名管道
# （Windows 的匿名管道就是命名管道实现的，dsh 文档说受限令牌下打不开命名管道），中间还隔着一层 cmd。
#   cmd-stdin     dsh.cmd，任务走 stdin，输出走管道（最早的做法）
#   exe-stdin     直接调 Electron exe（去掉 cmd），任务走 stdin
#   exe-arg       直接调 exe，任务是参数，stdin = NUL，输出走管道
#   exe-arg-file  同上，但 stdout/stderr 写临时文件，不用管道
WIRINGS = ("cmd-stdin", "exe-stdin", "exe-arg", "exe-arg-file")


def direct_dsh(dsh_path: str) -> tuple[Path, Path] | None:
    """dsh.cmd 背后的 (Electron exe, cli.js)。cli.js 在 app.asar 里，只能检查 exe 在不在。"""
    try:
        root = Path(dsh_path).resolve().parents[4]
    except (IndexError, OSError):
        return None
    exe = root / "DeepSeek Harness.exe"
    cli = (root / "resources" / "app.asar" / "dsh" / "node_modules" / "@deepseek-ai"
           / "dsh-desktop-host" / "lib" / "cli.js")
    return (exe, cli) if exe.is_file() and (root / "resources" / "app.asar").is_file() else None


def _tail_file(path: str, proc):
    """像读管道一样逐行读一个还在被写的文件，进程退出且读完为止。"""
    with open(path, "rb") as handle:
        pending = b""
        while True:
            chunk = handle.readline()
            if chunk:
                pending += chunk
                if pending.endswith(b"\n"):
                    yield pending
                    pending = b""
                continue
            if proc.poll() is not None:
                rest = handle.read()
                if pending or rest:
                    yield pending + rest
                return
            time.sleep(0.15)


def launch_flags(mode: str):
    """启动 dsh 的几种控制台方式（``tools/dsh_probe.py`` 逐个试，结果决定默认值）。

    * ``hidden-console``    新控制台 + SW_HIDE（当前默认）
    * ``minimized-console`` 新控制台 + 最小化不激活（任务栏上会闪一下）
    * ``no-window``         CREATE_NO_WINDOW（最早的做法）
    * ``inherit``           继承调用方的控制台——从终端启动时就是那个终端（对照组）
    用户实测（2026-10-30）：hidden-console 下只读能跑 pwsh，可写不行；在自己终端里手动跑
    可写却行——所以问题出在控制台，而不是 dsh 的可写沙箱本身。
    """
    if sys.platform != "win32" or mode == "inherit":
        return 0, None
    if mode == "no-window":
        return subprocess.CREATE_NO_WINDOW, None
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 7 if mode == "minimized-console" else 0   # SW_SHOWMINNOACTIVE / SW_HIDE
    return subprocess.CREATE_NEW_CONSOLE, startup


def _hidden_console():
    """给 dsh 一个**真的但藏起来的**控制台，而不是 CREATE_NO_WINDOW。

    dsh-sandbox-windows-acl 的 README（已知限制）：沙箱子进程共享宿主控制台，单独建控制台
    的会在 DLL 初始化时 0xC0000142。所以宿主（dsh）手里得有一个控制台可共享；新控制台 +
    SW_HIDE 满足它，窗口不显示。

    注意：用户机器上任务里跑命令仍然 0xC0000142，在**自己的真终端**里直接跑 dsh 也一样——
    真正的原因是没装 PowerShell 7（dsh 的命令工具只调 ``pwsh``，缺了它在沙箱里就报这个码）。
    见 ``pwsh_available()``。
    """
    if sys.platform != "win32":
        return 0, None
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0                                    # SW_HIDE
    return subprocess.CREATE_NEW_CONSOLE, startup


def _kill_tree(proc) -> None:
    """dsh.cmd → Electron(node) 是进程树，只杀 cmd 会留下孤儿。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
    except Exception:
        pass
