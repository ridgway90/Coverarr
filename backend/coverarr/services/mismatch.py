"""Same file, different title: spotting items a server has matched to the wrong film or show.

All servers point at the same media, so if one file is "9" on Jellyfin but "9 Songs" on Plex,
one of them is wrong. Coverarr suggests which, with its reasons, but never applies a fix
without the user confirming it.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import MediaItem, ServerItem
from coverarr.services.files import file_hints
from coverarr.services.matching import normalize_title

ID_LABELS = {"tmdb_id": "tmdb", "tvdb_id": "tvdb", "imdb_id": "imdb"}


def suggest(candidates: list[dict[str, Any]], hints: dict[str, Any], total_servers: int) -> None:
    """Score candidates in place; set ``suggested``/``reasons`` on each."""
    hint_ids = {k: hints[k] for k in ID_LABELS if hints.get(k)}
    counts = [len(c["servers"]) for c in candidates]
    for c in candidates:
        score, reasons = 0, []
        id_match = [k for k, v in hint_ids.items() if c.get(k) == v]
        if id_match:
            k = id_match[0]
            score += 100
            reasons.append(f"File name says {{{ID_LABELS[k]}-{hint_ids[k]}}}")
        elif hint_ids:
            k = next(iter(hint_ids))
            reasons.append(f"File name says {{{ID_LABELS[k]}-{hint_ids[k]}}}, not this")
        others = [n for n, other in zip(counts, candidates, strict=True) if other is not c]
        if len(c["servers"]) > max(others, default=0):
            score += 30
            reasons.append(f"{len(c['servers'])} of {total_servers} servers agree")
        if hints.get("title") and normalize_title(c["title"]) == normalize_title(hints["title"]):
            score += 20
            reasons.append("Title matches the file name")
        if hints.get("year") and c.get("year") == hints["year"]:
            score += 10
            reasons.append("Year matches the file name")
        c["score"], c["reasons"] = score, reasons


def pick_suggestion(candidates: list[dict[str, Any]]) -> tuple[int | None, str]:
    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)
    if (
        not ranked
        or ranked[0]["score"] == 0
        or (len(ranked) > 1 and ranked[0]["score"] == ranked[1]["score"])
    ):
        return None, "none"
    best = ranked[0]["score"]
    confidence = "high" if best >= 120 else "medium" if best >= 50 else "low"
    return ranked[0]["media_item_id"], confidence


async def compute_mismatches(
    session: AsyncSession, enabled: set[int]
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    """Mismatch issues keyed like other issues, and the media items they involve."""
    rows = (
        await session.execute(
            select(
                ServerItem.id,
                ServerItem.server_id,
                ServerItem.file_key,
                ServerItem.path,
                ServerItem.poster_ref,
                ServerItem.kind,
                MediaItem.id.label("mi_id"),
                MediaItem.library_id,
                MediaItem.title,
                MediaItem.year,
                MediaItem.tmdb_id,
                MediaItem.tvdb_id,
                MediaItem.imdb_id,
            )
            .join(MediaItem, MediaItem.id == ServerItem.media_item_id)
            .where(
                ServerItem.kind.in_(("movie", "show")),
                ServerItem.file_key.is_not(None),
                ServerItem.server_id.in_(enabled),
            )
            .order_by(ServerItem.server_id)
        )
    ).all()
    groups: dict[tuple[int, str], list] = defaultdict(list)
    for r in rows:
        groups[(r.library_id, r.file_key)].append(r)

    issues: dict[str, dict[str, Any]] = {}
    involved: set[int] = set()
    for (library_id, key), group in groups.items():
        if len({r.mi_id for r in group}) < 2:
            continue
        by_item: dict[int, dict[str, Any]] = {}
        for r in group:
            c = by_item.setdefault(
                r.mi_id,
                {
                    "media_item_id": r.mi_id,
                    "title": r.title,
                    "year": r.year,
                    "tmdb_id": r.tmdb_id,
                    "tvdb_id": r.tvdb_id,
                    "imdb_id": r.imdb_id,
                    "servers": [],
                    "server_items": [],
                },
            )
            c["servers"].append(r.server_id)
            c["server_items"].append(
                {
                    "server_id": r.server_id,
                    "server_item_id": r.id,
                    "has_poster": bool(r.poster_ref),
                    "poster_version": (r.poster_ref or "")[-24:],
                }
            )
        candidates = list(by_item.values())
        kind = group[0].kind
        hints = file_hints(group[0].path, kind)
        hint_dict = {
            "tmdb_id": hints.tmdb_id,
            "tvdb_id": hints.tvdb_id,
            "imdb_id": hints.imdb_id,
            "title": hints.title,
            "year": hints.year,
        }
        suggest(candidates, hint_dict, len({r.server_id for r in group}))
        suggested, confidence = pick_suggestion(candidates)
        involved |= set(by_item)
        digest = hashlib.sha1(key.encode()).hexdigest()[:16]
        label = hints.title or group[0].path.replace("\\", "/").rstrip("/").split("/")[-1]
        issues[f"mismatch:{library_id}:{digest}"] = {
            "type": "mismatch",
            "title": f"{label} ({hints.year})" if hints.year and hints.title else label,
            "library_id": library_id,
            "details": {
                "kind": kind,
                "path": group[0].path,
                "hints": hint_dict,
                "candidates": candidates,
                "suggested": suggested,
                "confidence": confidence,
            },
        }
    return issues, involved
