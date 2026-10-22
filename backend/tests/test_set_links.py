"""Artwork from Kometa/MediUX YAML with image URLs."""

import io

import respx
from PIL import Image
from sqlalchemy import select

from coverarr.db import session_factory
from coverarr.models import Background, MediaItem
from coverarr.services.set_links import parse_artwork_yaml
from tests.conftest import run_job
from tests.test_flow import add_servers


def _jpeg(color: str, size=(200, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


async def _poster_source(title: str) -> str | None:
    async with session_factory()() as session:
        return await session.scalar(select(MediaItem.poster_source).where(MediaItem.title == title))


def test_artwork_yaml_is_read():
    images = parse_artwork_yaml(
        """
metadata:
  4638:
    url_poster: https://api.mediux.pro/assets/a
    url_background: https://api.mediux.pro/assets/b
  75592:
    seasons:
      1:
        url_poster: https://api.mediux.pro/assets/c
        episodes:
          1:
            url_poster: https://api.mediux.pro/assets/d
"""
    )
    assert [(i.key, i.what, i.season, i.episode) for i in images] == [
        (4638, "poster", None, None),
        (4638, "background", None, None),
        (75592, "poster", 1, None),
        (75592, "poster", 1, 1),
    ]


async def test_mediux_yaml(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    text = """
metadata:
  4638:   # Hot Fuzz (TMDb)
    url_poster: https://api.mediux.pro/assets/poster
    url_background: https://api.mediux.pro/assets/wide
  "Shaun of the Dead (2004)":
    url_poster: http://insecure.example/poster.jpg
  75592:  # Spaced (TVDb)
    seasons:
      1:
        url_poster: https://api.mediux.pro/assets/s1
        episodes:
          1:
            url_poster: https://api.mediux.pro/assets/e1
  999999:
    url_poster: https://api.mediux.pro/assets/nothing
"""
    assert (
        await client.post("/api/poster-sets/yaml", json={"yaml": "- just a list"})
    ).status_code == 422
    with respx.mock(assert_all_called=False) as mock:
        for name, size in (
            ("poster", (200, 300)),
            ("wide", (640, 360)),
            ("s1", (200, 300)),
            ("e1", (640, 360)),
        ):
            mock.get(f"https://api.mediux.pro/assets/{name}").respond(
                content=_jpeg("purple", size), headers={"Content-Type": "image/jpeg"}
            )
        resp = await client.post("/api/poster-sets/yaml", json={"yaml": text})
        assert resp.status_code == 200, resp.text
        assert resp.json()["images"] == 6
        await app.state.jobs.wait_idle()
    job = (await client.get(f"/api/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["imported"] == 4, result
    assert result["unmatched_entries"] == ["999999"]
    assert result["failed"] == 1  # the http:// link is refused
    assert any("only downloads images over https" in line for line in job["log"])

    assert (await _poster_source("Hot Fuzz") or "").startswith("artwork:")
    assert await _poster_source("Shaun of the Dead") is None
    async with session_factory()() as session:
        fuzz = await session.scalar(select(MediaItem).where(MediaItem.title == "Hot Fuzz"))
        assert (fuzz.background_source or "").startswith("background:")
        assert await session.scalar(select(Background.source)) == "set"
        spaced = await session.scalar(
            select(MediaItem).where(MediaItem.title == "Spaced", MediaItem.kind == "show")
        )
        season = await session.scalar(
            select(MediaItem).where(MediaItem.parent_id == spaced.id, MediaItem.season_number == 1)
        )
        episode = await session.scalar(
            select(MediaItem).where(MediaItem.parent_id == season.id, MediaItem.episode_number == 1)
        )
        assert (season.poster_source or "").startswith("artwork:")
        assert (episode.poster_source or "").startswith("artwork:")
