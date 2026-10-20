from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from coverarr import __version__
from coverarr.api import auth, jobs, kometa, library, review, servers, settings
from coverarr.config import Settings, get_settings
from coverarr.db import dispose_engine, init_engine, run_migrations, session_factory
from coverarr.security import current_user
from coverarr.services.actions import run_collection_rename, run_collection_sync, run_fix_match
from coverarr.services.client_pool import ClientPool
from coverarr.services.definitions import run_check_definitions
from coverarr.services.importer import run_import, run_rematch
from coverarr.services.jobs import JobRunner
from coverarr.services.kometa import run_kometa_originals
from coverarr.services.posters import ThumbnailCache, run_poster_scan
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
        runner = JobRunner(sm)
        runner.register("import", run_import)
        runner.register("rematch", run_rematch)
        runner.register("poster_scan", run_poster_scan)
        runner.register("collection_sync", run_collection_sync)
        runner.register("fix_match", run_fix_match)
        runner.register("collection_rename", run_collection_rename)
        runner.register("kometa_originals", run_kometa_originals)
        runner.register("check_definitions", run_check_definitions)
        await runner.start()
        app.state.jobs = runner
        app.state.client_pool = ClientPool()
        app.state.thumbs = ThumbnailCache(config.cache_dir, app.state.client_pool)
        try:
            yield
        finally:
            await runner.stop()
            await app.state.client_pool.close()
            await dispose_engine()

    app = FastAPI(title="Coverarr", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.include_router(auth.router)
    protected = [Depends(current_user)]
    for module in (servers, settings, library, review, jobs, kometa):
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
