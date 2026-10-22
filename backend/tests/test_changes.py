"""Pending poster changes and pushing them to the servers."""

from tests.conftest import run_job
from tests.fakeserver import poster_png
from tests.test_flow import add_servers, issues
from tests.test_kometa import _zip


async def _films(client) -> dict:
    libs = (await client.get("/api/libraries")).json()["libraries"]
    return next(lib for lib in libs if lib["kind"] == "movie")


async def _changes(client, **params) -> dict:
    resp = await client.get("/api/changes", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _push(app, client, **body) -> dict:
    resp = await client.post("/api/changes/push", json=body)
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    return job["result"]


async def _titles(client, library_id: int, filter_: str) -> list[str]:
    page = (
        await client.get("/api/items", params={"library_id": library_id, "issues": filter_})
    ).json()
    return [i["title"] for i in page["items"]]


async def test_push_server_poster(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")  # the import's own poster scan follows it
    films = await _films(client)
    plex, jf = fake_servers["plex"], fake_servers["jellyfin"]
    [arrival_issue] = await issues(client, "poster_differs")
    assert arrival_issue["title"] == "Arrival (2016)"
    assert (await _changes(client))["total"] == 0

    # Pick Jellyfin's poster: Plex (which has different artwork) now needs it. Arrival isn't
    # on Emby at all, and Jellyfin itself is the source.
    await client.post(
        "/api/review/actions",
        json={
            "action": "choose_poster",
            "issue_ids": [arrival_issue["id"]],
            "server_id": ids["jellyfin"],
        },
    )
    changes = await _changes(client)
    assert changes["total"] == 1 and changes["uploads"] == 1
    [change] = changes["items"]
    assert (change["title"], change["library"]) == ("Arrival (2016)", "Films")
    assert change["servers"] == [
        {
            "server_id": ids["plex"],
            "reason": "different poster",
            "current": change["servers"][0]["current"],
        }
    ]
    assert change["base"]["server_id"] == ids["jellyfin"]
    summary = (await client.get("/api/changes/summary")).json()
    assert summary == {
        "titles": 1,
        "collections": 0,
        "uploads": 1,
        "problems": 0,
        "backgrounds": 0,
        "background_uploads": 0,
        "metadata": 0,
        "metadata_uploads": 0,
    }
    # The library filters still find it, though it's no longer a review issue.
    assert await _titles(client, films["id"], "push") == ["Arrival"]
    assert await _titles(client, films["id"], "poster") == ["Arrival"]
    assert await issues(client, "poster_differs") == []

    result = await _push(app, client)
    assert (result["uploaded"], result["pushed"], result.get("failed", 0)) == (1, 1, 0)
    plex_arrival = plex.by_title("Arrival")
    assert plex_arrival.uploaded is not None
    assert "Overlay" not in plex_arrival.labels
    assert jf.by_title("Arrival").uploaded is None  # the source is left alone
    assert (await _changes(client))["total"] == 0

    # A re-import (and its poster scan) agrees: Plex now shows the base.
    await run_job(app, client, "import")
    assert (await _changes(client))["total"] == 0
    assert await issues(client, "poster_differs") == []

    # Someone changes the poster on Plex again: the change comes back.
    plex_arrival.uploaded, plex_arrival.alt_art = None, True
    plex_arrival.art_version += 1
    await run_job(app, client, "import")
    [change] = (await _changes(client))["items"]
    assert [s["server_id"] for s in change["servers"]] == [ids["plex"]]


async def test_push_kometa_originals(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    plex, jf, emby = fake_servers["plex"], fake_servers["jellyfin"], fake_servers["emby"]
    hot_fuzz = plex.by_title("Hot Fuzz")
    cornetto = next(c for c in plex.collections.values() if c.name == "Cornetto Collection")
    archive = _zip(
        {
            f"Films Original Posters/{hot_fuzz.id}.png": poster_png("movie:Hot Fuzz"),
            f"Films Original Posters/{cornetto.id}.png": poster_png("new cornetto art"),
        }
    )
    resp = await client.post(
        "/api/kometa/originals/upload",
        files={"file": ("originals.zip", archive, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()

    changes = {c["title"]: c for c in (await _changes(client))["items"]}
    # Jellyfin and Emby already show the clean original; Plex has Kometa's overlays on it.
    assert [(s["server_id"], s["reason"]) for s in changes["Hot Fuzz (2007)"]["servers"]] == [
        (ids["plex"], "has Kometa overlays")
    ]
    assert changes["Cornetto Collection"]["kind"] == "collection"
    # The collection only exists on Plex so far.
    assert [s["server_id"] for s in changes["Cornetto Collection"]["servers"]] == [ids["plex"]]
    assert [c["target"] for c in (await _changes(client, target="collection"))["items"]] == [
        "collection"
    ]

    # Push just the film first.
    film = changes["Hot Fuzz (2007)"]
    result = await _push(app, client, media_item_ids=[film["id"]])
    assert result["uploaded"] == 1
    assert hot_fuzz.uploaded is not None and "Overlay" not in hot_fuzz.labels
    assert jf.by_title("Hot Fuzz").uploaded is None
    assert [c["title"] for c in (await _changes(client))["items"]] == ["Cornetto Collection"]

    result = await _push(app, client, collection_ids=[changes["Cornetto Collection"]["id"]])
    assert result["uploaded"] == 1
    assert cornetto.uploaded is not None
    assert not any(
        c.name == "Cornetto Collection"
        for c in [*jf.collections.values(), *emby.collections.values()]
    )
    assert (await _changes(client))["total"] == 0


async def test_discard_change(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    [issue] = await issues(client, "poster_differs")
    await client.post(
        "/api/review/actions",
        json={"action": "choose_poster", "issue_ids": [issue["id"]], "server_id": ids["plex"]},
    )
    [change] = (await _changes(client))["items"]
    assert [s["server_id"] for s in change["servers"]] == [ids["jellyfin"]]

    assert (await client.post("/api/changes/discard", json={})).status_code == 422
    resp = await client.post("/api/changes/discard", json={"media_item_ids": [change["id"]]})
    assert resp.status_code == 200
    assert (await _changes(client))["total"] == 0
    assert [i["title"] for i in await issues(client, "poster_differs")] == ["Arrival (2016)"]
    item = (await client.get(f"/api/items/{change['id']}")).json()
    assert item["poster_source"] is None


async def test_overlays_without_kometa_label(app, authed, fake_servers):
    """Kometa's overlays are still on Plex but its "Overlay" label is gone (e.g. after a
    partial remove_overlays). The artwork matches Jellyfin's, but the edges don't."""
    client = authed
    ids = await add_servers(client)
    plex = fake_servers["plex"]
    for title in ("Hot Fuzz", "Elf"):
        plex.by_title(title).labels = []  # overlays stay drawn on
    plex.by_title("Elf").overlay = False  # Elf really is clean on Plex
    await run_job(app, client, "import")
    films = await _films(client)
    for title in ("Hot Fuzz", "Elf"):
        page = await client.get("/api/items", params={"library_id": films["id"], "q": title})
        item = next(i for i in page.json()["items"] if i["title"] == title)
        resp = await client.put(
            f"/api/items/{item['id']}/poster-source",
            json={"poster_source": f"server:{ids['jellyfin']}"},
        )
        assert resp.status_code == 200

    changes = {c["title"]: c for c in (await _changes(client))["items"]}
    assert "Elf (2003)" not in changes
    hot_fuzz = changes["Hot Fuzz (2007)"]
    assert [(s["server_id"], s["reason"]) for s in hot_fuzz["servers"]] == [
        (ids["plex"], "has overlays")
    ]
    await _push(app, client, media_item_ids=[hot_fuzz["id"]])
    assert plex.by_title("Hot Fuzz").uploaded is not None
    await run_job(app, client, "import")
    assert "Hot Fuzz (2007)" not in {c["title"] for c in (await _changes(client))["items"]}
