"""Importing from Kometa: original (pre-overlay) posters and collection YAML files.

Kometa keeps the clean poster of everything it overlays in
``config/overlays/<Library> Original Posters/<ratingKey>.<ext>``. Those are exactly the base
images Coverarr needs, keyed by the Plex rating key of the movie/show/season/episode or
collection they belong to.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import Artwork, Collection, MediaItem, Server, ServerCollection, ServerItem
from coverarr.services.jobs import JobContext
from coverarr.services.posters import poster_hash

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
FORMAT_SUFFIX = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
BATCH = 200


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
                if p.is_file()
            ]
        archive = self._zip
        return [
            (info.filename, (lambda i=info: archive.read(i)))
            for info in archive.infolist()
            if not info.is_dir() and "__MACOSX" not in info.filename
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
        fingerprint = poster_hash(data)
    except Exception:
        fingerprint = None
    return Prepared(rating_key(name) or "", name, data, suffix, width, height, fingerprint)


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
    config_dir = Path(ctx.params["config_dir"])
    source = Path(ctx.params["source"])
    remove_after = bool(ctx.params.get("remove_after"))
    assets = config_dir / "assets" / "kometa"
    assets.mkdir(parents=True, exist_ok=True)

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

    async def store(batch: list[Prepared]) -> None:
        async with ctx.sessionmaker() as session:
            labels = await _labels(
                session,
                [items[p.key] for p in batch if p.key in items],
                [colls[p.key] for p in batch if p.key not in items],
            )
            for p in batch:
                media_item_id = items.get(p.key)
                collection_id = None if media_item_id else colls.get(p.key)
                prefix = f"m{media_item_id}" if media_item_id else f"c{collection_id}"
                # The id keeps the link; the title is only there to make the folder browsable.
                label = labels.get(prefix)
                filename = f"{prefix} - {label}" if label else prefix
                rel = Path("assets") / "kometa" / f"{filename}.{p.suffix}"
                await asyncio.to_thread(_write_asset, assets, prefix, config_dir / rel, p.data)
                existing = await session.scalar(
                    select(Artwork).where(
                        Artwork.source == "kometa",
                        Artwork.media_item_id == media_item_id
                        if media_item_id
                        else Artwork.collection_id == collection_id,
                    )
                )
                art = existing or Artwork(
                    source="kometa", media_item_id=media_item_id, collection_id=collection_id
                )
                art.file_path, art.original_name = str(rel), p.name
                art.width, art.height, art.poster_hash = p.width, p.height, p.hash
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

        def work(entries: list[tuple[str, Any]]) -> list[Prepared | None]:
            return [prepare(name, reader()) for name, reader in entries]

        prepared = await asyncio.to_thread(work, pending)
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
        for model, mapping in ((MediaItem, imported_items), (Collection, imported_colls)):
            for ids in _chunks(list(mapping), 500):
                rows = (
                    await session.execute(
                        select(model.id, model.poster_source).where(model.id.in_(ids))
                    )
                ).all()
                for row_id, current in rows:
                    if current in (None, plex_source) or str(current).startswith("artwork:"):
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

    if remove_after:
        await asyncio.to_thread(source.unlink, missing_ok=True)
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


def _write_asset(folder: Path, prefix: str, dest: Path, data: bytes) -> None:
    # Replace any earlier copy, whatever its title was then.
    for old in [*folder.glob(f"{prefix}.*"), *folder.glob(f"{prefix} - *")]:
        old.unlink()
    dest.write_bytes(data)


def safe_filename(text: str, limit: int = 120) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].rstrip(" .")


async def _labels(
    session: AsyncSession, item_ids: list[int], collection_ids: list[int]
) -> dict[str, str]:
    """Readable names for asset files: "Clerks (1994)", "Spaced - S01E02 - Title"..."""
    out: dict[str, str] = {}
    if item_ids:
        rows = {
            r.id: r
            for r in (
                await session.execute(
                    select(
                        MediaItem.id,
                        MediaItem.kind,
                        MediaItem.title,
                        MediaItem.year,
                        MediaItem.parent_id,
                        MediaItem.season_number,
                        MediaItem.episode_number,
                    ).where(MediaItem.id.in_(item_ids))
                )
            ).all()
        }
        parent_ids = {r.parent_id for r in rows.values() if r.parent_id}
        parents = (
            {
                r.id: r
                for r in (
                    await session.execute(
                        select(
                            MediaItem.id,
                            MediaItem.title,
                            MediaItem.year,
                            MediaItem.parent_id,
                            MediaItem.season_number,
                        ).where(MediaItem.id.in_(parent_ids))
                    )
                ).all()
            }
            if parent_ids
            else {}
        )
        show_ids = {p.parent_id for p in parents.values() if p.parent_id}
        shows = (
            dict(
                (
                    await session.execute(
                        select(MediaItem.id, MediaItem.title).where(MediaItem.id.in_(show_ids))
                    )
                ).all()
            )
            if show_ids
            else {}
        )
        for r in rows.values():
            if r.kind in ("movie", "show"):
                label = f"{r.title} ({r.year})" if r.year else r.title
            elif r.kind == "season":
                parent = parents.get(r.parent_id)
                label = f"{parent.title if parent else 'Show'} - Season {r.season_number}"
            else:
                season = parents.get(r.parent_id)
                show = shows.get(season.parent_id) if season else None
                season_no = (season.season_number if season else 0) or 0
                number = f"S{season_no:02d}E{r.episode_number or 0:02d}"
                label = f"{show or 'Show'} - {number} - {r.title}"
            out[f"m{r.id}"] = safe_filename(label)
    if collection_ids:
        for cid, name in (
            await session.execute(
                select(Collection.id, Collection.name).where(Collection.id.in_(collection_ids))
            )
        ).all():
            out[f"c{cid}"] = safe_filename(name)
    return out


def _chunks(values: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
