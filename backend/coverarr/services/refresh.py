"""Refreshing a single title or collection from every server, without a full import."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import NotFoundError, client_for
from coverarr.clients.base import MediaServerError, RemoteCollection, RemoteItem
from coverarr.models import (
    Collection,
    MediaItem,
    Server,
    ServerCollection,
    ServerItem,
    ServerLibrary,
)
from coverarr.services.importer import apply_remote_item, upsert_items
from coverarr.services.jobs import JobContext
from coverarr.services.matching import match_all
from coverarr.services.mediainfo import refresh_facts
from coverarr.services.posters import _hash_one
from coverarr.services.review import refresh_issues

ID_FIELDS = ("tmdb_id", "tvdb_id", "imdb_id")
LABELS = {
    "tmdb_id": "TMDb id",
    "tvdb_id": "TVDB id",
    "imdb_id": "IMDb id",
    "title": "title",
    "poster_ref": "poster",
}


async def _finish(session: AsyncSession, library_id: int | None) -> dict[str, int]:
    await match_all(session, [library_id] if library_id is not None else [])
    await refresh_facts(session, [library_id] if library_id is not None else [])
    issues = await refresh_issues(session)
    await session.commit()
    return issues


async def run_refresh_item(ctx: JobContext) -> dict[str, Any]:
    """Re-read one movie or show (with its seasons and episodes) from each server it's on."""
    async with ctx.sessionmaker() as session:
        item = await session.get(MediaItem, ctx.params["media_item_id"])
        if item is None or item.kind not in ("movie", "show"):
            raise ValueError("That title no longer exists; run an import to refresh")
        title, library_id = item.title, item.library_id
        rows = (
            await session.execute(
                select(ServerItem, Server)
                .join(Server, Server.id == ServerItem.server_id)
                .where(ServerItem.media_item_id == item.id, Server.enabled.is_(True))
            )
        ).all()

    # Fetch first (network), then write everything in one go.
    fetched: list[tuple[ServerItem, Server, RemoteItem | None, list[RemoteItem], str | None]] = []
    for si, server in rows:
        await ctx.progress(len(fetched) / max(len(rows), 1) * 0.7, f"{server.name}: {title}")
        try:
            async with client_for(server) as client:
                remote = await client.get_item(si.remote_id, si.kind)
                children = await client.show_children(si.remote_id) if si.kind == "show" else []
                new_hash = None
                if remote.poster_ref and remote.poster_ref != si.poster_hash_ref:
                    new_hash = await _hash_one(client, si.remote_id, remote.poster_ref)
            fetched.append((si, server, remote, children, new_hash))
        except NotFoundError:
            fetched.append((si, server, None, [], None))
        except MediaServerError as exc:
            await ctx.log(f"{server.name}: could not refresh '{title}': {exc}")

    changes: list[str] = []
    async with ctx.sessionmaker() as session:
        for si, server, remote, children, new_hash in fetched:
            row = await session.get(ServerItem, si.id)
            if row is None:
                continue
            if remote is None:
                # Gone from that server: drop it (and its seasons/episodes).
                await session.execute(
                    delete(ServerItem).where(
                        ServerItem.server_library_id == row.server_library_id,
                        ServerItem.show_remote_id == row.remote_id,
                    )
                )
                await session.delete(row)
                changes.append(f"{server.name}: no longer on this server")
                continue
            before = {f: getattr(row, f) for f in (*ID_FIELDS, "title", "poster_ref")}
            apply_remote_item(row, remote)
            if new_hash is not None:
                row.poster_hash, row.poster_edges = new_hash
                row.poster_hash_ref = remote.poster_ref
            changed = [LABELS[f] for f, v in before.items() if getattr(row, f) != v]
            if row.kind == "show":
                await upsert_items(
                    session, server.id, row.server_library_id, children, row.seen_run
                )
                await session.execute(
                    delete(ServerItem).where(
                        ServerItem.server_library_id == row.server_library_id,
                        ServerItem.show_remote_id == row.remote_id,
                        ServerItem.remote_id.not_in([c.id for c in children]),
                    )
                )
                episodes = sum(1 for c in children if c.kind == "episode")
                changed.append(f"{episodes} episodes")
            changes.append(
                f"{server.name}: " + (", ".join(changed) + " updated" if changed else "no changes")
            )
        for line in changes:
            await ctx.log(line)
        await ctx.progress(0.85, "Re-matching")
        issues = await _finish(session, library_id)
    await ctx.progress(1.0, f"Refreshed {title}")
    return {"servers": len(fetched), "changes": changes, "issues": issues}


async def run_refresh_collection(ctx: JobContext) -> dict[str, Any]:
    """Re-read one collection (name, poster, members) from each server it's on."""
    async with ctx.sessionmaker() as session:
        coll = await session.get(Collection, ctx.params["collection_id"])
        if coll is None:
            raise ValueError("That collection no longer exists; run an import to refresh")
        name, library_id = coll.name, coll.library_id
        rows = (
            await session.execute(
                select(ServerCollection, Server, ServerLibrary.remote_id)
                .join(Server, Server.id == ServerCollection.server_id)
                .outerjoin(ServerLibrary, ServerLibrary.id == ServerCollection.server_library_id)
                .where(ServerCollection.collection_id == coll.id, Server.enabled.is_(True))
            )
        ).all()

    fetched: list[tuple[ServerCollection, Server, RemoteCollection | None, str | None]] = []
    for sc, server, library_remote_id in rows:
        try:
            async with client_for(server) as client:
                remote = await client.get_collection(library_remote_id, sc.remote_id)
                new_hash = None
                if remote.poster_ref and remote.poster_ref != sc.poster_hash_ref:
                    new_hash = await _hash_one(client, sc.remote_id, remote.poster_ref)
            fetched.append((sc, server, remote, new_hash))
        except NotFoundError:
            fetched.append((sc, server, None, None))
        except MediaServerError as exc:
            await ctx.log(f"{server.name}: could not refresh '{name}': {exc}")

    changes: list[str] = []
    async with ctx.sessionmaker() as session:
        for sc, server, remote, new_hash in fetched:
            row = await session.get(ServerCollection, sc.id)
            if row is None:
                continue
            if remote is None:
                await session.delete(row)
                changes.append(f"{server.name}: collection no longer exists there")
                continue
            before = len(row.member_remote_ids or [])
            poster_changed = row.poster_ref != remote.poster_ref
            row.name, row.sort_title, row.summary = remote.name, remote.sort_title, remote.summary
            row.smart, row.poster_ref, row.labels = remote.smart, remote.poster_ref, remote.labels
            row.member_remote_ids = remote.member_ids
            if new_hash is not None:
                row.poster_hash, row.poster_edges = new_hash
                row.poster_hash_ref = remote.poster_ref
            count = len(remote.member_ids)
            changes.append(
                f"{server.name}: {count} items"
                + (f" (was {before})" if count != before else "")
                + (", poster updated" if poster_changed else "")
            )
        for line in changes:
            await ctx.log(line)
        issues = await _finish(session, None)
    await ctx.progress(1.0, f"Refreshed {name}")
    return {
        "servers": len(fetched),
        "changes": changes,
        "issues": issues,
        "library_id": library_id,
    }


async def run_collection_remove(ctx: JobContext) -> dict[str, Any]:
    """Take one title out of a collection on every server where it's a member."""
    collection_id = ctx.params["collection_id"]
    media_item_id = ctx.params["media_item_id"]
    async with ctx.sessionmaker() as session:
        coll = await session.get(Collection, collection_id)
        item = await session.get(MediaItem, media_item_id)
        if coll is None or item is None:
            raise ValueError("That collection or title no longer exists")
        rows = (
            await session.execute(
                select(ServerCollection, Server, ServerLibrary.remote_id)
                .join(Server, Server.id == ServerCollection.server_id)
                .outerjoin(ServerLibrary, ServerLibrary.id == ServerCollection.server_library_id)
                .where(ServerCollection.collection_id == coll.id, Server.enabled.is_(True))
            )
        ).all()
        remote_ids = {
            sid: rid
            for sid, rid in (
                await session.execute(
                    select(ServerItem.server_id, ServerItem.remote_id).where(
                        ServerItem.media_item_id == item.id
                    )
                )
            ).all()
        }
        removed = 0
        for sc, server, library_remote_id in rows:
            rid = remote_ids.get(server.id)
            if rid is None or rid not in (sc.member_remote_ids or []):
                continue
            if sc.smart:
                await ctx.log(
                    f"{server.name}: '{coll.name}' is a smart collection; edit its rules there"
                )
                continue
            try:
                async with client_for(server) as client:
                    await client.remove_from_collection(library_remote_id, sc.remote_id, [rid])
            except MediaServerError as exc:
                await ctx.log(f"{server.name}: could not remove '{item.title}': {exc}")
                continue
            sc.member_remote_ids = [m for m in sc.member_remote_ids if m != rid]
            removed += 1
            await ctx.log(f"{server.name}: removed '{item.title}' from '{coll.name}'")
        issues = await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Removed")
    return {"removed": removed, "issues": issues}
