"""The weekly safety-net scan, and the clock that starts it.

The scan catches anything webhooks missed (a restart, a server that scanned late, changes
made directly on a server): a full import, fingerprints for changed posters, then a push of
only the posters that really differ.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from coverarr.services.jobs import JobContext, JobRunner

log = logging.getLogger(__name__)

LAST_RUN_KEY = "weekly_scan_last"
COLLECTIONS_LAST_KEY = "collections_sync_last"


class StageContext(JobContext):
    """One step of a bigger job: its progress maps onto a slice of the parent's bar."""

    def __init__(self, parent: JobContext, start: float, end: float, params: dict[str, Any]):
        super().__init__(parent.job_id, params, parent.sessionmaker)
        self.parent, self.start, self.end = parent, start, end
        self.state = parent.state
        self.log_lines = parent.log_lines

    async def progress(self, fraction: float, message: str | None = None) -> None:
        span = self.end - self.start
        await self.parent.progress(self.start + span * max(0.0, min(1.0, fraction)), message)

    async def log(self, line: str) -> None:
        await self.parent.log(line)


async def run_safety_scan(ctx: JobContext) -> dict[str, Any]:
    from coverarr.services.changes import run_push_posters
    from coverarr.services.definitions import run_sync_definitions
    from coverarr.services.importer import run_import
    from coverarr.services.posters import run_poster_scan

    result: dict[str, Any] = {}
    await ctx.log("Weekly safety-net scan: import, sync collections, fingerprint, push differences")
    result["import"] = await run_import(StageContext(ctx, 0.0, 0.5, {}))
    # The push at the end covers the collections' posters too.
    result["collections"] = await run_sync_definitions(
        StageContext(ctx, 0.5, 0.6, {"no_push": True})
    )
    result["fingerprints"] = await run_poster_scan(StageContext(ctx, 0.6, 0.75, {}))
    push = await run_push_posters(
        StageContext(ctx, 0.75, 1.0, {"config_dir": ctx.params["config_dir"]})
    )
    result["push"] = push
    await ctx.progress(1.0, f"Safety scan done: {push.get('uploaded', 0)} posters pushed")
    return result


def due_since(now: datetime, weekday: int, hour: int) -> datetime:
    """The most recent scheduled time at or before ``now``."""
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    slot -= timedelta(days=(now.weekday() - weekday) % 7)
    if slot > now:
        slot -= timedelta(days=7)
    return slot


def next_run(now: datetime, weekday: int, hour: int) -> datetime:
    return due_since(now, weekday, hour) + timedelta(days=7)


def daily_since(now: datetime, hour: int) -> datetime:
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=1)


class Scheduler:
    """Checks once a minute whether the weekly scan is due. A scan missed while Coverarr was
    down runs as soon as it's back."""

    interval = 60.0

    def __init__(self, runner: JobRunner, config_dir: str):
        self.runner, self.config_dir = runner, config_dir
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="coverarr-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # never let the clock stop
                log.exception("Scheduler tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self, now: datetime | None = None) -> list[str]:
        """Queue whatever is due; returns the job kinds queued."""
        from coverarr.services.arr import automation_settings

        now = now or datetime.now()
        queued: list[str] = []
        async with self.runner.sessionmaker() as session:
            settings = await automation_settings(session)
            weekly, daily = settings["weekly_scan"], settings["collections_sync"]
            if weekly["enabled"] and await _due(
                session,
                LAST_RUN_KEY,
                due_since(now, int(weekly["weekday"]), int(weekly["hour"])),
                now,
            ):
                queued.append("safety_scan")
            # The weekly scan syncs the collections itself.
            elif daily["enabled"] and await _due(
                session, COLLECTIONS_LAST_KEY, daily_since(now, int(daily["hour"])), now
            ):
                queued.append("sync_definitions")
            await session.commit()
        for kind in queued:
            params = {"config_dir": self.config_dir} if kind == "safety_scan" else {}
            await self.runner.enqueue(kind, params)
        return queued


async def _due(session: Any, key: str, slot: datetime, now: datetime) -> bool:
    """Whether the run recorded under ``key`` is older than ``slot``; records ``now`` if so.
    The first check only starts the clock, so switching something on doesn't run it at once."""
    from coverarr.services.settings_store import get_setting, set_setting

    last = await get_setting(session, key)
    if last is not None and datetime.fromisoformat(last) >= slot:
        return False
    await set_setting(session, key, now.isoformat(timespec="seconds"))
    return last is not None
