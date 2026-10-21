"""Housekeeping: suggested bases for titles without one, storing bases, tidying the assets
folder, and the media servers' own clean-up tasks."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.api.library import _close_poster_issue
from coverarr.clients import MediaServerError, client_for
from coverarr.db import get_session
from coverarr.models import Collection, MediaItem, Server, ServerCollection, ServerItem
from coverarr.services.housekeeping import coverage, library_names, suggest_bases

router = APIRouter(prefix="/api/housekeeping", tags=["housekeeping"])

Kind = Literal["movie", "show", "season", "collection"]
Confidence = Literal["high", "medium", "low", "none"]


def _kinds(kind: Kind | None) -> tuple[tuple[str, ...], bool]:
    if kind is None:
        return ("movie", "show", "season"), True
    if kind == "collection":
        return (), True
    return (kind,), False


@router.get("/summary")
async def summary(session: AsyncSession = Depends(get_session)):
    suggestions = await suggest_bases(session)
    by_confidence: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for s in suggestions:
        by_confidence[s.confidence] += 1
    return {"coverage": await coverage(session), "without_base": by_confidence}


@router.get("/bases")
async def bases(
    library_id: int | None = None,
    kind: Kind | None = None,
    confidence: Confidence | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(40, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Titles and collections without a base, each with a suggested one."""
    kinds, collections = _kinds(kind)
    suggestions = await suggest_bases(
        session, library_id=library_id, kinds=kinds, collections=collections
    )
    counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for s in suggestions:
        counts[s.confidence] += 1
    if confidence:
        suggestions = [s for s in suggestions if s.confidence == confidence]
    if q:
        needle = q.strip().lower()
        suggestions = [s for s in suggestions if needle in s.title.lower()]
    names = {row.id: row.name for row in (await session.execute(Server.__table__.select())).all()}
    libs = await library_names(session)
    rows = suggestions[(page - 1) * page_size : page * page_size]
    return {
        "total": len(suggestions),
        "counts": counts,
        "page": page,
        "page_size": page_size,
        "items": [{**s.out(names), "library": libs.get(s.library_id)} for s in rows],
    }


class Choice(BaseModel):
    target: Literal["item", "collection"]
    id: int
    server_id: int


class Accept(BaseModel):
    """Either explicit choices, or every suggestion at ``confidence`` (and above)."""

    choices: list[Choice] = []
    confidence: Confidence | None = None
    library_id: int | None = None
    kind: Kind | None = None
    store: bool = True  # keep a copy of each chosen poster in the assets folder


@router.post("/bases/accept")
async def accept(body: Accept, request: Request, session: AsyncSession = Depends(get_session)):
    choices = list(body.choices)
    if body.confidence:
        allowed = {"high": {"high"}, "medium": {"high", "medium"}, "low": {"high", "medium", "low"}}
        kinds, collections = _kinds(body.kind)
        for s in await suggest_bases(
            session, library_id=body.library_id, kinds=kinds, collections=collections
        ):
            if s.suggested is not None and s.confidence in allowed.get(body.confidence, set()):
                choices.append(Choice(target=s.target, id=s.id, server_id=s.suggested))
    if not choices:
        raise HTTPException(422, "Nothing to accept")
    items: list[int] = []
    colls: list[int] = []
    for choice in choices:
        model, row_model, owner = (
            (MediaItem, ServerItem, ServerItem.media_item_id)
            if choice.target == "item"
            else (Collection, ServerCollection, ServerCollection.collection_id)
        )
        target = await session.get(model, choice.id)
        if target is None:
            continue
        has = await session.scalar(
            select(row_model.id).where(owner == choice.id, row_model.server_id == choice.server_id)
        )
        if has is None:
            raise HTTPException(
                422, f"{choice.target} {choice.id} isn't on server {choice.server_id}"
            )
        target.poster_source = f"server:{choice.server_id}"
        if choice.target == "item":
            await _close_poster_issue(session, media_item_id=choice.id)
            items.append(choice.id)
        else:
            await _close_poster_issue(session, collection_id=choice.id)
            colls.append(choice.id)
    await session.commit()
    job_id = None
    if body.store and (items or colls):
        job = await request.app.state.jobs.enqueue(
            "store_bases",
            {
                "config_dir": str(request.app.state.config.config_dir),
                "media_item_ids": items,
                "collection_ids": colls,
            },
        )
        job_id = job.id
    return {"accepted": len(items) + len(colls), "job_id": job_id}


@router.post("/store-bases")
async def store_bases(request: Request):
    """Keep a copy of every base that still lives only on a server."""
    job = await request.app.state.jobs.enqueue(
        "store_bases", {"config_dir": str(request.app.state.config.config_dir)}
    )
    return {"job_id": job.id}


class Tidy(BaseModel):
    apply: bool = False


@router.post("/tidy")
async def tidy(body: Tidy, request: Request):
    job = await request.app.state.jobs.enqueue(
        "tidy_assets",
        {"config_dir": str(request.app.state.config.config_dir), "apply": body.apply},
    )
    return {"job_id": job.id}


class Maintenance(BaseModel):
    action: Literal["clean_bundles", "optimize", "empty_trash"]


@router.post("/servers/{server_id}/maintenance")
async def maintenance(
    server_id: int, body: Maintenance, session: AsyncSession = Depends(get_session)
):
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(404, "Server not found")
    if server.kind != "plex":
        raise HTTPException(422, "These clean-up tasks are Plex's; Jellyfin and Emby run their own")
    try:
        async with client_for(server) as client:
            message = await client.maintenance(body.action)  # type: ignore[attr-defined]
    except MediaServerError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"message": message}
