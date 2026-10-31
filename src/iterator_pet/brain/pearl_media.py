"""珍珠第二期：图片、网页、音视频 → "一段文字 + 最多一张图"，再交给它读。

**阻塞、慢**（截帧、下载、认字），只在工作线程里跑（``PearlMaker``）。思路：

* **图片**：缩到看图模型的尺寸（每张图最多约 1000 token），同时本地认一遍字；模型看不了图就只读字。
* **网页**：标准库抓一次（只抓用户亲手递来的链接），去掉导航 / 脚本，取正文；链接指向图片就当图片。
  视频网站的链接（B 站、YouTube……）装了 yt-dlp 才交给它下低清版本 + 字幕，没装就只读网页；
  网站拦程序访问（风控、要登录）就直说读不了，不绕。
* **视频**：思路照 dsh-video-storyboard（MIT）——字幕优先（同名字幕文件 → 内嵌字幕 → 配了语音识别才转写），
  画面压成**一张** 3×3 分镜：时长切 9 段，每段挑镜头切换最明显处的一帧，左上角标时间，
  每格配那一段的字幕。一张图顶得上几十帧，token 省一个数量级。
* **音频**：标签（标题 / 歌手 / 专辑 / 时长）+ 同名歌词 / 字幕，配了语音识别就转写。

外部工具都是可选的：ffmpeg / ffprobe（音视频）、yt-dlp（视频网站链接），在 PATH 里找或在 config 里指定。
"""

from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from .pearls import STORE_LIMIT, PearlError, _decode, read_file

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".ts"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
SUBTITLE_SUFFIXES = (".srt", ".vtt", ".ass", ".ssa", ".lrc")
VIDEO_SITES = ("bilibili.com/video", "b23.tv", "youtube.com/watch", "youtu.be/", "youtube.com/shorts",
               "v.qq.com", "ixigua.com", "douyin.com", "vimeo.com")
VISION_SIDE = 1300               # 看图模型会把图缩到约 1300×1300 的像素量：再大是白传
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_MEDIA_BYTES = 4 * 1024 * 1024 * 1024
GRID = 3                         # 分镜 3×3
CELL_WIDTH = 432
CELL_TEXT = 120                  # 每格配的字幕最多几个字
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) IteratorPet/0.4 (reading a page the user handed over)"


@dataclass
class Draft:
    title: str
    kind: str
    text: str
    source: str
    image: object = None          # PIL.Image 或 None


def media_kind(path) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return None


def is_url(text: str) -> bool:
    text = (text or "").strip()
    return bool(re.fullmatch(r"https?://\S+", text)) and len(text) < 2000


def find_tool(name: str, configured: str = "") -> str | None:
    if configured and Path(configured).is_file():
        return configured
    return shutil.which(name)


def _run(args, timeout: float, binary: bool = False) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags,
                          **({} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}))


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60:02d}:{rest % 60:02d}"


# -- 图片 ----------------------------------------------------------------------------
def fit(image, side: int = VISION_SIDE):
    """转 RGB（透明底垫黑）、等比缩到不超过 ``side``。"""
    from PIL import Image

    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        base = Image.new("RGB", image.size, (24, 24, 24))
        base.paste(image, mask=image.split()[-1])
        image = base
    else:
        image = image.convert("RGB")
    image.thumbnail((side, side))
    return image


def from_image(source, title: str, origin: str, ocr=None) -> Draft:
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(source)
        image.seek(0)                                   # GIF 只看第一帧
        width, height = image.size
        image = fit(image)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PearlError("这张图打不开") from exc
    if ocr is None:
        from .perception import ocr_image as ocr
    words = ocr(image, limit=3000) if ocr else ""
    text = f"（一张图，{width}×{height}）\n本地认出的字：{words or '（没认出字）'}"
    return Draft(title, "image", text, origin, image)


# -- 网页 ----------------------------------------------------------------------------
class _Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "button",
            "iframe", "template"}
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "td", "th", "dd", "dt",
             "figcaption", "br", "div", "section", "article", "main", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self._skip = 0
        self._in_title = False
        self._focus = 0                 # 在 <article> / <main> 里
        self.all: list[str] = []
        self.focus: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in ("og:title", "description", "og:description") and attrs.get("content"):
                self.meta[key] = attrs["content"].strip()
        elif tag in ("article", "main"):
            self._focus += 1
        if tag in self.BLOCK:
            self._newline()

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("article", "main") and self._focus:
            self._focus -= 1
        if tag in self.BLOCK:
            self._newline()

    def _newline(self):
        for bucket in (self.all, self.focus):
            if bucket and bucket[-1] != "\n":
                bucket.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip or not data.strip():
            return
        piece = re.sub(r"\s+", " ", data)
        self.all.append(piece)
        if self._focus:
            self.focus.append(piece)

    def text(self) -> str:
        body = "".join(self.focus)
        if len(body.strip()) < 200:
            body = "".join(self.all)
        lines = [line.strip() for line in body.split("\n")]
        return "\n".join(line for line in lines if len(line) >= 2)


def extract_html(html: str) -> tuple[str, str]:
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:                                   # noqa: BLE001 —— 坏 HTML 也尽量读
        pass
    title = (parser.meta.get("og:title") or parser.title or "").strip()
    body = parser.text()
    description = parser.meta.get("description") or parser.meta.get("og:description") or ""
    if description and description not in body[:500]:
        body = f"{description}\n\n{body}"
    return re.sub(r"\s+", " ", title)[:80], body


def fetch(url: str, limit: int = MAX_PAGE_BYTES, timeout: float = 15.0) -> tuple[bytes, str, str]:
    """(内容, Content-Type, 最终网址)。超过 ``limit`` 抛错。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read(limit + 1)
            final = resp.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 412, 429):
            raise PearlError(f"这个网站不让程序直接读（HTTP {exc.code}，要登录或者被风控拦了）。"
                             "可以把正文复制给它，或者存成文件再递") from exc
        raise PearlError(f"打不开（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise PearlError(f"打不开这个链接：{getattr(exc, 'reason', exc)}") from exc
    if len(data) > limit:
        raise PearlError(f"太大了（超过 {limit // 1024 // 1024} MB）")
    return data, ctype, final


def _charset(ctype: str, data: bytes) -> str:
    match = re.search(r"charset=([\w-]+)", ctype, re.I) or re.search(rb'charset=["\']?([\w-]+)', data[:4096], re.I)
    if match:
        name = match.group(1)
        return name.decode("ascii", "replace") if isinstance(name, bytes) else name
    return ""


def from_url(url: str, cfg=None) -> Draft:
    lowered = url.lower()
    if any(site in lowered for site in VIDEO_SITES):
        ytdlp = find_tool("yt-dlp", getattr(cfg, "ytdlp_path", ""))
        if ytdlp and find_tool("ffmpeg", getattr(cfg, "ffmpeg_path", "")):
            return from_video_site(url, ytdlp, cfg)
        hint = "（这是视频网站的链接。装了 yt-dlp 和 ffmpeg 才看得到画面和字幕，现在只读网页上的字）"
    else:
        hint = ""
    data, ctype, final = fetch(url, limit=MAX_IMAGE_BYTES if re.search(r"\.(png|jpe?g|gif|webp|bmp)(\?|$)", lowered)
                               else MAX_PAGE_BYTES)
    name = Path(urllib.parse.urlparse(final).path).name or urllib.parse.urlparse(final).netloc
    if ctype.startswith("image/"):
        return from_image(io.BytesIO(data), name[:60], url)
    if "html" in ctype or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        charset = _charset(ctype, data)
        try:
            html = data.decode(charset) if charset else _decode(data)
        except (LookupError, UnicodeDecodeError):
            html = _decode(data)
        title, body = extract_html(html)
        if len(body.strip()) < 40:
            raise PearlError("网页里几乎没有字（可能要登录，或者内容是脚本画出来的）")
    elif ctype.startswith(("text/", "application/json")):
        title, body = name, _decode(data)
    else:
        raise PearlError(f"这个链接是 {ctype.split(';')[0] or '未知类型'}，读不了")
    head = f"网址：{url}\n{hint}\n" if hint else f"网址：{url}\n"
    return Draft(title or urllib.parse.urlparse(final).netloc, "web", head + "\n" + body, url)


# -- 字幕 ----------------------------------------------------------------------------
def _seconds(stamp: str) -> float:
    parts = re.split(r"[:]", stamp.replace(",", "."))
    try:
        value = 0.0
        for part in parts:
            value = value * 60 + float(part)
        return value
    except ValueError:
        return 0.0


def parse_subtitles(text: str) -> list[tuple[float, float, str]]:
    """SRT / VTT / ASS / LRC → [(开始秒, 结束秒, 文字)]。"""
    cues: list[tuple[float, float, str]] = []
    if "[Events]" in text or "Dialogue:" in text:                      # ASS / SSA
        for line in text.splitlines():
            if line.startswith("Dialogue:"):
                fields = line.split(",", 9)
                if len(fields) == 10:
                    words = re.sub(r"\{[^}]*\}", "", fields[9]).replace("\\N", " ").strip()
                    if words:
                        cues.append((_seconds(fields[1]), _seconds(fields[2]), words))
        return cues
    lrc = re.findall(r"^\[(\d+:\d+(?:\.\d+)?)\](.*)$", text, re.M)
    if lrc and "-->" not in text:                                      # LRC 歌词
        for index, (stamp, words) in enumerate(lrc):
            start = _seconds(stamp)
            end = _seconds(lrc[index + 1][0]) if index + 1 < len(lrc) else start + 5
            if words.strip():
                cues.append((start, end, words.strip()))
        return cues
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):         # SRT / VTT
        match = re.search(r"(\d*:?\d+:\d+[.,]\d+)\s*-->\s*(\d*:?\d+:\d+[.,]\d+)", block)
        if not match:
            continue
        words = " ".join(line.strip() for line in block[match.end():].splitlines() if line.strip())
        words = re.sub(r"<[^>]+>", "", words)
        if words:
            cues.append((_seconds(match.group(1)), _seconds(match.group(2)), words))
    return cues


def sidecar_subtitles(path: Path) -> list[tuple[float, float, str]]:
    """同名字幕：``片名.srt``、``片名.zh-CN.srt``、``片名.lrc`` ……中文优先。"""
    candidates = [p for p in path.parent.glob(f"{glob_escape(path.stem)}*")
                  if p.suffix.lower() in SUBTITLE_SUFFIXES and p.is_file()]
    candidates.sort(key=lambda p: (not re.search(r"zh|chs|cn|中", p.name, re.I), len(p.name)))
    for candidate in candidates:
        try:
            cues = parse_subtitles(_decode(candidate.read_bytes()[:4 * 1024 * 1024]))
        except OSError:
            continue
        if cues:
            return cues
    return []


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


# -- ffmpeg --------------------------------------------------------------------------
def probe(path: Path, ffprobe: str) -> dict:
    proc = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        data = {}
    if not data.get("format"):
        raise PearlError("ffprobe 读不出这个文件（坏了，或者不是音视频）")
    return data


def embedded_subtitles(path: Path, ffmpeg: str, info: dict) -> list[tuple[float, float, str]]:
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"
               and s.get("codec_name") not in ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle")]
    if not streams:
        return []
    streams.sort(key=lambda s: not re.search(r"zh|chi|chs|cn", json.dumps(s.get("tags", {})), re.I))
    try:
        proc = _run([ffmpeg, "-v", "error", "-i", str(path), "-map", f"0:{streams[0]['index']}",
                     "-f", "srt", "pipe:1"], 120)
    except subprocess.TimeoutExpired:
        return []
    return parse_subtitles(proc.stdout or "")


def scene_cuts(path: Path, ffmpeg: str) -> list[tuple[float, float]]:
    """[(时间, 镜头切换分数 0~1)]：**只解关键帧**（``-skip_frame nokey``），缩到 160 宽比相邻两帧。

    原来逐帧全解：合成的测试视频几秒就完，用户真实的 1080p 视频要好几分钟，超时整颗珍珠就读不了。
    编码器本来就爱在镜头切换处插关键帧，只看关键帧快两个数量级，挑分镜够用。超时就当没找到切点（均匀取）。
    """
    try:
        proc = _run([ffmpeg, "-v", "error", "-skip_frame", "nokey", "-i", str(path), "-an", "-sn", "-dn",
                     "-vf", "scale=160:-2,select='gt(scene,0.2)',metadata=print:file=-",
                     "-fps_mode", "passthrough", "-f", "null", "-"], 90)
    except subprocess.TimeoutExpired:
        return []
    cuts, time = [], None
    for line in (proc.stdout or "").splitlines():
        match = re.search(r"pts_time:([\d.]+)", line)
        if match:
            time = float(match.group(1))
        match = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if match and time is not None:
            cuts.append((time, float(match.group(1))))
    return cuts


def pick_times(duration: float, cuts: list[tuple[float, float]], cells: int = GRID * GRID) -> list[float]:
    """时长切 ``cells`` 段，每段挑切换最明显的那个镜头（切点后 0.4 秒，躲开转场），没有就取段中间。"""
    duration = max(duration, 0.1)
    times = []
    for index in range(cells):
        start, end = duration * index / cells, duration * (index + 1) / cells
        inside = [c for c in cuts if start <= c[0] < end - 0.5]
        if inside:
            times.append(min(max(inside, key=lambda c: c[1])[0] + 0.4, end - 0.1))
        else:
            times.append((start + end) / 2)
    return times


def grab_frame(path: Path, ffmpeg: str, at: float, width: int = CELL_WIDTH):
    from PIL import Image

    try:
        proc = _run([ffmpeg, "-v", "error", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1",
                     "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "pipe:1"], 60, binary=True)
    except subprocess.TimeoutExpired:
        return None
    if not proc.stdout:
        return None
    try:
        return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
    except OSError:
        return None


def storyboard(frames: list, times: list[float], width: int = CELL_WIDTH):
    """3×3 拼图，每格左上角标时间。"""
    from PIL import Image, ImageDraw

    sample = next((f for f in frames if f is not None), None)
    height = sample.height if sample is not None else width * 9 // 16
    gap = 4
    sheet = Image.new("RGB", (GRID * width + (GRID - 1) * gap, GRID * height + (GRID - 1) * gap), (12, 12, 12))
    draw = ImageDraw.Draw(sheet)
    for index, (frame, at) in enumerate(zip(frames, times)):
        x, y = (index % GRID) * (width + gap), (index // GRID) * (height + gap)
        if frame is not None:
            sheet.paste(frame.resize((width, height)), (x, y))
        label = f"#{index + 1} | {_clock(at)}"
        draw.rectangle((x, y, x + 8 + 7 * len(label), y + 16), fill=(0, 0, 0))
        draw.text((x + 4, y + 2), label, fill=(255, 220, 120))
    return sheet


def transcribe(path: Path, ffmpeg: str, cfg) -> list[tuple[float, float, str]]:
    """配了语音识别才用：抽成 16 kHz 单声道 mp3（最多前 30 分钟），POST 到 /audio/transcriptions。"""
    base, model, key = (getattr(cfg, "asr_base_url", ""), getattr(cfg, "asr_model", ""),
                        getattr(cfg, "asr_api_key", ""))
    if not (base and model):
        return []
    fd, name = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    audio = Path(name)
    try:
        _run([ffmpeg, "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
              "-t", "1800", str(audio)], 300)
        payload = audio.read_bytes()
        if not payload:
            return []
        boundary = "----iterpet" + os.urandom(8).hex()
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.mp3\"\r\n"
            f"Content-Type: audio/mpeg\r\n\r\n".encode(), payload, f"\r\n--{boundary}--\r\n".encode()])
        request = urllib.request.Request(base.rstrip("/") + "/audio/transcriptions", data=body, method="POST",
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                                  **({"Authorization": f"Bearer {key}"} if key else {})})
        with urllib.request.urlopen(request, timeout=300) as resp:
            text = json.loads(resp.read().decode("utf-8")).get("text", "")
        return [(0.0, 1800.0, text.strip())] if text.strip() else []
    except (OSError, ValueError, urllib.error.URLError):
        return []
    finally:
        try:
            audio.unlink()
        except OSError:
            pass


def _cue_text(cues, start: float, end: float, limit: int) -> str:
    words = " ".join(c[2] for c in cues if c[1] > start and c[0] < end)
    return words[:limit] + ("…" if len(words) > limit else "")


def _duration(info: dict, stream: dict | None) -> float:
    """容器里的时长；webm / 部分 mkv 只把时长写在流上（或者流的 DURATION 标签里）。"""
    for value in (info.get("format", {}).get("duration"), (stream or {}).get("duration"),
                  ((stream or {}).get("tags") or {}).get("DURATION")):
        if value:
            seconds = _seconds(str(value)) if ":" in str(value) else float(value)
            if seconds > 0:
                return seconds
    return 0.0


def from_video(path: Path, cfg=None, title: str | None = None, intro: str = "", cues=None) -> Draft:
    ffmpeg = find_tool("ffmpeg", getattr(cfg, "ffmpeg_path", ""))
    ffprobe = find_tool("ffprobe", str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg else "")
    if not (ffmpeg and ffprobe):
        raise PearlError("看视频要装 ffmpeg（含 ffprobe），装好后放进 PATH，或在 config.json 的 ffmpeg_path 指定")
    if path.stat().st_size > MAX_MEDIA_BYTES:
        raise PearlError("视频太大了（上限 4 GB）")
    info = probe(path, ffprobe)
    # 封面图（mp3 / mkv 里的 attached_pic）也算 video 流，不是画面
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    duration = _duration(info, video)
    if video is None or duration <= 0:
        return from_audio(path, cfg, info=info)
    source_note = "字幕"
    if cues is None:
        cues = sidecar_subtitles(path) or embedded_subtitles(path, ffmpeg, info)
    if not cues:
        cues = transcribe(path, ffmpeg, cfg)
        source_note = "语音识别" if cues else "无"
    cuts = scene_cuts(path, ffmpeg)
    times = pick_times(duration, cuts)
    frames = [grab_frame(path, ffmpeg, at) for at in times]
    if not any(frames):
        raise PearlError("一帧画面都截不出来")
    sheet = storyboard(frames, times)
    cells = GRID * GRID
    lines = [f"（视频，时长 {_clock(duration)}，{video.get('width')}×{video.get('height')}，"
             f"附图是 {GRID}×{GRID} 分镜：时长切成 {cells} 段，每格标了序号和时间；台词来源：{source_note}）"]
    if intro:
        lines.append(intro)
    for index in range(cells):
        start, end = duration * index / cells, duration * (index + 1) / cells
        said = _cue_text(cues, start, end, CELL_TEXT) if cues else ""
        lines.append(f"[#{index + 1}] {_clock(start)}–{_clock(end)} {said or '（没台词）'}")
    if cues:
        full = " ".join(c[2] for c in cues)
        lines.append(f"\n全部台词：{full[:STORE_LIMIT - 3000]}")
    return Draft(title or path.stem, "video", "\n".join(lines), str(path), sheet)


def from_audio(path: Path, cfg=None, info: dict | None = None) -> Draft:
    ffmpeg = find_tool("ffmpeg", getattr(cfg, "ffmpeg_path", ""))
    ffprobe = find_tool("ffprobe", str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg else "")
    tags: dict = {}
    duration = 0.0
    if info is None and ffprobe:
        info = probe(path, ffprobe)
    if info:
        duration = _duration(info, next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None))
        tags = {k.lower(): v for k, v in (info["format"].get("tags") or {}).items()}
    cues = sidecar_subtitles(path)
    source_note = "同名歌词 / 字幕" if cues else "无"
    if not cues and ffmpeg:
        cues = transcribe(path, ffmpeg, cfg)
        source_note = "语音识别" if cues else "无"
    meta = "，".join(f"{label}：{tags[key]}" for key, label in
                    (("title", "标题"), ("artist", "歌手"), ("album", "专辑"), ("date", "年份"), ("genre", "类型"))
                    if tags.get(key))
    lines = [f"（音频，时长 {_clock(duration)}{'，' + meta if meta else ''}；文字来源：{source_note}）"]
    if cues:
        lines.append(" / ".join(c[2] for c in cues)[:STORE_LIMIT - 500])
    else:
        lines.append("没有歌词或字幕，也没配语音识别（config.json 的 asr_*），只知道上面这些。")
    return Draft(tags.get("title") or path.stem, "audio", "\n".join(lines), str(path))


def from_video_site(url: str, ytdlp: str, cfg=None) -> Draft:
    """视频网站链接：交给用户装的 yt-dlp 下一个低清版本 + 字幕（它自己处理各家网站），再按本地视频读。"""
    work = Path(tempfile.mkdtemp(prefix="iterpet_video_"))
    # 只要画面：分镜不需要声音。B 站 / YouTube 的清晰版本都是音画分开的（DASH），原来写的
    # "worst" 要求音画合一，B 站根本没有这种格式 → 下载直接失败。纯视频流挑 ≤480p 里最好的；
    # 配了语音识别（没字幕时要转写）才连音频一起下，交给 ffmpeg 合并。
    if getattr(cfg, "asr_base_url", "") and getattr(cfg, "asr_model", ""):
        fmt = "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b"
    else:
        fmt = "bv*[height<=480]/wv*/b[height<=480]/b"
    ffmpeg = find_tool("ffmpeg", getattr(cfg, "ffmpeg_path", ""))
    try:
        proc = _run([ytdlp, "--no-playlist", "--no-progress", "-f", fmt,
                     *(["--ffmpeg-location", ffmpeg] if ffmpeg else []),
                     "--max-filesize", "500M", "--write-subs", "--write-auto-subs",
                     "--sub-langs", "zh.*,ai-zh,en.*", "--convert-subs", "srt", "--write-info-json",
                     "-o", str(work / "v.%(ext)s"), url], 600)
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise PearlError("yt-dlp 下了 10 分钟还没下完，算了") from exc
    try:
        meta = {}
        for info_file in work.glob("*.info.json"):
            try:
                meta = json.loads(info_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        videos = [p for p in work.iterdir() if p.suffix.lower() in VIDEO_SUFFIXES]
        if not videos:
            reason = (proc.stderr or "").strip().splitlines()[-1:] or ["没下下来"]
            raise PearlError(f"yt-dlp 没拿到视频：{reason[0][:120]}")
        cues: list = []
        for sub in sorted(work.glob("*.srt"), key=lambda p: not re.search(r"zh", p.name)):
            cues = parse_subtitles(_decode(sub.read_bytes()))
            if cues:
                break
        intro = "\n".join(filter(None, [
            f"网址：{url}",
            f"上传者：{meta.get('uploader')}" if meta.get("uploader") else "",
            f"简介：{str(meta.get('description') or '')[:600]}" if meta.get("description") else ""]))
        draft = from_video(videos[0], cfg, title=str(meta.get("title") or url)[:60], intro=intro, cues=cues or None)
        draft.source = url
        return draft
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prepare(source: str, cfg=None, text: str | None = None) -> Draft:
    """文件路径 / 网址 / 一段文字 → Draft。慢，工作线程里调。"""
    if text is not None:
        from .pearls import from_text

        title, kind, body = from_text(text)
        return Draft(title, kind, body, "直接递来的文字")
    if is_url(source):
        return from_url(source.strip(), cfg)
    path = Path(source)
    if path.suffix.lower() == ".lnk" or path.is_dir() or not path.is_file():
        title, kind, body = read_file(path)              # 统一由它报"快捷方式 / 文件夹 / 找不到"
    kind = media_kind(path)
    if kind == "image":
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise PearlError("图太大了（上限 30 MB）")
        return from_image(path, path.name, str(path))
    if kind == "video":
        return from_video(path, cfg)
    if kind == "audio":
        return from_audio(path, cfg)
    title, kind, body = read_file(path)
    return Draft(title, kind, body, str(path))


class PearlMaker:
    """后台一颗一颗做珍珠：``start`` 排队，主线程每帧 ``poll`` 拿做好的 (Draft | None, 错误, 附带)。"""

    def __init__(self, cfg=None, prepare_fn=prepare):
        self.cfg = cfg
        self.prepare_fn = prepare_fn
        self._jobs: queue.Queue = queue.Queue()
        self._done: queue.Queue = queue.Queue()
        self.pending = 0
        self._thread: threading.Thread | None = None

    def start(self, source: str | None = None, text: str | None = None, extra=None) -> None:
        self.pending += 1
        self._jobs.put((source, text, extra))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._work, daemon=True)
            self._thread.start()

    def _work(self) -> None:
        while True:
            try:
                source, text, extra = self._jobs.get(timeout=1.0)
            except queue.Empty:
                return
            try:
                draft = self.prepare_fn(source or "", self.cfg, text=text) if text is not None \
                    else self.prepare_fn(source, self.cfg)
                self._done.put((draft, None, extra))
            except PearlError as exc:
                self._done.put((None, str(exc), extra))
            except Exception as exc:                       # noqa: BLE001
                self._done.put((None, f"{type(exc).__name__}: {exc}", extra))

    def poll(self) -> list:
        out = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                break
        self.pending -= len(out)
        return out
