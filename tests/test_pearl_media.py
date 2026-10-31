"""珍珠第二期：图片、网页、音视频变成"文字 + 一张图"，带图的珍珠走看图模型。"""

from __future__ import annotations

import http.server
import io
import json
import shutil
import subprocess
import threading
import time

import pytest

from iterator_pet.brain import pearl_media as M
from iterator_pet.brain.pearls import PearlError, PearlStore

SRT = """1
00:00:01,000 --> 00:00:03,500
第一句台词

2
00:00:05,000 --> 00:00:07,000
<i>第二句</i> 台词
"""


def test_parse_subtitle_formats():
    assert M.parse_subtitles(SRT) == [(1.0, 3.5, "第一句台词"), (5.0, 7.0, "第二句 台词")]
    vtt = "WEBVTT\n\n00:01.000 --> 00:02.000\nhello\n\n01:00:00.500 --> 01:00:01.000\nlate"
    assert M.parse_subtitles(vtt) == [(1.0, 2.0, "hello"), (3600.5, 3601.0, "late")]
    ass = "[Events]\nDialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,{\\i1}你好\\N世界"
    assert M.parse_subtitles(ass) == [(2.0, 4.0, "你好 世界")]
    lrc = "[00:12.00]第一行\n[00:15.50]第二行"
    assert M.parse_subtitles(lrc) == [(12.0, 15.5, "第一行"), (15.5, 20.5, "第二行")]


def test_extract_html_prefers_article_and_drops_chrome():
    html = """<html><head><title>站名 - 文章</title><meta name="description" content="一句话简介">
    <script>var x = "不要";</script></head><body><nav>首页 关于</nav>
    <article><h1>迭代器的日常</h1><p>""" + "五块卵石今天又在算。" * 30 + """</p><ul><li>第一条</li></ul></article>
    <footer>版权所有</footer></body></html>"""
    title, body = M.extract_html(html)
    assert title == "站名 - 文章"
    assert body.startswith("一句话简介") and "迭代器的日常" in body and "第一条" in body
    assert "不要" not in body and "首页" not in body and "版权所有" not in body


def test_pick_times_snaps_to_strongest_cut_in_each_segment():
    times = M.pick_times(90.0, [(5.0, 0.3), (7.0, 0.9), (50.0, 0.5)], cells=9)
    assert len(times) == 9
    assert times[0] == pytest.approx(7.4), "第 1 段挑分数最高的切点，往后挪一点躲转场"
    assert times[1] == pytest.approx(15.0), "没有切点就取段中间"
    assert times[5] == pytest.approx(50.4)


def test_image_pearl_is_downscaled_and_ocr_attached(tmp_path):
    from PIL import Image

    big = tmp_path / "照片.png"
    Image.new("RGBA", (4000, 1000), (255, 0, 0, 128)).save(big)
    draft = M.from_image(big, big.name, str(big), ocr=lambda image, limit: "认出的字")
    assert draft.kind == "image" and draft.image.size == (1300, 325) and draft.image.mode == "RGB"
    assert "4000×1000" in draft.text and "认出的字" in draft.text
    store = PearlStore(tmp_path / "pearls")
    pearl = store.add(draft.title, draft.kind, draft.source, draft.text, image=draft.image)
    assert pearl["image"] and store.image_path(pearl["id"]).suffix == ".jpg"
    store.remove(pearl["id"])
    assert not (tmp_path / "pearls" / f"{pearl['id']}.jpg").exists(), "扔掉珍珠连图一起删"


def test_bad_inputs_explain_themselves(tmp_path):
    with pytest.raises(PearlError, match="找不到"):
        M.prepare(str(tmp_path / "nope.txt"))
    broken = tmp_path / "坏图.png"
    broken.write_bytes(b"not a png")
    with pytest.raises(PearlError, match="打不开"):
        M.prepare(str(broken))
    note = tmp_path / "a.md"
    note.write_text("# 标题\n正文", encoding="utf-8")
    assert M.prepare(str(note)).kind == "text"


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/page":
            body = ("<html><head><meta charset='gbk'><title>中文页</title></head><body><main><p>"
                    + "这是一段正文。" * 20 + "</p></main></body></html>").encode("gbk")
            self._send(200, "text/html", body)
        elif self.path == "/pic.png":
            from PIL import Image

            buffer = io.BytesIO()
            Image.new("RGB", (64, 32), (0, 128, 255)).save(buffer, "PNG")
            self._send(200, "image/png", buffer.getvalue())
        elif self.path == "/blocked":
            self._send(412, "text/html", b"risk control")
        else:
            self._send(200, "text/html", b"<html><body><div id=app></div></body></html>")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_web_pages_images_and_refusals(site, monkeypatch):
    monkeypatch.setattr("iterator_pet.brain.perception.ocr_image", lambda image, limit=0: "")
    page = M.prepare(f"{site}/page")
    assert page.kind == "web" and page.title == "中文页" and "这是一段正文。" in page.text, "按页面声明的 GBK 解码"
    assert page.text.startswith(f"网址：{site}/page")
    picture = M.prepare(f"{site}/pic.png")
    assert picture.kind == "image" and picture.image.size == (64, 32)
    with pytest.raises(PearlError, match="不让程序直接读"):
        M.prepare(f"{site}/blocked")
    with pytest.raises(PearlError, match="几乎没有字"):
        M.prepare(f"{site}/spa")


FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


@pytest.mark.skipif(not FFMPEG, reason="没装 ffmpeg")
def test_video_becomes_storyboard_with_subtitles(tmp_path):
    clip = tmp_path / "演示.mp4"
    # 三段颜色不同的画面（两次硬切）+ 一个音轨，18 秒
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=red:s=320x180:d=6",
                    "-f", "lavfi", "-i", "color=c=green:s=320x180:d=6",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=18",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
                    "-map", "[v]", "-map", "3:a", "-pix_fmt", "yuv420p", str(clip)], check=True, timeout=60)
    (tmp_path / "演示.zh.srt").write_text(SRT, encoding="utf-8")
    draft = M.prepare(str(clip))
    assert draft.kind == "video" and draft.title == "演示"
    assert draft.image.size == (3 * 432 + 8, 3 * 244 + 8), "一张 3×3 分镜（ffmpeg 把高度取成偶数 244）"
    assert "时长 00:18" in draft.text and "台词来源：字幕" in draft.text
    assert "[#1] 00:00–00:02 第一句台词" in draft.text
    top_left = draft.image.getpixel((200, 120))
    bottom_right = draft.image.getpixel((2 * 436 + 200, 2 * 248 + 120))
    assert top_left[0] > 200 and bottom_right[2] > 200, "前面是红的，后面是蓝的"


@pytest.mark.skipif(not FFMPEG, reason="没装 ffmpeg")
def test_audio_reads_tags_and_lyrics(tmp_path):
    song = tmp_path / "歌.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=3",
                    "-metadata", "title=雨世界", "-metadata", "artist=某人", str(song)], check=True, timeout=60)
    (tmp_path / "歌.lrc").write_text("[00:00.50]下雨了\n[00:02.00]回家吧", encoding="utf-8")
    draft = M.prepare(str(song))
    assert draft.kind == "audio" and draft.title == "雨世界" and draft.image is None
    assert "歌手：某人" in draft.text and "下雨了 / 回家吧" in draft.text


def test_maker_runs_in_background():
    gate = threading.Event()

    def slow(source, cfg, text=None):
        gate.wait(2)
        if source == "bad":
            raise PearlError("读不了")
        return M.Draft("t", "note", "x", source)

    maker = M.PearlMaker(prepare_fn=slow)
    maker.start("good", extra=(1, 2))
    maker.start("bad")
    assert maker.poll() == [] and maker.pending == 2, "不阻塞主线程"
    gate.set()
    results = []
    deadline = time.time() + 3
    while len(results) < 2 and time.time() < deadline:
        results += maker.poll()
        time.sleep(0.01)
    assert results[0][0].source == "good" and results[0][2] == (1, 2)
    assert results[1][0] is None and results[1][1] == "读不了" and maker.pending == 0


# -- 带图的珍珠：看图模型 -------------------------------------------------------------
class VisionClient:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def complete(self, messages, model=None, **_):
        self.calls.append((model, messages))
        if model and self.fail:
            raise RuntimeError("HTTP 400: model does not support image")
        return json.dumps({"reading": ["一张红色的图。"], "pearl_summary": "红图"}, ensure_ascii=False), {}


def _read_image_pearl(tmp_path, client):
    from PIL import Image

    from iterator_pet.brain.director import Director
    from iterator_pet.simulation.state import SimulationState

    store = PearlStore(tmp_path / "pearls")
    pearl = store.add("红.png", "image", "红.png", "（一张图）", image=Image.new("RGB", (40, 40), (255, 0, 0)))
    director = Director(client, idle_fn=lambda: 0.0, pearls=store)
    director.vision_model = "deepseek-flash"
    sim = SimulationState((0.0, 0.0, 1138.0, 608.0))
    director.read_pearl(pearl["id"])
    director.tick(sim)
    deadline = time.time() + 2
    while director.busy and time.time() < deadline:
        time.sleep(0.005)
    director.tick(sim)
    return store, pearl


def test_image_pearl_goes_to_vision_model(tmp_path):
    client = VisionClient()
    store, pearl = _read_image_pearl(tmp_path, client)
    model, messages = client.calls[0]
    content = messages[-1]["content"]
    assert model == "deepseek-flash" and isinstance(content, list)
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "附了一张图" in content[0]["text"]
    assert all(isinstance(m["content"], str) for m in messages[:-1]), "图只放在最后一条 user 消息里"
    assert store.get(pearl["id"])["summary"] == "红图"


def test_vision_failure_falls_back_to_text(tmp_path):
    client = VisionClient(fail=True)
    store, pearl = _read_image_pearl(tmp_path, client)
    assert len(client.calls) == 2 and client.calls[1][0] is None
    text = client.calls[1][1][-1]["content"]
    assert isinstance(text, str) and "图没能传过去" in text and "附了一张图" not in text
    assert store.get(pearl["id"])["summary"] == "红图"
