import json
from datetime import date

import pytest

from backend.node_edit import (
    edit_node,
    edit_node_with_llm,
    node_detail,
    update_node,
)
from backend.scheduler import prepare_daily_cards

TODAY = date(2026, 11, 13)


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, *args, **kwargs):
        raise AssertionError("不应调用 complete")

    def complete_json(self, system, user, **kwargs):
        self.calls.append((system, user))
        return self.payload


def add_node(
    conn,
    *,
    title="旧标题",
    content="旧内容",
    importance=0.6,
    due_at="2026-11-13",
):
    cursor = conn.execute(
        """
        INSERT INTO node(
            type, title, content, importance, status, source_segment_ids,
            created_at, updated_at, step, interval_days, due_at, reps, lapses
        ) VALUES ('fact', ?, ?, ?, 'active', '[]', '2026-11-01T00:00:00',
                  '2026-11-01T00:00:00', 0, 1, ?, 0, 0)
        """,
        (title, content, importance, due_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


def test_direct_update_and_cache_invalidation(conn):
    node_id = add_node(conn)
    prepare_daily_cards(conn, None, TODAY)
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM daily_card"
    ).fetchone()["c"] == 1

    result = update_node(
        conn,
        node_id,
        title="新标题",
        content="新内容",
        today=TODAY,
    )

    assert result.changed is True
    row = conn.execute(
        "SELECT * FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    assert row["title"] == "新标题"
    assert row["content"] == "新内容"
    assert row["due_at"] == "2026-11-13"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM daily_card"
    ).fetchone()["c"] == 0


def test_direct_update_masks_sensitive_text(conn):
    conn.execute(
        "INSERT INTO placeholder_map(placeholder, real_value, kind) "
        "VALUES ('[人物1]', '张三', 'person')"
    )
    conn.commit()
    node_id = add_node(conn)

    result = update_node(
        conn,
        node_id,
        content="张三的电话是 13800138000",
        today=TODAY,
    )

    assert "张三" not in result.content
    assert "13800138000" not in result.content
    assert "[人物1]" in result.content
    assert "[手机1]" in result.content


def test_low_importance_clears_due_and_high_reschedules(conn):
    node_id = add_node(conn, due_at="2026-11-13")

    low = update_node(conn, node_id, importance=0.2, today=TODAY)
    assert low.due_at is None

    high = update_node(conn, node_id, importance=0.9, today=TODAY)
    assert high.due_at == TODAY.isoformat()


def test_llm_edit_masks_instruction_and_applies(conn):
    conn.execute(
        "INSERT INTO placeholder_map(placeholder, real_value, kind) "
        "VALUES ('[人物1]', '张三', 'person')"
    )
    conn.commit()
    node_id = add_node(conn)
    llm = FakeLLM(
        {
            "title": "新标题",
            "content": "[人物1]负责记忆整合，电话是 [手机1]。",
            "type": "fact",
            "importance": 0.9,
            "note": "已改写",
        }
    )

    result = edit_node_with_llm(
        conn,
        llm,
        node_id,
        "把张三负责记忆整合、电话 13800138000 补充进去",
        today=TODAY,
    )

    sent = json.loads(llm.calls[0][1])
    assert "张三" not in sent["instruction"]
    assert "13800138000" not in sent["instruction"]
    assert "[人物1]" in sent["instruction"]
    assert "[手机1]" in sent["instruction"]
    assert result.title == "新标题"
    assert result.importance == 0.9
    assert "[人物1]" in result.content
    assert "张三" not in result.content
    assert result.note == "已改写"


def test_edit_node_wrapper_requires_llm_for_instruction(conn):
    node_id = add_node(conn)

    with pytest.raises(ValueError, match="必须传入 llm"):
        edit_node(conn, node_id, instruction="改一下")

    result = edit_node(conn, node_id, title="直接改")
    assert result.title == "直接改"


def test_node_detail_includes_edges(conn):
    node_id = add_node(conn)
    other = add_node(conn, title="另一个")
    conn.execute(
        "INSERT INTO edge(src, dst, relation, created_at) "
        "VALUES (?, ?, 'related', '2026-11-01T00:00:00')",
        (node_id, other),
    )
    conn.commit()

    detail = node_detail(conn, node_id)

    assert detail["title"] == "旧标题"
    assert detail["edges_out"][0]["dst"] == other
