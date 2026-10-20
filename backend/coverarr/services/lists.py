"""Fetching list sources (TMDb lists and collections, Trakt lists) as plain title entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from coverarr.clients.base import USER_AGENT

TMDB_API = "https://api.themoviedb.org/3"
TRAKT_API = "https://api.trakt.tv"
MAX_PAGES = 50


class ListError(Exception):
    pass


@dataclass(slots=True)
class ListEntry:
    kind: str  # movie | show
    title: str
    year: int | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None


def tmdb_id_from(value: Any, kind: str) -> str:
    """Accept ``8694016`` or ``https://www.themoviedb.org/list/8694016-comfort``."""
    text = str(value).strip()
    if text.isdigit():
        return text
    if m := re.search(rf"/{kind}/(\d+)", text):
        return m.group(1)
    raise ListError(f"Not a TMDb {kind} id or URL: {text}")


def trakt_list_path(url: str) -> str:
    """``https://trakt.tv/users/<user>/lists/<slug>`` -> ``users/<user>/lists/<slug>``."""
    if m := re.search(r"trakt\.tv/(users/[^/]+/lists/[^/?#]+)", url):
        return m.group(1)
    if m := re.search(r"trakt\.tv/lists/(\d+)", url):
        return f"lists/{m.group(1)}"
    raise ListError(f"Not a Trakt list URL: {url}")


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


class ListClient:
    def __init__(
        self,
        *,
        tmdb_key: str | None,
        trakt_client_id: str | None,
        trakt_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.tmdb_key = tmdb_key
        self.trakt_client_id = trakt_client_id
        self.trakt_token = trakt_token
        self._http = httpx.AsyncClient(
            timeout=30, headers={"User-Agent": USER_AGENT}, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        try:
            resp = await self._http.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise ListError(f"{url}: {exc}") from exc
        if resp.status_code == 401:
            raise ListError(f"{url}: API key rejected")
        if resp.status_code == 404:
            raise ListError(f"{url}: not found (private or deleted?)")
        if resp.status_code >= 400:
            raise ListError(f"{url}: HTTP {resp.status_code}")
        return resp

    # --- TMDb -------------------------------------------------------------------------------

    def _tmdb_auth(self) -> tuple[dict[str, str], dict[str, str]]:
        if not self.tmdb_key:
            raise ListError("Add a TMDb API key in Settings → General")
        # v4 read access tokens are long JWTs; v3 keys are 32 hex characters.
        if len(self.tmdb_key) > 40:
            return {"Authorization": f"Bearer {self.tmdb_key}"}, {}
        return {}, {"api_key": self.tmdb_key}

    async def tmdb_list(self, list_id: str) -> list[ListEntry]:
        headers, params = self._tmdb_auth()
        entries: list[ListEntry] = []
        page, pages = 1, 1
        while page <= min(pages, MAX_PAGES):
            data = (
                await self._get(
                    f"{TMDB_API}/list/{list_id}", headers=headers, params={**params, "page": page}
                )
            ).json()
            pages = int(data.get("total_pages") or 1)
            for item in data.get("items") or []:
                kind = "show" if item.get("media_type") == "tv" else "movie"
                entries.append(
                    ListEntry(
                        kind=kind,
                        title=item.get("title") or item.get("name") or "",
                        year=_year(item.get("release_date") or item.get("first_air_date")),
                        tmdb_id=str(item["id"]),
                    )
                )
            page += 1
        return entries

    async def tmdb_collection(self, collection_id: str) -> list[ListEntry]:
        headers, params = self._tmdb_auth()
        data = (
            await self._get(
                f"{TMDB_API}/collection/{collection_id}", headers=headers, params=params
            )
        ).json()
        return [
            ListEntry(
                kind="movie",
                title=part.get("title") or "",
                year=_year(part.get("release_date")),
                tmdb_id=str(part["id"]),
            )
            for part in data.get("parts") or []
        ]

    # --- Trakt ------------------------------------------------------------------------------

    async def trakt_list(self, url: str) -> list[ListEntry]:
        if not self.trakt_client_id:
            raise ListError("Add a Trakt client ID in Settings → General")
        headers = {"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id}
        if self.trakt_token:
            headers["Authorization"] = f"Bearer {self.trakt_token}"
        path = trakt_list_path(url)
        entries: list[ListEntry] = []
        page, pages = 1, 1
        while page <= min(pages, MAX_PAGES):
            resp = await self._get(
                f"{TRAKT_API}/{path}/items/movie,show",
                headers=headers,
                params={"page": page, "limit": 100},
            )
            pages = int(resp.headers.get("X-Pagination-Page-Count") or 1)
            for row in resp.json():
                kind = row.get("type")
                media = row.get(kind) or {}
                ids = media.get("ids") or {}
                entries.append(
                    ListEntry(
                        kind="show" if kind == "show" else "movie",
                        title=media.get("title") or "",
                        year=media.get("year"),
                        tmdb_id=str(ids["tmdb"]) if ids.get("tmdb") else None,
                        tvdb_id=str(ids["tvdb"]) if ids.get("tvdb") else None,
                        imdb_id=ids.get("imdb"),
                    )
                )
            page += 1
        return entries

    async def resolve(self, builder: dict[str, Any]) -> list[ListEntry]:
        kind, value = builder["type"], builder["value"]
        if kind == "tmdb_list":
            return await self.tmdb_list(tmdb_id_from(value, "list"))
        if kind == "tmdb_collection":
            return await self.tmdb_collection(tmdb_id_from(value, "collection"))
        if kind == "trakt_list":
            return await self.trakt_list(str(value))
        raise ListError(f"Unsupported source {kind}")
