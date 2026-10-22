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

With overlays on for a library (services/overlays.py), every title there is a target, and the
poster to push is the base with overlays drawn on. Fingerprints can't tell those apart (they
deliberately ignore overlays), so here a server is done only when Coverarr pushed exactly that
render (``pushed_key``) and the server still shows it (``pushed_ref``). A title with no chosen
base uses a clean server poster, which is saved as its "original" artwork before the first
overlaid push, so overlays are always drawn on a clean poster and never stack.
"""

from __future__ import annotations

import asyncio
import io
import time
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import String, and_, or_, select, type_coerce
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
from coverarr.services import cpu
from coverarr.services.assets import taken_paths
from coverarr.services.jobs import JobContext
from coverarr.services.load import load_profile
from coverarr.services.overlays import (
    LEVELS,
    OverlaySetup,
    RenderPlan,
    config_dir,
    library_config,
    load_config,
    load_setup,
    pack_dir,
    plan_args,
    render_to_file,
    rendered_path,
)
from coverarr.services.posters import edges_differ, fingerprints, hamming
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
class Render:
    """An overlaid poster to push: the base it's drawn on and the overlay files."""

    level: str
    base: str  # "artwork:<id>" or "server:<server row id>:<poster ref>"
    plan: RenderPlan
    key: str

    @property
    def pushed_key(self) -> str:
        return f"r:{self.key}"


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
    render: Render | None = None

    def out(self) -> dict[str, Any]:
        base = self.base
        if self.render is not None:
            base = {
                "url": f"/api/overlays/preview/item/{self.id}?k={self.render.key}",
                "server_id": None,
                "has_poster": True,
                "poster_version": self.render.key,
                "kometa_overlay": False,
            }
        return {
            "target": self.target,
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "library_id": self.library_id,
            "source": self.source,
            "top_id": self.top_id,
            "base": base,
            "problem": self.problem,
            "overlay": self.render is not None,
            "missing_overlays": self.render.plan.missing if self.render else [],
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
    ServerItem.poster_edges,
    ServerItem.pushed_ref,
    ServerItem.pushed_key,
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
    ServerCollection.poster_edges,
    ServerCollection.pushed_ref,
    ServerCollection.pushed_key,
    type_coerce(ServerCollection.labels, String).ilike('%"overlay"%').label("overlay_label"),
)


def _fresh_hash(row: Any) -> str | None:
    return row.poster_hash if row.poster_ref and row.poster_hash_ref == row.poster_ref else None


def _fresh_edges(row: Any) -> str | None:
    return row.poster_edges if row.poster_ref and row.poster_hash_ref == row.poster_ref else None


def _shows_render(row: Any) -> bool:
    """The server shows an overlaid poster Coverarr pushed (so it isn't a clean base)."""
    return bool(
        row.pushed_key and row.pushed_key.startswith("r:") and row.pushed_ref == row.poster_ref
    )


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


def _assess(
    change: Change, rows: list[Any], artworks: dict[int, Any], threshold: int, force: bool = False
) -> None:
    """Fill in ``change.base`` and the servers that still need the base poster."""
    key = "server_item_id" if change.target == "item" else "server_collection_id"
    kind, _, ref = change.source.partition(":")
    base_hash: str | None = None
    base_edges: str | None = None
    source_server: int | None = None
    if kind == "artwork":
        art = artworks.get(int(ref)) if ref.isdigit() else None
        if art is None:
            change.problem = "The chosen artwork no longer exists"
            return
        change.base, base_hash, base_edges = _art_poster(art), art.poster_hash, art.poster_edges
    elif kind == "server" and ref.isdigit():
        source_server = int(ref)
        src = next((r for r in rows if r.server_id == source_server), None)
        if src is None or not src.poster_ref:
            change.problem = "The server you picked the poster from no longer has it"
            return
        change.base, base_hash = _row_poster(src, key), _fresh_hash(src)
        base_edges = _fresh_edges(src)
    else:
        change.problem = f"Unknown poster source {change.source!r}"
        return

    for row in rows:
        if row.server_id == source_server:
            continue
        if _shows_render(row):
            # Overlays were switched off since: put the plain base back.
            change.servers.append(
                ServerChange(
                    row.server_id,
                    row.id,
                    row.remote_id,
                    "has Coverarr overlays",
                    _row_poster(row, key),
                )
            )
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
        elif edges_differ(_fresh_edges(row), base_edges):
            # Same artwork, but gradients or badges at the top/bottom that the base doesn't
            # have (e.g. Kometa overlays whose label was removed).
            reason = "has overlays"
        elif force:
            reason = "force push"
        else:
            continue
        change.servers.append(
            ServerChange(row.server_id, row.id, row.remote_id, reason, _row_poster(row, key))
        )


def _assess_overlay(
    change: Change,
    rows: list[Any],
    artworks: dict[int, Any],
    facts: dict[str, Any] | None,
    layers: list[dict],
    setup: OverlaySetup,
    kinds: dict[int, str],
    force: bool = False,
    info: dict[str, Any] | None = None,
) -> None:
    """Like ``_assess``, for a title that gets overlays (``info`` fills any text layers)."""
    kind, _, ref = change.source.partition(":") if change.source else ("", "", "")
    if not kind:
        # Every server may show Coverarr's render (e.g. after a Discard); the clean poster
        # kept on the first push is still a base.
        clean = [r for r in rows if r.poster_ref and not _has_overlay(r) and not _shows_render(r)]
        kept = [
            a for a in artworks.values() if a.source == "original" and a.media_item_id == change.id
        ]
        if not clean and kept:
            kind, ref = "artwork", str(max(kept, key=lambda a: a.id).id)
    if kind == "artwork":
        art = artworks.get(int(ref)) if ref.isdigit() else None
        if art is None:
            change.problem = "The chosen artwork no longer exists"
            return
        change.base, base = _art_poster(art), f"artwork:{art.id}"
    else:
        if kind == "server" and ref.isdigit():
            src = next((r for r in rows if r.server_id == int(ref) and r.poster_ref), None)
            if src is None:
                change.problem = "The server you picked the poster from no longer has it"
                return
        else:
            # No base chosen: a clean server poster, Jellyfin/Emby before Plex (which is
            # where Kometa draws its overlays).
            clean = [r for r in rows if r.poster_ref and not _has_overlay(r)]
            fresh = [r for r in clean if not _shows_render(r)]
            fresh.sort(key=lambda r: (kinds.get(r.server_id) == "plex", r.server_id))
            if not fresh:
                change.problem = (
                    "No clean poster to draw overlays on: upload one or pick one"
                    if not clean
                    else "Coverarr's overlays are on every server and the original wasn't kept"
                )
                return
            src = fresh[0]
        if _shows_render(src):
            change.problem = "That server shows Coverarr's overlaid poster, not a clean one"
            return
        change.base = _row_poster(src, "server_item_id")
        base = f"server:{src.id}:{src.poster_ref}"
    plan = setup.plan(change.kind, layers, facts, info)
    change.render = Render(change.kind, base, plan, plan.key(base))
    wanted = change.render.pushed_key
    for row in rows:
        in_sync = row.pushed_key == wanted and row.pushed_ref == row.poster_ref and row.poster_ref
        if in_sync and not force:
            continue
        if in_sync:
            reason = "force push"
        elif not row.poster_ref:
            reason = "no poster"
        elif _shows_render(row):
            reason = "overlays out of date"
        elif row.pushed_key and row.pushed_ref != row.poster_ref:
            reason = "changed on the server"
        else:
            reason = "needs overlays"
        change.servers.append(
            ServerChange(
                row.server_id, row.id, row.remote_id, reason, _row_poster(row, "server_item_id")
            )
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
    MediaItem.facts,
    MediaItem.details,
)


def overlay_facts(item: Any) -> dict[str, Any]:
    """What overlays know about a title: the servers' facts and the details from TMDb."""
    return {**(item.details or {}), **(item.facts or {})}


def detail_text(details: dict[str, Any] | None) -> dict[str, str]:
    """Ratings, runtime and status as overlay text shows them."""
    out: dict[str, str] = {}
    d = details or {}
    for key in ("tmdb_rating", "imdb_rating", "letterboxd_rating"):
        if isinstance(d.get(key), int | float):
            out[key] = f"{d[key]:.1f}"
    for key in ("rt_rating", "rt_audience"):
        if isinstance(d.get(key), int | float):
            out[key] = f"{round(d[key])}%"
    if isinstance(d.get("metacritic"), int | float):
        out["metacritic"] = str(round(d["metacritic"]))
    if isinstance(d.get("runtime"), int) and d["runtime"] > 0:
        hours, minutes = divmod(d["runtime"], 60)
        out["runtime"] = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    if d.get("status"):
        out["status"] = str(d["status"]).title()
    return out


async def _labels_for(
    session: AsyncSession, items: list[Any], info: dict[int, dict[str, Any]] | None = None
) -> dict[int, tuple]:
    """Readable titles for seasons/episodes ("Spaced S01E02") plus their top-level show id.
    ``info`` is filled with what overlay text can show for each (title, show, season…)."""
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
            name = "Specials" if item.season_number == 0 else f"Season {item.season_number}"
            title = f"{top.title}: {name}"
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
        if info is not None:
            season = parents.get(item.parent_id) if item.kind == "episode" else None
            info[item.id] = {
                "title": item.title,
                "show": top.title,
                "season": item.season_number
                if item.season_number is not None
                else (season.season_number if season else None),
                "episode": item.episode_number,
                "year": item.year or top.year,
                **detail_text(item.details),
            }
    return out


async def compute_changes(
    session: AsyncSession,
    *,
    library_id: int | None = None,
    media_item_ids: list[int] | None = None,
    collection_ids: list[int] | None = None,
    items: bool = True,
    collections: bool = True,
    force: bool = False,
) -> list[Change]:
    """Every pending poster change (optionally narrowed), in a stable order. With ``force``,
    every server gets the poster again even if it already shows it."""
    threshold = int(await get_setting(session, "poster_match_threshold"))
    enabled = select(Server.id).where(Server.enabled.is_(True))
    kinds = dict((await session.execute(select(Server.id, Server.kind))).all())
    setup = await load_setup(session)
    changes: list[Change] = []

    if items and collection_ids is None:
        # Titles with a chosen base, plus everything at a level that has overlays.
        wanted = [MediaItem.poster_source.is_not(None)] + [
            and_(MediaItem.library_id == lib_id, MediaItem.kind.in_(list(levels)))
            for lib_id, levels in setup.libraries.items()
        ]
        where = [or_(*wanted)]
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
            info: dict[int, dict[str, Any]] = {}
            labels = await _labels_for(session, targets, info)
            for n, t in enumerate(targets):
                if n % 2000 == 1999:
                    await asyncio.sleep(0)  # let the API answer during a long pass
                title, top = labels[t.id]
                change = Change(
                    "item", t.id, title, t.kind, t.library_id, t.poster_source or "", top, None
                )
                layers = setup.layers_for(t.library_id, t.kind)
                if layers is not None:
                    _assess_overlay(
                        change,
                        rows[t.id],
                        arts,
                        overlay_facts(t),
                        layers,
                        setup,
                        kinds,
                        force,
                        info[t.id],
                    )
                else:
                    _assess(change, rows[t.id], arts, threshold, force)
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
                _assess(change, crows[c.id], arts, threshold, force)
                if change.servers or change.problem:
                    changes.append(change)

    changes.sort(key=lambda c: (c.target != "collection", c.title.lower()))
    return changes


# --- the full list, cached ---------------------------------------------------------------------
# Working out every pending change takes seconds on a big library with overlays, and the
# Changes page, its summary and the library filters all ask for it. The full list is kept
# until something could have changed it: any write through the API, or a job starting or
# ending. While a job runs (e.g. a push), it's also redone every 30 seconds.

_version = 0
_jobs_running = 0
_cached: tuple[int, float, list[Change]] | None = None
BUSY_TTL = 30.0


def changes_stale() -> None:
    """Something that can change the pending list happened."""
    global _version, _cached
    _version += 1
    _cached = None


def job_started() -> None:
    global _jobs_running
    _jobs_running += 1
    changes_stale()


def job_finished() -> None:
    global _jobs_running
    _jobs_running = max(0, _jobs_running - 1)
    changes_stale()


async def pending_changes(
    session: AsyncSession,
    *,
    library_id: int | None = None,
    items: bool = True,
    collections: bool = True,
) -> list[Change]:
    """``compute_changes`` for whole libraries, from the cached full list."""
    global _cached
    now = time.monotonic()
    cached = _cached
    if cached is None or cached[0] != _version or (_jobs_running and now - cached[1] > BUSY_TTL):
        version = _version
        full = await compute_changes(session)
        if version == _version:  # nothing changed while it was worked out
            _cached = (version, now, full)
    else:
        full = cached[2]
    return [
        c
        for c in full
        if (library_id is None or c.library_id == library_id)
        and (items or c.target != "item")
        and (collections or c.target != "collection")
    ]


async def _artworks(session: AsyncSession, owner_filter: Any) -> dict[int, Any]:
    rows = await session.execute(
        select(
            Artwork.id,
            Artwork.source,
            Artwork.poster_hash,
            Artwork.poster_edges,
            Artwork.created_at,
            Artwork.media_item_id,
        ).where(owner_filter)
    )
    return {a.id: a for a in rows}


async def changes_summary(session: AsyncSession) -> dict[str, int]:
    from coverarr.services.backgrounds import background_changes
    from coverarr.services.metadata import metadata_changes

    changes = await pending_changes(session)
    backgrounds = await background_changes(session)
    metadata = await metadata_changes(session)
    return {
        "metadata": len(metadata),
        "metadata_uploads": sum(len(c.servers) for c in metadata),
        "backgrounds": sum(1 for c in backgrounds if not c.problem),
        "background_uploads": sum(len(c.servers) for c in backgrounds if not c.problem),
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


def _clean_old_renders(dest: Path) -> None:
    owner = dest.name.rsplit("-", 1)[0]
    for old in dest.parent.glob(f"{owner}-*.jpg"):
        if old != dest:
            old.unlink(missing_ok=True)


async def run_push_posters(ctx: JobContext) -> dict[str, Any]:
    """Upload each pending poster (the base, or the base with overlays) to the servers that
    don't show it yet."""
    from coverarr.services.artwork import save_artwork

    sm = ctx.sessionmaker
    config_dir = Path(ctx.params["config_dir"])
    item_ids = ctx.params.get("media_item_ids")
    coll_ids = ctx.params.get("collection_ids")
    force = bool(ctx.params.get("force"))
    library_id = ctx.params.get("library_id")
    async with sm() as session:
        if item_ids is None and coll_ids is None:
            changes = await compute_changes(session, library_id=library_id, force=force)
        else:
            changes = []
            if item_ids:
                changes += await compute_changes(session, media_item_ids=item_ids, force=force)
            if coll_ids:
                changes += await compute_changes(session, collection_ids=coll_ids, force=force)
        changes = [c for c in changes if not c.problem]
        from coverarr.services.backgrounds import background_changes, push_backgrounds

        if item_ids is None and coll_ids is None:
            bg_changes = await background_changes(session, library_id=library_id)
        else:
            bg_changes = (
                await background_changes(session, media_item_ids=item_ids) if item_ids else []
            )
        bg_changes = [c for c in bg_changes if not c.problem]
        from coverarr.services.metadata import metadata_changes, push_metadata

        if item_ids is None and coll_ids is None:
            meta_changes = await metadata_changes(session, library_id=library_id)
        else:
            meta_changes = (
                await metadata_changes(session, media_item_ids=item_ids) if item_ids else []
            )
        servers = {s.id: s for s in await session.scalars(select(Server))}
        profile = await load_profile(session)

    uploads = sum(len(c.servers) for c in changes)
    renders = sum(1 for c in changes if c.render)
    await ctx.log(
        f"{len(changes)} posters to push ({uploads} uploads"
        + (f", {renders} to render with overlays" if renders else "")
        + ")"
    )
    counts: dict[str, int] = defaultdict(int)
    done = 0
    write_lock = asyncio.Lock()
    taken: dict[str, str] | None = None

    async with AsyncExitStack() as stack:
        clients: dict[int, MediaServerClient] = {}
        for sid, server in servers.items():
            if server.enabled:
                clients[sid] = await stack.enter_async_context(
                    client_for(server, concurrency=profile.poster_concurrency)
                )

        async def rendered(change: Change) -> tuple[bytes, str, str]:
            """(JPEG, fingerprint, pushed_key) of the overlaid poster, rendering it if needed.
            A server poster used as the base is kept as the title's "original" artwork first,
            so later renders start from it rather than from an overlaid copy."""
            nonlocal taken
            assert change.render is not None
            base = change.render.base
            if base.startswith("server:"):
                row_id = int(base.split(":", 2)[1])
                async with sm() as session:
                    row = await session.get(ServerItem, row_id)
                    if row is None or row.server_id not in clients or not row.poster_ref:
                        raise ValueError("the server poster to draw on is gone")
                    remote_id, ref, server_id = row.remote_id, row.poster_ref, row.server_id
                data = await clients[server_id].fetch_poster(remote_id, ref)
                async with write_lock, sm() as session:
                    if taken is None:
                        taken = await taken_paths(session)
                    art = await save_artwork(
                        session,
                        config_dir,
                        source="original",
                        data=data,
                        name=f"{servers[server_id].name} poster",
                        media_item_id=change.id,
                        choose=True,
                        taken=taken,
                    )
                    await session.commit()
                    base, file_path = f"artwork:{art.id}", art.file_path
                counts["originals_kept"] += 1
            else:
                async with sm() as session:
                    art = await session.get(Artwork, int(base.split(":", 1)[1]))
                    if art is None:
                        raise ValueError("the chosen artwork no longer exists")
                    file_path = art.file_path
            key = change.render.plan.key(base)
            dest = rendered_path(config_dir, f"m{change.id}", key)
            if await asyncio.to_thread(dest.is_file):
                data = await asyncio.to_thread(dest.read_bytes)
                fingerprint = await cpu.run(fingerprints, data)
            else:
                fingerprint = await cpu.run(
                    render_to_file,
                    str(config_dir / file_path),
                    change.render.plan.size,
                    plan_args(change.render.plan),
                    str(dest),
                )
                await asyncio.to_thread(_clean_old_renders, dest)
                data = await asyncio.to_thread(dest.read_bytes)
                counts["rendered"] += 1
            return data, fingerprint, f"r:{key}"

        async def push(change: Change) -> None:
            """Upload one title's poster to each server that needs it."""
            if change.render is not None:
                data, fingerprint, pushed_key = await rendered(change)
            else:
                async with sm() as session:
                    data = await _base_bytes(session, change, clients, config_dir)
                fingerprint = await cpu.run(fingerprints, data)
                pushed_key = f"b:{change.source}"
            content_type = await asyncio.to_thread(_content_type, data)

            async def upload(sc: ServerChange) -> bool:
                try:
                    new_ref = await clients[sc.server_id].upload_poster(
                        sc.remote_id, data, content_type
                    )
                except MediaServerError as exc:
                    counts["failed"] += 1
                    await ctx.log(f"{servers[sc.server_id].name}: {change.title}: {exc}")
                    return False
                # Noted straight away, so a cancel never forgets an upload that happened.
                uploaded.append((change.target, sc.row_id, new_ref, fingerprint, pushed_key))
                return True

            if all(await asyncio.gather(*(upload(sc) for sc in change.servers))):
                counts["pushed"] += 1

        uploaded: list[tuple[str, int, str | None, tuple[str, str], str]] = []

        async def record() -> None:
            """Write the uploads noted so far, a whole batch per query."""
            if not uploaded:
                return
            batch = list(uploaded)
            uploaded.clear()
            async with write_lock, sm() as session:
                for target, model in (("item", ServerItem), ("collection", ServerCollection)):
                    mine = [u for u in batch if u[0] == target]
                    if not mine:
                        continue
                    found = await session.scalars(
                        select(model).where(model.id.in_({u[1] for u in mine}))
                    )
                    rows = {row.id: row for row in found}
                    for _, row_id, new_ref, fingerprint, pushed_key in mine:
                        row = rows.get(row_id)
                        if row is None:
                            continue
                        row.poster_ref = new_ref or row.poster_ref
                        row.pushed_ref, row.pushed_key = row.poster_ref, pushed_key
                        row.poster_hash, row.poster_edges = fingerprint
                        row.poster_hash_ref = row.poster_ref
                        row.labels = [
                            label for label in row.labels or [] if str(label).lower() != "overlay"
                        ]
                        counts["uploaded"] += 1
                await session.commit()

        # A pool of workers takes the next title as each finishes, so one slow upload
        # doesn't hold up the rest.
        width = max(CHUNK, cpu.workers() * 4)
        queue = iter(changes)

        async def worker() -> None:
            nonlocal done
            for change in queue:
                try:
                    await push(change)
                except Exception as exc:
                    counts["failed"] += len(change.servers)
                    await ctx.log(f"{change.title}: {exc}")
                done += 1
                if len(uploaded) >= width:
                    await record()
                if done % 10 == 0 or done == len(changes):
                    await ctx.progress(
                        0.98 * done / max(len(changes), 1), f"Pushed {done}/{len(changes)}"
                    )

        try:
            await asyncio.gather(*(worker() for _ in range(min(width, len(changes)))))
        finally:
            await asyncio.shield(record())

        if meta_changes:
            await ctx.log(f"{len(meta_changes)} titles' metadata to push")
            names = {sid: s.name for sid, s in servers.items()}
            for key, value in (await push_metadata(ctx, meta_changes, clients, names)).items():
                counts[key] += value

        if bg_changes:
            await ctx.log(
                f"{len(bg_changes)} backgrounds to push "
                f"({sum(len(c.servers) for c in bg_changes)} uploads)"
            )
            names = {sid: s.name for sid, s in servers.items()}
            for key, value in (
                await push_backgrounds(ctx, bg_changes, clients, config_dir, names)
            ).items():
                counts[key] += value

    from coverarr.services.review import refresh_issues

    async with sm() as session:
        await refresh_issues(session)
        await session.commit()
    await ctx.progress(1.0, "Push complete")
    await ctx.log(
        f"Uploaded {counts['uploaded']} posters for {counts['pushed']} titles/collections"
        + (f"; rendered {counts['rendered']} with overlays" if counts["rendered"] else "")
        + (f"; {counts['failed']} failed" if counts["failed"] else "")
        + (
            f"; {counts['backgrounds']} backgrounds for {counts['background_titles']} titles"
            if counts["backgrounds"]
            else ""
        )
        + (f" ({counts['backgrounds_failed']} failed)" if counts["backgrounds_failed"] else "")
        + (f"; metadata on {counts['metadata']} servers' titles" if counts["metadata"] else "")
        + (f" ({counts['metadata_failed']} failed)" if counts["metadata_failed"] else "")
    )
    return {"titles": len(changes), **counts}


async def plan_item(
    session: AsyncSession, media_item_id: int, layers: list[dict] | None = None
) -> Change | None:
    """The overlay render for one title as things stand (for previews), using ``layers`` or
    the library's own layers for its level (even if overlays are switched off there)."""
    t = (await session.execute(select(*ITEM_INFO).where(MediaItem.id == media_item_id))).first()
    if t is None:
        return None
    lib = await session.get(Library, t.library_id)
    if lib is None or lib.kind not in LEVELS:
        return None
    config = await load_config(session)
    if layers is None:
        layers = library_config(config, lib.id, lib.kind)["levels"].get(t.kind)
    if layers is None:
        return None
    setup = OverlaySetup(pack_dir(config_dir(), config.get("pack")), {})
    kinds = dict((await session.execute(select(Server.id, Server.kind))).all())
    enabled = select(Server.id).where(Server.enabled.is_(True))
    rows = (
        await session.execute(
            select(*ITEM_COLS)
            .where(ServerItem.media_item_id == t.id, ServerItem.server_id.in_(enabled))
            .order_by(ServerItem.server_id)
        )
    ).all()
    arts = await _artworks(session, Artwork.media_item_id == t.id)
    info: dict[int, dict[str, Any]] = {}
    title, top = (await _labels_for(session, [t], info))[t.id]
    change = Change("item", t.id, title, t.kind, t.library_id, t.poster_source or "", top, None)
    _assess_overlay(change, rows, arts, overlay_facts(t), layers, setup, kinds, info=info[t.id])
    return change
