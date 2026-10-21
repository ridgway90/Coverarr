from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.models import Library
from coverarr.services.definitions import save_definitions
from coverarr.services.kometa_yaml import (
    KometaYamlError,
    ParsedCollection,
    is_config_file,
    parse_collection_file,
    parse_config,
)
from coverarr.services.settings_store import set_setting

router = APIRouter(prefix="/api", tags=["kometa"])

MAX_YAML_BYTES = 2_000_000


def _config_dir(request: Request) -> Path:
    return request.app.state.config.config_dir


def _save_upload(upload: UploadFile, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(upload.file, out, length=1024 * 1024)


@router.post("/kometa/originals/upload")
async def upload_originals(
    request: Request,
    file: Annotated[UploadFile, File()],
    server_id: Annotated[int | None, Form()] = None,
):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(422, "Upload a .zip of the Kometa 'Original Posters' folder(s)")
    dest = _config_dir(request) / "imports" / f"kometa-originals-{uuid.uuid4().hex}.zip"
    await asyncio.to_thread(_save_upload, file, dest)
    job = await request.app.state.jobs.enqueue(
        "kometa_originals",
        {
            "source": str(dest),
            "server_id": server_id,
            "config_dir": str(_config_dir(request)),
            "remove_after": True,
        },
    )
    return {"job_id": job.id}


class FolderImport(BaseModel):
    path: str
    server_id: int | None = None


@router.post("/kometa/originals/folder")
async def import_originals_folder(body: FolderImport, request: Request):
    path = Path(body.path.strip())
    if not path.is_absolute():
        raise HTTPException(422, "Give the full path as Coverarr sees it, e.g. /config/kometa")
    if not await asyncio.to_thread(path.is_dir):
        raise HTTPException(422, f"Folder not found inside the Coverarr container: {path}")
    job = await request.app.state.jobs.enqueue(
        "kometa_originals",
        {"source": str(path), "server_id": body.server_id, "config_dir": str(_config_dir(request))},
    )
    return {"job_id": job.id}


@router.post("/kometa/yaml")
async def import_yaml(
    files: Annotated[list[UploadFile], File()],
    library_id: Annotated[int | None, Form()] = None,
    # An opt-out: a missing boolean form field reads as False (HTML checkbox semantics).
    skip_keys: Annotated[bool, Form()] = False,
    session: AsyncSession = Depends(get_session),
):
    if library_id is not None and await session.get(Library, library_id) is None:
        raise HTTPException(422, "Unknown library")
    parsed: list[ParsedCollection] = []
    errors: list[str] = []
    files_by_library: dict[str, list[str]] = {}
    keys_imported: list[str] = []
    for upload in files:
        name = upload.filename or "file.yml"
        raw = await upload.read(MAX_YAML_BYTES + 1)
        if len(raw) > MAX_YAML_BYTES:
            errors.append(f"{name}: too large for a Kometa YAML file")
            continue
        text = raw.decode("utf-8", errors="replace")
        try:
            if is_config_file(text):
                config = await asyncio.to_thread(parse_config, text, name)
                files_by_library.update(config.files_by_library)
                if not skip_keys:
                    keys_imported += await _import_keys(session, config)
            else:
                parsed += await asyncio.to_thread(parse_collection_file, text, name)
        except KometaYamlError as exc:
            errors.append(str(exc))
    saved = await save_definitions(session, parsed, files_by_library, library_id)
    await session.commit()
    warnings = {p.name: p.warnings for p in parsed if p.warnings}
    return {
        "saved": len(saved),
        "errors": errors,
        "warnings": warnings,
        "keys_imported": keys_imported,
        "unassigned": [d.name for d in saved if d.library_id is None],
    }


async def _import_keys(session: AsyncSession, config) -> list[str]:
    imported = []
    for key, value in (
        ("tmdb_api_key", config.tmdb_key),
        ("trakt_client_id", config.trakt_client_id),
        ("trakt_client_secret", config.trakt_client_secret),
        ("trakt_access_token", config.trakt_access_token),
        ("trakt_refresh_token", config.trakt_refresh_token),
    ):
        if value:
            await set_setting(session, key, value)
            imported.append(key)
    return imported
