"""Collection definitions: collections built from lists (TMDb, Trakt, charts), defined in
Coverarr or imported from Kometa.

A definition can be checked (its lists compared with the library and each server) or synced:
the collection is created on every server where it's missing, missing titles are added and,
in "sync" mode, titles that left the lists are removed. Sort title, summary and order are
set on each server too.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import client_for
from coverarr.clients.base import MediaServerError, RemoteLibrary
from coverarr.models import (
    Collection,
    CollectionDefinition,
    Library,
    MediaItem,
    Server,
    ServerCollection,
    ServerItem,
    ServerLibrary,
    utcnow,
)
from coverarr.services import trakt
from coverarr.services.jobs import JobContext
from coverarr.services.kometa_yaml import ParsedCollection
from coverarr.services.lists import (
    ListClient,
    ListEntry,
    ListError,
    default_sync_mode,
    tmdb_id_from,
)
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


@dataclass
class Resolved:
    """A definition's lists, merged and looked up in its library."""

    entries: dict[str, ListEntry] = field(default_factory=dict)  # in list order
    in_library: dict[str, int] = field(default_factory=dict)  # entry key -> media item
    errors: list[str] = field(default_factory=list)

    @property
    def ordered(self) -> list[int]:
        """Media items in the lists' order, each once."""
        seen: dict[int, None] = {}
        for key in self.entries:
            if key in self.in_library:
                seen.setdefault(self.in_library[key])
        return list(seen)

    @property
    def missing(self) -> list[ListEntry]:
        return [e for k, e in self.entries.items() if k not in self.in_library]


async def resolve_definition(
    session: AsyncSession, definition: CollectionDefinition, lists: ListClient
) -> Resolved:
    library = await session.get(Library, definition.library_id) if definition.library_id else None
    kind = library.kind if library is not None else "movie"
    resolved = Resolved()
    for builder in definition.builders or []:
        try:
            for entry in await lists.resolve(builder, kind):
                if entry.kind == kind:
                    resolved.entries.setdefault(_key(entry), entry)
        except ListError as exc:
            if str(exc) not in resolved.errors:  # e.g. a missing key, once per list
                resolved.errors.append(str(exc))
    if library is None:
        return resolved
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
    for key, entry in resolved.entries.items():
        for candidate in (
            f"tmdb:{entry.tmdb_id}" if entry.tmdb_id else None,
            f"imdb:{entry.imdb_id}" if entry.imdb_id else None,
            f"tvdb:{entry.tvdb_id}" if entry.tvdb_id else None,
        ):
            if candidate and candidate in index:
                resolved.in_library[key] = index[candidate]
                break
    return resolved


async def _find_collection(
    session: AsyncSession, definition: CollectionDefinition
) -> Collection | None:
    if definition.library_id is None:
        return None
    return await session.scalar(
        select(Collection)
        .where(
            Collection.library_id == definition.library_id,
            Collection.normalized_name == normalize_collection_name(definition.name),
        )
        .order_by(Collection.id)
        .limit(1)
    )


async def check_definition(
    session: AsyncSession, definition: CollectionDefinition, lists: ListClient
) -> dict[str, Any]:
    resolved = await resolve_definition(session, definition, lists)
    missing = sorted(resolved.missing, key=lambda e: (e.year or 0, e.title))
    result: dict[str, Any] = {
        "total": len(resolved.entries),
        "in_library": len(resolved.in_library),
        "missing_count": len(missing),
        "missing": [{"title": e.title, "year": e.year} for e in missing[:MAX_MISSING_LISTED]],
        "errors": resolved.errors,
        "collection": None,
    }

    # Compare with the collection Kometa already built on each server, if there is one.
    coll = await _find_collection(session, definition)
    if coll is not None:
        wanted = set(resolved.in_library.values())
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


def _ordered_ids(
    order: str, wanted: list[int], items: dict[int, tuple[str, int | None]]
) -> list[int]:
    """Members sorted the way the collection shows them (for comparing and Plex's custom
    order); ``items`` maps media item -> (sort title, year)."""
    if order == "alpha":
        return sorted(wanted, key=lambda m: items[m][0])
    if order == "release":
        return sorted(wanted, key=lambda m: (items[m][1] or 9999, items[m][0]))
    return wanted


async def sync_definition(
    session: AsyncSession, definition: CollectionDefinition, lists: ListClient
) -> dict[str, Any]:
    """Make the collection on every server match the definition: created where missing,
    missing titles added, and (sync mode "sync") titles no longer listed removed."""
    if definition.library_id is None:
        raise ListError("Choose a library for this collection first")
    resolved = await resolve_definition(session, definition, lists)
    options = definition.options or {}
    mode = options.get("sync_mode") or default_sync_mode(definition.builders or [])
    order = options.get("collection_order") or "release"
    wanted = resolved.ordered
    result: dict[str, Any] = {
        "total": len(resolved.entries),
        "in_library": len(wanted),
        "missing_count": len(resolved.missing),
        "missing": [
            {"title": e.title, "year": e.year} for e in resolved.missing[:MAX_MISSING_LISTED]
        ],
        "errors": resolved.errors,
        "servers": {},
        "notes": [],
    }
    # Never empty a collection because a list failed or came back empty.
    can_remove = mode == "sync" and not resolved.errors and bool(wanted)
    if resolved.errors and not wanted:
        result["notes"].append("Nothing synced: the lists couldn't be read")
        return result

    coll = await _find_collection(session, definition)
    if coll is None:
        if not wanted:
            result["notes"].append("None of these titles are in the library yet")
            return result
        coll = Collection(
            library_id=definition.library_id,
            name=definition.name,
            normalized_name=normalize_collection_name(definition.name),
        )
        session.add(coll)
        await session.flush()
        result["created"] = True
    if options.get("sort_title"):
        coll.sort_title = str(options["sort_title"])
    if options.get("summary"):
        coll.summary = str(options["summary"])
    result["collection_id"] = coll.id

    meta = {
        mid: ((sort or title or "").lower(), year)
        for mid, sort, title, year in (
            await session.execute(
                select(MediaItem.id, MediaItem.sort_title, MediaItem.title, MediaItem.year).where(
                    MediaItem.id.in_(wanted)
                )
            )
        ).all()
    }
    wanted = _ordered_ids(order, wanted, meta)

    targets = (
        await session.execute(
            select(Server, ServerLibrary)
            .join(ServerLibrary, ServerLibrary.server_id == Server.id)
            .where(
                ServerLibrary.library_id == definition.library_id,
                ServerLibrary.enabled.is_(True),
                Server.enabled.is_(True),
            )
            .order_by(Server.id, ServerLibrary.id)
        )
    ).all()
    existing = {
        sc.server_id: sc
        for sc in await session.scalars(
            select(ServerCollection).where(ServerCollection.collection_id == coll.id)
        )
    }
    done_servers: set[int] = set()
    for server, sl in targets:
        if server.id in done_servers:
            continue
        done_servers.add(server.id)
        remote_of = dict(
            (
                await session.execute(
                    select(ServerItem.media_item_id, ServerItem.remote_id)
                    .where(
                        ServerItem.server_id == server.id,
                        ServerItem.server_library_id == sl.id,
                        ServerItem.media_item_id.in_(wanted),
                    )
                    .order_by(ServerItem.id.desc())
                )
            ).all()
        )
        remote_wanted = [remote_of[m] for m in wanted if m in remote_of]
        sc = existing.get(server.id)
        outcome: dict[str, Any] = {"added": 0, "removed": 0}
        library = RemoteLibrary(id=sl.remote_id, name=sl.name, kind=sl.kind)
        try:
            async with client_for(server) as client:
                if sc is None:
                    if not remote_wanted:
                        outcome["skipped"] = "none of the titles are on this server"
                        result["servers"][server.name] = outcome
                        continue
                    new_id = await client.create_collection(library, coll.name, remote_wanted)
                    sc = ServerCollection(
                        server_id=server.id,
                        remote_id=new_id,
                        name=coll.name,
                        server_library_id=sl.id if server.kind == "plex" else None,
                        member_remote_ids=list(remote_wanted),
                        collection_id=coll.id,
                    )
                    session.add(sc)
                    outcome["created"] = True
                    outcome["added"] = len(remote_wanted)
                elif sc.smart:
                    outcome["skipped"] = "smart collection"
                    result["servers"][server.name] = outcome
                    continue
                else:
                    current = list(sc.member_remote_ids or [])
                    add = [r for r in remote_wanted if r not in set(current)]
                    remove = (
                        [r for r in current if r not in set(remote_wanted)] if can_remove else []
                    )
                    if add:
                        await client.add_to_collection(library, sc.remote_id, add)
                    if remove:
                        await client.remove_from_collection(sl.remote_id, sc.remote_id, remove)
                    sc.member_remote_ids = [r for r in current if r not in set(remove)] + add
                    outcome["added"], outcome["removed"] = len(add), len(remove)
                # Members are in by now: a failure here only costs the sort title/order.
                try:
                    notes = await client.update_collection(
                        sl.remote_id,
                        sc.remote_id,
                        sort_title=coll.sort_title,
                        summary=coll.summary,
                        order=order,
                        member_order=remote_wanted if order == "custom" else None,
                    )
                except MediaServerError as exc:
                    notes = [f"couldn't set the sort title, summary or order: {exc}"]
                else:
                    if coll.sort_title:
                        sc.sort_title = coll.sort_title
                    if coll.summary:
                        sc.summary = coll.summary
                result["notes"] += [f"{server.name}: {n}" for n in notes]
        except MediaServerError as exc:
            outcome["error"] = str(exc)
        result["servers"][server.name] = outcome
    return result


async def _lists(session: AsyncSession) -> ListClient:
    return ListClient(
        tmdb_key=await get_setting(session, "tmdb_api_key"),
        trakt_client_id=await get_setting(session, "trakt_client_id"),
        trakt_token=await trakt.access_token(session),
    )


async def _tmdb_collection_poster(
    session: AsyncSession, definition: CollectionDefinition, collection_id: int, lists: ListClient
) -> bool:
    """A new collection built from a TMDb collection starts with TMDb's poster."""
    from coverarr.services.artwork import TMDB_IMAGES, ArtworkError, save_artwork
    from coverarr.services.overlays import config_dir

    source = next((b for b in definition.builders or [] if b["type"] == "tmdb_collection"), None)
    if source is None:
        return False
    try:
        data = await lists.tmdb_json(f"/collection/{tmdb_id_from(source['value'], 'collection')}")
        if not data.get("poster_path"):
            return False
        image = await lists.download(f"{TMDB_IMAGES}/original{data['poster_path']}")
        await save_artwork(
            session,
            config_dir(),
            source="tmdb",
            data=image,
            name=str(data["poster_path"]).lstrip("/"),
            collection_id=collection_id,
            choose=True,
        )
    except (ListError, ArtworkError):
        return False
    return True


async def _forget_rejected_token(ctx: JobContext, session: AsyncSession, lists: ListClient) -> None:
    """Trakt refused the saved sign-in: drop it, so later syncs don't trip over it."""
    if lists.trakt_token_rejected:
        await trakt.disconnect(session)
        await ctx.log(
            "Trakt refused the saved sign-in (expired, or made before Trakt's sign-in change), "
            "so it was removed. Public lists work without it; to read private lists, use "
            "Connect Trakt in Settings → General."
        )


async def run_sync_definitions(ctx: JobContext) -> dict[str, Any]:
    """Sync the given definitions, or every one that's switched on."""
    from coverarr.services.arr import automation_settings
    from coverarr.services.review import refresh_issues

    summary: dict[str, int] = defaultdict(int)
    push_ids: list[int] = []
    async with ctx.sessionmaker() as session:
        query = select(CollectionDefinition).order_by(CollectionDefinition.name)
        if ctx.params.get("ids"):
            query = query.where(CollectionDefinition.id.in_(ctx.params["ids"]))
        else:
            query = query.where(CollectionDefinition.enabled.is_(True))
        definitions = list(await session.scalars(query))
        auto_push = (await automation_settings(session))["auto_push"]
        async with await _lists(session) as lists:
            for idx, definition in enumerate(definitions):
                await ctx.progress(idx / max(len(definitions), 1), f"Syncing {definition.name}")
                try:
                    result = await sync_definition(session, definition, lists)
                except ListError as exc:
                    result = {"errors": [str(exc)], "servers": {}, "notes": []}
                if result.get("created") and result.get("collection_id"):
                    if await _tmdb_collection_poster(
                        session, definition, result["collection_id"], lists
                    ):
                        push_ids.append(result["collection_id"])
                definition.sync_result = result
                definition.synced_at = utcnow()
                await session.commit()
                summary["synced"] += 1
                summary["with_errors"] += bool(result["errors"])
                changes = [
                    f"{name}: "
                    + (
                        o.get("error")
                        or o.get("skipped")
                        or ("created, " if o.get("created") else "")
                        + f"+{o['added']} -{o['removed']}"
                    )
                    for name, o in result["servers"].items()
                ]
                await ctx.log(
                    f"{definition.name}: {result.get('in_library', 0)} of "
                    f"{result.get('total', 0)} titles in the library"
                    + (f"; {'; '.join(changes)}" if changes else "")
                    + (f"; errors: {'; '.join(result['errors'])}" if result["errors"] else "")
                )
                for note in result["notes"]:
                    await ctx.log(f"{definition.name}: {note}")
        await _forget_rejected_token(ctx, session, lists)
        await ctx.progress(0.95, "Checking for differences")
        await refresh_issues(session)
        await session.commit()
    if push_ids and auto_push and not ctx.params.get("no_push"):
        from coverarr.services import arr
        from coverarr.services.overlays import config_dir

        if arr.queue is not None:
            await arr.queue.runner.enqueue(
                "push_posters",
                {"config_dir": str(config_dir()), "media_item_ids": [], "collection_ids": push_ids},
            )
    await ctx.progress(1.0, "Collections synced")
    return dict(summary)


async def run_check_definitions(ctx: JobContext) -> dict[str, Any]:
    async with ctx.sessionmaker() as session:
        query = select(CollectionDefinition).order_by(CollectionDefinition.name)
        if ctx.params.get("ids"):
            query = query.where(CollectionDefinition.id.in_(ctx.params["ids"]))
        definitions = list(await session.scalars(query))
        summary: dict[str, int] = defaultdict(int)
        async with await _lists(session) as lists:
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
        await _forget_rejected_token(ctx, session, lists)
    await ctx.progress(1.0, "Checked")
    return dict(summary)
