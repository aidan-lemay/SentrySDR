"""Decide which recorder owns each source at a given moment.

Pure functions, so the override and revert behaviour can be tested directly.
An active scheduled recorder overrides the source's base assignment; when its
window ends the base assignment applies again automatically.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Protocol


class RecorderLike(Protocol):
    id: int
    source_id: int
    enabled: bool
    schedule_days: list | None
    schedule_start: str | None
    schedule_end: str | None


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def valid_time(value: str) -> bool:
    try:
        hours, minutes = value.split(":")
        return len(value) == 5 and 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59
    except ValueError:
        return False


def is_window_active(days: list[int], start: str, end: str, local: datetime) -> bool:
    """A window belongs to the day it starts on and may run past midnight.

    An end equal to the start means a full 24 hours.
    """
    begin, finish = _minutes(start), _minutes(end)
    current = local.hour * 60 + local.minute
    if begin < finish:
        return local.weekday() in days and begin <= current < finish
    if local.weekday() in days and current >= begin:
        return True
    return (local - timedelta(days=1)).weekday() in days and current < finish


def is_scheduled(recorder: RecorderLike) -> bool:
    return recorder.schedule_days is not None


def resolve(recorders: Iterable[RecorderLike], local: datetime) -> dict[int, RecorderLike]:
    """Return {source_id: the recorder in control}. The lowest id wins a tie."""
    base: dict[int, RecorderLike] = {}
    active: dict[int, RecorderLike] = {}
    for rec in sorted(recorders, key=lambda r: r.id):
        if not rec.enabled:
            continue
        if not is_scheduled(rec):
            base.setdefault(rec.source_id, rec)
        elif is_window_active(rec.schedule_days, rec.schedule_start, rec.schedule_end, local):
            active.setdefault(rec.source_id, rec)
    return {**base, **active}
