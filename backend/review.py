"""复习调度算法（设计文档第 6 节）。

里程碑 1 只用到自然复习；三按钮反馈留给调度器模块复用同一套阶梯。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, timedelta

INTERVALS = (1, 3, 7, 14, 30, 60, 120)
MAX_STEP = len(INTERVALS) - 1


def interval_for_step(step: int) -> int:
    return INTERVALS[max(0, min(step, MAX_STEP))]


def next_due(step: int, from_date: Date) -> str:
    return (from_date + timedelta(days=interval_for_step(step))).isoformat()


@dataclass(frozen=True)
class ReviewUpdate:
    step: int
    interval_days: int
    due_at: str
    reps: int
    lapses: int


def natural_review_update(
    step: int, reps: int, lapses: int, today: Date
) -> ReviewUpdate:
    """当天再次提到某个节点：按一次“记得”处理。"""
    new_step = min(step + 1, MAX_STEP)
    interval = interval_for_step(new_step)
    return ReviewUpdate(
        step=new_step,
        interval_days=interval,
        due_at=(today + timedelta(days=interval)).isoformat(),
        reps=reps + 1,
        lapses=lapses,
    )


def apply_feedback(
    step: int,
    reps: int,
    lapses: int,
    feedback: str,
    today: Date,
) -> ReviewUpdate:
    if feedback == "remembered":
        new_step = min(step + 1, MAX_STEP)
        interval = interval_for_step(new_step)
        new_reps, new_lapses = reps + 1, lapses
    elif feedback == "fuzzy":
        new_step = step
        interval = max(1, interval_for_step(step) // 2)
        new_reps, new_lapses = reps, lapses
    elif feedback == "forgot":
        new_step = max(0, step - 2)
        interval = 1
        new_reps, new_lapses = reps, lapses + 1
    else:
        raise ValueError(f"未知反馈: {feedback!r}")

    return ReviewUpdate(
        step=new_step,
        interval_days=interval,
        due_at=(today + timedelta(days=interval)).isoformat(),
        reps=new_reps,
        lapses=new_lapses,
    )
