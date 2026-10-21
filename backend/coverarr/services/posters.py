"""Poster fingerprinting and the incremental poster scan.

Each server's poster reference (Plex thumb path, Jellyfin/Emby image tag) changes whenever the
artwork changes. The scan only downloads posters whose reference differs from the one last
hashed, so after the first run it touches just what actually changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import MediaServerClient, MediaServerError, client_for
from coverarr.models import MediaItem, Server, ServerCollection, ServerItem
from coverarr.services import cpu
from coverarr.services.client_pool import ClientPool
from coverarr.services.jobs import JobContext
from coverarr.services.load import load_profile

log = logging.getLogger(__name__)

HASH_WIDTH = 200
# Overlay gradients (e.g. Kometa's jmxd set) cover ~17% at the top and bottom, and badges sit
# in the corners. Hashing only the middle band means "same artwork, different overlay" matches.
CROP_FRACTION = 0.18


# Edge signature: a 16x24 greyscale thumbnail. The middle-band hash above deliberately ignores
# the top and bottom of a poster (so "same artwork, other overlays" still matches); this
# keeps those edges, so overlays on otherwise identical artwork can be told apart.
EDGE_W, EDGE_H, EDGE_ROWS = 16, 24, 4
EDGE_MEAN, EDGE_MAX = 6, 30  # re-encoding moves edges ~1; jmxd gradients ~50, badges 26+


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def poster_hash(data: bytes) -> str:
    """64-bit difference hash of the poster's middle band."""
    with Image.open(io.BytesIO(data)) as img:
        gray = img.convert("L")
        w, h = gray.size
        crop = int(h * CROP_FRACTION)
        band = gray.crop((0, crop, w, h - crop)).resize((9, 8), Image.Resampling.LANCZOS)
        px = band.load()
        bits = 0
        for y in range(8):
            for x in range(8):
                bits = (bits << 1) | int(px[x, y] > px[x + 1, y])
    return f"{bits:016x}"


def edge_signature(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as img:
        small = img.convert("L").resize((EDGE_W, EDGE_H), Image.Resampling.BOX)
        return small.tobytes().hex()


def edges_differ(a: str | None, b: str | None) -> bool | None:
    """Whether two posters' top and bottom edges differ (``None`` if either is unknown)."""
    if not a or not b or len(a) != len(b):
        return None
    pa, pb = bytes.fromhex(a), bytes.fromhex(b)
    rows = [*range(EDGE_ROWS), *range(EDGE_H - EDGE_ROWS, EDGE_H)]
    diffs = [abs(pa[r * EDGE_W + c] - pb[r * EDGE_W + c]) for r in rows for c in range(EDGE_W)]
    return sum(diffs) / len(diffs) >= EDGE_MEAN or max(diffs) >= EDGE_MAX


def fingerprints(data: bytes) -> tuple[str, str]:
    """(middle-band hash, edge signature)."""
    return poster_hash(data), edge_signature(data)


def _file_edges(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return edge_signature(fh.read())
    except (OSError, ValueError):
        return None


async def _hash_one(client: MediaServerClient, remote_id: str, ref: str) -> tuple[str, str] | None:
    try:
        data = await client.fetch_poster(remote_id, ref, max_width=HASH_WIDTH)
        return await cpu.run(fingerprints, data)
    except (MediaServerError, OSError, ValueError) as exc:
        log.warning("Could not hash poster %s: %s", remote_id, exc)
        return None


async def _pending(session: AsyncSession, server_id: int) -> list[tuple[str, int, str, str]]:
    """(table, row id, remote id, poster ref) for posters needing a (re)hash."""
    stale = or_(
        ServerItem.poster_hash_ref.is_(None),
        ServerItem.poster_hash_ref != ServerItem.poster_ref,
        ServerItem.poster_edges.is_(None),
    )
    items = await session.execute(
        select(ServerItem.id, ServerItem.remote_id, ServerItem.poster_ref).where(
            ServerItem.server_id == server_id,
            # Episodes only when a base poster was chosen for them (e.g. Kometa originals):
            # that's when we need to know whether each server already shows it.
            or_(
                ServerItem.kind.in_(("movie", "show", "season")),
                ServerItem.media_item_id.in_(
                    select(MediaItem.id).where(
                        MediaItem.poster_source.is_not(None), MediaItem.kind == "episode"
                    )
                ),
            ),
            ServerItem.media_item_id.is_not(None),
            ServerItem.poster_ref.is_not(None),
            stale,
        )
    )
    stale_c = or_(
        ServerCollection.poster_hash_ref.is_(None),
        ServerCollection.poster_hash_ref != ServerCollection.poster_ref,
        ServerCollection.poster_edges.is_(None),
    )
    colls = await session.execute(
        select(ServerCollection.id, ServerCollection.remote_id, ServerCollection.poster_ref).where(
            ServerCollection.server_id == server_id,
            ServerCollection.collection_id.is_not(None),
            ServerCollection.poster_ref.is_not(None),
            stale_c,
        )
    )
    return [("item", *r) for r in items.all()] + [("collection", *r) for r in colls.all()]


async def _backfill_artwork_edges(sm: Any, ctx: JobContext) -> None:
    """Edge signatures for artwork stored before they existed (read from the local files)."""
    from coverarr.models import Artwork
    from coverarr.services.overlays import config_dir

    async with sm() as session:
        rows = (
            await session.execute(
                select(Artwork.id, Artwork.file_path).where(Artwork.poster_edges.is_(None))
            )
        ).all()
    if not rows:
        return
    root = config_dir()

    for start in range(0, len(rows), 200):
        batch = rows[start : start + 200]
        sigs = await asyncio.gather(*(cpu.run(_file_edges, str(root / r.file_path)) for r in batch))
        updates = [{"id": r.id, "poster_edges": s} for r, s in zip(batch, sigs, strict=True) if s]
        if updates:
            async with sm() as session:
                await session.execute(update(Artwork), updates)
                await session.commit()
    await ctx.log(f"Edge signatures for {len(rows)} stored artworks")


async def run_poster_scan(ctx: JobContext) -> dict[str, Any]:
    from coverarr.services.review import refresh_issues

    sm = ctx.sessionmaker
    async with sm() as session:
        servers = list(await session.scalars(select(Server).where(Server.enabled.is_(True))))
        work = {s.id: await _pending(session, s.id) for s in servers}
        profile = await load_profile(session)
    total = sum(len(v) for v in work.values())
    await ctx.log(f"{total} posters to fingerprint")
    done = 0
    counts: dict[str, int] = defaultdict(int)
    lock = asyncio.Lock()

    async def scan_server(server: Server) -> None:
        nonlocal done
        rows = work[server.id]
        if not rows:
            return
        async with client_for(server, concurrency=profile.poster_concurrency) as client:
            for start in range(0, len(rows), 100):
                batch = rows[start : start + 100]
                hashes = await asyncio.gather(
                    *(_hash_one(client, remote_id, ref) for _, _, remote_id, ref in batch)
                )
                items, colls = [], []
                for (table, row_id, _, ref), h in zip(batch, hashes, strict=True):
                    if h is None:
                        counts["failed"] += 1
                        continue
                    target = items if table == "item" else colls
                    target.append(
                        {
                            "id": row_id,
                            "poster_hash": h[0],
                            "poster_edges": h[1],
                            "poster_hash_ref": ref,
                        }
                    )
                async with lock, sm() as session:
                    if items:
                        await session.execute(update(ServerItem), items)
                    if colls:
                        await session.execute(update(ServerCollection), colls)
                    await session.commit()
                counts["hashed"] += len(items) + len(colls)
                done += len(batch)
                await ctx.progress(0.95 * done / max(total, 1), f"Fingerprinted {done}/{total}")

    await _backfill_artwork_edges(sm, ctx)
    results = await asyncio.gather(*(scan_server(s) for s in servers), return_exceptions=True)
    for server, res in zip(servers, results, strict=True):
        if isinstance(res, Exception):
            await ctx.log(f"{server.name}: poster scan failed: {res}")
            counts["server_errors"] += 1

    async with sm() as session:
        issues = await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Poster scan complete")
    await ctx.log(f"Hashed {counts['hashed']}, failed {counts['failed']}; issues: {issues}")
    return {"total": total, **counts, "issues": issues}


class ThumbnailCache:
    """Disk cache for posters shown in the UI, keyed by server, row and poster ref.

    Because the poster ref changes with the artwork, a cached file never goes stale.
    """

    def __init__(self, root: Path, pool: ClientPool):
        self.root = root
        self.pool = pool

    def path_for(self, server_id: int, remote_id: str, ref: str, width: int) -> Path:
        digest = hashlib.sha1(f"{server_id}:{remote_id}:{ref}:{width}".encode()).hexdigest()
        return self.root / "thumbs" / digest[:2] / f"{digest}.jpg"

    async def get(self, server: Server, remote_id: str, ref: str, width: int) -> Path:
        path = self.path_for(server.id, remote_id, ref, width)
        if path.exists():
            return path
        client = await self.pool.get(server)
        data = await client.fetch_poster(remote_id, ref, max_width=width)
        await asyncio.to_thread(self._store, path, data, width)
        return path

    async def get_local(self, key: str, source: Path, width: int) -> Path:
        """A resized copy of an image Coverarr holds itself (e.g. a Kometa original)."""
        stat = await asyncio.to_thread(source.stat)
        path = self.path_for(0, key, str(stat.st_mtime_ns), width)
        if not path.exists():
            data = await asyncio.to_thread(source.read_bytes)
            await asyncio.to_thread(self._store, path, data, width)
        return path

    @staticmethod
    def _store(path: Path, data: bytes, width: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            if img.width > width:
                img = img.resize((width, round(img.height * width / img.width)))
            tmp = path.with_suffix(".tmp")
            img.save(tmp, "JPEG", quality=85)
            tmp.replace(path)
