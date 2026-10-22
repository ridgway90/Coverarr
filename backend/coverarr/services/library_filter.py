"""Collections built from the library itself ("library_filter"), like Kometa's smart filters
and its dynamic default collections: every title whose genre, year, decade, studio, network,
resolution or content rating matches.

A filter is a comma-separated list of conditions, all of which must hold:
``genre=Comedy, decade=1990``, ``year=2000-2009``, ``year>=2015``, ``genre!=Horror``. Several
``library_filter`` lists on one collection add up (any of them), as all its lists do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

FILTER_KEYS = ("genre", "year", "decade", "studio", "network", "resolution", "content_rating")
_COND = re.compile(r"^\s*(\w+)\s*(!=|>=|<=|=)\s*(.+?)\s*$")


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    key: str
    op: str  # = | != | >= | <=
    value: str


def parse_filter(text: str) -> list[Condition]:
    conditions = []
    for part in str(text).split(","):
        if not part.strip():
            continue
        m = _COND.match(part)
        if not m:
            raise FilterError(f"Not a filter condition: {part.strip()!r} (e.g. genre=Comedy)")
        key, op, value = m.group(1).lower(), m.group(2), m.group(3)
        if key not in FILTER_KEYS:
            raise FilterError(f"Can't filter on {key!r}: use one of {', '.join(FILTER_KEYS)}")
        if op in (">=", "<=") and key not in ("year", "decade"):
            raise FilterError(f"{key} can only be compared with = or !=")
        conditions.append(Condition(key, op, value))
    if not conditions:
        raise FilterError("A library filter needs at least one condition")
    return conditions


def format_filter(conditions: list[Condition]) -> str:
    return ", ".join(f"{c.key}{c.op}{c.value}" for c in conditions)


def _values(key: str, year: int | None, facts: dict[str, Any]) -> list[str]:
    """What a title has for ``key`` (several for genres)."""
    if key == "year":
        return [str(year)] if year else []
    if key == "decade":
        return [str(year // 10 * 10)] if year else []
    if key == "genre":
        return [str(g) for g in facts.get("genres") or []]
    value = facts.get(key)
    return [str(value)] if value else []


def _matches(cond: Condition, year: int | None, facts: dict[str, Any]) -> bool:
    have = _values(cond.key, year, facts)
    if cond.key in ("year", "decade") and cond.op in (">=", "<=") or "-" in cond.value:
        numbers = [int(v) for v in have if v.isdigit()]
        if not numbers:
            return False
        n = numbers[0]
        if "-" in cond.value and cond.op in ("=", "!="):
            low, _, high = cond.value.partition("-")
            inside = int(low) <= n <= int(high or low)
            return inside if cond.op == "=" else not inside
        bound = int(cond.value)
        return n >= bound if cond.op == ">=" else n <= bound
    wanted = cond.value.casefold()
    hit = any(v.casefold() == wanted for v in have)
    return hit if cond.op == "=" else not hit


def matches(conditions: list[Condition], year: int | None, facts: dict[str, Any] | None) -> bool:
    return all(_matches(c, year, facts or {}) for c in conditions)
