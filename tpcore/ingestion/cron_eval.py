"""Cron expression evaluation. Thin wrapper around ``croniter`` so the
rest of the package treats next-fire computation as a pure function.
"""
from __future__ import annotations

from datetime import datetime

from croniter import croniter


def next_run_after(schedule: str, after: datetime) -> datetime:
    """Return the next cron fire strictly after ``after``.

    ``schedule`` is a standard 5-field cron expression — minute, hour,
    day-of-month, month, day-of-week. ``croniter`` accepts the usual
    aliases (``MON-FRI``, ``SUN``).
    """
    if after.tzinfo is None:
        raise ValueError("next_run_after requires a timezone-aware datetime")
    itr = croniter(schedule, after)
    return itr.get_next(datetime)


__all__ = ["next_run_after"]
