"""Persistent background jobs.

Jobs run one at a time on a single worker task. That keeps SQLite writes simple and stops a
burst of work from hammering the media servers. The ``jobs`` table holds history and lets queued
jobs survive a restart. Live progress and log lines of the running job stay in memory (read via
``JobRunner.live``): writing them to the database mid-job would contend with the job's own open
write transaction.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coverarr.models import Job, utcnow

log = logging.getLogger(__name__)

MAX_LOG_LINES = 500


class JobContext:
    def __init__(self, job_id: int, params: dict[str, Any], sm: async_sessionmaker[AsyncSession]):
        self.job_id = job_id
        self.params = params
        self.sessionmaker = sm
        self.log_lines: list[str] = []
        self.fraction = 0.0
        self.message: str | None = None
        # Structured live status for the UI, e.g. per-server import state.
        self.state: dict[str, Any] = {}

    async def progress(self, fraction: float, message: str | None = None) -> None:
        self.fraction = max(0.0, min(1.0, fraction))
        if message is not None:
            self.message = message
        await asyncio.sleep(0)  # let API requests in between long loops

    async def log(self, line: str) -> None:
        log.info("job %s: %s", self.job_id, line)
        self.log_lines.append(line)
        if len(self.log_lines) > MAX_LOG_LINES:
            del self.log_lines[: len(self.log_lines) - MAX_LOG_LINES]


JobHandler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]


class JobRunner:
    def __init__(self, sm: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sm
        self.handlers: dict[str, JobHandler] = {}
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        # The running job's context, keyed by job id.
        self.live: dict[int, JobContext] = {}
        self._running: tuple[int, asyncio.Task[Any]] | None = None
        self._cancelling: set[int] = set()

    def register(self, kind: str, handler: JobHandler) -> None:
        self.handlers[kind] = handler

    async def start(self) -> None:
        async with self.sessionmaker() as session:
            running = await session.scalars(select(Job).where(Job.status == "running"))
            for job in running:
                job.status = "failed"
                job.error = "Interrupted by restart"
                job.finished_at = utcnow()
            queued = await session.scalars(
                select(Job).where(Job.status == "queued").order_by(Job.id)
            )
            for job in queued:
                self._queue.put_nowait(job.id)
            await session.commit()
        self._worker = asyncio.create_task(self._run(), name="coverarr-job-worker")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def enqueue(self, kind: str, params: dict[str, Any] | None = None) -> Job:
        """Queue a job, or return the identical job already waiting. One that is running now
        is not reused: it may have read its work before the change that asks for this one."""
        if kind not in self.handlers:
            raise ValueError(f"Unknown job kind {kind!r}")
        params = params or {}
        async with self.sessionmaker() as session:
            active = await session.scalars(
                select(Job).where(Job.kind == kind, Job.status == "queued")
            )
            for job in active:
                if job.params == params:
                    return job
            job = Job(kind=kind, params=params, status="queued")
            session.add(job)
            await session.commit()
            await session.refresh(job)
        self._queue.put_nowait(job.id)
        return job

    async def cancel(self, job_id: int) -> bool:
        """Stop a queued or running job; work it already finished (e.g. posters pushed) is
        kept. False if the job has already ended."""
        async with self.sessionmaker() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status not in ("queued", "running"):
                return False
            if job.status == "queued":
                # Only if the worker hasn't just picked it up.
                done = await session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "queued")
                    .values(status="cancelled", message="Cancelled", finished_at=utcnow())
                )
                await session.commit()
                if done.rowcount:
                    return True
        # The worker may be between marking it running and starting the handler.
        for _ in range(50):
            if self._running and self._running[0] == job_id:
                break
            await asyncio.sleep(0.02)
        if self._running and self._running[0] == job_id:
            self._cancelling.add(job_id)
            self._running[1].cancel()
            return True
        return False

    async def wait_idle(self) -> None:
        """Block until the queue is drained (used by tests)."""
        await self._queue.join()

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._execute(job_id)
            except Exception:  # never let the worker die
                log.exception("Job %s crashed the runner", job_id)
            finally:
                self._queue.task_done()

    async def _execute(self, job_id: int) -> None:
        async with self.sessionmaker() as session:
            claimed = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(status="running", started_at=utcnow())
            )
            job = await session.get(Job, job_id) if claimed.rowcount else None
            await session.commit()
            if job is None:
                return
            kind, params = job.kind, dict(job.params or {})

        from coverarr.services import changes

        ctx = JobContext(job_id, params, self.sessionmaker)
        self.live[job_id] = ctx
        changes.job_started()
        status, result, error = "succeeded", None, None
        try:
            handler = self.handlers.get(kind)
            if handler is None:
                raise RuntimeError(f"Coverarr no longer has a job called {kind!r}")
            task = asyncio.create_task(handler(ctx))
            self._running = (job_id, task)
            try:
                result = await task
            except asyncio.CancelledError:
                if job_id not in self._cancelling:  # the app is shutting down
                    task.cancel()
                    raise
                status = "cancelled"
                ctx.log_lines.append("Cancelled")
            except Exception as exc:
                log.exception("Job %s (%s) failed", job_id, kind)
                status, error = "failed", f"{exc}\n\n{traceback.format_exc()}"
                ctx.log_lines.append(f"Failed: {exc}")
        except RuntimeError as exc:
            status, error = "failed", str(exc)
            ctx.log_lines.append(f"Failed: {exc}")

        try:
            await self._finish(job_id, ctx, status, result, error)
        finally:
            changes.job_finished()
            self.live.pop(job_id, None)
            self._running = None
            self._cancelling.discard(job_id)

    async def _finish(
        self, job_id: int, ctx: JobContext, status: str, result: Any, error: str | None
    ) -> None:
        """Record how the job ended, retrying if the database is busy for a moment."""
        for attempt in range(5):
            try:
                async with self.sessionmaker() as session:
                    job = await session.get(Job, job_id)
                    if job is None:
                        return
                    job.status = status
                    job.result = result
                    job.error = error
                    job.log = ctx.log_lines
                    job.message = ctx.message if status == "succeeded" else status.capitalize()
                    job.progress = 1.0 if status == "succeeded" else ctx.fraction
                    job.finished_at = utcnow()
                    await session.commit()
                return
            except Exception:
                if attempt == 4:
                    raise
                log.warning("Couldn't record the end of job %s; retrying", job_id)
                await asyncio.sleep(2)
