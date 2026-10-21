"""Housekeeping: making Coverarr the source of truth for every poster.

* **Base audit**: every film, show, season and collection without a chosen base gets a best
  guess, with its reasons and a confidence, like the mismatch suggestions. Nothing is chosen
  until the user accepts it.
* **Store bases**: a base taken from a server is downloaded into the assets folder (as the
  title's "original" artwork), so Coverarr keeps the clean poster itself.
* **Tidy assets**: misnamed files moved, duplicates and files nothing uses removed, records
  whose file has gone dropped.

Guessing the clean poster: servers showing the same artwork (middle-band hash) are grouped;
the biggest group wins (ties go to the group not on Plex, where Kometa draws its overlays).
Within it, a poster with Kometa's label, Coverarr's overlays, or top/bottom edges darker than
just inside them (gradients) is passed over for a clean-looking one.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import String, func, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import MediaServerError, client_for
from coverarr.models import (
    Artwork,
    Collection,
    Library,
    MediaItem,
    Server,
    ServerCollection,
    ServerItem,
)
from coverarr.services.assets import ASSETS, _prune, describe_owners, owner_key, relocate_artwork
from coverarr.services.jobs import JobContext
from coverarr.services.load import load_profile
from coverarr.services.posters import EDGE_H, EDGE_W, hamming
from coverarr.services.settings_store import get_setting

LOOKS_OVERLAID = 40  # edge darkness of a jmxd/Kometa gradient is 50-90; clean posters ~0


def edge_darkness(signature: str | None) -> float | None:
    """How much darker the top and bottom edges are than just inside them."""
    if not signature or len(signature) != EDGE_W * EDGE_H * 2:
        return None
    data = bytes.fromhex(signature)

    def row(r: int) -> float:
        return sum(data[r * EDGE_W : (r + 1) * EDGE_W]) / EDGE_W

    outer = (row(0) + row(1) + row(EDGE_H - 1) + row(EDGE_H - 2)) / 4
    inner = (row(5) + row(6) + row(EDGE_H - 6) + row(EDGE_H - 7)) / 4
    return inner - outer


@dataclass
class Candidate:
    server_id: int
    server_kind: str
    row_id: int
    poster: dict[str, Any] | None
    hash: str | None
    darkness: float | None
    kometa_label: bool
    coverarr_overlay: bool
    group: int = -1

    @property
    def overlaid(self) -> bool:
        return self.kometa_label or self.coverarr_overlay


@dataclass
class Suggestion:
    target: str  # item | collection
    id: int
    title: str
    kind: str
    library_id: int | None
    top_id: int | None
    candidates: list[Candidate] = field(default_factory=list)
    suggested: int | None = None  # server id
    confidence: str = "none"  # high | medium | low | none
    reasons: list[str] = field(default_factory=list)

    def out(self, servers: dict[int, str]) -> dict[str, Any]:
        return {
            "target": self.target,
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "library_id": self.library_id,
            "top_id": self.top_id,
            "suggested_server_id": self.suggested,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "candidates": [
                {
                    "server_id": c.server_id,
                    "server": servers.get(c.server_id),
                    "poster": c.poster,
                    "group": c.group,
                    "overlaid": c.overlaid
                    or (c.darkness is not None and c.darkness >= LOOKS_OVERLAID),
                    "edge_darkness": None if c.darkness is None else round(c.darkness, 1),
                }
                for c in self.candidates
            ],
        }


def guess(s: Suggestion, threshold: int, names: dict[int, str]) -> None:
    """Pick the clean poster to suggest, with reasons and a confidence."""
    usable = [c for c in s.candidates if c.poster and c.hash]
    if not usable:
        s.reasons.append(
            "No server has a fingerprinted poster yet (run Scan posters)"
            if any(c.poster for c in s.candidates)
            else "No server has a poster for this"
        )
        return
    # Group servers showing the same artwork.
    groups: list[list[Candidate]] = []
    for c in usable:
        for g in groups:
            if hamming(g[0].hash, c.hash) <= threshold:
                g.append(c)
                break
        else:
            groups.append([c])
    for i, g in enumerate(groups):
        for c in g:
            c.group = i

    def group_rank(g: list[Candidate]) -> tuple:
        clean = [c for c in g if not c.overlaid]
        return (len(g), bool(clean), any(c.server_kind != "plex" for c in g))

    groups.sort(key=group_rank, reverse=True)
    best = groups[0]
    tie = len(groups) > 1 and len(groups[1]) == len(best)
    confidence = 3  # high, lowered below

    if len(groups) == 1:
        if len(usable) > 1:
            s.reasons.append(f"All {len(usable)} servers show the same artwork")
        else:
            s.reasons.append(f"Only {names.get(usable[0].server_id, 'one server')} has a poster")
            confidence = 2
    elif tie:
        s.reasons.append(
            f"Servers disagree ({len(groups)} different posters); picked "
            + ("the one not on Plex" if any(c.server_kind != "plex" for c in best) else "one")
        )
        confidence = 1
    else:
        s.reasons.append(f"{len(best)} of {len(usable)} servers show this artwork")
        confidence = 2

    # Within the group, prefer a clean-looking poster.
    def clean_rank(c: Candidate) -> tuple:
        dark = c.darkness if c.darkness is not None else 0.0
        return (c.overlaid, dark, c.server_kind == "plex", c.server_id)

    ranked = sorted(best, key=clean_rank)
    pick = ranked[0]
    if any(c.kometa_label for c in best if c is not pick):
        s.reasons.append("Skipped a poster with Kometa's overlays")
    if any(c.coverarr_overlay for c in best if c is not pick):
        s.reasons.append("Skipped a poster with Coverarr's overlays")
    if len(ranked) > 1 and pick.darkness is not None:
        darker = [
            c for c in ranked[1:] if c.darkness is not None and c.darkness - pick.darkness > 15
        ]
        if darker:
            s.reasons.append("Others have darker top/bottom edges (overlays left behind?)")
    if pick.overlaid:
        s.reasons.append("Every copy has overlays: upload or pick a clean poster instead")
        confidence = 0
    elif pick.darkness is not None and pick.darkness >= LOOKS_OVERLAID:
        s.reasons.append("Its top and bottom look darker too: check it has no overlays")
        confidence = min(confidence, 1)
    s.suggested = pick.server_id
    s.confidence = {3: "high", 2: "medium", 1: "low", 0: "none"}[confidence]
    if confidence == 0:
        s.suggested = None


_LABEL_OVERLAY = '%"overlay"%'


async def suggest_bases(
    session: AsyncSession,
    *,
    library_id: int | None = None,
    kinds: tuple[str, ...] = ("movie", "show", "season"),
    collections: bool = True,
    media_item_ids: list[int] | None = None,
    collection_ids: list[int] | None = None,
) -> list[Suggestion]:
    """A suggested base for every title/collection without one."""
    from coverarr.services.changes import ITEM_INFO, _labels_for, _row_poster, _shows_render

    threshold = int(await get_setting(session, "poster_match_threshold"))
    servers = {
        s.id: s for s in await session.scalars(select(Server).where(Server.enabled.is_(True)))
    }
    names = {sid: s.name for sid, s in servers.items()}
    out: list[Suggestion] = []

    if kinds and collection_ids is None:
        where = [MediaItem.poster_source.is_(None), MediaItem.kind.in_(kinds)]
        if library_id is not None:
            where.append(MediaItem.library_id == library_id)
        if media_item_ids is not None:
            where.append(MediaItem.id.in_(media_item_ids))
        targets = (await session.execute(select(*ITEM_INFO).where(*where))).all()
        if targets:
            rows: dict[int, list] = defaultdict(list)
            for r in await session.execute(
                select(
                    ServerItem.id,
                    ServerItem.server_id,
                    ServerItem.media_item_id,
                    ServerItem.poster_ref,
                    ServerItem.poster_hash,
                    ServerItem.poster_hash_ref,
                    ServerItem.poster_edges,
                    ServerItem.pushed_ref,
                    ServerItem.pushed_key,
                    type_coerce(ServerItem.labels, String)
                    .ilike(_LABEL_OVERLAY)
                    .label("overlay_label"),
                )
                .where(
                    ServerItem.media_item_id.in_(select(MediaItem.id).where(*where)),
                    ServerItem.server_id.in_(list(servers)),
                )
                .order_by(ServerItem.server_id)
            ):
                rows[r.media_item_id].append(r)
            labels = await _labels_for(session, targets)
            for t in targets:
                title, top = labels[t.id]
                s = Suggestion("item", t.id, title, t.kind, t.library_id, top)
                s.candidates = [
                    _candidate(r, servers, "server_item_id", _row_poster, _shows_render)
                    for r in rows[t.id]
                ]
                guess(s, threshold, names)
                out.append(s)

    if collections and media_item_ids is None:
        where = [Collection.poster_source.is_(None)]
        if library_id is not None:
            where.append(Collection.library_id == library_id)
        if collection_ids is not None:
            where.append(Collection.id.in_(collection_ids))
        colls = (
            await session.execute(
                select(Collection.id, Collection.name, Collection.library_id).where(*where)
            )
        ).all()
        if colls:
            crows: dict[int, list] = defaultdict(list)
            for r in await session.execute(
                select(
                    ServerCollection.id,
                    ServerCollection.server_id,
                    ServerCollection.collection_id,
                    ServerCollection.poster_ref,
                    ServerCollection.poster_hash,
                    ServerCollection.poster_hash_ref,
                    ServerCollection.poster_edges,
                    ServerCollection.pushed_ref,
                    ServerCollection.pushed_key,
                    type_coerce(ServerCollection.labels, String)
                    .ilike(_LABEL_OVERLAY)
                    .label("overlay_label"),
                )
                .where(
                    ServerCollection.collection_id.in_(select(Collection.id).where(*where)),
                    ServerCollection.server_id.in_(list(servers)),
                )
                .order_by(ServerCollection.server_id)
            ):
                crows[r.collection_id].append(r)
            for c in colls:
                s = Suggestion("collection", c.id, c.name, "collection", c.library_id, None)
                s.candidates = [
                    _candidate(r, servers, "server_collection_id", _row_poster, _shows_render)
                    for r in crows[c.id]
                ]
                guess(s, threshold, names)
                out.append(s)

    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    out.sort(key=lambda s: (order[s.confidence], s.title.lower()))
    return out


def _candidate(
    row: Any, servers: dict[int, Server], key: str, row_poster, shows_render
) -> Candidate:
    fresh = bool(row.poster_ref) and row.poster_hash_ref == row.poster_ref
    return Candidate(
        server_id=row.server_id,
        server_kind=servers[row.server_id].kind,
        row_id=row.id,
        poster=row_poster(row, key),
        hash=row.poster_hash if fresh else None,
        darkness=edge_darkness(row.poster_edges) if fresh else None,
        kometa_label=bool(row.overlay_label)
        and not (row.pushed_ref and row.pushed_ref == row.poster_ref),
        coverarr_overlay=shows_render(row),
    )


async def coverage(session: AsyncSession) -> dict[str, Any]:
    """How many titles/collections have a stored base, a server base, or none."""
    out: dict[str, Any] = {}
    for label, model, where in (
        ("titles", MediaItem, MediaItem.kind.in_(("movie", "show"))),
        ("seasons", MediaItem, MediaItem.kind == "season"),
        ("collections", Collection, Collection.id.is_not(None)),
    ):
        kind = func.coalesce(
            func.substr(model.poster_source, 1, func.instr(model.poster_source, ":") - 1), "none"
        ).label("kind")
        rows = dict(
            (await session.execute(select(kind, func.count()).where(where).group_by(kind))).all()
        )
        out[label] = {
            "stored": rows.get("artwork", 0),
            "server": rows.get("server", 0),
            "none": rows.get("none", 0),
        }
    return out


# --- storing bases ---------------------------------------------------------------------------


async def run_store_bases(ctx: JobContext) -> dict[str, Any]:
    """Download every base taken from a server into the assets folder, so Coverarr holds
    the clean poster itself (and it's still there if the server's poster changes)."""
    from coverarr.services.artwork import ArtworkError, save_artwork

    sm = ctx.sessionmaker
    config_dir = Path(ctx.params["config_dir"])
    item_ids = ctx.params.get("media_item_ids")
    coll_ids = ctx.params.get("collection_ids")
    async with sm() as session:
        servers = {
            s.id: s for s in await session.scalars(select(Server).where(Server.enabled.is_(True)))
        }
        profile = await load_profile(session)
        work: list[tuple[str, int, int, str, str]] = []  # target, id, server, remote id, ref
        for model, row_model, owner, ids, target in (
            (MediaItem, ServerItem, ServerItem.media_item_id, item_ids, "item"),
            (Collection, ServerCollection, ServerCollection.collection_id, coll_ids, "collection"),
        ):
            narrowed = item_ids is not None or coll_ids is not None
            if narrowed and not ids:
                continue  # only the other kind was asked for
            query = select(model.id, model.poster_source).where(
                model.poster_source.like("server:%")
            )
            if narrowed:
                query = query.where(model.id.in_(ids))
            chosen = {
                rid: int(src.split(":", 1)[1])
                for rid, src in (await session.execute(query)).all()
                if src.split(":", 1)[1].isdigit()
            }
            if not chosen:
                continue
            for r in await session.execute(
                select(owner, row_model.server_id, row_model.remote_id, row_model.poster_ref).where(
                    owner.in_(list(chosen)), row_model.poster_ref.is_not(None)
                )
            ):
                if chosen.get(r[0]) == r.server_id and r.server_id in servers:
                    work.append((target, r[0], r.server_id, r.remote_id, r.poster_ref))

    await ctx.log(f"{len(work)} server posters to keep as stored bases")
    counts: dict[str, int] = defaultdict(int)
    lock = asyncio.Lock()
    by_server: dict[int, list] = defaultdict(list)
    for w in work:
        by_server[w[2]].append(w)
    done = 0

    async def one_server(server_id: int, jobs: list) -> None:
        nonlocal done
        async with client_for(servers[server_id], concurrency=profile.poster_concurrency) as client:
            for start in range(0, len(jobs), 20):
                batch = jobs[start : start + 20]

                async def fetch(w):
                    try:
                        return await client.fetch_poster(w[3], w[4])
                    except MediaServerError as exc:
                        await ctx.log(f"{servers[server_id].name}: {exc}")
                        return None

                datas = await asyncio.gather(*(fetch(w) for w in batch))
                async with lock, sm() as session:
                    for w, data in zip(batch, datas, strict=True):
                        if data is None:
                            counts["failed"] += 1
                            continue
                        try:
                            await save_artwork(
                                session,
                                config_dir,
                                source="original",
                                data=data,
                                name=f"{servers[server_id].name} poster",
                                media_item_id=w[1] if w[0] == "item" else None,
                                collection_id=w[1] if w[0] == "collection" else None,
                                choose=True,
                            )
                            counts["stored"] += 1
                        except ArtworkError as exc:
                            counts["failed"] += 1
                            await ctx.log(f"{w[0]} {w[1]}: {exc}")
                    await session.commit()
                done += len(batch)
                await ctx.progress(0.98 * done / max(len(work), 1), f"Stored {done}/{len(work)}")

    await asyncio.gather(*(one_server(sid, jobs) for sid, jobs in by_server.items()))
    await ctx.progress(1.0, "Bases stored")
    await ctx.log(
        f"Stored {counts['stored']} bases"
        + (f"; {counts['failed']} failed" if counts["failed"] else "")
    )
    return {"total": len(work), **counts}


# --- tidying the assets folder ---------------------------------------------------------------


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


async def run_tidy_assets(ctx: JobContext) -> dict[str, Any]:
    """Check (and with ``apply``, fix) the stored posters: names and folders, duplicates,
    files nothing refers to, and records whose file is gone."""
    sm = ctx.sessionmaker
    config_dir = Path(ctx.params["config_dir"])
    apply = bool(ctx.params.get("apply"))
    root = config_dir / ASSETS
    overlays = root / "overlays"
    report: dict[str, list[str]] = defaultdict(list)

    # 1. Records whose file is missing.
    async with sm() as session:
        arts = list(await session.scalars(select(Artwork)))
        for art in arts:
            if not await asyncio.to_thread((config_dir / art.file_path).is_file):
                report["missing_files"].append(art.file_path)
                if apply:
                    for model, owner in (
                        (MediaItem, art.media_item_id),
                        (Collection, art.collection_id),
                    ):
                        if owner:
                            target = await session.get(model, owner)
                            if target is not None and target.poster_source == f"artwork:{art.id}":
                                target.poster_source = None
                    await session.delete(art)
        if apply:
            await session.commit()
    await ctx.progress(0.2, "Checked stored posters exist")

    # 2. Identical images stored twice for the same title: keep the chosen (else newest) one.
    async with sm() as session:
        arts = list(await session.scalars(select(Artwork).order_by(Artwork.id)))
        chosen = {
            src
            for (src,) in (
                await session.execute(
                    select(MediaItem.poster_source).where(MediaItem.poster_source.like("artwork:%"))
                )
            ).all()
        } | {
            src
            for (src,) in (
                await session.execute(
                    select(Collection.poster_source).where(
                        Collection.poster_source.like("artwork:%")
                    )
                )
            ).all()
        }
        by_owner: dict[str, list[Artwork]] = defaultdict(list)
        for art in arts:
            by_owner[owner_key(art)].append(art)
        for group in by_owner.values():
            if len(group) < 2:
                continue
            digests = await asyncio.to_thread(
                lambda g=group: [_digest(config_dir / a.file_path) for a in g]
            )
            seen: dict[str, Artwork] = {}
            for art, digest in sorted(
                zip(group, digests, strict=True),
                key=lambda p: (f"artwork:{p[0].id}" not in chosen, -p[0].id),
            ):
                if digest is None:
                    continue
                if digest in seen:
                    report["duplicates"].append(art.file_path)
                    if apply:
                        await asyncio.to_thread(
                            (config_dir / art.file_path).unlink, missing_ok=True
                        )
                        await session.delete(art)
                else:
                    seen[digest] = art
        if apply:
            await session.commit()
    await ctx.progress(0.45, "Checked for duplicates")

    # 3. Files in the wrong place or with the wrong name (e.g. a title was renamed).
    if apply:
        moved = await relocate_artwork(sm, config_dir, force=True)
        if moved:
            report["renamed"].append(f"{moved} files moved to their expected names")
    else:
        async with sm() as session:
            arts = list(await session.scalars(select(Artwork)))
            owners = await describe_owners(
                session,
                [a.media_item_id for a in arts if a.media_item_id],
                [a.collection_id for a in arts if a.collection_id],
            )
        for art in arts:
            folder, stem = owners.get(owner_key(art), (None, None))
            if folder is None:
                continue
            suffix = Path(art.file_path).suffix.lstrip(".") or "jpg"
            expected = folder / f"{stem}-{art.source}.{suffix}"
            current = Path(art.file_path)
            # A " [m123]" folder suffix is how name clashes are kept apart: that's fine.
            if current.name != expected.name or not current.parent.name.startswith(folder.name):
                report["renamed"].append(f"{art.file_path} -> {expected}")
    await ctx.progress(0.7, "Checked names")

    # 4. Files nothing refers to (old posters, leftovers of deleted titles).
    async with sm() as session:
        known = {p for (p,) in (await session.execute(select(Artwork.file_path))).all()}

    def walk() -> list[Path]:
        if not root.is_dir():
            return []
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            if Path(dirpath) == overlays or overlays in Path(dirpath).parents:
                dirnames[:] = []
                continue
            for name in filenames:
                out.append(Path(dirpath) / name)
        return out

    for path in await asyncio.to_thread(walk):
        rel = str(path.relative_to(config_dir))
        if rel not in known:
            report["unused_files"].append(rel)
            if apply:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                await asyncio.to_thread(_prune, config_dir, path.parent)
    await ctx.progress(1.0, "Tidy complete" if apply else "Check complete")

    summary = {k: len(v) for k, v in report.items()}
    verb = "Fixed" if apply else "Found"
    await ctx.log(
        f"{verb}: {summary.get('missing_files', 0)} missing files, "
        f"{summary.get('duplicates', 0)} duplicates, {summary.get('renamed', 0)} misnamed, "
        f"{summary.get('unused_files', 0)} unused files"
    )
    return {"applied": apply, "counts": summary, "examples": {k: v[:50] for k, v in report.items()}}


async def library_names(session: AsyncSession) -> dict[int, str]:
    return dict((await session.execute(select(Library.id, Library.name))).all())
