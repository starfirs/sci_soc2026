"""夜间任务：整合未处理片段，并预生成次日的复习卡片。

用法：
    python -m backend.nightly
    python -m backend.nightly --date 2026-11-13
    python -m backend.nightly --offline
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date as Date, timedelta

from . import memory
from .config import configure_stdio, get_db_path, get_llm_config
from .db import connect
from .llm import DeepSeekClient
from .scheduler import (
    DEFAULT_DAILY_LIMIT,
    PreparationReport,
    prepare_daily_cards,
)


@dataclass
class NightlyReport:
    consolidations: list[dict] = field(default_factory=list)
    preparation: dict | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "consolidations": self.consolidations,
            "preparation": self.preparation,
            "errors": self.errors,
        }


def _unprocessed_dates(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT substr(ts_start, 1, 10) AS day FROM segment "
        "WHERE processed = 0 ORDER BY day"
    ).fetchall()
    return [row["day"] for row in rows]


def run_nightly(
    conn,
    llm,
    *,
    base_date: Date | None = None,
    limit: int = DEFAULT_DAILY_LIMIT,
) -> NightlyReport:
    """整合所有未处理片段，并为 base_date 的次日准备卡片。"""
    base_date = base_date or Date.today()
    report = NightlyReport()

    if llm is None:
        report.errors.append("未配置 LLM，已跳过整合，仅准备本地模板卡片")
    else:
        for date_str in _unprocessed_dates(conn):
            try:
                consolidation = memory.consolidate(conn, llm, date_str)
                report.consolidations.append(consolidation.to_dict())
            except Exception as exc:  # 单日失败不阻断其他日期
                report.errors.append(f"{date_str} 整合失败：{exc}")

    target = base_date + timedelta(days=1)
    try:
        preparation: PreparationReport = prepare_daily_cards(
            conn, llm, target, limit=limit
        )
        report.preparation = preparation.to_dict()
        if preparation.warning:
            report.errors.append(preparation.warning)
    except Exception as exc:
        report.errors.append(f"{target.isoformat()} 出题失败：{exc}")

    return report


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="AI 记忆增强伙伴 - 夜间整合与次日卡片"
    )
    parser.add_argument("--db", help="数据库路径，默认 data/memory.db")
    parser.add_argument(
        "--date",
        help="当晚日期，默认今天；卡片会生成到它的次日（YYYY-MM-DD）",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_DAILY_LIMIT)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="跳过整合，只用本地模板准备次日卡片",
    )
    args = parser.parse_args(argv)

    base_date = Date.fromisoformat(args.date) if args.date else Date.today()
    conn = connect(args.db or get_db_path())
    llm = None if args.offline else DeepSeekClient(get_llm_config())
    report = run_nightly(
        conn, llm, base_date=base_date, limit=args.limit
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
