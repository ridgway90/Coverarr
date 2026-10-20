"""What overlays need to know about a title: resolution, HDR / Dolby Vision, edition, network.

Each server row keeps what that server reported (``ServerItem.media``); the file name adds what
Radarr/Sonarr wrote into it (``[Bluray-2160p][DV HDR10]``, ``{edition-Director's Cut}``).
``merge`` combines them into one set of facts per title, and ``aggregate`` rolls episodes up to
their season and show.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import MediaItem, Server, ServerItem

RESOLUTIONS = ("4k", "1080p", "720p", "576p", "480p")
RANK = {r: i for i, r in enumerate(reversed(RESOLUTIONS))}  # higher is better

_RES_PATTERNS = (
    ("4k", re.compile(r"(?<![a-z0-9])(2160p|4k|uhd)(?![a-z0-9])", re.IGNORECASE)),
    ("1080p", re.compile(r"(?<![a-z0-9])1080[pi](?![a-z0-9])", re.IGNORECASE)),
    ("720p", re.compile(r"(?<![a-z0-9])720p(?![a-z0-9])", re.IGNORECASE)),
    ("576p", re.compile(r"(?<![a-z0-9])576[pi](?![a-z0-9])", re.IGNORECASE)),
    ("480p", re.compile(r"(?<![a-z0-9])480[pi](?![a-z0-9])", re.IGNORECASE)),
)
_DV = re.compile(r"(?<![a-z0-9])(dv|dovi|dolby[ ._-]?vision)(?![a-z0-9])", re.IGNORECASE)
_HDR10PLUS = re.compile(r"hdr10(\+|plus)", re.IGNORECASE)
_HDR = re.compile(r"(?<![a-z0-9])(hdr(10)?|hlg|pq)(?![a-z0-9])", re.IGNORECASE)
_EDITION_TAG = re.compile(r"\{edition-([^}]+)\}", re.IGNORECASE)

# Edition names as Plex/Radarr write them -> the jmxd/Kometa edition image key.
EDITIONS: list[tuple[str, re.Pattern[str]]] = [
    (key, re.compile(pattern, re.IGNORECASE))
    for key, pattern in (
        ("richarddonner", r"richard\s*donner"),
        ("directors", r"director'?s"),
        ("extended", r"extended"),
        ("theatrical", r"theatrical"),
        ("unrated", r"unrated"),
        ("uncut", r"uncut"),
        ("imax", r"imax"),
        ("criterion", r"criterion"),
        ("remastered", r"remaster"),
        ("anniversary", r"anniversary"),
        ("collector", r"collector'?s"),
        ("ultimate", r"ultimate"),
        ("final", r"final\s*cut"),
        ("special", r"special\s*edition"),
        ("international", r"international"),
        ("alternate", r"alternate|alternative"),
        ("definitive", r"definitive"),
        ("diamond", r"diamond"),
        ("platinum", r"platinum"),
        ("blackchrome", r"black\s*(and|&)?\s*chrome"),
        ("enhanced", r"enhanced"),
        ("coda", r"coda"),
        ("ulysses", r"ulysses"),
    )
]


def edition_key(name: str | None) -> str | None:
    if not name:
        return None
    for key, pattern in EDITIONS:
        if pattern.search(name):
            return key
    return None


def normalize_resolution(value: Any) -> str | None:
    """Plex's ``videoResolution`` ("4k", "1080", "sd"...) or similar -> one of RESOLUTIONS."""
    text = str(value or "").lower().strip()
    if not text:
        return None
    if text in ("4k", "2160", "2160p", "uhd"):
        return "4k"
    digits = re.sub(r"[^0-9]", "", text)
    if digits in ("1080", "720", "576", "480"):
        return f"{digits}p"
    if text == "sd":
        return "480p"
    return None


def resolution_from_size(width: Any, height: Any) -> str | None:
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return None
    if not w and not h:
        return None
    # Width first: scope films are wide but short (1920x800 is still 1080p).
    if w >= 3200 or h >= 2000:
        return "4k"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    if h >= 560:
        return "576p"
    return "480p"


def from_filename(path: str | None) -> dict[str, Any]:
    """Facts Radarr/Sonarr put into the file name."""
    if not path:
        return {}
    name = re.split(r"[\\/]", path)[-1]
    facts: dict[str, Any] = {}
    for res, pattern in _RES_PATTERNS:
        if pattern.search(name):
            facts["resolution"] = res
            break
    if _DV.search(name):
        facts["dv"] = True
    if _HDR10PLUS.search(name):
        facts["hdr10plus"] = True
        facts["hdr"] = True
    elif _HDR.search(name):
        facts["hdr"] = True
    tag = _EDITION_TAG.search(name)
    if tag:
        facts["edition"] = tag.group(1).strip()
    return facts


def merge(server_media: list[dict[str, Any] | None], path: str | None) -> dict[str, Any]:
    """One title's facts. ``server_media`` is in priority order (Plex first)."""
    sources = [m for m in server_media if m] + [from_filename(path)]
    facts: dict[str, Any] = {}
    for key in ("resolution", "edition", "network"):
        facts[key] = next((m[key] for m in sources if m.get(key)), None)
    for flag in ("hdr", "dv", "hdr10plus"):
        facts[flag] = any(m.get(flag) for m in sources)
    return facts


def aggregate(children: list[dict[str, Any]], own: dict[str, Any] | None = None) -> dict[str, Any]:
    """Season/show facts from their episodes: the best resolution any of them has, and HDR/DV
    if any of them has it (like Kometa's show- and season-level overlays)."""
    facts = dict(own or {})
    best = max(
        (c["resolution"] for c in children if c.get("resolution")),
        key=lambda r: RANK.get(r, -1),
        default=None,
    )
    facts["resolution"] = best or facts.get("resolution")
    for flag in ("hdr", "dv", "hdr10plus"):
        facts[flag] = bool(facts.get(flag)) or any(c.get(flag) for c in children)
    facts.setdefault("edition", None)
    facts.setdefault("network", None)
    return facts


def resolution_key(facts: dict[str, Any]) -> str | None:
    """The jmxd/Kometa resolution image name: ``4k``, ``1080pdvhdr``, ``720pplus``, ``dv``..."""
    res = facts.get("resolution")
    dv, hdr, plus = facts.get("dv"), facts.get("hdr"), facts.get("hdr10plus")
    suffix = ""
    if dv and plus:
        suffix = "dvhdrplus"
    elif dv and hdr:
        suffix = "dvhdr"
    elif dv:
        suffix = "dv"
    elif plus:
        suffix = "plus"
    elif hdr:
        suffix = "hdr"
    if not res:
        return suffix or None
    return f"{res}{suffix}"


async def refresh_facts(session: AsyncSession, library_ids: list[int] | None = None) -> int:
    """Recompute ``MediaItem.facts`` from the server rows; returns how many changed."""
    kinds = dict((await session.execute(select(Server.id, Server.kind))).all())
    query = select(MediaItem.id, MediaItem.kind, MediaItem.parent_id, MediaItem.facts)
    rows_query = select(
        ServerItem.media_item_id, ServerItem.server_id, ServerItem.media, ServerItem.path
    ).where(ServerItem.media_item_id.is_not(None))
    if library_ids is not None:
        query = query.where(MediaItem.library_id.in_(library_ids))
        rows_query = rows_query.join(MediaItem, MediaItem.id == ServerItem.media_item_id).where(
            MediaItem.library_id.in_(library_ids)
        )
    items = (await session.execute(query)).all()
    per_item: dict[int, list] = defaultdict(list)
    for r in (await session.execute(rows_query)).all():
        per_item[r.media_item_id].append(r)

    own: dict[int, dict[str, Any]] = {}
    for item in items:
        # Plex first (it knows HDR, DV and editions), then the others in a stable order.
        rows = sorted(
            per_item.get(item.id, []), key=lambda r: (kinds.get(r.server_id) != "plex", r.server_id)
        )
        path = next((r.path for r in rows if r.path), None)
        own[item.id] = merge(
            [r.media for r in rows], path if item.kind in ("movie", "episode") else None
        )

    by_id = {i.id: i for i in items}
    season_eps: dict[int, list] = defaultdict(list)
    show_eps: dict[int, list] = defaultdict(list)
    for item in items:
        if item.kind != "episode" or item.parent_id is None:
            continue
        season_eps[item.parent_id].append(own[item.id])
        season = by_id.get(item.parent_id)
        if season is not None and season.parent_id is not None:
            show_eps[season.parent_id].append(own[item.id])
    facts: dict[int, dict[str, Any]] = {}
    for item in items:
        if item.kind == "season":
            facts[item.id] = aggregate(season_eps.get(item.id, []), own[item.id])
        elif item.kind == "show":
            facts[item.id] = aggregate(show_eps.get(item.id, []), own[item.id])
        else:
            facts[item.id] = own[item.id]

    changed = [
        {"id": item.id, "facts": facts[item.id]} for item in items if item.facts != facts[item.id]
    ]
    if changed:
        await session.execute(update(MediaItem), changed)
    return len(changed)
