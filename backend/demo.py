"""生成一个用于试玩桌宠的本地演示库。

只写 data/demo_start.db，不会碰真实 data/memory.db。
每次运行都会重建演示库，并预生成今天的 3 张卡片。
"""
from __future__ import annotations

from datetime import date, datetime

from .config import ROOT_DIR, configure_stdio
from .db import connect
from .scheduler import prepare_daily_cards

DEMO_DB = ROOT_DIR / "data" / "demo_start.db"

DEMO_NODES = [
    (
        "concept",
        "洛必达法则",
        "当极限是 0/0 或 ∞/∞ 型时，对分子分母分别求导后再求极限。",
        0.9,
        "洛必达法则适用于哪两种极限形式？具体怎么做？",
        "适用于 0/0 型和 ∞/∞ 型；对分子分母分别求导后再求极限。",
    ),
    (
        "fact",
        "里程碑一分工",
        "里程碑一优先实现 SQLite 记忆图和本地脱敏；记忆整合模块由[人物1]负责。",
        0.85,
        "里程碑一优先实现哪两个部分？记忆整合模块由谁负责？",
        "优先实现 SQLite 记忆图和本地脱敏；记忆整合由[人物1]负责。",
    ),
    (
        "concept",
        "番茄工作法",
        "每 25 分钟专注工作，休息 5 分钟；四个番茄钟后休息更久。",
        0.75,
        "番茄工作法的基本节奏是什么？",
        "25 分钟专注 + 5 分钟休息；四个番茄钟后休息更久。",
    ),
]


def main() -> int:
    configure_stdio()
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    if DEMO_DB.exists():
        DEMO_DB.unlink()

    conn = connect(DEMO_DB)
    today = date.today()
    now = datetime.now().isoformat(timespec="seconds")
    node_ids: list[tuple[int, str, str]] = []
    try:
        for (
            node_type,
            title,
            content,
            importance,
            question,
            answer,
        ) in DEMO_NODES:
            cursor = conn.execute(
                """
                INSERT INTO node(
                    type, title, content, importance, status,
                    source_segment_ids, created_at, updated_at,
                    step, interval_days, due_at, reps, lapses
                ) VALUES (?, ?, ?, ?, 'active', '[]', ?, ?, 0, 1, ?, 0, 0)
                """,
                (
                    node_type,
                    title,
                    content,
                    importance,
                    now,
                    now,
                    today.isoformat(),
                ),
            )
            node_ids.append(
                (int(cursor.lastrowid), question, answer)
            )
        conn.commit()

        # 先按正常流程写入 daily_card（包含 step/content_hash），
        # 再把模板题替换成更自然的演示题。
        prepare_daily_cards(conn, None, today, limit=8)
        for node_id, question, answer in node_ids:
            conn.execute(
                "UPDATE daily_card SET question = ?, answer = ? "
                "WHERE review_date = ? AND node_id = ?",
                (question, answer, today.isoformat(), node_id),
            )
        conn.commit()
    finally:
        conn.close()

    print(f"演示库已生成：{DEMO_DB}")
    print(f"今天的卡片：{len(DEMO_NODES)} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
