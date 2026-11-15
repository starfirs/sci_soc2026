from datetime import date

import pytest

from backend.review import (
    MAX_STEP,
    apply_feedback,
    interval_for_step,
    natural_review_update,
)

TODAY = date(2026, 11, 10)


def test_ladder():
    assert [interval_for_step(step) for step in range(7)] == [
        1,
        3,
        7,
        14,
        30,
        60,
        120,
    ]


def test_remembered_advances_step_and_interval():
    update = apply_feedback(2, 0, 0, "remembered", TODAY)

    assert update.step == 3
    assert update.interval_days == 14
    assert update.due_at == "2026-11-24"
    assert update.reps == 1


def test_remembered_caps_at_max_step():
    update = apply_feedback(MAX_STEP, 3, 0, "remembered", TODAY)

    assert update.step == MAX_STEP
    assert update.interval_days == 120


def test_fuzzy_keeps_step_and_halves_interval():
    update = apply_feedback(2, 0, 0, "fuzzy", TODAY)

    assert update.step == 2
    assert update.interval_days == 3  # 7 // 2
    assert update.due_at == "2026-11-13"


def test_fuzzy_has_minimum_one_day():
    update = apply_feedback(0, 0, 0, "fuzzy", TODAY)

    assert update.interval_days == 1


def test_forgot_drops_two_steps_and_due_tomorrow():
    update = apply_feedback(3, 1, 0, "forgot", TODAY)

    assert update.step == 1
    assert update.interval_days == 1
    assert update.due_at == "2026-11-11"
    assert update.lapses == 1


def test_forgot_floors_at_zero():
    update = apply_feedback(1, 0, 0, "forgot", TODAY)

    assert update.step == 0


def test_natural_review_counts_as_remembered():
    update = natural_review_update(1, 2, 0, TODAY)

    assert update.step == 2
    assert update.reps == 3
    assert update.lapses == 0
    assert update.due_at == "2026-11-17"


def test_unknown_feedback_raises():
    with pytest.raises(ValueError):
        apply_feedback(0, 0, 0, "nope", TODAY)
