"""Importing from Kometa: original posters (zip/folder) and collection YAML files."""

import io
import zipfile

import httpx
import respx

from tests.conftest import run_job
from tests.fakeserver import poster_png
from tests.test_flow import add_servers, issues

COMFORT_J = """\
collections:

  Comfort J Films:
    tmdb_list: 8694016
    sync_mode: sync
    collection_order: release.asc
    sort_title: "!010_Comfort_J"
    label: Comfort
    summary: "Personal comfort and feel good films."
"""

CHRISTMAS = """\
collections:

  Christmas Movies:
    trakt_list:
      - https://trakt.tv/users/k0meta/lists/christmas-extravaganza-non-tv-movie
      - https://trakt.tv/users/movistapp/lists/christmas-movies
    sync_mode: sync
    collection_order: release.asc
    sort_title: "!020_Christmas"
    label: Christmas
    summary: "Christmas and holiday films."
    template: {name: holiday}
"""

CONFIG = """\
plex:
  url: http://192.168.1.10:32400
  token: [token]
tmdb:
  apikey: [key]
trakt:
  client_id: abc123clientid
  client_secret: [secret]
libraries:
  Films:
    collection_files:
      - file: config/christmas.yml
      - file: config/comfort-j.yml
"""


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buf.getvalue()


async def test_kometa_originals_zip(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")
    await run_job(app, client, "poster_scan")
    assert [i["title"] for i in await issues(client, "poster_differs")] == ["Arrival (2016)"]

    plex = fake_servers["plex"]
    arrival = plex.by_title("Arrival")
    hot_fuzz = plex.by_title("Hot Fuzz")
    season = next(i for i in plex.items.values() if i.kind == "season")
    cornetto = next(c for c in plex.collections.values() if c.name == "Cornetto Collection")

    # The user already picked Jellyfin's poster for Hot Fuzz: Kometa must not override that.
    films = next(
        lib
        for lib in (await client.get("/api/libraries")).json()["libraries"]
        if lib["kind"] == "movie"
    )
    page = (
        await client.get("/api/items", params={"library_id": films["id"], "q": "Hot Fuzz"})
    ).json()
    hot_fuzz_id = page["items"][0]["id"]
    await client.put(
        f"/api/items/{hot_fuzz_id}/poster-source",
        json={"poster_source": f"server:{ids['jellyfin']}"},
    )

    archive = _zip(
        {
            f"Films Original Posters/{arrival.id}.png": poster_png("movie:Arrival"),
            f"Films Original Posters/{hot_fuzz.id}.jpg": poster_png("movie:Hot Fuzz"),
            f"TV Programmes Original Posters/{season.id}.png": poster_png("season"),
            f"Films Original Posters/{cornetto.id}.png": poster_png("coll"),
            "Films Original Posters/999999.png": poster_png("gone"),
            "Films Original Posters/readme.txt": b"not an image",
            f"Films Original Posters/{plex.by_title('Elf').id}.png": b"corrupt",
        }
    )
    resp = await client.post(
        "/api/kometa/originals/upload",
        files={"file": ("originals.zip", archive, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    r = job["result"]
    assert (r["imported"], r["unmatched"], r["not_original"], r["unreadable"]) == (4, 1, 1, 1)
    assert (r["applied"], r["kept_choice"]) == (3, 1)

    # Files land in the assets tree by what they belong to; the upload is removed afterwards.
    config_dir = app.state.config.config_dir
    assets = config_dir / "assets"
    files = sorted(str(p.relative_to(assets)) for p in assets.rglob("*") if p.is_file())
    assert "movies/Arrival (2016) {tmdb-329865}/poster-kometa.png" in files, files
    # Named by the image's real format, not the source file's extension (that one was a PNG).
    assert "movies/Hot Fuzz (2007) {tmdb-4638}/poster-kometa.png" in files
    assert "collections/Cornetto Collection/poster-kometa.png" in files
    show = season.show and fake_servers["plex"].items[season.show]
    show_folder = f"tv/{show.title} ({show.year}) {{tvdb-{show.ids['tvdb']}}}"
    assert f"{show_folder}/season{season.index:02d}-kometa.png" in files
    assert not list((config_dir / "imports").iterdir())

    # Arrival now uses the Kometa original as its base, so its poster issue is gone.
    assert await issues(client, "poster_differs") == []
    page = (
        await client.get("/api/items", params={"library_id": films["id"], "q": "Arrival"})
    ).json()
    poster = page["items"][0]["poster"]
    assert poster["source"] == "kometa" and page["items"][0]["poster_source"].startswith("artwork:")
    img = await client.get(f"/api/images/artwork/{poster['artwork_id']}", params={"w": 120})
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    detail = (await client.get(f"/api/items/{hot_fuzz_id}")).json()
    assert detail["poster_source"] == f"server:{ids['jellyfin']}"
    assert [a["source"] for a in detail["artwork"]] == ["kometa"]

    # Re-importing replaces files rather than piling up copies.
    resp = await client.post(
        "/api/kometa/originals/upload",
        files={
            "file": ("again.zip", _zip({f"{arrival.id}.png": poster_png("x")}), "application/zip")
        },
    )
    await app.state.jobs.wait_idle()
    arrival_dir = assets / "movies" / "Arrival (2016) {tmdb-329865}"
    assert [p.name for p in arrival_dir.iterdir()] == ["poster-kometa.png"]


async def test_kometa_originals_folder(app, authed, fake_servers, tmp_path):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    folder = tmp_path / "Films Original Posters"
    folder.mkdir()
    (folder / f"{fake_servers['plex'].by_title('Elf').id}.png").write_bytes(poster_png("elf"))
    bad = await client.post("/api/kometa/originals/folder", json={"path": "relative/path"})
    assert bad.status_code == 422
    missing = await client.post(
        "/api/kometa/originals/folder", json={"path": str(tmp_path / "nope")}
    )
    assert missing.status_code == 422
    resp = await client.post("/api/kometa/originals/folder", json={"path": str(tmp_path)})
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["result"]["imported"] == 1
    assert folder.exists()  # a folder the user pointed at is never deleted


async def test_kometa_yaml_import_and_check(app, authed, fake_servers):
    client = authed
    ids = await add_servers(client)
    await run_job(app, client, "import")

    files = [
        ("files", ("comfort-j.yml", COMFORT_J.encode(), "text/yaml")),
        ("files", ("christmas.yml", CHRISTMAS.encode(), "text/yaml")),
        ("files", ("config.yml", CONFIG.encode(), "text/yaml")),
        ("files", ("broken.yml", b"collections: [unclosed", "text/yaml")),
    ]
    resp = await client.post("/api/kometa/yaml", files=files)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["saved"] == 2 and out["unassigned"] == []
    assert len(out["errors"]) == 1 and "broken.yml" in out["errors"][0]
    # Redacted placeholders like [key] aren't imported; the real client id is.
    assert out["keys_imported"] == ["trakt_client_id"]

    defs = {d["name"]: d for d in (await client.get("/api/definitions")).json()}
    comfort, christmas = defs["Comfort J Films"], defs["Christmas Movies"]
    assert comfort["builders"] == [{"type": "tmdb_list", "value": "8694016"}]
    assert comfort["options"]["sort_title"] == "!010_Comfort_J"
    assert comfort["collection"]["name"] == "Comfort J Films"
    assert [b["type"] for b in christmas["builders"]] == ["trakt_list", "trakt_list"]
    assert christmas["unsupported"] == ["template"]

    await client.put("/api/settings", json={"tmdb_api_key": "0123456789abcdef0123456789abcdef"})
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.themoviedb.org/3/list/8694016").respond(
            json={
                "total_pages": 1,
                "items": [
                    {
                        "id": 116149,
                        "media_type": "movie",
                        "title": "Paddington",
                        "release_date": "2014-11-28",
                    },
                    {
                        "id": 346648,
                        "media_type": "movie",
                        "title": "Paddington 2",
                        "release_date": "2017-11-10",
                    },
                    {
                        "id": 10719,
                        "media_type": "movie",
                        "title": "Elf",
                        "release_date": "2003-10-09",
                    },
                    {
                        "id": 13,
                        "media_type": "movie",
                        "title": "Forrest Gump",
                        "release_date": "1994-06-23",
                    },
                ],
            }
        )
        trakt = mock.get(
            url__regex=r"https://api\.trakt\.tv/users/[^/]+/lists/[^/]+/items/movie,show"
        )
        trakt.side_effect = [
            httpx.Response(
                200,
                headers={"X-Pagination-Page-Count": "1"},
                json=[
                    {
                        "type": "movie",
                        "movie": {
                            "title": "Elf",
                            "year": 2003,
                            "ids": {"tmdb": 10719, "imdb": "tt0319343"},
                        },
                    },
                    {
                        "type": "movie",
                        "movie": {"title": "Home Alone", "year": 1990, "ids": {"tmdb": 771}},
                    },
                ],
            ),
            httpx.Response(404),
        ]
        resp = await client.post("/api/definitions/check", json={})
        await app.state.jobs.wait_idle()
        assert trakt.calls[0].request.headers["trakt-api-key"] == "abc123clientid"
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job

    defs = {d["name"]: d for d in (await client.get("/api/definitions")).json()}
    comfort = defs["Comfort J Films"]["check_result"]
    assert (comfort["total"], comfort["in_library"], comfort["missing_count"]) == (4, 3, 1)
    assert comfort["missing"] == [{"title": "Forrest Gump", "year": 1994}]
    assert comfort["collection"]["servers"][str(ids["plex"])] == {"in_collection": 3, "extra": 0}
    christmas = defs["Christmas Movies"]["check_result"]
    assert (christmas["total"], christmas["in_library"]) == (2, 2)
    assert len(christmas["errors"]) == 1 and "not found" in christmas["errors"][0]


async def test_yaml_without_config_uses_chosen_library(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    libs = (await client.get("/api/libraries")).json()["libraries"]
    tv = next(lib for lib in libs if lib["kind"] == "show")
    resp = await client.post(
        "/api/kometa/yaml",
        files=[("files", ("comfort-j.yml", COMFORT_J.encode(), "text/yaml"))],
        data={"library_id": str(tv["id"])},
    )
    assert resp.json()["saved"] == 1
    [definition] = (await client.get("/api/definitions")).json()
    assert definition["library_id"] == tv["id"]
    await client.delete(f"/api/definitions/{definition['id']}")
    assert (await client.get("/api/definitions")).json() == []


async def test_old_kometa_layout_is_moved(app, authed, fake_servers):
    """Artwork imported into assets/kometa by an earlier version moves into the new tree."""
    from sqlalchemy import select

    from coverarr.db import session_factory
    from coverarr.models import Artwork, MediaItem
    from coverarr.services.assets import relocate_artwork
    from coverarr.services.settings_store import set_setting

    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    config_dir = app.state.config.config_dir
    old = config_dir / "assets" / "kometa" / "m1 - Elf (2003).png"
    old.parent.mkdir(parents=True)
    old.write_bytes(poster_png("elf"))
    async with session_factory()() as session:
        elf = await session.scalar(select(MediaItem).where(MediaItem.title == "Elf"))
        session.add(
            Artwork(
                source="kometa",
                media_item_id=elf.id,
                file_path="assets/kometa/m1 - Elf (2003).png",
            )
        )
        await set_setting(session, "artwork_layout", 1)
        await session.commit()

    assert await relocate_artwork(session_factory(), config_dir) == 1
    new = config_dir / "assets" / "movies" / "Elf (2003) {tmdb-10719}" / "poster-kometa.png"
    assert new.exists() and not old.exists()
    assert not (config_dir / "assets" / "kometa").exists()  # emptied folder is removed
    async with session_factory()() as session:
        art = await session.scalar(select(Artwork))
        assert art.file_path == "assets/movies/Elf (2003) {tmdb-10719}/poster-kometa.png"
    # It only runs once.
    assert await relocate_artwork(session_factory(), config_dir) == 0
