"""Trakt sign-in with PKCE: no client secret, tokens refreshed before they run out."""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import respx

from coverarr.services import trakt
from coverarr.services.settings_store import set_setting

REDIRECT = "https://coverarr.example/trakt/callback"


async def test_sign_in_refresh_and_disconnect(app, authed):
    client = authed
    assert (
        await client.post("/api/trakt/connect", json={"redirect_uri": REDIRECT})
    ).status_code == 422
    await client.put("/api/settings", json={"trakt_client_id": "abc123"})

    url = (await client.post("/api/trakt/connect", json={"redirect_uri": REDIRECT})).json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://auth.trakt.tv/oauth/authorize?")
    assert q["client_id"] == "abc123" and q["redirect_uri"] == REDIRECT
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"

    sent: list[dict] = []

    def token(request):
        body = json.loads(request.content)
        sent.append(body)
        assert "client_secret" not in body
        assert request.headers["trakt-api-key"] == "abc123"
        n = len(sent)
        return httpx.Response(
            200,
            json={
                "access_token": f"access{n}",
                "refresh_token": f"refresh{n}",
                "expires_in": 7 * 86400,
                "created_at": int(time.time()) - (7 * 86400 - 3600 if n == 1 else 0),
            },
        )

    with respx.mock() as mock:
        mock.post("https://api.trakt.tv/oauth/token").mock(side_effect=token)
        mock.get("https://api.trakt.tv/users/settings").respond(json={"user": {"username": "matt"}})

        # A sign-in started elsewhere (wrong state) is refused.
        bad = await client.post(
            "/api/trakt/callback", json={"response": f"{REDIRECT}?code=xyz&state=other"}
        )
        assert bad.status_code == 422
        # The address the browser landed on, pasted back.
        resp = await client.post(
            "/api/trakt/callback", json={"response": f"{REDIRECT}?code=xyz&state={q['state']}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["connected"] and resp.json()["username"] == "matt"
        first = sent[0]
        assert first["grant_type"] == "authorization_code" and first["code"] == "xyz"
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(first["code_verifier"].encode()).digest()
        )
        assert challenge.rstrip(b"=").decode() == q["code_challenge"]
        assert 43 <= len(first["code_verifier"]) <= 128

        # The token runs out within a day: it's refreshed and both tokens replaced.
        async with app.state.jobs.sessionmaker() as session:
            assert await trakt.access_token(session) == "access2"
            assert await trakt.access_token(session) == "access2"  # fresh now: no new request
        assert sent[1] == {
            "client_id": "abc123",
            "refresh_token": "refresh1",
            "redirect_uri": REDIRECT,
            "grant_type": "refresh_token",
        }
        assert len(sent) == 2

    # Lists are read with the token; disconnecting forgets it.
    from coverarr.services.definitions import _lists

    async with app.state.jobs.sessionmaker() as session:
        async with await _lists(session) as lists:
            assert lists.trakt_token == "access2"
    status = (await client.delete("/api/trakt")).json()
    assert not status["connected"]


def test_pasted_code_forms():
    assert trakt.code_from("https://x/cb?code=abc&state=s1") == ("abc", "s1")
    assert trakt.code_from("  abc123  ") == ("abc123", None)


async def test_rejected_refresh_after_expiry_signs_out(app, authed):
    async with app.state.jobs.sessionmaker() as session:
        await set_setting(session, "trakt_client_id", "abc123")
        await set_setting(session, "trakt_access_token", "old")
        await set_setting(session, "trakt_refresh_token", "used")
        await set_setting(session, "trakt_token_expires", int(time.time()) - 60)
        await session.commit()
        with respx.mock() as mock:
            mock.post("https://api.trakt.tv/oauth/token").respond(
                400, json={"error": "invalid_grant", "error_description": "session not found"}
            )
            assert await trakt.access_token(session) is None
        assert not (await trakt.status(session))["connected"]


async def test_refused_sign_in_falls_back_to_public_and_is_dropped(app, authed, fake_servers):
    """A sign-in Trakt no longer accepts (e.g. imported from Kometa) mustn't break public lists."""
    from tests.conftest import run_job
    from tests.test_flow import add_servers

    client = authed
    await add_servers(client)
    await run_job(app, client, "import")
    libs = (await client.get("/api/libraries")).json()["libraries"]
    films = next(lib["id"] for lib in libs if lib["kind"] == "movie")
    await client.put("/api/settings", json={"trakt_client_id": "abc123"})
    async with app.state.jobs.sessionmaker() as session:
        await set_setting(session, "trakt_access_token", "from-kometa")  # no expiry stored
        await session.commit()
    url = "https://trakt.tv/users/2borno2b/lists/christmas-movies-extravanganza"
    definition = (
        await client.post(
            "/api/definitions",
            json={
                "name": "Xmas",
                "library_id": films,
                "builders": [{"type": "trakt_list", "value": url}],
            },
        )
    ).json()

    def items(request):
        if "Authorization" in request.headers:
            return httpx.Response(401)
        movie = {"title": "Elf", "year": 2003, "ids": {"tmdb": 10719}}
        return httpx.Response(200, json=[{"type": "movie", "movie": movie}])

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://api\.trakt\.tv/users/2borno2b/.*").mock(side_effect=items)
        await client.post("/api/definitions/check", json={"ids": [definition["id"]]})
        await app.state.jobs.wait_idle()
    checked = next(
        d for d in (await client.get("/api/definitions")).json() if d["id"] == definition["id"]
    )
    assert checked["check_result"]["errors"] == []
    assert checked["check_result"]["in_library"] == 1
    assert not (await client.get("/api/trakt")).json()["connected"]
    job = next(
        j for j in (await client.get("/api/jobs")).json() if j["kind"] == "check_definitions"
    )
    log = (await client.get(f"/api/jobs/{job['id']}")).json()["log"]
    assert any("refused the saved sign-in" in line for line in log)

    # A wrong client ID says so, rather than "API key rejected".
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://api\.trakt\.tv/.*").respond(403)
        await client.post("/api/definitions/check", json={"ids": [definition["id"]]})
        await app.state.jobs.wait_idle()
    checked = next(
        d for d in (await client.get("/api/definitions")).json() if d["id"] == definition["id"]
    )
    assert "client ID" in checked["check_result"]["errors"][0]
