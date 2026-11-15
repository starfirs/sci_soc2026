"""记忆更新流水线：脱敏片段 -> 候选记忆项 -> 决策 -> 写入 SQLite 图。

对应设计文档第 4、5 节。与文档的差异：
- 相似节点检索暂用字符 bigram 的 Jaccard 相似度，等本地嵌入模型接入后替换。
- “仅连线”在候选项不是节点时，连接“当天日期节点”和已有目标节点。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date as Date, datetime

from .llm import LLMClient
from .review import next_due, natural_review_update

NODE_TYPES = {"fact", "event", "person", "concept", "todo"}
RELATIONS = {"related", "part_of", "causes", "same_as"}
ACTIONS = {"create", "merge", "link", "ignore", "supersede"}
SIMILAR_LIMIT = 5
REVIEW_MIN_IMPORTANCE = 0.3

EXTRACT_SYSTEM = """你是一个记忆整理助手。输入是用户当天脱敏后的语音转写文本。
请提炼出值得长期记住的记忆项。只输出 JSON，不要输出解释或 Markdown 代码块。

JSON 格式：
{
  "summary": "当天内容的一句话摘要",
  "topics": ["主题1", "主题2"],
  "candidates": [
    {
      "type": "fact | event | person | concept | todo",
      "title": "简短标题，不超过 20 字",
      "content": "完整、独立可读的描述",
      "importance": 0.0
    }
  ]
}

规则：
- 去掉闲聊、重复和无信息量的内容；没有值得记住的内容时 candidates 为空数组。
- importance 取 0 到 1；日常琐事低于 0.3。
- 文本中的 [人物1]、[手机1] 等是脱敏占位符，必须原样保留。
"""

DECISION_SYSTEM = """你是记忆图维护助手。给定今天提取出的候选项，以及图中已有的相似节点，
请为每个候选项决定如何更新记忆图。只输出 JSON，不要输出解释或 Markdown 代码块。

JSON 格式：
{
  "decisions": [
    {
      "candidate_index": 0,
      "action": "create | merge | link | ignore | supersede",
      "target_node_id": null,
      "relation": "related | part_of | causes | same_as",
      "merged_content": null,
      "importance": 0.5
    }
  ]
}

动作含义：
- create：全新信息，新建节点。若与某个旧节点相关，填 target_node_id 和 relation。
- merge：与 target_node_id 是同一事实，合并细节；merged_content 给合并后的完整内容。
- link：相关但不是同一件事，只在“当天日期节点”和 target_node_id 之间建立边。
- supersede：与 target_node_id 矛盾（时间、数字等变了），新建节点并把旧节点标为已被更新。
- ignore：闲聊或无信息量。

每个候选项都必须有一条 decision，candidate_index 对应输入的序号。
importance 取 0 到 1；关系不确定时用 related。
"""


@dataclass(frozen=True)
class Candidate:
    index: int
    type: str
    title: str
    content: str
    importance: float


@dataclass(frozen=True)
class SimilarNode:
    id: int
    type: str
    title: str
    content: str
    importance: float


@dataclass(frozen=True)
class Decision:
    action: str
    target_node_id: int | None = None
    relation: str = "related"
    merged_content: str | None = None
    importance: float | None = None


@dataclass
class ConsolidationReport:
    date: str
    segments_processed: int = 0
    created: int = 0
    merged: int = 0
    linked: int = 0
    ignored: int = 0
    superseded: int = 0
    node_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp01(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _tokens(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    grams = {compact[i : i + 2] for i in range(len(compact) - 1)}
    grams.update(re.findall(r"[a-z0-9]+", compact))
    return grams


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _load_segments(
    conn: sqlite3.Connection, date_str: str, segment_ids: list[int] | None
) -> list[sqlite3.Row]:
    if segment_ids:
        marks = ",".join("?" * len(segment_ids))
        return conn.execute(
            f"SELECT * FROM segment WHERE processed = 0 AND id IN ({marks}) "
            "ORDER BY ts_start",
            tuple(segment_ids),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM segment WHERE processed = 0 AND substr(ts_start, 1, 10) = ? "
        "ORDER BY ts_start",
        (date_str,),
    ).fetchall()


def _extract(
    llm: LLMClient, segments: list[sqlite3.Row]
) -> tuple[str, list[str], list[Candidate]]:
    body = "\n".join(f"- {row['text_masked']}" for row in segments)
    data = llm.complete_json(EXTRACT_SYSTEM, f"当天脱敏文本：\n{body}")
    if not isinstance(data, dict):
        raise ValueError("提取结果不是 JSON 对象")

    summary = str(data.get("summary") or "").strip()
    raw_topics = data.get("topics")
    topics = (
        [str(t).strip() for t in raw_topics if str(t).strip()]
        if isinstance(raw_topics, list)
        else []
    )

    candidates: list[Candidate] = []
    raw_candidates = data.get("candidates")
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            node_type = str(item.get("type") or "").lower().strip()
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if node_type not in NODE_TYPES or not title or not content:
                continue
            candidates.append(
                Candidate(
                    index=len(candidates),
                    type=node_type,
                    title=title,
                    content=content,
                    importance=_clamp01(item.get("importance", 0.5)),
                )
            )
    return summary, topics[:20], candidates


def _similar_nodes(
    conn: sqlite3.Connection, text: str, limit: int = SIMILAR_LIMIT
) -> list[SimilarNode]:
    rows = conn.execute(
        "SELECT id, type, title, content, importance FROM node "
        "WHERE status = 'active' AND type != 'day'"
    ).fetchall()
    scored = sorted(
        ((_similarity(text, f"{row['title']} {row['content']}"), row) for row in rows),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [
        SimilarNode(
            id=row["id"],
            type=row["type"],
            title=row["title"],
            content=row["content"],
            importance=row["importance"],
        )
        for _, row in scored[:limit]
    ]


def _decide(
    llm: LLMClient,
    date_str: str,
    candidates: list[Candidate],
    similar: dict[int, list[SimilarNode]],
) -> dict[int, Decision]:
    payload = {
        "date": date_str,
        "candidates": [
            {
                "candidate_index": c.index,
                "type": c.type,
                "title": c.title,
                "content": c.content,
                "importance": c.importance,
            }
            for c in candidates
        ],
        "similar_nodes": {
            str(index): [asdict(node) for node in nodes]
            for index, nodes in similar.items()
        },
    }
    data = llm.complete_json(
        DECISION_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=2)
    )
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise ValueError("决策结果缺少 decisions 数组")

    decisions: dict[int, Decision] = {}
    for item in data["decisions"]:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("candidate_index"))
        except (TypeError, ValueError):
            continue

        action = str(item.get("action") or "ignore").lower().strip()
        if action not in ACTIONS:
            action = "ignore"

        target = item.get("target_node_id")
        try:
            target_id = int(target) if target is not None else None
        except (TypeError, ValueError):
            target_id = None

        relation = str(item.get("relation") or "related").lower().strip()
        if relation not in RELATIONS:
            relation = "related"

        merged = item.get("merged_content")
        importance = item.get("importance")
        decisions[index] = Decision(
            action=action,
            target_node_id=target_id,
            relation=relation,
            merged_content=str(merged).strip() if merged else None,
            importance=_clamp01(importance) if importance is not None else None,
        )
    return decisions


def _initial_due(node_type: str, importance: float, today: Date) -> str | None:
    # todo 不走复习；person 仅在用户标为重要后进入复习（标记功能待做）
    if node_type in {"todo", "person"} or importance < REVIEW_MIN_IMPORTANCE:
        return None
    return next_due(0, today)


def _insert_node(
    conn: sqlite3.Connection,
    *,
    node_type: str,
    title: str,
    content: str,
    importance: float,
    segment_ids: list[int],
    due_at: str | None,
    now: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO node(
            type, title, content, importance, status, source_segment_ids,
            created_at, updated_at, step, interval_days, due_at,
            last_reviewed_at, reps, lapses
        ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 0, 1, ?, NULL, 0, 0)
        """,
        (
            node_type,
            title,
            content,
            importance,
            json.dumps(sorted(set(segment_ids))),
            now,
            now,
            due_at,
        ),
    )
    return int(cur.lastrowid)


def _add_edge(
    conn: sqlite3.Connection, src: int, dst: int, relation: str, now: str
) -> None:
    if src == dst:
        return
    conn.execute(
        "INSERT OR IGNORE INTO edge(src, dst, relation, weight, created_at) "
        "VALUES (?, ?, ?, 1.0, ?)",
        (src, dst, relation, now),
    )


def _merge_source_ids(raw: str, new_ids: list[int]) -> str:
    try:
        existing = set(json.loads(raw))
    except (TypeError, ValueError):
        existing = set()
    existing.update(new_ids)
    return json.dumps(sorted(existing))


def _get_node(conn: sqlite3.Connection, node_id: int | None):
    if node_id is None:
        return None
    return conn.execute("SELECT * FROM node WHERE id = ?", (node_id,)).fetchone()


def _ensure_day_node(
    conn: sqlite3.Connection, date_str: str, summary: str, now: str
) -> int:
    row = conn.execute(
        "SELECT id FROM node WHERE type = 'day' AND title = ? ORDER BY id LIMIT 1",
        (date_str,),
    ).fetchone()
    if row:
        if summary:
            conn.execute(
                "UPDATE node SET content = ?, updated_at = ? WHERE id = ?",
                (summary, now, row["id"]),
            )
        return int(row["id"])

    cur = conn.execute(
        """
        INSERT INTO node(
            type, title, content, importance, status, source_segment_ids,
            created_at, updated_at, step, interval_days, due_at
        ) VALUES ('day', ?, ?, 1.0, 'active', '[]', ?, ?, 0, 120, NULL)
        """,
        (date_str, summary or date_str, now, now),
    )
    return int(cur.lastrowid)


def consolidate(
    conn: sqlite3.Connection,
    llm: LLMClient,
    date_str: str,
    *,
    segment_ids: list[int] | None = None,
    today: Date | None = None,
) -> ConsolidationReport:
    """处理某天未整合的片段；重复运行不会产生重复节点。"""
    today = today or Date.fromisoformat(date_str)
    segments = _load_segments(conn, date_str, segment_ids)
    if not segments:
        return ConsolidationReport(date=date_str)

    ids = [int(row["id"]) for row in segments]
    summary, topics, candidates = _extract(llm, segments)

    similar = {
        c.index: _similar_nodes(conn, f"{c.title} {c.content}") for c in candidates
    }
    decisions = _decide(llm, date_str, candidates, similar) if candidates else {}

    report = ConsolidationReport(date=date_str, segments_processed=len(segments))
    now = datetime.now().isoformat(timespec="seconds")

    with conn:
        day_id = _ensure_day_node(conn, date_str, summary, now)
        touched: list[int] = []

        for candidate in candidates:
            decision = decisions.get(
                candidate.index, Decision(action="ignore")
            )

            if decision.action == "ignore":
                report.ignored += 1
                continue

            if decision.action == "merge":
                target = _get_node(conn, decision.target_node_id)
                if (
                    target is None
                    or target["type"] == "day"
                    or target["status"] != "active"
                ):
                    # 目标已失效，退化为新建
                    decision = Decision(
                        action="create",
                        relation=decision.relation,
                        importance=decision.importance,
                    )
                else:
                    content = decision.merged_content or (
                        f"{target['content']}\n{candidate.content}"
                    )
                    importance = max(
                        float(target["importance"]),
                        candidate.importance,
                        _clamp01(decision.importance or 0),
                    )
                    if target["due_at"] is not None:
                        update = natural_review_update(
                            int(target["step"]),
                            int(target["reps"]),
                            int(target["lapses"]),
                            today,
                        )
                        step, interval = update.step, update.interval_days
                        due_at, reps, lapses = (
                            update.due_at,
                            update.reps,
                            update.lapses,
                        )
                    else:
                        step, interval = int(target["step"]), int(
                            target["interval_days"]
                        )
                        due_at = None
                        reps, lapses = int(target["reps"]), int(target["lapses"])

                    conn.execute(
                        """
                        UPDATE node SET content = ?, importance = ?,
                            source_segment_ids = ?, updated_at = ?, step = ?,
                            interval_days = ?, due_at = ?, reps = ?, lapses = ?
                        WHERE id = ?
                        """,
                        (
                            content,
                            importance,
                            _merge_source_ids(target["source_segment_ids"], ids),
                            now,
                            step,
                            interval,
                            due_at,
                            reps,
                            lapses,
                            target["id"],
                        ),
                    )
                    _add_edge(conn, int(target["id"]), day_id, "part_of", now)
                    touched.append(int(target["id"]))
                    report.merged += 1
                    continue

            if decision.action in {"create", "supersede"}:
                if decision.action == "supersede":
                    old = _get_node(conn, decision.target_node_id)
                    if old is None or old["type"] == "day":
                        decision = Decision(
                            action="create",
                            relation=decision.relation,
                            importance=decision.importance,
                        )
                    else:
                        conn.execute(
                            "UPDATE node SET status = 'superseded', "
                            "updated_at = ?, due_at = NULL WHERE id = ?",
                            (now, old["id"]),
                        )
                        report.superseded += 1

                importance = _clamp01(
                    decision.importance
                    if decision.importance is not None
                    else candidate.importance
                )
                node_id = _insert_node(
                    conn,
                    node_type=candidate.type,
                    title=candidate.title,
                    content=candidate.content,
                    importance=importance,
                    segment_ids=ids,
                    due_at=_initial_due(candidate.type, importance, today),
                    now=now,
                )
                _add_edge(conn, node_id, day_id, "part_of", now)
                target = _get_node(conn, decision.target_node_id)
                if (
                    target is not None
                    and int(target["id"]) != node_id
                    and target["type"] != "day"
                ):
                    _add_edge(conn, node_id, int(target["id"]), decision.relation, now)
                touched.append(node_id)
                report.created += 1
                continue

            if decision.action == "link":
                target = _get_node(conn, decision.target_node_id)
                if target is not None and target["type"] != "day":
                    _add_edge(conn, day_id, int(target["id"]), decision.relation, now)
                    touched.append(int(target["id"]))
                    report.linked += 1
                else:
                    report.ignored += 1

        node_ids = sorted(set(touched))
        report.node_ids = node_ids

        previous = conn.execute(
            "SELECT summary FROM daily_summary WHERE date = ?", (date_str,)
        ).fetchone()
        final_summary = summary or (previous["summary"] if previous else "")
        conn.execute(
            """
            INSERT INTO daily_summary(date, summary, topics, node_ids)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                summary = excluded.summary,
                topics = excluded.topics,
                node_ids = excluded.node_ids
            """,
            (
                date_str,
                final_summary,
                json.dumps(topics, ensure_ascii=False),
                json.dumps(node_ids),
            ),
        )

        conn.executemany(
            "UPDATE segment SET processed = 1 WHERE id = ?", [(i,) for i in ids]
        )
        conn.execute(
            """
            INSERT INTO consolidation_report(date, report_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                report_json = excluded.report_json,
                created_at = excluded.created_at
            """,
            (date_str, json.dumps(report.to_dict(), ensure_ascii=False), now),
        )

    return report
