from datetime import date

import pytest

from backend.scheduler import (
    card_type_for,
    delete_node,
    get_today_queue,
    prepare_daily_cards,
    select_due,
    set_node_importance,
    submit_feedback,
)

TODAY = date(2026, 11, 13)


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, *args, **kwargs):
        raise AssertionError("不应调用 complete")

    def complete_json(self, *args, **kwargs):
        return self.payload


def add_node(
    conn,
    *,
    node_type="fact",
    title="测试节点",
    content="节点内容",
    importance=0.6,
    status="active",
    step=0,
    interval=1,
    due_at="2026-11-13",
    lapses=0,
):
    cur = conn.execute(
        """
        INSERT INTO node(
            type, title, content, importance, status, source_segment_ids,
            created_at, updated_at, step, interval_days, due_at, reps, lapses
        ) VALUES (?, ?, ?, ?, ?, '[]', '2026-11-01T00:00:00',
                  '2026-11-01T00:00:00', ?, ?, ?, 0, ?)
        """,
        (
            node_type,
            title,
            content,
            importance,
            status,
            step,
            interval,
            due_at,
            lapses,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_select_due_excludes_future_null_and_non_review_types(conn):
    due_id = add_node(conn, title="到期", due_at="2026-11-13")
    add_node(conn, title="未来", due_at="2026-11-20")
    add_node(conn, title="无到期", due_at=None)
    add_node(conn, node_type="day", title="日期", due_at="2026-11-13")
    add_node(conn, node_type="todo", title="待办", due_at="2026-11-13")

    nodes = select_due(conn, TODAY)

    assert [node.node_id for node in nodes] == [due_id]


def test_select_due_orders_by_overdue_priority(conn):
    high = add_node(
        conn,
        title="严重逾期",
        due_at="2026-11-03",
        importance=0.8,
        interval=1,
        lapses=2,
    )
    low = add_node(
        conn,
        title="轻微逾期",
        due_at="2026-11-12",
        importance=0.9,
        interval=7,
    )

    nodes = select_due(conn, TODAY)

    assert [node.node_id for node in nodes] == [high, low]


def test_select_due_respects_limit(conn):
    for index in range(3):
        add_node(conn, title=f"节点{index}")

    assert len(select_due(conn, TODAY, limit=2)) == 2


@pytest.mark.parametrize(
    "step, expected",
    [
        (0, "clue"),
        (2, "clue"),
        (3, "cloze"),
        (4, "scenario"),
        (5, "scenario"),
        (6, "relation"),
    ],
)
def test_card_type_follows_step(step, expected):
    assert card_type_for(step) == expected


def test_queue_uses_local_templates_without_llm(conn):
    node_id = add_node(conn, title="洛必达法则", content="用于求极限")

    cards = get_today_queue(conn, today=TODAY)

    assert len(cards) == 1
    assert cards[0].node_id == node_id
    assert "洛必达法则" in cards[0].question
    assert cards[0].answer == "用于求极限"


def test_queue_uses_llm_generated_card(conn):
    node_id = add_node(conn, title="洛必达法则", content="用于求极限")
    llm = FakeLLM(
        {
            "cards": [
                {
                    "node_id": node_id,
                    "card_type": "cloze",
                    "question": "求极限的法则叫什么？",
                    "answer": "洛必达法则",
                }
            ]
        }
    )

    cards = get_today_queue(conn, llm=llm, today=TODAY)

    assert cards[0].card_type == "cloze"
    assert cards[0].question == "求极限的法则叫什么？"
    assert cards[0].answer == "洛必达法则"


def test_queue_falls_back_when_llm_card_is_invalid(conn):
    node_id = add_node(conn, title="洛必达法则", content="用于求极限")
    llm = FakeLLM({"cards": [{"node_id": node_id, "card_type": "unknown"}]})

    cards = get_today_queue(conn, llm=llm, today=TODAY)

    assert cards[0].card_type == "clue"
    assert cards[0].answer == "用于求极限"


def test_remembered_updates_node_and_writes_log(conn):
    node_id = add_node(conn, step=0, interval=1, due_at="2026-11-13")

    result = submit_feedback(
        conn, node_id, "clue", "remembered", today=TODAY
    )

    assert result.step_before == 0
    assert result.step_after == 1
    assert result.interval_days == 3
    assert result.due_at == "2026-11-16"
    node = conn.execute(
        "SELECT * FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    assert node["step"] == 1
    assert node["reps"] == 1
    assert node["last_reviewed_at"] is not None

    log = conn.execute("SELECT * FROM review_log").fetchone()
    assert log["node_id"] == node_id
    assert log["feedback"] == "remembered"
    assert log["card_type"] == "clue"
    assert log["step_before"] == 0
    assert log["step_after"] == 1


def test_fuzzy_halves_interval_without_advancing_step(conn):
    node_id = add_node(conn, step=2, interval=7, due_at="2026-11-13")

    result = submit_feedback(conn, node_id, "cloze", "fuzzy", today=TODAY)

    assert result.step_after == 2
    assert result.interval_days == 3
    assert result.due_at == "2026-11-16"


def test_forgot_drops_steps_and_counts_lapse(conn):
    node_id = add_node(
        conn, step=3, interval=14, due_at="2026-11-13", lapses=0
    )

    result = submit_feedback(conn, node_id, "cloze", "forgot", today=TODAY)

    assert result.step_after == 1
    assert result.interval_days == 1
    assert result.due_at == "2026-11-14"
    assert result.lapses == 1


def test_unknown_feedback_raises_and_writes_nothing(conn):
    node_id = add_node(conn)

    with pytest.raises(ValueError):
        submit_feedback(conn, node_id, "clue", "nope", today=TODAY)

    assert conn.execute("SELECT COUNT(*) AS c FROM review_log").fetchone()["c"] == 0


def test_missing_node_raises(conn):
    with pytest.raises(ValueError):
        submit_feedback(conn, 999, "clue", "remembered", today=TODAY)


def test_low_importance_clears_due_and_leaves_queue(conn):
    node_id = add_node(conn, importance=0.6, due_at="2026-11-13")

    result = set_node_importance(conn, node_id, 0.2, today=TODAY)

    assert result.reviewable is False
    assert result.due_at is None
    node = conn.execute(
        "SELECT importance, due_at FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    assert node["importance"] == 0.2
    assert node["due_at"] is None
    assert select_due(conn, TODAY) == []


def test_high_importance_schedules_a_non_reviewable_node(conn):
    node_id = add_node(conn, importance=0.2, due_at=None)

    result = set_node_importance(conn, node_id, 0.9, today=TODAY)

    assert result.reviewable is True
    assert result.due_at == TODAY.isoformat()
    assert [n.node_id for n in select_due(conn, TODAY)] == [node_id]


def test_delete_node_cascades_edges_and_daily_cards(conn):
    node_id = add_node(conn, due_at="2026-11-13")
    other_id = add_node(conn, title="另一个节点", due_at="2026-11-20")
    conn.execute(
        "INSERT INTO edge(src, dst, relation, created_at) "
        "VALUES (?, ?, 'related', '2026-11-01T00:00:00')",
        (node_id, other_id),
    )
    prepare_daily_cards(conn, None, TODAY)

    assert delete_node(conn, node_id) is True

    assert conn.execute(
        "SELECT 1 FROM node WHERE id = ?", (node_id,)
    ).fetchone() is None
    assert conn.execute("SELECT COUNT(*) AS c FROM edge").fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM daily_card"
    ).fetchone()["c"] == 0
    assert delete_node(conn, 999) is False


class CountingLLM(FakeLLM):
    def __init__(self, payload):
        super().__init__(payload)
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        return super().complete_json(*args, **kwargs)


class BrokenLLM:
    def complete(self, *args, **kwargs):
        raise AssertionError("不应调用 complete")

    def complete_json(self, *args, **kwargs):
        raise RuntimeError("offline")


def card_payload(node_id, question="云端问题", answer="云端答案"):
    return {
        "cards": [
            {
                "node_id": node_id,
                "card_type": "cloze",
                "question": question,
                "answer": answer,
            }
        ]
    }


def test_prepare_daily_cards_stores_llm_cards(conn):
    node_id = add_node(
        conn, title="洛必达法则", content="用于求极限", due_at="2026-11-14"
    )

    report = prepare_daily_cards(
        conn, FakeLLM(card_payload(node_id)), date(2026, 11, 14)
    )

    assert report.review_date == "2026-11-14"
    assert report.total == 1
    assert report.llm_cards == 1
    row = conn.execute("SELECT * FROM daily_card").fetchone()
    assert row["review_date"] == "2026-11-14"
    assert row["node_id"] == node_id
    assert row["question"] == "云端问题"
    assert row["answer"] == "云端答案"
    assert row["source"] == "llm"
    assert row["step"] == 0


def test_prepare_daily_cards_replaces_existing(conn):
    node_id = add_node(conn, due_at="2026-11-14")
    day = date(2026, 11, 14)

    prepare_daily_cards(
        conn, FakeLLM(card_payload(node_id, "第一版")), day
    )
    prepare_daily_cards(
        conn, FakeLLM(card_payload(node_id, "第二版")), day
    )

    rows = conn.execute("SELECT * FROM daily_card").fetchall()
    assert len(rows) == 1
    assert rows[0]["question"] == "第二版"


def test_prepare_daily_cards_falls_back_on_llm_failure(conn):
    node_id = add_node(
        conn, content="本地答案", due_at="2026-11-14"
    )

    report = prepare_daily_cards(conn, BrokenLLM(), date(2026, 11, 14))

    assert report.llm_cards == 0
    assert report.local_cards == 1
    assert "offline" in report.warning
    row = conn.execute(
        "SELECT * FROM daily_card WHERE node_id = ?", (node_id,)
    ).fetchone()
    assert row["source"] == "local"
    assert row["answer"] == "本地答案"


def test_queue_prefers_cache_and_skips_llm(conn):
    node_id = add_node(conn, due_at="2026-11-13")
    prepare_daily_cards(conn, FakeLLM(card_payload(node_id)), TODAY)
    llm = CountingLLM({"cards": []})

    cards = get_today_queue(conn, llm=llm, today=TODAY)

    assert cards[0].source == "cache"
    assert cards[0].question == "云端问题"
    assert llm.calls == 0


def test_queue_ignores_cache_when_node_changed(conn):
    node_id = add_node(
        conn, content="旧内容", due_at="2026-11-13"
    )
    prepare_daily_cards(conn, FakeLLM(card_payload(node_id)), TODAY)
    conn.execute(
        "UPDATE node SET content = '新内容' WHERE id = ?", (node_id,)
    )
    conn.commit()

    cards = get_today_queue(conn, today=TODAY)

    assert cards[0].source == "local"
    assert cards[0].answer == "新内容"


def test_partial_cache_only_asks_llm_for_missing(conn):
    first = add_node(conn, title="第一题", due_at="2026-11-13")
    second = add_node(conn, title="第二题", due_at="2026-11-13")
    payload = {
        "cards": [
            {
                "node_id": first,
                "card_type": "clue",
                "question": "Q1",
                "answer": "A1",
            },
            {
                "node_id": second,
                "card_type": "clue",
                "question": "Q2",
                "answer": "A2",
            },
        ]
    }
    prepare_daily_cards(conn, FakeLLM(payload), TODAY)
    conn.execute(
        "DELETE FROM daily_card WHERE node_id = ?", (second,)
    )
    conn.commit()
    llm = CountingLLM(
        {
            "cards": [
                {
                    "node_id": second,
                    "card_type": "cloze",
                    "question": "Q2-new",
                    "answer": "A2-new",
                }
            ]
        }
    )

    cards = get_today_queue(conn, llm=llm, today=TODAY)

    by_id = {card.node_id: card for card in cards}
    assert llm.calls == 1
    assert by_id[first].source == "cache"
    assert by_id[first].question == "Q1"
    assert by_id[second].source == "llm"
    assert by_id[second].question == "Q2-new"
