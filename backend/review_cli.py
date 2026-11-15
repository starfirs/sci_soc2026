"""复习命令行：挑出今日到期卡片，先回忆、再揭晓、最后收集反馈。"""
from __future__ import annotations

import argparse

from .config import configure_stdio, get_db_path, get_llm_config
from .db import connect
from .llm import DeepSeekClient
from .scheduler import DEFAULT_DAILY_LIMIT, get_today_queue, submit_feedback

FEEDBACK_BY_KEY = {"1": "remembered", "2": "fuzzy", "3": "forgot"}
FEEDBACK_LABELS = {
    "remembered": "记得",
    "fuzzy": "模糊",
    "forgot": "不记得",
}
CARD_TYPE_LABELS = {
    "clue": "线索回忆",
    "cloze": "填空",
    "scenario": "情景重现",
    "relation": "关联提问",
}


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="AI 记忆增强伙伴 - 今日复习")
    parser.add_argument("--limit", type=int, default=DEFAULT_DAILY_LIMIT)
    parser.add_argument("--db")
    parser.add_argument("--list", action="store_true", help="只列出题目和答案")
    parser.add_argument(
        "--offline", action="store_true", help="用本地模板出题，不调用云端"
    )
    args = parser.parse_args(argv)

    conn = connect(args.db or get_db_path())
    llm = None if args.offline else DeepSeekClient(get_llm_config())
    cards = get_today_queue(conn, llm=llm, limit=args.limit)

    if not cards:
        print("今天没有到期的记忆，休息一下。")
        return 0

    print(f"今天共有 {len(cards)} 张卡片。\n")

    if args.list:
        for index, card in enumerate(cards, 1):
            label = CARD_TYPE_LABELS.get(card.card_type, card.card_type)
            print(f"[{index}] ({label}) {card.question}")
            print(f"    答案：{card.answer}\n")
        return 0

    for index, card in enumerate(cards, 1):
        label = CARD_TYPE_LABELS.get(card.card_type, card.card_type)
        print(f"[{index}/{len(cards)}] {label}")
        print(card.question)
        key = input("\n按回车显示答案，输入 q 退出：").strip().lower()
        if key == "q":
            break

        print(f"\n答案：{card.answer}\n")
        while True:
            raw = input(
                "记得(1) / 模糊(2) / 不记得(3) / 跳过(s)："
            ).strip().lower()
            if raw in {"q", "s", ""}:
                break
            feedback = FEEDBACK_BY_KEY.get(raw)
            if feedback is None:
                print("请输入 1、2、3、s 或 q。")
                continue
            result = submit_feedback(
                conn, card.node_id, card.card_type, feedback
            )
            print(
                f"已记录「{FEEDBACK_LABELS[feedback]}」，"
                f"下次复习：{result.due_at}\n"
            )
            break
        if raw == "q":
            break

    print("复习结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
