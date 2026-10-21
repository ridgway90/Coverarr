from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.models import Collection, Issue, Job, MediaItem, Server, ServerItem

router = APIRouter(prefix="/api", tags=["jobs"])


def job_out(job: Job, runner=None, with_log: bool = False) -> dict:
    live = runner.live.get(job.id) if runner is not None and job.status == "running" else None
    out = {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "progress": live.fraction if live else job.progress,
        "message": live.message if live else job.message,
        "state": live.state if live else {},
        "params": job.params,
        "result": job.result,
        "error": job.error.split("\n\n", 1)[0] if job.error else None,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
    if with_log:
        out["log"] = list(live.log_lines) if live else job.log
    return out


@router.get("/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(30, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    jobs = await session.scalars(select(Job).order_by(Job.id.desc()).limit(limit))
    return [job_out(j, request.app.state.jobs) for j in jobs]


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job_out(job, request.app.state.jobs, with_log=True)


class JobRequest(BaseModel):
    kind: Literal["import", "poster_scan", "rematch"]
    server_ids: list[int] | None = None


@router.post("/jobs")
async def start_job(body: JobRequest, request: Request):
    params = {"server_ids": body.server_ids} if body.kind == "import" and body.server_ids else {}
    job = await request.app.state.jobs.enqueue(body.kind, params)
    if body.kind == "import":
        # Fingerprint whatever posters changed, so poster differences and pending pushes are
        # up to date without a separate scan. Only new or changed posters are downloaded.
        await request.app.state.jobs.enqueue("poster_scan")
    return job_out(job, request.app.state.jobs)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request):
    if not await request.app.state.jobs.cancel(job_id):
        raise HTTPException(409, "That job has already finished")
    return {"ok": True}


@router.get("/dashboard")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    servers = list(await session.scalars(select(Server).order_by(Server.id)))
    per_server = dict(
        (
            await session.execute(
                select(ServerItem.server_id, func.count(ServerItem.id))
                .where(ServerItem.kind.in_(("movie", "show")))
                .group_by(ServerItem.server_id)
            )
        ).all()
    )
    kinds = dict(
        (
            await session.execute(
                select(MediaItem.kind, func.count(MediaItem.id)).group_by(MediaItem.kind)
            )
        ).all()
    )
    collections = await session.scalar(select(func.count(Collection.id)))
    issues = dict(
        (
            await session.execute(
                select(Issue.type, func.count(Issue.id))
                .where(Issue.status == "open")
                .group_by(Issue.type)
            )
        ).all()
    )
    runner = request.app.state.jobs
    last_import = await session.scalar(
        select(Job).where(Job.kind == "import").order_by(Job.id.desc()).limit(1)
    )
    active = await session.scalars(
        select(Job).where(Job.status.in_(("queued", "running"))).order_by(Job.id)
    )
    return {
        "servers": [
            {
                "id": s.id,
                "name": s.name,
                "kind": s.kind,
                "enabled": s.enabled,
                "version": s.version,
                "last_ok_at": s.last_ok_at,
                "last_error": s.last_error,
                "items": per_server.get(s.id, 0),
            }
            for s in servers
        ],
        "counts": {
            "movie": kinds.get("movie", 0),
            "show": kinds.get("show", 0),
            "season": kinds.get("season", 0),
            "episode": kinds.get("episode", 0),
            "collection": collections or 0,
        },
        "issues": issues,
        "last_import": job_out(last_import, runner) if last_import else None,
        "active_jobs": [job_out(j, runner) for j in active],
    }
