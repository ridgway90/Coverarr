"""Refreshing one title or collection, and removing a collection member, without an import."""

from tests.conftest import run_job
from tests.test_flow import add_servers, issues


async def _job(app, client, resp) -> dict:
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    return job


async def _films(client) -> dict:
    libs = (await client.get("/api/libraries")).json()["libraries"]
    return next(lib for lib in libs if lib["kind"] == "movie")


async def _item(client, library_id: int, title: str) -> dict:
    page = (await client.get("/api/items", params={"library_id": library_id, "q": title})).json()
    return next(i for i in page["items"] if i["title"] == title)


async def _collection(client, library_id: int, name: str) -> dict:
    colls = (await client.get("/api/collections", params={"library_id": library_id})).json()
    return next(c for c in colls if c["name"] == name)


async def test_remove_member_everywhere(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    films = await _films(client)
    comfort = await _collection(client, films["id"], "Comfort J Films")
    elf = await _item(client, films["id"], "Elf")

    job = await _job(
        app,
        client,
        await client.post(
            f"/api/collections/{comfort['id']}/remove-member", json={"media_item_id": elf["id"]}
        ),
    )
    assert job["result"]["removed"] == 2  # Plex and Jellyfin; Emby doesn't have this collection
    for kind in ("plex", "jellyfin"):
        srv = fake_servers[kind]
        coll = next(c for c in srv.collections.values() if c.name == "Comfort J Films")
        assert "Elf" not in [srv.items[m].title for m in coll.members]
    detail = (await client.get(f"/api/collections/{comfort['id']}")).json()
    assert "Elf" not in [m["title"] for m in detail["members"]]
    assert sorted(s["member_count"] for s in detail["servers"]) == [2, 2]
    assert ids  # servers stay connected


async def test_refresh_collection(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    films = await _films(client)
    paddington = await _collection(client, films["id"], "Paddington Collection")

    # Someone edits the collection on Plex and deletes it on Emby, outside Coverarr.
    plex, emby = fake_servers["plex"], fake_servers["emby"]
    plex_coll = next(c for c in plex.collections.values() if c.name == "Paddington Collection")
    plex_coll.members.append(plex.by_title("Elf").id)
    emby_coll = next(c for c in emby.collections.values() if c.name == "Paddington Collection")
    del emby.collections[emby_coll.id]

    job = await _job(app, client, await client.post(f"/api/collections/{paddington['id']}/refresh"))
    assert "Plex: 3 items (was 2)" in job["result"]["changes"]
    assert "Emby: collection no longer exists there" in job["result"]["changes"]
    detail = (await client.get(f"/api/collections/{paddington['id']}")).json()
    counts = {s["server_id"]: s["member_count"] for s in detail["servers"]}
    assert counts == {ids["plex"]: 3, ids["jellyfin"]: 2}
    missing = {
        i["title"]: i["details"]["missing"] for i in await issues(client, "collection_missing")
    }
    assert missing["Paddington Collection"] == [ids["emby"]]
    differ = {i["title"] for i in await issues(client, "collection_members_differ")}
    assert "Paddington Collection" in differ


async def test_refresh_movie_and_show(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    await run_job(app, client, "poster_scan")
    films = await _films(client)
    assert [i["title"] for i in await issues(client, "poster_differs")] == ["Arrival (2016)"]

    # Jellyfin gets new artwork for Hot Fuzz; Emby loses Elf entirely.
    jf, emby = fake_servers["jellyfin"], fake_servers["emby"]
    jf.by_title("Hot Fuzz").alt_art = True
    jf.by_title("Hot Fuzz").art_version += 1
    emby.items.pop(emby.by_title("Elf").id)

    hot_fuzz = await _item(client, films["id"], "Hot Fuzz")
    job = await _job(app, client, await client.post(f"/api/items/{hot_fuzz['id']}/refresh"))
    assert any(line.endswith("poster updated") for line in job["result"]["changes"])
    # The new poster is fingerprinted right away: no poster scan needed.
    assert "Hot Fuzz (2007)" in [i["title"] for i in await issues(client, "poster_differs")]

    elf = await _item(client, films["id"], "Elf")
    job = await _job(app, client, await client.post(f"/api/items/{elf['id']}/refresh"))
    assert any("no longer on this server" in line for line in job["result"]["changes"])
    missing = {i["title"]: i["details"]["missing"] for i in await issues(client, "item_missing")}
    assert missing["Elf (2003)"] == [ids["emby"]]

    # Shows bring their seasons and episodes: Plex gains an episode, Emby loses one.
    libs = (await client.get("/api/libraries")).json()["libraries"]
    tv = next(lib for lib in libs if lib["kind"] == "show")
    spaced = await _item(client, tv["id"], "Spaced")
    plex = fake_servers["plex"]
    show = plex.by_title("Spaced")
    season2 = next(i for i in plex.items.values() if i.parent == show.id and i.index == 2)
    from tests.fakeserver import Item

    plex.add(
        Item(
            plex.new_id(),
            "episode",
            "Spaced 2x8",
            season2.library,
            parent=season2.id,
            show=show.id,
            index=8,
            parent_index=2,
            art_seed="ep:Spaced:2:8",
            file=f"{show.file}/Season 02/Spaced - S02E08.mkv",
        )
    )
    emby_show = emby.by_title("Spaced")
    gone = next(i for i in emby.items.values() if i.show == emby_show.id and i.kind == "episode")
    emby.items.pop(gone.id)

    await _job(app, client, await client.post(f"/api/items/{spaced['id']}/refresh"))
    detail = (await client.get(f"/api/items/{spaced['id']}")).json()
    s2 = next(s for s in detail["seasons"] if s["season_number"] == 2)
    assert s2["episodes"][str(ids["plex"])] == 8
    s1 = next(s for s in detail["seasons"] if s["season_number"] == 1)
    assert s1["episodes"][str(ids["emby"])] == 6


async def test_refresh_validation(app, authed, fake_servers):
    client = authed
    assert (await client.post("/api/items/999/refresh")).status_code == 404
    assert (await client.post("/api/collections/999/refresh")).status_code == 404
    resp = await client.post("/api/collections/999/remove-member", json={"media_item_id": 1})
    assert resp.status_code == 404
