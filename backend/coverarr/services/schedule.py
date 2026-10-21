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
    from coverarr.services.importer import run_import
    from coverarr.services.posters import run_poster_scan

    result: dict[str, Any] = {}
    await ctx.log("Weekly safety-net scan: import, fingerprint, push differences")
    result["import"] = await run_import(StageContext(ctx, 0.0, 0.6, {}))
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

    async def tick(self, now: datetime | None = None) -> bool:
        """Queue the weekly scan if it's due; True if it was queued."""
        from coverarr.services.arr import automation_settings
        from coverarr.services.settings_store import get_setting, set_setting

        now = now or datetime.now()
        async with self.runner.sessionmaker() as session:
            weekly = (await automation_settings(session))["weekly_scan"]
            if not weekly["enabled"]:
                return False
            slot = due_since(now, int(weekly["weekday"]), int(weekly["hour"]))
            last = await get_setting(session, LAST_RUN_KEY)
            if last is None:
                # Just switched on: count from now rather than scanning straight away.
                await set_setting(session, LAST_RUN_KEY, now.isoformat(timespec="seconds"))
                await session.commit()
                return False
            if datetime.fromisoformat(last) >= slot:
                return False
            await set_setting(session, LAST_RUN_KEY, now.isoformat(timespec="seconds"))
            await session.commit()
        await self.runner.enqueue("safety_scan", {"config_dir": self.config_dir})
        return True
