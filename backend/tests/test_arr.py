"""Radarr/Sonarr webhooks: scan, find the title on each server, give it a base and push it."""

import asyncio

import httpx
import respx

from coverarr.services import arr
from tests.conftest import run_job
from tests.fakeserver import MOUNTS, Item
from tests.test_flow import add_servers

KEY = "0123456789abcdef0123456789abcdef"


async def _setup(app, client, monkeypatch, **automation) -> str:
    monkeypatch.setattr(arr.ArrQueue, "debounce", 0.0)
    monkeypatch.setattr(arr, "RETRY_DELAYS", (0, 0))
    await add_servers(client)
    await run_job(app, client, "import")
    body = {"wait_seconds": 0, "new_base": "none", **automation}
    resp = await client.put("/api/automation", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["webhook_path"]


async def _settle(app) -> None:
    """Let the webhook's timers fire and every job they queue finish."""
    for _ in range(200):
        await asyncio.sleep(0.01)
        await app.state.jobs.wait_idle()
        q = arr.queue
        if not (q.pending or q._timers or q._tasks):
            break
    await app.state.jobs.wait_idle()


async def _jobs(client, kind: str) -> list[dict]:
    return [j for j in (await client.get("/api/jobs?limit=100")).json() if j["kind"] == kind]


def _new_movie(srv, title: str, year: int, tmdb: str) -> Item:
    name = f"{title} ({year})"
    movies = srv.libraries[0][0]
    item = Item(
        srv.new_id(),
        "movie",
        title,
        movies,
        year,
        {"tmdb": tmdb},
        art_seed=f"movie:{title}",
        file=f"{MOUNTS[srv.kind]}/Movies/{name}/{name} [WEBDL-1080p].mkv",
        video={"resolution": "1080"},
    )
    srv.unscanned.append(item)
    return item


def _radarr(title: str, year: int, tmdb: str, file: str, **extra) -> dict:
    name = f"{title} ({year})"
    return {
        "eventType": "Download",
        "movie": {
            "title": title,
            "year": year,
            "tmdbId": int(tmdb),
            "folderPath": f"/movies/{name}",
        },
        "movieFile": {"path": f"/movies/{name}/{file}"},
        **extra,
    }


async def test_webhook_token_and_events(app, authed, fake_servers, monkeypatch):
    client = authed
    path = await _setup(app, client, monkeypatch)
    assert (
        await client.post("/api/webhooks/arr/wrong", json={"eventType": "Test"})
    ).status_code == 401
    # No login needed: Radarr/Sonarr only know the URL.
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c.test")
    async with anon:
        assert (await anon.post(path, json={"eventType": "Test"})).json()["test"] is True
        assert (await anon.post(path, json={"eventType": "Grab"})).json()["ignored"] == "Grab"
        assert (await anon.get("/api/automation")).status_code == 401
    old = path
    path = (await client.post("/api/automation/webhook-token")).json()["webhook_path"]
    assert path != old
    assert (await client.post(old, json={"eventType": "Test"})).status_code == 401


async def test_new_movie_gets_tmdb_base_and_is_pushed(app, authed, fake_servers, monkeypatch):
    client = authed
    path = await _setup(app, client, monkeypatch, new_base="tmdb")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    added = {kind: _new_movie(srv, "Hot Rod", 2007, "10074") for kind, srv in fake_servers.items()}

    with respx.mock(assert_all_called=True) as mock:
        mock.get(url__regex=r"https://api\.themoviedb\.org/3/movie/10074").respond(
            json={"poster_path": "/hotrod.jpg"}
        )
        from tests.fakeserver import poster_png

        mock.get("https://image.tmdb.org/t/p/original/hotrod.jpg").respond(
            content=poster_png("tmdb hot rod"), headers={"Content-Type": "image/png"}
        )
        resp = await client.post(
            path, json=_radarr("Hot Rod", 2007, "10074", "Hot Rod (2007) [WEBDL-1080p].mkv")
        )
        assert resp.json()["title"] == "Hot Rod (2007)"
        await _settle(app)

    for kind, srv in fake_servers.items():
        assert any(p.endswith("/Movies/Hot Rod (2007)") for p in srv.scans), kind
        assert added[kind].id in srv.items
        assert srv.items[added[kind].id].uploaded is not None, kind  # base pushed everywhere
    events = await _jobs(client, "arr_event")
    assert [j["result"]["status"] for j in reversed(events)] == ["scanning", "done"]
    assert events[0]["result"]["new_bases"] == 1
    push = (await _jobs(client, "push_posters"))[0]
    assert push["status"] == "succeeded" and push["result"]["uploaded"] == 3


async def test_server_that_never_scans_is_retried_then_skipped(
    app, authed, fake_servers, monkeypatch
):
    client = authed
    path = await _setup(app, client, monkeypatch)
    for kind in ("plex", "jellyfin"):  # Emby never gets the file
        _new_movie(fake_servers[kind], "Hot Rod", 2007, "10074")
    await client.post(
        path, json=_radarr("Hot Rod", 2007, "10074", "Hot Rod (2007) [WEBDL-1080p].mkv")
    )
    await _settle(app)

    statuses = [j["result"]["status"] for j in reversed(await _jobs(client, "arr_event"))]
    assert statuses == ["scanning", "waiting", "waiting", "done"]
    final = (await _jobs(client, "arr_event"))[0]
    assert final["result"]["waiting"] == ["Emby"]
    libs = (await client.get("/api/libraries")).json()["libraries"]
    films = next(lib for lib in libs if lib["kind"] == "movie")
    page = (
        await client.get("/api/items", params={"library_id": films["id"], "q": "Hot Rod"})
    ).json()
    detail = (await client.get(f"/api/items/{page['items'][0]['id']}")).json()
    assert len(detail["servers"]) == 2


async def test_upgrade_refreshes_media_info(app, authed, fake_servers, monkeypatch):
    client = authed
    path = await _setup(app, client, monkeypatch)
    for srv in fake_servers.values():
        fuzz = srv.by_title("Hot Fuzz")
        fuzz.file = fuzz.file.replace("WEBDL-1080p", "Bluray-2160p")
        fuzz.video = {"resolution": "4k", "hdr": True}
    await client.post(
        path,
        json=_radarr(
            "Hot Fuzz",
            2007,
            "4638",
            "Hot Fuzz (2007) {tmdb-4638} [Bluray-2160p].mkv",
            isUpgrade=True,
        ),
    )
    await _settle(app)
    libs = (await client.get("/api/libraries")).json()["libraries"]
    films = next(lib for lib in libs if lib["kind"] == "movie")
    page = (
        await client.get("/api/items", params={"library_id": films["id"], "q": "Hot Fuzz"})
    ).json()
    detail = (await client.get(f"/api/items/{page['items'][0]['id']}")).json()
    assert detail["facts"]["resolution"] == "4k"
    assert detail["facts"].get("hdr") is True  # from Plex's stream details
    push = (await _jobs(client, "push_posters"))[0]
    assert push["params"]["media_item_ids"] == [detail["id"]]


async def test_season_pack_is_one_job(app, authed, fake_servers, monkeypatch):
    client = authed
    path = await _setup(app, client, monkeypatch)
    monkeypatch.setattr(arr.ArrQueue, "debounce", 0.2)
    for srv in fake_servers.values():
        show = srv.by_title("Spaced")
        tv = show.library
        season = Item(srv.new_id(), "season", "Season 3", tv, parent=show.id, show=show.id, index=3)
        srv.unscanned.append(season)
        for e in (1, 2):
            srv.unscanned.append(
                Item(
                    srv.new_id(),
                    "episode",
                    f"Spaced 3x{e}",
                    tv,
                    parent=season.id,
                    show=show.id,
                    index=e,
                    parent_index=3,
                    file=f"{show.file}/Season 03/Spaced - S03E{e:02d}.mkv",
                )
            )
    series = {
        "title": "Spaced",
        "year": 1999,
        "tvdbId": 75592,
        "path": "/tv/Spaced (1999) {tvdb-75592}",
    }
    replies = []
    for e in (1, 2):
        payload = {
            "eventType": "Download",
            "series": series,
            "episodes": [{"seasonNumber": 3, "episodeNumber": e}],
            "episodeFile": {
                "path": f"/tv/Spaced (1999) {{tvdb-75592}}/Season 03/Spaced - S03E{e:02d}.mkv"
            },
        }
        replies.append((await client.post(path, json=payload)).json())
    assert [r["merged"] for r in replies] == [False, True]
    await _settle(app)

    statuses = [j["result"]["status"] for j in reversed(await _jobs(client, "arr_event"))]
    assert statuses == ["scanning", "done"]
    libs = (await client.get("/api/libraries")).json()["libraries"]
    tv = next(lib for lib in libs if lib["kind"] == "show")
    page = (await client.get("/api/items", params={"library_id": tv["id"], "q": "Spaced"})).json()
    detail = (await client.get(f"/api/items/{page['items'][0]['id']}")).json()
    assert 3 in [s["season_number"] for s in detail["seasons"]]


def test_weekly_slots():
    from datetime import datetime

    from coverarr.services.schedule import due_since, next_run

    wed = datetime(2026, 10, 21, 12, 0)  # a Wednesday
    assert due_since(wed, 6, 4) == datetime(2026, 10, 18, 4, 0)  # last Sunday 04:00
    assert due_since(wed, 2, 4) == datetime(2026, 10, 21, 4, 0)  # this morning
    assert due_since(wed, 2, 13) == datetime(2026, 10, 14, 13, 0)  # later today: last week
    assert next_run(wed, 6, 4) == datetime(2026, 10, 25, 4, 0)


async def test_weekly_scan(app, authed, fake_servers, monkeypatch):
    from datetime import datetime, timedelta

    client = authed
    await _setup(app, client, monkeypatch)
    settings = (await client.get("/api/automation")).json()
    weekly = {"enabled": True, "weekday": datetime.now().weekday(), "hour": 0}
    resp = await client.put("/api/automation", json={**settings, "weekly_scan": weekly})
    assert resp.json()["weekly_next_run"]
    scheduler = app.state.scheduler
    assert await scheduler.tick() is False  # switching it on doesn't scan straight away

    # Emby's poster is the base; then someone changes Hot Fuzz's poster directly on Jellyfin.
    libs = (await client.get("/api/libraries")).json()["libraries"]
    films = next(lib for lib in libs if lib["kind"] == "movie")
    page = (
        await client.get("/api/items", params={"library_id": films["id"], "q": "Hot Fuzz"})
    ).json()
    fuzz = (await client.get(f"/api/items/{page['items'][0]['id']}")).json()
    servers = {s["id"]: s["kind"] for s in (await client.get("/api/servers")).json()}
    emby = next(p for p in fuzz["servers"] if servers[p["server_id"]] == "emby")
    resp = await client.put(
        f"/api/items/{fuzz['id']}/poster-source",
        json={"poster_source": f"server:{emby['server_id']}"},
    )
    assert resp.status_code == 200, resp.text
    jf = fake_servers["jellyfin"].by_title("Hot Fuzz")
    jf.alt_art, jf.art_version = True, jf.art_version + 1

    assert await scheduler.tick(datetime.now() + timedelta(days=7)) is True
    await app.state.jobs.wait_idle()
    assert await scheduler.tick(datetime.now() + timedelta(days=7, hours=1)) is False  # once
    job = (await _jobs(client, "safety_scan"))[0]
    assert job["status"] == "succeeded", job
    assert job["result"]["push"]["uploaded"] >= 1
    assert jf.uploaded is not None
    assert (await client.get("/api/automation")).json()["weekly_last_run"]
