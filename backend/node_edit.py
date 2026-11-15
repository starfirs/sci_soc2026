"""记忆节点编辑接口：直接修改字段，或让 LLM 按自然语言改写。

- 直接修改：title/content/type/importance/status。
- LLM 修改：传入自然语言要求，云端只看到脱敏后的节点和指令，
  返回修改后的完整节点；占位符保持不变。
- 修改标题/内容/类型会让该节点的 daily_card 缓存失效。
- 重要度低于 0.3 或状态非 active 时自动移出复习队列。

用法：
    python -m backend.node_edit --list
    python -m backend.node_edit --id 3 --show
    python -m backend.node_edit --id 3 --title "新标题" --content "新内容"
    python -m backend.node_edit --id 3 --importance 0.9 --type concept
    python -m backend.node_edit --id 3 --instruction "把这条记忆改得更简洁"
    python -m backend.node_edit --id 3 --delete
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date as Date, datetime
from pathlib import Path

from . import scrub
from .config import configure_stdio, get_db_path, get_llm_config
from .db import connect
from .llm import DeepSeekClient, LLMError
from .pipeline import load_entity_map, save_entity_map
from .scheduler import delete_node

EDITABLE_TYPES = ("fact", "event", "person", "concept", "todo")
STATUSES = ("active", "archived", "superseded")
NON_REVIEW_TYPES = {"todo", "person", "day"}

EDIT_SYSTEM = """你是记忆节点编辑助手。节点内容已经脱敏，可能包含
[人物1]、[手机1] 等占位符。

根据用户的修改要求，返回修改后的完整节点。只输出 JSON，不要解释：
{
  "title": "标题",
  "content": "完整内容",
  "type": "fact | event | person | concept | todo",
  "importance": 0.0,
  "note": "可选说明"
}

规则：
- 保留所有占位符，不要还原真实姓名、电话等信息。
- 只修改用户要求的部分，不要编造节点中没有的事实。
- content 要完整、简洁；importance 在 0 到 1 之间。
- 如果要求无法理解，保持原值，并在 note 里说明。
"""


@dataclass(frozen=True)
class NodeEditResult:
    node_id: int
    title: str
    content: str
    node_type: str
    importance: float
    status: str
    due_at: str | None
    changed: bool
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def get_node(conn, node_id: int):
    row = conn.execute(
        "SELECT * FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"节点 {node_id} 不存在")
    return row


def node_detail(conn, node_id: int) -> dict:
    row = get_node(conn, node_id)
    outgoing = conn.execute(
        "SELECT dst, relation, weight FROM edge WHERE src = ?", (node_id,)
    ).fetchall()
    incoming = conn.execute(
        "SELECT src, relation, weight FROM edge WHERE dst = ?", (node_id,)
    ).fetchall()
    return {
        "id": row["id"],
        "type": row["type"],
        "title": row["title"],
        "content": row["content"],
        "importance": row["importance"],
        "status": row["status"],
        "step": row["step"],
        "interval_days": row["interval_days"],
        "due_at": row["due_at"],
        "last_reviewed_at": row["last_reviewed_at"],
        "reps": row["reps"],
        "lapses": row["lapses"],
        "source_segment_ids": row["source_segment_ids"],
        "edges_out": [dict(item) for item in outgoing],
        "edges_in": [dict(item) for item in incoming],
    }


def _mask_text(conn, text: str, known_names=()) -> str:
    rows = conn.execute(
        "SELECT placeholder, real_value, kind FROM placeholder_map"
    ).fetchall()
    existing = {
        row["placeholder"]: row["real_value"] for row in rows
    }
    known = set(known_names)
    known.update(
        row["real_value"] for row in rows if row["kind"] == "person"
    )
    masked, new_map = scrub.mask(
        text, known_names=known, existing=existing
    )
    if new_map:
        save_entity_map(conn, new_map)
    return masked


def _due_after_edit(
    row,
    node_type: str,
    importance: float,
    status: str,
    today: Date,
) -> str | None:
    if node_type in NON_REVIEW_TYPES or importance < 0.3 or status != "active":
        return None
    if row["due_at"] is None:
        return today.isoformat()
    return row["due_at"]


def update_node(
    conn,
    node_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    node_type: str | None = None,
    importance: float | None = None,
    status: str | None = None,
    known_names=(),
    today: Date | None = None,
    note: str = "",
) -> NodeEditResult:
    """直接修改节点字段；文本会先本地脱敏。"""
    row = get_node(conn, node_id)
    if row["type"] == "day":
        raise ValueError("day 节点是时间锚点，不支持这样编辑")

    new_title = row["title"] if title is None else title.strip()
    new_content = row["content"] if content is None else content.strip()
    if not new_title:
        raise ValueError("标题不能为空")
    if not new_content:
        raise ValueError("内容不能为空")
    if title is not None:
        new_title = _mask_text(conn, new_title, known_names)
    if content is not None:
        new_content = _mask_text(conn, new_content, known_names)

    new_type = row["type"] if node_type is None else node_type
    if new_type not in EDITABLE_TYPES:
        raise ValueError(f"不支持的节点类型：{new_type}")
    new_importance = (
        float(row["importance"])
        if importance is None
        else max(0.0, min(1.0, float(importance)))
    )
    new_status = row["status"] if status is None else status
    if new_status not in STATUSES:
        raise ValueError(f"不支持的状态：{new_status}")

    today = today or Date.today()
    due_at = _due_after_edit(
        row, new_type, new_importance, new_status, today
    )
    changed = any(
        (
            new_title != row["title"],
            new_content != row["content"],
            new_type != row["type"],
            new_importance != float(row["importance"]),
            new_status != row["status"],
            due_at != row["due_at"],
        )
    )
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            """
            UPDATE node
            SET title = ?, content = ?, type = ?, importance = ?,
                status = ?, due_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_title,
                new_content,
                new_type,
                new_importance,
                new_status,
                due_at,
                now,
                node_id,
            ),
        )
        content_changed = (
            new_title != row["title"]
            or new_content != row["content"]
            or new_type != row["type"]
        )
        if content_changed:
            # 题面/答案依赖节点内容，旧缓存全部失效。
            conn.execute(
                "DELETE FROM daily_card WHERE node_id = ?", (node_id,)
            )
    return NodeEditResult(
        node_id=node_id,
        title=new_title,
        content=new_content,
        node_type=new_type,
        importance=new_importance,
        status=new_status,
        due_at=due_at,
        changed=changed,
        note=note,
    )


def edit_node(
    conn,
    node_id: int,
    *,
    instruction: str | None = None,
    llm=None,
    known_names=(),
    today: Date | None = None,
    **fields,
) -> NodeEditResult:
    """统一入口：有 instruction 走 LLM，否则直接改字段。"""
    if instruction is not None:
        if llm is None:
            raise ValueError("使用 instruction 时必须传入 llm")
        return edit_node_with_llm(
            conn,
            llm,
            node_id,
            instruction,
            known_names=known_names,
            today=today,
        )
    return update_node(
        conn,
        node_id,
        known_names=known_names,
        today=today,
        **fields,
    )


def edit_node_with_llm(
    conn,
    llm,
    node_id: int,
    instruction: str,
    *,
    known_names=(),
    today: Date | None = None,
) -> NodeEditResult:
    """让 LLM 按自然语言要求改写节点；输入输出都只含脱敏文本。"""
    instruction = instruction.strip()
    if not instruction:
        raise ValueError("修改要求不能为空")
    row = get_node(conn, node_id)
    if row["type"] == "day":
        raise ValueError("day 节点是时间锚点，不支持这样编辑")

    masked_instruction = _mask_text(
        conn, instruction, known_names
    )
    payload = {
        "node": {
            "id": row["id"],
            "type": row["type"],
            "title": row["title"],
            "content": row["content"],
            "importance": row["importance"],
        },
        "instruction": masked_instruction,
    }
    data = llm.complete_json(
        EDIT_SYSTEM,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    if not isinstance(data, dict):
        raise LLMError("LLM 没有返回节点 JSON")

    new_title = str(data.get("title") or row["title"]).strip()
    new_content = str(data.get("content") or row["content"]).strip()
    new_type = str(data.get("type") or row["type"]).strip()
    if new_type not in EDITABLE_TYPES:
        new_type = row["type"]
    try:
        new_importance = float(
            data.get("importance", row["importance"])
        )
    except (TypeError, ValueError):
        new_importance = float(row["importance"])
    note = str(data.get("note") or "").strip()

    return update_node(
        conn,
        node_id,
        title=new_title,
        content=new_content,
        node_type=new_type,
        importance=new_importance,
        known_names=known_names,
        today=today,
        note=note,
    )


def _print_list(conn) -> None:
    rows = conn.execute(
        "SELECT id, type, title, importance, status, due_at "
        "FROM node ORDER BY id"
    ).fetchall()
    if not rows:
        print("记忆图为空。")
        return
    for row in rows:
        print(
            f"[{row['id']}] {row['type']:<7} {row['title']} "
            f"(importance={row['importance']:.2f}, "
            f"due={row['due_at']}, status={row['status']})"
        )


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="AI 记忆增强伙伴 - 节点查看与编辑"
    )
    parser.add_argument("--db", help="数据库路径，默认 data/memory.db")
    parser.add_argument("--id", type=int, help="节点 id")
    parser.add_argument("--list", action="store_true", help="列出节点")
    parser.add_argument("--show", action="store_true", help="查看节点详情")
    parser.add_argument("--title", help="新标题")
    parser.add_argument("--content", help="新内容")
    parser.add_argument(
        "--content-file", type=Path, help="从 UTF-8 文件读取新内容"
    )
    parser.add_argument(
        "--type", dest="node_type", choices=EDITABLE_TYPES
    )
    parser.add_argument("--importance", type=float)
    parser.add_argument("--status", choices=STATUSES)
    parser.add_argument(
        "--instruction", help="自然语言修改要求，由 LLM 改写"
    )
    parser.add_argument(
        "--name", action="append", default=[],
        help="需要脱敏的真实姓名，可重复",
    )
    parser.add_argument(
        "--delete", action="store_true",
        help="删除该节点（级联删除边/复习记录/卡片）",
    )
    args = parser.parse_args(argv)

    conn = connect(args.db or get_db_path())
    if args.list:
        _print_list(conn)
        return 0
    if args.id is None:
        parser.error("需要 --id，或使用 --list")
    if args.show:
        print(
            json.dumps(
                node_detail(conn, args.id),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.delete:
        if not delete_node(conn, args.id):
            print(f"节点 {args.id} 不存在")
            return 1
        print(f"已删除节点 {args.id}")
        return 0

    content = args.content
    if args.content_file is not None:
        if content is not None:
            parser.error("--content 和 --content-file 只能用一个")
        content = args.content_file.read_text(encoding="utf-8")

    direct_fields = {
        "title": args.title,
        "content": content,
        "node_type": args.node_type,
        "importance": args.importance,
        "status": args.status,
    }
    has_direct = any(value is not None for value in direct_fields.values())
    if not has_direct and not args.instruction:
        parser.error(
            "需要 --title/--content/--type/--importance/--status，"
            "或 --instruction"
        )

    try:
        if args.instruction:
            result = edit_node_with_llm(
                conn,
                DeepSeekClient(get_llm_config()),
                args.id,
                args.instruction,
                known_names=tuple(args.name),
            )
        else:
            result = update_node(
                conn,
                args.id,
                **direct_fields,
                known_names=tuple(args.name),
            )
    except (ValueError, LLMError) as exc:
        print(f"编辑失败：{exc}")
        return 1

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
