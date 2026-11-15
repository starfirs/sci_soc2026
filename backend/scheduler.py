"""复习调度器：挑选到期节点、生成卡片、把反馈写回记忆图。

对应设计文档第 6 节。题型选择是一个简化映射：
step 0-2 线索回忆，3 填空，4-5 情景重现，6 关联提问。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date as Date, datetime

from . import review
from .llm import LLMClient

DEFAULT_DAILY_LIMIT = 8
CARD_TYPES = {"clue", "cloze", "scenario", "relation"}

CARD_SYSTEM = """你是复习出题助手。给定今天到期的记忆节点（内容已脱敏），
请为每个节点生成一道主动回忆题和答案。只输出 JSON，不要输出解释或 Markdown。

JSON 格式：
{
  "cards": [
    {
      "node_id": 1,
      "card_type": "clue | cloze | scenario | relation",
      "question": "题目",
      "answer": "答案"
    }
  ]
}

规则：
- 先给线索，不要直接把答案写进问题；答案必须来自节点内容，不要编造。
- 尽量使用节点给出的 suggested_card_type。
- 保留 [人物1]、[手机1] 等脱敏占位符。
"""

_FALLBACK_QUESTIONS = {
    "clue": "关于「{title}」，你能想起哪些要点？",
    "cloze": "补全：「{title}」的关键内容是什么？",
    "scenario": "回忆一下：你是在什么情境下记下「{title}」的？",
    "relation": "「{title}」和你记忆中的其他内容有什么联系？",
}


@dataclass(frozen=True)
class DueNode:
    node_id: int
    type: str
    title: str
    content: str
    importance: float
    step: int
    interval_days: int
    due_at: str
    lapses: int
    score: float


@dataclass(frozen=True)
class Card:
    node_id: int
    type: str
    title: str
    card_type: str
    question: str
    answer: str
    step: int
    due_at: str
    importance: float
    source: str = "local"  # cache / llm / local


@dataclass(frozen=True)
class PreparationReport:
    review_date: str
    total: int
    llm_cards: int
    local_cards: int
    warning: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ImportanceResult:
    node_id: int
    importance: float
    due_at: str | None
    reviewable: bool


IMPORTANCE_VALUES = {
    "very_high": 1.0,
    "high": 0.8,
    "normal": 0.5,
    "low": 0.2,
}


@dataclass(frozen=True)
class ReviewResult:
    node_id: int
    feedback: str
    step_before: int
    step_after: int
    interval_days: int
    due_at: str
    reps: int
    lapses: int


def card_type_for(step: int) -> str:
    if step <= 2:
        return "clue"
    if step == 3:
        return "cloze"
    if step <= 5:
        return "scenario"
    return "relation"


def _priority(row, today: Date) -> float:
    overdue = max(0, (today - Date.fromisoformat(row["due_at"])).days)
    interval = max(1, int(row["interval_days"]))
    base = (overdue / interval) * (0.5 + float(row["importance"]))
    # lapses 多的节点额外加权
    return base * (1 + 0.2 * int(row["lapses"]))


def select_due(
    conn, today: Date, limit: int = DEFAULT_DAILY_LIMIT
) -> list[DueNode]:
    rows = conn.execute(
        """
        SELECT id, type, title, content, importance, step, interval_days,
               due_at, lapses
        FROM node
        WHERE status = 'active'
          AND due_at IS NOT NULL
          AND due_at <= ?
          AND type NOT IN ('day', 'todo')
        ORDER BY due_at
        """,
        (today.isoformat(),),
    ).fetchall()

    nodes = [
        DueNode(
            node_id=int(row["id"]),
            type=row["type"],
            title=row["title"],
            content=row["content"],
            importance=float(row["importance"]),
            step=int(row["step"]),
            interval_days=int(row["interval_days"]),
            due_at=row["due_at"],
            lapses=int(row["lapses"]),
            score=_priority(row, today),
        )
        for row in rows
    ]
    # score 高者优先；同分时重要度高、到期早者优先
    nodes.sort(key=lambda node: (node.score, node.importance), reverse=True)
    return nodes[:limit]


def _fallback(node: DueNode) -> tuple[str, str, str]:
    card_type = card_type_for(node.step)
    question = _FALLBACK_QUESTIONS[card_type].format(title=node.title)
    return card_type, question, node.content


def _generate_with_llm(
    llm: LLMClient, nodes: list[DueNode]
) -> dict[int, tuple[str, str, str]]:
    payload = {
        "cards": [
            {
                "node_id": node.node_id,
                "suggested_card_type": card_type_for(node.step),
                "title": node.title,
                "content": node.content,
                "step": node.step,
            }
            for node in nodes
        ]
    }
    data = llm.complete_json(
        CARD_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=2)
    )

    valid_ids = {node.node_id for node in nodes}
    generated: dict[int, tuple[str, str, str]] = {}
    if isinstance(data, dict) and isinstance(data.get("cards"), list):
        for item in data["cards"]:
            if not isinstance(item, dict):
                continue
            try:
                node_id = int(item.get("node_id"))
            except (TypeError, ValueError):
                continue
            if node_id not in valid_ids:
                continue
            card_type = str(item.get("card_type") or "").lower().strip()
            if card_type not in CARD_TYPES:
                continue
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if not question or not answer:
                continue
            generated[node_id] = (card_type, question, answer)
    return generated


def _content_hash(node: DueNode) -> str:
    raw = f"{node.title}\n{node.content}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_cached_cards(
    conn, review_date: Date, due_nodes: list[DueNode]
) -> dict[int, Card]:
    """取出当天有效缓存；step 或内容变化过的卡片视为失效。"""
    if not due_nodes:
        return {}
    date_str = review_date.isoformat()
    node_ids = [node.node_id for node in due_nodes]
    placeholders = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"SELECT * FROM daily_card WHERE review_date = ? "
        f"AND node_id IN ({placeholders})",
        (date_str, *node_ids),
    ).fetchall()
    by_id = {int(row["node_id"]): row for row in rows}

    cached: dict[int, Card] = {}
    for node in due_nodes:
        row = by_id.get(node.node_id)
        if row is None:
            continue
        if int(row["step"]) != node.step:
            continue
        if row["content_hash"] != _content_hash(node):
            continue
        cached[node.node_id] = Card(
            node_id=node.node_id,
            type=node.type,
            title=node.title,
            card_type=row["card_type"],
            question=row["question"],
            answer=row["answer"],
            step=node.step,
            due_at=node.due_at,
            importance=node.importance,
            source="cache",
        )
    return cached


def prepare_daily_cards(
    conn,
    llm: LLMClient | None,
    review_date: Date,
    *,
    limit: int = DEFAULT_DAILY_LIMIT,
) -> PreparationReport:
    """生成 review_date 当天的卡片并写入本地缓存，重复运行会覆盖。"""
    due = select_due(conn, review_date, limit)
    generated: dict[int, tuple[str, str, str]] = {}
    warning = ""
    if due and llm is not None:
        try:
            generated = _generate_with_llm(llm, due)
        except Exception as exc:
            warning = f"云端出题失败，已使用本地模板：{exc}"

    date_str = review_date.isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM daily_card WHERE review_date = ?", (date_str,))
        for node in due:
            card_type, question, answer = generated.get(
                node.node_id, _fallback(node)
            )
            source = "llm" if node.node_id in generated else "local"
            conn.execute(
                """
                INSERT INTO daily_card(
                    review_date, node_id, card_type, question, answer,
                    step, content_hash, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    date_str,
                    node.node_id,
                    card_type,
                    question,
                    answer,
                    node.step,
                    _content_hash(node),
                    source,
                    now,
                ),
            )
    return PreparationReport(
        review_date=date_str,
        total=len(due),
        llm_cards=len(generated),
        local_cards=len(due) - len(generated),
        warning=warning,
    )


def get_today_queue(
    conn,
    *,
    llm: LLMClient | None = None,
    today: Date | None = None,
    limit: int = DEFAULT_DAILY_LIMIT,
    use_cache: bool = True,
) -> list[Card]:
    """返回今日卡片；优先读昨晚预生成的缓存，缺失时才调用 LLM。"""
    today = today or Date.today()
    due = select_due(conn, today, limit)
    if not due:
        return []

    cached = _load_cached_cards(conn, today, due) if use_cache else {}
    missing = [node for node in due if node.node_id not in cached]
    generated = (
        _generate_with_llm(llm, missing)
        if llm is not None and missing
        else {}
    )

    cards: list[Card] = []
    for node in due:
        if node.node_id in cached:
            cards.append(cached[node.node_id])
            continue
        card_type, question, answer = generated.get(
            node.node_id, _fallback(node)
        )
        cards.append(
            Card(
                node_id=node.node_id,
                type=node.type,
                title=node.title,
                card_type=card_type,
                question=question,
                answer=answer,
                step=node.step,
                due_at=node.due_at,
                importance=node.importance,
                source="llm" if node.node_id in generated else "local",
            )
        )
    return cards


def submit_feedback(
    conn,
    node_id: int,
    card_type: str,
    feedback: str,
    *,
    today: Date | None = None,
) -> ReviewResult:
    today = today or Date.today()
    row = conn.execute("SELECT * FROM node WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        raise ValueError(f"节点 {node_id} 不存在")

    update = review.apply_feedback(
        int(row["step"]),
        int(row["reps"]),
        int(row["lapses"]),
        feedback,
        today,
    )
    now = datetime.now().isoformat(timespec="seconds")

    with conn:
        conn.execute(
            """
            UPDATE node SET step = ?, interval_days = ?, due_at = ?,
                last_reviewed_at = ?, reps = ?, lapses = ?
            WHERE id = ?
            """,
            (
                update.step,
                update.interval_days,
                update.due_at,
                now,
                update.reps,
                update.lapses,
                node_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO review_log(
                node_id, ts, card_type, feedback, step_before, step_after
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                now,
                card_type,
                feedback,
                int(row["step"]),
                update.step,
            ),
        )

    return ReviewResult(
        node_id=node_id,
        feedback=feedback,
        step_before=int(row["step"]),
        step_after=update.step,
        interval_days=update.interval_days,
        due_at=update.due_at,
        reps=update.reps,
        lapses=update.lapses,
    )


def set_node_importance(
    conn,
    node_id: int,
    importance: float,
    *,
    today: Date | None = None,
) -> ImportanceResult:
    """设置重要度；低于 0.3 时移出复习队列。"""
    importance = max(0.0, min(1.0, float(importance)))
    row = conn.execute(
        "SELECT * FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"节点 {node_id} 不存在")

    today = today or Date.today()
    due_at = row["due_at"]
    if importance < 0.3:
        due_at = None
    elif due_at is None and row["type"] not in {
        "day",
        "person",
        "todo",
    }:
        # 从“不复习”改成重要：立即进入今天的队列。
        due_at = today.isoformat()

    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "UPDATE node SET importance = ?, due_at = ?, updated_at = ? "
            "WHERE id = ?",
            (importance, due_at, now, node_id),
        )
    return ImportanceResult(
        node_id=node_id,
        importance=importance,
        due_at=due_at,
        reviewable=due_at is not None,
    )


def delete_node(conn, node_id: int) -> bool:
    """删除节点；边、复习记录和当日卡片由外键级联删除。"""
    row = conn.execute(
        "SELECT id, type FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None:
        return False
    if row["type"] == "day":
        raise ValueError("不能删除 day 节点")

    with conn:
        conn.execute("DELETE FROM node WHERE id = ?", (node_id,))
        # daily_summary.node_ids 是 JSON，不是外键，顺手清掉悬空 id。
        summaries = conn.execute(
            "SELECT date, node_ids FROM daily_summary"
        ).fetchall()
        for summary in summaries:
            try:
                ids = json.loads(summary["node_ids"] or "[]")
            except (TypeError, ValueError):
                continue
            if node_id not in ids:
                continue
            cleaned = [item for item in ids if item != node_id]
            conn.execute(
                "UPDATE daily_summary SET node_ids = ? WHERE date = ?",
                (json.dumps(cleaned, ensure_ascii=False), summary["date"]),
            )
    return True
