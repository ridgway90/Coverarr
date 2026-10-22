"""Backgrounds (fanart): chosen once, pushed to every server, kept in sync."""

import io

from PIL import Image
from sqlalchemy import select

from coverarr.db import session_factory
from coverarr.models import ServerItem
from tests.conftest import run_job
from tests.test_flow import add_servers


def _jpeg(color: str, size=(1280, 720)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


async def _title(client, title: str) -> dict:
    libs = (await client.get("/api/libraries")).json()["libraries"]
    films = next(lib for lib in libs if lib["name"] == "Films")
    items = (await client.get("/api/items", params={"library_id": films["id"], "q": title})).json()[
        "items"
    ]
    return next(i for i in items if i["title"] == title)


async def _push(app, client, **body) -> dict:
    resp = await client.post("/api/changes/push", json=body)
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    return job["result"]


def _colour(data: bytes) -> tuple[int, int, int]:
    return Image.open(io.BytesIO(data)).convert("RGB").getpixel((10, 10))


async def test_import_reads_backgrounds(app, authed, fake_servers):
    ids = await add_servers(authed)
    await run_job(app, authed, "import")
    async with session_factory()() as session:
        rows = (await session.execute(select(ServerItem.kind, ServerItem.background_ref))).all()
    assert all(ref for kind, ref in rows if kind in ("movie", "show"))
    assert not any(ref for kind, ref in rows if kind in ("season", "episode"))
    assert ids  # all three servers


async def test_choose_upload_and_push_backgrounds(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    fuzz = await _title(client, "Hot Fuzz")

    info = (await client.get(f"/api/items/{fuzz['id']}/backgrounds")).json()
    assert info["chosen"] is None and info["stored"] == []
    shown = [s for s in info["servers"] if s["url"]]
    assert {s["server_id"] for s in shown} == set(ids.values())
    assert (await client.get(shown[0]["url"])).status_code == 200

    # Jellyfin's background everywhere.
    resp = await client.put(
        f"/api/items/{fuzz['id']}/background", json={"source": f"server:{ids['jellyfin']}"}
    )
    assert resp.status_code == 200, resp.text
    pending = (await client.get("/api/changes/backgrounds")).json()
    assert pending["total"] == 1 and pending["uploads"] == 2
    assert {s["server_id"] for s in pending["items"][0]["servers"]} == {
        ids["plex"],
        ids["emby"],
    }
    summary = (await client.get("/api/changes/summary")).json()
    assert summary["backgrounds"] == 1 and summary["background_uploads"] == 2

    result = await _push(app, client)
    assert result["backgrounds"] == 2 and result["background_titles"] == 1
    jellyfin_bg = fake_servers["jellyfin"].background(
        fake_servers["jellyfin"].by_title("Hot Fuzz").id, None
    )
    for kind in ("plex", "emby"):
        srv = fake_servers[kind]
        uploaded = srv.items[srv.by_title("Hot Fuzz").id].bg_uploaded
        assert uploaded is not None and _colour(uploaded) == _colour(jellyfin_bg)
    assert (await client.get("/api/changes/backgrounds")).json()["total"] == 0
    # A fresh import reads the refs the servers now report: still nothing to do.
    await run_job(app, client, "import")
    assert (await client.get("/api/changes/backgrounds")).json()["total"] == 0

    # Your own background replaces it everywhere, Jellyfin included.
    resp = await client.post(
        f"/api/items/{fuzz['id']}/background/upload",
        files={"file": ("wide.jpg", _jpeg("#22AA44"), "image/jpeg")},
    )
    assert resp.status_code == 200, resp.text
    stored = resp.json()
    assert (await client.get(stored["url"])).status_code == 200
    info = (await client.get(f"/api/items/{fuzz['id']}/backgrounds")).json()
    assert info["chosen"] == f"background:{stored['id']}"
    assert {s["server_id"] for s in info["servers"] if s["pending"]} == set(ids.values())
    result = await _push(app, client, media_item_ids=[fuzz["id"]])
    assert result["backgrounds"] == 3
    for srv in fake_servers.values():
        uploaded = srv.items[srv.by_title("Hot Fuzz").id].bg_uploaded
        r, g, b = _colour(uploaded)
        assert g > 150 and r < 80 and b < 100

    # Someone changes Plex's background: it's pushed again.
    plex = fake_servers["plex"]
    plex.items[plex.by_title("Hot Fuzz").id].bg_version += 1
    await run_job(app, client, "import")
    pending = (await client.get("/api/changes/backgrounds")).json()
    assert [s["reason"] for s in pending["items"][0]["servers"]] == ["changed on the server"]

    # The tidy-up keeps background files.
    await client.post("/api/housekeeping/tidy", json={"apply": True})
    await app.state.jobs.wait_idle()
    assert (await client.get(stored["url"] + "?w=200")).status_code == 200

    # Removing the background stops managing it.
    assert (await client.delete(f"/api/backgrounds/{stored['id']}")).status_code == 200
    info = (await client.get(f"/api/items/{fuzz['id']}/backgrounds")).json()
    assert info["chosen"] is None and info["stored"] == []
    assert (await client.get("/api/changes/backgrounds")).json()["total"] == 0


async def test_background_choices_are_checked(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    fuzz = await _title(client, "Hot Fuzz")
    for source in ("server:999", "background:5", "nonsense"):
        resp = await client.put(f"/api/items/{fuzz['id']}/background", json={"source": source})
        assert resp.status_code == 422, source
    resp = await client.post(
        f"/api/items/{fuzz['id']}/background/upload",
        files={"file": ("x.txt", b"not an image", "text/plain")},
    )
    assert resp.status_code == 422
