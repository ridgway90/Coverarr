from __future__ import annotations

import httpx
import pytest

import coverarr.api.servers
import coverarr.clients
from coverarr.config import Settings
from coverarr.main import create_app
from tests.fakeserver import create_fake_app, default_servers

FAKE_BASE = "http://fake.test"
SERVER_CONFIGS = [
    {"kind": "plex", "name": "Plex", "url": f"{FAKE_BASE}/plex", "token": "plex-token"},
    {"kind": "jellyfin", "name": "Jellyfin", "url": f"{FAKE_BASE}/jellyfin", "token": "jf-key"},
    {"kind": "emby", "name": "Emby", "url": f"{FAKE_BASE}/embysrv", "token": "emby-key"},
]


@pytest.fixture
def fake_servers(monkeypatch):
    """Route every media-server client to an in-process fake."""
    servers = default_servers()
    fake_app = create_fake_app(servers)
    original = coverarr.clients.make_client

    def make_client(kind, url, token, *, verify_ssl=True, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=fake_app)
        return original(kind, url, token, verify_ssl=verify_ssl, **kwargs)

    monkeypatch.setattr(coverarr.clients, "make_client", make_client)
    monkeypatch.setattr(coverarr.api.servers, "make_client", make_client)
    return servers


@pytest.fixture
async def app(tmp_path):
    application = create_app(Settings(config_dir=tmp_path, static_dir=tmp_path / "nostatic"))
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://coverarr.test") as c:
        yield c


@pytest.fixture
async def authed(client):
    resp = await client.post(
        "/api/auth/setup", json={"username": "admin", "password": "password123"}
    )
    assert resp.status_code == 200, resp.text
    return client


async def run_job(app, client, kind: str, **extra) -> dict:
    resp = await client.post("/api/jobs", json={"kind": kind, **extra})
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['id']}")).json()
    assert job["status"] == "succeeded", job
    return job


async def wait_jobs(app) -> None:
    await app.state.jobs.wait_idle()
