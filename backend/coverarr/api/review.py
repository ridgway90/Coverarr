from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.api.library import _issue_out
from coverarr.db import get_session
from coverarr.models import Collection, Issue, MediaItem, Server
from coverarr.services.review import ISSUE_TYPES

router = APIRouter(prefix="/api/review", tags=["review"])

IssueType = Literal[
    "mismatch",
    "similar_collections",
    "item_missing",
    "poster_differs",
    "collection_missing",
    "collection_members_differ",
]


@router.get("/summary")
async def summary(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Issue.type, Issue.status, func.count(Issue.id)).group_by(
                Issue.type, Issue.status
            )
        )
    ).all()
    out = {t: {"open": 0, "ignored": 0} for t in ISSUE_TYPES}
    for t, status, n in rows:
        out.setdefault(t, {"open": 0, "ignored": 0})[status] = n
    return out


@router.get("/issues")
async def list_issues(
    type: IssueType,
    status: Literal["open", "ignored"] = "open",
    library_id: int | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    query = select(Issue).where(Issue.type == type, Issue.status == status)
    if library_id is not None:
        query = query.where(Issue.library_id == library_id)
    if q:
        query = query.where(Issue.title.ilike(f"%{q.strip()}%"))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    issues = await session.scalars(
        query.order_by(Issue.title).offset((page - 1) * page_size).limit(page_size)
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "issues": [_issue_out(i) for i in issues],
    }


class BulkAction(BaseModel):
    action: Literal["ignore", "reopen", "create_collections", "sync_members", "choose_poster"]
    # Either explicit ids or "everything open of this type" (optionally within a library).
    issue_ids: list[int] | None = None
    all_of_type: IssueType | None = None
    library_id: int | None = None
    server_id: int | None = None  # for choose_poster and to limit collection pushes


EXPECTED_TYPE = {
    "create_collections": "collection_missing",
    "sync_members": "collection_members_differ",
    "choose_poster": "poster_differs",
}


@router.post("/actions")
async def bulk_action(
    body: BulkAction, request: Request, session: AsyncSession = Depends(get_session)
):
    if body.issue_ids:
        query = select(Issue).where(Issue.id.in_(body.issue_ids))
    elif body.all_of_type:
        status = "ignored" if body.action == "reopen" else "open"
        query = select(Issue).where(Issue.type == body.all_of_type, Issue.status == status)
        if body.library_id is not None:
            query = query.where(Issue.library_id == body.library_id)
    else:
        raise HTTPException(422, "Give issue_ids or all_of_type")
    issues = list(await session.scalars(query))
    expected = EXPECTED_TYPE.get(body.action)
    if expected:
        issues = [i for i in issues if i.type == expected]

    if body.action in ("ignore", "reopen"):
        for issue in issues:
            issue.status = "ignored" if body.action == "ignore" else "open"
        await session.commit()
        return {"updated": len(issues)}

    if body.action in ("create_collections", "sync_members"):
        ids = sorted({i.collection_id for i in issues if i.collection_id})
        if not ids:
            return {"job_id": None, "updated": 0}
        params = {
            "collection_ids": ids,
            "create": body.action == "create_collections",
            "add_members": body.action == "sync_members",
            "server_ids": [body.server_id] if body.server_id else None,
        }
        job = await request.app.state.jobs.enqueue("collection_sync", params)
        return {"job_id": job.id, "updated": len(ids)}

    # choose_poster: record which server's poster is the clean base for each item.
    if body.server_id is None or await session.get(Server, body.server_id) is None:
        raise HTTPException(422, "choose_poster needs a valid server_id")
    updated = skipped = 0
    for issue in issues:
        entry = next(
            (s for s in issue.details.get("servers", []) if s["server_id"] == body.server_id), None
        )
        if not entry or not entry.get("has_poster"):
            skipped += 1
            continue
        target = (
            await session.get(MediaItem, issue.media_item_id)
            if issue.media_item_id
            else await session.get(Collection, issue.collection_id)
        )
        if target is None:
            skipped += 1
            continue
        target.poster_source = f"server:{body.server_id}"
        await session.delete(issue)
        updated += 1
    await session.commit()
    return {"updated": updated, "skipped": skipped}


class MatchTargetIn(BaseModel):
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None
    title: str | None = None
    year: int | None = None


class FixMatch(BaseModel):
    target: MatchTargetIn


@router.post("/issues/{issue_id}/fix-match")
async def fix_match(
    issue_id: int, body: FixMatch, request: Request, session: AsyncSession = Depends(get_session)
):
    issue = await session.get(Issue, issue_id)
    if issue is None or issue.type != "mismatch":
        raise HTTPException(404, "Mismatch not found")
    target = {
        k: (v.strip() if isinstance(v, str) else v) or None
        for k, v in body.target.model_dump().items()
    }
    if not (target["tmdb_id"] or target["tvdb_id"] or target["imdb_id"]):
        raise HTTPException(422, "Give a TMDb, TVDB or IMDb id to match to")
    if target["tmdb_id"] and not target["tmdb_id"].isdigit():
        raise HTTPException(422, "TMDb ids are numbers")
    if target["tvdb_id"] and not target["tvdb_id"].isdigit():
        raise HTTPException(422, "TVDB ids are numbers")
    if target["imdb_id"] and not target["imdb_id"].startswith("tt"):
        raise HTTPException(422, "IMDb ids look like tt0123456")
    job = await request.app.state.jobs.enqueue(
        "fix_match", {"issue_id": issue_id, "target": target}
    )
    return {"job_id": job.id}
