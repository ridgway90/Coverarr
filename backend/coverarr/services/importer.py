"""Full import from every enabled server, followed by matching and the review pass.

Each server row carries ``seen_run``; anything not seen in a complete fetch of its library is
deleted afterwards. A server that fails part-way keeps its previous data untouched.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coverarr.clients import MediaServerClient, MediaServerError, RemoteLibrary, client_for
from coverarr.clients.base import RemoteCollection, RemoteItem
from coverarr.models import Library, Server, ServerCollection, ServerItem, ServerLibrary, utcnow
from coverarr.services.files import file_key, show_folder
from coverarr.services.jobs import JobContext
from coverarr.services.load import LoadProfile, load_profile
from coverarr.services.matching import match_all, normalize_title
from coverarr.services.mediainfo import refresh_facts
from coverarr.services.review import refresh_issues

log = logging.getLogger(__name__)

KINDS_FOR_LIBRARY = {"movie": ("movie",), "show": ("show", "season", "episode")}
ITEM_FIELDS = (
    "kind",
    "title",
    "sort_title",
    "year",
    "tmdb_id",
    "tvdb_id",
    "imdb_id",
    "show_remote_id",
    "season_remote_id",
    "season_number",
    "episode_number",
    "poster_ref",
    "labels",
    "path",
    "file_key",
    "media",
)


async def _auto_library(
    session: AsyncSession, server: Server, remote: RemoteLibrary, guess: bool = True
) -> int:
    """Pick the logical library for a newly seen server library.

    Same kind and name wins; otherwise, if exactly one library of that kind has nothing from
    this server yet (e.g. Plex "Films" vs Emby "Movies"), use it; else create a new one.
    ``guess=False`` skips that second step (the server has several unmatched libraries of
    this kind, so which is which can't be told).
    """
    libraries = list(await session.scalars(select(Library).where(Library.kind == remote.kind)))
    for lib in libraries:
        if normalize_title(lib.name) == normalize_title(remote.name):
            return lib.id
    used = set(
        await session.scalars(
            select(ServerLibrary.library_id).where(ServerLibrary.server_id == server.id)
        )
    )
    free = [lib for lib in libraries if lib.id not in used]
    if guess and len(free) == 1:
        return free[0].id
    lib = Library(name=remote.name, kind=remote.kind)
    session.add(lib)
    await session.flush()
    return lib.id


async def sync_libraries(
    session: AsyncSession, server: Server, remote_libs: list[RemoteLibrary]
) -> list[tuple[ServerLibrary, RemoteLibrary]]:
    """Upsert the server's libraries; return the enabled, mapped ones."""
    existing = {sl.remote_id: sl for sl in server.libraries}
    active: list[tuple[ServerLibrary, RemoteLibrary]] = []
    # New libraries with a same-named match claim it first, so the "only free library"
    # guess for the others can't take it from them (e.g. "Kids Movies" before "Movies").
    names = {
        (lib.kind, normalize_title(lib.name)) for lib in await session.scalars(select(Library))
    }
    unmatched: dict[str, int] = {}
    for remote in remote_libs:
        if remote.id not in existing and (remote.kind, normalize_title(remote.name)) not in names:
            unmatched[remote.kind] = unmatched.get(remote.kind, 0) + 1
    remote_libs = sorted(remote_libs, key=lambda r: (r.kind, normalize_title(r.name)) not in names)
    for remote in remote_libs:
        sl = existing.pop(remote.id, None)
        if sl is None:
            sl = ServerLibrary(
                server_id=server.id,
                remote_id=remote.id,
                name=remote.name,
                kind=remote.kind,
                library_id=await _auto_library(
                    session, server, remote, guess=unmatched.get(remote.kind, 0) <= 1
                ),
            )
            session.add(sl)
            await session.flush()
        else:
            sl.name, sl.kind = remote.name, remote.kind
        if sl.enabled and sl.library_id is not None:
            active.append((sl, remote))
    for gone in existing.values():
        await session.delete(gone)
    await session.flush()
    return active


def _item_values(item: RemoteItem) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "title": item.title,
        "sort_title": item.sort_title,
        "year": item.year,
        "tmdb_id": item.tmdb_id,
        "tvdb_id": item.tvdb_id,
        "imdb_id": item.imdb_id,
        "show_remote_id": item.show_id,
        "season_remote_id": item.season_id,
        "season_number": item.season_number,
        "episode_number": item.episode_number,
        "poster_ref": item.poster_ref,
        "labels": item.labels,
        "path": item.path,
        "media": item.media,
        "file_key": file_key(item.path, item.kind) if item.kind in ("movie", "show") else None,
    }


def apply_remote_item(row: ServerItem, item: RemoteItem) -> None:
    """Refresh one stored row from a freshly fetched remote item."""
    for field_name, value in _item_values(item).items():
        setattr(row, field_name, value)


async def upsert_items(
    session: AsyncSession, server_id: int, sl_id: int, items: list[RemoteItem], run_id: int
) -> None:
    if not items:
        return
    now = utcnow()
    rows = [
        {
            "server_id": server_id,
            "server_library_id": sl_id,
            "remote_id": item.id,
            "seen_run": run_id,
            "updated_at": now,
            **_item_values(item),
        }
        for item in items
    ]
    stmt = sqlite_insert(ServerItem)
    stmt = stmt.on_conflict_do_update(
        index_elements=["server_id", "remote_id"],
        set_={
            "server_library_id": stmt.excluded.server_library_id,
            "seen_run": stmt.excluded.seen_run,
            "updated_at": stmt.excluded.updated_at,
            **{f: getattr(stmt.excluded, f) for f in ITEM_FIELDS},
        },
    )
    await session.execute(stmt, rows)


async def upsert_collections(
    session: AsyncSession,
    server: Server,
    collections: list[RemoteCollection],
    sl_by_remote: dict[str, int],
    run_id: int,
) -> None:
    existing = {
        sc.remote_id: sc
        for sc in await session.scalars(
            select(ServerCollection).where(ServerCollection.server_id == server.id)
        )
    }
    for rc in collections:
        sc = existing.get(rc.id)
        if sc is None:
            sc = ServerCollection(server_id=server.id, remote_id=rc.id)
            session.add(sc)
        sc.name = rc.name
        sc.server_library_id = sl_by_remote.get(rc.library_id) if rc.library_id else None
        sc.sort_title = rc.sort_title
        sc.summary = rc.summary
        sc.smart = rc.smart
        sc.poster_ref = rc.poster_ref
        sc.labels = rc.labels
        sc.member_remote_ids = rc.member_ids
        sc.seen_run = run_id
    await session.flush()
    await session.execute(
        delete(ServerCollection).where(
            ServerCollection.server_id == server.id, ServerCollection.seen_run != run_id
        )
    )


async def fill_show_paths(session: AsyncSession, sl_id: int) -> None:
    """Give shows without a folder path (Plex listings often omit it) one from an episode."""
    missing = set(
        await session.scalars(
            select(ServerItem.remote_id).where(
                ServerItem.server_library_id == sl_id,
                ServerItem.kind == "show",
                ServerItem.path.is_(None),
            )
        )
    )
    if not missing:
        return
    rows = await session.execute(
        select(ServerItem.show_remote_id, func.min(ServerItem.path))
        .where(
            ServerItem.server_library_id == sl_id,
            ServerItem.kind == "episode",
            ServerItem.path.is_not(None),
            ServerItem.show_remote_id.in_(missing),
        )
        .group_by(ServerItem.show_remote_id)
    )
    for show_remote_id, episode_path in rows:
        folder = show_folder(episode_path)
        await session.execute(
            update(ServerItem)
            .where(ServerItem.server_library_id == sl_id, ServerItem.remote_id == show_remote_id)
            .values(path=folder, file_key=file_key(folder, "show"))
        )


Report = Callable[[float, str], Awaitable[None]]


async def import_server(
    ctx: JobContext,
    server_id: int,
    run_id: int,
    report: Report,
    profile: LoadProfile,
    db_lock: asyncio.Lock,
) -> dict[str, Any]:
    """Import one server. Network calls run freely; database writes take ``db_lock`` so
    several servers can import at once without SQLite lock contention."""
    sm = ctx.sessionmaker
    async with sm() as session:
        async with db_lock:
            server = await session.get(Server, server_id, options=[selectinload(Server.libraries)])
            assert server is not None
            await session.commit()
        client: MediaServerClient = client_for(server, concurrency=profile.concurrency)
        client.page_size = profile.page_size
        async with client:
            try:
                info = await client.server_info()
                remote_libs = await client.libraries()
            except MediaServerError as exc:
                async with db_lock:
                    server.last_error = str(exc)
                    session.add(server)
                    await session.commit()
                raise
            async with db_lock:
                session.add(server)
                server.remote_id, server.version = info.remote_id, info.version
                server.last_ok_at, server.last_error = utcnow(), None
                active = await sync_libraries(session, server, remote_libs)
                await session.commit()
            await ctx.log(
                f"{server.name}: connected ({info.name} {info.version or ''}), "
                f"{len(active)} libraries"
            )

            counts: dict[str, int] = {}
            for idx, (sl, remote) in enumerate(active):
                fraction = 0.9 * idx / max(len(active), 1)
                for kind in KINDS_FOR_LIBRARY[sl.kind]:
                    n = 0
                    async for page in client.items(remote, kind):
                        async with db_lock:
                            await upsert_items(session, server.id, sl.id, page, run_id)
                            # Commit per page so other writers never wait long on the lock.
                            await session.commit()
                        n += len(page)
                        await report(fraction, f"{server.name} / {sl.name}: {n} {kind}s")
                    counts[kind] = counts.get(kind, 0) + n
                async with db_lock:
                    await session.execute(
                        delete(ServerItem).where(
                            ServerItem.server_library_id == sl.id, ServerItem.seen_run != run_id
                        )
                    )
                    if sl.kind == "show":
                        await fill_show_paths(session, sl.id)
                    await session.commit()

            await report(0.9, f"{server.name}: collections")
            remote_collections = await client.collections([remote for _, remote in active])
            sl_by_remote = {sl.remote_id: sl.id for sl, _ in active}
            async with db_lock:
                await upsert_collections(session, server, remote_collections, sl_by_remote, run_id)
                await session.commit()
            counts["collection"] = len(remote_collections)
            await report(1.0, f"{server.name}: done")
            await ctx.log(f"{server.name}: " + ", ".join(f"{v} {k}s" for k, v in counts.items()))
            return counts


async def run_import(ctx: JobContext) -> dict[str, Any]:
    sm = ctx.sessionmaker
    async with sm() as session:
        query = select(Server.id, Server.name).where(Server.enabled.is_(True)).order_by(Server.id)
        if ctx.params.get("server_ids"):
            query = query.where(Server.id.in_(ctx.params["server_ids"]))
        servers = (await session.execute(query)).all()
        profile = await load_profile(session)
    if not servers:
        raise RuntimeError("No enabled servers to import from")

    result: dict[str, Any] = {"servers": {}, "errors": {}}
    status = ctx.state["servers"] = {str(server_id): "waiting" for server_id, _ in servers}
    fractions = {server_id: 0.0 for server_id, _ in servers}
    db_lock = asyncio.Lock()

    async def one(server_id: int, name: str) -> None:
        async def report(fraction: float, message: str) -> None:
            fractions[server_id] = fraction
            await ctx.progress(0.85 * sum(fractions.values()) / len(fractions), message)

        status[str(server_id)] = "importing"
        try:
            result["servers"][name] = await import_server(
                ctx, server_id, ctx.job_id, report, profile, db_lock
            )
            status[str(server_id)] = "done"
        except Exception as exc:
            status[str(server_id)] = "failed"
            fractions[server_id] = 1.0
            if not isinstance(exc, MediaServerError):
                log.exception("Import from %s failed", name)
            result["errors"][name] = str(exc)
            await ctx.log(f"{name}: import failed, keeping previous data: {exc}")

    if profile.parallel_servers:
        await asyncio.gather(*(one(server_id, name) for server_id, name in servers))
    else:
        for server_id, name in servers:
            await one(server_id, name)

    await ctx.progress(0.87, "Matching items across servers")
    async with sm() as session:
        result["matching"] = await match_all(session)
        await refresh_facts(session)
        await session.commit()
    await ctx.progress(0.95, "Checking for differences")
    async with sm() as session:
        result["issues"] = await refresh_issues(session)
        await session.commit()
    await ctx.log(f"Matched: {result['matching']['items']}; issues: {result['issues']}")
    await ctx.progress(1.0, "Import complete")
    if len(result["errors"]) == len(servers):
        raise RuntimeError("Every server failed to import: " + "; ".join(result["errors"].values()))
    return result


async def run_rematch(ctx: JobContext) -> dict[str, Any]:
    async with ctx.sessionmaker() as session:
        await ctx.progress(0.1, "Matching items across servers")
        matching = await match_all(session)
        await refresh_facts(session)
        await ctx.progress(0.8, "Checking for differences")
        issues = await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Done")
    return {"matching": matching, "issues": issues}
