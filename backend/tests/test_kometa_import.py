"""The rest of Kometa's config: templates, plex_search, default collections and overlays,
metadata files, asset folders, and the TMDb discover / MDBList / AniList builders."""

import io
import zipfile

import httpx
import respx
from PIL import Image
from sqlalchemy import select

from coverarr.db import session_factory
from coverarr.models import CollectionDefinition, MediaItem
from coverarr.services.artwork import parse_set_name
from coverarr.services.kometa_yaml import parse_collection_file, parse_config
from coverarr.services.library_filter import matches, parse_filter
from coverarr.services.lists import ListClient, discover_query, mdblist_path, normalize_builder
from coverarr.services.set_links import parse_metadata_edits
from tests.conftest import run_job
from tests.test_definitions_sync import KEY, _coll, _films, _sync, _titles
from tests.test_flow import add_servers

GENRES = {
    "Shaun of the Dead": ["Comedy", "Horror"],
    "Hot Fuzz": ["Comedy", "Action"],
    "The World's End": ["Comedy", "Science Fiction"],
    "Elf": ["Comedy", "Family"],
    "Home Alone": ["Comedy", "Family"],
    "Arrival": ["Science Fiction", "Drama"],
}


def _jpeg(color: str, size=(200, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buf.getvalue()


def _give_genres(fake_servers) -> None:
    for srv in fake_servers.values():
        for item in srv.items.values():
            if item.kind == "movie" and item.title in GENRES:
                item.genres = list(GENRES[item.title])
                item.studio = (
                    "Working Title" if item.title in ("Hot Fuzz", "Shaun of the Dead") else None
                )


# --- parsing -------------------------------------------------------------------------------------


def test_templates_are_expanded():
    text = """
templates:
  Studio:
    default:
      order: release.asc
    optional: [summary_text]
    tmdb_collection: <<collection>>
    collection_order: <<order>>
    summary: <<summary_text>>
    sort_title: "!<<collection_name>>"
collections:
  Cornetto:
    template: {name: Studio, collection: 10}
    collection_order: alpha
  Nothing:
    template: {name: Missing}
"""
    cornetto, nothing = parse_collection_file(text, "t.yml")
    assert cornetto.builders == [{"type": "tmdb_collection", "value": "10"}]
    # The collection's own keys win over the template's.
    assert cornetto.options["collection_order"] == "alpha"
    assert cornetto.options["sort_title"] == "!Cornetto"
    assert "summary" not in cornetto.options  # optional and not given
    assert cornetto.unsupported == []
    assert nothing.warnings[0] == "Template 'Missing' isn't in this file"


def test_plex_search_becomes_library_filters():
    text = """
collections:
  90s Comedies:
    plex_search:
      all:
        genre: Comedy
        decade: 1990
        genre.not: Horror
  Everything Recent:
    plex_all: true
    filters:
      year.gte: 2015
"""
    comedies, recent = parse_collection_file(text, "s.yml")
    assert comedies.builders == [
        {"type": "library_filter", "value": "genre=Comedy, decade=1990, genre!=Horror"}
    ]
    assert recent.builders[0]["type"] == "library_filter"


def test_library_filter_matching():
    facts = {"genres": ["Comedy", "Action"], "studio": "Working Title", "resolution": "1080p"}
    assert matches(parse_filter("genre=comedy, studio=Working Title"), 2007, facts)
    assert matches(parse_filter("year=2000-2009"), 2007, facts)
    assert not matches(parse_filter("genre!=Action"), 2007, facts)
    assert matches(parse_filter("decade=2000, year>=2005"), 2007, facts)
    assert not matches(parse_filter("year<=2000"), 2007, facts)
    assert not matches(parse_filter("year>=2000"), None, facts)


def test_config_parts_are_read():
    config = parse_config(
        """
settings:
  asset_directory: [config/assets]
mdblist:
  apikey: mdb-key
libraries:
  Films:
    collection_files:
      - default: imdb
      - default: Genre
      - file: config/films.yml
    overlay_files:
      - default: resolution
      - default: ribbon
    metadata_files:
      - file: config/films-meta.yml
""",
        "config.yml",
    )
    assert config.defaults_by_library == {"Films": ["imdb", "genre"]}
    assert config.files_by_library == {"Films": ["films.yml"]}
    assert [o["default"] for o in config.overlays_by_library["Films"]] == ["resolution", "ribbon"]
    assert config.metadata_by_library == {"Films": ["films-meta.yml"]}
    assert config.asset_directories == ["config/assets"]
    assert config.mdblist_key == "mdb-key"


def test_asset_folder_names():
    poster = parse_set_name("assets/Hot Fuzz (2007)/poster.jpg")
    assert (poster.title, poster.year, poster.what, poster.season) == (
        "Hot Fuzz",
        2007,
        "poster",
        None,
    )
    assert parse_set_name("Hot Fuzz (2007)/background.png").what == "background"
    assert parse_set_name("Hot Fuzz (2007)_background.jpg").what == "background"
    season = parse_set_name("Spaced (1999)/Season02.jpg")
    assert (season.season, season.episode) == (2, None)
    episode = parse_set_name("Spaced (1999)_S01E03.jpg")
    assert (episode.title, episode.season, episode.episode) == ("Spaced", 1, 3)
    # Ordinary set names are unchanged.
    assert parse_set_name("set/Spaced (1999) - Season 1.jpg").season == 1


def test_metadata_file_edits():
    edits = parse_metadata_edits(
        """
metadata:
  Hot Fuzz (2007):
    sort_title: Cornetto 2
    label: Cornetto, Favourites
    url_poster: https://example.com/p.jpg
  747:
    summary: "Zombies."
  Nothing:
    url_poster: https://example.com/x.jpg
"""
    )
    assert edits == [
        ("Hot Fuzz (2007)", {"sort_title": "Cornetto 2", "labels": ["Cornetto", "Favourites"]}),
        (747, {"summary": "Zombies."}),
    ]


def test_new_builder_values():
    assert mdblist_path(
        "https://mdblist.com/lists/linaspurinis/top-watched-movies-of-the-week"
    ) == ("linaspurinis/top-watched-movies-of-the-week")
    assert discover_query({"with_genres": [27, 53], "sort_by": "popularity.desc", "limit": 50}) == (
        "with_genres=27,53&sort_by=popularity.desc&limit=50"
    )
    assert normalize_builder("anilist_popular", 30) == {"type": "anilist_popular", "value": "30"}
    text = """
collections:
  Horror:
    tmdb_discover:
      with_genres: 27
      limit: 10
  Letterboxd:
    letterboxd_list: https://letterboxd.com/dave/list/official-top-250-narrative-feature-films/
"""
    horror, letterboxd = parse_collection_file(text, "d.yml")
    assert horror.builders == [{"type": "tmdb_discover", "value": "with_genres=27&limit=10"}]
    assert letterboxd.unsupported == ["letterboxd_list"]


async def test_discover_mdblist_and_anilist_lists():
    with respx.mock(assert_all_called=True) as mock:
        mock.get(url__regex=r"https://api\.themoviedb\.org/3/discover/movie").respond(
            json={
                "total_pages": 1,
                "results": [
                    {"id": 747, "title": "Shaun of the Dead", "release_date": "2004-04-09"}
                ],
            }
        )
        mock.get(url__regex=r"https://api\.mdblist\.com/lists/me/cornetto/items").respond(
            json={
                "movies": [
                    {
                        "id": 4638,
                        "imdb_id": "tt0425112",
                        "title": "Hot Fuzz",
                        "release_year": 2007,
                        "mediatype": "movie",
                    }
                ],
                "shows": [{"id": 1, "tvdb_id": 75592, "title": "Spaced", "mediatype": "show"}],
            }
        )
        mock.get(
            url__regex=r"https://raw\.githubusercontent\.com/Kometa-Team/Anime-IDs/.*"
        ).respond(
            json={
                "1": {"anilist_id": 21, "tvdb_id": 81797},
                "2": {"anilist_id": 199, "tmdb_movie_id": 129},
            }
        )
        mock.post("https://graphql.anilist.co").respond(
            json={
                "data": {
                    "Page": {
                        "pageInfo": {"hasNextPage": False},
                        "media": [
                            {
                                "id": 21,
                                "format": "TV",
                                "title": {"english": "One Piece"},
                                "startDate": {"year": 1999},
                            },
                            {
                                "id": 199,
                                "format": "MOVIE",
                                "title": {"romaji": "Sen to Chihiro"},
                                "startDate": {"year": 2001},
                            },
                        ],
                    }
                }
            }
        )
        async with ListClient(tmdb_key=KEY, trakt_client_id=None, mdblist_key="m") as lists:
            found = await lists.resolve({"type": "tmdb_discover", "value": "with_genres=27"})
            assert [e.tmdb_id for e in found] == ["747"]
            found = await lists.resolve({"type": "mdblist_list", "value": "me/cornetto"})
            assert [(e.tmdb_id, e.imdb_id) for e in found] == [("4638", "tt0425112")]
            shows = await lists.resolve({"type": "anilist_popular", "value": "10"}, "show")
            assert [(e.title, e.tvdb_id) for e in shows] == [("One Piece", "81797")]
            films = await lists.resolve({"type": "anilist_popular", "value": "10"}, "movie")
            assert [(e.title, e.tmdb_id) for e in films] == [("Sen to Chihiro", "129")]


# --- end to end ------------------------------------------------------------------------------


CONFIG = """\
tmdb:
  apikey: [key]
libraries:
  Films:
    collection_files:
      - default: genre
      - default: studio
      - default: imdb
      - default: franchise
      - default: seasonal
      - default: universe
    overlay_files:
      - default: resolution
        template_variables:
          use_edition: false
      - default: ribbon
      - default: status
      - default: languages
    metadata_files:
      - file: config/films-meta.yml
  Nowhere:
    collection_files:
      - default: year
"""


async def test_defaults_overlays_and_franchises_from_config(app, authed, fake_servers):
    client = authed
    _give_genres(fake_servers)
    await add_servers(client)
    await run_job(app, client, "import")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    films = await _films(client)

    def movie(request):
        tmdb_id = request.url.path.rsplit("/", 1)[1]
        cornetto = {"id": 9999, "name": "Three Flavours Cornetto"}
        paddington = {"id": 746, "name": "Paddington Collection"}
        part_of = {"747": cornetto, "4638": cornetto, "107985": cornetto, "116149": paddington}
        return httpx.Response(
            200, json={"id": int(tmdb_id), "belongs_to_collection": part_of.get(tmdb_id)}
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+(\?|$)").mock(
            side_effect=movie
        )
        resp = await client.post(
            "/api/kometa/yaml",
            files=[("files", ("config.yml", CONFIG.encode(), "text/yaml"))],
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        await app.state.jobs.wait_idle()

    report = out["defaults"]["Films"]
    assert report["unsupported"] == ["universe"] and report["franchise"]
    assert out["unknown_libraries"] == ["Nowhere"]
    assert out["metadata_files"] == {"Films": ["films-meta.yml"]}
    overlays = out["overlays"]["Films"]
    # use_edition: false; the ribbon layer is added; films have no show status.
    assert overlays["applied"] == ["movie resolution", "movie ribbon"]
    assert overlays["unsupported"] == ["languages", "status"]
    assert out["keys_imported"] == []  # "[key]" placeholders aren't keys

    defs = {d["name"]: d for d in (await client.get("/api/definitions")).json()}
    # One per genre and studio the library has, plus the IMDb charts.
    for name in ("Comedy", "Family", "Science Fiction", "Horror", "Working Title", "IMDb Top 250"):
        assert name in defs, sorted(defs)
    assert defs["Comedy"]["builders"] == [{"type": "library_filter", "value": "genre=Comedy"}]
    assert all(defs[n]["library_id"] == films for n in ("Comedy", "IMDb Top 250"))
    # Franchises with two or more of your films; Paddington has only one tagged here.
    assert defs["Three Flavours Cornetto"]["builders"] == [
        {"type": "tmdb_collection", "value": "9999"}
    ]
    assert "Paddington Collection" not in defs
    christmas = defs["Christmas"]
    assert christmas["builders"] == [{"type": "tmdb_keyword", "value": "christmas"}]
    assert christmas["options"]["schedule"] == "range(12/01-12/31)"
    assert christmas["options"]["delete_not_scheduled"] is True

    # A genre collection syncs from the library itself, no list needed.
    synced = await _sync(app, client, defs["Family"]["id"])
    assert synced["sync_result"]["in_library"] == 2
    plex = fake_servers["plex"]
    assert sorted(_titles(plex, _coll(plex, "Family"))) == ["Elf", "Home Alone"]

    # Importing again doesn't duplicate anything, and overlay settings now exist so are kept.
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+(\?|$)").mock(
            side_effect=movie
        )
        again = await client.post(
            "/api/kometa/yaml", files=[("files", ("config.yml", CONFIG.encode(), "text/yaml"))]
        )
        await app.state.jobs.wait_idle()
    assert again.json()["overlays"]["Films"]["kept_existing"]
    async with session_factory()() as session:
        names = list(await session.scalars(select(CollectionDefinition.name)))
    assert len(names) == len(set(names))


async def test_metadata_file_edits_are_saved(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    text = """
metadata:
  4638:
    sort_title: Cornetto 2
    label: [Cornetto]
  Shaun of the Dead (2004):
    summary: Zombies in Crouch End.
"""
    resp = await client.post("/api/poster-sets/yaml", json={"yaml": text})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"job_id": resp.json()["job_id"], "images": 0, "edits": 2}
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded" and job["result"]["edited"] == 2, job
    async with session_factory()() as session:
        fuzz = await session.scalar(select(MediaItem).where(MediaItem.title == "Hot Fuzz"))
        shaun = await session.scalar(
            select(MediaItem).where(MediaItem.title == "Shaun of the Dead")
        )
    assert fuzz.metadata_edits == {"sort_title": "Cornetto 2", "labels": ["Cornetto"]}
    assert shaun.metadata_edits == {"summary": "Zombies in Crouch End."}
    pending = (await client.get("/api/changes/metadata")).json()
    assert pending["total"] == 2
    assert (await client.post("/api/poster-sets/yaml", json={"yaml": "a: 1"})).status_code == 422


async def test_asset_folders_import_as_a_poster_set(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    archive = _zip(
        {
            "assets/Hot Fuzz (2007)/poster.jpg": _jpeg("red"),
            "assets/Hot Fuzz (2007)/background.jpg": _jpeg("blue", (640, 360)),
            "assets/Spaced (1999)/Season01.jpg": _jpeg("green"),
            "assets/Spaced (1999)/S01E02.jpg": _jpeg("yellow", (640, 360)),
            "assets/Spaced (1999)_S02E01.jpg": _jpeg("white", (640, 360)),
        }
    )
    resp = await client.post(
        "/api/poster-sets/upload", files={"file": ("assets.zip", archive, "application/zip")}
    )
    assert resp.status_code == 200, resp.text
    await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["imported"] == 5 and job["result"]["unmatched"] == 0, job["result"]
    async with session_factory()() as session:
        fuzz = await session.scalar(select(MediaItem).where(MediaItem.title == "Hot Fuzz"))
        assert (fuzz.poster_source or "").startswith("artwork:")
        assert (fuzz.background_source or "").startswith("background:")
        spaced = await session.scalar(
            select(MediaItem).where(MediaItem.title == "Spaced", MediaItem.kind == "show")
        )
        seasons = {
            s.season_number: s
            for s in await session.scalars(
                select(MediaItem).where(MediaItem.parent_id == spaced.id)
            )
        }
        assert (seasons[1].poster_source or "").startswith("artwork:")
        for season, number in ((1, 2), (2, 1)):
            episode = await session.scalar(
                select(MediaItem).where(
                    MediaItem.parent_id == seasons[season].id, MediaItem.episode_number == number
                )
            )
            assert (episode.poster_source or "").startswith("artwork:"), (season, number)
