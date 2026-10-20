"""Changes pushed to servers from the review screen."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coverarr.clients import MediaServerError, RemoteLibrary, client_for
from coverarr.models import Collection, Server, ServerCollection, ServerItem, ServerLibrary
from coverarr.services.jobs import JobContext
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
