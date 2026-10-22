"""Metadata edits: set a title's name, sort title, summary and labels once, push everywhere."""

from tests.conftest import run_job
from tests.test_backgrounds import _push, _title
from tests.test_flow import add_servers


def _labels(srv, item):
    return item.labels if srv.kind == "plex" else item.tags


async def test_metadata_edits_are_pushed_and_kept(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    fuzz = await _title(client, "Hot Fuzz")
    fake_servers["plex"].by_title("Hot Fuzz").labels.append("Hand")  # set on Plex by hand

    resp = await client.put(
        f"/api/items/{fuzz['id']}/metadata",
        json={
            "title": "Hot Fuzz!",
            "sort_title": "Fuzz, Hot",
            "summary": "  Cops in the country.  ",
            "labels": "Cornetto, Kids",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["edits"] == {
        "title": "Hot Fuzz!",
        "sort_title": "Fuzz, Hot",
        "summary": "Cops in the country.",
        "labels": ["Cornetto", "Kids"],
    }
    pending = (await client.get("/api/changes/metadata")).json()
    assert pending["total"] == 1 and pending["uploads"] == 3
    assert {s["reason"] for s in pending["items"][0]["servers"]} == {"not pushed yet"}
    assert (await client.get("/api/changes/summary")).json()["metadata"] == 1

    result = await _push(app, client)
    assert result["metadata"] == 3 and not result.get("metadata_failed")
    for srv in fake_servers.values():
        item = next(i for i in srv.items.values() if i.title == "Hot Fuzz!")
        assert (item.sort_title, item.summary) == ("Fuzz, Hot", "Cops in the country."), srv.kind
        assert {"Cornetto", "Kids"} <= set(_labels(srv, item)), srv.kind
        assert "title" in item.locked or "Name" in item.locked, srv.kind
    assert (await client.get("/api/changes/metadata")).json()["total"] == 0
    # A fresh import sees the edits on every server: nothing to do.
    await run_job(app, client, "import")
    assert (await client.get("/api/changes/metadata")).json()["total"] == 0

    # Dropping a label takes it off; the hand-set one stays.
    await client.put(
        f"/api/items/{fuzz['id']}/metadata",
        json={"title": "Hot Fuzz!", "sort_title": "Fuzz, Hot", "labels": ["Cornetto"]},
    )
    await _push(app, client, media_item_ids=[fuzz["id"]])
    for srv in fake_servers.values():
        item = next(i for i in srv.items.values() if i.title == "Hot Fuzz!")
        assert "Kids" not in _labels(srv, item) and "Cornetto" in _labels(srv, item), srv.kind
    assert "Hand" in fake_servers["plex"].by_title("Hot Fuzz!").labels

    # Someone renames it on Plex: it's listed again.
    fake_servers["plex"].by_title("Hot Fuzz!").title = "Hot Fuzz"
    await run_job(app, client, "import")
    info = (await client.get(f"/api/items/{fuzz['id']}/metadata")).json()
    pending = {s["server_name"]: s["pending"] for s in info["servers"]}
    assert pending == {"Plex": "title changed on the server", "Jellyfin": None, "Emby": None}

    # Clearing every field stops managing it.
    await client.put(f"/api/items/{fuzz['id']}/metadata", json={"title": " ", "labels": []})
    info = (await client.get(f"/api/items/{fuzz['id']}/metadata")).json()
    assert info["edits"] == {}
    assert (await client.get("/api/changes/metadata")).json()["total"] == 0
