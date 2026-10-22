"""Backgrounds (fanart): what each server shows, choosing one, and pushing it everywhere."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import MediaServerError
from coverarr.db import get_session
from coverarr.models import Background, MediaItem, Server, ServerItem
from coverarr.services.artwork import ArtworkError, download_tmdb
from coverarr.services.backgrounds import (
    MAX_BYTES,
    background_changes,
    delete_background,
    save_background,
    tmdb_backdrops,
)
from coverarr.services.changes import library_names

router = APIRouter(prefix="/api", tags=["backgrounds"])
CACHE = {"Cache-Control": "private, max-age=604800"}


def _config_dir(request: Request) -> Path:
    return request.app.state.config.config_dir


async def _title(session: AsyncSession, media_item_id: int) -> MediaItem:
    item = await session.get(MediaItem, media_item_id)
    if item is None:
        raise HTTPException(404, "Title not found")
    if item.kind not in ("movie", "show"):
        raise HTTPException(422, "Backgrounds are for films and shows")
    return item


def _stored_out(bg: Background) -> dict:
    return {
        "id": bg.id,
        "source": bg.source,
        "name": bg.original_name,
        "width": bg.width,
        "height": bg.height,
        "url": f"/api/images/background/{bg.id}",
    }


@router.get("/items/{media_item_id}/backgrounds")
async def item_backgrounds(media_item_id: int, session: AsyncSession = Depends(get_session)):
    """The background on each server, the ones Coverarr keeps, and which is chosen."""
    item = await _title(session, media_item_id)
    pending = {
        t.server_id: t.reason
        for c in await background_changes(session, media_item_ids=[item.id])
        for t in c.servers
    }
    problem = next(
        (c.problem for c in await background_changes(session, media_item_ids=[item.id])),
        None,
    )
    servers = []
    for row, server in (
        await session.execute(
            select(ServerItem, Server)
            .join(Server, Server.id == ServerItem.server_id)
            .where(ServerItem.media_item_id == item.id)
            .order_by(Server.id)
        )
    ).all():
        servers.append(
            {
                "server_id": server.id,
                "server_name": server.name,
                "server_kind": server.kind,
                "url": f"/api/images/background/server/{row.id}?v={row.background_ref}"
                if row.background_ref
                else None,
                "pending": pending.get(server.id),
            }
        )
    stored = await session.scalars(
        select(Background).where(Background.media_item_id == item.id).order_by(Background.id)
    )
    return {
        "chosen": item.background_source,
        "problem": problem,
        "servers": servers,
        "stored": [_stored_out(bg) for bg in stored],
    }


class Choice(BaseModel):
    # "server:<id>", "background:<id>", or None to stop managing this title's background.
    source: str | None


@router.put("/items/{media_item_id}/background")
async def choose_background(
    media_item_id: int, body: Choice, session: AsyncSession = Depends(get_session)
):
    item = await _title(session, media_item_id)
    source = body.source
    if source is not None:
        kind, _, ref = source.partition(":")
        if not ref.isdigit():
            raise HTTPException(422, "Unknown background")
        if kind == "server":
            has = await session.scalar(
                select(ServerItem.id).where(
                    ServerItem.media_item_id == item.id,
                    ServerItem.server_id == int(ref),
                    ServerItem.background_ref.is_not(None),
                )
            )
            if has is None:
                raise HTTPException(422, "That server has no background for this title")
        elif kind == "background":
            bg = await session.get(Background, int(ref))
            if bg is None or bg.media_item_id != item.id:
                raise HTTPException(422, "Unknown background")
        else:
            raise HTTPException(422, "Unknown background")
    item.background_source = source
    await session.commit()
    return {"chosen": source}


@router.post("/items/{media_item_id}/background/upload")
async def upload_background(
    media_item_id: int,
    request: Request,
    file: Annotated[UploadFile, File()],
    session: AsyncSession = Depends(get_session),
):
    """Upload your own background; it becomes the one every server gets."""
    await _title(session, media_item_id)
    data = await file.read(MAX_BYTES + 1)
    try:
        bg = await save_background(
            session,
            _config_dir(request),
            media_item_id=media_item_id,
            source="upload",
            data=data,
            name=file.filename,
        )
    except ArtworkError as exc:
        raise HTTPException(422, str(exc)) from exc
    await session.commit()
    return _stored_out(bg)


@router.get("/items/{media_item_id}/tmdb-backgrounds")
async def list_tmdb_backgrounds(media_item_id: int, session: AsyncSession = Depends(get_session)):
    await _title(session, media_item_id)
    try:
        return await tmdb_backdrops(session, media_item_id)
    except ArtworkError as exc:
        raise HTTPException(422, str(exc)) from exc


class TmdbChoice(BaseModel):
    file_path: str


@router.post("/items/{media_item_id}/background/tmdb")
async def choose_tmdb_background(
    media_item_id: int,
    body: TmdbChoice,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Download a TMDb backdrop and use it as the background."""
    await _title(session, media_item_id)
    try:
        data = await download_tmdb(session, body.file_path)
        bg = await save_background(
            session,
            _config_dir(request),
            media_item_id=media_item_id,
            source="tmdb",
            data=data,
            name=body.file_path.lstrip("/"),
        )
    except ArtworkError as exc:
        raise HTTPException(422, str(exc)) from exc
    await session.commit()
    return _stored_out(bg)


@router.delete("/backgrounds/{background_id}")
async def remove_background(
    background_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    bg = await session.get(Background, background_id)
    if bg is None:
        raise HTTPException(404, "No such background")
    await delete_background(session, _config_dir(request), bg)
    await session.commit()
    return {"ok": True}


@router.get("/changes/backgrounds")
async def pending_backgrounds(
    library_id: int | None = None, session: AsyncSession = Depends(get_session)
):
    """Every chosen background some server doesn't show yet."""
    changes = await background_changes(session, library_id=library_id)
    names = await library_names(session)
    return {
        "total": len(changes),
        "uploads": sum(len(c.servers) for c in changes if not c.problem),
        "items": [{**c.out(), "library": names.get(c.library_id)} for c in changes],
    }


# --- images ---------------------------------------------------------------------------------


@router.get("/images/background/server/{server_item_id}")
async def server_background_image(
    server_item_id: int,
    request: Request,
    w: int = Query(640, ge=100, le=1920),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(ServerItem, server_item_id)
    if row is None or not row.background_ref:
        raise HTTPException(404, "No background")
    server = await session.get(Server, row.server_id)
    try:
        path = await request.app.state.thumbs.get_background(
            server, row.remote_id, row.background_ref, w
        )
    except (MediaServerError, OSError) as exc:
        raise HTTPException(502, str(exc)) from exc
    return FileResponse(path, media_type="image/jpeg", headers=CACHE)


@router.get("/images/background/{background_id}")
async def stored_background_image(
    background_id: int,
    request: Request,
    w: int = Query(640, ge=100, le=1920),
    session: AsyncSession = Depends(get_session),
):
    bg = await session.get(Background, background_id)
    if bg is None:
        raise HTTPException(404, "No such background")
    source = _config_dir(request) / bg.file_path
    try:
        path = await request.app.state.thumbs.get_local(f"background:{bg.id}", source, w)
    except OSError as exc:
        raise HTTPException(404, f"Background file missing or unreadable: {exc}") from exc
    return FileResponse(path, media_type="image/jpeg", headers=CACHE)
