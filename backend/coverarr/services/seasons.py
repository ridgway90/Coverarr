"""Collections that only exist part of the year (Kometa's ``schedule: range(12/01-12/31)``),
such as Christmas films in December. Outside its dates a collection isn't synced, and with
``delete_not_scheduled`` it's taken off the servers until its season comes round again.
"""

from __future__ import annotations

import re
from datetime import date

_RANGE = re.compile(r"range\(\s*(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})\s*\)", re.IGNORECASE)


class ScheduleError(ValueError):
    pass


Span = tuple[tuple[int, int], tuple[int, int]]


def parse_schedule(text: str) -> list[Span]:
    """``range(12/01-12/31)``, several joined by ``|``, as ((month, day), (month, day)) pairs."""
    spans: list[Span] = []
    for part in str(text).split("|"):
        part = part.strip()
        if not part:
            continue
        m = _RANGE.fullmatch(part)
        if not m:
            raise ScheduleError(
                f"Only date ranges are supported, like range(12/01-12/31); got {part!r}"
            )
        a, b, c, d = (int(g) for g in m.groups())
        for month, day in ((a, b), (c, d)):
            try:
                date(2000, month, day)  # a leap year, so 02/29 is fine
            except ValueError as exc:
                raise ScheduleError(f"Not a date: {month:02d}/{day:02d}") from exc
        spans.append(((a, b), (c, d)))
    if not spans:
        raise ScheduleError("A schedule needs a date range, like range(12/01-12/31)")
    return spans


def format_schedule(spans: list[Span]) -> str:
    return "|".join(f"range({a:02d}/{b:02d}-{c:02d}/{d:02d})" for (a, b), (c, d) in spans)


def in_season(text: str | None, today: date) -> bool:
    """Whether ``today`` falls in the schedule (always, when there's none). A range may wrap
    the new year: range(12/26-01/05)."""
    if not text:
        return True
    try:
        spans = parse_schedule(text)
    except ScheduleError:
        return True
    now = (today.month, today.day)
    for start, end in spans:
        if start <= end:
            if start <= now <= end:
                return True
        elif now >= start or now <= end:
            return True
    return False
