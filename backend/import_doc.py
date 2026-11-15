"""导入 PDF / DOCX / TXT / Markdown，脱敏并整合成记忆节点。

文本 PDF 用 pypdf 提取；扫描版 PDF 目前会提示需要 OCR。
文档按约 4000 字符切块，每块走现有 pipeline（脱敏 → segment → 整合）。
也可以用 --defer 只写入 segment，交给 nightly 统一整合。

用法：
    python -m backend.import_doc notes.pdf
    python -m backend.import_doc notes.pdf slides.docx --date 2026-11-15
    python -m backend.import_doc notes.pdf --defer     # 不调用 LLM
    python -m backend.import_doc notes.pdf --dry-run   # 只提取/切块
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date as Date
from pathlib import Path
from xml.etree import ElementTree as ET

from .config import configure_stdio, get_db_path, get_llm_config
from .db import connect
from .llm import DeepSeekClient
from .pipeline import ingest_text

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".pdf", ".docx"}
DEFAULT_MAX_CHARS = 4000


@dataclass
class ImportReport:
    file: str
    chars: int
    chunks: int
    segments: int = 0
    created: int = 0
    merged: int = 0
    linked: int = 0
    ignored: int = 0
    superseded: int = 0
    deferred: bool = False
    reports: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "chars": self.chars,
            "chunks": self.chunks,
            "segments": self.segments,
            "created": self.created,
            "merged": self.merged,
            "linked": self.linked,
            "ignored": self.ignored,
            "superseded": self.superseded,
            "deferred": self.deferred,
        }


def _read_text_file(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ET.fromstring(xml)
    namespace = {
        "w": "http://schemas.openxmlformats.org/"
        "wordprocessingml/2006/main"
    }
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(
            node.text or ""
            for node in paragraph.findall(".//w:t", namespace)
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError(
                "PDF 已加密，无法导入"
            ) from exc
    pages: list[str] = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(text)
    if not pages:
        raise ValueError(
            "PDF 没有可提取的文本，可能是扫描版；请先 OCR 或使用图片 OCR"
        )
    return "\n\n".join(pages)


def extract_text(path: str | Path) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"不支持的文件类型：{suffix}；支持 "
            f"{', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )
    if suffix in {".txt", ".md", ".markdown"}:
        return _read_text_file(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    raise ValueError(f"不支持的文件类型：{suffix}")


def split_text(
    text: str, max_chars: int = DEFAULT_MAX_CHARS
) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    paragraphs = [
        part.strip()
        for part in re.split(r"\n\s*\n", text)
        if part.strip()
    ]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for paragraph in paragraphs:
        while len(paragraph) > max_chars:
            cut = max(
                paragraph.rfind(mark, 0, max_chars)
                for mark in "。！？.!?"
            )
            if cut < max_chars // 3:
                piece = paragraph[:max_chars].strip()
                paragraph = paragraph[max_chars:].strip()
            else:
                piece = paragraph[: cut + 1].strip()
                paragraph = paragraph[cut + 1 :].strip()
            flush()
            if piece:
                chunks.append(piece)
        if not paragraph:
            continue
        if not current:
            current = paragraph
        elif len(current) + 2 + len(paragraph) <= max_chars:
            current += "\n\n" + paragraph
        else:
            flush()
            current = paragraph
    flush()
    return chunks


def import_file(
    conn,
    llm,
    path: str | Path,
    *,
    date_str: str | None = None,
    known_names=(),
    defer: bool = False,
    max_chars: int = DEFAULT_MAX_CHARS,
    progress=None,
) -> ImportReport:
    path = Path(path)
    text = extract_text(path)
    chunks = split_text(text, max_chars)
    if not chunks:
        raise ValueError(f"{path.name} 没有可导入的文本")
    date_str = date_str or Date.today().isoformat()
    report = ImportReport(
        file=str(path),
        chars=len(text),
        chunks=len(chunks),
        deferred=defer,
    )
    for index, chunk in enumerate(chunks, 1):
        _, chunk_report = ingest_text(
            conn,
            llm,
            chunk,
            date_str=date_str,
            source="import",
            known_names=tuple(known_names),
            consolidate=not defer,
        )
        report.segments += 1
        if chunk_report is not None:
            report.created += chunk_report.created
            report.merged += chunk_report.merged
            report.linked += chunk_report.linked
            report.ignored += chunk_report.ignored
            report.superseded += chunk_report.superseded
            report.reports.append(chunk_report.to_dict())
        if progress is not None:
            progress(index, len(chunks), chunk_report)
    return report


def pick_files() -> list[Path]:
    """打开系统文件选择器，可从任意目录选择文件。"""
    from PyQt6.QtWidgets import QApplication, QFileDialog

    app = QApplication.instance() or QApplication(sys.argv[:1])
    paths, _ = QFileDialog.getOpenFileNames(
        None,
        "选择要导入记忆的文件",
        "",
        "文档 (*.pdf *.docx *.txt *.md *.markdown);;所有文件 (*.*)",
    )
    return [Path(path) for path in paths]


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="AI 记忆增强伙伴 - 导入 PDF/DOCX/TXT/Markdown"
    )
    parser.add_argument(
        "files", nargs="*", type=Path,
        help="任意位置的 PDF/DOCX/TXT/MD 路径",
    )
    parser.add_argument(
        "--pick", action="store_true",
        help="打开文件选择器，从任意目录选择文件",
    )
    parser.add_argument("--db", help="数据库路径，默认 data/memory.db")
    parser.add_argument("--date", help="归入哪一天，默认今天")
    parser.add_argument(
        "--name", action="append", default=[],
        help="需要脱敏的真实姓名，可重复",
    )
    parser.add_argument(
        "--defer", action="store_true",
        help="只写入 segment，交给 nightly 整合（不调用 LLM）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只提取和切块，不写数据库、不调用 LLM",
    )
    parser.add_argument(
        "--max-chars", type=int, default=DEFAULT_MAX_CHARS
    )
    parser.add_argument(
        "--show-text", action="store_true",
        help="dry-run 时打印每块文本",
    )
    args = parser.parse_args(argv)

    files = list(args.files)
    if args.pick:
        files.extend(pick_files())
    if not files:
        print("没有选择文件。")
        return 1

    total_chunks = 0
    for path in files:
        try:
            text = extract_text(path)
            chunks = split_text(text, args.max_chars)
        except Exception as exc:
            print(f"[失败] {path}: {exc}")
            return 1
        print(
            f"{path.name}: {len(text)} 字符，切成 {len(chunks)} 块"
        )
        total_chunks += len(chunks)
        if args.dry_run:
            if args.show_text:
                for index, chunk in enumerate(chunks, 1):
                    print(f"\n--- 第 {index}/{len(chunks)} 块 ---")
                    print(chunk)
            continue

        conn = connect(args.db or get_db_path())
        try:
            llm = (
                None
                if args.defer
                else DeepSeekClient(get_llm_config())
            )

            def progress(index, total, chunk_report):
                status = (
                    "仅写入"
                    if chunk_report is None
                    else (
                        f"新建 {chunk_report.created}，"
                        f"合并 {chunk_report.merged}，"
                        f"连线 {chunk_report.linked}"
                    )
                )
                print(f"  [{index}/{total}] {status}")

            report = import_file(
                conn,
                llm,
                path,
                date_str=args.date,
                known_names=tuple(args.name),
                defer=args.defer,
                max_chars=args.max_chars,
                progress=progress,
            )
        except Exception as exc:
            print(f"[失败] {path}: {exc}")
            return 1
        finally:
            conn.close()

        print(
            f"  完成：{report.segments} 个 segment，"
            f"新建 {report.created}，合并 {report.merged}，"
            f"连线 {report.linked}"
            + (
                "；等待 nightly 整合"
                if report.deferred
                else ""
            )
        )
    print(f"\n共处理 {total_chunks} 块。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
