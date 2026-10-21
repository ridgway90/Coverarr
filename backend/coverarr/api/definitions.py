"""Collections built from lists: defined here or imported from Kometa, checked and synced."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.models import Collection, CollectionDefinition, Library, ServerCollection
from coverarr.services.lists import ListError, default_sync_mode, normalize_builder
from coverarr.services.matching import normalize_collection_name

router = APIRouter(prefix="/api", tags=["definitions"])


def _definition_out(d: CollectionDefinition, colls: dict[tuple[int | None, str], Any]) -> dict:
    coll = colls.get((d.library_id, normalize_collection_name(d.name)))
    return {
        "id": d.id,
        "name": d.name,
        "library_id": d.library_id,
        "source": d.source,
        "source_file": d.source_file,
        "builders": d.builders,
        "options": d.options,
        # What a sync does when the definition doesn't say (charts replace, lists add).
        "sync_mode": (d.options or {}).get("sync_mode") or default_sync_mode(d.builders or []),
        "unsupported": d.unsupported,
        "enabled": d.enabled,
        "check_result": d.check_result,
        "checked_at": d.checked_at,
        "sync_result": d.sync_result,
        "synced_at": d.synced_at,
        "collection": coll,
    }


async def _collections(session: AsyncSession) -> dict[tuple[int | None, str], Any]:
    colls: dict[tuple[int | None, str], Any] = {}
    servers: dict[int, list[int]] = {}
    for cid, sid in (
        await session.execute(select(ServerCollection.collection_id, ServerCollection.server_id))
    ).all():
        if cid is not None:
            servers.setdefault(cid, []).append(sid)
    for cid, name, lib_id, norm in await session.execute(
        select(Collection.id, Collection.name, Collection.library_id, Collection.normalized_name)
    ):
        colls[(lib_id, norm)] = {"id": cid, "name": name, "servers": sorted(servers.get(cid, []))}
    return colls


@router.get("/definitions")
async def list_definitions(session: AsyncSession = Depends(get_session)):
    definitions = list(
        await session.scalars(select(CollectionDefinition).order_by(CollectionDefinition.name))
    )
    colls = await _collections(session)
    return [_definition_out(d, colls) for d in definitions]


class Builder(BaseModel):
    type: str
    value: str | int


class Options(BaseModel):
    sync_mode: Literal["append", "sync"] = "append"
    collection_order: Literal["release", "alpha", "custom"] = "release"
    sort_title: str | None = None
    summary: str | None = None


class DefinitionIn(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    library_id: int
    builders: list[Builder] = Field(min_length=1)
    options: Options = Options()
    enabled: bool = True


class DefinitionUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=500)
    library_id: int | None = None
    builders: list[Builder] | None = None
    options: Options | None = None
    enabled: bool | None = None


def _builders(raw: list[Builder]) -> list[dict[str, str]]:
    try:
        return [normalize_builder(b.type, b.value) for b in raw]
    except ListError as exc:
        raise HTTPException(422, str(exc)) from exc


def _options(options: Options, keep: dict[str, Any] | None = None) -> dict[str, Any]:
    """The options sent over the old ones; an empty value clears one. Kometa keys Coverarr
    doesn't edit (e.g. label) stay."""
    out = dict(keep or {})
    sent = options.model_dump(exclude_unset=keep is not None)
    for key, value in sent.items():
        if isinstance(value, str):
            value = value.strip()
        if value in (None, ""):
            out.pop(key, None)
        else:
            out[key] = value
    return out


async def _check_library(session: AsyncSession, library_id: int | None) -> None:
    if library_id is not None and await session.get(Library, library_id) is None:
        raise HTTPException(422, "Unknown library")


async def _check_name_free(
    session: AsyncSession, library_id: int | None, name: str, skip: int | None = None
) -> None:
    clash = await session.scalar(
        select(CollectionDefinition.id).where(
            CollectionDefinition.library_id == library_id,
            CollectionDefinition.name == name,
            CollectionDefinition.id != (skip or 0),
        )
    )
    if clash is not None:
        raise HTTPException(422, f"There's already a collection called {name!r} in that library")


@router.post("/definitions")
async def create_definition(body: DefinitionIn, session: AsyncSession = Depends(get_session)):
    name = body.name.strip()
    await _check_library(session, body.library_id)
    await _check_name_free(session, body.library_id, name)
    definition = CollectionDefinition(
        library_id=body.library_id,
        name=name,
        source="coverarr",
        builders=_builders(body.builders),
        options=_options(body.options),
        unsupported=[],
        enabled=body.enabled,
    )
    session.add(definition)
    await session.commit()
    return _definition_out(definition, await _collections(session))


@router.put("/definitions/{definition_id}")
async def update_definition(
    definition_id: int,
    body: DefinitionUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    definition = await session.get(CollectionDefinition, definition_id)
    if definition is None:
        raise HTTPException(404, "Definition not found")
    sent = body.model_fields_set
    old_library = definition.library_id
    new_library = body.library_id if "library_id" in sent else old_library
    name = body.name.strip() if body.name is not None else definition.name
    if "library_id" in sent:
        await _check_library(session, body.library_id)
    if new_library != old_library or name != definition.name:
        await _check_name_free(session, new_library, name, definition.id)
    rename_job = None
    if name != definition.name:
        # The collection on the servers follows the new name (it stays in its old library).
        coll = (
            await session.scalar(
                select(Collection).where(
                    Collection.library_id == old_library,
                    Collection.normalized_name == normalize_collection_name(definition.name),
                )
            )
            if old_library is not None and old_library == new_library
            else None
        )
        definition.name = name
        if coll is not None:
            rename_job = (
                await request.app.state.jobs.enqueue(
                    "collection_rename", {"collection_id": coll.id, "name": name}
                )
            ).id
    if new_library != old_library:
        definition.library_id = new_library
        definition.check_result = None
    if body.builders is not None:
        if not body.builders:
            raise HTTPException(422, "Add at least one list")
        definition.builders = _builders(body.builders)
        definition.check_result = None
    if body.options is not None:
        definition.options = _options(body.options, definition.options)
    if body.enabled is not None:
        definition.enabled = body.enabled
    await session.commit()
    return {**_definition_out(definition, await _collections(session)), "rename_job": rename_job}


@router.delete("/definitions/{definition_id}")
async def delete_definition(definition_id: int, session: AsyncSession = Depends(get_session)):
    """Forget the definition; the collection stays on the servers as it is."""
    definition = await session.get(CollectionDefinition, definition_id)
    if definition is None:
        raise HTTPException(404, "Definition not found")
    await session.delete(definition)
    await session.commit()
    return {"ok": True}


class IdsRequest(BaseModel):
    ids: list[int] | None = None


@router.post("/definitions/check")
async def check_definitions(body: IdsRequest, request: Request):
    job = await request.app.state.jobs.enqueue(
        "check_definitions", {"ids": body.ids} if body.ids else {}
    )
    return {"job_id": job.id}


@router.post("/definitions/sync")
async def sync_definitions(body: IdsRequest, request: Request):
    """Sync the given definitions now, or every one that's switched on."""
    job = await request.app.state.jobs.enqueue(
        "sync_definitions", {"ids": body.ids} if body.ids else {}
    )
    return {"job_id": job.id}
