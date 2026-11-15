"""桌宠的纯逻辑状态机，不依赖 Qt，便于单独测试。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date as Date

from .scheduler import (
    IMPORTANCE_VALUES,
    Card,
    ImportanceResult,
    delete_node,
    get_today_queue,
    set_node_importance,
    submit_feedback,
)


@dataclass
class PetController:
    conn: object
    llm: object | None = None
    limit: int = 8
    today: Date | None = None
    cards: list[Card] = field(default_factory=list)
    index: int = 0
    revealed: bool = False

    def load(self) -> list[Card]:
        self.cards = get_today_queue(
            self.conn,
            llm=self.llm,
            today=self.today,
            limit=self.limit,
        )
        self.index = 0
        self.revealed = False
        return self.cards

    def set_cards(self, cards: list[Card]) -> None:
        self.cards = list(cards)
        self.index = 0
        self.revealed = False

    @property
    def current(self) -> Card | None:
        if 0 <= self.index < len(self.cards):
            return self.cards[self.index]
        return None

    @property
    def done(self) -> bool:
        return self.index >= len(self.cards)

    @property
    def total(self) -> int:
        return len(self.cards)

    def progress(self) -> tuple[int, int]:
        if not self.cards:
            return (0, 0)
        return (min(self.index + 1, len(self.cards)), len(self.cards))

    def reveal(self) -> None:
        if self.current is not None:
            self.revealed = True

    def feedback(self, value: str):
        card = self.current
        if card is None:
            return None
        result = submit_feedback(
            self.conn,
            card.node_id,
            card.card_type,
            value,
            today=self.today,
        )
        self.advance()
        return result

    def set_importance(self, value: str | float):
        card = self.current
        if card is None:
            return None
        importance = (
            IMPORTANCE_VALUES[value]
            if isinstance(value, str)
            else float(value)
        )
        result = set_node_importance(
            self.conn,
            card.node_id,
            importance,
            today=self.today,
        )
        self.cards[self.index] = replace(
            card, importance=result.importance
        )
        return result

    def delete_current(self) -> bool:
        card = self.current
        if card is None:
            return False
        if not delete_node(self.conn, card.node_id):
            return False
        del self.cards[self.index]
        self.revealed = False
        return True

    def skip(self) -> None:
        self.advance()

    def advance(self) -> None:
        self.index += 1
        self.revealed = False
