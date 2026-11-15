from backend.pipeline import ingest_text


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


def extraction(*candidates):
    return {"summary": "当天摘要", "topics": [], "candidates": list(candidates)}


def fact_candidate():
    return {
        "type": "fact",
        "title": "项目会议",
        "content": "讨论了里程碑安排",
        "importance": 0.7,
    }


def create_decision():
    return {"decisions": [{"candidate_index": 0, "action": "create"}]}


def test_ingest_masks_persists_mapping_and_writes(conn):
    llm = FakeLLM(extraction(fact_candidate()), create_decision())

    masked, report = ingest_text(
        conn,
        llm,
        "张三说电话是 13800138000",
        known_names=("张三",),
        date_str="2026-11-10",
        ts="2026-11-10T20:00:00",
    )

    assert "张三" not in masked
    assert "13800138000" not in masked
    assert report.created == 1

    segment = conn.execute("SELECT * FROM segment").fetchone()
    assert segment["processed"] == 1
    assert "13800138000" not in segment["text_masked"]

    mapping = {
        row["real_value"]: row["placeholder"]
        for row in conn.execute("SELECT * FROM placeholder_map")
    }
    assert mapping["张三"] == "[人物1]"


def test_known_name_placeholder_is_stable_across_runs(conn):
    empty = extraction()

    first, _ = ingest_text(
        conn,
        FakeLLM(empty),
        "张三来了",
        known_names=("张三",),
        date_str="2026-11-10",
        ts="2026-11-10T20:00:00",
    )
    second, _ = ingest_text(
        conn,
        FakeLLM(empty),
        "张三又来了",
        known_names=("张三",),
        date_str="2026-11-11",
        ts="2026-11-11T20:00:00",
    )

    assert "[人物1]" in first
    assert "[人物1]" in second
    assert "[人物2]" not in second
