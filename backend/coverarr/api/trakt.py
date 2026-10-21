"""Trakt sign-in (OAuth PKCE: no client secret needed)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.services import trakt

router = APIRouter(prefix="/api/trakt", tags=["trakt"])


@router.get("")
async def trakt_status(session: AsyncSession = Depends(get_session)):
    return await trakt.status(session)


class Connect(BaseModel):
    redirect_uri: str


@router.post("/connect")
async def connect(body: Connect, session: AsyncSession = Depends(get_session)):
    """Start signing in: returns the Trakt page to send the browser to."""
    try:
        return {"url": await trakt.start(session, body.redirect_uri)}
    except trakt.TraktAuthError as exc:
        raise HTTPException(422, str(exc)) from exc


class Callback(BaseModel):
    # The address Trakt sent the browser back to (or just the code).
    response: str


@router.post("/callback")
async def callback(body: Callback, session: AsyncSession = Depends(get_session)):
    try:
        return await trakt.finish(session, body.response)
    except trakt.TraktAuthError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("")
async def disconnect(session: AsyncSession = Depends(get_session)):
    await trakt.disconnect(session)
    return await trakt.status(session)
