"""Full import from every enabled server, followed by matching and the review pass.

Each server row carries ``seen_run``; anything not seen in a complete fetch of its library is
deleted afterwards. A server that fails part-way keeps its previous data untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coverarr.clients import MediaServerClient, MediaServerError, RemoteLibrary, client_for
from coverarr.clients.base import RemoteCollection, RemoteItem
from coverarr.models import Library, Server, ServerCollection, ServerItem, ServerLibrary, utcnow
from coverarr.services.jobs import JobContext
from coverarr.services.matching import match_all, normalize_title
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
)


async def _auto_library(session: AsyncSession, server: Server, remote: RemoteLibrary) -> int:
    """Pick the logical library for a newly seen server library.

    Same kind and name wins; otherwise, if exactly one library of that kind has nothing from
    this server yet (e.g. Plex "Films" vs Emby "Movies"), use it; else create a new one.
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
    if len(free) == 1:
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
    for remote in remote_libs:
        sl = existing.pop(remote.id, None)
        if sl is None:
            sl = ServerLibrary(
                server_id=server.id,
                remote_id=remote.id,
                name=remote.name,
                kind=remote.kind,
                library_id=await _auto_library(session, server, remote),
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
    }


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


async def import_server(
    ctx: JobContext, server_id: int, run_id: int, base: float, span: float
) -> dict[str, Any]:
    sm = ctx.sessionmaker
    async with sm() as session:
        server = await session.get(Server, server_id, options=[selectinload(Server.libraries)])
        assert server is not None
        client: MediaServerClient = client_for(server)
        async with client:
            try:
                info = await client.server_info()
            except MediaServerError as exc:
                server.last_error = str(exc)
                await session.commit()
                raise
            server.remote_id, server.version = info.remote_id, info.version
            server.last_ok_at, server.last_error = utcnow(), None
            active = await sync_libraries(session, server, await client.libraries())
            await session.commit()
            await ctx.log(
                f"{server.name}: connected ({info.name} {info.version or ''}), "
                f"{len(active)} libraries"
            )

            counts: dict[str, int] = {}
            for idx, (sl, remote) in enumerate(active):
                lib_base = base + span * 0.9 * idx / max(len(active), 1)
                for kind in KINDS_FOR_LIBRARY[sl.kind]:
                    n = 0
                    async for page in client.items(remote, kind):
                        await upsert_items(session, server.id, sl.id, page, run_id)
                        # Commit per page so the UI's own writes never wait long on the lock.
                        await session.commit()
                        n += len(page)
                        await ctx.progress(lib_base, f"{server.name} / {sl.name}: {n} {kind}s")
                    counts[kind] = counts.get(kind, 0) + n
                await session.execute(
                    delete(ServerItem).where(
                        ServerItem.server_library_id == sl.id, ServerItem.seen_run != run_id
                    )
                )
                await session.commit()

            await ctx.progress(base + span * 0.9, f"{server.name}: collections")
            remote_collections = await client.collections([remote for _, remote in active])
            sl_by_remote = {sl.remote_id: sl.id for sl, _ in active}
            await upsert_collections(session, server, remote_collections, sl_by_remote, run_id)
            await session.commit()
            counts["collection"] = len(remote_collections)
            await ctx.log(f"{server.name}: " + ", ".join(f"{v} {k}s" for k, v in counts.items()))
            return counts


async def run_import(ctx: JobContext) -> dict[str, Any]:
    sm = ctx.sessionmaker
    async with sm() as session:
        query = select(Server.id, Server.name).where(Server.enabled.is_(True)).order_by(Server.id)
        if ctx.params.get("server_ids"):
            query = query.where(Server.id.in_(ctx.params["server_ids"]))
        servers = (await session.execute(query)).all()
    if not servers:
        raise RuntimeError("No enabled servers to import from")

    result: dict[str, Any] = {"servers": {}, "errors": {}}
    span = 0.85 / len(servers)
    for idx, (server_id, name) in enumerate(servers):
        try:
            result["servers"][name] = await import_server(
                ctx, server_id, ctx.job_id, idx * span, span
            )
        except Exception as exc:
            if not isinstance(exc, MediaServerError):
                log.exception("Import from %s failed", name)
            result["errors"][name] = str(exc)
            await ctx.log(f"{name}: import failed, keeping previous data: {exc}")

    await ctx.progress(0.87, "Matching items across servers")
    async with sm() as session:
        result["matching"] = await match_all(session)
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
        await ctx.progress(0.8, "Checking for differences")
        issues = await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Done")
    return {"matching": matching, "issues": issues}
