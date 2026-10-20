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
        "movie": 10,
        "show": 2,
        "season": 5,
        "episode": 32,
        "collection": 4,
    }

    # Plex "Films" and Jellyfin/Emby "Movies" land in one logical library.
    libs = (await client.get("/api/libraries")).json()
    assert sorted((lib["name"], lib["item_count"]) for lib in libs["libraries"]) == [
        ("Films", 10),
        ("TV Programmes", 2),
    ]
    assert all(len(lib["server_libraries"]) == 3 for lib in libs["libraries"])

    dash = (await client.get("/api/dashboard")).json()
    assert dash["counts"] == {"movie": 10, "show": 2, "season": 5, "episode": 32, "collection": 4}

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
