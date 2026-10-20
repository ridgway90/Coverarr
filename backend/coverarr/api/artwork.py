"""Your own artwork: uploads, posters picked from TMDb, and poster sets."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.api.kometa import _save_upload
from coverarr.api.library import _artwork_out, _close_poster_issue
from coverarr.db import get_session
from coverarr.models import Artwork, Collection, MediaItem
from coverarr.services.artwork import (
    MAX_BYTES,
    ArtworkError,
    delete_artwork,
    download_tmdb,
    save_artwork,
    tmdb_posters,
)

router = APIRouter(prefix="/api", tags=["artwork"])


def _config_dir(request: Request) -> Path:
    return request.app.state.config.config_dir


async def _owner(session: AsyncSession, kind: str, owner_id: int) -> dict[str, int]:
    model = MediaItem if kind == "items" else Collection
    if await session.get(model, owner_id) is None:
        raise HTTPException(404, "Not found")
    return {"media_item_id": owner_id} if kind == "items" else {"collection_id": owner_id}


async def _save(
    session: AsyncSession,
    request: Request,
    owner: dict[str, int],
    source: str,
    data: bytes,
    name: str | None,
) -> dict:
    try:
        art = await save_artwork(
            session, _config_dir(request), source=source, data=data, name=name, **owner
        )
    except ArtworkError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _close_poster_issue(session, **owner)
    await session.commit()
    await session.refresh(art)
    return _artwork_out(art)


@router.post("/{kind}/{owner_id}/artwork")
async def upload_artwork(
    kind: str,
    owner_id: int,
    request: Request,
    file: Annotated[UploadFile, File()],
    session: AsyncSession = Depends(get_session),
):
    """Upload your own poster; it becomes the base poster for this title or collection."""
    if kind not in ("items", "collections"):
        raise HTTPException(404)
    owner = await _owner(session, kind, owner_id)
    data = await file.read(MAX_BYTES + 1)
    return await _save(session, request, owner, "upload", data, file.filename)


@router.get("/{kind}/{owner_id}/tmdb-posters")
async def list_tmdb_posters(kind: str, owner_id: int, session: AsyncSession = Depends(get_session)):
    if kind not in ("items", "collections"):
        raise HTTPException(404)
    owner = await _owner(session, kind, owner_id)
    try:
        return await tmdb_posters(session, **owner)
    except ArtworkError as exc:
        raise HTTPException(422, str(exc)) from exc


class TmdbChoice(BaseModel):
    file_path: str


@router.post("/{kind}/{owner_id}/artwork/tmdb")
async def choose_tmdb_poster(
    kind: str,
    owner_id: int,
    body: TmdbChoice,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Download a TMDb poster into the assets folder and use it as the base."""
    if kind not in ("items", "collections"):
        raise HTTPException(404)
    owner = await _owner(session, kind, owner_id)
    try:
        data = await download_tmdb(session, body.file_path)
    except ArtworkError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await _save(session, request, owner, "tmdb", data, body.file_path.lstrip("/"))


@router.delete("/artwork/{artwork_id}")
async def remove_artwork(
    artwork_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    art = await session.get(Artwork, artwork_id)
    if art is None:
        raise HTTPException(404, "No such artwork")
    await delete_artwork(session, _config_dir(request), art)
    from coverarr.services.review import refresh_issues

    await refresh_issues(session)
    await session.commit()
    return {"ok": True}


# --- poster sets -----------------------------------------------------------------------------


@router.post("/poster-sets/upload")
async def upload_poster_set(
    request: Request,
    file: Annotated[UploadFile, File()],
    keep_choices: Annotated[bool, Form()] = False,
):
    """A zip of posters named by title, e.g. a set downloaded from ThePosterDB."""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(422, "Upload the poster set as a .zip")
    dest = _config_dir(request) / "imports" / f"poster-set-{uuid.uuid4().hex}.zip"
    await asyncio.to_thread(_save_upload, file, dest)
    job = await request.app.state.jobs.enqueue(
        "poster_set",
        {
            "source": str(dest),
            "config_dir": str(_config_dir(request)),
            "keep_choices": keep_choices,
            "remove_after": True,
        },
    )
    return {"job_id": job.id}


class SetFolder(BaseModel):
    path: str
    keep_choices: bool = False


@router.post("/poster-sets/folder")
async def import_poster_set_folder(body: SetFolder, request: Request):
    path = Path(body.path.strip())
    if not path.is_absolute():
        raise HTTPException(422, "Give the full path as Coverarr sees it, e.g. /config/sets")
    if not await asyncio.to_thread(path.is_dir):
        raise HTTPException(422, f"Folder not found inside the Coverarr container: {path}")
    job = await request.app.state.jobs.enqueue(
        "poster_set",
        {
            "source": str(path),
            "config_dir": str(_config_dir(request)),
            "keep_choices": body.keep_choices,
        },
    )
    return {"job_id": job.id}
