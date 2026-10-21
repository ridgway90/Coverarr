"""Your own posters: uploads, TMDb picks, poster sets, and the A-Z filter."""

import io

import respx
from PIL import Image

from tests.conftest import run_job
from tests.fakeserver import poster_png
from tests.test_flow import add_servers
from tests.test_kometa import _zip


async def _libs(client) -> tuple[dict, dict]:
    libs = (await client.get("/api/libraries")).json()["libraries"]
    return (
        next(lib for lib in libs if lib["kind"] == "movie"),
        next(lib for lib in libs if lib["kind"] == "show"),
    )


async def _item(client, library_id: int, title: str) -> dict:
    page = (await client.get("/api/items", params={"library_id": library_id, "q": title})).json()
    return next(i for i in page["items"] if i["title"] == title)


def _jpeg(seed: str) -> bytes:
    buf = io.BytesIO()
    Image.open(io.BytesIO(poster_png(seed))).convert("RGB").save(buf, "JPEG")
    return buf.getvalue()


async def test_upload_replace_and_delete(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films, _ = await _libs(client)
    elf = await _item(client, films["id"], "Elf")
    url = f"/api/items/{elf['id']}/artwork"

    bad = await client.post(url, files={"file": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 422

    first = await client.post(url, files={"file": ("mine.png", poster_png("my elf"), "image/png")})
    assert first.status_code == 200, first.text
    art = first.json()
    detail = (await client.get(f"/api/items/{elf['id']}")).json()
    assert detail["poster_source"] == f"artwork:{art['artwork_id']}"
    assert [a["source"] for a in detail["artwork"]] == ["upload"]
    assets = app.state.config.config_dir / "assets" / "movies"
    assert sorted(p.name for p in assets.rglob("*")) == [
        "Elf (2003) {tmdb-10719}",
        "poster-upload.png",
    ]
    # Every server needs it now.
    [change] = (await client.get("/api/changes")).json()["items"]
    assert change["title"] == "Elf (2003)" and len(change["servers"]) == 3

    # Uploading again replaces it (new id, so cached thumbnails can't go stale).
    second = await client.post(url, files={"file": ("mine.jpg", _jpeg("better elf"), "image/jpeg")})
    new = second.json()
    assert new["artwork_id"] != art["artwork_id"]
    names = sorted(p.name for p in assets.rglob("*") if p.is_file())
    assert names == ["poster-upload.jpg"]
    detail = (await client.get(f"/api/items/{elf['id']}")).json()
    assert detail["poster_source"] == f"artwork:{new['artwork_id']}"

    resp = await client.delete(f"/api/artwork/{new['artwork_id']}")
    assert resp.status_code == 200
    detail = (await client.get(f"/api/items/{elf['id']}")).json()
    assert detail["poster_source"] is None and detail["artwork"] == []
    assert not list(assets.rglob("*"))


async def test_tmdb_posters(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films, tv = await _libs(client)
    arrival = await _item(client, films["id"], "Arrival")
    spaced = await _item(client, tv["id"], "Spaced")

    no_key = await client.get(f"/api/items/{arrival['id']}/tmdb-posters")
    assert no_key.status_code == 422 and "TMDb API key" in no_key.json()["detail"]

    await client.put("/api/settings", json={"tmdb_api_key": "0123456789abcdef0123456789abcdef"})
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.themoviedb.org/3/movie/329865/images").respond(
            json={
                "posters": [
                    {"file_path": "/fr.jpg", "iso_639_1": "fr", "vote_average": 9, "width": 2000},
                    {"file_path": "/en.jpg", "iso_639_1": "en", "vote_average": 5, "width": 2000},
                    {"file_path": "/none.jpg", "iso_639_1": None, "vote_average": 6, "width": 1000},
                ]
            }
        )
        mock.get("https://image.tmdb.org/t/p/original/en.jpg").respond(
            content=_jpeg("tmdb arrival"), headers={"Content-Type": "image/jpeg"}
        )
        # Spaced only has a TVDB id: look it up first.
        mock.get("https://api.themoviedb.org/3/find/75592").respond(
            json={"tv_results": [{"id": 4228}]}
        )
        mock.get("https://api.themoviedb.org/3/tv/4228/images").respond(
            json={"posters": [{"file_path": "/spaced.jpg", "iso_639_1": "en"}]}
        )
        mock.get("https://api.themoviedb.org/3/search/collection").respond(
            json={
                "results": [
                    {"id": 9435, "name": "Three Flavours Cornetto Collection"},
                    {"id": 9436, "name": "Cornetto Collection"},
                ]
            }
        )
        mock.get("https://api.themoviedb.org/3/collection/9436/images").respond(
            json={"posters": []}
        )

        found = (await client.get(f"/api/items/{arrival['id']}/tmdb-posters")).json()
        # English first, then textless, then other languages.
        assert [p["file_path"] for p in found["posters"]] == ["/en.jpg", "/none.jpg", "/fr.jpg"]
        assert found["posters"][0]["thumb"] == "https://image.tmdb.org/t/p/w342/en.jpg"

        bad = await client.post(
            f"/api/items/{arrival['id']}/artwork/tmdb", json={"file_path": "https://evil/x.jpg"}
        )
        assert bad.status_code == 422
        chosen = await client.post(
            f"/api/items/{arrival['id']}/artwork/tmdb", json={"file_path": "/en.jpg"}
        )
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["source"] == "tmdb"

        shows = (await client.get(f"/api/items/{spaced['id']}/tmdb-posters")).json()
        assert [p["file_path"] for p in shows["posters"]] == ["/spaced.jpg"]

        colls = (await client.get("/api/collections", params={"library_id": films["id"]})).json()
        cornetto = next(c for c in colls if c["name"] == "Cornetto Collection")
        result = (await client.get(f"/api/collections/{cornetto['id']}/tmdb-posters")).json()
        assert result == {"source": "TMDb collection: Cornetto Collection", "posters": []}

    detail = (await client.get(f"/api/items/{arrival['id']}")).json()
    assert detail["poster_source"].startswith("artwork:")


async def test_poster_set(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films, tv = await _libs(client)
    elf = await _item(client, films["id"], "Elf")
    # A choice made earlier is kept when asked to.
    await client.post(
        f"/api/items/{elf['id']}/artwork",
        files={"file": ("mine.png", poster_png("my elf"), "image/png")},
    )

    archive = _zip(
        {
            "Set/Hot Fuzz (2007).png": poster_png("set hot fuzz"),
            "Set/The World's End (2013).png": poster_png("set worlds end"),
            "Set/Shaun of the Dead (2004) {tmdb-747}.png": poster_png("set shaun"),
            "Set/Elf (2003).png": poster_png("set elf"),
            "Set/Cornetto Collection.png": poster_png("set cornetto"),
            "Set/Spaced (1999) - Season 1.png": poster_png("set spaced s1"),
            "Set/Spaced (1999) - Specials.png": poster_png("set specials"),
            "Set/Black Books.png": poster_png("set black books"),
            "Set/Not In My Library (2001).png": poster_png("nope"),
            "Set/readme.txt": b"hello",
            "__MACOSX/Set/._Hot Fuzz (2007).png": b"junk",
        }
    )
    resp = await client.post(
        "/api/poster-sets/upload",
        files={"file": ("set.zip", archive, "application/zip")},
        data={"keep_choices": "true"},
    )
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    r = job["result"]
    assert (r["imported"], r["applied"], r["kept_choice"]) == (7, 6, 1)
    assert sorted(r["unmatched_files"]) == [
        "Not In My Library (2001).png",
        "Spaced (1999) - Specials.png",  # no specials season here
    ]
    assert r["skipped"] == 1

    changes = {c["title"] for c in (await client.get("/api/changes")).json()["items"]}
    assert {
        "Hot Fuzz (2007)",
        "The World's End (2013)",
        "Shaun of the Dead (2004)",
        "Cornetto Collection",
        "Spaced: Season 1",
        "Black Books (2000)",
    } <= changes
    elf_detail = (await client.get(f"/api/items/{elf['id']}")).json()
    sources = {a["artwork_id"]: a["source"] for a in elf_detail["artwork"]}
    assert sources[int(elf_detail["poster_source"].split(":")[1])] == "upload"
    assert sorted(sources.values()) == ["set", "upload"]
    tv_assets = app.state.config.config_dir / "assets" / "tv"
    assert sorted(p.name for p in tv_assets.rglob("*.png")) == [
        "poster-set.png",
        "season01-set.png",
    ]


async def test_alphabet_filter(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films, _ = await _libs(client)

    page = (await client.get("/api/items", params={"library_id": films["id"]})).json()
    letters = page["letters"]
    assert letters["H"] == 3  # Hot Fuzz, Home Alone, The Holiday
    assert letters["#"] == 2  # 9, and Plex's mismatched "9 Songs"
    assert "T" not in letters  # "The ..." files under its next word

    h = (await client.get("/api/items", params={"library_id": films["id"], "letter": "H"})).json()
    assert sorted(i["title"] for i in h["items"]) == ["Home Alone", "Hot Fuzz", "The Holiday"]
    digits = (
        await client.get("/api/items", params={"library_id": films["id"], "letter": "#"})
    ).json()
    assert sorted(i["title"] for i in digits["items"]) == ["9", "9 Songs"]
    bad = await client.get("/api/items", params={"library_id": films["id"], "letter": "AB"})
    assert bad.status_code == 422


async def test_previous_next(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films, _ = await _libs(client)
    page = (
        await client.get("/api/items", params={"library_id": films["id"], "letter": "H"})
    ).json()
    ids = [i["id"] for i in page["items"]]
    middle = (await client.get(f"/api/items/{ids[1]}/neighbours", params={"letter": "H"})).json()
    assert (middle["prev"]["id"], middle["next"]["id"]) == (ids[0], ids[2])
    assert (middle["position"], middle["total"]) == (2, 3)
    last = (await client.get(f"/api/items/{ids[2]}/neighbours", params={"letter": "H"})).json()
    assert last["next"] is None and last["prev"]["id"] == ids[1]
    # Without filters it walks the whole library in the listing's order.
    everything = (
        await client.get("/api/items", params={"library_id": films["id"], "page_size": 100})
    ).json()["items"]
    first = (await client.get(f"/api/items/{everything[0]['id']}/neighbours")).json()
    assert first["prev"] is None and first["next"]["id"] == everything[1]["id"]
    # Filtered out of the list: no neighbours rather than wrong ones.
    outside = (
        await client.get(f"/api/items/{everything[0]['id']}/neighbours", params={"letter": "Z"})
    ).json()
    assert outside["position"] is None

    colls = (await client.get("/api/collections", params={"library_id": films["id"]})).json()
    nav = (await client.get(f"/api/collections/{colls[0]['id']}/neighbours")).json()
    assert nav["prev"] is None and nav["next"]["id"] == colls[1]["id"]
    nav = (
        await client.get(f"/api/collections/{colls[0]['id']}/neighbours", params={"letter": "P"})
    ).json()
    assert nav["position"] is None


async def test_season_posters(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    _, tv = await _libs(client)
    spaced = await _item(client, tv["id"], "Spaced")
    show = (await client.get(f"/api/items/{spaced['id']}")).json()
    season1 = next(s for s in show["seasons"] if s["season_number"] == 1)
    assert season1["poster"]["has_poster"] and season1["poster_source"] is None

    detail = (await client.get(f"/api/items/{season1['id']}")).json()
    assert detail["kind"] == "season" and detail["title"] == "Spaced: Season 1"
    assert detail["parent"] == {"id": spaced["id"], "title": "Spaced", "year": 1999}
    assert len(detail["servers"]) == 3

    resp = await client.post(
        f"/api/items/{season1['id']}/artwork",
        files={"file": ("s1.png", poster_png("my season one"), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    show = (await client.get(f"/api/items/{spaced['id']}")).json()
    season1 = next(s for s in show["seasons"] if s["season_number"] == 1)
    assert season1["poster_source"] == f"artwork:{resp.json()['artwork_id']}"
    assert season1["poster"]["artwork_id"] == resp.json()["artwork_id"]

    [change] = (await client.get("/api/changes")).json()["items"]
    assert change["title"] == "Spaced: Season 1" and change["kind"] == "season"
    assert change["top_id"] == spaced["id"] and len(change["servers"]) == 3
    job = (await client.post("/api/changes/push", json={"media_item_ids": [season1["id"]]})).json()
    await app.state.jobs.wait_idle()
    result = (await client.get(f"/api/jobs/{job['job_id']}")).json()["result"]
    assert result["uploaded"] == 3
    plex = fake_servers["plex"]
    plex_show = plex.by_title("Spaced")
    plex_season = next(i for i in plex.items.values() if i.parent == plex_show.id and i.index == 1)
    assert plex_season.uploaded is not None
    assert ids  # all three servers took it


async def test_reset_from_tmdb(app, authed, fake_servers):
    import httpx

    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films, _ = await _libs(client)
    hot_fuzz = await _item(client, films["id"], "Hot Fuzz")
    await client.post(
        f"/api/items/{hot_fuzz['id']}/artwork",
        files={"file": ("mine.png", poster_png("my hot fuzz"), "image/png")},
    )
    await client.put("/api/settings", json={"tmdb_api_key": "0123456789abcdef0123456789abcdef"})

    def movie(request):
        tmdb_id = request.url.path.rsplit("/", 1)[1]
        # Elf has no poster on TMDb.
        return httpx.Response(
            200, json={"poster_path": None if tmdb_id == "10719" else f"/m{tmdb_id}.jpg"}
        )

    def search(request):
        name = request.url.params["query"]
        results = (
            [{"id": 9436, "name": name, "poster_path": "/c.jpg"}] if "Cornetto" in name else []
        )
        return httpx.Response(200, json={"results": results})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+").mock(side_effect=movie)
        mock.get("https://api.themoviedb.org/3/find/75592").respond(
            json={"tv_results": [{"id": 4228}]}
        )
        mock.get("https://api.themoviedb.org/3/find/72216").respond(
            json={"tv_results": [{"id": 1234}]}
        )
        mock.get(url__regex=r"https://api\.themoviedb\.org/3/tv/\d+/season/\d+").respond(
            json={"poster_path": "/season.jpg"}
        )
        mock.get(url__regex=r"https://api\.themoviedb\.org/3/tv/\d+(\?|$)").respond(
            json={"poster_path": "/show.jpg"}
        )
        mock.get("https://api.themoviedb.org/3/search/collection").mock(side_effect=search)
        mock.get(url__regex=r"https://image\.tmdb\.org/t/p/original/.*").respond(
            content=_jpeg("tmdb poster"), headers={"Content-Type": "image/jpeg"}
        )
        resp = await client.post("/api/posters/reset-from-tmdb", json={})
        assert resp.status_code == 200, resp.text
        await app.state.jobs.wait_idle()
        job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
        assert job["status"] == "succeeded", job
        r = job["result"]
        # 12 film entries (with Plex's mismatched "9 Songs") minus Hot Fuzz (your upload is
        # kept), Elf (no poster on TMDb) and Family Holiday (no TMDb id) = 9; three
        # collections TMDb doesn't have aren't found.
        assert "Elf" in r["not_found"] and "Family Holiday 2019" in r["not_found"]
        assert "Comfort J Films" in r["not_found"]
        assert r["replaced"] == 9 + 2 + 5 + 1  # films, shows, seasons, Cornetto

        home_alone = (
            await client.get(f"/api/items/{(await _item(client, films['id'], 'Home Alone'))['id']}")
        ).json()
        chosen = int(home_alone["poster_source"].split(":")[1])
        assert {a["artwork_id"]: a["source"] for a in home_alone["artwork"]}[chosen] == "tmdb"
        kept = (await client.get(f"/api/items/{hot_fuzz['id']}")).json()
        assert {a["artwork_id"]: a["source"] for a in kept["artwork"]}[
            int(kept["poster_source"].split(":")[1])
        ] == "upload"

        # A plain re-run (e.g. after a cancel) skips what is already on TMDb's poster.
        resp = await client.post("/api/posters/reset-from-tmdb", json={})
        await app.state.jobs.wait_idle()
        job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
        assert job["result"].get("replaced", 0) == 0
        assert job["result"]["unchanged"] == 9 + 2 + 5 + 1

        # Only titles without a base: everything found has one now.
        resp = await client.post("/api/posters/reset-from-tmdb", json={"only_missing": True})
        await app.state.jobs.wait_idle()
        job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
        assert job["result"].get("replaced", 0) == 0

    bad = await client.post(
        "/api/posters/reset-from-tmdb", json={"kinds": [], "collections": False}
    )
    assert bad.status_code == 422
