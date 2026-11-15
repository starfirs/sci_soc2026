import pytest

from backend import memory


class FakeLLM:
    def __init__(self, extraction, decisions=None):
        self.extraction = extraction
        self.decisions = decisions
        self.calls = []

    def complete(self, *args, **kwargs):
        raise AssertionError("不应调用 complete")

    def complete_json(self, system, user, **kwargs):
        self.calls.append((system, user))
        return self.extraction if len(self.calls) == 1 else self.decisions


class ExplodingLLM:
    def complete(self, *args, **kwargs):
        raise AssertionError("不应调用 complete")

    def complete_json(self, *args, **kwargs):
        raise RuntimeError("boom")


def extraction(*candidates, summary="当天摘要", topics=None):
    return {
        "summary": summary,
        "topics": topics or [],
        "candidates": list(candidates),
    }


def make_candidate(
    node_type="fact",
    title="洛必达法则",
    content="求极限的方法",
    importance=0.8,
):
    return {
        "type": node_type,
        "title": title,
        "content": content,
        "importance": importance,
    }


def decision(candidate_index=0, action="create", **kwargs):
    item = {"candidate_index": candidate_index, "action": action}
    item.update(kwargs)
    return item


def add_segment(conn, text, ts="2026-11-10T20:00:00"):
    cur = conn.execute(
        "INSERT INTO segment(ts_start, ts_end, text_masked, source) "
        "VALUES (?, ?, ?, 'manual')",
        (ts, ts, text),
    )
    conn.commit()
    return int(cur.lastrowid)


def create_first_node(conn, *, importance=0.8):
    add_segment(conn, "今天学了洛必达法则")
    llm = FakeLLM(
        extraction(make_candidate(importance=importance)),
        {"decisions": [decision(importance=importance)]},
    )
    memory.consolidate(conn, llm, "2026-11-10")
    return conn.execute("SELECT * FROM node WHERE type = 'fact'").fetchone()


def test_consolidate_creates_node_day_and_part_of_edge(conn):
    add_segment(conn, "今天学了洛必达法则")
    llm = FakeLLM(
        extraction(make_candidate()),
        {"decisions": [decision(importance=0.8)]},
    )

    report = memory.consolidate(conn, llm, "2026-11-10")

    assert report.segments_processed == 1
    assert report.created == 1
    node = conn.execute("SELECT * FROM node WHERE type = 'fact'").fetchone()
    day = conn.execute("SELECT * FROM node WHERE type = 'day'").fetchone()
    assert node["title"] == "洛必达法则"
    assert node["due_at"] == "2026-11-11"
    assert day["title"] == "2026-11-10"
    assert day["due_at"] is None
    edge = conn.execute(
        "SELECT 1 FROM edge WHERE src = ? AND dst = ? AND relation = 'part_of'",
        (node["id"], day["id"]),
    ).fetchone()
    assert edge is not None
    assert conn.execute("SELECT processed FROM segment").fetchone()["processed"] == 1


def test_rerun_without_new_segments_is_noop(conn):
    create_first_node(conn)
    before = conn.execute("SELECT COUNT(*) AS c FROM node").fetchone()["c"]

    report = memory.consolidate(conn, FakeLLM(extraction()), "2026-11-10")

    assert report.segments_processed == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM node").fetchone()["c"] == before


def test_merge_updates_target_and_applies_natural_review(conn):
    target = create_first_node(conn)
    add_segment(conn, "又提到洛必达法则的适用条件", ts="2026-11-10T21:00:00")
    llm = FakeLLM(
        extraction(make_candidate(content="适用于 0/0 型")),
        {
            "decisions": [
                decision(
                    action="merge",
                    target_node_id=target["id"],
                    merged_content="洛必达法则：适用于 0/0 型",
                    importance=0.9,
                )
            ]
        },
    )

    report = memory.consolidate(conn, llm, "2026-11-10")

    assert report.merged == 1
    merged = conn.execute(
        "SELECT * FROM node WHERE id = ?", (target["id"],)
    ).fetchone()
    assert merged["content"] == "洛必达法则：适用于 0/0 型"
    assert merged["step"] == 1
    assert merged["reps"] == 1
    assert merged["due_at"] == "2026-11-13"
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM node WHERE type = 'fact'"
        ).fetchone()["c"]
        == 1
    )


def test_merge_with_missing_target_falls_back_to_create(conn):
    add_segment(conn, "洛必达法则")
    llm = FakeLLM(
        extraction(make_candidate()),
        {
            "decisions": [
                decision(action="merge", target_node_id=999, importance=0.8)
            ]
        },
    )

    report = memory.consolidate(conn, llm, "2026-11-10")

    assert report.created == 1
    assert report.merged == 0


def test_link_creates_edge_from_day_without_new_node(conn):
    target = create_first_node(conn)
    add_segment(conn, "今天又聊到了洛必达法则的历史", ts="2026-11-10T21:00:00")
    llm = FakeLLM(
        extraction(make_candidate(title="洛必达法则的历史")),
        {
            "decisions": [
                decision(
                    action="link",
                    target_node_id=target["id"],
                    relation="related",
                )
            ]
        },
    )

    report = memory.consolidate(conn, llm, "2026-11-10")

    assert report.linked == 1
    day = conn.execute("SELECT * FROM node WHERE type = 'day'").fetchone()
    assert (
        conn.execute(
            "SELECT 1 FROM edge WHERE src = ? AND dst = ? AND relation = 'related'",
            (day["id"], target["id"]),
        ).fetchone()
        is not None
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM node WHERE type = 'fact'"
        ).fetchone()["c"]
        == 1
    )


def test_supersede_marks_old_node_and_creates_new_one(conn):
    old = create_first_node(conn)
    add_segment(conn, "洛必达法则的日期改了", ts="2026-11-10T21:00:00")
    llm = FakeLLM(
        extraction(make_candidate(content="修订后的内容", importance=0.9)),
        {
            "decisions": [
                decision(
                    action="supersede",
                    target_node_id=old["id"],
                    importance=0.9,
                )
            ]
        },
    )

    report = memory.consolidate(conn, llm, "2026-11-10")

    assert report.superseded == 1
    assert report.created == 1
    superseded = conn.execute(
        "SELECT * FROM node WHERE id = ?", (old["id"],)
    ).fetchone()
    assert superseded["status"] == "superseded"
    assert superseded["due_at"] is None
    fresh = conn.execute(
        "SELECT * FROM node WHERE type = 'fact' AND status = 'active'"
    ).fetchone()
    assert fresh["content"] == "修订后的内容"
    assert fresh["due_at"] == "2026-11-11"


@pytest.mark.parametrize(
    "node_type, importance",
    [("fact", 0.1), ("person", 0.9), ("todo", 0.9)],
)
def test_non_review_nodes_have_no_due_date(conn, node_type, importance):
    add_segment(conn, "一些内容")
    llm = FakeLLM(
        extraction(make_candidate(node_type=node_type, importance=importance)),
        {"decisions": [decision(importance=importance)]},
    )

    memory.consolidate(conn, llm, "2026-11-10")

    node = conn.execute(
        "SELECT * FROM node WHERE type = ?", (node_type,)
    ).fetchone()
    assert node["due_at"] is None


def test_ignore_creates_no_memory_node(conn):
    add_segment(conn, "一些闲聊")
    llm = FakeLLM(
        extraction(make_candidate()),
        {"decisions": [decision(action="ignore")]},
    )

    report = memory.consolidate(conn, llm, "2026-11-10")

    assert report.ignored == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM node WHERE type != 'day'"
        ).fetchone()["c"]
        == 0
    )
    assert conn.execute("SELECT processed FROM segment").fetchone()["processed"] == 1


def test_llm_failure_leaves_segment_unprocessed(conn):
    add_segment(conn, "今天学了洛必达法则")

    with pytest.raises(RuntimeError):
        memory.consolidate(conn, ExplodingLLM(), "2026-11-10")

    assert conn.execute("SELECT processed FROM segment").fetchone()["processed"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM node").fetchone()["c"] == 0
