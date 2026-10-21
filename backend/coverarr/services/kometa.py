"""Importing from Kometa: original (pre-overlay) posters and collection YAML files.

Kometa keeps the clean poster of everything it overlays in
``config/overlays/<Library> Original Posters/<ratingKey>.<ext>``. Those are exactly the base
images Coverarr needs, keyed by the Plex rating key of the movie/show/season/episode or
collection they belong to. They're stored in the assets tree (see services/assets.py) as
``poster-kometa.<ext>`` and friends.
"""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import Artwork, Collection, MediaItem, Server, ServerCollection, ServerItem
from coverarr.services import cpu
from coverarr.services.assets import artwork_path, describe_owners, taken_paths, write_asset
from coverarr.services.jobs import JobContext
from coverarr.services.posters import fingerprints

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
FORMAT_SUFFIX = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
BATCH = 200
# Larger files aren't posters; skipping them by their listed size means a zip bomb is never
# unpacked into memory (zipfile stops reading an entry at its listed size).
MAX_ENTRY_BYTES = 25 * 1024 * 1024


def rating_key(name: str) -> str | None:
    """``12345.png`` -> ``12345``; anything else isn't a Kometa original."""
    path = PurePosixPath(name.replace("\\", "/"))
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    return path.stem if path.stem.isdigit() else None


class Source:
    """A zip or folder of originals. Stays open until ``close()`` so entries can be read lazily."""

    def __init__(self, path: Path):
        self.path = path
        self._zip = None if path.is_dir() else zipfile.ZipFile(path)

    def entries(self) -> list[tuple[str, Any]]:
        """(name, reader) pairs; reader() returns the file's bytes."""
        if self._zip is None:
            return [
                (str(p.relative_to(self.path)), p.read_bytes)
                for p in sorted(self.path.rglob("*"))
                if p.is_file() and p.stat().st_size <= MAX_ENTRY_BYTES
            ]
        archive = self._zip
        return [
            (info.filename, (lambda i=info: archive.read(i)))
            for info in archive.infolist()
            if not info.is_dir()
            and "__MACOSX" not in info.filename
            and info.file_size <= MAX_ENTRY_BYTES
        ]

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


@dataclass
class Prepared:
    key: str
    name: str
    data: bytes
    suffix: str
    width: int
    height: int
    hash: str | None
    edges: str | None = None


def prepare(name: str, data: bytes) -> Prepared | None:
    """Validate an image and fingerprint it; ``None`` if it isn't a readable image."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            suffix = FORMAT_SUFFIX.get(img.format or "", "jpg")
            width, height = img.size
    except Exception:
        return None
    try:
        fingerprint, edges = fingerprints(data)
    except Exception:
        fingerprint = edges = None
    return Prepared(rating_key(name) or "", name, data, suffix, width, height, fingerprint, edges)


def _prepare_meta(name: str, data: bytes) -> Prepared | None:
    """``prepare`` for a worker process: the image bytes aren't sent back."""
    prepared = prepare(name, data)
    if prepared is not None:
        prepared.data = b""
    return prepared


async def prepare_async(name: str, data: bytes) -> Prepared | None:
    """``prepare`` on the shared worker pool, so many images use every core."""
    prepared = await cpu.run(_prepare_meta, name, data)
    if prepared is not None:
        prepared.data = data
    return prepared


async def _plex_server(session: AsyncSession, server_id: int | None) -> Server:
    if server_id is not None:
        server = await session.get(Server, server_id)
        if server is None or server.kind != "plex":
            raise ValueError("Pick a Plex server: Kometa originals are named by Plex rating key")
        return server
    plex = list(await session.scalars(select(Server).where(Server.kind == "plex")))
    if len(plex) != 1:
        raise ValueError("Choose which Plex server Kometa ran against")
    return plex[0]


async def run_kometa_originals(ctx: JobContext) -> dict[str, Any]:
    try:
        return await _kometa_originals(ctx)
    finally:
        # An uploaded zip goes however the job ends.
        if ctx.params.get("remove_after"):
            await asyncio.to_thread(Path(ctx.params["source"]).unlink, missing_ok=True)


async def _kometa_originals(ctx: JobContext) -> dict[str, Any]:
    config_dir = Path(ctx.params["config_dir"])
    source = Path(ctx.params["source"])

    async with ctx.sessionmaker() as session:
        server = await _plex_server(session, ctx.params.get("server_id"))
        items = {
            rid: mi
            for rid, mi in (
                await session.execute(
                    select(ServerItem.remote_id, ServerItem.media_item_id).where(
                        ServerItem.server_id == server.id, ServerItem.media_item_id.is_not(None)
                    )
                )
            ).all()
        }
        colls = {
            rid: cid
            for rid, cid in (
                await session.execute(
                    select(ServerCollection.remote_id, ServerCollection.collection_id).where(
                        ServerCollection.server_id == server.id,
                        ServerCollection.collection_id.is_not(None),
                    )
                )
            ).all()
        }
    await ctx.log(
        f"Matching Kometa originals against {server.name}: "
        f"{len(items)} items and {len(colls)} collections known"
    )

    counts = {"imported": 0, "unmatched": 0, "not_original": 0, "unreadable": 0}
    unmatched_examples: list[str] = []
    imported_items: dict[int, int] = {}  # media_item_id -> artwork id
    imported_colls: dict[int, int] = {}

    async with ctx.sessionmaker() as session:
        taken = await taken_paths(session)

    async def store(batch: list[Prepared]) -> None:
        async with ctx.sessionmaker() as session:
            owners = await describe_owners(
                session,
                [items[p.key] for p in batch if p.key in items],
                [colls[p.key] for p in batch if p.key not in items],
            )
            for p in batch:
                media_item_id = items.get(p.key)
                collection_id = None if media_item_id else colls.get(p.key)
                key = f"m{media_item_id}" if media_item_id else f"c{collection_id}"
                existing = await session.scalar(
                    select(Artwork).where(
                        Artwork.source == "kometa",
                        Artwork.media_item_id == media_item_id
                        if media_item_id
                        else Artwork.collection_id == collection_id,
                    )
                )
                folder, stem = owners[key]
                rel = artwork_path(key, folder, stem, "kometa", p.suffix, taken)
                old = existing.file_path if existing else None
                await asyncio.to_thread(write_asset, config_dir, rel, p.data, old)
                art = existing or Artwork(
                    source="kometa", media_item_id=media_item_id, collection_id=collection_id
                )
                art.file_path, art.original_name = str(rel), p.name
                art.width, art.height, art.poster_hash = p.width, p.height, p.hash
                art.poster_edges = p.edges
                session.add(art)
                await session.flush()
                (imported_items if media_item_id else imported_colls)[
                    media_item_id or collection_id
                ] = art.id
            await session.commit()

    pending: list[tuple[str, Any]] = []
    seen = 0

    async def flush() -> None:
        nonlocal pending
        if not pending:
            return

        def read(entries: list[tuple[str, Any]]) -> list[bytes]:
            return [reader() for _, reader in entries]

        # Reading the zip stays in one thread; decoding and fingerprinting use every core.
        datas = await asyncio.to_thread(read, pending)
        prepared = await asyncio.gather(
            *(prepare_async(name, data) for (name, _), data in zip(pending, datas, strict=True))
        )
        ok = []
        for (name, _), p in zip(pending, prepared, strict=True):
            if p is None:
                counts["unreadable"] += 1
                await ctx.log(f"Skipped unreadable image {name}")
            else:
                ok.append(p)
        await store(ok)
        counts["imported"] += len(ok)
        pending = []

    reader_source = await asyncio.to_thread(Source, source)
    try:
        entries = await asyncio.to_thread(reader_source.entries)
        total = len(entries)
        for name, reader in entries:
            seen += 1
            key = rating_key(name)
            if key is None:
                counts["not_original"] += 1
            elif key not in items and key not in colls:
                counts["unmatched"] += 1
                if len(unmatched_examples) < 20:
                    unmatched_examples.append(name)
            else:
                pending.append((name, reader))
                if len(pending) >= BATCH:
                    await flush()
            if seen % 100 == 0:
                await ctx.progress(0.9 * seen / max(total, 1), f"Read {seen}/{total} files")
        await flush()
    finally:
        reader_source.close()

    # Use the originals as the base wherever the user hasn't chosen one, or chose Plex's own
    # (overlaid) poster.
    plex_source = f"server:{server.id}"
    applied = kept = 0
    async with ctx.sessionmaker() as session:
        earlier_kometa = {
            f"artwork:{art_id}"
            for art_id in await session.scalars(
                select(Artwork.id).where(Artwork.source == "kometa")
            )
        }
        for model, mapping in ((MediaItem, imported_items), (Collection, imported_colls)):
            for ids in _chunks(list(mapping), 500):
                rows = (
                    await session.execute(
                        select(model.id, model.poster_source).where(model.id.in_(ids))
                    )
                ).all()
                for row_id, current in rows:
                    # Replace an earlier Kometa original, never the user's own upload or pick.
                    if current in (None, plex_source) or current in earlier_kometa:
                        await session.execute(
                            update(model)
                            .where(model.id == row_id)
                            .values(poster_source=f"artwork:{mapping[row_id]}")
                        )
                        applied += 1
                    else:
                        kept += 1
        from coverarr.services.review import refresh_issues

        issues = await refresh_issues(session)
        await session.commit()

    await ctx.log(
        f"Imported {counts['imported']} originals; {counts['unmatched']} didn't match anything "
        f"on {server.name}; used as base for {applied}, kept your own choice for {kept}"
    )
    if unmatched_examples:
        await ctx.log("Unmatched examples: " + ", ".join(unmatched_examples[:10]))
    await ctx.progress(1.0, "Kometa originals imported")
    return {
        **counts,
        "applied": applied,
        "kept_choice": kept,
        "unmatched_examples": unmatched_examples,
        "issues": issues,
    }


def _chunks(values: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
