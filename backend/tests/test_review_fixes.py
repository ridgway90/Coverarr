"""Regression tests for bugs found in the deep review."""

import asyncio
import io

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from coverarr.clients.base import RemoteLibrary
from coverarr.db import session_factory
from coverarr.models import Artwork, Collection, Library, Server, ServerItem, ServerLibrary
from coverarr.services.artwork import save_artwork
from coverarr.services.assets import relocate_artwork
from coverarr.services.importer import sync_libraries
from coverarr.services.kometa_yaml import KometaYamlError, parse_collection_file
from coverarr.services.overlays import rendered_path
from tests.conftest import run_job
from tests.test_arr import _jobs, _new_movie, _radarr, _settle, _setup
from tests.test_flow import add_servers


def _jpeg(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 150), color).save(buf, "JPEG")
    return buf.getvalue()


async def test_a_running_job_is_not_reused(app, authed):
    runner = app.state.jobs
    started, release = asyncio.Event(), asyncio.Event()
    runs = []

    async def slow(ctx):
        runs.append(ctx.job_id)
        started.set()
        await release.wait()

    runner.register("slow_dedupe", slow)
    first = await runner.enqueue("slow_dedupe")
    await asyncio.wait_for(started.wait(), 5)
    second = await runner.enqueue("slow_dedupe")
    third = await runner.enqueue("slow_dedupe")
    assert second.id != first.id
    assert third.id == second.id  # still waiting, so that one is reused
    release.set()
    await asyncio.wait_for(runner.wait_idle(), 5)
    assert runs == [first.id, second.id]


async def test_unknown_job_kind_fails_instead_of_hanging(app, authed):
    runner = app.state.jobs
    runner.register("gone_soon", lambda ctx: asyncio.sleep(0))
    job = await runner.enqueue("gone_soon")
    del runner.handlers["gone_soon"]
    await asyncio.wait_for(runner.wait_idle(), 5)
    assert (await authed.get(f"/api/jobs/{job.id}")).json()["status"] == "failed"
    assert not runner.live


async def test_disabling_a_server_library_unlinks_its_rows(app, authed, fake_servers):
    await add_servers(authed)
    await run_job(app, authed, "import")
    libs = (await authed.get("/api/libraries")).json()["libraries"]
    films = next(lib for lib in libs if lib["name"] == "Films")
    emby = next(sl for sl in films["server_libraries"] if sl["server_kind"] == "emby")
    resp = await authed.put(
        f"/api/server-libraries/{emby['id']}", json={"enabled": False, "library_id": films["id"]}
    )
    assert resp.status_code == 200
    await app.state.jobs.wait_idle()
    async with session_factory()() as session:
        linked = await session.scalar(
            select(func.count())
            .select_from(ServerItem)
            .where(ServerItem.server_library_id == emby["id"])
            .where(ServerItem.media_item_id.is_not(None))
        )
    assert linked == 0


async def test_new_server_libraries_claim_same_named_ones_first(app):
    async with session_factory()() as session:
        session.add(Library(name="Movies", kind="movie"))
        server = Server(kind="plex", name="p", url="http://x", token="t")
        session.add(server)
        await session.commit()
        server_id = server.id
    async with session_factory()() as session:
        server = await session.get(Server, server_id, options=[selectinload(Server.libraries)])
        await sync_libraries(
            session,
            server,
            [RemoteLibrary("1", "Kids Movies", "movie"), RemoteLibrary("2", "Movies", "movie")],
        )
        await session.commit()
        mapping = dict(
            (await session.execute(select(ServerLibrary.name, Library.name).join(Library))).all()
        )
    assert mapping == {"Movies": "Movies", "Kids Movies": "Kids Movies"}


async def test_new_title_doesnt_wait_on_a_servers_other_library(
    app, authed, fake_servers, monkeypatch
):
    plex = fake_servers["plex"]
    lib = plex.add_library("Kids Films", "movie")
    plex.locations[lib] = "/data/kids"
    path = await _setup(app, authed, monkeypatch)
    for srv in fake_servers.values():
        _new_movie(srv, "Hot Rod", 2007, "10074")
    await authed.post(
        path, json=_radarr("Hot Rod", 2007, "10074", "Hot Rod (2007) [WEBDL-1080p].mkv")
    )
    await _settle(app)
    statuses = [j["result"]["status"] for j in await _jobs(authed, "arr_event") if j["result"]]
    assert "waiting" not in statuses


async def test_tidy_never_moves_a_poster_onto_another_titles(app, tmp_path):
    config_dir = app.state.config.config_dir
    async with session_factory()() as session:
        c1 = Collection(name="Marvel", normalized_name="marvel")
        c2 = Collection(name="Marvel", normalized_name="marvel")
        session.add_all([c1, c2])
        await session.flush()
        for owner, color in ((c2, "red"), (c1, "blue"), (c2, "green")):
            await save_artwork(
                session, config_dir, source="upload", data=_jpeg(color), collection_id=owner.id
            )
            await session.commit()
        ids = {c1.id: "blue", c2.id: "green"}
    await relocate_artwork(session_factory(), config_dir, force=True)
    async with session_factory()() as session:
        arts = list(await session.scalars(select(Artwork)))
    assert len({a.file_path for a in arts}) == 2
    for art in arts:
        pixel = Image.open(config_dir / art.file_path).convert("RGB").getpixel((5, 5))
        want = {"blue": 2, "green": 1}[ids[art.collection_id]]
        assert max(range(3), key=lambda i: pixel[i]) == want


def test_yaml_alias_bomb_is_refused_quickly():
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 9):
        prev = f"*a{i - 1}"
        lines.append(f"a{i}: &a{i} [{', '.join([prev] * 10)}]")
    lines.append("collections:\n  Boom:\n    tmdb_list: *a8")
    with pytest.raises(KometaYamlError, match="anchors"):
        parse_collection_file("\n".join(lines), "boom.yml")


def test_renders_spread_over_many_folders(tmp_path):
    folders = {rendered_path(tmp_path, f"m{i}", "k").parent.name for i in range(1, 2000)}
    assert len(folders) == 256
