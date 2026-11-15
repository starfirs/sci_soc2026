"""里程碑 1 入口：粘贴文字 -> 本地脱敏 -> 落库 -> 云端整合 -> 打印报告。

用法：
    python -m backend.pipeline --text "今天高数课学了洛必达法则……"
    python -m backend.pipeline --file notes.txt --date 2026-11-10
    python -m backend.pipeline --list-nodes
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from . import memory, scrub
from .config import configure_stdio, get_db_path, get_llm_config
from .db import connect
from .llm import DeepSeekClient

_KIND_BY_LABEL = {
    "人物": "person",
    "手机": "phone",
    "邮箱": "email",
    "身份证": "id_card",
}


def _kind_of(placeholder: str) -> str:
    for label, kind in _KIND_BY_LABEL.items():
        if placeholder.startswith(f"[{label}"):
            return kind
    return "unknown"


def load_entity_map(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT placeholder, real_value FROM placeholder_map"
    ).fetchall()
    return {row["placeholder"]: row["real_value"] for row in rows}


def save_entity_map(conn, entity_map: dict[str, str]) -> None:
    if not entity_map:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO placeholder_map(placeholder, real_value, kind) "
        "VALUES (?, ?, ?)",
        [(ph, real, _kind_of(ph)) for ph, real in entity_map.items()],
    )
    conn.commit()


def ingest_text(
    conn,
    llm,
    text: str,
    *,
    date_str: str | None = None,
    source: str = "manual",
    ts: str | None = None,
    known_names: tuple[str, ...] = (),
):
    """脱敏、写入一条 segment，然后触发当天整合。"""
    now = datetime.now()
    date_str = date_str or now.date().isoformat()
    ts = ts or now.isoformat(timespec="seconds")
    if not ts.startswith(date_str):
        ts = f"{date_str}T{now.strftime('%H:%M:%S')}"

    masked, new_map = scrub.mask(
        text,
        known_names=known_names,
        existing=load_entity_map(conn),
    )
    save_entity_map(conn, new_map)

    conn.execute(
        "INSERT INTO segment(ts_start, ts_end, text_masked, source) "
        "VALUES (?, ?, ?, ?)",
        (ts, ts, masked, source),
    )
    conn.commit()

    report = memory.consolidate(conn, llm, date_str)
    return masked, report


def _print_nodes(conn) -> None:
    rows = conn.execute(
        "SELECT id, type, title, importance, due_at, status FROM node "
        "ORDER BY id"
    ).fetchall()
    if not rows:
        print("记忆图为空。")
        return
    for row in rows:
        print(
            f"[{row['id']}] {row['type']:<7} {row['title']} "
            f"(importance={row['importance']:.2f}, due={row['due_at']}, "
            f"status={row['status']})"
        )


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="AI 记忆增强伙伴 - 里程碑 1")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="直接粘贴的文字")
    source.add_argument("--file", help="从 UTF-8 文本文件读取")
    parser.add_argument("--date", help="归入哪一天，默认今天（YYYY-MM-DD）")
    parser.add_argument("--db", help="数据库路径，默认 data/memory.db")
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="需要脱敏的真实姓名，可重复传入",
    )
    parser.add_argument(
        "--list-nodes", action="store_true", help="只打印当前记忆图"
    )
    args = parser.parse_args(argv)

    conn = connect(args.db or get_db_path())

    if args.list_nodes:
        _print_nodes(conn)
        return 0

    if not args.text and not args.file:
        parser.error("需要 --text、--file 或 --list-nodes 之一")
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = args.text

    llm = DeepSeekClient(get_llm_config())
    masked, report = ingest_text(
        conn,
        llm,
        text,
        date_str=args.date,
        known_names=tuple(args.name),
    )

    print("=== 脱敏后文本（这是唯一发往云端的文本） ===")
    print(masked)
    print("\n=== 整合报告 ===")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
