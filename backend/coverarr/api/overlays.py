"""Overlay settings, the overlay pack, and rendered previews."""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.api.kometa import _save_upload
from coverarr.db import get_session
from coverarr.models import Artwork, Library, MediaItem, Server, ServerItem
from coverarr.services import cpu, overlay_slots
from coverarr.services.changes import plan_item
from coverarr.services.mediainfo import RANK
from coverarr.services.overlays import (
    LEVELS,
    OverlayError,
    _safe_rel,
    clean_layers,
    default_library,
    describe_pack,
    install_pack,
    library_config,
    load_config,
    pack_dir,
    plan_args,
    render_image,
    render_to_file,
    rendered_path,
    save_config,
)

router = APIRouter(prefix="/api/overlays", tags=["overlays"])


def _config_dir(request: Request) -> Path:
    return request.app.state.config.config_dir


@router.get("")
async def get_overlays(request: Request, session: AsyncSession = Depends(get_session)):
    config = await load_config(session)
    libraries = []
    for lib in await session.scalars(select(Library).order_by(Library.id)):
        if lib.kind not in LEVELS:
            continue
        libraries.append(
            {
                "id": lib.id,
                "name": lib.name,
                "kind": lib.kind,
                **library_config(config, lib.id, lib.kind),
            }
        )
    # Titles imported before media info was collected have no facts yet.
    no_facts = await session.scalar(
        select(func.count(MediaItem.id)).where(
            MediaItem.kind.in_(("movie", "episode")), MediaItem.facts.is_(None)
        )
    )
    return {
        "pack": await asyncio.to_thread(describe_pack, _config_dir(request), config.get("pack")),
        "libraries": libraries,
        "needs_import": bool(no_facts),
    }


class LibraryOverlays(BaseModel):
    enabled: bool
    levels: dict[str, Any]


@router.put("/libraries/{library_id}")
async def save_library(
    library_id: int, body: LibraryOverlays, session: AsyncSession = Depends(get_session)
):
    lib = await session.get(Library, library_id)
    if lib is None or lib.kind not in LEVELS:
        raise HTTPException(404, "Library not found")
    levels = {}
    try:
        for level, layers in body.levels.items():
            if level not in LEVELS[lib.kind]:
                raise OverlayError(f"{lib.name} has no {level} level")
            levels[level] = clean_layers(layers)
    except OverlayError as exc:
        raise HTTPException(422, str(exc)) from exc
    config = await load_config(session)
    config["libraries"][str(library_id)] = {"enabled": body.enabled, "levels": levels}
    await save_config(session, config)
    await session.commit()
    return library_config(config, lib.id, lib.kind)


@router.delete("/libraries/{library_id}")
async def reset_library(library_id: int, session: AsyncSession = Depends(get_session)):
    """Back to the default layers (and switched off)."""
    lib = await session.get(Library, library_id)
    if lib is None or lib.kind not in LEVELS:
        raise HTTPException(404, "Library not found")
    config = await load_config(session)
    config["libraries"].pop(str(library_id), None)
    await save_config(session, config)
    await session.commit()
    return default_library(lib.kind)


@router.post("/pack")
async def upload_pack(
    request: Request,
    file: Annotated[UploadFile, File()],
    session: AsyncSession = Depends(get_session),
):
    """Install an overlay pack (e.g. the jmxd zip) into /config/assets/overlays."""
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(422, "Upload the overlay pack as a .zip")
    dest = _config_dir(request) / "imports" / f"overlay-pack-{uuid.uuid4().hex}.zip"
    await asyncio.to_thread(_save_upload, file, dest)
    name = Path(filename).stem or "pack"
    try:
        info = await asyncio.to_thread(install_pack, _config_dir(request), dest, name)
    except zipfile.BadZipFile as exc:
        raise HTTPException(422, "That file isn't a valid zip") from exc
    except (OverlayError, OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await asyncio.to_thread(dest.unlink, missing_ok=True)
    config = await load_config(session)
    config["pack"] = info["name"]
    await save_config(session, config)
    await session.commit()
    return info


@router.get("/samples")
async def samples(
    library_id: int,
    level: str,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Titles to preview overlays on: ones with a resolution first, then by name."""
    query = select(MediaItem).where(MediaItem.library_id == library_id, MediaItem.kind == level)
    if q:
        query = query.where(MediaItem.title.ilike(f"%{q.strip()}%"))
    rows = list(
        await session.scalars(
            query.order_by(MediaItem.facts.is_(None), MediaItem.sort_title, MediaItem.title).limit(
                200
            )
        )
    )

    def richness(m: MediaItem) -> tuple:
        facts = m.facts or {}
        return (
            -RANK.get(facts.get("resolution") or "", -1),
            -(bool(facts.get("dv")) + bool(facts.get("hdr"))),
        )

    rows.sort(key=richness)
    out = []
    for m in rows[:30]:
        label = m.title
        if level in ("season", "episode") and m.parent_id:
            parent = await session.get(MediaItem, m.parent_id)
            show = (
                await session.get(MediaItem, parent.parent_id)
                if parent is not None and level == "episode" and parent.parent_id
                else parent
            )
            if level == "season":
                label = f"{show.title if show else '?'}: Season {m.season_number}"
            else:
                season_number = m.season_number
                if season_number is None and parent is not None:
                    season_number = parent.season_number
                label = (
                    f"{show.title if show else '?'} S{(season_number or 0):02d}"
                    f"E{(m.episode_number or 0):02d}"
                )
        out.append({"id": m.id, "title": label, "facts": m.facts or {}})
    return out


async def _base_bytes(request: Request, session: AsyncSession, base: str) -> bytes:
    if base.startswith("artwork:"):
        art = await session.get(Artwork, int(base.split(":", 1)[1]))
        if art is None:
            raise HTTPException(404, "Artwork missing")
        return await asyncio.to_thread((_config_dir(request) / art.file_path).read_bytes)
    row = await session.get(ServerItem, int(base.split(":", 2)[1]))
    server = await session.get(Server, row.server_id) if row else None
    if row is None or server is None or not row.poster_ref:
        raise HTTPException(404, "Base poster missing")
    client = await request.app.state.client_pool.get(server)
    return await client.fetch_poster(row.remote_id, row.poster_ref)


def _resize(data: bytes, width: int) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=88)
        return out.getvalue()


@router.get("/preview/item/{media_item_id}")
async def preview_item(
    media_item_id: int,
    request: Request,
    w: int = Query(400, ge=50, le=2000),
    session: AsyncSession = Depends(get_session),
):
    """This title's poster with its overlays, as it would be pushed (cached by render key).
    Renders on the worker processes, a few at a time, so a page of previews can't starve
    the API."""
    change = await plan_item(session, media_item_id)
    if change is None or change.render is None:
        raise HTTPException(404, change.problem if change else "No overlays for this title")
    render = change.render
    dest = rendered_path(_config_dir(request), f"m{media_item_id}", render.key)
    if not await asyncio.to_thread(dest.is_file):
        base = await _base_bytes(request, session, render.base)
        async with _preview_slots():
            if not await asyncio.to_thread(dest.is_file):
                await cpu.run(
                    render_to_file, base, render.plan.size, plan_args(render.plan), str(dest)
                )
    path = await request.app.state.thumbs.get_local(f"rendered:{dest.name}", dest, w)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"}
    )


_preview_lock: asyncio.Semaphore | None = None


def _preview_slots() -> asyncio.Semaphore:
    global _preview_lock
    if _preview_lock is None:
        _preview_lock = asyncio.Semaphore(cpu.workers())
    return _preview_lock


class PreviewRequest(BaseModel):
    media_item_id: int
    layers: list[dict[str, Any]]
    width: int = 500


@router.post("/preview")
async def preview_layers(
    body: PreviewRequest, request: Request, session: AsyncSession = Depends(get_session)
):
    """Live preview of unsaved layers on one title (nothing is stored)."""
    try:
        layers = clean_layers(body.layers)
    except OverlayError as exc:
        raise HTTPException(422, str(exc)) from exc
    change = await plan_item(session, body.media_item_id, layers)
    if change is None:
        raise HTTPException(404, "Title not found")
    if change.render is None:
        raise HTTPException(422, change.problem or "Nothing to draw on")
    base = await _base_bytes(request, session, change.render.base)
    data = await asyncio.to_thread(
        render_image, base, change.render.plan.size, plan_args(change.render.plan)
    )
    data = await asyncio.to_thread(_resize, data, max(100, min(body.width, 1200)))
    return Response(
        data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            # Network names can be in any script; headers are latin-1 only.
            "X-Overlay-Missing": quote(", ".join(change.render.plan.missing)[:500]),
        },
    )


# --- single overlay images ---------------------------------------------------------------------


async def _slot_context(
    request: Request, session: AsyncSession
) -> tuple[dict[str, Any], Path | None, list[overlay_slots.Slot]]:
    config = await load_config(session)
    networks = [
        f["network"]
        for (f,) in (
            await session.execute(select(MediaItem.facts).where(MediaItem.kind == "show"))
        ).all()
        if f and f.get("network")
    ]
    root = pack_dir(_config_dir(request), config.get("pack"))
    return config, root, overlay_slots.catalogue(config, networks)


@router.get("/slots")
async def list_slots(request: Request, session: AsyncSession = Depends(get_session)):
    """Every overlay image the layers can use, and what fills each slot now."""
    config, root, slots = await _slot_context(request, session)
    described = await asyncio.to_thread(overlay_slots.describe, root, slots)
    return {
        "pack": config.get("pack"),
        "folders": overlay_slots.folders(config),
        "slots": described,
    }


@router.post("/slots")
async def upload_slot(
    request: Request,
    file: Annotated[UploadFile, File()],
    path: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
):
    """Put one image into a slot: converted to PNG and named as the renderer expects."""
    config, root, slots = await _slot_context(request, session)
    slot = next((s for s in slots if s.path == path), None)
    if slot is None:
        raise HTTPException(422, "Unknown overlay slot")
    if root is None:
        # No pack yet: start one, so single images can be built up from nothing.
        config["pack"] = "custom"
        root = pack_dir(_config_dir(request), "custom")
        await save_config(session, config)
        await session.commit()
    data = await file.read(overlay_slots.MAX_IMAGE_BYTES + 1)
    try:
        saved = await asyncio.to_thread(overlay_slots.save, root, slot, data)
    except OverlayError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"path": saved}


@router.delete("/slots")
async def delete_slot(
    request: Request, path: str = Query(...), session: AsyncSession = Depends(get_session)
):
    config, root, slots = await _slot_context(request, session)
    if root is None or not any(s.path == path for s in slots):
        raise HTTPException(404, "Unknown overlay slot")
    await asyncio.to_thread(overlay_slots.remove, root, path)
    return {"ok": True}


class GuessRequest(BaseModel):
    names: list[str]


@router.post("/slots/guess")
async def guess_slots(
    body: GuessRequest, request: Request, session: AsyncSession = Depends(get_session)
):
    """Which slot each file name is probably for (None when it can't tell)."""
    _, _, slots = await _slot_context(request, session)
    return [{"name": n, "path": overlay_slots.guess(n, slots)} for n in body.names[:200]]


@router.get("/pack/file")
async def pack_file(
    request: Request, path: str = Query(...), session: AsyncSession = Depends(get_session)
):
    """One image from the pack (for thumbnails)."""
    config = await load_config(session)
    root = pack_dir(_config_dir(request), config.get("pack"))
    try:
        rel = _safe_rel(path)
    except OverlayError as exc:
        raise HTTPException(422, str(exc)) from exc
    full = root / rel if root else None
    if (
        full is None
        or full.suffix.lower() not in overlay_slots.IMAGE_SUFFIXES
        or not full.is_file()
    ):
        raise HTTPException(404, "Not in the overlay pack")
    return FileResponse(full, headers={"Cache-Control": "private, max-age=3600"})
