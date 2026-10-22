"""Backgrounds (fanart) as managed artwork.

A film or show can have a chosen background, like its base poster: another server's
("server:<id>"), or one Coverarr keeps itself ("background:<id>", an upload or a TMDb
backdrop). Pushing uploads it to every server that doesn't show it yet. Coverarr remembers the
background ref each server reported right after its upload and what it uploaded, so a server
counts as done until its background changes again; there's no fingerprinting, and titles with
no chosen background are left alone.
"""

from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import MediaServerClient, MediaServerError
from coverarr.models import Background, MediaItem, Server, ServerItem
from coverarr.services.artwork import (
    TMDB_IMAGES,
    ArtworkError,
    _list_client,
    _tmdb_images_path,
)
from coverarr.services.assets import ASSETS
from coverarr.services.jobs import JobContext
from coverarr.services.lists import ListError

FOLDER = ASSETS / "backgrounds"
MAX_BYTES = 25 * 1024 * 1024
FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}


def _inspect(data: bytes) -> tuple[str, str, int, int]:
    """(suffix, content type, width, height); ArtworkError if it isn't an image we take."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            fmt, (width, height) = (img.format or "").upper(), img.size
    except Exception as exc:
        raise ArtworkError("That file isn't a JPEG, PNG or WebP image") from exc
    if fmt not in FORMATS:
        raise ArtworkError("That file isn't a JPEG, PNG or WebP image")
    suffix, content_type = FORMATS[fmt]
    return suffix, content_type, width, height


def content_type(data: bytes) -> str:
    try:
        return _inspect(data)[1]
    except ArtworkError:
        return "image/jpeg"


async def save_background(
    session: AsyncSession,
    config_dir: Path,
    *,
    media_item_id: int,
    source: str,
    data: bytes,
    name: str | None = None,
    choose: bool = True,
) -> Background:
    """Store ``data`` as this title's ``source`` background (replacing an earlier one from
    the same source) and, with ``choose``, make it the background every server gets."""
    if len(data) > MAX_BYTES:
        raise ArtworkError("That image is larger than 25 MB")
    item = await session.get(MediaItem, media_item_id)
    if item is None or item.kind not in ("movie", "show"):
        raise ArtworkError("Backgrounds are for films and shows")
    suffix, _, width, height = await asyncio.to_thread(_inspect, data)
    old = await session.scalar(
        select(Background).where(
            Background.media_item_id == media_item_id, Background.source == source
        )
    )
    rel = FOLDER / f"m{media_item_id}-{source}.{suffix}"

    def write() -> None:
        dest = config_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if old is not None and old.file_path != str(rel):
            (config_dir / old.file_path).unlink(missing_ok=True)

    await asyncio.to_thread(write)
    was_chosen = old is not None and item.background_source == f"background:{old.id}"
    # A fresh row (new id) each time, so cached thumbnails never show the old image.
    bg = Background(
        media_item_id=media_item_id,
        source=source,
        file_path=str(rel),
        original_name=name,
        width=width,
        height=height,
    )
    session.add(bg)
    await session.flush()
    if old is not None:
        await session.delete(old)
    if choose or was_chosen:
        item.background_source = f"background:{bg.id}"
    return bg


async def delete_background(session: AsyncSession, config_dir: Path, bg: Background) -> None:
    item = await session.get(MediaItem, bg.media_item_id)
    if item is not None and item.background_source == f"background:{bg.id}":
        item.background_source = None
    await asyncio.to_thread((config_dir / bg.file_path).unlink, missing_ok=True)
    await session.delete(bg)


async def tmdb_backdrops(session: AsyncSession, media_item_id: int) -> dict[str, Any]:
    """Background candidates from TMDb, textless first."""
    item = await session.get(MediaItem, media_item_id)
    if item is None or item.kind not in ("movie", "show"):
        raise ArtworkError("Backgrounds are for films and shows")
    client = await _list_client(session)
    async with client:
        try:
            path, label = await _tmdb_images_path(session, client, media_item_id, None)
            data = await client.tmdb_json(path, include_image_language="en,null")
        except ListError as exc:
            raise ArtworkError(str(exc)) from exc
    backdrops = sorted(
        data.get("backdrops") or [],
        key=lambda b: (
            {None: 0, "en": 1}.get(b.get("iso_639_1"), 2),
            -(b.get("vote_average") or 0),
            -(b.get("width") or 0),
        ),
    )
    return {
        "source": label,
        "backdrops": [
            {
                "file_path": b["file_path"],
                "thumb": f"{TMDB_IMAGES}/w780{b['file_path']}",
                "width": b.get("width"),
                "height": b.get("height"),
                "language": b.get("iso_639_1"),
                "votes": b.get("vote_count") or 0,
                "rating": round(b.get("vote_average") or 0, 1),
            }
            for b in backdrops[:40]
        ],
    }


# --- what needs pushing ----------------------------------------------------------------------


@dataclass
class BackgroundTarget:
    server_id: int
    row_id: int
    remote_id: str
    reason: str


@dataclass
class BackgroundChange:
    media_item_id: int
    title: str
    kind: str
    library_id: int
    source: str
    key: str = ""  # what every server should end up showing
    problem: str | None = None
    servers: list[BackgroundTarget] = field(default_factory=list)
    # Where the image comes from when pushing.
    from_server: tuple[int, str, str] | None = None  # server id, remote id, background ref
    from_file: str | None = None

    def out(self) -> dict[str, Any]:
        return {
            "id": self.media_item_id,
            "title": self.title,
            "kind": self.kind,
            "library_id": self.library_id,
            "source": self.source,
            "problem": self.problem,
            "servers": [{"server_id": t.server_id, "reason": t.reason} for t in self.servers],
        }


BG_COLS = (
    ServerItem.id,
    ServerItem.server_id,
    ServerItem.remote_id,
    ServerItem.media_item_id,
    ServerItem.background_ref,
    ServerItem.background_pushed_ref,
    ServerItem.background_pushed_key,
)


async def background_changes(
    session: AsyncSession,
    *,
    media_item_ids: list[int] | None = None,
    library_id: int | None = None,
) -> list[BackgroundChange]:
    """Every chosen background that some server doesn't show yet (or can't be pushed)."""
    where = [MediaItem.background_source.is_not(None)]
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
                MediaItem.background_source,
            ).where(*where)
        )
    ).all()
    if not items:
        return []
    chosen = select(MediaItem.id).where(*where)
    enabled = select(Server.id).where(Server.enabled.is_(True))
    rows: dict[int, list[Any]] = defaultdict(list)
    for r in await session.execute(
        select(*BG_COLS)
        .where(ServerItem.media_item_id.in_(chosen), ServerItem.server_id.in_(enabled))
        .order_by(ServerItem.server_id)
    ):
        rows[r.media_item_id].append(r)
    files = dict(
        (
            await session.execute(
                select(Background.id, Background.file_path).where(
                    Background.media_item_id.in_(chosen)
                )
            )
        ).all()
    )
    out: list[BackgroundChange] = []
    for item in items:
        title = f"{item.title} ({item.year})" if item.year else item.title
        change = BackgroundChange(
            item.id, title, item.kind, item.library_id, item.background_source
        )
        kind, _, ref = item.background_source.partition(":")
        source_server: int | None = None
        if kind == "server" and ref.isdigit():
            source_server = int(ref)
            src = next((r for r in rows[item.id] if r.server_id == source_server), None)
            if src is None or not src.background_ref:
                change.problem = "The server you picked the background from has none now"
            else:
                change.key = f"s:{src.server_id}:{src.background_ref}"
                change.from_server = (src.server_id, src.remote_id, src.background_ref)
        elif kind == "background" and ref.isdigit() and int(ref) in files:
            change.key = f"a:{ref}"
            change.from_file = files[int(ref)]
        else:
            change.problem = "The chosen background no longer exists"
        if change.problem is None:
            for row in rows[item.id]:
                if row.server_id == source_server:
                    continue
                if (
                    row.background_pushed_key == change.key
                    and row.background_ref
                    and (row.background_ref == row.background_pushed_ref)
                ):
                    continue
                if not row.background_ref:
                    reason = "no background"
                elif row.background_pushed_key == change.key:
                    reason = "changed on the server"
                else:
                    reason = "different background"
                change.servers.append(
                    BackgroundTarget(row.server_id, row.id, row.remote_id, reason)
                )
        if change.servers or change.problem:
            out.append(change)
    out.sort(key=lambda c: c.title.lower())
    return out


# --- pushing ---------------------------------------------------------------------------------


async def push_backgrounds(
    ctx: JobContext,
    changes: list[BackgroundChange],
    clients: dict[int, MediaServerClient],
    config_dir: Path,
    names: dict[int, str],
) -> dict[str, int]:
    """Upload each chosen background to the servers that don't show it yet."""
    counts = {"backgrounds": 0, "background_titles": 0, "backgrounds_failed": 0}
    lock = asyncio.Semaphore(8)

    async def one(change: BackgroundChange) -> None:
        async with lock:
            try:
                if change.from_server is not None:
                    server_id, remote_id, ref = change.from_server
                    if server_id not in clients:
                        raise ValueError("the server it comes from is disabled")
                    data = await clients[server_id].fetch_background(remote_id, ref)
                else:
                    assert change.from_file is not None
                    data = await asyncio.to_thread((config_dir / change.from_file).read_bytes)
            except (MediaServerError, OSError, ValueError) as exc:
                counts["backgrounds_failed"] += len(change.servers)
                await ctx.log(f"{change.title}: background: {exc}")
                return
            kind = await asyncio.to_thread(content_type, data)
            done: list[tuple[int, str | None]] = []
            for target in change.servers:
                client = clients.get(target.server_id)
                if client is None:
                    continue
                try:
                    new_ref = await client.upload_background(target.remote_id, data, kind)
                except MediaServerError as exc:
                    counts["backgrounds_failed"] += 1
                    await ctx.log(
                        f"{names.get(target.server_id)}: {change.title}: background: {exc}"
                    )
                    continue
                done.append((target.row_id, new_ref))
            async with ctx.sessionmaker() as session:
                for row_id, new_ref in done:
                    row = await session.get(ServerItem, row_id)
                    if row is None:
                        continue
                    row.background_ref = new_ref or row.background_ref
                    row.background_pushed_ref = row.background_ref
                    row.background_pushed_key = change.key
                await session.commit()
            counts["backgrounds"] += len(done)
            if done and len(done) == len(change.servers):
                counts["background_titles"] += 1

    await asyncio.gather(*(one(c) for c in changes if not c.problem))
    return counts
