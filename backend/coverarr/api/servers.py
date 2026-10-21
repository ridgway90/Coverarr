from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import MediaServerError, make_client
from coverarr.db import get_session
from coverarr.models import Server, utcnow

router = APIRouter(prefix="/api/servers", tags=["servers"])

ServerKind = Literal["plex", "jellyfin", "emby"]


class ServerIn(BaseModel):
    kind: ServerKind
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=500)
    token: str | None = Field(default=None, max_length=500)
    verify_ssl: bool = True
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class TestRequest(ServerIn):
    id: int | None = None  # test with the stored token when editing without re-entering it


def server_out(s: Server) -> dict:
    return {
        "id": s.id,
        "kind": s.kind,
        "name": s.name,
        "url": s.url,
        "has_token": bool(s.token),
        "verify_ssl": s.verify_ssl,
        "enabled": s.enabled,
        "version": s.version,
        "last_ok_at": s.last_ok_at,
        "last_error": s.last_error,
    }


async def _test(kind: str, url: str, token: str, verify_ssl: bool) -> dict:
    async with make_client(kind, url, token, verify_ssl=verify_ssl, timeout=15) as client:
        info = await client.server_info()
        libs = await client.libraries()
    return {
        "ok": True,
        "name": info.name,
        "version": info.version,
        "remote_id": info.remote_id,
        "libraries": [{"id": lib.id, "name": lib.name, "kind": lib.kind} for lib in libs],
    }


@router.get("")
async def list_servers(session: AsyncSession = Depends(get_session)):
    servers = await session.scalars(select(Server).order_by(Server.id))
    return [server_out(s) for s in servers]


@router.post("/test")
async def test_server(body: TestRequest, session: AsyncSession = Depends(get_session)):
    token = body.token
    if not token and body.id:
        stored = await session.get(Server, body.id)
        # The saved token only goes to the saved server, never to a newly typed address.
        if stored and body.url.rstrip("/") == stored.url.rstrip("/"):
            token = stored.token
        elif stored:
            raise HTTPException(422, "Enter the token again to test a different address")
    if not token:
        raise HTTPException(422, "A token / API key is required")
    try:
        return await _test(body.kind, body.url, token, body.verify_ssl)
    except MediaServerError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("")
async def create_server(body: ServerIn, session: AsyncSession = Depends(get_session)):
    if not body.token:
        raise HTTPException(422, "A token / API key is required")
    server = Server(**body.model_dump())
    session.add(server)
    await session.commit()
    return server_out(server)


@router.put("/{server_id}")
async def update_server(
    server_id: int, body: ServerIn, request: Request, session: AsyncSession = Depends(get_session)
):
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(404, "Server not found")
    if body.kind != server.kind:
        raise HTTPException(422, "Server type can't be changed; add a new server instead")
    data = body.model_dump()
    if token := data.pop("token"):  # blank means keep the stored token
        server.token = token
    for key, value in data.items():
        setattr(server, key, value)
    server.last_error = None
    await session.commit()
    await request.app.state.client_pool.drop(server.id)
    return server_out(server)


@router.delete("/{server_id}")
async def delete_server(
    server_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(404, "Server not found")
    await session.delete(server)
    await session.commit()
    await request.app.state.client_pool.drop(server_id)
    await request.app.state.jobs.enqueue("rematch")
    return {"ok": True}


@router.post("/{server_id}/test")
async def test_saved_server(server_id: int, session: AsyncSession = Depends(get_session)):
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(404, "Server not found")
    try:
        result = await _test(server.kind, server.url, server.token, server.verify_ssl)
        server.last_ok_at, server.last_error = utcnow(), None
        server.version = result["version"]
    except MediaServerError as exc:
        server.last_error = str(exc)
        result = {"ok": False, "error": str(exc)}
    await session.commit()
    return result
