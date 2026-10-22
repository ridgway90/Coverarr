"""Collections built from lists: defined in Coverarr, synced to every server."""

import json

import httpx
import respx

from coverarr.services.kometa_yaml import parse_collection_file
from coverarr.services.lists import ListClient
from tests.conftest import run_job
from tests.fakeserver import MOVIE_CATALOG, poster_png
from tests.test_flow import add_servers

KEY = "0123456789abcdef0123456789abcdef"


def _tmdb(mock) -> None:
    """TMDb as far as these tests need it."""

    def movie(request):
        tmdb_id = request.url.path.rsplit("/", 1)[1]
        title, year, imdb = MOVIE_CATALOG[tmdb_id]
        return httpx.Response(
            200,
            json={
                "id": int(tmdb_id),
                "title": title,
                "release_date": f"{year}-01-01",
                "imdb_id": imdb,
            },
        )

    mock.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+(\?|$)").mock(side_effect=movie)
    mock.get(url__regex=r"https://api\.themoviedb\.org/3/movie/popular").respond(
        json={
            "total_pages": 1,
            "results": [
                {"id": 346648, "title": "Paddington 2", "release_date": "2017-11-10"},
                {"id": 999001, "title": "Not In Your Library", "release_date": "2026-05-01"},
                {"id": 771, "title": "Home Alone", "release_date": "1990-11-16"},
            ],
        }
    )
    mock.get(url__regex=r"https://api\.themoviedb\.org/3/collection/746").respond(
        json={
            "poster_path": "/paddington.jpg",
            "parts": [
                {"id": 116149, "title": "Paddington", "release_date": "2014-11-28"},
                {"id": 346648, "title": "Paddington 2", "release_date": "2017-11-10"},
                {"id": 999002, "title": "Paddington in Peru", "release_date": "2024-11-08"},
            ],
        }
    )
    mock.get("https://image.tmdb.org/t/p/original/paddington.jpg").respond(
        content=poster_png("tmdb paddington"), headers={"Content-Type": "image/png"}
    )


async def _films(client) -> int:
    libs = (await client.get("/api/libraries")).json()["libraries"]
    return next(lib["id"] for lib in libs if lib["kind"] == "movie")


def _coll(srv, name: str):
    return next((c for c in srv.collections.values() if c.name == name), None)


def _titles(srv, coll) -> list[str]:
    return [srv.items[m].title for m in coll.members]


async def _sync(app, client, definition_id: int) -> dict:
    resp = await client.post("/api/definitions/sync", json={"ids": [definition_id]})
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    defs = (await client.get("/api/definitions")).json()
    return next(d for d in defs if d["id"] == definition_id)


async def test_define_sync_reorder_and_trim(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    films = await _films(client)

    bad = await client.post(
        "/api/definitions",
        json={
            "name": "X",
            "library_id": films,
            "builders": [{"type": "imdb_chart", "value": "top"}],
        },
    )
    assert bad.status_code == 422
    resp = await client.post(
        "/api/definitions",
        json={
            "name": "Edgar Wright",
            "library_id": films,
            "builders": [
                {"type": "tmdb_movie", "value": "https://www.themoviedb.org/movie/747-shaun"},
                {"type": "tmdb_movie", "value": 4638},
            ],
            "options": {
                "collection_order": "custom",
                "summary": "Cornetto and friends",
                "sort_title": "!01 Edgar Wright",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    definition = resp.json()
    assert definition["enabled"] is True
    assert definition["builders"][0] == {"type": "tmdb_movie", "value": "747"}

    with respx.mock(assert_all_called=False) as mock:
        _tmdb(mock)
        synced = await _sync(app, client, definition["id"])
        assert synced["sync_result"]["in_library"] == 2
        for srv in fake_servers.values():
            coll = _coll(srv, "Edgar Wright")
            assert _titles(srv, coll) == ["Shaun of the Dead", "Hot Fuzz"]
            assert coll.summary == "Cornetto and friends"
            assert coll.sort_title == "!01 Edgar Wright"
        assert _coll(fake_servers["plex"], "Edgar Wright").order == "2"  # custom
        assert any("custom order" in n for n in synced["sync_result"]["notes"])

        # New list order: Plex follows it.
        await client.put(
            f"/api/definitions/{definition['id']}",
            json={
                "builders": [
                    {"type": "tmdb_movie", "value": 4638},
                    {"type": "tmdb_movie", "value": 747},
                ]
            },
        )
        await _sync(app, client, definition["id"])
        plex = fake_servers["plex"]
        assert _titles(plex, _coll(plex, "Edgar Wright")) == ["Hot Fuzz", "Shaun of the Dead"]

        # Add-only keeps a title that left the list; exact sync removes it.
        await client.put(
            f"/api/definitions/{definition['id']}",
            json={"builders": [{"type": "tmdb_movie", "value": 4638}]},
        )
        await _sync(app, client, definition["id"])
        assert "Shaun of the Dead" in _titles(plex, _coll(plex, "Edgar Wright"))
        await client.put(
            f"/api/definitions/{definition['id']}",
            json={"options": {"sync_mode": "sync", "collection_order": "release"}},
        )
        synced = await _sync(app, client, definition["id"])
        for srv in fake_servers.values():
            assert _titles(srv, _coll(srv, "Edgar Wright")) == ["Hot Fuzz"]
        assert synced["sync_result"]["servers"]["Plex"]["removed"] == 1
        assert synced["options"]["summary"] == "Cornetto and friends"  # kept: not sent again


async def test_chart_and_failed_lists_never_empty_a_collection(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    films = await _films(client)
    definition = (
        await client.post(
            "/api/definitions",
            json={
                "name": "Popular",
                "library_id": films,
                "builders": [{"type": "tmdb_popular", "value": 10}],
                "options": {"sync_mode": "sync", "collection_order": "alpha"},
            },
        )
    ).json()
    with respx.mock(assert_all_called=False) as mock:
        _tmdb(mock)
        synced = await _sync(app, client, definition["id"])
    result = synced["sync_result"]
    assert (result["total"], result["in_library"]) == (3, 2)
    assert result["missing"] == [{"title": "Not In Your Library", "year": 2026}]
    plex = fake_servers["plex"]
    assert _titles(plex, _coll(plex, "Popular")) == ["Home Alone", "Paddington 2"]  # alpha

    # TMDb down: the sync-mode collection is left alone rather than emptied.
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://api\.themoviedb\.org/.*").respond(503)
        synced = await _sync(app, client, definition["id"])
    assert synced["sync_result"]["errors"]
    assert _titles(plex, _coll(plex, "Popular")) == ["Home Alone", "Paddington 2"]


async def test_new_tmdb_collection_gets_its_poster(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    films = await _films(client)
    definition = (
        await client.post(
            "/api/definitions",
            json={
                "name": "Paddington Films",
                "library_id": films,
                "builders": [{"type": "tmdb_collection", "value": 746}],
            },
        )
    ).json()
    with respx.mock(assert_all_called=False) as mock:
        _tmdb(mock)
        synced = await _sync(app, client, definition["id"])
    assert synced["sync_result"]["created"] is True
    assert synced["sync_result"]["missing_count"] == 1  # Paddington in Peru
    for srv in fake_servers.values():
        coll = _coll(srv, "Paddington Films")
        assert coll is not None and coll.uploaded is not None  # TMDb's poster, pushed
    # Switched off: the daily sync leaves it alone.
    await client.put(f"/api/definitions/{definition['id']}", json={"enabled": False})
    await client.post("/api/definitions/sync", json={})
    await app.state.jobs.wait_idle()
    jobs = [j for j in (await client.get("/api/jobs")).json() if j["kind"] == "sync_definitions"]
    assert jobs[0]["result"].get("synced", 0) == 0


async def test_rename_follows_on_servers(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films = await _films(client)
    yaml = "collections:\n  Paddington Collection:\n    tmdb_collection: 746\n"
    files = {"files": ("paddington.yml", yaml.encode(), "text/yaml")}
    resp = await client.post("/api/kometa/yaml", files=files, data={"library_id": str(films)})
    assert resp.status_code == 200, resp.text
    definition = next(d for d in (await client.get("/api/definitions")).json())
    assert definition["enabled"] is False  # Kometa's collections aren't taken over unasked
    resp = await client.put(f"/api/definitions/{definition['id']}", json={"name": "Paddington"})
    assert resp.json()["rename_job"]
    await app.state.jobs.wait_idle()
    for srv in fake_servers.values():
        assert _coll(srv, "Paddington") is not None
        assert _coll(srv, "Paddington Collection") is None


def test_kometa_charts_parse():
    text = "collections:\n  Trending:\n    tmdb_trending_weekly: 30\n    trakt_popular: 10\n"
    parsed = parse_collection_file(text, "charts.yml")[0]
    assert parsed.builders == [
        {"type": "tmdb_trending_weekly", "value": "30"},
        {"type": "trakt_popular", "value": "10"},
    ]
    assert not parsed.warnings


async def test_trakt_charts():
    movie = {"title": "Hot Fuzz", "year": 2007, "ids": {"tmdb": 4638, "imdb": "tt0425112"}}
    with respx.mock() as mock:
        mock.get("https://api.trakt.tv/movies/popular").respond(json=[movie])
        mock.get("https://api.trakt.tv/movies/trending").respond(
            json=[{"watchers": 9, "movie": movie}]
        )
        async with ListClient(tmdb_key=None, trakt_client_id="abc") as lists:
            for chart in ("trakt_popular", "trakt_trending"):
                entries = await lists.resolve({"type": chart, "value": "5"}, "movie")
                assert [(e.title, e.tmdb_id) for e in entries] == [("Hot Fuzz", "4638")]
            try:
                await lists.resolve({"type": "trakt_boxoffice", "value": "5"}, "show")
                raise AssertionError("box office has no shows")
            except Exception as exc:  # noqa: BLE001
                assert "isn't available" in str(exc)


async def test_kometa_chart_replaces_its_titles(app, authed, fake_servers):
    """Kometa charts carry no sync mode (it's usually in a template): they default to exact."""
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    films = await _films(client)
    yaml = "collections:\n  TMDb Popular:\n    tmdb_popular: 10\n    template: {name: chart}\n"
    files = {"files": ("tmdb.yml", yaml.encode(), "text/yaml")}
    await client.post("/api/kometa/yaml", files=files, data={"library_id": str(films)})
    definition = (await client.get("/api/definitions")).json()[0]
    assert definition["sync_mode"] == "sync"
    plex = fake_servers["plex"]
    with respx.mock(assert_all_called=False) as mock:
        _tmdb(mock)
        await _sync(app, client, definition["id"])
        assert sorted(_titles(plex, _coll(plex, "TMDb Popular"))) == ["Home Alone", "Paddington 2"]
        # Now only the top 1: Home Alone drops off the chart and out of the collection.
        await client.put(
            f"/api/definitions/{definition['id']}",
            json={"builders": [{"type": "tmdb_popular", "value": 1}]},
        )
        await _sync(app, client, definition["id"])
    for srv in fake_servers.values():
        assert _titles(srv, _coll(srv, "TMDb Popular")) == ["Paddington 2"]


async def test_collection_and_item_labels(app, authed, fake_servers):
    """Labels go on the collection and its titles everywhere; ones dropped come off."""
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    films = await _films(client)
    resp = await client.post(
        "/api/definitions",
        json={
            "name": "Labelled Picks",
            "library_id": films,
            "builders": [
                {"type": "tmdb_movie", "value": 747},
                {"type": "tmdb_movie", "value": 4638},
            ],
            "options": {
                "label": "Kids, Favourites",
                "item_label": ["Cornetto"],
                "sync_mode": "sync",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    definition = resp.json()
    assert definition["options"]["label"] == ["Kids", "Favourites"]

    def item_labels(srv, title):
        item = srv.by_title(title)
        return item.labels if srv.kind == "plex" else item.tags

    with respx.mock(assert_all_called=False) as mock:
        _tmdb(mock)
        synced = await _sync(app, client, definition["id"])
        assert not [n for n in synced["sync_result"]["notes"] if "label" in n], synced[
            "sync_result"
        ]
        for srv in fake_servers.values():
            assert _coll(srv, "Labelled Picks").labels == ["Kids", "Favourites"], srv.kind
            for title in ("Shaun of the Dead", "Hot Fuzz"):
                assert "Cornetto" in item_labels(srv, title), (srv.kind, title)

        # A label taken off the definition comes off the collection; a title that leaves
        # the list loses the item label.
        await client.put(
            f"/api/definitions/{definition['id']}",
            json={
                "options": {"label": ["Kids"]},
                "builders": [{"type": "tmdb_movie", "value": 4638}],
            },
        )
        await run_job(app, client, "import")  # labels as the servers now have them
        await _sync(app, client, definition["id"])
        for srv in fake_servers.values():
            assert _coll(srv, "Labelled Picks").labels == ["Kids"], srv.kind
            assert "Cornetto" in item_labels(srv, "Hot Fuzz")
            assert "Cornetto" not in item_labels(srv, "Shaun of the Dead"), srv.kind


def test_kometa_labels_are_read():
    parsed = parse_collection_file(
        "collections:\n  Kids:\n    tmdb_movie: 747\n    label: kids, family\n"
        "    item_label: [Kids Film]\n",
        "kids.yml",
    )
    assert parsed[0].options == {"label": "kids, family", "item_label": ["Kids Film"]}
    assert "item_label" not in parsed[0].unsupported


def _imdb_page(titles: list[tuple[str, str, int]], *, next_data: bool = True) -> str:
    """An IMDb chart/list page: embedded data when ``next_data``, else only title links."""
    links = "".join(
        f'<a href="/title/{tid}/?ref_=chttp_t_{i}">{t}</a>' for i, (tid, t, _) in enumerate(titles)
    )
    if not next_data:
        return f"<html><body><a href='/title/tt9999999/'>Ad</a>{links}</body></html>"
    edges = [
        {
            "currentRank": i + 1,
            "node": {
                "id": tid,
                "titleText": {"text": t},
                "releaseYear": {"year": y},
                "titleType": {"id": "movie"},
            },
        }
        for i, (tid, t, y) in enumerate(titles)
    ]
    data = {
        "props": {
            "pageProps": {
                "pageData": {"chartTitles": {"edges": edges}},
                "nav": [{"id": "tt0000001", "titleText": {"text": "Promo"}}],
            }
        }
    }
    return (
        "<html><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
        f"{links}</body></html>"
    )


def test_imdb_pages_are_read():
    from coverarr.services.lists import imdb_titles, normalize_builder

    page = _imdb_page([("tt0425112", "Hot Fuzz", 2007), ("tt0365748", "Shaun of the Dead", 2004)])
    entries = imdb_titles(page, "movie")
    assert [(e.imdb_id, e.title, e.year) for e in entries] == [
        ("tt0425112", "Hot Fuzz", 2007),
        ("tt0365748", "Shaun of the Dead", 2004),
    ]
    # Without the embedded data the title links still give the ids, in order.
    bare = imdb_titles(_imdb_page([("tt0425112", "Hot Fuzz", 2007)], next_data=False), "movie")
    assert [e.imdb_id for e in bare] == ["tt9999999", "tt0425112"]
    assert normalize_builder("imdb_chart", "Top_Movies") == {
        "type": "imdb_chart",
        "value": "top_movies",
    }
    assert normalize_builder("imdb_list", "https://www.imdb.com/list/ls055592025/?sort=x") == {
        "type": "imdb_list",
        "value": "ls055592025",
    }
    assert (
        normalize_builder("imdb_id", "https://www.imdb.com/title/tt0425112/")["value"]
        == "tt0425112"
    )


async def test_imdb_chart_and_list_collections(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films = await _films(client)
    resp = await client.post(
        "/api/definitions",
        json={
            "name": "IMDb Picks",
            "library_id": films,
            "builders": [
                {"type": "imdb_chart", "value": "top_movies"},
                {"type": "imdb_list", "value": "https://www.imdb.com/list/ls000000001/"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    definition = resp.json()
    assert definition["sync_mode"] == "append"  # a list as well as a chart
    chart = _imdb_page([("tt0099785", "Home Alone", 1990), ("tt1234567", "Not Yours", 2020)])
    listing = _imdb_page([("tt0319343", "Elf", 2003)])
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://www.imdb.com/chart/top/").respond(text=chart)
        mock.get(url__regex=r"https://www\.imdb\.com/list/ls000000001/\?page=1$").respond(
            text=listing
        )
        mock.get(url__regex=r"https://www\.imdb\.com/list/ls000000001/\?page=\d+$").respond(
            text=listing
        )
        synced = await _sync(app, client, definition["id"])
    result = synced["sync_result"]
    assert result["errors"] == [], result
    assert result["in_library"] == 2 and result["missing"] == [{"title": "Not Yours", "year": 2020}]
    for srv in fake_servers.values():
        assert sorted(_titles(srv, _coll(srv, "IMDb Picks"))) == ["Elf", "Home Alone"]

    # IMDb holding the page back is an error on the list, and nothing is removed.
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://www.imdb.com/chart/top/").respond(202, text="")
        mock.get(url__regex=r"https://www\.imdb\.com/list/.*").respond(text=listing)
        synced = await _sync(app, client, definition["id"])
    assert any("IMDb didn't send the page" in e for e in synced["sync_result"]["errors"])
    for srv in fake_servers.values():
        assert sorted(_titles(srv, _coll(srv, "IMDb Picks"))) == ["Elf", "Home Alone"]
