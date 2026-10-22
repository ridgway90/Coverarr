"""Collections built from the library itself ("library_filter"), like Kometa's smart filters
and its dynamic default collections: every title whose genre, year, decade, studio, network,
resolution or content rating matches, or (from the details fetched from TMDb) where it
streams, who directed or stars in it, its country, a show's status or its rating.

A filter is a comma-separated list of conditions, all of which must hold:
``genre=Comedy, decade=1990``, ``year=2000-2009``, ``year>=2015``, ``genre!=Horror``,
``streaming=Netflix``, ``imdb_rating>=8``. Several
``library_filter`` lists on one collection add up (any of them), as all its lists do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

FILTER_KEYS = (
    "genre",
    "year",
    "decade",
    "studio",
    "network",
    "resolution",
    "content_rating",
    "streaming",
    "director",
    "actor",
    "country",
    "status",
    "tmdb_rating",
    "imdb_rating",
    "rt_rating",
)
NUMERIC_KEYS = ("year", "decade", "tmdb_rating", "imdb_rating", "rt_rating")
# Filter key -> the list it's read from (in facts merged with details).
LIST_KEYS = {
    "genre": "genres",
    "streaming": "streaming",
    "director": "directors",
    "actor": "cast",
    "country": "countries",
}
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
        if op in (">=", "<=") and key not in NUMERIC_KEYS:
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
    if key in LIST_KEYS:
        return [str(g) for g in facts.get(LIST_KEYS[key]) or []]
    value = facts.get(key)
    return [str(value)] if value not in (None, "") else []


def _number(text: str) -> float | None:
    try:
        return float(text.strip().rstrip("%"))
    except ValueError:
        return None


def _matches(cond: Condition, year: int | None, facts: dict[str, Any]) -> bool:
    have = _values(cond.key, year, facts)
    numeric = cond.key in NUMERIC_KEYS and (cond.op in (">=", "<=") or "-" in cond.value.strip("-"))
    if numeric:
        numbers = [n for n in map(_number, have) if n is not None]
        if not numbers:
            return False
        n = numbers[0]
        if cond.op in ("=", "!="):
            low, _, high = cond.value.partition("-")
            lo, hi = _number(low), _number(high or low)
            inside = lo is not None and hi is not None and lo <= n <= hi
            return inside if cond.op == "=" else not inside
        bound = _number(cond.value)
        if bound is None:
            return False
        return n >= bound if cond.op == ">=" else n <= bound
    if cond.key in NUMERIC_KEYS and cond.key not in ("year", "decade"):
        wanted_n = _number(cond.value)
        hit = wanted_n is not None and any(_number(v) == wanted_n for v in have)
        return hit if cond.op == "=" else not hit
    wanted = cond.value.casefold()
    hit = any(v.casefold() == wanted for v in have)
    return hit if cond.op == "=" else not hit


def matches(conditions: list[Condition], year: int | None, facts: dict[str, Any] | None) -> bool:
    return all(_matches(c, year, facts or {}) for c in conditions)
