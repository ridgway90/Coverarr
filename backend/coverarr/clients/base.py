from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

LibraryKind = Literal["movie", "show"]
ItemKind = Literal["movie", "show", "season", "episode"]

USER_AGENT = "Coverarr/0.1"


class MediaServerError(Exception):
    pass


class NotFoundError(MediaServerError):
    """The item or collection no longer exists on the server."""


@dataclass(slots=True)
class ServerInfo:
    name: str
    version: str | None
    remote_id: str | None


@dataclass(slots=True)
class RemoteLibrary:
    id: str
    name: str
    kind: LibraryKind


@dataclass(slots=True)
class RemoteItem:
    id: str
    kind: ItemKind
    title: str
    sort_title: str | None = None
    year: int | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None
    show_id: str | None = None
    season_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    poster_ref: str | None = None
    labels: list[str] = field(default_factory=list)
    # Media file (movies, episodes) or folder (shows) as the server sees it.
    path: str | None = None
    # What overlays need, as far as this server knows: resolution, hdr, dv, edition, network.
    media: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MatchTarget:
    """The identity to re-match an item to."""

    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None
    title: str | None = None
    year: int | None = None

    def ids(self) -> dict[str, str]:
        return {
            k: v
            for k, v in (("tmdb", self.tmdb_id), ("tvdb", self.tvdb_id), ("imdb", self.imdb_id))
            if v
        }


@dataclass(slots=True)
class RemoteCollection:
    id: str
    name: str
    library_id: str | None
    sort_title: str | None = None
    summary: str | None = None
    smart: bool = False
    poster_ref: str | None = None
    labels: list[str] = field(default_factory=list)
    member_ids: list[str] = field(default_factory=list)


def to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


class MediaServerClient(ABC):
    kind: str
    page_size = 500

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 60.0,
        concurrency: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = url.rstrip("/")
        self.token = token
        self._http = httpx.AsyncClient(
            base_url=self.api_root(),
            headers={"User-Agent": USER_AGENT, **self.auth_headers()},
            verify=verify_ssl,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        # Bounds parallel requests (collection members, image downloads) per server.
        self.limiter = asyncio.Semaphore(concurrency)

    def api_root(self) -> str:
        return self.base_url

    @abstractmethod
    def auth_headers(self) -> dict[str, str]: ...

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with self.limiter:
                resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MediaServerError(f"{self.kind}: {method} {path} failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise MediaServerError(f"{self.kind}: authentication rejected ({resp.status_code})")
        if resp.status_code == 404:
            raise NotFoundError(f"{self.kind}: {method} {path} not found")
        if resp.status_code >= 400:
            raise MediaServerError(
                f"{self.kind}: {method} {path} returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp

    async def _json(self, method: str, path: str, **kwargs) -> Any:
        resp = await self._request(method, path, **kwargs)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise MediaServerError(f"{self.kind}: {path} did not return JSON") from exc

    # --- interface -------------------------------------------------------------------------

    @abstractmethod
    async def server_info(self) -> ServerInfo: ...

    @abstractmethod
    async def libraries(self) -> list[RemoteLibrary]: ...

    @abstractmethod
    def items(self, library: RemoteLibrary, kind: ItemKind) -> AsyncIterator[list[RemoteItem]]:
        """Yield pages of items of one kind from a library."""

    @abstractmethod
    async def collections(self, libraries: list[RemoteLibrary]) -> list[RemoteCollection]:
        """All collections relevant to the given libraries, with member ids filled in."""

    @abstractmethod
    async def fetch_poster(
        self, item_id: str, poster_ref: str, max_width: int | None = None
    ) -> bytes: ...

    @abstractmethod
    async def create_collection(
        self, library: RemoteLibrary, name: str, member_ids: list[str]
    ) -> str:
        """Create a collection containing ``member_ids``; returns its remote id."""

    @abstractmethod
    async def get_item(self, item_id: str, kind: ItemKind) -> RemoteItem:
        """Fetch one movie or show fresh from the server."""

    @abstractmethod
    async def fix_match(self, item_id: str, kind: ItemKind, target: MatchTarget) -> str:
        """Re-match a movie or show to ``target`` using the server's own matching.

        Returns a short description of what the server matched it to.
        """

    @abstractmethod
    async def add_to_collection(
        self, library: RemoteLibrary, collection_id: str, member_ids: list[str]
    ) -> None: ...

    @abstractmethod
    async def rename_collection(
        self, library_remote_id: str | None, collection_id: str, name: str
    ) -> None:
        """Rename a collection (and lock the title so the server doesn't revert it)."""

    @abstractmethod
    async def get_collection(
        self, library_remote_id: str | None, collection_id: str
    ) -> RemoteCollection:
        """Fetch one collection fresh, members included."""

    @abstractmethod
    async def show_children(self, show_id: str) -> list[RemoteItem]:
        """All seasons and episodes of one show."""

    @abstractmethod
    async def upload_poster(self, item_id: str, data: bytes, content_type: str) -> str | None:
        """Upload a poster for an item or collection and make it the selected one.

        Returns the new poster ref, so the caller can tell later whether it changed again.
        """

    @abstractmethod
    async def remove_from_collection(
        self, library_remote_id: str | None, collection_id: str, member_ids: list[str]
    ) -> None: ...
