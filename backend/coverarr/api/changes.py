"""Pending poster changes: list them, push them to the servers, or discard them."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.models import Collection, MediaItem
from coverarr.services.changes import changes_summary, compute_changes, library_names
from coverarr.services.review import refresh_issues

router = APIRouter(prefix="/api/changes", tags=["changes"])


@router.get("")
async def list_changes(
    library_id: int | None = None,
    target: Literal["item", "collection"] | None = None,
    media_item_id: int | None = None,
    collection_id: int | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    changes = await compute_changes(
        session,
        library_id=library_id,
        media_item_ids=[media_item_id] if media_item_id is not None else None,
        collection_ids=[collection_id] if collection_id is not None else None,
        items=target in (None, "item"),
        collections=target in (None, "collection"),
    )
    if q:
        needle = q.strip().lower()
        changes = [c for c in changes if needle in c.title.lower()]
    names = await library_names(session)
    rows = changes[(page - 1) * page_size : page * page_size]
    return {
        "total": len(changes),
        "uploads": sum(len(c.servers) for c in changes if not c.problem),
        "page": page,
        "page_size": page_size,
        "items": [{**c.out(), "library": names.get(c.library_id)} for c in rows],
    }


@router.get("/summary")
async def summary(session: AsyncSession = Depends(get_session)):
    return await changes_summary(session)


class Targets(BaseModel):
    """Which changes to act on. Both omitted means all of them."""

    media_item_ids: list[int] | None = None
    collection_ids: list[int] | None = None


class PushRequest(Targets):
    # Upload to every server even where the poster already looks right.
    force: bool = False
    library_id: int | None = None


@router.post("/push")
async def push(body: PushRequest, request: Request):
    params: dict = {"config_dir": str(request.app.state.config.config_dir)}
    if body.force:
        params["force"] = True
    if body.library_id is not None:
        params["library_id"] = body.library_id
    if body.media_item_ids is not None or body.collection_ids is not None:
        params["media_item_ids"] = body.media_item_ids or []
        params["collection_ids"] = body.collection_ids or []
    job = await request.app.state.jobs.enqueue("push_posters", params)
    return {"job_id": job.id}


@router.post("/discard")
async def discard(body: Targets, session: AsyncSession = Depends(get_session)):
    """Forget the chosen base poster, so the servers are left as they are."""
    if not body.media_item_ids and not body.collection_ids:
        raise HTTPException(422, "Say which titles or collections to discard")
    if body.media_item_ids:
        await session.execute(
            update(MediaItem)
            .where(MediaItem.id.in_(body.media_item_ids))
            .values(poster_source=None)
        )
    if body.collection_ids:
        await session.execute(
            update(Collection)
            .where(Collection.id.in_(body.collection_ids))
            .values(poster_source=None)
        )
    await refresh_issues(session)
    await session.commit()
    return {"ok": True}
