"""Match per-server rows into logical media items and collections.

Runs entirely against the database (no server calls), so it is cheap to re-run after a
library is remapped in the UI. Existing links are kept when still consistent, which keeps
logical ids (and any user decisions attached to them) stable across imports.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import (
    Collection,
    Library,
    MediaItem,
    ServerCollection,
    ServerItem,
    ServerLibrary,
)

ID_KEYS = ("tmdb_id", "tvdb_id", "imdb_id")
ID_PRIORITY = {
    "movie": ("tmdb_id", "imdb_id", "tvdb_id"),
    "show": ("tvdb_id", "tmdb_id", "imdb_id"),
}
COLLECTION_OVERLAP = 0.5


def normalize_title(title: str | None) -> str:
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w\s]", " ", text.replace("&", " and "))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^the ", "", text)


def normalize_collection_name(name: str | None) -> str:
    return re.sub(r" collection$", "", normalize_title(name)).strip()


@dataclass
class MatchStats:
    created: int = 0
    linked: int = 0
    removed: int = 0
    unlinked: int = 0
    by_kind: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "linked": self.linked,
            "removed": self.removed,
            "unlinked": self.unlinked,
            "items": dict(self.by_kind),
        }


async def _bulk_insert_items(session: AsyncSession, rows: list[dict[str, Any]]) -> list[int]:
    if not rows:
        return []
    ids: list[int] = []
    for start in range(0, len(rows), 500):
        chunk = rows[start : start + 500]
        result = await session.execute(
            insert(MediaItem).returning(MediaItem.id, sort_by_parameter_order=True), chunk
        )
        ids.extend(result.scalars().all())
    return ids


async def _bulk_update(session: AsyncSession, model, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), 500):
        await session.execute(update(model), rows[start : start + 500])


def _ids(row) -> dict[str, str]:
    return {k: getattr(row, k) for k in ID_KEYS if getattr(row, k)}


def _compatible(a: dict[str, str], b: dict[str, str]) -> bool:
    return all(a[k] == b[k] for k in a.keys() & b.keys())


async def _library_server_libs(session: AsyncSession, library_id: int) -> list[int]:
    return list(
        await session.scalars(
            select(ServerLibrary.id).where(
                ServerLibrary.library_id == library_id, ServerLibrary.enabled.is_(True)
            )
        )
    )


async def match_top_level(
    session: AsyncSession, library: Library, sl_ids: list[int], stats: MatchStats
) -> dict[tuple[int, str], int]:
    """Link movies/shows; returns (server_id, remote_id) -> media_item_id."""
    kind = library.kind
    rows = (
        await session.execute(
            select(
                ServerItem.id,
                ServerItem.server_id,
                ServerItem.remote_id,
                ServerItem.title,
                ServerItem.sort_title,
                ServerItem.year,
                ServerItem.tmdb_id,
                ServerItem.tvdb_id,
                ServerItem.imdb_id,
                ServerItem.media_item_id,
            )
            .where(ServerItem.server_library_id.in_(sl_ids), ServerItem.kind == kind)
            .order_by(ServerItem.server_id, ServerItem.id)
        )
    ).all()
    existing = {
        r.id: r
        for r in (
            await session.execute(
                select(
                    MediaItem.id,
                    MediaItem.title,
                    MediaItem.sort_title,
                    MediaItem.year,
                    MediaItem.tmdb_id,
                    MediaItem.tvdb_id,
                    MediaItem.imdb_id,
                ).where(MediaItem.library_id == library.id, MediaItem.kind == kind)
            )
        ).all()
    }

    # Media item key: real id for existing items, negative placeholder for new ones.
    agg_ids: dict[int, dict[str, str]] = {}
    index: dict[str, dict[str, int]] = {k: {} for k in ID_KEYS}
    title_index: dict[tuple[str, int | None], int] = {}
    links: dict[int, int] = {}
    members: dict[int, list] = defaultdict(list)
    next_temp = -1

    def register(mi: int, row) -> None:
        ids = agg_ids.setdefault(mi, {})
        for k, v in _ids(row).items():
            ids.setdefault(k, v)
            index[k].setdefault(v, mi)
        title_index.setdefault((normalize_title(row.title), row.year), mi)
        links[row.id] = mi
        members[mi].append(row)

    # Keep existing links first, best-supported items first: when a server re-matches an
    # item, its row must join the item the other servers agree on rather than drag its old
    # record along to the new ids (which would duplicate the title).
    support = Counter(row.media_item_id for row in rows if row.media_item_id in existing)
    pending = []
    for row in sorted(rows, key=lambda r: -support.get(r.media_item_id, 0)):
        mi = row.media_item_id
        ids = _ids(row)
        claimed_elsewhere = any(index[k].get(v) not in (None, mi) for k, v in ids.items())
        if mi in existing and _compatible(agg_ids.get(mi, {}), ids) and not claimed_elsewhere:
            register(mi, row)
        else:
            pending.append(row)

    for row in pending:
        ids = _ids(row)
        mi = None
        for key in ID_PRIORITY[kind]:
            if key in ids and ids[key] in index[key]:
                candidate = index[key][ids[key]]
                if _compatible(agg_ids.get(candidate, {}), ids):
                    mi = candidate
                    break
        if mi is None and not ids:
            mi = title_index.get((normalize_title(row.title), row.year))
        if mi is None:
            mi = next_temp
            next_temp -= 1
        register(mi, row)

    # Create new logical items.
    temp_keys = sorted((k for k in members if k < 0), reverse=True)
    new_rows = []
    for temp in temp_keys:
        first = members[temp][0]
        new_rows.append(
            {
                "library_id": library.id,
                "kind": kind,
                "title": first.title,
                "sort_title": first.sort_title,
                "year": first.year,
                **{k: agg_ids[temp].get(k) for k in ID_KEYS},
            }
        )
    real_ids = await _bulk_insert_items(session, new_rows)
    temp_to_real = dict(zip(temp_keys, real_ids, strict=True))
    stats.created += len(real_ids)

    # Refresh existing items' denormalised fields from their first linked server row.
    item_updates = []
    for mi, rows_for in members.items():
        if mi < 0:
            continue
        first, cur = rows_for[0], existing[mi]
        wanted = {
            "title": first.title,
            "sort_title": first.sort_title,
            "year": first.year,
            **{k: agg_ids[mi].get(k) for k in ID_KEYS},
        }
        if any(getattr(cur, k) != v for k, v in wanted.items()):
            item_updates.append({"id": mi, **wanted})
    await _bulk_update(session, MediaItem, item_updates)

    link_updates = []
    result: dict[tuple[int, str], int] = {}
    for row in rows:
        mi = links[row.id]
        mi = temp_to_real.get(mi, mi)
        result[(row.server_id, row.remote_id)] = mi
        if row.media_item_id != mi:
            link_updates.append({"id": row.id, "media_item_id": mi})
    await _bulk_update(session, ServerItem, link_updates)
    stats.linked += len(link_updates)
    stats.by_kind[kind] = len(members)

    orphans = set(existing) - set(members)
    if orphans:
        await session.execute(delete(MediaItem).where(MediaItem.id.in_(orphans)))
        stats.removed += len(orphans)
    return result


async def match_children(
    session: AsyncSession,
    library: Library,
    sl_ids: list[int],
    shows: dict[tuple[int, str], int],
    stats: MatchStats,
) -> None:
    """Link seasons by (show, number) and episodes by (show, season, number)."""
    existing_seasons: dict[tuple[int, int], int] = {}
    existing_episodes: dict[tuple[int, int], int] = {}
    for r in (
        await session.execute(
            select(
                MediaItem.id,
                MediaItem.kind,
                MediaItem.parent_id,
                MediaItem.season_number,
                MediaItem.episode_number,
            ).where(MediaItem.library_id == library.id, MediaItem.kind.in_(("season", "episode")))
        )
    ).all():
        if r.kind == "season" and r.season_number is not None:
            existing_seasons[(r.parent_id, r.season_number)] = r.id
        elif r.kind == "episode" and r.episode_number is not None:
            existing_episodes[(r.parent_id, r.episode_number)] = r.id

    async def ensure(cache: dict[tuple[int, int], int], keys: list[tuple[int, int]], make) -> None:
        missing = list(dict.fromkeys(k for k in keys if k not in cache))
        ids = await _bulk_insert_items(session, [make(k) for k in missing])
        cache.update(zip(missing, ids, strict=True))
        stats.created += len(ids)

    def season_row(key: tuple[int, int]) -> dict[str, Any]:
        show_id, number = key
        title = "Specials" if number == 0 else f"Season {number}"
        return {
            "library_id": library.id,
            "kind": "season",
            "title": title,
            "parent_id": show_id,
            "season_number": number,
        }

    seasons = (
        await session.execute(
            select(
                ServerItem.id,
                ServerItem.server_id,
                ServerItem.show_remote_id,
                ServerItem.season_number,
                ServerItem.media_item_id,
            ).where(ServerItem.server_library_id.in_(sl_ids), ServerItem.kind == "season")
        )
    ).all()
    episodes = (
        await session.execute(
            select(
                ServerItem.id,
                ServerItem.server_id,
                ServerItem.show_remote_id,
                ServerItem.season_number,
                ServerItem.episode_number,
                ServerItem.title,
                ServerItem.media_item_id,
            ).where(ServerItem.server_library_id.in_(sl_ids), ServerItem.kind == "episode")
        )
    ).all()

    def season_key(row) -> tuple[int, int] | None:
        show_mi = shows.get((row.server_id, row.show_remote_id))
        if show_mi is None or row.season_number is None:
            return None
        return (show_mi, row.season_number)

    season_keys = [k for r in seasons if (k := season_key(r))]
    season_keys += [k for r in episodes if (k := season_key(r)) and r.episode_number is not None]
    await ensure(existing_seasons, season_keys, season_row)

    episode_key: dict[int, tuple[int, int]] = {}
    titles: dict[tuple[int, int], str] = {}
    for r in episodes:
        sk = season_key(r)
        if sk and r.episode_number is not None:
            key = (existing_seasons[sk], r.episode_number)
            episode_key[r.id] = key
            titles.setdefault(key, r.title)
    await ensure(
        existing_episodes,
        list(episode_key.values()),
        lambda key: {
            "library_id": library.id,
            "kind": "episode",
            "title": titles[key],
            "parent_id": key[0],
            "season_number": None,
            "episode_number": key[1],
        },
    )

    link_updates = []
    used: set[int] = set()
    for r in seasons:
        sk = season_key(r)
        mi = existing_seasons[sk] if sk else None
        if mi:
            used.add(mi)
        if r.media_item_id != mi:
            link_updates.append({"id": r.id, "media_item_id": mi})
    for r in episodes:
        key = episode_key.get(r.id)
        mi = existing_episodes[key] if key else None
        if mi:
            used.add(mi)
            used.add(key[0])
        if r.media_item_id != mi:
            link_updates.append({"id": r.id, "media_item_id": mi})
    await _bulk_update(session, ServerItem, link_updates)
    stats.linked += len(link_updates)
    stats.unlinked += sum(1 for u in link_updates if u["media_item_id"] is None)
    stats.by_kind["season"] = len({v for v in existing_seasons.values() if v in used})
    stats.by_kind["episode"] = len({v for v in existing_episodes.values() if v in used})

    orphans = (set(existing_seasons.values()) | set(existing_episodes.values())) - used
    if orphans:
        await session.execute(delete(MediaItem).where(MediaItem.id.in_(orphans)))
        stats.removed += len(orphans)


async def match_collections(session: AsyncSession, stats: MatchStats) -> None:
    sl_to_lib = dict(
        (await session.execute(select(ServerLibrary.id, ServerLibrary.library_id))).all()
    )
    member_map: dict[tuple[int, str], tuple[int, int]] = {}
    for r in (
        await session.execute(
            select(
                ServerItem.server_id,
                ServerItem.remote_id,
                ServerItem.media_item_id,
                MediaItem.library_id,
            )
            .join(MediaItem, MediaItem.id == ServerItem.media_item_id)
            .where(ServerItem.kind.in_(("movie", "show")))
        )
    ).all():
        member_map[(r.server_id, r.remote_id)] = (r.media_item_id, r.library_id)

    server_colls = list(
        await session.scalars(
            select(ServerCollection).order_by(ServerCollection.server_id, ServerCollection.id)
        )
    )
    collections = {c.id: c for c in await session.scalars(select(Collection))}
    # What each logical collection gathered during this pass: members, servers and the
    # (casefolded) server-side names linked to it.
    coll_members: dict[int, set[int]] = defaultdict(set)
    coll_servers: dict[int, set[int]] = defaultdict(set)
    coll_names: dict[int, set[str]] = defaultdict(set)

    def infer_library(sc: ServerCollection, members: list[tuple[int, int]]) -> int | None:
        if sc.server_library_id and sl_to_lib.get(sc.server_library_id):
            return sl_to_lib[sc.server_library_id]
        votes = Counter(lib for _, lib in members)
        return votes.most_common(1)[0][0] if votes else None

    def score(coll: Collection, sc: ServerCollection, norm: str, members: set[int]) -> float:
        """How well a server collection fits a logical one; 0 means it doesn't."""
        names = coll_names[coll.id] or {coll.name.casefold()}
        mset = coll_members[coll.id]
        inter = len(members & mset)
        overlap = inter / len(members | mset) if mset and members else 0.0
        same_norm = coll.normalized_name == norm
        if not same_norm and not (inter >= 2 and overlap >= COLLECTION_OVERLAP):
            return 0.0
        value = 0.5 * same_norm + overlap
        if sc.name.casefold() in names:
            value += 1.0
        if coll.id == sc.collection_id:
            value += 0.25  # prefer the existing link, keeping ids stable
        return value

    def best_match(sc: ServerCollection, lib_id: int | None, norm: str, members: set[int]):
        candidates = [
            c
            for c in collections.values()
            # A server never has two copies of one collection: if it already has one here,
            # this must be a different collection (e.g. a franchise vs its "universe").
            if sc.server_id not in coll_servers[c.id] and (c.library_id == lib_id or lib_id is None)
        ]
        scored = [(score(c, sc, norm, members), c) for c in candidates]
        scored = [(v, c) for v, c in scored if v > 0]
        if lib_id is None:
            # Empty box sets: only an unambiguous name match.
            scored = [(v, c) for v, c in scored if c.normalized_name == norm]
            return scored[0][1] if len(scored) == 1 else None
        return max(scored, key=lambda vc: vc[0])[1] if scored else None

    resolved: list[tuple[ServerCollection, Collection | None, int | None]] = []
    # Plex rows (library known) first so name-only fallbacks for empty box sets can find them.
    ordered = sorted(
        server_colls, key=lambda sc: (sc.server_library_id is None, sc.server_id, sc.id)
    )
    for sc in ordered:
        members = [
            m for rid in sc.member_remote_ids or [] if (m := member_map.get((sc.server_id, rid)))
        ]
        member_ids = {mi for mi, _ in members}
        lib_id = infer_library(sc, members)
        norm = normalize_collection_name(sc.name)
        coll = best_match(sc, lib_id, norm, member_ids)
        if coll is None and lib_id is not None:
            coll = Collection(
                library_id=lib_id,
                name=sc.name,
                normalized_name=norm,
                sort_title=sc.sort_title,
                summary=sc.summary,
            )
            session.add(coll)
            await session.flush()
            collections[coll.id] = coll
            stats.created += 1
        if coll is not None:
            if not coll_servers[coll.id]:
                # The first server's collection names the logical one.
                coll.name, coll.normalized_name = sc.name, norm
            coll_members[coll.id] |= member_ids
            coll_servers[coll.id].add(sc.server_id)
            coll_names[coll.id].add(sc.name.casefold())
        resolved.append((sc, coll, lib_id))

    for sc, coll, _ in resolved:
        new_id = coll.id if coll else None
        if sc.collection_id != new_id:
            sc.collection_id = new_id
            stats.linked += 1
    stats.by_kind["collection"] = len(coll_servers)

    orphans = set(collections) - set(coll_servers)
    if orphans:
        await session.execute(delete(Collection).where(Collection.id.in_(orphans)))
        stats.removed += len(orphans)


async def match_all(session: AsyncSession, library_ids: list[int] | None = None) -> dict[str, Any]:
    """Re-link everything, or only ``library_ids`` (collections are always re-linked)."""
    stats = MatchStats()
    query = select(Library).order_by(Library.id)
    if library_ids is not None:
        query = query.where(Library.id.in_(library_ids))
    libraries = list(await session.scalars(query))
    for library in libraries:
        sl_ids = await _library_server_libs(session, library.id)
        shows = await match_top_level(session, library, sl_ids, stats)
        if library.kind == "show":
            await match_children(session, library, sl_ids, shows, stats)
    await session.flush()
    await match_collections(session, stats)
    await session.flush()
    return stats.as_dict()
