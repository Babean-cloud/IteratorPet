"""感知：前台窗口 / 键鼠空闲（ctypes，几乎零成本）+ 本地 OCR（Windows 自带，离线）。

档位（config.perception）：
* ``off``   —— 什么都不看；
* ``title`` —— 只看前台窗口的进程名 + 标题 + 空闲秒数；
* ``ocr``   —— 再加：换窗口停稳后 / 同一窗口每隔几分钟，截一次前台窗口，
  用 ``Windows.Media.Ocr`` 在本机认字，压成一两百字。**截图不出本机**，只有文字进请求。

黑名单（进程名或标题里含关键词）：标题打码成"（隐私窗口）"，也不截图。
"""

from __future__ import annotations

import ctypes
import asyncio
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BLACKLIST = (
    "keepass", "1password", "bitwarden", "lastpass",
    "wechat", "weixin", "qq.exe", "tim.exe", "telegram", "whatsapp",
    "密码", "password", "银行", "bank", "支付", "alipay",
    # 心事能读后台窗口之后补的：放密钥 / 令牌的文件和页面
    "密钥", "api key", "apikey", "api_key", "secret", "token", "credential", ".env",
)
OCR_MAX_CHARS = 1000
OCR_SETTLE_SECONDS = 3.0       # 换窗口后停留多久才截图（切来切去不截）
PROBE_SECONDS = 1.8           # 感知 tick 约 2 s；留余量避免错过一次 tick
OCR_MIN_GAP = 20.0             # 任意两次 OCR 至少隔多久（浏览器来回切标签页时不狂截）
READ_MIN_SHOWN = 1.2           # 读屏幕的扫描圈至少亮多久
FOUND_SHOWN = 1.0              # 认出新内容后"亮一下"持续多久
READ_FADE = 0.8               # 认完后淡出多久
SCREEN_NEW_MAX_CHARS = 400    # 只发新增行时的上限
OCR_FRESH_SECONDS = 90.0      # 认出的字超过这么久就不再交给模型（过时的内容只会让它说胡话）

# PowerShell 5.1 + WinRT。写成字符串而不是数据文件：打包不用改 datas。
_OCR_SCRIPT = r"""
param([string]$Path)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $task.Wait(15000)
    $task.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { exit 3 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) { [Console]::WriteLine($line.Text) }
$stream.Dispose()
"""


@dataclass
class WindowInfo:
    hwnd: int
    app: str
    title: str
    rect: tuple[int, int, int, int]
    private: bool
    since: float


# -- Win32 ------------------------------------------------------------------
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """距离上次键盘/鼠标输入多少秒。"""
    if sys.platform != "win32":
        return 0.0
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    now = ctypes.windll.kernel32.GetTickCount()
    return max(0.0, ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0)


def _process_name(pid: int) -> str:
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _window_details(hwnd: int) -> tuple[int, int, str, str, tuple[int, int, int, int]] | None:
    """按 HWND 取窗口元数据；给桌宠输入界面背后的目标窗口复核身份。"""
    if sys.platform != "win32" or not hwnd:
        return None
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = user32.IsIconic.argtypes = (ctypes.c_void_p,)
    user32.GetWindowTextLengthW.argtypes = (ctypes.c_void_p,)
    user32.GetWindowTextW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
    user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    user32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    handle = ctypes.c_void_p(hwnd)
    if not user32.IsWindow(handle) or user32.IsIconic(handle):
        return None
    length = user32.GetWindowTextLengthW(handle)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    rect = wintypes.RECT()
    user32.GetWindowRect(handle, ctypes.byref(rect))
    return (int(hwnd), int(pid.value), _process_name(pid.value), buf.value,
            (rect.left, rect.top, rect.right, rect.bottom))


def foreground() -> tuple[int, int, str, str, tuple[int, int, int, int]] | None:
    """(hwnd, pid, exe, title, rect)；拿不到返回 None。"""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return _window_details(user32.GetForegroundWindow())


# -- 文本清洗 ------------------------------------------------------------------
_CJK = r"[　-〿㐀-鿿＀-￯]"


def clean_ocr(lines: list[str], limit: int = OCR_MAX_CHARS) -> str:
    """Windows OCR 会在每个汉字之间插空格；去掉，丢掉碎片，去重，截断。"""
    seen: set[str] = set()
    kept: list[str] = []
    for raw in lines:
        line = re.sub(rf"(?<={_CJK})\s+(?={_CJK})", "", raw.strip())
        line = re.sub(r"\s{2,}", " ", line)
        short_button = (len(line) == 2 and
                        (line.upper() in {"OK", "NO"} or re.fullmatch(rf"{_CJK}{{2}}", line)))
        if (len(line) < 3 and not short_button) or line in seen:
            continue
        seen.add(line)
        kept.append(line)
    text = " / ".join(kept)
    return text[:limit]


def _norm_line(line: str) -> str:
    """行级 diff 用的规范化：去掉空白和数字，时钟、行号、计数器变了不算新内容。"""
    return re.sub(r"[\s\d]+", "", line)


def new_stats() -> dict:
    return {"ocr_n": 0, "ocr_s": 0.0, "observe_n": 0, "screen_chars": 0, "tokens": 0}


_script_file: Path | None = None
_ocr_engine = None
_ocr_engine_lock = threading.Lock()


def _script_path() -> Path:
    global _script_file
    if _script_file is None or not _script_file.is_file():
        path = Path(tempfile.gettempdir()) / "iteratorpet_ocr.ps1"
        # UTF-8 with BOM：PowerShell 5.1 才不会按 ANSI 读
        path.write_text(_OCR_SCRIPT, encoding="utf-8-sig")
        _script_file = path
    return _script_file


def _make_ocr_engine():
    from winrt.windows.media.ocr import OcrEngine

    return OcrEngine.try_create_from_user_profile_languages()


def _get_ocr_engine():
    """进程内复用同一个 WinRT OCR 引擎。"""
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = _make_ocr_engine()
        if _ocr_engine is None:
            raise RuntimeError("系统没有可用的 OCR 语言")
    return _ocr_engine


async def _recognize_lines(image, engine) -> list[str]:
    """图片只在内存中转成 WinRT 位图。"""
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    data = io.BytesIO()
    image.save(data, format="PNG")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    try:
        writer.write_bytes(data.getvalue())
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
        return [line.text for line in result.lines]
    finally:
        writer.close()
        stream.close()


def _powershell_ocr_lines(image) -> list[str]:
    """未安装 PyWinRT 时的兼容路径；临时图片认完立刻删除。"""
    fd, name = tempfile.mkstemp(prefix="iterpet_", suffix=".png")
    os.close(fd)
    shot = Path(name)
    try:
        image.save(shot)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(_script_path()), str(shot)],
            capture_output=True, timeout=30, creationflags=flags,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"OCR 退出码 {proc.returncode}: "
                               f"{proc.stderr.decode('utf-8', 'replace')[:120]}")
        return proc.stdout.decode("utf-8", "replace").splitlines()
    finally:
        try:
            shot.unlink()
        except OSError:
            pass


def ocr_lines(image) -> list[str]:
    """PIL 图 → 本机 WinRT OCR；缺少依赖时兼容旧版 PowerShell。"""
    try:
        from winrt.windows.media import ocr as _ocr_module  # noqa: F401
        from winrt.windows.graphics import imaging as _imaging_module  # noqa: F401
        from winrt.windows.storage import streams as _streams_module  # noqa: F401
    except ModuleNotFoundError:
        return _powershell_ocr_lines(image)
    with _ocr_engine_lock:
        return asyncio.run(_recognize_lines(image, _get_ocr_engine()))


def ocr_image(image, limit: int = OCR_MAX_CHARS) -> str:
    """认字 + 清洗；失败返回空串。"""
    try:
        return clean_ocr(ocr_lines(image), limit=limit)
    except Exception:                                 # noqa: BLE001
        return ""


class Perception:
    def __init__(self, level: str = "ocr", blacklist=DEFAULT_BLACKLIST, clock=time.monotonic):
        from .window_events import PopupWatcher

        self.level = level if level in ("off", "title", "ocr") else "ocr"
        self.blacklist = tuple(str(item).lower() for item in blacklist if str(item).strip())
        self.clock = clock
        self.own_pid = os.getpid()
        self.window: WindowInfo | None = None
        self._view_since = -1e9          # 每次切窗口/标题都重置，不影响长会话计时
        self._window_read_pending = False
        self._own_window_active = False
        self._own_overlay: tuple[int, int, int, int] | None = None
        self._overlay_version = 0
        # OCR 结果：(hwnd, title) -> (时间, 文本)。只在工作线程写一个新 dict，主线程读引用。
        self._ocr: dict[tuple[int, str], tuple[float, str]] = {}
        self._frames = {}             # 每个窗口的上一张小灰图；不保存完整截图
        self._volatility = {}         # 12×8 格的长期闪动程度
        self._probe_frames = {}
        self._probe_volatility = {}
        self._intervals = {}
        self._change_ready = None
        self._probe_busy = False
        self._last_probe_at = -1e9
        self._recent_frames = {}       # 同一窗口近期指纹 → (小灰图, 文字)
        self._checked_at = {}          # 最近一次 L2 检查；探针不改这个时间
        self._full_ocr_at = {}         # 定期整窗核对，防止小字变化被缩略图漏掉
        self._ocr_busy = False
        self._full_requested = False    # 用户明确让它看屏幕时，下一次强制整窗
        self.popup_watcher = PopupWatcher(clock)
        self._pending_popup: tuple[int, float] | None = None
        self._popup_ready = False
        self._popup_target: tuple[int, float] | None = None
        self._last_ocr_at = -1e9
        self._read_start = self._read_end = -1e9      # 头顶扫描圈的起止（reading_level）
        self._found_at = -1e9                         # 上次认出新行的时间（found_level）
        self.ocr_error: str | None = None
        self.ocr_version = 0          # 每认出一次新文字 +1
        # 基线计数（dev/notes/attention-capture-design.md §5）；director 每小时取走并清零
        self.stats = new_stats()
        # 已经发给模型的行：(hwnd, title) -> (规范化后的行集合, 上次发出的时间)
        self._sent: dict[tuple[int, str], tuple[set[str], float]] = {}

    # -- 主线程，便宜 --------------------------------------------------------
    def _private(self, app: str, title: str) -> bool:
        haystack = f"{app} {title}".lower()
        return any(word in haystack for word in self.blacklist)

    def start_events(self) -> None:
        if self.level == "ocr":
            self.popup_watcher.start()

    def stop_events(self) -> None:
        self.popup_watcher.stop()

    def set_own_overlay(self, rect: tuple[int, int, int, int] | None) -> None:
        """AI 状态面板的屏幕矩形；截图时盖白，避免读到桌宠自己的字。"""
        if rect != self._own_overlay:
            self._own_overlay = rect
            self._overlay_version += 1

    def has_ready_window_event(self) -> bool:
        return self.popup_watcher.foreground_ready()

    def window_read_due(self, now: float) -> bool:
        return (self._window_read_pending and not self._ocr_busy and self.level == "ocr"
                and not self._own_window_active and now - self._view_since >= OCR_SETTLE_SECONDS)

    def change_read_due(self, now: float) -> bool:
        w = self.window
        return bool(w is not None and not w.private and self._change_ready is not None
                    and self._change_ready[0] == (w.hwnd, w.title)
                    and now - self._change_ready[1] <= 20.0
                    and now - self._last_ocr_at >= 15.0
                    and not self._ocr_busy and not self._own_window_active)

    def _grab_masked(self, rect, overlay_version):
        from PIL import ImageDraw, ImageGrab

        image = ImageGrab.grab(bbox=rect, all_screens=True)
        if self._overlay_version != overlay_version:
            return None
        overlay = self._own_overlay
        if overlay is not None:
            left, top, right, bottom = rect
            x0, y0 = max(left, overlay[0]) - left, max(top, overlay[1]) - top
            x1, y1 = min(right, overlay[2]) - left, min(bottom, overlay[3]) - top
            if x1 > x0 and y1 > y0:
                ImageDraw.Draw(image).rectangle((x0, y0, x1 - 1, y1 - 1), fill="white")
        return image

    def capture_vision(self, mode: str, key: tuple[int, str],
                       own_ui_hidden: bool = False) -> bytes | None:
        """明确问屏幕时在工作线程截图；只返回内存里的 JPEG，不写文件。"""
        if mode not in ("roi", "full") or self.level == "off":
            return None
        w = self.window
        if w is None or w.private or (w.hwnd, w.title) != key:
            return None
        current = foreground()
        if current is None:
            return None
        if current[1] == self.own_pid:
            # 只接受调用方已经收起输入界面的情况；按 HWND 抓窗口在部分 GPU 程序上会是空图。
            if not own_ui_hidden:
                return None
            target = _window_details(w.hwnd)
            if (target is None or target[1] == self.own_pid or target[2] != w.app
                    or target[3] != w.title or self._private(target[2], target[3])):
                return None
            image = self._grab_masked(target[4], self._overlay_version)
        else:
            if (current[0] != w.hwnd or current[3] != w.title
                    or self._private(current[2], current[3])):
                return None
            target = current
            image = self._grab_masked(current[4], self._overlay_version)
        if image is None:
            return None
        rect = target[4]
        if mode == "roi":
            from .attention import TILES, compare_frames, regions_from_tiles

            previous = self._frames.get(key)
            change = compare_frames(previous, image) if previous is not None else None
            if change is not None and 0 < len(change.important_tiles) <= TILES[0] * TILES[1] // 2:
                boxes = regions_from_tiles(change.important_tiles, image.size)
                if boxes:
                    image = image.crop(max(boxes, key=lambda box: (box[2] - box[0]) * (box[3] - box[1])))
            else:
                from .uia import focus_snapshot

                focus = focus_snapshot(w.hwnd, rect)
                if focus is not None:
                    left, top, _right, _bottom = rect
                    x0, y0, x1, y1 = focus.rect
                    box = (max(0, x0 - left), max(0, y0 - top),
                           min(image.width, x1 - left), min(image.height, y1 - top))
                    if box[2] - box[0] >= 100 and box[3] - box[1] >= 60:
                        image = image.crop(box)
        from .pearl_media import fit

        stream = io.BytesIO()
        fit(image).save(stream, format="JPEG", quality=78)
        return stream.getvalue()

    def maybe_probe(self, now: float | None = None) -> bool:
        """低频后台看小图；只标记变化，不在主线程截屏或认字。"""
        w = self.window
        if (self.level != "ocr" or w is None or w.private or self._own_window_active
                or self._window_read_pending or self._ocr_busy or self._probe_busy):
            return False
        now = self.clock() if now is None else now
        if now - self._last_probe_at < PROBE_SECONDS:
            return False
        self._probe_busy = True
        self._last_probe_at = now
        threading.Thread(target=self._run_probe, args=((w.hwnd, w.title), w.rect, self._overlay_version),
                         daemon=True).start()
        return True

    def _run_probe(self, key, rect, overlay_version) -> None:
        try:
            from PIL import ImageChops
            from .attention import BASE_INTERVAL, compare_frames, dhash, next_interval, thumbnail

            current = foreground()
            if current is None or current[1] == self.own_pid or current[0] != key[0]:
                return
            image = self._grab_masked(rect, overlay_version)
            if image is None:
                return
            frame = thumbnail(image)
            previous = self._probe_frames.get(key) or self._frames.get(key)
            self._probe_frames[key] = frame
            if len(self._probe_frames) > 20:
                self._probe_frames = dict(list(self._probe_frames.items())[-20:])
            if previous is None:
                return
            change = compare_frames(previous, frame, self._probe_volatility.get(key))
            self._probe_volatility[key] = change.volatility
            if change.changed:
                self._intervals[key] = BASE_INTERVAL
                self._change_ready = (key, self.clock())
            else:
                self._intervals[key] = next_interval(self._intervals.get(key, BASE_INTERVAL),
                                                      stable=change.fingerprint == dhash(previous))
                last = self._frames.get(key)
                hit = self._ocr.get(key)
                if last is not None and hit and ImageChops.difference(last, frame).getbbox() is None:
                    table = dict(self._ocr)
                    table[key] = (self.clock(), hit[1])  # 画面逐像素不变：文字仍新鲜
                    self._ocr = table
        except Exception:                         # noqa: BLE001
            pass                                    # 探针失败不影响下一轮正常 OCR
        finally:
            self._probe_busy = False

    def poll(self) -> tuple[WindowInfo | None, bool]:
        """读前台窗口。返回 (当前窗口, 是否刚换了窗口)。自己的窗口不算。"""
        if self.level == "off":
            return None, False
        self.popup_watcher.take_foreground()
        popup = self.popup_watcher.ready()
        if popup is not None:
            self._pending_popup = (popup, self.clock())
        try:
            fg = foreground()
        except Exception:
            fg = None
        if fg is None:
            return self.window, False
        hwnd, pid, app, title, rect = fg
        self._own_window_active = pid == self.own_pid
        if self._own_window_active or not title.strip():
            return self.window, False
        pending = self._pending_popup
        if pending is not None:
            if self.clock() - pending[1] > 5.0:
                self._pending_popup = None
            elif pending[0] == hwnd:
                from .window_events import inspect_window, is_popup_candidate

                previous = inspect_window(self.window.hwnd) if self.window is not None else None
                meta = inspect_window(hwnd)
                if is_popup_candidate(meta, hwnd, previous.pid if previous else 0, self.own_pid):
                    self._popup_ready = True
                    self._popup_target = (hwnd, self.clock())
                self._pending_popup = None
        changed = (self.window is None or self.window.hwnd != hwnd
                   or self.window.title != title)
        if changed:
            app_changed = self.window is None or self.window.app != app
            since = self.clock() if app_changed or self.window is None else self.window.since
            self.window = WindowInfo(hwnd, app, title, rect, self._private(app, title), since)
            self._view_since = self.clock()
            self._window_read_pending = True
            return self.window, app_changed
        self.window.rect = rect
        return self.window, False

    def describe(self) -> dict | None:
        """给模型看的窗口摘要。"""
        w = self.window
        if w is None:
            return None
        minutes = int((self.clock() - w.since) / 60.0)
        if w.private:
            return {"app": "（隐私窗口）", "title": "", "minutes": minutes}
        app = w.app[:-4] if w.app.lower().endswith(".exe") else w.app
        return {"app": app, "title": w.title[:80], "minutes": minutes}

    def ocr_text(self, max_age: float | None = OCR_FRESH_SECONDS) -> str | None:
        """当前窗口认出的字。超过 ``max_age`` 秒的不给：切回一个很久以前认过的窗口时，
        缓存里那份是旧的（用户反馈"会说些过时屏幕内容里莫名其妙的话"），宁可没有。"""
        w = self.window
        if w is None or w.private:
            return None
        hit = self._ocr.get((w.hwnd, w.title))
        if not hit or (max_age is not None and self.clock() - hit[0] > max_age):
            return None
        return hit[1]

    def reading_level(self) -> float:
        """正在读屏幕的"亮度" 0..1，给头顶的扫描圈用：开始时快速淡入，认完后慢慢淡出；
        至少亮 ``READ_MIN_SHOWN`` 秒（OCR 很快时也看得见）。隐私窗口不读，就不亮。"""
        now = self.clock()
        w = self.window
        if w is None or w.private or self.level != "ocr":
            return 0.0
        end = now if self._ocr_busy else max(self._read_end, self._read_start + READ_MIN_SHOWN)
        if now > end:
            return max(0.0, 1.0 - (now - end) / READ_FADE)
        return min(1.0, (now - self._read_start) / 0.3)

    def found_level(self) -> float:
        """刚认出新内容的"余光" 1→0（``FOUND_SHOWN`` 秒内衰减），给扫描圈亮一下用。"""
        w = self.window
        if w is None or w.private or self.level != "ocr":
            return 0.0
        return max(0.0, 1.0 - (self.clock() - self._found_at) / FOUND_SHOWN)

    def screen_delta(self, full: bool = False, limit: int = SCREEN_NEW_MAX_CHARS) -> dict:
        """要塞进这次请求的屏幕文字字段。同一窗口里已经发过的行不再发：

        * 第一次 / ``full``（用户在问话）→ ``screen_text``：整段；
        * 有新增行 → ``screen_new``：只发新增的行；
        * 一字未变 → ``screen_unchanged_s``：距离上次发出多少秒。
        """
        text = self.ocr_text()
        w = self.window
        if not text or w is None:
            return {}
        key = (w.hwnd, w.title)
        now = self.clock()
        lines = text.split(" / ")
        norms = [_norm_line(line) for line in lines]
        seen, sent_at = self._sent.get(key, (None, now))
        if seen is None or full:
            out = {"screen_text": text}
        else:
            fresh = [line for line, norm in zip(lines, norms) if norm and norm not in seen]
            if not fresh:
                return {"screen_unchanged_s": int(now - sent_at)}
            out = {"screen_new": " / ".join(fresh)[:limit]}
        table = dict(self._sent)
        table[key] = ((seen or set()) | set(norms), now)
        if len(table) > 20:
            for old in sorted(table, key=lambda k: table[k][1])[:-20]:
                del table[old]
        self._sent = table
        age = self.ocr_age()
        if age is not None:
            out["screen_text_age_s"] = int(age)
        return out

    def ocr_age(self) -> float | None:
        w = self.window
        hit = self._ocr.get((w.hwnd, w.title)) if w is not None else None
        return None if hit is None else max(0.0, self.clock() - hit[0])

    # -- OCR（工作线程） ------------------------------------------------------
    def request_full(self) -> None:
        self._full_requested = True

    def maybe_ocr(self, now: float | None = None) -> bool:
        """够条件就在后台截一次图认字。返回是否发起了。"""
        if self.level != "ocr" or self._ocr_busy or self._probe_busy or self._own_window_active:
            return False
        w = self.window
        if w is None or w.private:
            if w is not None and w.private:
                self._full_requested = False
                self._popup_ready = False
                self._popup_target = None
            return False
        now = self.clock() if now is None else now
        popup_now = (self._popup_ready and self._popup_target is not None
                     and self._popup_target[0] == w.hwnd and now - self._popup_target[1] <= 5.0)
        if not popup_now:
            self._popup_ready = False
            self._popup_target = None
        forced = self._full_requested or popup_now
        switched = self._window_read_pending
        changed = self.change_read_due(now)
        if not forced and now - self._view_since < OCR_SETTLE_SECONDS:
            return False
        if not forced and not switched and not changed and now - self._last_ocr_at < OCR_MIN_GAP:
            return False
        last = self._ocr.get((w.hwnd, w.title))
        key = (w.hwnd, w.title)
        audit = last is not None and now - self._full_ocr_at.get(key, last[0]) >= 300.0
        interval = self._intervals.get(key, 60.0)
        if (not forced and not switched and not changed and not audit and last is not None
                and now - self._checked_at.get(key, last[0]) < interval):
            return False
        self._full_requested = False
        self._window_read_pending = False
        if changed:
            self._change_ready = None
        self._popup_ready = False
        self._popup_target = None
        self._ocr_busy = True
        self._read_start = self.clock()
        self._last_ocr_at = now
        threading.Thread(target=self._run_ocr, args=(key, w.rect, now, forced or switched or audit),
                         daemon=True).start()
        return True

    def _run_ocr(self, key, rect, started: float, forced: bool = False) -> None:
        try:
            from PIL import ImageChops
            from .attention import TILES, compare_frames, dhash, regions_from_tiles, thumbnail

            overlay_version = self._overlay_version
            try:
                current = foreground()
            except Exception:
                current = None
            if current is not None and current[1] == self.own_pid:
                self._last_ocr_at = -1e9
                return
            left, top, right, bottom = rect
            if right - left < 50 or bottom - top < 50:
                raise RuntimeError("窗口太小")
            from .uia import focus_snapshot

            old = self._ocr.get(key)
            focus = focus_snapshot(key[0], rect)
            focus_text = clean_ocr(focus.text.splitlines()) if focus is not None else ""
            did_ocr = [False]

            def read(crop):
                began = time.perf_counter()
                did_ocr[0] = True
                try:
                    return clean_ocr(ocr_lines(crop))
                finally:
                    self.stats["ocr_s"] += time.perf_counter() - began
                    self.stats["ocr_n"] += 1

            frame = None
            if not forced and old is not None and len(focus_text) >= 20:
                text = focus_text                 # UIA 有字时直接用，不再截图
            else:
                image = self._grab_masked(rect, overlay_version)
                if image is None:
                    self._last_ocr_at = -1e9
                    return
                previous = self._frames.get(key)
                change = compare_frames(previous, image, self._volatility.get(key)) if previous else None
                if change is not None:
                    self._volatility[key] = change.volatility
                frames = dict(self._frames)
                frames[key] = thumbnail(image)
                frame = frames[key]
                self._frames = dict(list(frames.items())[-20:])
                self._probe_frames[key] = frames[key]
                self._volatility = {k: v for k, v in self._volatility.items() if k in self._frames}
                cached = next((text for fingerprint, frame, text in reversed(self._recent_frames.get(key, []))
                               if fingerprint == dhash(frames[key])
                               and ImageChops.difference(frame, frames[key]).getbbox() is None), None)
                if not forced and cached is not None:
                    text = cached
                elif forced or change is None or old is None or len(change.important_tiles) > TILES[0] * TILES[1] // 2:
                    text = read(image)          # 新窗口 / 大片变化：整窗
                elif change.fingerprint == dhash(previous) and not change.changed:
                    text = old[1]               # 相同画面：沿用文字、刷新时间
                else:
                    regions = []
                    if focus is not None:
                        x0, y0, x1, y1 = focus.rect
                        regions = [(x0 - left, y0 - top, x1 - left, y1 - top)]
                    if not regions:
                        regions = regions_from_tiles(change.important_tiles, image.size)
                    text = " / ".join(read(image.crop(box)) for box in regions) if regions else ""
                    if len(text.strip()) < 20:
                        text = read(image)      # 小区域没取到足够文字：整窗兜底
            if focus_text and focus_text not in text:
                text = clean_ocr([focus_text, *text.split(" / ")])
            if self._overlay_version != overlay_version:
                self._last_ocr_at = -1e9
                return
            if old and old[1]:
                before = {_norm_line(line) for line in old[1].split(" / ")}
                if any(n and n not in before for n in map(_norm_line, text.split(" / "))):
                    self._found_at = self.clock()
            table = dict(self._ocr)
            table[key] = (started, text)
            if len(table) > 20:
                for old in sorted(table, key=lambda k: table[k][0])[:-20]:
                    del table[old]
            self._ocr = table
            self._checked_at[key] = started
            if did_ocr[0]:
                self._full_ocr_at[key] = started
            if frame is not None and text:
                recent = list(self._recent_frames.get(key, []))[-3:]
                recent.append((dhash(frame), frame, text))
                self._recent_frames[key] = recent
                if len(self._recent_frames) > 20:
                    self._recent_frames = dict(list(self._recent_frames.items())[-20:])
            self.ocr_version += 1
            self.ocr_error = None
        except Exception as exc:                      # noqa: BLE001
            self.ocr_error = f"{type(exc).__name__}: {exc}"
            table = dict(self._ocr)
            table[key] = (started, "")                # 失败也记一笔，免得每 tick 重试
            self._ocr = table
        finally:
            self._read_end = self.clock()
            self._ocr_busy = False
