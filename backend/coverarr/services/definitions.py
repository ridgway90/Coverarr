"""Collection definitions (rule-based collections, e.g. imported from Kometa).

For now a definition can be checked: its lists are fetched and compared with the library and
with the matching collection on each server. Keeping the collections in sync comes later.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import (
    Collection,
    CollectionDefinition,
    Library,
    MediaItem,
    ServerCollection,
    ServerItem,
    utcnow,
)
from coverarr.services.jobs import JobContext
from coverarr.services.kometa_yaml import ParsedCollection
from coverarr.services.lists import ListClient, ListEntry, ListError
from coverarr.services.matching import normalize_collection_name, normalize_title
from coverarr.services.settings_store import get_setting

MAX_MISSING_LISTED = 100


async def save_definitions(
    session: AsyncSession,
    parsed: list[ParsedCollection],
    files_by_library: dict[str, list[str]],
    default_library_id: int | None,
) -> list[CollectionDefinition]:
    libraries = list(await session.scalars(select(Library)))
    by_name = {normalize_title(lib.name): lib.id for lib in libraries}
    file_library: dict[str, int] = {}
    for lib_name, files in files_by_library.items():
        lib_id = by_name.get(normalize_title(lib_name))
        if lib_id is not None:
            for f in files:
                file_library[f] = lib_id
    if default_library_id is None:
        movie_libs = [lib.id for lib in libraries if lib.kind == "movie"]
        default_library_id = movie_libs[0] if len(movie_libs) == 1 else None

    saved = []
    for p in parsed:
        library_id = file_library.get(p.source_file, default_library_id)
        definition = await session.scalar(
            select(CollectionDefinition).where(
                CollectionDefinition.library_id == library_id,
                CollectionDefinition.name == p.name,
            )
        )
        if definition is None:
            definition = CollectionDefinition(library_id=library_id, name=p.name, source="kometa")
            session.add(definition)
        definition.source_file = p.source_file
        definition.builders = p.builders
        definition.options = p.options
        definition.unsupported = p.unsupported
        definition.check_result = None
        definition.checked_at = None
        saved.append(definition)
    await session.flush()
    return saved


def _key(entry: ListEntry) -> str:
    return (
        f"tmdb:{entry.tmdb_id}"
        if entry.tmdb_id
        else f"imdb:{entry.imdb_id}"
        if entry.imdb_id
        else f"title:{normalize_title(entry.title)}:{entry.year}"
    )


async def check_definition(
    session: AsyncSession, definition: CollectionDefinition, lists: ListClient
) -> dict[str, Any]:
    entries: dict[str, ListEntry] = {}
    errors: list[str] = []
    for builder in definition.builders or []:
        try:
            for entry in await lists.resolve(builder):
                entries.setdefault(_key(entry), entry)
        except ListError as exc:
            errors.append(str(exc))

    library = await session.get(Library, definition.library_id) if definition.library_id else None
    in_library: dict[str, int] = {}
    if library is not None:
        rows = (
            await session.execute(
                select(MediaItem.id, MediaItem.tmdb_id, MediaItem.imdb_id, MediaItem.tvdb_id).where(
                    MediaItem.library_id == library.id, MediaItem.kind == library.kind
                )
            )
        ).all()
        index: dict[str, int] = {}
        for mi, tmdb, imdb, tvdb in rows:
            for prefix, value in (("tmdb", tmdb), ("imdb", imdb), ("tvdb", tvdb)):
                if value:
                    index.setdefault(f"{prefix}:{value}", mi)
        for key, entry in entries.items():
            for candidate in (
                f"tmdb:{entry.tmdb_id}" if entry.tmdb_id else None,
                f"imdb:{entry.imdb_id}" if entry.imdb_id else None,
                f"tvdb:{entry.tvdb_id}" if entry.tvdb_id else None,
            ):
                if candidate and candidate in index:
                    in_library[key] = index[candidate]
                    break

    missing = sorted(
        (e for k, e in entries.items() if k not in in_library),
        key=lambda e: (e.year or 0, e.title),
    )
    result: dict[str, Any] = {
        "total": len(entries),
        "in_library": len(in_library),
        "missing_count": len(missing),
        "missing": [{"title": e.title, "year": e.year} for e in missing[:MAX_MISSING_LISTED]],
        "errors": errors,
        "collection": None,
    }

    # Compare with the collection Kometa already built on each server, if there is one.
    if definition.library_id is not None:
        norm = normalize_collection_name(definition.name)
        coll = await session.scalar(
            select(Collection).where(
                Collection.library_id == definition.library_id,
                Collection.normalized_name == norm,
            )
        )
        if coll is not None:
            wanted = set(in_library.values())
            per_server: dict[str, dict[str, int]] = {}
            for sc in await session.scalars(
                select(ServerCollection).where(ServerCollection.collection_id == coll.id)
            ):
                members = set(
                    await session.scalars(
                        select(ServerItem.media_item_id).where(
                            ServerItem.server_id == sc.server_id,
                            ServerItem.remote_id.in_(sc.member_remote_ids or []),
                        )
                    )
                )
                per_server[str(sc.server_id)] = {
                    "in_collection": len(wanted & members),
                    "extra": len(members - wanted),
                }
            result["collection"] = {"id": coll.id, "name": coll.name, "servers": per_server}
    return result


async def run_check_definitions(ctx: JobContext) -> dict[str, Any]:
    async with ctx.sessionmaker() as session:
        query = select(CollectionDefinition).order_by(CollectionDefinition.name)
        if ctx.params.get("ids"):
            query = query.where(CollectionDefinition.id.in_(ctx.params["ids"]))
        definitions = list(await session.scalars(query))
        lists = ListClient(
            tmdb_key=await get_setting(session, "tmdb_api_key"),
            trakt_client_id=await get_setting(session, "trakt_client_id"),
            trakt_token=await get_setting(session, "trakt_access_token"),
        )
        summary: dict[str, int] = defaultdict(int)
        async with lists:
            for idx, definition in enumerate(definitions):
                await ctx.progress(idx / max(len(definitions), 1), f"Checking {definition.name}")
                result = await check_definition(session, definition, lists)
                definition.check_result = result
                definition.checked_at = utcnow()
                await session.commit()
                summary["checked"] += 1
                summary["with_errors"] += bool(result["errors"])
                await ctx.log(
                    f"{definition.name}: {result['total']} titles, {result['in_library']} in "
                    f"library"
                    + (f"; errors: {'; '.join(result['errors'])}" if result["errors"] else "")
                )
    await ctx.progress(1.0, "Checked")
    return dict(summary)
