"""采集文本的共享处理：归一化、敏感行丢弃、脱敏、去重、写入 segment。

屏幕 OCR 和语音转写都走这里，保证两条链路的隐私和入库规则一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from . import scrub

_CJK = "㐀-䶿一-鿿豈-﫿"
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(password|passwd|pwd|密码|口令)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,}){2}\b"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"(?<![\d-])(?:\d[ -]?){13,19}(?![\d-])"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9])"),
)

AUDIO_SOURCES = ("microphone", "system")


@dataclass(frozen=True)
class CaptureIngestResult:
    action: str
    chars: int
    segment_id: int | None = None
    dropped_lines: int = 0
    masked_text: str = ""
    reason: str = ""


def normalize_captured_text(text: str) -> str:
    """去掉中文识别结果里字符之间多余的空格。"""
    text = re.sub(rf"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])", "", text)
    text = re.sub(
        rf"(?<=[{_CJK}])[ \t]+(?=[，。！？；：、）】》])", "", text
    )
    text = re.sub(
        rf"(?<=[，。！？；：、）】》])[ \t]+(?=[{_CJK}])", "", text
    )
    text = re.sub(
        rf"(?<=[（【《])[ \t]+(?=[{_CJK}])", "", text
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def drop_sensitive_lines(text: str) -> tuple[str, int]:
    """整行丢弃疑似密码、密钥、令牌、卡号的内容。"""
    kept: list[str] = []
    dropped = 0
    for line in text.splitlines():
        if not line.strip():
            kept.append("")
            continue
        if any(pattern.search(line) for pattern in _SENSITIVE_PATTERNS):
            dropped += 1
            continue
        kept.append(line)
    return "\n".join(kept), dropped


def text_similarity(a: str, b: str) -> float:
    a = re.sub(r"\s+", "", a)
    b = re.sub(r"\s+", "", b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    grams_a = {a[i : i + 2] for i in range(len(a) - 1)}
    grams_b = {b[i : i + 2] for i in range(len(b) - 1)}
    if not grams_a or not grams_b:
        return 0.0
    return len(grams_a & grams_b) / len(grams_a | grams_b)


def ingest_captured_text(
    conn,
    text: str,
    *,
    source: str,
    ts: str | None = None,
    known_names=(),
    min_chars: int = 20,
    max_chars: int = 4000,
    dedup: bool = True,
    dry_run: bool = False,
) -> CaptureIngestResult:
    """脱敏并写入一条 segment；重复/过短/全敏感则不写。

    source 为 screen 时只和最近屏幕片段去重；麦克风和系统声音互相去重，
    避免同一个声音被录两遍。
    """
    normalized = normalize_captured_text(text)
    filtered, dropped = drop_sensitive_lines(normalized)
    if not filtered.strip():
        return CaptureIngestResult(
            "skipped_sensitive",
            0,
            dropped_lines=dropped,
            reason="全部内容都被判定为敏感",
        )

    rows = conn.execute(
        "SELECT placeholder, real_value, kind FROM placeholder_map"
    ).fetchall()
    existing = {
        row["placeholder"]: row["real_value"] for row in rows
    }
    known = set(known_names)
    known.update(
        row["real_value"] for row in rows if row["kind"] == "person"
    )
    masked, new_map = scrub.mask(
        filtered, known_names=known, existing=existing
    )
    masked = masked.strip()
    if len(masked) > max_chars:
        masked = masked[:max_chars].rstrip()
    if len(masked) < min_chars:
        return CaptureIngestResult(
            "skipped_short",
            len(masked),
            dropped_lines=dropped,
            masked_text=masked,
            reason=f"少于 {min_chars} 字符",
        )

    if dedup:
        dedup_sources = (
            ("screen",) if source == "screen" else AUDIO_SOURCES
        )
        placeholders = ",".join("?" for _ in dedup_sources)
        recent = conn.execute(
            f"SELECT text_masked FROM segment WHERE source IN "
            f"({placeholders}) ORDER BY id DESC LIMIT 5",
            dedup_sources,
        ).fetchall()
        for row in recent:
            if text_similarity(row["text_masked"], masked) >= 0.9:
                return CaptureIngestResult(
                    "skipped_duplicate",
                    len(masked),
                    dropped_lines=dropped,
                    masked_text=masked,
                    reason="与最近采集片段相似",
                )

    if dry_run:
        return CaptureIngestResult(
            "dry_run",
            len(masked),
            dropped_lines=dropped,
            masked_text=masked,
        )

    if new_map:
        from .pipeline import save_entity_map

        save_entity_map(conn, new_map)
    timestamp = ts or datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        "INSERT INTO segment(ts_start, ts_end, text_masked, source) "
        "VALUES (?, ?, ?, ?)",
        (timestamp, timestamp, masked, source),
    )
    conn.commit()
    return CaptureIngestResult(
        "written",
        len(masked),
        int(cursor.lastrowid),
        dropped_lines=dropped,
        masked_text=masked,
    )
