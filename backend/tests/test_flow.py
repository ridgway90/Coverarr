"""End-to-end: three fake servers -> import -> review -> push fixes -> re-import."""

from tests.conftest import SERVER_CONFIGS, run_job


async def add_servers(client) -> dict[str, int]:
    ids = {}
    for cfg in SERVER_CONFIGS:
        test = (await client.post("/api/servers/test", json=cfg)).json()
        assert test["ok"], test
        resp = await client.post("/api/servers", json=cfg)
        assert resp.status_code == 200, resp.text
        ids[cfg["kind"]] = resp.json()["id"]
    return ids


async def issues(client, type_, status="open") -> list[dict]:
    resp = await client.get("/api/review/issues", params={"type": type_, "status": status})
    return resp.json()["issues"]


async def test_requires_login(client):
    assert (await client.get("/api/servers")).status_code == 401
    status = (await client.get("/api/auth/status")).json()
    assert status == {"setup_required": True, "authenticated": False, "username": None}


async def test_login_roundtrip(authed):
    await authed.post("/api/auth/logout")
    authed.cookies.clear()
    assert (await authed.get("/api/servers")).status_code == 401
    bad = await authed.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401
    ok = await authed.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert ok.status_code == 200
    assert (await authed.get("/api/servers")).status_code == 200
    again = await authed.post("/api/auth/setup", json={"username": "x", "password": "password123"})
    assert again.status_code == 409


async def test_bad_token_reported(authed, fake_servers):
    cfg = {**SERVER_CONFIGS[0], "token": "wrong"}
    result = (await authed.post("/api/servers/test", json=cfg)).json()
    assert result["ok"] is False and "authentication" in result["error"]


async def test_full_flow(app, authed, fake_servers):
    client = authed
    server_ids = await add_servers(client)

    job = await run_job(app, client, "import")
    assert job["result"]["errors"] == {}
    assert job["result"]["servers"]["Plex"] == {
        "movie": 11,
        "show": 2,
        "season": 5,
        "episode": 32,
        "collection": 4,
    }

    # Plex "Films" and Jellyfin/Emby "Movies" land in one logical library.
    libs = (await client.get("/api/libraries")).json()
    assert sorted((lib["name"], lib["item_count"]) for lib in libs["libraries"]) == [
        ("Films", 12),  # "9" and Plex's mismatched "9 Songs" until fixed
        ("TV Programmes", 2),
    ]
    assert all(len(lib["server_libraries"]) == 3 for lib in libs["libraries"])

    dash = (await client.get("/api/dashboard")).json()
    assert dash["counts"] == {"movie": 12, "show": 2, "season": 5, "episode": 32, "collection": 4}

    films = next(lib for lib in libs["libraries"] if lib["name"] == "Films")
    # Arrival is missing on Emby; the id-less home video still matched by title + year.
    missing = await issues(client, "item_missing")
    assert [(i["title"], i["details"]["missing"]) for i in missing] == [
        ("Arrival (2016)", [server_ids["emby"]])
    ]
    page = (
        await client.get("/api/items", params={"library_id": films["id"], "q": "Family"})
    ).json()
    assert len(page["items"][0]["servers"]) == 3

    missing_colls = {
        i["title"]: i["details"]["missing"] for i in await issues(client, "collection_missing")
    }
    assert missing_colls == {
        "Cornetto Collection": [server_ids["jellyfin"], server_ids["emby"]],
        "Christmas Movies": [server_ids["emby"]],
        "Comfort J Films": [server_ids["emby"]],
    }
    differ = await issues(client, "collection_members_differ")
    assert [(i["title"], list(i["details"]["missing"])) for i in differ] == [
        ("Christmas Movies", [str(server_ids["jellyfin"])])
    ]
    assert differ[0]["details"]["missing"][str(server_ids["jellyfin"])]["items"][0]["title"] == (
        "The Holiday (2006)"
    )

    # Poster fingerprints: Kometa overlays alone don't count as different artwork.
    scan = await run_job(app, client, "poster_scan")
    assert scan["result"]["failed"] == 0
    posters = await issues(client, "poster_differs")
    assert [i["title"] for i in posters] == ["Arrival (2016)"]
    plex_entry = next(
        s for s in posters[0]["details"]["servers"] if s["server_id"] == server_ids["plex"]
    )
    assert plex_entry["kometa_overlay"] is True

    # Thumbnails are proxied and cached.
    item = (await client.get(f"/api/items/{posters[0]['media_item_id']}")).json()
    img = await client.get(
        f"/api/images/item/{item['poster']['server_item_id']}", params={"w": 150}
    )
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    # Choose Jellyfin's poster as Arrival's base; the issue disappears.
    resp = await client.post(
        "/api/review/actions",
        json={
            "action": "choose_poster",
            "issue_ids": [posters[0]["id"]],
            "server_id": server_ids["jellyfin"],
        },
    )
    assert resp.json() == {"updated": 1, "skipped": 0}
    assert await issues(client, "poster_differs") == []

    # Push: create missing collections everywhere and fill in missing members.
    resp = await client.post(
        "/api/review/actions",
        json={"action": "create_collections", "all_of_type": "collection_missing"},
    )
    assert resp.json()["updated"] == 3
    await app.state.jobs.wait_idle()
    resp = await client.post(
        "/api/review/actions",
        json={"action": "sync_members", "all_of_type": "collection_members_differ"},
    )
    await app.state.jobs.wait_idle()
    assert await issues(client, "collection_missing") == []
    assert await issues(client, "collection_members_differ") == []

    jf = fake_servers["jellyfin"]
    cornetto = next(c for c in jf.collections.values() if c.name == "Cornetto Collection")
    assert sorted(jf.items[m].title for m in cornetto.members) == [
        "Hot Fuzz",
        "Shaun of the Dead",
        "The World's End",
    ]
    christmas = next(c for c in jf.collections.values() if c.name == "Christmas Movies")
    assert "The Holiday" in [jf.items[m].title for m in christmas.members]

    # Ignore the Arrival-missing issue; it must stay ignored across a re-import.
    missing = await issues(client, "item_missing")
    await client.post(
        "/api/review/actions", json={"action": "ignore", "issue_ids": [missing[0]["id"]]}
    )

    before = (
        await client.get("/api/items", params={"library_id": films["id"], "page_size": 100})
    ).json()
    await run_job(app, client, "import")
    after = (
        await client.get("/api/items", params={"library_id": films["id"], "page_size": 100})
    ).json()
    assert [i["id"] for i in before["items"]] == [i["id"] for i in after["items"]]
    assert await issues(client, "item_missing") == []
    assert len(await issues(client, "item_missing", status="ignored")) == 1
    colls = (await client.get("/api/collections", params={"library_id": films["id"]})).json()
    assert {c["name"]: len(c["servers"]) for c in colls} == {
        "Christmas Movies": 3,
        "Comfort J Films": 3,
        "Cornetto Collection": 3,
        "Paddington Collection": 3,
    }
    arrival = next(i for i in after["items"] if i["title"] == "Arrival")
    assert arrival["poster_source"] == f"server:{server_ids['jellyfin']}"

    # Incremental poster scan: only the newly created collections need fingerprints.
    rescan = await run_job(app, client, "poster_scan")
    assert rescan["result"]["total"] == 4

    # A changed poster on one server is picked up by its new poster ref alone.
    jf.by_title("Hot Fuzz").alt_art = True
    jf.by_title("Hot Fuzz").art_version += 1
    await run_job(app, client, "import")
    rescan = await run_job(app, client, "poster_scan")
    assert rescan["result"]["total"] == 1
    assert [i["title"] for i in await issues(client, "poster_differs")] == ["Hot Fuzz (2007)"]


async def test_show_detail_and_remap(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    libs = (await client.get("/api/libraries")).json()["libraries"]
    tv = next(lib for lib in libs if lib["kind"] == "show")
    shows = (await client.get("/api/items", params={"library_id": tv["id"]})).json()["items"]
    black_books = next(s for s in shows if s["title"] == "Black Books")
    detail = (await client.get(f"/api/items/{black_books['id']}")).json()
    s3 = next(s for s in detail["seasons"] if s["season_number"] == 3)
    assert sorted(s3["episodes"].values()) == [5, 6, 6]

    # Split Emby's TV library into its own logical library, then re-match.
    emby_sl = next(sl for sl in tv["server_libraries"] if sl["server_kind"] == "emby")
    resp = await client.put(
        f"/api/server-libraries/{emby_sl['id']}",
        json={"enabled": True, "new_library_name": "Emby TV"},
    )
    assert resp.status_code == 200
    await app.state.jobs.wait_idle()
    libs = (await client.get("/api/libraries")).json()["libraries"]
    assert sorted(lib["name"] for lib in libs) == ["Emby TV", "Films", "TV Programmes"]
    emby_tv = next(lib for lib in libs if lib["name"] == "Emby TV")
    assert emby_tv["item_count"] == 2


async def test_mismatch_suggest_and_fix(app, authed, fake_servers):
    client = authed
    server_ids = await add_servers(client)
    await run_job(app, client, "import")

    [issue] = await issues(client, "mismatch")
    d = issue["details"]
    assert "9 (2009) {tmdb-12244}" in d["path"]
    assert d["hints"]["tmdb_id"] == "12244"
    by_title = {c["title"]: c for c in d["candidates"]}
    assert set(by_title) == {"9", "9 Songs"}
    assert by_title["9 Songs"]["servers"] == [server_ids["plex"]]
    assert d["suggested"] == by_title["9"]["media_item_id"]
    assert d["confidence"] == "high"
    assert "2 of 3 servers agree" in by_title["9"]["reasons"]
    # Titles in a mismatch aren't also reported as missing.
    assert [i["title"] for i in await issues(client, "item_missing")] == ["Arrival (2016)"]

    bad = await client.post(
        f"/api/review/issues/{issue['id']}/fix-match", json={"target": {"tmdb_id": "abc"}}
    )
    assert bad.status_code == 422
    chosen = by_title["9"]
    resp = await client.post(
        f"/api/review/issues/{issue['id']}/fix-match",
        json={"target": {k: chosen[k] for k in ("tmdb_id", "imdb_id", "title", "year")}},
    )
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["fixed"] == 1

    plex_item = next(
        i for i in fake_servers["plex"].items.values() if "{tmdb-12244}" in (i.file or "")
    )
    assert (plex_item.title, plex_item.ids["tmdb"]) == ("9", "12244")
    assert await issues(client, "mismatch") == []
    libs = (await client.get("/api/libraries")).json()["libraries"]
    films = next(lib for lib in libs if lib["name"] == "Films")
    assert films["item_count"] == 11
    page = (await client.get("/api/items", params={"library_id": films["id"], "q": "9"})).json()
    nine = next(i for i in page["items"] if i["title"] == "9")
    assert sorted(s["server_id"] for s in nine["servers"]) == sorted(server_ids.values())


async def test_gentle_profile_and_show_paths(app, authed, fake_servers):
    from sqlalchemy import select

    from coverarr.db import session_factory
    from coverarr.models import ServerItem

    client = authed
    resp = await client.put("/api/settings", json={"server_load": "gentle"})
    assert resp.json()["server_load"] == "gentle"
    assert (await client.put("/api/settings", json={"server_load": "turbo"})).status_code == 422
    server_ids = await add_servers(client)
    job = await run_job(app, client, "import")
    assert job["result"]["errors"] == {}

    async with session_factory()() as session:
        plex_shows = await session.scalars(
            select(ServerItem).where(
                ServerItem.server_id == server_ids["plex"], ServerItem.kind == "show"
            )
        )
        # Plex listings carry no show folder; it's derived from the episode files.
        paths = sorted(s.path for s in plex_shows)
    assert paths == [
        "/data/media/TV/Black Books (2000) {tvdb-72216}",
        "/data/media/TV/Spaced (1999) {tvdb-75592}",
    ]


async def test_same_name_collections_stay_separate(app, authed, fake_servers):
    """A franchise and its "universe" on one server ("Jay and Silent Bob Collection" vs
    "Jay & Silent Bob") normalise to the same name but must stay separate collections."""
    from sqlalchemy import select, update

    from coverarr.db import session_factory
    from coverarr.models import Collection, ServerCollection

    client = authed
    plex = fake_servers["plex"]
    films = plex.libraries[0][0]
    plex.add_collection("Paddington", films, ["Paddington", "Paddington 2", "Elf", "Home Alone"])
    server_ids = await add_servers(client)
    await run_job(app, client, "import")

    async def check() -> None:
        libs = (await client.get("/api/libraries")).json()["libraries"]
        films_lib = next(lib for lib in libs if lib["name"] == "Films")
        colls = (
            await client.get("/api/collections", params={"library_id": films_lib["id"]})
        ).json()
        servers_of = {c["name"]: sorted(s["server_id"] for s in c["servers"]) for c in colls}
        assert servers_of["Paddington"] == [server_ids["plex"]]
        assert servers_of["Paddington Collection"] == sorted(server_ids.values())
        missing = {
            i["title"]: i["details"]["missing"] for i in await issues(client, "collection_missing")
        }
        assert missing["Paddington"] == sorted([server_ids["jellyfin"], server_ids["emby"]])
        differ = [i["title"] for i in await issues(client, "collection_members_differ")]
        assert not any(t.startswith("Paddington") for t in differ)

    await check()

    # Databases from before this fix have both Plex collections merged into one record.
    async with session_factory()() as session:
        target = await session.scalar(
            select(Collection.id).where(Collection.name == "Paddington Collection")
        )
        await session.execute(
            update(ServerCollection)
            .where(ServerCollection.name.in_(("Paddington", "Paddington Collection")))
            .values(collection_id=target)
        )
        await session.commit()
    await run_job(app, client, "rematch")
    await check()
