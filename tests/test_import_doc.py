import zipfile
from pathlib import Path

from backend.import_doc import (
    extract_text,
    import_file,
    split_text,
)


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, *args, **kwargs):
        raise AssertionError("不应调用 complete")

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        if self.calls % 2 == 1:
            return {
                "summary": "导入摘要",
                "topics": [],
                "candidates": [
                    {
                        "type": "fact",
                        "title": "导入的知识",
                        "content": "来自导入文件的内容",
                        "importance": 0.7,
                    }
                ],
            }
        return {
            "decisions": [
                {"candidate_index": 0, "action": "create"}
            ]
        }


def _write_pdf(path: Path, text: str) -> None:
    content = (
        f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET"
    ).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        (
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode("ascii")
        data += obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    data += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        data += f"{offset:010d} 00000 n \n".encode("ascii")
    data += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    path.write_bytes(data)


def _write_docx(path: Path, text: str) -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        f"{text}"
        "</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def test_split_text_respects_limit_and_preserves_content():
    text = "第一段。" * 30 + "\n\n" + "第二段。" * 20
    chunks = split_text(text, max_chars=60)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 60 for chunk in chunks)
    assert "第一段" in chunks[0]


def test_extract_text_supports_txt_docx_pdf(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("纯文本记忆内容", encoding="utf-8")
    docx = tmp_path / "notes.docx"
    _write_docx(docx, "Word 记忆内容")
    pdf = tmp_path / "notes.pdf"
    _write_pdf(pdf, "PDF memory content")

    assert "纯文本" in extract_text(txt)
    assert "Word" in extract_text(docx)
    assert "PDF memory" in extract_text(pdf)


def test_import_file_defer_only_writes_segments(conn, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("今天学了洛必达法则，用于求极限。" * 5, encoding="utf-8")

    report = import_file(conn, None, path, defer=True)

    assert report.segments > 0
    assert report.deferred is True
    rows = conn.execute(
        "SELECT source, processed FROM segment"
    ).fetchall()
    assert rows
    assert all(row["source"] == "import" for row in rows)
    assert all(row["processed"] == 0 for row in rows)


def test_import_file_consolidates_with_llm(conn, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text(
        "今天学了洛必达法则，用于求极限。\n\n"
        "项目会议讨论了记忆图的分工。",
        encoding="utf-8",
    )
    llm = FakeLLM()

    report = import_file(conn, llm, path, date_str="2026-11-13")

    assert report.chunks >= 1
    assert report.created >= 1
    assert conn.execute("SELECT COUNT(*) AS c FROM node").fetchone()["c"] >= 1
    assert all(
        row["processed"] == 1
        for row in conn.execute("SELECT processed FROM segment")
    )
