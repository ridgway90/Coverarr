from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import MediaServerError
from coverarr.db import get_session
from coverarr.models import (
    Artwork,
    Collection,
    Issue,
    Library,
    MediaItem,
    Server,
    ServerCollection,
    ServerItem,
    ServerLibrary,
)
from coverarr.services.changes import pending_changes

router = APIRouter(prefix="/api", tags=["library"])


# --- libraries --------------------------------------------------------------------------------


@router.get("/libraries")
async def list_libraries(session: AsyncSession = Depends(get_session)):
    libraries = list(await session.scalars(select(Library).order_by(Library.kind, Library.name)))
    counts = dict(
        (
            await session.execute(
                select(MediaItem.library_id, func.count(MediaItem.id))
                .where(MediaItem.kind.in_(("movie", "show")))
                .group_by(MediaItem.library_id)
            )
        ).all()
    )
    server_libs = (
        await session.execute(
            select(ServerLibrary, Server.name, Server.kind)
            .join(Server, Server.id == ServerLibrary.server_id)
            .order_by(Server.id, ServerLibrary.name)
        )
    ).all()
    by_lib: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for sl, server_name, server_kind in server_libs:
        by_lib[sl.library_id].append(
            {
                "id": sl.id,
                "server_id": sl.server_id,
                "server_name": server_name,
                "server_kind": server_kind,
                "name": sl.name,
                "kind": sl.kind,
                "enabled": sl.enabled,
                "library_id": sl.library_id,
            }
        )
    return {
        "libraries": [
            {
                "id": lib.id,
                "name": lib.name,
                "kind": lib.kind,
                "item_count": counts.get(lib.id, 0),
                "server_libraries": by_lib.get(lib.id, []),
            }
            for lib in libraries
        ],
        "unmapped": by_lib.get(None, []),
    }


class LibraryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.put("/libraries/{library_id}")
async def rename_library(
    library_id: int, body: LibraryUpdate, session: AsyncSession = Depends(get_session)
):
    lib = await session.get(Library, library_id)
    if lib is None:
        raise HTTPException(404, "Library not found")
    lib.name = body.name.strip()
    await session.commit()
    return {"ok": True}


class ServerLibraryUpdate(BaseModel):
    enabled: bool
    # Map to an existing logical library, or create a new one with ``new_library_name``.
    library_id: int | None = None
    new_library_name: str | None = None


@router.put("/server-libraries/{sl_id}")
async def update_server_library(
    sl_id: int,
    body: ServerLibraryUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    sl = await session.get(ServerLibrary, sl_id)
    if sl is None:
        raise HTTPException(404, "Server library not found")
    if body.new_library_name:
        lib = Library(name=body.new_library_name.strip(), kind=sl.kind)
        session.add(lib)
        await session.flush()
        sl.library_id = lib.id
    elif body.library_id is not None:
        lib = await session.get(Library, body.library_id)
        if lib is None or lib.kind != sl.kind:
            raise HTTPException(422, "Target library must exist and be the same type")
        sl.library_id = lib.id
    else:
        sl.library_id = None
    sl.enabled = body.enabled
    await session.commit()
    # Drop logical libraries left with no server libraries.
    empty = await session.scalars(select(Library).where(~Library.server_libraries.any()))
    for lib in empty:
        await session.delete(lib)
    await session.commit()
    await request.app.state.jobs.enqueue("rematch")
    return {"ok": True}


# --- items ------------------------------------------------------------------------------------


def _presence(si: ServerItem) -> dict[str, Any]:
    return {
        "server_id": si.server_id,
        "server_item_id": si.id,
        "remote_id": si.remote_id,
        "title": si.title,
        "has_poster": bool(si.poster_ref),
        "poster_version": (si.poster_ref or "")[-24:],
        "kometa_overlay": si.has_kometa_overlay,
    }


def _artwork_out(art: Artwork) -> dict[str, Any]:
    return {
        "artwork_id": art.id,
        "server_id": None,
        "source": art.source,
        "has_poster": True,
        "poster_version": f"{art.id}-{int(art.created_at.timestamp())}",
        "kometa_overlay": False,
        "width": art.width,
        "height": art.height,
    }


async def _artworks_for(
    session: AsyncSession, *, item_ids: list[int] | None = None, coll_ids: list[int] | None = None
) -> dict[int, list[dict[str, Any]]]:
    """Coverarr-held artwork per media item (or per collection)."""
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if item_ids:
        for art in await session.scalars(
            select(Artwork).where(Artwork.media_item_id.in_(item_ids))
        ):
            out[art.media_item_id].append(_artwork_out(art))
    if coll_ids:
        for art in await session.scalars(
            select(Artwork).where(Artwork.collection_id.in_(coll_ids))
        ):
            out[art.collection_id].append(_artwork_out(art))
    return out


def preferred_poster(
    presence: list[dict[str, Any]],
    poster_source: str | None,
    artworks: list[dict[str, Any]] | None = None,
) -> dict | None:
    """Pick the poster to show: the chosen source, else a clean one, else any."""
    if poster_source and poster_source.startswith("artwork:"):
        art_id = int(poster_source.split(":", 1)[1])
        for art in artworks or []:
            if art["artwork_id"] == art_id:
                return art
    with_poster = [p for p in presence if p["has_poster"]]
    if poster_source and poster_source.startswith("server:"):
        sid = int(poster_source.split(":", 1)[1])
        for p in with_poster:
            if p["server_id"] == sid:
                return p
    clean = [p for p in with_poster if not p["kometa_overlay"]]
    return (clean or with_poster or [None])[0]


def initial_of(expr: Any) -> Any:
    """SQL for the A-Z bar: first letter, ignoring a leading "The"/"A"/"An"; "#" otherwise."""
    lowered = func.lower(expr)
    trimmed = case(
        (func.substr(lowered, 1, 4) == "the ", func.substr(expr, 5)),
        (func.substr(lowered, 1, 3) == "an ", func.substr(expr, 4)),
        (func.substr(lowered, 1, 2) == "a ", func.substr(expr, 3)),
        else_=expr,
    )
    first = func.upper(func.substr(func.ltrim(trimmed), 1, 1))
    return case((first.between("A", "Z"), first), else_="#")


IssueFilter = Literal["any", "poster", "push", "missing"]


def _item_order() -> tuple:
    return (func.coalesce(MediaItem.sort_title, MediaItem.title), MediaItem.year, MediaItem.id)


async def _filtered_items(
    session: AsyncSession,
    lib: Library,
    q: str | None,
    missing_on: int | None,
    issues: IssueFilter | None,
) -> tuple[Any, set[int]]:
    """The library listing's query with every filter but the letter, and the titles with
    pending poster pushes."""
    query = select(MediaItem).where(MediaItem.library_id == lib.id, MediaItem.kind == lib.kind)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(MediaItem.title.ilike(like), MediaItem.tmdb_id == q.strip()))
    if missing_on is not None:
        query = query.where(~MediaItem.server_items.any(ServerItem.server_id == missing_on))
    pending: set[int] = set()
    if issues in ("any", "poster", "push"):
        # Titles whose chosen base poster isn't on every server yet. Their posters differ
        # too, but they're no longer review issues because the choice has been made.
        changes = await pending_changes(session, library_id=lib.id, collections=False)
        pending = {c.top_id for c in changes if c.top_id is not None}
    if issues:
        types = {
            "any": ("item_missing", "poster_differs"),
            "poster": ("poster_differs",),
            "push": (),
            "missing": ("item_missing",),
        }[issues]
        query = query.where(
            or_(
                MediaItem.id.in_(
                    select(Issue.media_item_id).where(Issue.type.in_(types), Issue.status == "open")
                ),
                MediaItem.id.in_(pending),
            )
        )
    return query, pending


def _with_letter(query: Any, letter: str | None) -> Any:
    if not letter:
        return query
    return query.where(initial_of(func.coalesce(MediaItem.sort_title, MediaItem.title)) == letter)


@router.get("/items")
async def list_items(
    library_id: int,
    q: str | None = None,
    missing_on: int | None = None,
    issues: IssueFilter | None = None,
    letter: str | None = Query(None, pattern="^[A-Z#]$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    lib = await session.get(Library, library_id)
    if lib is None:
        raise HTTPException(404, "Library not found")
    query, pending = await _filtered_items(session, lib, q, missing_on, issues)
    # Titles per first letter (with the other filters applied), for the A-Z bar.
    sub = query.subquery()
    first = initial_of(func.coalesce(sub.c.sort_title, sub.c.title))
    letters = dict((await session.execute(select(first, func.count()).group_by(first))).all())
    query = _with_letter(query, letter)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    items = list(
        await session.scalars(
            query.order_by(*_item_order()).offset((page - 1) * page_size).limit(page_size)
        )
    )
    presence = await _presence_for(session, [i.id for i in items])
    artworks = await _artworks_for(session, item_ids=[i.id for i in items])
    open_issues = await _issue_types_for(session, [i.id for i in items])
    for i in items:
        if i.id in pending:
            open_issues[i.id].append("push_pending")
    # Posters not fingerprinted yet can't be compared, so filters may miss them.
    unscanned = await session.scalar(
        select(func.count(ServerItem.id))
        .join(ServerLibrary, ServerLibrary.id == ServerItem.server_library_id)
        .join(Server, Server.id == ServerItem.server_id)
        .where(
            ServerLibrary.library_id == library_id,
            Server.enabled.is_(True),
            ServerItem.kind == lib.kind,
            ServerItem.media_item_id.is_not(None),
            ServerItem.poster_ref.is_not(None),
            or_(
                ServerItem.poster_hash_ref.is_(None),
                ServerItem.poster_hash_ref != ServerItem.poster_ref,
            ),
        )
    )
    return {
        "total": total,
        "unscanned": unscanned,
        "letters": letters,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": i.id,
                "kind": i.kind,
                "title": i.title,
                "year": i.year,
                "poster_source": i.poster_source,
                "servers": presence.get(i.id, []),
                "poster": preferred_poster(
                    presence.get(i.id, []), i.poster_source, artworks.get(i.id)
                ),
                "issues": open_issues.get(i.id, []),
            }
            for i in items
        ],
    }


async def _presence_for(session: AsyncSession, ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    if ids:
        rows = await session.scalars(
            select(ServerItem)
            .where(ServerItem.media_item_id.in_(ids))
            .order_by(ServerItem.server_id)
        )
        for si in rows:
            out[si.media_item_id].append(_presence(si))
    return out


async def _issue_types_for(session: AsyncSession, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = defaultdict(list)
    if ids:
        rows = await session.execute(
            select(Issue.media_item_id, Issue.type).where(
                Issue.media_item_id.in_(ids), Issue.status == "open"
            )
        )
        for mi, t in rows:
            out[mi].append(t)
    return out


@router.get("/items/{item_id}/neighbours")
async def item_neighbours(
    item_id: int,
    q: str | None = None,
    missing_on: int | None = None,
    issues: IssueFilter | None = None,
    letter: str | None = Query(None, pattern="^[A-Z#]$"),
    session: AsyncSession = Depends(get_session),
):
    """The previous and next title in the library listing (with the same filters)."""
    item = await session.get(MediaItem, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    lib = await session.get(Library, item.library_id)
    if lib is None or item.kind != lib.kind:
        return {"prev": None, "next": None, "position": None, "total": 0}
    query, _ = await _filtered_items(session, lib, q, missing_on, issues)
    query = _with_letter(query, letter)
    rows = (
        await session.execute(
            query.with_only_columns(MediaItem.id, MediaItem.title).order_by(*_item_order())
        )
    ).all()
    ids = [r.id for r in rows]
    if item_id not in ids:
        return {"prev": None, "next": None, "position": None, "total": len(ids)}
    i = ids.index(item_id)

    def ref(j: int) -> dict | None:
        return {"id": rows[j].id, "title": rows[j].title} if 0 <= j < len(rows) else None

    return {"prev": ref(i - 1), "next": ref(i + 1), "position": i + 1, "total": len(ids)}


@router.get("/items/{item_id}")
async def get_item(item_id: int, session: AsyncSession = Depends(get_session)):
    item = await session.get(MediaItem, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    presence = (await _presence_for(session, [item.id])).get(item.id, [])
    children = []
    if item.kind == "show":
        seasons = list(
            await session.scalars(
                select(MediaItem)
                .where(MediaItem.parent_id == item.id)
                .order_by(MediaItem.season_number)
            )
        )
        season_presence = await _presence_for(session, [s.id for s in seasons])
        episode_counts = (
            await session.execute(
                select(MediaItem.parent_id, ServerItem.server_id, func.count(ServerItem.id))
                .join(ServerItem, ServerItem.media_item_id == MediaItem.id)
                .where(MediaItem.parent_id.in_([s.id for s in seasons]))
                .group_by(MediaItem.parent_id, ServerItem.server_id)
            )
        ).all()
        counts: dict[int, dict[int, int]] = defaultdict(dict)
        for season_id, server_id, n in episode_counts:
            counts[season_id][server_id] = n
        season_art = await _artworks_for(session, item_ids=[s.id for s in seasons])
        children = [
            {
                "id": s.id,
                "title": s.title,
                "season_number": s.season_number,
                "poster_source": s.poster_source,
                "servers": season_presence.get(s.id, []),
                "poster": preferred_poster(
                    season_presence.get(s.id, []), s.poster_source, season_art.get(s.id)
                ),
                "episodes": counts.get(s.id, {}),
            }
            for s in seasons
        ]
    parent = None
    title = item.title
    if item.kind == "season" and item.parent_id:
        show = await session.get(MediaItem, item.parent_id)
        if show is not None:
            parent = {"id": show.id, "title": show.title, "year": show.year}
            name = "Specials" if item.season_number == 0 else f"Season {item.season_number}"
            title = f"{show.title}: {name}"
    collections = await _collections_containing(session, item.id)
    artwork = (await _artworks_for(session, item_ids=[item.id])).get(item.id, [])
    issues = list(await session.scalars(select(Issue).where(Issue.media_item_id == item.id)))
    return {
        "id": item.id,
        "library_id": item.library_id,
        "kind": item.kind,
        "title": title,
        "year": item.year,
        "parent": parent,
        "tmdb_id": item.tmdb_id,
        "tvdb_id": item.tvdb_id,
        "imdb_id": item.imdb_id,
        "poster_source": item.poster_source,
        "facts": item.facts or {},
        "details": item.details or {},
        "servers": presence,
        "artwork": artwork,
        "poster": preferred_poster(presence, item.poster_source, artwork),
        "seasons": children,
        "collections": collections,
        "issues": [_issue_out(i) for i in issues],
    }


async def _collections_containing(session: AsyncSession, item_id: int) -> list[dict]:
    remote = {
        (sid, rid)
        for sid, rid in (
            await session.execute(
                select(ServerItem.server_id, ServerItem.remote_id).where(
                    ServerItem.media_item_id == item_id
                )
            )
        ).all()
    }
    if not remote:
        return []
    server_ids = {sid for sid, _ in remote}
    found: dict[int, str] = {}
    rows = await session.execute(
        select(ServerCollection.server_id, ServerCollection.member_remote_ids, Collection)
        .join(Collection, Collection.id == ServerCollection.collection_id)
        .where(ServerCollection.server_id.in_(server_ids))
    )
    for sid, members, coll in rows:
        if any((sid, rid) in remote for rid in members or []):
            found[coll.id] = coll.name
    return [{"id": cid, "name": name} for cid, name in sorted(found.items(), key=lambda x: x[1])]


class PosterSource(BaseModel):
    poster_source: str | None


async def _validate_source(
    session: AsyncSession,
    source: str | None,
    *,
    media_item_id: int | None = None,
    collection_id: int | None = None,
) -> None:
    if source is None:
        return
    kind, _, value = source.partition(":")
    if not value.isdigit():
        raise HTTPException(422, "poster_source must be 'server:<id>' or 'artwork:<id>'")
    if kind == "server" and await session.get(Server, int(value)):
        return
    if kind == "artwork":
        art = await session.get(Artwork, int(value))
        if art and (
            (media_item_id is not None and art.media_item_id == media_item_id)
            or (collection_id is not None and art.collection_id == collection_id)
        ):
            return
    raise HTTPException(422, "poster_source must be an existing server or this title's artwork")


@router.put("/items/{item_id}/poster-source")
async def set_item_poster_source(
    item_id: int, body: PosterSource, session: AsyncSession = Depends(get_session)
):
    item = await session.get(MediaItem, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    await _validate_source(session, body.poster_source, media_item_id=item.id)
    item.poster_source = body.poster_source
    if body.poster_source:
        await _close_poster_issue(session, media_item_id=item.id)
    await session.commit()
    return {"ok": True}


async def _close_poster_issue(
    session: AsyncSession, *, media_item_id: int | None = None, collection_id: int | None = None
) -> None:
    query = select(Issue).where(Issue.type == "poster_differs")
    if media_item_id is not None:
        query = query.where(Issue.media_item_id == media_item_id)
    else:
        query = query.where(Issue.collection_id == collection_id)
    for issue in await session.scalars(query):
        await session.delete(issue)


# --- collections ------------------------------------------------------------------------------


def _sc_out(sc: ServerCollection, member_map: dict[tuple[int, str], int]) -> dict[str, Any]:
    return {
        "server_id": sc.server_id,
        "server_collection_id": sc.id,
        "remote_id": sc.remote_id,
        "name": sc.name,
        "smart": sc.smart,
        "member_count": len(sc.member_remote_ids or []),
        "matched_members": [
            m for rid in sc.member_remote_ids or [] if (m := member_map.get((sc.server_id, rid)))
        ],
        "has_poster": bool(sc.poster_ref),
        "poster_version": (sc.poster_ref or "")[-24:],
        "kometa_overlay": any(str(t).lower() == "overlay" for t in sc.labels or []),
    }


async def _member_map(session: AsyncSession, scs: list[ServerCollection]) -> dict:
    wanted = {(sc.server_id, rid) for sc in scs for rid in sc.member_remote_ids or []}
    if not wanted:
        return {}
    server_ids = {sid for sid, _ in wanted}
    rows = await session.execute(
        select(ServerItem.server_id, ServerItem.remote_id, ServerItem.media_item_id).where(
            ServerItem.server_id.in_(server_ids),
            ServerItem.kind.in_(("movie", "show")),
            ServerItem.media_item_id.is_not(None),
        )
    )
    return {(sid, rid): mi for sid, rid, mi in rows if (sid, rid) in wanted}


def initial_letter(name: str) -> str:
    """Python twin of ``initial_of`` (and the UI's A-Z bar)."""
    trimmed = re.sub(r"^(the|an|a)\s+", "", name or "", flags=re.IGNORECASE).lstrip()
    first = trimmed[:1].upper()
    return first if "A" <= first <= "Z" else "#"


@router.get("/collections/{collection_id}/neighbours")
async def collection_neighbours(
    collection_id: int,
    q: str | None = None,
    issues_only: bool = False,
    letter: str | None = Query(None, pattern="^[A-Z#]$"),
    session: AsyncSession = Depends(get_session),
):
    """The previous and next collection as listed on the Collections page."""
    coll = await session.get(Collection, collection_id)
    if coll is None:
        raise HTTPException(404, "Collection not found")
    query = select(Collection.id, Collection.name).where(
        Collection.library_id == coll.library_id
        if coll.library_id is not None
        else Collection.library_id.is_(None)
    )
    if q:
        query = query.where(Collection.name.ilike(f"%{q.strip()}%"))
    if issues_only:
        query = query.where(
            Collection.id.in_(select(Issue.collection_id).where(Issue.status == "open"))
        )
    rows = [
        r
        for r in (
            await session.execute(
                query.order_by(func.coalesce(Collection.sort_title, Collection.name), Collection.id)
            )
        ).all()
        if not letter or initial_letter(r.name) == letter
    ]
    ids = [r.id for r in rows]
    if collection_id not in ids:
        return {"prev": None, "next": None, "position": None, "total": len(ids)}
    i = ids.index(collection_id)

    def ref(j: int) -> dict | None:
        return {"id": rows[j].id, "title": rows[j].name} if 0 <= j < len(rows) else None

    return {"prev": ref(i - 1), "next": ref(i + 1), "position": i + 1, "total": len(ids)}


@router.get("/collections")
async def list_collections(
    library_id: int | None = None,
    q: str | None = None,
    issues_only: bool = False,
    session: AsyncSession = Depends(get_session),
):
    query = select(Collection)
    if library_id is not None:
        query = query.where(Collection.library_id == library_id)
    if q:
        query = query.where(Collection.name.ilike(f"%{q.strip()}%"))
    if issues_only:
        query = query.where(
            Collection.id.in_(select(Issue.collection_id).where(Issue.status == "open"))
        )
    colls = list(
        await session.scalars(
            query.order_by(func.coalesce(Collection.sort_title, Collection.name), Collection.id)
        )
    )
    ids = [c.id for c in colls]
    scs = (
        list(
            await session.scalars(
                select(ServerCollection)
                .where(ServerCollection.collection_id.in_(ids))
                .order_by(ServerCollection.server_id)
            )
        )
        if ids
        else []
    )
    member_map = await _member_map(session, scs)
    by_coll: dict[int, list[dict]] = defaultdict(list)
    for sc in scs:
        out = _sc_out(sc, member_map)
        out["matched_members"] = len(out["matched_members"])
        by_coll[sc.collection_id].append(out)
    issue_rows = (
        (
            await session.execute(
                select(Issue.collection_id, Issue.type).where(
                    Issue.collection_id.in_(ids), Issue.status == "open"
                )
            )
        ).all()
        if ids
        else []
    )
    issue_map: dict[int, list[str]] = defaultdict(list)
    for cid, t in issue_rows:
        issue_map[cid].append(t)
    artworks = await _artworks_for(session, coll_ids=ids)
    return [
        {
            "id": c.id,
            "library_id": c.library_id,
            "name": c.name,
            "sort_title": c.sort_title,
            "poster_source": c.poster_source,
            "servers": by_coll.get(c.id, []),
            "poster": preferred_poster(by_coll.get(c.id, []), c.poster_source, artworks.get(c.id)),
            "issues": issue_map.get(c.id, []),
        }
        for c in colls
    ]


@router.get("/collections/{collection_id}")
async def get_collection(collection_id: int, session: AsyncSession = Depends(get_session)):
    coll = await session.get(Collection, collection_id)
    if coll is None:
        raise HTTPException(404, "Collection not found")
    scs = list(
        await session.scalars(
            select(ServerCollection)
            .where(ServerCollection.collection_id == coll.id)
            .order_by(ServerCollection.server_id)
        )
    )
    member_map = await _member_map(session, scs)
    servers = [_sc_out(sc, member_map) for sc in scs]
    all_members = sorted({m for s in servers for m in s["matched_members"]})
    items = {
        i.id: i
        for i in await session.scalars(select(MediaItem).where(MediaItem.id.in_(all_members)))
    }
    presence = await _presence_for(session, all_members)
    member_art = await _artworks_for(session, item_ids=all_members)
    artwork = (await _artworks_for(session, coll_ids=[coll.id])).get(coll.id, [])
    members = sorted(
        (
            {
                "id": mid,
                "title": items[mid].title,
                "year": items[mid].year,
                "in_collection_on": [
                    s["server_id"] for s in servers if mid in s["matched_members"]
                ],
                "servers": presence.get(mid, []),
                "poster": preferred_poster(
                    presence.get(mid, []), items[mid].poster_source, member_art.get(mid)
                ),
            }
            for mid in all_members
            if mid in items
        ),
        key=lambda m: (m["year"] or 0, m["title"]),
    )
    for s in servers:
        s["matched_members"] = len(s["matched_members"])
    issues = list(await session.scalars(select(Issue).where(Issue.collection_id == coll.id)))
    return {
        "id": coll.id,
        "library_id": coll.library_id,
        "name": coll.name,
        "sort_title": coll.sort_title,
        "summary": coll.summary,
        "poster_source": coll.poster_source,
        "servers": servers,
        "artwork": artwork,
        "poster": preferred_poster(servers, coll.poster_source, artwork),
        "members": members,
        "issues": [_issue_out(i) for i in issues],
    }


@router.put("/collections/{collection_id}/poster-source")
async def set_collection_poster_source(
    collection_id: int, body: PosterSource, session: AsyncSession = Depends(get_session)
):
    coll = await session.get(Collection, collection_id)
    if coll is None:
        raise HTTPException(404, "Collection not found")
    await _validate_source(session, body.poster_source, collection_id=coll.id)
    coll.poster_source = body.poster_source
    if body.poster_source:
        await _close_poster_issue(session, collection_id=coll.id)
    await session.commit()
    return {"ok": True}


class CollectionSync(BaseModel):
    create: bool = True
    add_members: bool = True
    server_ids: list[int] | None = None


@router.post("/collections/{collection_id}/sync")
async def sync_collection(
    collection_id: int,
    body: CollectionSync,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if await session.get(Collection, collection_id) is None:
        raise HTTPException(404, "Collection not found")
    job = await request.app.state.jobs.enqueue(
        "collection_sync", {"collection_ids": [collection_id], **body.model_dump()}
    )
    return {"job_id": job.id}


class CollectionRename(BaseModel):
    name: str = Field(min_length=1, max_length=300)


@router.post("/collections/{collection_id}/rename")
async def rename_collection(
    collection_id: int,
    body: CollectionRename,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if await session.get(Collection, collection_id) is None:
        raise HTTPException(404, "Collection not found")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Name can't be blank")
    job = await request.app.state.jobs.enqueue(
        "collection_rename", {"collection_id": collection_id, "name": name}
    )
    return {"job_id": job.id}


@router.post("/items/{item_id}/refresh")
async def refresh_item(
    item_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    item = await session.get(MediaItem, item_id)
    if item is None or item.kind not in ("movie", "show"):
        raise HTTPException(404, "Only movies and shows can be refreshed")
    job = await request.app.state.jobs.enqueue("refresh_item", {"media_item_id": item_id})
    return {"job_id": job.id}


@router.post("/collections/{collection_id}/refresh")
async def refresh_collection(
    collection_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    if await session.get(Collection, collection_id) is None:
        raise HTTPException(404, "Collection not found")
    job = await request.app.state.jobs.enqueue(
        "refresh_collection", {"collection_id": collection_id}
    )
    return {"job_id": job.id}


class RemoveMember(BaseModel):
    media_item_id: int


@router.post("/collections/{collection_id}/remove-member")
async def remove_collection_member(
    collection_id: int,
    body: RemoveMember,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if await session.get(Collection, collection_id) is None:
        raise HTTPException(404, "Collection not found")
    if await session.get(MediaItem, body.media_item_id) is None:
        raise HTTPException(404, "Title not found")
    job = await request.app.state.jobs.enqueue(
        "collection_remove", {"collection_id": collection_id, "media_item_id": body.media_item_id}
    )
    return {"job_id": job.id}


# --- images -----------------------------------------------------------------------------------


async def _thumb(request: Request, server: Server, remote_id: str, ref: str, width: int):
    try:
        path = await request.app.state.thumbs.get(server, remote_id, ref, width)
    except MediaServerError as exc:
        raise HTTPException(502, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(502, f"Unreadable image: {exc}") from exc
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"}
    )


@router.get("/images/item/{server_item_id}")
async def item_image(
    server_item_id: int,
    request: Request,
    w: int = Query(300, ge=50, le=1000),
    session: AsyncSession = Depends(get_session),
):
    si = await session.get(ServerItem, server_item_id)
    if si is None or not si.poster_ref:
        raise HTTPException(404, "No poster")
    server = await session.get(Server, si.server_id)
    return await _thumb(request, server, si.remote_id, si.poster_ref, w)


@router.get("/images/artwork/{artwork_id}")
async def artwork_image(
    artwork_id: int,
    request: Request,
    w: int = Query(300, ge=50, le=1000),
    session: AsyncSession = Depends(get_session),
):
    art = await session.get(Artwork, artwork_id)
    if art is None:
        raise HTTPException(404, "No such artwork")
    source = request.app.state.config.config_dir / art.file_path
    try:
        path = await request.app.state.thumbs.get_local(f"artwork:{art.id}", source, w)
    except OSError as exc:
        raise HTTPException(404, f"Artwork file missing or unreadable: {exc}") from exc
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"}
    )


@router.get("/images/collection/{server_collection_id}")
async def collection_image(
    server_collection_id: int,
    request: Request,
    w: int = Query(300, ge=50, le=1000),
    session: AsyncSession = Depends(get_session),
):
    sc = await session.get(ServerCollection, server_collection_id)
    if sc is None or not sc.poster_ref:
        raise HTTPException(404, "No poster")
    server = await session.get(Server, sc.server_id)
    return await _thumb(request, server, sc.remote_id, sc.poster_ref, w)


def _issue_out(i: Issue) -> dict[str, Any]:
    return {
        "id": i.id,
        "type": i.type,
        "status": i.status,
        "title": i.title,
        "library_id": i.library_id,
        "media_item_id": i.media_item_id,
        "collection_id": i.collection_id,
        "details": i.details,
        "updated_at": i.updated_at,
    }
