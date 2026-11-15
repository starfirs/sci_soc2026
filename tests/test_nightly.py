import json
from datetime import date

from backend import memory
from backend.nightly import run_nightly
from backend.scheduler import CARD_SYSTEM

BASE = date(2026, 11, 13)


class NightlyLLM:
    def __init__(self):
        self.calls = []

    def complete(self, *args, **kwargs):
        raise AssertionError("不应调用 complete")

    def complete_json(self, system, user, **kwargs):
        self.calls.append(system)
        if system == memory.EXTRACT_SYSTEM:
            return {
                "summary": "当天摘要",
                "topics": [],
                "candidates": [
                    {
                        "type": "fact",
                        "title": "洛必达法则",
                        "content": "求极限的方法",
                        "importance": 0.8,
                    }
                ],
            }
        if system == memory.DECISION_SYSTEM:
            return {
                "decisions": [{"candidate_index": 0, "action": "create"}]
            }
        if system == CARD_SYSTEM:
            payload = json.loads(user)
            return {
                "cards": [
                    {
                        "node_id": item["node_id"],
                        "card_type": item["suggested_card_type"],
                        "question": f"问题{item['node_id']}",
                        "answer": f"答案{item['node_id']}",
                    }
                    for item in payload["cards"]
                ]
            }
        raise AssertionError(f"未预期的 system: {system[:30]}")


def add_segment(conn, text, ts="2026-11-13T20:00:00"):
    conn.execute(
        "INSERT INTO segment(ts_start, ts_end, text_masked, source) "
        "VALUES (?, ?, ?, 'manual')",
        (ts, ts, text),
    )
    conn.commit()


def add_node(conn, *, title="已有节点", content="已有内容", due_at="2026-11-14"):
    cur = conn.execute(
        """
        INSERT INTO node(
            type, title, content, importance, status, source_segment_ids,
            created_at, updated_at, step, interval_days, due_at, reps, lapses
        ) VALUES ('fact', ?, ?, 0.8, 'active', '[]', '2026-11-01T00:00:00',
                  '2026-11-01T00:00:00', 0, 1, ?, 0, 0)
        """,
        (title, content, due_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_nightly_consolidates_then_prepares_next_day(conn):
    add_segment(conn, "今天学了洛必达法则")
    llm = NightlyLLM()

    report = run_nightly(conn, llm, base_date=BASE)

    assert len(report.consolidations) == 1
    assert report.consolidations[0]["created"] == 1
    assert report.preparation["review_date"] == "2026-11-14"
    assert report.preparation["total"] == 1
    assert report.preparation["llm_cards"] == 1
    assert conn.execute(
        "SELECT processed FROM segment"
    ).fetchone()["processed"] == 1

    row = conn.execute(
        "SELECT * FROM daily_card WHERE review_date = '2026-11-14'"
    ).fetchone()
    assert row["source"] == "llm"
    assert row["question"].startswith("问题")


def test_nightly_offline_skips_consolidation_and_uses_local_cards(conn):
    node_id = add_node(conn, due_at="2026-11-14")

    report = run_nightly(conn, None, base_date=BASE)

    assert report.consolidations == []
    assert any("跳过整合" in error for error in report.errors)
    assert report.preparation["local_cards"] == 1
    row = conn.execute(
        "SELECT * FROM daily_card WHERE node_id = ?", (node_id,)
    ).fetchone()
    assert row["source"] == "local"
    assert row["answer"] == "已有内容"


def test_nightly_is_idempotent(conn):
    add_node(conn, due_at="2026-11-14")

    run_nightly(conn, None, base_date=BASE)
    run_nightly(conn, None, base_date=BASE)

    count = conn.execute(
        "SELECT COUNT(*) AS c FROM daily_card WHERE review_date = '2026-11-14'"
    ).fetchone()["c"]
    assert count == 1
