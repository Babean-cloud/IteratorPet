"""文档读取与导出：给委托任务执行器用（``executor.py`` 的 ``read_file`` / ``export_document``）。

* **读**：用微软的 markitdown（MIT）把 PDF / PPT / Word / Excel 转成 Markdown 文字，模型就能读课件、论文了。
  只转本地文件（``convert_local``），不给它网址——联网读网页走 ``web.py``，那边有自己的限制。
* **写**：讲义先写成 Markdown。要 docx 时优先用 pandoc（装了的话），没有就用 python-docx 做一份朴素排版
  （标题、列表、粗体、代码块）；pdf 只有 pandoc（还得有排版引擎）才行，没有就老实说做不到。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

DOC_SUFFIXES = frozenset({".pdf", ".pptx", ".docx", ".xlsx", ".xls"})
EXPORT_FORMATS = ("docx", "pdf")


class DocError(RuntimeError):
    """给模型看的失败原因（中文，一句话）。"""


def _ensure_magika() -> None:
    """markitdown 模块顶上 ``import magika``，只拿它按内容猜文件类型。我们总是按真实路径读、扩展名已知，用不上；
    magika 连带 onnxruntime 占发布包约 40 MB，所以打包时不带它（packaging/build.py）。**一律用替身**：
    猜测返回"跳过"，markitdown 就按扩展名走。就算装了真的也不去 import——加载 onnxruntime 要好几秒，
    第一次读文档会白等（并行测试里还会超时）。别处已经导入了真的就不动它。"""
    import sys

    if sys.modules.get("magika") is not None:
        return
    import types

    class Magika:
        def identify_stream(self, stream):
            return types.SimpleNamespace(status="skipped")

        def identify_path(self, path):
            return types.SimpleNamespace(status="skipped")

    stub = types.ModuleType("magika")
    stub.Magika = Magika
    sys.modules["magika"] = stub


def read_document(path: str | Path) -> str:
    """文档 → Markdown 文本。失败抛 ``DocError``。"""
    _ensure_magika()
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise DocError("没装 markitdown，读不了 PDF / Office 文件（pip install markitdown）") from exc
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        with path.open("rb") as handle:                # markitdown 认不出格式时会当纯文本，坏 PDF 要提前拦
            if b"%PDF" not in handle.read(1024):
                raise DocError("这不是有效的 PDF 文件")
    try:
        result = MarkItDown(enable_plugins=False).convert_local(str(path))
    except Exception as exc:                      # noqa: BLE001 —— 坏文件、加密文件、缺依赖都会走到这
        raise DocError(f"这个文件读不出来：{type(exc).__name__}: {str(exc)[:120]}") from exc
    text = (getattr(result, "markdown", None) or getattr(result, "text_content", "") or "").strip()
    if not text:
        raise DocError("文件里没有抽出文字（可能是扫描件或纯图片，需要先 OCR）")
    return text


def find_pandoc() -> str | None:
    return shutil.which("pandoc")


def export(source: Path, fmt: str, pandoc: str | None = None) -> Path:
    """把 Markdown 文件导出成同名的 docx / pdf，返回新文件路径。"""
    if fmt not in EXPORT_FORMATS:
        raise DocError(f"只能导出 {' / '.join(EXPORT_FORMATS)}")
    if not source.is_file():
        raise DocError("源文件不存在")
    target = source.with_suffix("." + fmt)
    pandoc = pandoc if pandoc is not None else find_pandoc()
    if pandoc:
        proc = subprocess.run([pandoc, str(source), "-o", str(target)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            raise DocError(f"pandoc 失败：{(proc.stderr or proc.stdout).strip()[:200]}")
        return target
    if fmt == "pdf":
        raise DocError("导出 pdf 需要装 pandoc（和排版引擎）；现在可以先导出 docx，再用 Word 另存为 pdf")
    markdown_to_docx(source.read_text(encoding="utf-8", errors="replace"), target)
    return target


_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def _runs(paragraph, text: str) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            paragraph.add_run(part[1:-1]).font.name = "Consolas"
        else:
            paragraph.add_run(part)


def markdown_to_docx(markdown: str, target: Path) -> None:
    """没有 pandoc 时的朴素转换：标题 / 有序无序列表 / 代码块 / 粗体 / 行内代码 / 段落。表格按原文放进去。"""
    try:
        import docx
    except ImportError as exc:
        raise DocError("没装 python-docx，也没有 pandoc，导出不了 docx") from exc
    document = docx.Document()
    in_code, code_lines = False, []
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                paragraph = document.add_paragraph()
                paragraph.add_run("\n".join(code_lines)).font.name = "Consolas"
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        text = line.rstrip()
        if not text.strip():
            continue
        heading = re.match(r"(#{1,6})\s+(.*)", text)
        bullet = re.match(r"\s*[-*+]\s+(.*)", text)
        numbered = re.match(r"\s*\d+[.)]\s+(.*)", text)
        if heading:
            document.add_heading(heading.group(2).strip(), level=min(len(heading.group(1)), 4))
        elif bullet:
            _runs(document.add_paragraph(style="List Bullet"), bullet.group(1))
        elif numbered:
            _runs(document.add_paragraph(style="List Number"), numbered.group(1))
        else:
            _runs(document.add_paragraph(), text.strip())
    if code_lines:
        document.add_paragraph().add_run("\n".join(code_lines)).font.name = "Consolas"
    document.save(str(target))
