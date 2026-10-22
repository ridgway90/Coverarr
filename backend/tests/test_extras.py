"""Streaming services, ratings, show status and people from TMDb; award and seasonal
collections; the new overlay badges; Plex home-screen visibility."""

import io
from datetime import date

import httpx
import pytest
import respx
from PIL import Image
from sqlalchemy import select

from coverarr.db import session_factory
from coverarr.models import Collection, MediaItem
from coverarr.services import definitions as definitions_service
from coverarr.services import lists as lists_service
from coverarr.services.details import parse_mdblist, parse_tmdb
from coverarr.services.library_filter import matches, parse_filter
from coverarr.services.lists import ListError, award_query, award_value
from coverarr.services.mediainfo import audio_key
from coverarr.services.overlays import badge_names, build_plan, fill_template
from coverarr.services.seasons import ScheduleError, format_schedule, in_season, parse_schedule
from tests.conftest import run_job
from tests.test_definitions_sync import KEY, _coll, _films, _sync, _titles
from tests.test_flow import add_servers

# --- units -----------------------------------------------------------------------------------


def test_tmdb_details_are_read():
    movie = {
        "vote_average": 7.734,
        "vote_count": 9000,
        "runtime": 121,
        "production_countries": [{"iso_3166_1": "GB", "name": "United Kingdom"}],
        "credits": {
            "crew": [{"job": "Director", "name": "Edgar Wright"}, {"job": "Writer", "name": "X"}],
            "cast": [{"name": f"Actor {n}"} for n in range(15)],
        },
        "watch/providers": {
            "results": {
                "GB": {
                    "flatrate": [
                        {"provider_name": "Disney Plus", "display_priority": 5},
                        {"provider_name": "Netflix", "display_priority": 1},
                    ],
                    "rent": [{"provider_name": "Apple TV", "display_priority": 0}],
                },
                "US": {"flatrate": [{"provider_name": "Peacock", "display_priority": 1}]},
            }
        },
    }
    out = parse_tmdb(movie, "movie", "gb", date(2026, 10, 22))
    assert out["streaming"] == ["Netflix", "Disney Plus"]  # streaming only, by priority
    assert (out["tmdb_rating"], out["runtime"], out["directors"]) == (7.7, 121, ["Edgar Wright"])
    assert out["countries"] == ["United Kingdom"] and len(out["cast"]) == 10

    show = {
        "status": "Returning Series",
        "next_episode_to_air": {"air_date": "2026-10-25"},
        "episode_run_time": [24],
        "created_by": [{"name": "Simon Pegg"}],
        "aggregate_credits": {"cast": [{"name": "Jessica Hynes"}]},
    }
    out = parse_tmdb(show, "show", "GB", date(2026, 10, 22))
    assert out["status"] == "airing" and out["streaming"] == []
    assert out["directors"] == ["Simon Pegg"] and out["cast"] == ["Jessica Hynes"]
    later = parse_tmdb(
        {**show, "next_episode_to_air": {"air_date": "2027-03-01"}},
        "show",
        "GB",
        date(2026, 10, 22),
    )
    assert later["status"] == "returning"
    assert parse_tmdb({"status": "Canceled"}, "show", "GB", date.today())["status"] == "canceled"
    assert "tmdb_rating" not in parse_tmdb({"vote_count": 0}, "movie", "GB", date.today())

    ratings = parse_mdblist(
        {
            "ratings": [
                {"source": "imdb", "value": 7.8},
                {"source": "tomatoes", "value": 91},
                {"source": "metacritic", "value": None},
                {"source": "trakt", "value": 80},
            ]
        }
    )
    assert ratings == {"imdb_rating": 7.8, "rt_rating": 91}


def test_seasons():
    assert format_schedule(parse_schedule("range(12/1-12/31)")) == "range(12/01-12/31)"
    assert in_season("range(12/01-12/31)", date(2026, 12, 25))
    assert not in_season("range(12/01-12/31)", date(2026, 11, 30))
    # Wrapping the new year, and several ranges.
    assert in_season("range(12/26-01/05)", date(2027, 1, 3))
    assert in_season("range(12/26-01/05)", date(2026, 12, 31))
    assert not in_season("range(12/26-01/05)", date(2026, 6, 1))
    assert in_season("range(02/01-02/14)|range(10/01-10/31)", date(2026, 10, 9))
    assert in_season(None, date(2026, 6, 1))
    for bad in ("weekly(sunday)", "range(13/01-12/31)", "range(02/30-03/01)", ""):
        with pytest.raises(ScheduleError):
            parse_schedule(bad)


def test_award_values_and_queries():
    assert award_value("Oscars_Best_Picture") == "oscars_best_picture"
    assert award_value("Q102427:nominated") == "Q102427:nominated"
    for bad in ("nope", "oscars:lost"):
        with pytest.raises(ListError):
            award_value(bad)
    single = award_query("oscars_best_picture", "movie")
    assert "?f wdt:P166 wd:Q102427 ;" in single and "wd:Q11424" in single
    ceremony = award_query("bafta:nominated", "movie")
    # Any award of the ceremony; the film's own properties stay on the film.
    assert "?a wdt:P31 wd:Q732997 . ?f wdt:P1411 ?a ; wdt:P345 ?imdb" in ceremony
    assert "wd:Q5398426" in award_query("emmys_best_drama", "show")


def test_audio_codecs():
    assert audio_key("Dune [Bluray-2160p][TrueHD Atmos 7.1].mkv") == "truehd_atmos"
    assert audio_key("X [WEBDL-1080p][EAC3 Atmos 5.1].mkv") == "dd+_atmos"
    assert audio_key("X [DTS-HD MA 5.1].mkv") == "dts-hd_ma"
    assert audio_key("X [DTS-X 7.1].mkv") == "dts-x"
    assert audio_key("X [DDP5.1].mkv") == "dd+"
    assert audio_key("X [AAC 2.0].mkv") == "aac"
    assert audio_key("Hot Fuzz (2007) [WEBDL-1080p].mkv") is None


def test_detail_filters():
    facts = {
        "genres": ["Sci-Fi"],
        "streaming": ["Netflix"],
        "directors": ["Edgar Wright"],
        "cast": ["Simon Pegg"],
        "countries": ["United Kingdom"],
        "imdb_rating": 7.8,
        "status": "ended",
    }
    assert matches(parse_filter("genre=Sci-Fi"), 2007, facts)  # a hyphen isn't a range here
    assert matches(parse_filter("streaming=netflix, director=Edgar Wright"), 2007, facts)
    assert matches(parse_filter("actor=Simon Pegg, country=United Kingdom"), 2007, facts)
    assert matches(parse_filter("imdb_rating>=7.5, status=ended"), 2007, facts)
    assert not matches(parse_filter("imdb_rating>=8"), 2007, facts)
    assert matches(parse_filter("imdb_rating=7-8"), 2007, facts)
    assert not matches(parse_filter("tmdb_rating>=1"), 2007, facts)  # not fetched


def test_badges_and_rating_text(tmp_path):
    for rel in (
        "streaming/Disney Plus.png",
        "ribbon/oscars_best_picture.png",
        "ribbon/imdb_top_250.png",
        "status/ended.png",
        "audio_codec/TrueHD-Atmos.png",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(path)

    def badge(kind, folder, **extra):
        return {
            "type": kind,
            "folder": folder,
            "enabled": True,
            "h_align": "left",
            "v_align": "top",
            "h_offset": 0,
            "v_offset": 0,
            "scale": 1.0,
            **extra,
        }

    layers = [
        badge("streaming", "streaming"),
        badge("ribbon", "ribbon", ribbons=["imdb_top_250", "oscars_best_picture"]),
        badge("status", "status"),
        badge("audio", "audio_codec"),
    ]
    facts = {
        "streaming": ["Netflix", "Disney Plus"],  # no Netflix logo: the next service's
        "ribbons": ["oscars_best_picture"],
        "status": "ended",
        "audio": "truehd_atmos",
    }
    plan = build_plan(tmp_path, "show", layers, facts)
    assert [p.path.rsplit("/", 1)[1] for p in plan.layers] == [
        "Disney Plus.png",
        "oscars_best_picture.png",
        "ended.png",
        "TrueHD-Atmos.png",
    ]
    assert not plan.missing
    # The ribbon order is the layer's, not the title's.
    both = {"ribbons": ["oscars_best_picture", "imdb_top_250"]}
    assert badge_names(layers[1], both) == ["imdb_top_250", "oscars_best_picture"]
    assert build_plan(tmp_path, "show", layers, {}).layers == []

    assert fill_template("IMDb {imdb_rating}", {"imdb_rating": "7.8"}) == "IMDb 7.8"
    assert fill_template("IMDb {imdb_rating}", {}) == ""  # no rating: no line at all
    assert fill_template("S{season:02} {title}", {"season": 1}) == "S01"


# --- end to end ------------------------------------------------------------------------------

GENRE = "Comedy"
STREAMING = {"4638": ["Netflix"], "747": ["Netflix", "Disney Plus"], "1581": ["Disney Plus"]}
DIRECTORS = {"4638": "Edgar Wright", "747": "Edgar Wright", "107985": "Edgar Wright"}


def _tmdb(mock) -> None:
    def movie(request):
        tmdb_id = request.url.path.rsplit("/", 1)[1]
        providers = [
            {"provider_name": p, "display_priority": n}
            for n, p in enumerate(STREAMING.get(tmdb_id, []))
        ]
        return httpx.Response(
            200,
            json={
                "id": int(tmdb_id),
                "vote_average": 7.0 + len(tmdb_id) / 10,
                "vote_count": 100,
                "runtime": 100,
                "credits": {
                    "crew": [{"job": "Director", "name": DIRECTORS[tmdb_id]}]
                    if tmdb_id in DIRECTORS
                    else [],
                    "cast": [{"name": "Simon Pegg"}] if tmdb_id in DIRECTORS else [],
                },
                "watch/providers": {"results": {"GB": {"flatrate": providers}}},
            },
        )

    mock.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+(\?|$)").mock(side_effect=movie)
    mock.get(url__regex=r"https://api\.themoviedb\.org/3/find/75592").respond(
        json={"tv_results": [{"id": 4000}]}
    )
    mock.get(url__regex=r"https://api\.themoviedb\.org/3/find/\d+").respond(json={"tv_results": []})
    mock.get(url__regex=r"https://api\.themoviedb\.org/3/tv/4000").respond(
        json={
            "status": "Ended",
            "watch/providers": {"results": {"GB": {"flatrate": [{"provider_name": "Channel 4"}]}}},
        }
    )


async def test_details_feed_collections_and_ribbons(app, authed, fake_servers, monkeypatch):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films = await _films(client)

    # No TMDb key: nothing to do, but not a failure.
    job = await run_job(app, client, "fetch_details")
    assert job["result"]["skipped"] == "no TMDb key"

    assert (await client.put("/api/settings", json={"watch_region": "united"})).status_code == 422
    await client.put("/api/settings", json={"tmdb_api_key": KEY, "watch_region": "gb"})
    assert (await client.get("/api/settings")).json()["watch_region"] == "GB"

    # A ribbon layer asks for Best Picture winners, from Wikidata.
    overlays = (await client.get("/api/overlays")).json()
    lib = next(x for x in overlays["libraries"] if x["kind"] == "movie")
    ribbon = {
        "type": "ribbon",
        "enabled": True,
        "folder": "ribbon",
        "ribbons": ["oscars_best_picture"],
        "h_align": "right",
        "v_align": "bottom",
        "h_offset": 0,
        "v_offset": 0,
        "scale": 1,
    }
    resp = await client.put(
        f"/api/overlays/libraries/{lib['id']}",
        json={"enabled": False, "levels": {"movie": [*lib["levels"]["movie"], ribbon]}},
    )
    assert resp.status_code == 200, resp.text
    queries = []

    def fake_sparql(query):
        queries.append(query)
        return {
            "results": {
                "bindings": [{"imdb": {"value": "tt0425112"}, "label": {"value": "Hot Fuzz"}}]
            }
        }

    monkeypatch.setattr(lists_service, "sparql", fake_sparql)

    with respx.mock(assert_all_called=False) as mock:
        _tmdb(mock)
        job = await run_job(app, client, "fetch_details")
    result = job["result"]
    assert result["fetched"] >= 10 and result["failed"] == 0, result
    assert result["ribbons"] == 1 and len(queries) == 1

    async with session_factory()() as session:
        rows = {
            m.title: m
            for m in await session.scalars(
                select(MediaItem).where(MediaItem.kind.in_(("movie", "show")))
            )
        }
    fuzz = rows["Hot Fuzz"].details
    assert fuzz["streaming"] == ["Netflix"] and fuzz["directors"] == ["Edgar Wright"]
    assert fuzz["ribbons"] == ["oscars_best_picture"] and fuzz["tmdb_rating"] == 7.4
    assert rows["Spaced"].details["status"] == "ended"
    assert rows["Spaced"].details["streaming"] == ["Channel 4"]
    assert "status" not in (rows["Black Books"].details or {})  # no TMDb match

    # Fetched a minute ago: not again, unless forced.
    with respx.mock(assert_all_called=False) as mock:
        _tmdb(mock)
        again = await run_job(app, client, "fetch_details")
        assert again["result"]["considered"] == 0

    # Wikidata down: titles keep their ribbon.
    def down(query):
        raise ListError("Wikidata is limiting requests just now; try again later")

    monkeypatch.setattr(lists_service, "sparql", down)
    with respx.mock(assert_all_called=False) as mock:
        _tmdb(mock)
        await run_job(app, client, "fetch_details")
    async with session_factory()() as session:
        fuzz_row = await session.scalar(select(MediaItem).where(MediaItem.title == "Hot Fuzz"))
        assert fuzz_row.details["ribbons"] == ["oscars_best_picture"]

    # The title page shows them.
    item = (await client.get(f"/api/items/{rows['Hot Fuzz'].id}")).json()
    assert item["details"]["streaming"] == ["Netflix"]

    # A collection of what's on Netflix, straight from the library.
    netflix = (
        await client.post(
            "/api/definitions",
            json={
                "name": "On Netflix",
                "library_id": films,
                "builders": [{"type": "library_filter", "value": "streaming=Netflix"}],
            },
        )
    ).json()
    synced = await _sync(app, client, netflix["id"])
    assert synced["sync_result"]["in_library"] == 2
    plex = fake_servers["plex"]
    assert sorted(_titles(plex, _coll(plex, "On Netflix"))) == ["Hot Fuzz", "Shaun of the Dead"]

    # Kometa's streaming and director defaults, from the fetched details.
    config = (
        "libraries:\n  Films:\n    collection_files:\n"
        "      - default: streaming\n      - default: director\n      - default: actor\n"
    )
    out = (
        await client.post(
            "/api/kometa/yaml", files=[("files", ("config.yml", config.encode(), "text/yaml"))]
        )
    ).json()
    assert out["defaults"]["Films"]["notes"] == []
    defs = {d["name"]: d for d in (await client.get("/api/definitions")).json()}
    assert defs["Netflix"]["builders"] == [{"type": "library_filter", "value": "streaming=Netflix"}]
    assert "Disney Plus" in defs
    assert defs["Edgar Wright"]["builders"] == [
        {"type": "library_filter", "value": "director=Edgar Wright"}
    ]
    assert defs["Simon Pegg"]["builders"] == [
        {"type": "library_filter", "value": "actor=Simon Pegg"}
    ]


class _Today(date):
    when = date(2026, 12, 10)

    @classmethod
    def today(cls):
        return cls.when


async def test_seasonal_collection_comes_and_goes(app, authed, fake_servers, monkeypatch):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    films = await _films(client)
    monkeypatch.setattr(definitions_service, "date", _Today)

    bad = await client.post(
        "/api/definitions",
        json={
            "name": "Christmas",
            "library_id": films,
            "builders": [{"type": "tmdb_keyword", "value": "Christmas"}],
            "options": {"schedule": "every december"},
        },
    )
    assert bad.status_code == 422
    christmas = (
        await client.post(
            "/api/definitions",
            json={
                "name": "Christmas",
                "library_id": films,
                "builders": [{"type": "tmdb_keyword", "value": "Christmas"}],
                "options": {
                    "schedule": "range(12/1-12/31)",
                    "delete_not_scheduled": True,
                    "collection_order": "alpha",
                },
            },
        )
    ).json()
    assert christmas["options"]["schedule"] == "range(12/01-12/31)"
    assert christmas["builders"] == [{"type": "tmdb_keyword", "value": "christmas"}]

    def tmdb(mock):
        mock.get(url__regex=r"https://api\.themoviedb\.org/3/search/keyword").respond(
            json={
                "results": [{"id": 207317, "name": "christmas"}, {"id": 1, "name": "christmas eve"}]
            }
        )
        mock.get(
            url__regex=r"https://api\.themoviedb\.org/3/discover/movie.*with_keywords=207317"
        ).respond(
            json={
                "total_pages": 1,
                "results": [
                    {"id": 10719, "title": "Elf", "release_date": "2003-10-09"},
                    {"id": 771, "title": "Home Alone", "release_date": "1990-11-16"},
                    {"id": 1581, "title": "The Holiday", "release_date": "2006-12-08"},
                ],
            }
        )

    with respx.mock(assert_all_called=False) as mock:
        tmdb(mock)
        synced = await _sync(app, client, christmas["id"])
    assert synced["sync_result"]["in_library"] == 3, synced["sync_result"]
    for srv in fake_servers.values():
        assert sorted(_titles(srv, _coll(srv, "Christmas"))) == ["Elf", "Home Alone", "The Holiday"]

    # January: off every server, but Coverarr keeps the collection (and its poster).
    _Today.when = date(2027, 1, 10)
    synced = await _sync(app, client, christmas["id"])
    assert synced["sync_result"]["out_of_season"]
    servers = synced["sync_result"]["servers"]
    assert len(servers) == 3 and all(v == {"deleted": True} for v in servers.values())
    for srv in fake_servers.values():
        assert _coll(srv, "Christmas") is None
    async with session_factory()() as session:
        assert await session.scalar(select(Collection.id).where(Collection.name == "Christmas"))

    # Next December it's back.
    _Today.when = date(2027, 12, 1)
    with respx.mock(assert_all_called=False) as mock:
        tmdb(mock)
        synced = await _sync(app, client, christmas["id"])
    assert synced["sync_result"].get("created") is None  # the same collection
    for srv in fake_servers.values():
        assert _coll(srv, "Christmas") is not None


async def test_plex_home_screen_visibility(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    await client.put("/api/settings", json={"tmdb_api_key": KEY})
    films = await _films(client)
    definition = (
        await client.post(
            "/api/definitions",
            json={
                "name": "Noughties",
                "library_id": films,
                "builders": [{"type": "library_filter", "value": "year=2004-2013"}],
                "options": {"visible_home": True, "visible_shared": False},
            },
        )
    ).json()
    synced = await _sync(app, client, definition["id"])
    assert not [n for n in synced["sync_result"]["notes"] if "visibility" in n], synced[
        "sync_result"
    ]
    plex = fake_servers["plex"]
    coll = _coll(plex, "Noughties")
    hub = next(v for k, v in plex.hubs.items() if k.endswith(f".{coll.id}"))
    # Library recommendations weren't set in Coverarr, so they keep Plex's value (off).
    assert hub == {
        "promotedToRecommended": "0",
        "promotedToOwnHome": "1",
        "promotedToSharedHome": "0",
    }
    # Jellyfin and Emby have no such setting; nothing is attempted there.
    assert not fake_servers["jellyfin"].hubs


async def test_award_collection(app, authed, fake_servers, monkeypatch):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    films = await _films(client)
    monkeypatch.setattr(
        lists_service,
        "sparql",
        lambda q: {
            "results": {
                "bindings": [
                    {
                        "imdb": {"value": "tt0099785"},
                        "label": {"value": "Home Alone"},
                        "year": {"value": "1990"},
                    },
                    {
                        "imdb": {"value": "tt0111161"},
                        "label": {"value": "Not Yours"},
                        "year": {"value": "1994"},
                    },
                ]
            }
        },
    )
    definition = (
        await client.post(
            "/api/definitions",
            json={
                "name": "Winners",
                "library_id": films,
                "builders": [{"type": "wikidata_award", "value": "oscars"}],
            },
        )
    ).json()
    synced = await _sync(app, client, definition["id"])
    result = synced["sync_result"]
    assert (result["total"], result["in_library"]) == (2, 1)
    assert result["missing"] == [{"title": "Not Yours", "year": 1994}]


def test_wikidata_answers_are_cached(tmp_path, monkeypatch):
    from coverarr.services import overlays

    monkeypatch.setattr(overlays, "_config_dir", tmp_path)
    calls = []

    def fetch(query):
        calls.append(query)
        if len(calls) > 1:
            raise ListError("Wikidata is limiting requests just now; try again later")
        return {"results": {"bindings": []}}

    monkeypatch.setattr(lists_service, "_sparql_fetch", fetch)
    assert lists_service.sparql("Q") == {"results": {"bindings": []}}
    assert lists_service.sparql("Q") == {"results": {"bindings": []}}  # from the cache
    assert len(calls) == 1
    # A week on, Wikidata refusing still leaves the old answer.
    cache = next((tmp_path / "cache" / "wikidata").iterdir())
    import os

    os.utime(cache, (0, 0))
    assert lists_service.sparql("Q") == {"results": {"bindings": []}}
    assert len(calls) == 2
    with pytest.raises(ListError):
        lists_service.sparql("another")


async def test_overlay_preview_shows_streaming_logo(app, authed, fake_servers):
    """The details reach the renderer: a streaming logo is drawn from them."""
    from tests.test_overlays import _png

    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pack/streaming/Netflix.png", _png((200, 100), (255, 0, 0, 255)))
        z.writestr("pack/gradient_top.png", _png((1000, 1500)))
    await client.post(
        "/api/overlays/pack", files={"file": ("pack.zip", buf.getvalue(), "application/zip")}
    )
    async with session_factory()() as session:
        fuzz = await session.scalar(select(MediaItem).where(MediaItem.title == "Hot Fuzz"))
        fuzz.details = {"streaming": ["Netflix"], "imdb_rating": 7.8}
        await session.commit()
        fuzz_id = fuzz.id
    layer = {
        "type": "streaming",
        "enabled": True,
        "folder": "streaming",
        "h_align": "left",
        "v_align": "top",
        "h_offset": 0,
        "v_offset": 0,
        "scale": 1,
    }
    live = await client.post(
        "/api/overlays/preview", json={"media_item_id": fuzz_id, "layers": [layer], "width": 1000}
    )
    assert live.status_code == 200, live.text
    img = Image.open(io.BytesIO(live.content)).convert("RGB").resize((1000, 1500))
    r, g, b = img.getpixel((50, 50))
    assert r > 200 and g < 60 and b < 60
