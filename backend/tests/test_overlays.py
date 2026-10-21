"""Overlays: the pack, per-library layers, rendering, and pushing overlaid posters."""

import io
import zipfile

from PIL import Image, ImageDraw

from tests.conftest import run_job
from tests.test_flow import add_servers


def _png(size, color=(0, 0, 0, 0), box=None, box_color=(0, 0, 0, 255)) -> bytes:
    img = Image.new("RGBA", size, color)
    if box:
        ImageDraw.Draw(img).rectangle(box, fill=box_color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _pack() -> bytes:
    """A tiny jmxd-shaped pack (inside a top-level folder, as the real zip is)."""
    files = {
        "gradient_top.png": _png((1000, 1500), box=(0, 0, 999, 120)),
        "gradient_bottom.png": _png((1000, 1500), box=(0, 1380, 999, 1499)),
        "gradient_episode_top.png": _png((1920, 1080), box=(0, 0, 1919, 100)),
        "gradient_episode_bottom.png": _png((1920, 1080), box=(0, 980, 1919, 1079)),
        "resolution/4kdvhdr.png": _png((300, 50), (255, 0, 0, 255)),
        "resolution/1080p.png": _png((150, 50), (0, 255, 0, 255)),
        "resolution/720p.png": _png((150, 50), (0, 0, 255, 255)),
        "resolution/4k.png": _png((150, 50), (255, 255, 0, 255)),
        "resolution/4khdr.png": _png((200, 50), (255, 0, 255, 255)),
        "edition/directors.png": _png((1000, 153), (255, 255, 255, 255)),
        "network-images/images/network/color/Channel 4.png": _png((100, 85), (0, 255, 255, 255)),
        "gradient.yml": b"overlays: {}\n",
        "../evil.png": _png((10, 10)),  # never written outside the pack
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(f"jmxd/{name}" if not name.startswith("..") else name, data)
        z.writestr("__MACOSX/jmxd/._gradient_top.png", b"junk")
    return buf.getvalue()


async def _setup(app, client):
    ids = await add_servers(client)
    await run_job(app, client, "import")
    overlays = (await client.get("/api/overlays")).json()
    assert overlays["pack"] is None and not overlays["needs_import"]
    films = next(lib for lib in overlays["libraries"] if lib["kind"] == "movie")
    tv = next(lib for lib in overlays["libraries"] if lib["kind"] == "show")
    assert films["enabled"] is False and list(films["levels"]) == ["movie"]
    assert list(tv["levels"]) == ["show", "season", "episode"]
    return ids, films, tv


async def _changes(client, **params) -> dict:
    return (await client.get("/api/changes", params={"page_size": 200, **params})).json()


async def _push(app, client, **body) -> dict:
    resp = await client.post("/api/changes/push", json=body)
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    return job["result"]


async def test_pack_upload(app, authed, fake_servers):
    client = authed
    await _setup(app, client)
    bad = await client.post(
        "/api/overlays/pack", files={"file": ("x.zip", b"not a zip", "application/zip")}
    )
    assert bad.status_code == 422
    resp = await client.post(
        "/api/overlays/pack", files={"file": ("jmxd.zip", _pack(), "application/zip")}
    )
    assert resp.status_code == 200, resp.text
    info = resp.json()
    # The shared "jmxd/" folder is stripped; the unsafe entry is refused.
    assert resp.status_code == 200 and info["name"] == "jmxd"
    assert "gradient_top.png" in info["top_files"]
    assert info["folders"]["resolution"] == 5
    root = app.state.config.config_dir
    assert (root / "assets/overlays/jmxd/resolution/4kdvhdr.png").is_file()
    assert not (root / "assets/overlays/evil.png").exists()
    assert not (root / "assets/evil.png").exists()
    assert (await client.get("/api/overlays")).json()["pack"]["name"] == "jmxd"


async def test_overlays_render_and_push(app, authed, fake_servers):
    client = authed
    ids, films, tv = await _setup(app, client)
    await client.post(
        "/api/overlays/pack", files={"file": ("jmxd.zip", _pack(), "application/zip")}
    )
    plex, jf = fake_servers["plex"], fake_servers["jellyfin"]

    # Nothing happens until a library is switched on.
    assert (await _changes(client))["total"] == 0
    levels = films["levels"]
    levels["movie"][3]["enabled"] = True  # the edition banner
    bad = await client.put(
        f"/api/overlays/libraries/{films['id']}",
        json={"enabled": True, "levels": {"movie": [{"type": "image", "file": "../x.png"}]}},
    )
    assert bad.status_code == 422
    saved = await client.put(
        f"/api/overlays/libraries/{films['id']}", json={"enabled": True, "levels": levels}
    )
    assert saved.status_code == 200, saved.text

    changes = {c["title"]: c for c in (await _changes(client))["items"]}
    arrival = changes["Arrival (2016)"]
    assert arrival["overlay"] and arrival["missing_overlays"] == []
    assert arrival["base"]["url"].startswith(f"/api/overlays/preview/item/{arrival['id']}")
    # Plex has Kometa's overlays; Jellyfin's clean poster is drawn on, and both get the result.
    assert sorted((s["server_id"], s["reason"]) for s in arrival["servers"]) == sorted(
        [(ids["plex"], "needs overlays"), (ids["jellyfin"], "needs overlays")]
    )
    elf = changes["Elf (2003)"]
    assert len(elf["servers"]) == 3

    preview = await client.get(arrival["base"]["url"] + "&w=200")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(preview.content))
    assert img.size == (200, 300)
    live = await client.post(
        "/api/overlays/preview",
        json={"media_item_id": arrival["id"], "layers": levels["movie"], "width": 300},
    )
    assert live.status_code == 200
    # The 4K DV HDR badge (red, top-left at 50,50) is drawn.
    big = Image.open(io.BytesIO(live.content)).convert("RGB").resize((1000, 1500))
    r, g, b = big.getpixel((80, 70))
    assert r > 200 and g < 60 and b < 60

    # "9 Songs" only exists on Plex, which has Kometa's overlays: nothing clean to draw on.
    assert changes["9 Songs (2004)"]["problem"].startswith("No clean poster")
    pushable = [c for c in changes.values() if not c["problem"]]
    result = await _push(app, client)
    assert result.get("failed", 0) == 0
    assert result["rendered"] == result["pushed"] == len(pushable)
    assert result["originals_kept"] == len(pushable)
    assert [c["title"] for c in (await _changes(client))["items"]] == ["9 Songs (2004)"]
    plex_arrival = plex.by_title("Arrival")
    assert plex_arrival.uploaded is not None and "Overlay" not in plex_arrival.labels
    uploaded = Image.open(io.BytesIO(plex_arrival.uploaded)).convert("RGB")
    assert uploaded.size == (1000, 1500)
    assert uploaded.getpixel((80, 70))[0] > 200
    # The clean original is kept and is now the base.
    detail = (await client.get(f"/api/items/{arrival['id']}")).json()
    assert [a["source"] for a in detail["artwork"]] == ["original"]
    assert detail["poster_source"] == f"artwork:{detail['artwork'][0]['artwork_id']}"

    # A re-import (and its poster scan) doesn't make anything pending again.
    await run_job(app, client, "import")
    assert [c["title"] for c in (await _changes(client))["items"]] == ["9 Songs (2004)"]

    # Radarr upgrades a file: its badge changes, so just that title needs a new render.
    plex_arrival.video = {"resolution": "1080"}
    plex_arrival.file = plex_arrival.file.replace("Bluray-2160p][DV HDR10", "WEBDL-1080p")
    for srv in (jf,):
        srv.by_title("Arrival").file = plex_arrival.file
    await run_job(app, client, "import")
    [change] = [c for c in (await _changes(client))["items"] if not c["problem"]]
    assert change["title"] == "Arrival (2016)"
    assert {s["reason"] for s in change["servers"]} == {"overlays out of date"}
    result = await _push(app, client)
    assert result["rendered"] == 1 and result.get("originals_kept", 0) == 0
    uploaded = Image.open(io.BytesIO(plex.by_title("Arrival").uploaded)).convert("RGB")
    r, g, b = uploaded.getpixel((80, 70))
    assert g > 200 and r < 60  # the green 1080p badge now

    # Switching overlays off puts the clean posters back.
    await client.put(
        f"/api/overlays/libraries/{films['id']}", json={"enabled": False, "levels": levels}
    )
    changes = (await _changes(client))["items"]
    assert changes and all(
        s["reason"] == "has Coverarr overlays" for c in changes for s in c["servers"]
    )
    await _push(app, client)
    assert (await _changes(client))["total"] == 0  # 9 Songs had no base chosen: not a target
    restored = Image.open(io.BytesIO(plex.by_title("Arrival").uploaded)).convert("RGB")
    assert restored.getpixel((80, 70))[0] < 200


async def test_tv_levels(app, authed, fake_servers):
    client = authed
    ids, films, tv = await _setup(app, client)
    await client.post(
        "/api/overlays/pack", files={"file": ("jmxd.zip", _pack(), "application/zip")}
    )
    levels = tv["levels"]
    # Shows and seasons only, to keep the test small.
    levels["episode"] = [dict(layer, enabled=False) for layer in levels["episode"]]
    await client.put(
        f"/api/overlays/libraries/{tv['id']}", json={"enabled": True, "levels": levels}
    )
    items = (await _changes(client))["items"]
    kinds = sorted({c["kind"] for c in items})
    assert kinds == ["season", "show"]
    spaced = next(c for c in items if c["title"] == "Spaced (1999)")
    assert spaced["missing_overlays"] == []

    samples = (
        await client.get(
            "/api/overlays/samples", params={"library_id": tv["id"], "level": "episode"}
        )
    ).json()
    # Spaced series 2 is 4K: those come first.
    assert samples[0]["title"].startswith("Spaced S02")
    # The show gets the network logo (cyan) bottom-left and its best episode resolution (4K).
    live = await client.post(
        "/api/overlays/preview",
        json={"media_item_id": spaced["id"], "layers": levels["show"], "width": 1000},
    )
    img = Image.open(io.BytesIO(live.content)).convert("RGB")
    r, g, b = img.getpixel((90, 1500 - 50 - 40))
    assert r < 60 and g > 200 and b > 200
    r, g, b = img.getpixel((60, 70))
    assert r > 200 and g > 200 and b < 60  # yellow "4k"

    result = await _push(app, client)
    assert result.get("failed", 0) == 0 and result["rendered"] == len(items)
    assert (await _changes(client))["total"] == 0
