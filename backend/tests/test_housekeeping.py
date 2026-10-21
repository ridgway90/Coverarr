"""Housekeeping: suggested bases, storing them, tidying assets, force push, server clean-up."""

import shutil

from tests.conftest import run_job
from tests.fakeserver import poster_png
from tests.test_flow import add_servers


async def _wait(app, client, resp) -> dict:
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    return job["result"]


async def _bases(client, **params) -> dict:
    resp = await client.get("/api/housekeeping/bases", params={"page_size": 200, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_suggested_bases(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    plex = fake_servers["plex"]
    plex.by_title("Elf").labels = []  # Kometa's overlays still drawn, label gone
    await run_job(app, client, "import")

    data = await _bases(client)
    by_title = {s["title"]: s for s in data["items"]}
    # Plex has Kometa's overlays; Jellyfin and Emby agree on the clean poster.
    hot_fuzz = by_title["Hot Fuzz (2007)"]
    assert hot_fuzz["confidence"] == "high"
    assert hot_fuzz["suggested_server_id"] == ids["jellyfin"]
    assert "All 3 servers show the same artwork" in hot_fuzz["reasons"]
    assert "Skipped a poster with Kometa's overlays" in hot_fuzz["reasons"]
    plex_candidate = next(c for c in hot_fuzz["candidates"] if c["server_id"] == ids["plex"])
    assert plex_candidate["overlaid"] is True
    # No label, but the darker edges give the overlays away.
    elf = by_title["Elf (2003)"]
    assert elf["suggested_server_id"] != ids["plex"] and elf["confidence"] == "high"
    assert any("darker top/bottom" in r for r in elf["reasons"])
    # Plex and Jellyfin have different artwork for Arrival (and Emby hasn't got it).
    arrival = by_title["Arrival (2016)"]
    assert arrival["confidence"] == "low" and arrival["suggested_server_id"] == ids["jellyfin"]
    # Seasons and collections are covered too.
    assert by_title["Spaced: Season 1"]["confidence"] == "high"
    cornetto = by_title["Cornetto Collection"]
    assert cornetto["kind"] == "collection" and cornetto["confidence"] == "medium"
    assert data["counts"]["high"] >= 10

    seasons = await _bases(client, kind="season")
    assert {s["kind"] for s in seasons["items"]} == {"season"}
    low = await _bases(client, confidence="low")
    assert [s["title"] for s in low["items"]] == ["Arrival (2016)"]

    summary = (await client.get("/api/housekeeping/summary")).json()
    films_and_shows = [s for s in data["items"] if s["kind"] in ("movie", "show")]
    assert summary["coverage"]["titles"] == {
        "stored": 0,
        "server": 0,
        "none": len(films_and_shows),
    }
    assert summary["without_base"]["low"] == 1


async def test_accept_and_store(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    films = (await client.get("/api/libraries")).json()["libraries"]
    film_lib = next(lib for lib in films if lib["kind"] == "movie")

    # One by hand (Arrival: take Plex's), then every high-confidence film.
    arrival = next(s for s in (await _bases(client))["items"] if s["title"] == "Arrival (2016)")
    result = await _wait(
        app,
        client,
        await client.post(
            "/api/housekeeping/bases/accept",
            json={"choices": [{"target": "item", "id": arrival["id"], "server_id": ids["plex"]}]},
        ),
    )
    assert result["stored"] == 1
    detail = (await client.get(f"/api/items/{arrival['id']}")).json()
    assert detail["poster_source"].startswith("artwork:")
    assert [a["source"] for a in detail["artwork"]] == ["original"]

    resp = await client.post(
        "/api/housekeeping/bases/accept",
        json={"confidence": "high", "library_id": film_lib["id"], "kind": "movie"},
    )
    result = await _wait(app, client, resp)
    assert result["stored"] >= 8 and result.get("failed", 0) == 0
    left = await _bases(client, kind="movie")
    assert {s["confidence"] for s in left["items"]} <= {"medium", "low", "none"}
    coverage = (await client.get("/api/housekeeping/summary")).json()["coverage"]
    assert coverage["titles"]["stored"] >= 9

    # Stored bases from Jellyfin: Plex (with Kometa's overlays) still needs them.
    changes = (await client.get("/api/changes", params={"page_size": 200})).json()
    assert changes["total"] >= 8
    # Unknown titles are skipped; nothing to accept is an error.
    gone = await client.post(
        "/api/housekeeping/bases/accept",
        json={"choices": [{"target": "item", "id": 999999, "server_id": 1}], "store": False},
    )
    assert gone.json() == {"accepted": 0, "job_id": None}
    assert (await client.post("/api/housekeeping/bases/accept", json={})).status_code == 422


async def test_force_push(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    films = next(
        lib
        for lib in (await client.get("/api/libraries")).json()["libraries"]
        if lib["kind"] == "movie"
    )
    hot_fuzz = next(s for s in (await _bases(client))["items"] if s["title"] == "Hot Fuzz (2007)")
    await _wait(
        app,
        client,
        await client.post(
            "/api/housekeeping/bases/accept",
            json={
                "choices": [{"target": "item", "id": hot_fuzz["id"], "server_id": ids["jellyfin"]}]
            },
        ),
    )
    await _wait(app, client, await client.post("/api/changes/push", json={}))
    assert (await client.get("/api/changes")).json()["total"] == 0

    # Everything looks pushed, but force sends it to every server again.
    result = await _wait(
        app,
        client,
        await client.post("/api/changes/push", json={"force": True, "library_id": films["id"]}),
    )
    assert result["uploaded"] == 3 and result.get("failed", 0) == 0
    assert fake_servers["jellyfin"].by_title("Hot Fuzz").uploaded is not None


async def test_tidy_assets(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films = next(
        lib
        for lib in (await client.get("/api/libraries")).json()["libraries"]
        if lib["kind"] == "movie"
    )
    page = (
        await client.get("/api/items", params={"library_id": films["id"], "page_size": 100})
    ).json()
    items = {i["title"]: i for i in page["items"]}
    config = app.state.config.config_dir
    # Two uploads (the second replaces the first), a poster set copy of the same image, ...
    for title in ("Elf", "Paddington"):
        await client.post(
            f"/api/items/{items[title]['id']}/artwork",
            files={"file": ("p.png", poster_png(f"mine {title}"), "image/png")},
        )
    from coverarr.db import session_factory
    from coverarr.models import Artwork

    async with session_factory()() as session:
        from sqlalchemy import select

        elf_art = await session.scalar(
            select(Artwork).where(Artwork.media_item_id == items["Elf"]["id"])
        )
        pad_art = await session.scalar(
            select(Artwork).where(Artwork.media_item_id == items["Paddington"]["id"])
        )
        # ... a duplicate of Elf's upload stored as a poster set image,
        dup_rel = elf_art.file_path.replace("poster-upload", "poster-set")
        shutil.copy(config / elf_art.file_path, config / dup_rel)
        session.add(Artwork(media_item_id=items["Elf"]["id"], source="set", file_path=dup_rel))
        # ... a record whose file has gone,
        session.add(
            Artwork(
                media_item_id=items["Home Alone"]["id"],
                source="upload",
                file_path="assets/movies/Home Alone (1990) {tmdb-771}/poster-upload.png",
            )
        )
        # ... and Paddington's file under the wrong name.
        wrong = "assets/movies/somewhere/odd-name.png"
        (config / wrong).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(config / pad_art.file_path, config / wrong)
        pad_art.file_path = wrong
        await session.commit()
    # An old poster nothing refers to.
    orphan = config / "assets/movies/Old Film (1999)/poster-kometa.jpg"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"old")

    check = await _wait(app, client, await client.post("/api/housekeeping/tidy", json={}))
    assert check["applied"] is False
    assert check["counts"] == {"missing_files": 1, "duplicates": 1, "renamed": 1, "unused_files": 1}
    assert orphan.exists() and (config / dup_rel).exists()  # a check changes nothing

    fixed = await _wait(
        app, client, await client.post("/api/housekeeping/tidy", json={"apply": True})
    )
    assert fixed["counts"]["unused_files"] == 1 and fixed["counts"]["duplicates"] == 1
    assert not orphan.exists() and not orphan.parent.exists()
    assert not (config / dup_rel).exists()
    elf = (await client.get(f"/api/items/{items['Elf']['id']}")).json()
    assert [a["source"] for a in elf["artwork"]] == ["upload"]  # the chosen one is kept
    again = await _wait(app, client, await client.post("/api/housekeeping/tidy", json={}))
    assert again["counts"] == {}, again["examples"]


async def test_plex_maintenance(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    for action in ("clean_bundles", "optimize", "empty_trash"):
        resp = await client.post(
            f"/api/housekeeping/servers/{ids['plex']}/maintenance", json={"action": action}
        )
        assert resp.status_code == 200, resp.text
    assert fake_servers["plex"].maintenance == [
        "clean_bundles",
        "optimize",
        "empty_trash:1",
        "empty_trash:2",
    ]
    resp = await client.post(
        f"/api/housekeeping/servers/{ids['jellyfin']}/maintenance", json={"action": "optimize"}
    )
    assert resp.status_code == 422
