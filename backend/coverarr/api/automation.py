"""Radarr/Sonarr webhooks (public, guarded by a secret in the URL) and automation settings."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.models import Job
from coverarr.services import arr
from coverarr.services.arr import AUTOMATION_KEY, automation_settings, parse_payload
from coverarr.services.schedule import COLLECTIONS_LAST_KEY, LAST_RUN_KEY, next_run
from coverarr.services.settings_store import ensure_secret, set_setting

TOKEN_KEY = "webhook_token"

# No login: Radarr and Sonarr call this. The token in the path is the credential.
public = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
router = APIRouter(prefix="/api/automation", tags=["automation"])


@public.post("/arr/{token}")
async def arr_webhook(token: str, request: Request, session: AsyncSession = Depends(get_session)):
    expected = await ensure_secret(session, TOKEN_KEY)
    if not secrets.compare_digest(token, expected):
        raise HTTPException(401, "Unknown webhook token")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "Expected a JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Expected a JSON object")
    event = parse_payload(payload)
    if event == "test":
        return {"ok": True, "test": True}
    if event is None or isinstance(event, str):
        return {"ok": True, "ignored": payload.get("eventType")}
    if arr.queue is None:
        raise HTTPException(503, "Not ready yet")
    started = arr.queue.receive(event)
    return {"ok": True, "title": event.label, "action": event.action, "merged": not started}


class WeeklyScan(BaseModel):
    enabled: bool = False
    weekday: int = Field(6, ge=0, le=6)
    hour: int = Field(4, ge=0, le=23)


class CollectionsSync(BaseModel):
    enabled: bool = True
    hour: int = Field(3, ge=0, le=23)


class Automation(BaseModel):
    scan_servers: bool = True
    wait_seconds: int = Field(30, ge=0, le=3600)
    new_base: Literal["tmdb", "none"] = "tmdb"
    auto_push: bool = True
    weekly_scan: WeeklyScan = WeeklyScan()
    collections_sync: CollectionsSync = CollectionsSync()


async def _out(session: AsyncSession) -> dict[str, Any]:
    token = await ensure_secret(session, TOKEN_KEY)
    settings = await automation_settings(session)
    weekly = settings["weekly_scan"]
    last = await session.scalar(
        select(Job.finished_at)
        .where(Job.kind == "safety_scan", Job.status == "succeeded")
        .order_by(Job.id.desc())
        .limit(1)
    )
    return {
        **settings,
        "webhook_path": f"/api/webhooks/arr/{token}",
        "weekly_last_run": last,
        "weekly_next_run": next_run(datetime.now(), weekly["weekday"], weekly["hour"]).isoformat(
            timespec="minutes"
        )
        if weekly["enabled"]
        else None,
    }


@router.get("")
async def read_automation(session: AsyncSession = Depends(get_session)):
    return await _out(session)


@router.put("")
async def update_automation(body: Automation, session: AsyncSession = Depends(get_session)):
    before = await automation_settings(session)
    await set_setting(session, AUTOMATION_KEY, body.model_dump())
    # Count from now, so switching something on doesn't start it straight away.
    now = datetime.now().isoformat(timespec="seconds")
    if body.weekly_scan.enabled and not before["weekly_scan"]["enabled"]:
        await set_setting(session, LAST_RUN_KEY, now)
    if body.collections_sync.enabled and not before["collections_sync"]["enabled"]:
        await set_setting(session, COLLECTIONS_LAST_KEY, now)
    await session.commit()
    return await _out(session)


@router.post("/scan-now")
async def scan_now(request: Request):
    job = await request.app.state.jobs.enqueue(
        "safety_scan", {"config_dir": str(request.app.state.config.config_dir)}
    )
    return {"job_id": job.id}


@router.post("/webhook-token")
async def new_webhook_token(session: AsyncSession = Depends(get_session)):
    """Replace the webhook secret (the old URL stops working)."""
    await set_setting(session, TOKEN_KEY, secrets.token_urlsafe(32))
    await session.commit()
    return await _out(session)
