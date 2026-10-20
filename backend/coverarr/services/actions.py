"""Changes pushed to servers from the review screen."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coverarr.clients import (
    MatchTarget,
    MediaServerClient,
    MediaServerError,
    RemoteLibrary,
    client_for,
)
from coverarr.clients.base import RemoteItem
from coverarr.models import Collection, Issue, Server, ServerCollection, ServerItem, ServerLibrary
from coverarr.services.importer import apply_remote_item
from coverarr.services.jobs import JobContext
from coverarr.services.matching import match_all, normalize_collection_name
from coverarr.services.review import refresh_issues


async def _members_of(session: AsyncSession, sc: ServerCollection) -> set[int]:
    if not sc.member_remote_ids:
        return set()
    rows = await session.scalars(
        select(ServerItem.media_item_id).where(
            ServerItem.server_id == sc.server_id,
            ServerItem.remote_id.in_(sc.member_remote_ids),
            ServerItem.media_item_id.is_not(None),
        )
    )
    return set(rows)


async def sync_collection(
    session: AsyncSession,
    collection_id: int,
    *,
    create: bool,
    add_members: bool,
    server_ids: list[int] | None = None,
) -> list[str]:
    """Create the collection where it's missing and/or add missing members.

    Membership target is the union of members across all servers. Nothing is ever removed.
    Returns human-readable log lines.
    """
    coll = await session.get(
        Collection, collection_id, options=[selectinload(Collection.server_collections)]
    )
    if coll is None:
        return [f"Collection {collection_id} no longer exists"]
    existing = {sc.server_id: sc for sc in coll.server_collections}
    union: set[int] = set()
    for sc in existing.values():
        union |= await _members_of(session, sc)

    targets = (
        await session.execute(
            select(Server, ServerLibrary)
            .join(ServerLibrary, ServerLibrary.server_id == Server.id)
            .where(
                ServerLibrary.library_id == coll.library_id,
                ServerLibrary.enabled.is_(True),
                Server.enabled.is_(True),
            )
            .order_by(Server.id, ServerLibrary.id)
        )
    ).all()
    seen_servers: set[int] = set()
    lines: list[str] = []
    for server, sl in targets:
        if server.id in seen_servers or (server_ids and server.id not in server_ids):
            continue
        seen_servers.add(server.id)
        sc = existing.get(server.id)
        if sc is None and not create:
            continue
        if sc is not None and (not add_members or sc.smart):
            continue

        have = await _members_of(session, sc) if sc else set()
        wanted = union - have
        rows = (
            await session.execute(
                select(ServerItem.media_item_id, ServerItem.remote_id)
                .where(
                    ServerItem.server_id == server.id,
                    ServerItem.server_library_id == sl.id,
                    ServerItem.media_item_id.in_(wanted),
                )
                .order_by(ServerItem.id)
            )
        ).all()
        remote_ids = list(dict(rows).values())  # one remote item per logical item
        if not remote_ids:
            if sc is None:
                lines.append(f"{server.name}: skipped '{coll.name}' (no members on this server)")
            continue

        remote_lib = RemoteLibrary(id=sl.remote_id, name=sl.name, kind=sl.kind)
        try:
            async with client_for(server) as client:
                if sc is None:
                    new_id = await client.create_collection(remote_lib, coll.name, remote_ids)
                    session.add(
                        ServerCollection(
                            server_id=server.id,
                            remote_id=new_id,
                            name=coll.name,
                            server_library_id=sl.id if server.kind == "plex" else None,
                            member_remote_ids=remote_ids,
                            collection_id=coll.id,
                        )
                    )
                    lines.append(
                        f"{server.name}: created '{coll.name}' with {len(remote_ids)} items"
                    )
                else:
                    await client.add_to_collection(remote_lib, sc.remote_id, remote_ids)
                    sc.member_remote_ids = list(sc.member_remote_ids or []) + remote_ids
                    lines.append(f"{server.name}: added {len(remote_ids)} items to '{coll.name}'")
        except MediaServerError as exc:
            lines.append(f"{server.name}: failed on '{coll.name}': {exc}")
    return lines


async def run_collection_sync(ctx: JobContext) -> dict[str, Any]:
    ids: list[int] = ctx.params.get("collection_ids") or []
    create = bool(ctx.params.get("create"))
    add_members = bool(ctx.params.get("add_members"))
    server_ids = ctx.params.get("server_ids")
    failures = 0
    for idx, cid in enumerate(ids):
        async with ctx.sessionmaker() as session:
            lines = await sync_collection(
                session, cid, create=create, add_members=add_members, server_ids=server_ids
            )
            await session.commit()
        for line in lines:
            failures += "failed" in line
            await ctx.log(line)
        await ctx.progress((idx + 1) / max(len(ids), 1) * 0.95, f"{idx + 1}/{len(ids)} collections")
    async with ctx.sessionmaker() as session:
        issues = await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Collections updated")
    return {"collections": len(ids), "failures": failures, "issues": issues}


MATCH_POLL_ATTEMPTS = 10
MATCH_POLL_SECONDS = 2.0


async def _wait_for_ids(
    client: MediaServerClient, remote_id: str, kind: str, wanted: dict[str, str]
) -> RemoteItem | None:
    """Servers refresh metadata after a re-match in the background; wait for the new ids."""
    for attempt in range(MATCH_POLL_ATTEMPTS):
        item = await client.get_item(remote_id, kind)
        if any(getattr(item, k) == v for k, v in wanted.items()):
            return item
        if attempt < MATCH_POLL_ATTEMPTS - 1:
            await asyncio.sleep(MATCH_POLL_SECONDS)
    return None


async def run_fix_match(ctx: JobContext) -> dict[str, Any]:
    """Re-match the servers that disagree with the user's chosen identity for a file."""
    target = MatchTarget(**ctx.params["target"])
    wanted = {f"{k}_id": v for k, v in target.ids().items()}
    if not wanted:
        raise ValueError("A TMDb, TVDB or IMDb id is required")
    async with ctx.sessionmaker() as session:
        issue = await session.get(Issue, ctx.params["issue_id"])
        if issue is None or issue.type != "mismatch":
            raise ValueError("That mismatch no longer exists; run an import to refresh")
        ids = [
            si["server_item_id"] for c in issue.details["candidates"] for si in c["server_items"]
        ]
        rows = (
            await session.execute(
                select(ServerItem, Server)
                .join(Server, Server.id == ServerItem.server_id)
                .where(ServerItem.id.in_(ids))
            )
        ).all()
    to_fix = [
        (si, srv) for si, srv in rows if not any(getattr(si, k) == v for k, v in wanted.items())
    ]
    label = target.title or ", ".join(f"{k}-{v}" for k, v in target.ids().items())
    if not to_fix:
        await ctx.log(f"Every server already matches {label}")

    fixed = pending = failed = 0
    for idx, (si, server) in enumerate(to_fix):
        await ctx.progress(
            idx / max(len(to_fix), 1) * 0.8, f"{server.name}: re-matching {si.title}"
        )
        try:
            async with client_for(server) as client:
                description = await client.fix_match(si.remote_id, si.kind, target)
                await ctx.log(f"{server.name}: re-matched '{si.title}' to {description}")
                remote = await _wait_for_ids(client, si.remote_id, si.kind, wanted)
        except MediaServerError as exc:
            failed += 1
            await ctx.log(f"{server.name}: could not re-match '{si.title}': {exc}")
            continue
        if remote is None:
            pending += 1
            await ctx.log(
                f"{server.name}: still reports the old match; check it on the server, "
                "the next import picks up any change"
            )
            continue
        async with ctx.sessionmaker() as session:
            row = await session.get(ServerItem, si.id)
            if row is not None:
                apply_remote_item(row, remote)
                await session.commit()
        fixed += 1

    await ctx.progress(0.85, "Re-matching across servers")
    async with ctx.sessionmaker() as session:
        await match_all(session)
        issues = await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Done")
    if to_fix and failed == len(to_fix):
        raise RuntimeError("No server accepted the new match; see the log")
    return {"fixed": fixed, "pending": pending, "failed": failed, "issues": issues}


async def run_collection_rename(ctx: JobContext) -> dict[str, Any]:
    """Rename a collection on every server it exists on."""
    name: str = ctx.params["name"]
    async with ctx.sessionmaker() as session:
        coll = await session.get(Collection, ctx.params["collection_id"])
        if coll is None:
            raise ValueError("That collection no longer exists")
        rows = (
            await session.execute(
                select(ServerCollection, Server, ServerLibrary.remote_id)
                .join(Server, Server.id == ServerCollection.server_id)
                .outerjoin(ServerLibrary, ServerLibrary.id == ServerCollection.server_library_id)
                .where(ServerCollection.collection_id == coll.id)
            )
        ).all()
        renamed = failed = 0
        for sc, server, library_remote_id in rows:
            try:
                async with client_for(server) as client:
                    await client.rename_collection(library_remote_id, sc.remote_id, name)
            except MediaServerError as exc:
                failed += 1
                await ctx.log(f"{server.name}: could not rename '{sc.name}': {exc}")
                continue
            await ctx.log(f"{server.name}: renamed '{sc.name}' to '{name}'")
            sc.name = name
            renamed += 1
        if renamed:
            coll.name, coll.normalized_name = name, normalize_collection_name(name)
        await session.commit()
    async with ctx.sessionmaker() as session:
        issues = await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Renamed")
    if rows and failed == len(rows):
        raise RuntimeError("No server accepted the new name; see the log")
    return {"renamed": renamed, "failed": failed, "issues": issues}
