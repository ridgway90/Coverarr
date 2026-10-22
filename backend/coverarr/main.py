from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from coverarr import __version__
from coverarr.api import (
    artwork,
    auth,
    automation,
    changes,
    definitions,
    housekeeping,
    jobs,
    kometa,
    library,
    overlays,
    review,
    servers,
    settings,
    trakt,
)
from coverarr.config import Settings, get_settings
from coverarr.db import dispose_engine, init_engine, run_migrations, session_factory
from coverarr.security import current_user
from coverarr.services import arr, cpu
from coverarr.services.actions import run_collection_rename, run_collection_sync, run_fix_match
from coverarr.services.artwork import run_poster_set, run_tmdb_reset
from coverarr.services.assets import relocate_artwork
from coverarr.services.changes import changes_stale, run_push_posters
from coverarr.services.client_pool import ClientPool
from coverarr.services.definitions import run_check_definitions, run_sync_definitions
from coverarr.services.housekeeping import run_store_bases, run_tidy_assets
from coverarr.services.importer import run_import, run_rematch
from coverarr.services.jobs import JobRunner
from coverarr.services.kometa import run_kometa_originals
from coverarr.services.overlays import drop_old_render_folders, set_config_dir
from coverarr.services.posters import ThumbnailCache, run_poster_scan
from coverarr.services.refresh import (
    run_collection_remove,
    run_refresh_collection,
    run_refresh_item,
)
from coverarr.services.schedule import Scheduler, run_safety_scan
from coverarr.services.settings_store import ensure_secret


def create_app(config: Settings | None = None) -> FastAPI:
    config = config or get_settings()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(run_migrations, config)
        init_engine(config)
        sm = session_factory()
        async with sm() as session:
            app.state.secret_key = await ensure_secret(session, "secret_key")
        await relocate_artwork(sm, config.config_dir)
        set_config_dir(config.config_dir)
        await asyncio.to_thread(drop_old_render_folders, config.config_dir)
        runner = JobRunner(sm)
        runner.register("import", run_import)
        runner.register("rematch", run_rematch)
        runner.register("poster_scan", run_poster_scan)
        runner.register("collection_sync", run_collection_sync)
        runner.register("fix_match", run_fix_match)
        runner.register("collection_rename", run_collection_rename)
        runner.register("kometa_originals", run_kometa_originals)
        runner.register("refresh_item", run_refresh_item)
        runner.register("refresh_collection", run_refresh_collection)
        runner.register("collection_remove", run_collection_remove)
        runner.register("check_definitions", run_check_definitions)
        runner.register("sync_definitions", run_sync_definitions)
        runner.register("push_posters", run_push_posters)
        runner.register("poster_set", run_poster_set)
        runner.register("tmdb_reset", run_tmdb_reset)
        runner.register("store_bases", run_store_bases)
        runner.register("tidy_assets", run_tidy_assets)
        runner.register("arr_event", arr.run_arr_event)
        runner.register("safety_scan", run_safety_scan)
        await runner.start()
        app.state.jobs = runner
        arr.queue = arr.ArrQueue(runner)
        scheduler = Scheduler(runner, str(config.config_dir))
        scheduler.start()
        app.state.scheduler = scheduler
        app.state.client_pool = ClientPool()
        app.state.thumbs = ThumbnailCache(config.cache_dir, app.state.client_pool)
        try:
            yield
        finally:
            await scheduler.stop()
            arr.queue.close()
            arr.queue = None
            await runner.stop()
            cpu.shutdown()
            await app.state.client_pool.close()
            await dispose_engine()

    app = FastAPI(title="Coverarr", version=__version__, lifespan=lifespan)
    app.state.config = config

    @app.middleware("http")
    async def note_writes(request: Request, call_next):
        response = await call_next(request)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            changes_stale()  # a choice, setting or upload may change what's pending
        return response

    app.include_router(auth.router)
    app.include_router(automation.public)
    protected = [Depends(current_user)]
    for module in (
        servers,
        settings,
        library,
        review,
        jobs,
        kometa,
        changes,
        artwork,
        overlays,
        housekeeping,
        automation,
        definitions,
        trakt,
    ):
        app.include_router(module.router, dependencies=protected)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": __version__}

    _mount_frontend(app, config.static_dir)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    index = static_dir / "index.html"
    if not index.exists():
        return
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    # Top-level files (favicon etc.); everything else is a client-side route.
    root_files = {p.name: p for p in static_dir.iterdir() if p.is_file()}

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        return FileResponse(root_files.get(path, index))


app = create_app()
