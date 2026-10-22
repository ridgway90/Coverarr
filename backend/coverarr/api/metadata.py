"""Metadata edits: a title's name, sort title, summary and labels, pushed to every server."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.models import MediaItem, Server, ServerItem
from coverarr.services.changes import library_names
from coverarr.services.metadata import clean_edits, metadata_changes

router = APIRouter(prefix="/api", tags=["metadata"])


async def _title(session: AsyncSession, media_item_id: int) -> MediaItem:
    item = await session.get(MediaItem, media_item_id)
    if item is None:
        raise HTTPException(404, "Title not found")
    if item.kind not in ("movie", "show"):
        raise HTTPException(422, "Metadata edits are for films and shows")
    return item


@router.get("/items/{media_item_id}/metadata")
async def item_metadata(media_item_id: int, session: AsyncSession = Depends(get_session)):
    """The edits, and what each server shows now (as of the last import)."""
    item = await _title(session, media_item_id)
    pending = {
        t.server_id: t.reason
        for c in await metadata_changes(session, media_item_ids=[item.id])
        for t in c.servers
    }
    servers = [
        {
            "server_id": server.id,
            "server_name": server.name,
            "title": row.title,
            "sort_title": row.sort_title,
            "labels": row.labels or [],
            "pending": pending.get(server.id),
        }
        for row, server in (
            await session.execute(
                select(ServerItem, Server)
                .join(Server, Server.id == ServerItem.server_id)
                .where(ServerItem.media_item_id == item.id)
                .order_by(Server.id)
            )
        ).all()
    ]
    return {"edits": item.metadata_edits or {}, "servers": servers}


class Edits(BaseModel):
    title: str | None = Field(None, max_length=500)
    sort_title: str | None = Field(None, max_length=500)
    summary: str | None = Field(None, max_length=10_000)
    labels: list[str] | str | None = None


@router.put("/items/{media_item_id}/metadata")
async def save_metadata(
    media_item_id: int, body: Edits, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Replace the edits; blank fields aren't managed (each server keeps its own)."""
    item = await _title(session, media_item_id)
    edits = clean_edits(body.model_dump())
    item.metadata_edits = edits or None
    await session.commit()
    return {"edits": edits}


@router.get("/changes/metadata")
async def pending_metadata(
    library_id: int | None = None, session: AsyncSession = Depends(get_session)
):
    changes = await metadata_changes(session, library_id=library_id)
    names = await library_names(session)
    return {
        "total": len(changes),
        "uploads": sum(len(c.servers) for c in changes),
        "items": [{**c.out(), "library": names.get(c.library_id)} for c in changes],
    }
