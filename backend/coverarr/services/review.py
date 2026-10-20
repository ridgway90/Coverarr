"""Discrepancy detection across servers.

Issue types:

* ``mismatch``                the same file is matched to different titles on different servers
* ``item_missing``            a movie/show is absent (or unmatched) on some servers
* ``poster_differs``          the same movie/show has visibly different posters per server
* ``collection_missing``      a collection exists on some servers but not others
* ``collection_members_differ`` a collection's members differ between servers
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import (
    Collection,
    Issue,
    MediaItem,
    Server,
    ServerCollection,
    ServerItem,
    ServerLibrary,
)
from coverarr.services.mismatch import compute_mismatches
from coverarr.services.posters import hamming
from coverarr.services.settings_store import get_setting

ISSUE_TYPES = (
    "mismatch",
    "item_missing",
    "poster_differs",
    "collection_missing",
    "collection_members_differ",
)
MAX_LISTED_MEMBERS = 50


def _has_overlay(labels: list[str] | None) -> bool:
    return any(str(label).lower() == "overlay" for label in labels or [])


def poster_comparison(entries: list[dict[str, Any]], threshold: int) -> dict[str, Any] | None:
    """Compare per-server posters. Returns details when they differ, ``None`` when they match
    or when not every poster has been hashed yet."""
    with_poster = [e for e in entries if e["has_poster"]]
    if not with_poster:
        return None
    if any(e["hash"] is None for e in with_poster):
        return None  # poster scan hasn't covered these yet
    distance = 0
    for i, a in enumerate(with_poster):
        for b in with_poster[i + 1 :]:
            distance = max(distance, hamming(a["hash"], b["hash"]))
    missing_poster = len(with_poster) < len(entries)
    if distance <= threshold and not missing_poster:
        return None
    return {"servers": entries, "distance": distance, "missing_poster": missing_poster}


async def _library_servers(session: AsyncSession) -> dict[int, set[int]]:
    rows = await session.execute(
        select(ServerLibrary.library_id, ServerLibrary.server_id)
        .join(Server, Server.id == ServerLibrary.server_id)
        .where(
            ServerLibrary.enabled.is_(True),
            Server.enabled.is_(True),
            ServerLibrary.library_id.is_not(None),
        )
    )
    result: dict[int, set[int]] = defaultdict(set)
    for lib_id, server_id in rows:
        result[lib_id].add(server_id)
    return result


async def compute_issues(session: AsyncSession) -> dict[str, dict[str, Any]]:
    threshold = int(await get_setting(session, "poster_match_threshold"))
    lib_servers = await _library_servers(session)
    enabled = set(await session.scalars(select(Server.id).where(Server.enabled.is_(True))))
    # Titles caught up in a same-file mismatch aren't really missing; don't list them twice.
    issues, mismatched = await compute_mismatches(session, enabled)

    # --- movies / shows ---------------------------------------------------------------------
    rows = (
        await session.execute(
            select(
                MediaItem.id,
                MediaItem.library_id,
                MediaItem.title,
                MediaItem.year,
                MediaItem.tmdb_id,
                MediaItem.tvdb_id,
                MediaItem.imdb_id,
                MediaItem.poster_source,
                ServerItem.id.label("si_id"),
                ServerItem.server_id,
                ServerItem.poster_ref,
                ServerItem.poster_hash,
                ServerItem.poster_hash_ref,
                ServerItem.labels,
            )
            .join(ServerItem, ServerItem.media_item_id == MediaItem.id)
            .where(MediaItem.kind.in_(("movie", "show")), ServerItem.server_id.in_(enabled))
            .order_by(MediaItem.id, ServerItem.server_id)
        )
    ).all()
    grouped: dict[int, list] = defaultdict(list)
    for r in rows:
        grouped[r.id].append(r)

    # (server_id, media_item_id) presence, used for collection member checks below.
    on_server: dict[int, set[int]] = defaultdict(set)
    titles: dict[int, str] = {}
    for mi_id, rs in grouped.items():
        first = rs[0]
        label = f"{first.title} ({first.year})" if first.year else first.title
        titles[mi_id] = label
        present = {r.server_id for r in rs}
        for sid in present:
            on_server[sid].add(mi_id)
        missing = lib_servers.get(first.library_id, set()) - present
        if missing and mi_id not in mismatched:
            issues[f"item_missing:{mi_id}"] = {
                "type": "item_missing",
                "title": label,
                "library_id": first.library_id,
                "media_item_id": mi_id,
                "details": {
                    "present": sorted(present),
                    "missing": sorted(missing),
                    "no_ids": not (first.tmdb_id or first.tvdb_id or first.imdb_id),
                },
            }
        if len(present) >= 2 and first.poster_source is None:
            entries = [
                {
                    "server_id": r.server_id,
                    "server_item_id": r.si_id,
                    "has_poster": bool(r.poster_ref),
                    "hash": r.poster_hash if r.poster_hash_ref == r.poster_ref else None,
                    "kometa_overlay": _has_overlay(r.labels),
                }
                for r in rs
            ]
            diff = poster_comparison(entries, threshold)
            if diff:
                issues[f"poster_differs:item:{mi_id}"] = {
                    "type": "poster_differs",
                    "title": label,
                    "library_id": first.library_id,
                    "media_item_id": mi_id,
                    "details": {"target": "item", **diff},
                }

    # --- collections ------------------------------------------------------------------------
    member_map: dict[tuple[int, str], int] = {
        (sid, rid): mi
        for sid, rid, mi in (
            await session.execute(
                select(ServerItem.server_id, ServerItem.remote_id, ServerItem.media_item_id).where(
                    ServerItem.kind.in_(("movie", "show")), ServerItem.media_item_id.is_not(None)
                )
            )
        ).all()
    }
    collections = {c.id: c for c in await session.scalars(select(Collection))}
    by_collection: dict[int, list[ServerCollection]] = defaultdict(list)
    for sc in await session.scalars(
        select(ServerCollection)
        .where(ServerCollection.collection_id.is_not(None), ServerCollection.server_id.in_(enabled))
        .order_by(ServerCollection.server_id)
    ):
        by_collection[sc.collection_id].append(sc)

    for cid, scs in by_collection.items():
        coll = collections[cid]
        members = {
            sc.server_id: {
                mi
                for rid in sc.member_remote_ids or []
                if (mi := member_map.get((sc.server_id, rid)))
            }
            for sc in scs
        }
        union = set().union(*members.values())
        present = set(members)
        missing = lib_servers.get(coll.library_id, set()) - present
        if missing:
            issues[f"collection_missing:{cid}"] = {
                "type": "collection_missing",
                "title": coll.name,
                "library_id": coll.library_id,
                "collection_id": cid,
                "details": {
                    "present": sorted(present),
                    "missing": sorted(missing),
                    "member_count": len(union),
                    "smart_on": sorted(sc.server_id for sc in scs if sc.smart),
                    # How many of the members exist on each missing server.
                    "available": {str(sid): len(union & on_server[sid]) for sid in missing},
                },
            }
        if len(present) >= 2:
            gaps = {}
            for sc in scs:
                if sc.smart:
                    continue  # rule-based; members can't be added by hand
                lacking = (union & on_server[sc.server_id]) - members[sc.server_id]
                if lacking:
                    ordered = sorted(lacking, key=lambda m: titles.get(m, ""))
                    gaps[str(sc.server_id)] = {
                        "count": len(lacking),
                        "items": [
                            {"id": m, "title": titles.get(m, "?")}
                            for m in ordered[:MAX_LISTED_MEMBERS]
                        ],
                    }
            if gaps:
                issues[f"collection_members_differ:{cid}"] = {
                    "type": "collection_members_differ",
                    "title": coll.name,
                    "library_id": coll.library_id,
                    "collection_id": cid,
                    "details": {"missing": gaps, "member_count": len(union)},
                }
            if coll.poster_source is None:
                entries = [
                    {
                        "server_id": sc.server_id,
                        "server_collection_id": sc.id,
                        "has_poster": bool(sc.poster_ref),
                        "hash": sc.poster_hash if sc.poster_hash_ref == sc.poster_ref else None,
                        "kometa_overlay": _has_overlay(sc.labels),
                    }
                    for sc in scs
                ]
                diff = poster_comparison(entries, threshold)
                if diff:
                    issues[f"poster_differs:collection:{cid}"] = {
                        "type": "poster_differs",
                        "title": coll.name,
                        "library_id": coll.library_id,
                        "collection_id": cid,
                        "details": {"target": "collection", **diff},
                    }
    return issues


async def refresh_issues(session: AsyncSession) -> dict[str, int]:
    """Recompute issues, keeping 'ignored' status for issues that still apply."""
    fresh = await compute_issues(session)
    existing = {i.key: i for i in await session.scalars(select(Issue))}
    for key, data in fresh.items():
        issue = existing.pop(key, None)
        if issue is None:
            session.add(Issue(key=key, status="open", **data))
            continue
        for field_name in ("type", "title", "library_id", "details"):
            setattr(issue, field_name, data[field_name])
        issue.media_item_id = data.get("media_item_id")
        issue.collection_id = data.get("collection_id")
    for stale in existing.values():
        await session.delete(stale)
    await session.flush()
    counts = Counter(d["type"] for d in fresh.values())
    return {t: counts.get(t, 0) for t in ISSUE_TYPES}
