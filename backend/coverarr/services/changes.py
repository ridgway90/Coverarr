"""Pending poster changes, and pushing them to the servers.

Choosing a base poster (a server's poster, a Kometa original or an upload) only records the
choice. A change is pending for every server whose current poster doesn't look like that base:

* the base came from that server                       -> nothing to do
* the server's fingerprint matches the base's          -> nothing to do, unless it still
                                                          carries Kometa's overlays
* otherwise (different, missing or not yet fingerprinted) -> pending

Pushing uploads the base to each pending server and records the new poster ref and the base's
fingerprint, so the change drops off the list without another scan. If someone changes the
poster on the server afterwards, its ref changes and it shows up again after the next scan.
"""

from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import String, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import MediaServerClient, MediaServerError, client_for
from coverarr.models import (
    Artwork,
    Collection,
    Library,
    MediaItem,
    Server,
    ServerCollection,
    ServerItem,
)
from coverarr.services.jobs import JobContext
from coverarr.services.load import load_profile
from coverarr.services.posters import hamming, poster_hash
from coverarr.services.settings_store import get_setting

CHUNK = 20


@dataclass
class ServerChange:
    server_id: int
    row_id: int
    remote_id: str
    reason: str
    current: dict[str, Any] | None  # the poster the server shows now, for the UI


@dataclass
class Change:
    target: str  # item | collection
    id: int
    title: str
    kind: str  # movie | show | season | episode | collection
    library_id: int | None
    source: str
    # The movie/show a season or episode belongs to (itself for movies and shows).
    top_id: int | None
    base: dict[str, Any] | None
    servers: list[ServerChange] = field(default_factory=list)
    problem: str | None = None  # why it can't be pushed

    def out(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "library_id": self.library_id,
            "source": self.source,
            "top_id": self.top_id,
            "base": self.base,
            "problem": self.problem,
            "servers": [
                {"server_id": s.server_id, "reason": s.reason, "current": s.current}
                for s in self.servers
            ],
        }


# Server rows are plain column tuples (not ORM objects): there can be tens of thousands.
ITEM_COLS = (
    ServerItem.id,
    ServerItem.server_id,
    ServerItem.remote_id,
    ServerItem.poster_ref,
    ServerItem.poster_hash,
    ServerItem.poster_hash_ref,
    ServerItem.pushed_ref,
    # Decoding every row's labels JSON is the slowest part at scale; ask SQLite instead.
    type_coerce(ServerItem.labels, String).ilike('%"overlay"%').label("overlay_label"),
)
COLL_COLS = (
    ServerCollection.id,
    ServerCollection.server_id,
    ServerCollection.remote_id,
    ServerCollection.poster_ref,
    ServerCollection.poster_hash,
    ServerCollection.poster_hash_ref,
    ServerCollection.pushed_ref,
    type_coerce(ServerCollection.labels, String).ilike('%"overlay"%').label("overlay_label"),
)


def _fresh_hash(row: Any) -> str | None:
    return row.poster_hash if row.poster_ref and row.poster_hash_ref == row.poster_ref else None


def _has_overlay(row: Any) -> bool:
    """Kometa's overlays are still on this poster (we didn't replace it since)."""
    if row.pushed_ref and row.pushed_ref == row.poster_ref:
        return False
    return bool(row.overlay_label)


def _row_poster(row: Any, key: str) -> dict[str, Any] | None:
    if not row.poster_ref:
        return None
    return {
        key: row.id,
        "server_id": row.server_id,
        "has_poster": True,
        "poster_version": row.poster_ref[-24:],
        "kometa_overlay": _has_overlay(row),
    }


def _art_poster(art: Any) -> dict[str, Any]:
    return {
        "artwork_id": art.id,
        "server_id": None,
        "source": art.source,
        "has_poster": True,
        "poster_version": f"{art.id}-{int(art.created_at.timestamp())}",
        "kometa_overlay": False,
    }


def _assess(change: Change, rows: list[Any], artworks: dict[int, Any], threshold: int) -> None:
    """Fill in ``change.base`` and the servers that still need the base poster."""
    key = "server_item_id" if change.target == "item" else "server_collection_id"
    kind, _, ref = change.source.partition(":")
    base_hash: str | None = None
    source_server: int | None = None
    if kind == "artwork":
        art = artworks.get(int(ref)) if ref.isdigit() else None
        if art is None:
            change.problem = "The chosen artwork no longer exists"
            return
        change.base, base_hash = _art_poster(art), art.poster_hash
    elif kind == "server" and ref.isdigit():
        source_server = int(ref)
        src = next((r for r in rows if r.server_id == source_server), None)
        if src is None or not src.poster_ref:
            change.problem = "The server you picked the poster from no longer has it"
            return
        change.base, base_hash = _row_poster(src, key), _fresh_hash(src)
    else:
        change.problem = f"Unknown poster source {change.source!r}"
        return

    for row in rows:
        if row.server_id == source_server:
            continue
        current = _fresh_hash(row)
        if not row.poster_ref:
            reason = "no poster"
        elif current is None or base_hash is None:
            reason = "not fingerprinted yet"
        elif hamming(current, base_hash) > threshold:
            reason = "different poster"
        elif _has_overlay(row):
            reason = "has Kometa overlays"
        else:
            continue
        change.servers.append(
            ServerChange(row.server_id, row.id, row.remote_id, reason, _row_poster(row, key))
        )


ITEM_INFO = (
    MediaItem.id,
    MediaItem.kind,
    MediaItem.title,
    MediaItem.year,
    MediaItem.library_id,
    MediaItem.poster_source,
    MediaItem.parent_id,
    MediaItem.season_number,
    MediaItem.episode_number,
)


async def _labels_for(session: AsyncSession, items: list[Any]) -> dict[int, tuple]:
    """Readable titles for seasons/episodes ("Spaced S01E02") plus their top-level show id."""
    parents: dict[int, Any] = {}
    want = {i.parent_id for i in items if i.parent_id}
    while want:
        batch = list(want)
        for start in range(0, len(batch), 900):
            rows = (
                await session.execute(
                    select(*ITEM_INFO).where(MediaItem.id.in_(batch[start : start + 900]))
                )
            ).all()
            parents.update((r.id, r) for r in rows)
        want = {r.parent_id for r in parents.values() if r.parent_id and r.parent_id not in parents}

    out: dict[int, tuple] = {}
    for item in items:
        top = item
        while top.parent_id and top.parent_id in parents:
            top = parents[top.parent_id]
        if item.kind == "season":
            title = f"{top.title} - Season {item.season_number}"
        elif item.kind == "episode":
            season = parents.get(item.parent_id) if item.parent_id else None
            s = (
                item.season_number
                if item.season_number is not None
                else (season.season_number if season else None)
            )
            title = f"{top.title} S{(s or 0):02d}E{(item.episode_number or 0):02d}"
        else:
            title = f"{item.title} ({item.year})" if item.year else item.title
        out[item.id] = (title, top.id)
    return out


async def compute_changes(
    session: AsyncSession,
    *,
    library_id: int | None = None,
    media_item_ids: list[int] | None = None,
    collection_ids: list[int] | None = None,
    items: bool = True,
    collections: bool = True,
) -> list[Change]:
    """Every pending poster change (optionally narrowed), in a stable order."""
    threshold = int(await get_setting(session, "poster_match_threshold"))
    enabled = select(Server.id).where(Server.enabled.is_(True))
    changes: list[Change] = []

    if items and collection_ids is None:
        where = [MediaItem.poster_source.is_not(None)]
        if library_id is not None:
            where.append(MediaItem.library_id == library_id)
        if media_item_ids is not None:
            where.append(MediaItem.id.in_(media_item_ids))
        targets = (await session.execute(select(*ITEM_INFO).where(*where))).all()
        if targets:
            chosen = select(MediaItem.id).where(*where)
            rows: dict[int, list] = defaultdict(list)
            for r in await session.execute(
                select(*ITEM_COLS, ServerItem.media_item_id)
                .where(ServerItem.media_item_id.in_(chosen), ServerItem.server_id.in_(enabled))
                .order_by(ServerItem.server_id)
            ):
                rows[r.media_item_id].append(r)
            arts = await _artworks(session, Artwork.media_item_id.in_(chosen))
            labels = await _labels_for(session, targets)
            for t in targets:
                title, top = labels[t.id]
                change = Change(
                    "item", t.id, title, t.kind, t.library_id, t.poster_source, top, None
                )
                _assess(change, rows[t.id], arts, threshold)
                if change.servers or change.problem:
                    changes.append(change)

    if collections and media_item_ids is None:
        where = [Collection.poster_source.is_not(None)]
        if library_id is not None:
            where.append(Collection.library_id == library_id)
        if collection_ids is not None:
            where.append(Collection.id.in_(collection_ids))
        colls = (
            await session.execute(
                select(
                    Collection.id, Collection.name, Collection.library_id, Collection.poster_source
                ).where(*where)
            )
        ).all()
        if colls:
            chosen = select(Collection.id).where(*where)
            crows: dict[int, list] = defaultdict(list)
            for r in await session.execute(
                select(*COLL_COLS, ServerCollection.collection_id)
                .where(
                    ServerCollection.collection_id.in_(chosen),
                    ServerCollection.server_id.in_(enabled),
                )
                .order_by(ServerCollection.server_id)
            ):
                crows[r.collection_id].append(r)
            arts = await _artworks(session, Artwork.collection_id.in_(chosen))
            for c in colls:
                change = Change(
                    "collection",
                    c.id,
                    c.name,
                    "collection",
                    c.library_id,
                    c.poster_source,
                    None,
                    None,
                )
                _assess(change, crows[c.id], arts, threshold)
                if change.servers or change.problem:
                    changes.append(change)

    changes.sort(key=lambda c: (c.target != "collection", c.title.lower()))
    return changes


async def _artworks(session: AsyncSession, owner_filter: Any) -> dict[int, Any]:
    rows = await session.execute(
        select(Artwork.id, Artwork.source, Artwork.poster_hash, Artwork.created_at).where(
            owner_filter
        )
    )
    return {a.id: a for a in rows}


async def changes_summary(session: AsyncSession) -> dict[str, int]:
    changes = await compute_changes(session)
    return {
        "titles": sum(1 for c in changes if c.target == "item" and not c.problem),
        "collections": sum(1 for c in changes if c.target == "collection" and not c.problem),
        "uploads": sum(len(c.servers) for c in changes if not c.problem),
        "problems": sum(1 for c in changes if c.problem),
    }


async def library_names(session: AsyncSession) -> dict[int, str]:
    return dict((await session.execute(select(Library.id, Library.name))).all())


# --- pushing ---------------------------------------------------------------------------------


def _content_type(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as img:
        fmt = (img.format or "").upper()
    return {"PNG": "image/png", "WEBP": "image/webp"}.get(fmt, "image/jpeg")


async def _base_bytes(
    session: AsyncSession,
    change: Change,
    clients: dict[int, MediaServerClient],
    config_dir: Path,
) -> bytes:
    kind, _, ref = change.source.partition(":")
    if kind == "artwork":
        art = await session.get(Artwork, int(ref))
        if art is None:
            raise ValueError("the chosen artwork no longer exists")
        return await asyncio.to_thread((config_dir / art.file_path).read_bytes)
    server_id = int(ref)
    model = ServerItem if change.target == "item" else ServerCollection
    owner = model.media_item_id if change.target == "item" else model.collection_id
    row = await session.scalar(
        select(model).where(owner == change.id, model.server_id == server_id)
    )
    if row is None or not row.poster_ref:
        raise ValueError("the source server no longer has this poster")
    client = clients.get(server_id)
    if client is None:
        raise ValueError("the source server is disabled")
    return await client.fetch_poster(row.remote_id, row.poster_ref)


async def run_push_posters(ctx: JobContext) -> dict[str, Any]:
    """Upload each pending base poster to the servers that don't show it yet."""
    sm = ctx.sessionmaker
    config_dir = Path(ctx.params["config_dir"])
    item_ids = ctx.params.get("media_item_ids")
    coll_ids = ctx.params.get("collection_ids")
    async with sm() as session:
        if item_ids is None and coll_ids is None:
            changes = await compute_changes(session)
        else:
            changes = []
            if item_ids:
                changes += await compute_changes(session, media_item_ids=item_ids)
            if coll_ids:
                changes += await compute_changes(session, collection_ids=coll_ids)
        changes = [c for c in changes if not c.problem]
        servers = {s.id: s for s in await session.scalars(select(Server))}
        profile = await load_profile(session)

    uploads = sum(len(c.servers) for c in changes)
    await ctx.log(f"{len(changes)} posters to push ({uploads} uploads)")
    counts: dict[str, int] = defaultdict(int)
    done = 0

    async with AsyncExitStack() as stack:
        clients: dict[int, MediaServerClient] = {}
        for sid, server in servers.items():
            if server.enabled:
                clients[sid] = await stack.enter_async_context(
                    client_for(server, concurrency=profile.poster_concurrency)
                )

        async def push(change: Change) -> list[tuple[ServerChange, str | None, str]]:
            """Returns (server change, new ref, base hash) for each upload that worked."""
            async with sm() as session:
                data = await _base_bytes(session, change, clients, config_dir)
            fingerprint = await asyncio.to_thread(poster_hash, data)
            content_type = await asyncio.to_thread(_content_type, data)

            async def upload(sc: ServerChange):
                try:
                    new_ref = await clients[sc.server_id].upload_poster(
                        sc.remote_id, data, content_type
                    )
                    return sc, new_ref, fingerprint
                except MediaServerError as exc:
                    counts["failed"] += 1
                    await ctx.log(f"{servers[sc.server_id].name}: {change.title}: {exc}")
                    return None

            results = await asyncio.gather(*(upload(sc) for sc in change.servers))
            return [r for r in results if r]

        for start in range(0, len(changes), CHUNK):
            batch = changes[start : start + CHUNK]
            results = await asyncio.gather(*(push(c) for c in batch), return_exceptions=True)
            async with sm() as session:
                for change, result in zip(batch, results, strict=True):
                    if isinstance(result, BaseException):
                        counts["failed"] += len(change.servers)
                        await ctx.log(f"{change.title}: {result}")
                        continue
                    model = ServerItem if change.target == "item" else ServerCollection
                    for sc, new_ref, fingerprint in result:
                        row = await session.get(model, sc.row_id)
                        if row is None:
                            continue
                        row.poster_ref = new_ref or row.poster_ref
                        row.pushed_ref = row.poster_ref
                        row.poster_hash, row.poster_hash_ref = fingerprint, row.poster_ref
                        row.labels = [
                            label for label in row.labels or [] if str(label).lower() != "overlay"
                        ]
                        counts["uploaded"] += 1
                    if result and len(result) == len(change.servers):
                        counts["pushed"] += 1
                await session.commit()
            done += len(batch)
            await ctx.progress(0.98 * done / max(len(changes), 1), f"Pushed {done}/{len(changes)}")

    from coverarr.services.review import refresh_issues

    async with sm() as session:
        await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Push complete")
    await ctx.log(
        f"Uploaded {counts['uploaded']} posters for {counts['pushed']} titles/collections"
        + (f"; {counts['failed']} failed" if counts["failed"] else "")
    )
    return {"titles": len(changes), **counts}
