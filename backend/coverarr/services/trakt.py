"""Signing in to Trakt with OAuth PKCE, so no client secret is needed.

Public lists and charts only need the client ID; signing in lets Coverarr read private lists.
Access tokens last 7 days; refresh tokens are single-use, so each refresh stores the new pair.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients.base import USER_AGENT
from coverarr.services.settings_store import get_setting, set_setting

AUTHORIZE_URL = "https://auth.trakt.tv/oauth/authorize"
API = "https://api.trakt.tv"
PENDING_KEY = "trakt_pkce_pending"
# Refresh this long before the access token runs out.
REFRESH_MARGIN = 24 * 3600

# Tests swap in a mock transport.
transport: httpx.AsyncBaseTransport | None = None


class TraktAuthError(Exception):
    pass


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def start(session: AsyncSession, redirect_uri: str) -> str:
    """The Trakt sign-in URL; the verifier waits in the settings until the code comes back."""
    client_id = await get_setting(session, "trakt_client_id")
    if not client_id:
        raise TraktAuthError("Add your Trakt client ID first")
    redirect_uri = redirect_uri.strip()
    if not redirect_uri:
        raise TraktAuthError("Give the redirect URI set in your Trakt app")
    verifier = secrets.token_urlsafe(64)[:96]  # 43-128 of A-Z a-z 0-9 - _
    state = secrets.token_urlsafe(16)
    await set_setting(
        session,
        PENDING_KEY,
        {"verifier": verifier, "state": state, "redirect_uri": redirect_uri},
    )
    await session.commit()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def code_from(text: str) -> tuple[str, str | None]:
    """(code, state) from the address Trakt sent the browser to, or a bare code."""
    text = text.strip()
    if "code=" in text:
        params = parse_qs(urlparse(text).query or text.split("?", 1)[-1])
        if params.get("error"):
            raise TraktAuthError(f"Trakt said: {params['error'][0]}")
        return params["code"][0], (params.get("state") or [None])[0]
    if not text or " " in text:
        raise TraktAuthError("Paste the address you landed on after allowing Coverarr")
    return text, None


async def _token_request(session: AsyncSession, body: dict[str, Any]) -> dict[str, Any]:
    client_id = await get_setting(session, "trakt_client_id")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "trakt-api-version": "2",
        "trakt-api-key": client_id or "",
    }
    async with httpx.AsyncClient(timeout=30, transport=transport) as http:
        try:
            resp = await http.post(
                f"{API}/oauth/token", json={"client_id": client_id, **body}, headers=headers
            )
        except httpx.HTTPError as exc:
            raise TraktAuthError(f"Couldn't reach Trakt: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error_description") or resp.json().get("error")
        except ValueError:
            detail = resp.text[:200]
        raise TraktAuthError(f"Trakt refused the sign-in ({resp.status_code}): {detail}")
    return resp.json()


async def _store(session: AsyncSession, tokens: dict[str, Any]) -> None:
    created = int(tokens.get("created_at") or time.time())
    await set_setting(session, "trakt_access_token", tokens["access_token"])
    await set_setting(session, "trakt_refresh_token", tokens.get("refresh_token"))
    await set_setting(session, "trakt_token_expires", created + int(tokens.get("expires_in") or 0))


async def _username(session: AsyncSession, access_token: str) -> str | None:
    client_id = await get_setting(session, "trakt_client_id")
    headers = {
        "User-Agent": USER_AGENT,
        "trakt-api-version": "2",
        "trakt-api-key": client_id or "",
        "Authorization": f"Bearer {access_token}",
    }
    async with httpx.AsyncClient(timeout=30, transport=transport) as http:
        try:
            resp = await http.get(f"{API}/users/settings", headers=headers)
            return ((resp.json().get("user") or {}).get("username")) if resp.is_success else None
        except (httpx.HTTPError, ValueError):
            return None


async def finish(session: AsyncSession, pasted: str) -> dict[str, Any]:
    """Swap the code Trakt returned for tokens, proving it with the stored verifier."""
    pending = await get_setting(session, PENDING_KEY)
    if not pending:
        raise TraktAuthError("Start again with Connect Trakt")
    code, state = code_from(pasted)
    if state is not None and state != pending["state"]:
        raise TraktAuthError("That sign-in doesn't match the one started here; start again")
    tokens = await _token_request(
        session,
        {
            "code": code,
            "code_verifier": pending["verifier"],
            "redirect_uri": pending["redirect_uri"],
            "grant_type": "authorization_code",
        },
    )
    await _store(session, tokens)
    await set_setting(session, "trakt_redirect_uri", pending["redirect_uri"])
    await set_setting(session, PENDING_KEY, None)
    await set_setting(session, "trakt_username", await _username(session, tokens["access_token"]))
    await session.commit()
    return await status(session)


async def access_token(session: AsyncSession) -> str | None:
    """A usable access token, refreshed first when it's about to run out; None when not
    signed in (public lists still work with the client ID alone)."""
    token = await get_setting(session, "trakt_access_token")
    refresh = await get_setting(session, "trakt_refresh_token")
    expires = await get_setting(session, "trakt_token_expires")
    if not token or not refresh or not expires or expires - time.time() > REFRESH_MARGIN:
        return token
    body: dict[str, Any] = {
        "refresh_token": refresh,
        "redirect_uri": await get_setting(session, "trakt_redirect_uri")
        or "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "refresh_token",
    }
    # Tokens from before PKCE (e.g. imported from Kometa) were issued with the secret.
    if secret := await get_setting(session, "trakt_client_secret"):
        body["client_secret"] = secret
    try:
        tokens = await _token_request(session, body)
    except TraktAuthError:
        # Refresh tokens are single-use; once rejected, signing in again is the only way.
        if expires < time.time():
            await disconnect(session)
            return None
        return token
    await _store(session, tokens)
    await session.commit()
    return tokens["access_token"]


async def disconnect(session: AsyncSession) -> None:
    for key in ("trakt_access_token", "trakt_refresh_token", "trakt_token_expires"):
        await set_setting(session, key, None)
    await set_setting(session, "trakt_username", None)
    await session.commit()


async def status(session: AsyncSession) -> dict[str, Any]:
    return {
        "client_id": bool(await get_setting(session, "trakt_client_id")),
        "connected": bool(await get_setting(session, "trakt_access_token")),
        "username": await get_setting(session, "trakt_username"),
        "expires_at": await get_setting(session, "trakt_token_expires"),
        "redirect_uri": await get_setting(session, "trakt_redirect_uri"),
        "pending": bool(await get_setting(session, PENDING_KEY)),
    }
