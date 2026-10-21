"""Fetching list sources (TMDb lists, collections and charts, Trakt lists and charts) as plain
title entries.

Builder names follow Kometa's, so collection files imported from Kometa keep working."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

from coverarr.clients.base import USER_AGENT

TMDB_API = "https://api.themoviedb.org/3"
TRAKT_API = "https://api.trakt.tv"
MAX_PAGES = 50
DEFAULT_CHART_LIMIT = 20
MAX_CHART_LIMIT = 500

# Builder -> TMDb path per library kind (None: not available for that kind).
TMDB_CHARTS: dict[str, dict[str, str | None]] = {
    "tmdb_popular": {"movie": "/movie/popular", "show": "/tv/popular"},
    "tmdb_top_rated": {"movie": "/movie/top_rated", "show": "/tv/top_rated"},
    "tmdb_trending_daily": {"movie": "/trending/movie/day", "show": "/trending/tv/day"},
    "tmdb_trending_weekly": {"movie": "/trending/movie/week", "show": "/trending/tv/week"},
    "tmdb_now_playing": {"movie": "/movie/now_playing", "show": None},
    "tmdb_upcoming": {"movie": "/movie/upcoming", "show": None},
    "tmdb_airing_today": {"movie": None, "show": "/tv/airing_today"},
    "tmdb_on_the_air": {"movie": None, "show": "/tv/on_the_air"},
}
# Builder -> Trakt path under /movies or /shows.
TRAKT_CHARTS: dict[str, dict[str, str | None]] = {
    "trakt_trending": {"movie": "trending", "show": "trending"},
    "trakt_popular": {"movie": "popular", "show": "popular"},
    "trakt_anticipated": {"movie": "anticipated", "show": "anticipated"},
    "trakt_watched_weekly": {"movie": "watched/weekly", "show": "watched/weekly"},
    "trakt_boxoffice": {"movie": "boxoffice", "show": None},
}
LIST_BUILDERS = ("tmdb_list", "tmdb_collection", "trakt_list")
ID_BUILDERS = ("tmdb_movie", "tmdb_show")
BUILDERS = (*LIST_BUILDERS, *ID_BUILDERS, *TMDB_CHARTS, *TRAKT_CHARTS)


def chart_limit(value: Any) -> int:
    """Kometa writes a chart's size as its value, e.g. ``tmdb_popular: 30``."""
    try:
        limit = int(str(value).strip() or DEFAULT_CHART_LIMIT)
    except ValueError as exc:
        raise ListError(f"Expected a number of titles, got {value!r}") from exc
    return max(1, min(limit, MAX_CHART_LIMIT))


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


def normalize_builder(kind: str, value: Any) -> dict[str, str]:
    """Check a list source and store its value in one form (ids, or a chart's size)."""
    if kind not in BUILDERS:
        raise ListError(f"Unknown list source {kind}")
    if kind == "trakt_list":
        trakt_list_path(str(value))
        return {"type": kind, "value": str(value).strip()}
    if kind in TMDB_CHARTS or kind in TRAKT_CHARTS:
        return {"type": kind, "value": str(chart_limit(value))}
    noun = {"tmdb_show": "tv", "tmdb_movie": "movie"}.get(kind) or kind.split("_", 1)[1]
    return {"type": kind, "value": tmdb_id_from(value, noun)}


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
        # Rate limited (429) or a passing server error: wait and try again a few times.
        for attempt in range(4):
            try:
                resp = await self._http.get(url, **kwargs)
            except httpx.HTTPError as exc:
                raise ListError(f"{url}: {exc}") from exc
            if resp.status_code not in (429, 502, 503, 504) or attempt == 3:
                break
            try:
                wait = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                wait = 0.0
            await asyncio.sleep(min(max(wait, 1.0 * 2**attempt), 30.0))
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

    async def tmdb_json(self, path: str, **params: Any) -> dict[str, Any]:
        headers, auth = self._tmdb_auth()
        return (
            await self._get(f"{TMDB_API}{path}", headers=headers, params={**auth, **params})
        ).json()

    async def download(self, url: str) -> bytes:
        return (await self._get(url)).content

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

    async def tmdb_chart(self, path: str, kind: str, limit: int) -> list[ListEntry]:
        entries: list[ListEntry] = []
        page = 1
        while len(entries) < limit and page <= MAX_PAGES:
            data = await self.tmdb_json(path, page=page, language="en-US")
            for item in data.get("results") or []:
                entries.append(
                    ListEntry(
                        kind=kind,
                        title=item.get("title") or item.get("name") or "",
                        year=_year(item.get("release_date") or item.get("first_air_date")),
                        tmdb_id=str(item["id"]),
                    )
                )
            if page >= int(data.get("total_pages") or 1):
                break
            page += 1
        return entries[:limit]

    async def tmdb_title(self, tmdb_id: str, kind: str) -> ListEntry:
        data = await self.tmdb_json(f"/{'tv' if kind == 'show' else 'movie'}/{tmdb_id}")
        return ListEntry(
            kind=kind,
            title=data.get("title") or data.get("name") or "",
            year=_year(data.get("release_date") or data.get("first_air_date")),
            tmdb_id=str(tmdb_id),
            imdb_id=data.get("imdb_id"),
        )

    # --- Trakt ------------------------------------------------------------------------------

    def _trakt_headers(self) -> dict[str, str]:
        if not self.trakt_client_id:
            raise ListError("Add a Trakt client ID in Settings → General")
        headers = {"trakt-api-version": "2", "trakt-api-key": self.trakt_client_id}
        if self.trakt_token:
            headers["Authorization"] = f"Bearer {self.trakt_token}"
        return headers

    @staticmethod
    def _trakt_entry(kind: str, media: dict[str, Any]) -> ListEntry:
        ids = media.get("ids") or {}
        return ListEntry(
            kind=kind,
            title=media.get("title") or "",
            year=media.get("year"),
            tmdb_id=str(ids["tmdb"]) if ids.get("tmdb") else None,
            tvdb_id=str(ids["tvdb"]) if ids.get("tvdb") else None,
            imdb_id=ids.get("imdb"),
        )

    async def trakt_chart(self, path: str, kind: str, limit: int) -> list[ListEntry]:
        headers = self._trakt_headers()
        key = "show" if kind == "show" else "movie"
        resp = await self._get(
            f"{TRAKT_API}/{key}s/{path}", headers=headers, params={"limit": limit}
        )
        entries = []
        for row in resp.json()[:limit]:
            # Most charts wrap each title ({"watchers": 5, "movie": {...}}); popular doesn't.
            media = row.get(key) if isinstance(row.get(key), dict) else row
            entries.append(self._trakt_entry(kind, media))
        return entries

    async def trakt_list(self, url: str) -> list[ListEntry]:
        headers = self._trakt_headers()
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
                entries.append(
                    self._trakt_entry("show" if kind == "show" else "movie", row.get(kind) or {})
                )
            page += 1
        return entries

    async def resolve(
        self, builder: dict[str, Any], library_kind: str = "movie"
    ) -> list[ListEntry]:
        """The titles a builder lists, in the list's own order."""
        kind, value = builder["type"], builder["value"]
        if kind == "tmdb_list":
            return await self.tmdb_list(tmdb_id_from(value, "list"))
        if kind == "tmdb_collection":
            return await self.tmdb_collection(tmdb_id_from(value, "collection"))
        if kind == "trakt_list":
            return await self.trakt_list(str(value))
        if kind in ID_BUILDERS:
            media = "show" if kind == "tmdb_show" else "movie"
            return [
                await self.tmdb_title(
                    tmdb_id_from(value, "tv" if media == "show" else "movie"), media
                )
            ]
        for charts, fetch in ((TMDB_CHARTS, self.tmdb_chart), (TRAKT_CHARTS, self.trakt_chart)):
            if kind in charts:
                path = charts[kind].get(library_kind)
                if path is None:
                    noun = "films" if library_kind == "movie" else "shows"
                    raise ListError(f"{kind} isn't available for {noun}")
                return await fetch(path, library_kind, chart_limit(value))
        raise ListError(f"Unsupported source {kind}")
