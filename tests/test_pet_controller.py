from datetime import date

from backend.pet_controller import PetController

TODAY = date(2026, 11, 13)


def add_due_node(conn, title="洛必达法则", content="求极限的方法"):
    cur = conn.execute(
        """
        INSERT INTO node(
            type, title, content, importance, status, source_segment_ids,
            created_at, updated_at, step, interval_days, due_at, reps, lapses
        ) VALUES ('fact', ?, ?, 0.8, 'active', '[]', '2026-11-01T00:00:00',
                  '2026-11-01T00:00:00', 0, 1, '2026-11-13', 0, 0)
        """,
        (title, content),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_load_advances_through_cards_and_finishes(conn):
    add_due_node(conn, title="第一题")
    add_due_node(conn, title="第二题")
    controller = PetController(conn, llm=None, today=TODAY)

    cards = controller.load()

    assert len(cards) == 2
    assert controller.progress() == (1, 2)
    assert controller.current.title == "第一题"
    controller.skip()
    assert controller.current.title == "第二题"
    controller.skip()
    assert controller.done
    assert controller.current is None


def test_reveal_then_feedback_updates_db_and_moves_on(conn):
    node_id = add_due_node(conn)
    controller = PetController(conn, llm=None, today=TODAY)
    controller.load()

    assert not controller.revealed
    controller.reveal()
    assert controller.revealed

    result = controller.feedback("remembered")

    assert result is not None
    assert result.step_after == 1
    assert result.due_at == "2026-11-16"
    assert controller.done
    assert not controller.revealed
    node = conn.execute(
        "SELECT * FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    assert node["step"] == 1


def test_empty_queue_is_done_immediately(conn):
    controller = PetController(conn, llm=None, today=TODAY)

    assert controller.load() == []
    assert controller.done
    assert controller.progress() == (0, 0)
    assert controller.current is None


def test_importance_and_delete_current(conn):
    first = add_due_node(conn, title="第一题")
    second = add_due_node(conn, title="第二题")
    controller = PetController(conn, llm=None, today=TODAY)
    controller.load()

    result = controller.set_importance("low")

    assert result is not None
    assert result.importance == 0.2
    assert result.reviewable is False
    assert controller.current.node_id == first

    controller.advance()
    assert controller.current.node_id == second
    assert controller.delete_current() is True
    assert controller.done
    assert conn.execute(
        "SELECT 1 FROM node WHERE id = ?", (second,)
    ).fetchone() is None
    assert controller.delete_current() is False
