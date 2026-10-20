"""Media facts for overlays: file names, server reports, and rolling episodes up to shows."""

from coverarr.services.mediainfo import (
    aggregate,
    edition_key,
    from_filename,
    merge,
    resolution_key,
)
from tests.conftest import run_job
from tests.test_flow import add_servers


def test_filename_facts():
    name = "/m/Dune (2021) {tmdb-438631} {edition-IMAX} [Bluray-2160p][DV HDR10][TrueHD 7.1].mkv"
    assert from_filename(name) == {
        "resolution": "4k",
        "dv": True,
        "hdr": True,
        "edition": "IMAX",
    }
    assert from_filename("x [WEBDL-1080p][HDR10Plus].mkv") == {
        "resolution": "1080p",
        "hdr10plus": True,
        "hdr": True,
    }
    # Words that merely contain the letters don't count.
    assert from_filename("/m/Advent (2019)/Advent [HDTV-720p].mkv") == {"resolution": "720p"}
    assert from_filename(None) == {}


def test_keys():
    assert resolution_key({"resolution": "4k", "dv": True, "hdr": True}) == "4kdvhdr"
    assert resolution_key({"resolution": "4k", "dv": True, "hdr": True, "hdr10plus": True}) == (
        "4kdvhdrplus"
    )
    assert resolution_key({"resolution": "1080p", "hdr": True, "hdr10plus": True}) == "1080pplus"
    assert resolution_key({"resolution": "720p"}) == "720p"
    assert resolution_key({"dv": True}) == "dv"
    assert resolution_key({}) is None
    assert edition_key("Director's Cut") == "directors"
    assert edition_key("Richard Donner Cut") == "richarddonner"
    assert edition_key("Extended Edition") == "extended"
    assert edition_key("Something Else") is None


def test_merge_and_aggregate():
    facts = merge([{"resolution": "1080p"}, {"resolution": "4k", "hdr": True}], "a [2160p].mkv")
    assert facts == {
        "resolution": "1080p",  # the first server wins
        "edition": None,
        "network": None,
        "hdr": True,
        "dv": False,
        "hdr10plus": False,
    }
    show = aggregate(
        [{"resolution": "720p"}, {"resolution": "4k", "dv": True}], {"network": "Channel 4"}
    )
    assert show["resolution"] == "4k" and show["dv"] and show["network"] == "Channel 4"


async def test_facts_from_import(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    from sqlalchemy import select

    from coverarr.db import session_factory
    from coverarr.models import MediaItem

    async with session_factory()() as session:
        rows = {
            (m.kind, m.title, m.season_number): m.facts
            for m in await session.scalars(select(MediaItem))
        }
    arrival = rows[("movie", "Arrival", None)]
    assert (arrival["resolution"], arrival["hdr"], arrival["dv"]) == ("4k", True, True)
    # Plex knows HDR but not HDR10+; the file name adds it.
    paddington = rows[("movie", "Paddington", None)]
    assert (paddington["resolution"], paddington["hdr10plus"]) == ("4k", True)
    assert rows[("movie", "Elf", None)]["edition"] == "Director's Cut"
    assert rows[("movie", "Home Alone", None)]["resolution"] == "720p"
    spaced = rows[("show", "Spaced", None)]
    assert (spaced["network"], spaced["resolution"]) == ("Channel 4", "4k")
    season1 = next(f for (k, t, n), f in rows.items() if k == "season" and n == 1 and f)
    assert season1["resolution"] == "720p"


async def test_plex_ignoring_hdr_filter(app, authed, fake_servers):
    """A Plex whose ?hdr=1 / ?dovi=1 match every film (as if it ignored them) must not put an
    HDR badge on everything: the filters are dropped and the file names decide."""
    from sqlalchemy import select

    from coverarr.db import session_factory
    from coverarr.models import MediaItem

    for item in fake_servers["plex"].items.values():
        if item.kind == "movie":
            item.video.update(hdr=True, dv=True)
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    async with session_factory()() as session:
        facts = {
            m.title: m.facts
            for m in await session.scalars(select(MediaItem).where(MediaItem.kind == "movie"))
        }
    assert facts["Elf"]["hdr"] is False and facts["Elf"]["dv"] is False
    assert facts["Arrival"]["dv"] is True  # "[DV HDR10]" in its file name
