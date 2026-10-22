"""Metadata edits: a film's or show's title, sort title, summary and labels, set once in
Coverarr and pushed to every server (and locked there, so a metadata refresh keeps them).

Only the fields you set are managed. Coverarr remembers what it pushed to each server, so a
server is listed again when the edits change, or when an import shows its title, sort title or
labels drifting away from them. Labels you take off the edits come off the servers; labels set
there by hand are left alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import MediaServerClient, MediaServerError
from coverarr.models import MediaItem, Server, ServerItem, ServerLibrary
from coverarr.services.definitions import split_labels
from coverarr.services.jobs import JobContext

FIELDS = ("title", "sort_title", "summary", "labels")


def clean_edits(raw: dict[str, Any]) -> dict[str, Any]:
    """The fields that are set; blank ones are dropped (left as each server has them)."""
    out: dict[str, Any] = {}
    for key in ("title", "sort_title", "summary"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if raw.get("labels") is not None:
        labels = split_labels(raw["labels"])
        if labels:
            out["labels"] = labels
    return out


def edits_key(edits: dict[str, Any]) -> str:
    blob = json.dumps({k: edits.get(k) for k in FIELDS}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


@dataclass
class MetaTarget:
    server_id: int
    row_id: int
    remote_id: str
    library_remote_id: str | None
    reason: str


@dataclass
class MetaChange:
    media_item_id: int
    title: str
    kind: str
    library_id: int
    edits: dict[str, Any]
    key: str
    servers: list[MetaTarget] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        return {
            "id": self.media_item_id,
            "title": self.title,
            "kind": self.kind,
            "library_id": self.library_id,
            "edits": self.edits,
            "servers": [{"server_id": t.server_id, "reason": t.reason} for t in self.servers],
        }


def _drift(row: Any, edits: dict[str, Any]) -> str | None:
    """Why a server that had the edits pushed no longer matches them (from the last import)."""
    if "title" in edits and row.title != edits["title"]:
        return "title changed on the server"
    if "sort_title" in edits and (row.sort_title or "") != edits["sort_title"]:
        return "sort title changed on the server"
    have = {x.lower() for x in row.labels or []}
    if any(label.lower() not in have for label in edits.get("labels", [])):
        return "labels changed on the server"
    return None


async def metadata_changes(
    session: AsyncSession,
    *,
    media_item_ids: list[int] | None = None,
    library_id: int | None = None,
) -> list[MetaChange]:
    """Every title whose metadata edits some server doesn't have yet."""
    where = [MediaItem.metadata_edits.is_not(None)]
    if media_item_ids is not None:
        where.append(MediaItem.id.in_(media_item_ids))
    if library_id is not None:
        where.append(MediaItem.library_id == library_id)
    items = (
        await session.execute(
            select(
                MediaItem.id,
                MediaItem.title,
                MediaItem.year,
                MediaItem.kind,
                MediaItem.library_id,
                MediaItem.metadata_edits,
            ).where(*where)
        )
    ).all()
    items = [i for i in items if i.metadata_edits]
    if not items:
        return []
    enabled = select(Server.id).where(Server.enabled.is_(True))
    rows: dict[int, list[Any]] = defaultdict(list)
    for r in await session.execute(
        select(
            ServerItem.id,
            ServerItem.server_id,
            ServerItem.remote_id,
            ServerItem.media_item_id,
            ServerItem.title,
            ServerItem.sort_title,
            ServerItem.labels,
            ServerItem.metadata_pushed_key,
            ServerLibrary.remote_id.label("library_remote_id"),
        )
        .join(ServerLibrary, ServerLibrary.id == ServerItem.server_library_id)
        .where(
            ServerItem.media_item_id.in_([i.id for i in items]),
            ServerItem.server_id.in_(enabled),
        )
        .order_by(ServerItem.server_id)
    ):
        rows[r.media_item_id].append(r)
    out: list[MetaChange] = []
    for item in items:
        edits = item.metadata_edits
        key = edits_key(edits)
        title = f"{item.title} ({item.year})" if item.year else item.title
        change = MetaChange(item.id, title, item.kind, item.library_id, edits, key)
        for row in rows[item.id]:
            if row.metadata_pushed_key != key:
                reason = "not pushed yet"
            else:
                reason = _drift(row, edits)
                if reason is None:
                    continue
            change.servers.append(
                MetaTarget(row.server_id, row.id, row.remote_id, row.library_remote_id, reason)
            )
        if change.servers:
            out.append(change)
    out.sort(key=lambda c: c.title.lower())
    return out


async def push_metadata(
    ctx: JobContext,
    changes: list[MetaChange],
    clients: dict[int, MediaServerClient],
    names: dict[int, str],
) -> dict[str, int]:
    """Push each title's edits to the servers that don't have them."""
    counts = {"metadata": 0, "metadata_failed": 0}
    sem = asyncio.Semaphore(8)

    async def one(change: MetaChange) -> None:
        async with sem:
            edits = change.edits
            labels = edits.get("labels", [])
            for target in change.servers:
                client = clients.get(target.server_id)
                if client is None:
                    continue
                async with ctx.sessionmaker() as session:
                    row = await session.get(ServerItem, target.row_id)
                    if row is None:
                        continue
                    have = {x.lower() for x in row.labels or []}
                    wanted = {x.lower() for x in labels}
                    add = [x for x in labels if x.lower() not in have]
                    remove = [
                        x
                        for x in row.metadata_pushed_labels or []
                        if x.lower() not in wanted and x.lower() in have
                    ]
                    try:
                        await client.edit_metadata(
                            target.library_remote_id,
                            target.remote_id,
                            change.kind,
                            title=edits.get("title"),
                            sort_title=edits.get("sort_title"),
                            summary=edits.get("summary"),
                        )
                        await client.edit_labels(
                            target.library_remote_id, target.remote_id, change.kind, add, remove
                        )
                    except MediaServerError as exc:
                        counts["metadata_failed"] += 1
                        await ctx.log(f"{names.get(target.server_id)}: {change.title}: {exc}")
                        continue
                    row.title = edits.get("title", row.title)
                    row.sort_title = edits.get("sort_title", row.sort_title)
                    gone = {x.lower() for x in remove}
                    row.labels = [x for x in row.labels or [] if x.lower() not in gone] + add
                    row.metadata_pushed_key = change.key
                    row.metadata_pushed_labels = labels
                    await session.commit()
                counts["metadata"] += 1

    await asyncio.gather(*(one(c) for c in changes))
    return counts
