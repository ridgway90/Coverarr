from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from coverarr.clients.base import (
    ItemKind,
    MatchTarget,
    MediaServerClient,
    MediaServerError,
    RemoteCollection,
    RemoteItem,
    RemoteLibrary,
    ServerInfo,
    to_int,
)

PLEX_TYPES: dict[str, int] = {"movie": 1, "show": 2, "season": 3, "episode": 4}

# Legacy agent GUIDs, e.g. com.plexapp.agents.imdb://tt0111161?lang=en
_LEGACY_GUID = re.compile(r"^com\.plexapp\.agents\.(\w+)://([^/?]+)")
_LEGACY_AGENTS = {"imdb": "imdb", "themoviedb": "tmdb", "thetvdb": "tvdb", "tvdb": "tvdb"}

_CLIENT_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "coverarr"))


def parse_plex_guids(meta: dict[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for guid in meta.get("Guid") or []:
        scheme, _, value = str(guid.get("id", "")).partition("://")
        if scheme in ("imdb", "tmdb", "tvdb") and value:
            ids.setdefault(scheme, value)
    legacy = _LEGACY_GUID.match(str(meta.get("guid", "")))
    if legacy and legacy.group(1) in _LEGACY_AGENTS:
        ids.setdefault(_LEGACY_AGENTS[legacy.group(1)], legacy.group(2))
    return ids


def _tags(meta: dict[str, Any], key: str) -> list[str]:
    return [t["tag"] for t in meta.get(key) or [] if t.get("tag")]


def _media_path(meta: dict[str, Any]) -> str | None:
    """First file of a movie/episode, or the folder of a show."""
    for media in meta.get("Media") or []:
        for part in media.get("Part") or []:
            if part.get("file"):
                return part["file"]
    for location in meta.get("Location") or []:
        if location.get("path"):
            return location["path"]
    return None


class PlexClient(MediaServerClient):
    kind = "plex"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._machine_id: str | None = None

    def auth_headers(self) -> dict[str, str]:
        return {
            "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": _CLIENT_ID,
            "X-Plex-Product": "Coverarr",
            "Accept": "application/json",
        }

    async def _container(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        data = await self._json("GET", path, params=params)
        return data.get("MediaContainer") or {}

    async def _paged(
        self, path: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        start = 0
        while True:
            page_params = {
                **(params or {}),
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": self.page_size,
            }
            container = await self._container(path, page_params)
            rows = container.get("Metadata") or []
            if rows:
                yield rows
            total = to_int(container.get("totalSize"))
            start += len(rows)
            if not rows or len(rows) < self.page_size or (total is not None and start >= total):
                return

    async def server_info(self) -> ServerInfo:
        root = await self._container("/")
        self._machine_id = root.get("machineIdentifier")
        return ServerInfo(
            name=root.get("friendlyName") or "Plex",
            version=root.get("version"),
            remote_id=self._machine_id,
        )

    async def machine_id(self) -> str:
        if not self._machine_id:
            await self.server_info()
        if not self._machine_id:
            raise MediaServerError("plex: could not determine machineIdentifier")
        return self._machine_id

    async def libraries(self) -> list[RemoteLibrary]:
        container = await self._container("/library/sections")
        libs = []
        for d in container.get("Directory") or []:
            if d.get("type") in ("movie", "show"):
                libs.append(
                    RemoteLibrary(id=str(d["key"]), name=d.get("title", ""), kind=d["type"])
                )
        return libs

    async def _label_ids(self, library: RemoteLibrary, kind: ItemKind, label: str) -> set[str]:
        """Rating keys of ``kind`` carrying ``label``.

        Section listings don't reliably include labels, so look the tag up and filter by it.
        Kometa marks every item it has drawn an overlay on with the "Overlay" label.
        """
        type_code = PLEX_TYPES[kind]
        container = await self._container(
            f"/library/sections/{library.id}/label", {"type": type_code}
        )
        ids: set[str] = set()
        for d in container.get("Directory") or []:
            if str(d.get("title", "")).lower() == label.lower():
                async for rows in self._paged(
                    f"/library/sections/{library.id}/all",
                    {"type": type_code, "label": d.get("key")},
                ):
                    ids.update(str(r["ratingKey"]) for r in rows)
        return ids

    async def items(
        self, library: RemoteLibrary, kind: ItemKind
    ) -> AsyncIterator[list[RemoteItem]]:
        overlay_ids = await self._label_ids(library, kind, "Overlay")
        async for rows in self._paged(
            f"/library/sections/{library.id}/all",
            {"type": PLEX_TYPES[kind], "includeGuids": 1},
        ):
            yield [self._to_item(r, kind, overlay_ids) for r in rows]

    def _to_item(self, meta: dict[str, Any], kind: ItemKind, overlay_ids: set[str]) -> RemoteItem:
        rating_key = str(meta["ratingKey"])
        labels = _tags(meta, "Label")
        if rating_key in overlay_ids and "Overlay" not in labels:
            labels.append("Overlay")
        item = RemoteItem(
            id=rating_key,
            kind=kind,
            title=meta.get("title") or "",
            sort_title=meta.get("titleSort"),
            year=to_int(meta.get("year")),
            poster_ref=meta.get("thumb"),
            labels=labels,
        )
        item.path = _media_path(meta)
        if kind in ("movie", "show"):
            ids = parse_plex_guids(meta)
            item.tmdb_id, item.tvdb_id, item.imdb_id = (
                ids.get("tmdb"),
                ids.get("tvdb"),
                ids.get("imdb"),
            )
        elif kind == "season":
            item.show_id = str(meta.get("parentRatingKey") or "") or None
            item.season_number = to_int(meta.get("index"))
        elif kind == "episode":
            item.show_id = str(meta.get("grandparentRatingKey") or "") or None
            item.season_id = str(meta.get("parentRatingKey") or "") or None
            item.season_number = to_int(meta.get("parentIndex"))
            item.episode_number = to_int(meta.get("index"))
        return item

    async def get_item(self, item_id: str, kind: ItemKind) -> RemoteItem:
        container = await self._container(f"/library/metadata/{item_id}", {"includeGuids": 1})
        rows = container.get("Metadata") or []
        if not rows:
            raise MediaServerError(f"plex: item {item_id} not found")
        return self._to_item(rows[0], kind, set())

    async def fix_match(self, item_id: str, kind: ItemKind, target: MatchTarget) -> str:
        # Plex's match search accepts "tmdb-123" / "tvdb-123" / "imdb-tt123" as the title,
        # which pins the exact entry; title + year is the fallback.
        queries: list[tuple[str, int | None]] = [
            (f"{key}-{value}", None) for key, value in target.ids().items()
        ]
        if target.title:
            queries.append((target.title, target.year))
        for query, year in queries:
            params: dict[str, Any] = {"manual": 1, "title": query}
            if year:
                params["year"] = year
            container = await self._container(f"/library/metadata/{item_id}/matches", params)
            results = sorted(
                container.get("SearchResult") or [],
                key=lambda r: to_int(r.get("score")) or 0,
                reverse=True,
            )
            if not results:
                continue
            pick = results[0]
            await self._request(
                "PUT",
                f"/library/metadata/{item_id}/match",
                params={
                    "guid": pick["guid"],
                    "name": pick.get("name", ""),
                    "year": pick.get("year", ""),
                },
            )
            return f"{pick.get('name')} ({pick.get('year')})"
        raise MediaServerError("plex: no match found for " + (target.title or str(target.ids())))

    async def collections(self, libraries: list[RemoteLibrary]) -> list[RemoteCollection]:
        result: list[RemoteCollection] = []
        for library in libraries:
            async for rows in self._paged(f"/library/sections/{library.id}/collections"):
                for meta in rows:
                    result.append(
                        RemoteCollection(
                            id=str(meta["ratingKey"]),
                            name=meta.get("title") or "",
                            library_id=library.id,
                            sort_title=meta.get("titleSort"),
                            summary=meta.get("summary"),
                            smart=str(meta.get("smart", "0")) in ("1", "true", "True"),
                            poster_ref=meta.get("thumb"),
                            labels=_tags(meta, "Label"),
                        )
                    )

        async def load_members(coll: RemoteCollection) -> None:
            async for rows in self._paged(f"/library/collections/{coll.id}/children"):
                coll.member_ids.extend(str(r["ratingKey"]) for r in rows)

        await asyncio.gather(*(load_members(c) for c in result))
        return result

    async def fetch_poster(
        self, item_id: str, poster_ref: str, max_width: int | None = None
    ) -> bytes:
        if max_width:
            height = int(max_width * 1.5)
            path = (
                f"/photo/:/transcode?width={max_width}&height={height}&minSize=1&upscale=1"
                f"&url={quote(poster_ref, safe='')}"
            )
        else:
            path = poster_ref
        resp = await self._request("GET", path, headers={"Accept": "image/*"})
        return resp.content

    async def _metadata_uri(self, ids: list[str]) -> str:
        machine = await self.machine_id()
        return f"server://{machine}/com.plexapp.plugins.library/library/metadata/{','.join(ids)}"

    async def create_collection(
        self, library: RemoteLibrary, name: str, member_ids: list[str]
    ) -> str:
        if not member_ids:
            raise MediaServerError("plex: a collection needs at least one item")
        data = await self._json(
            "POST",
            "/library/collections",
            params={
                "type": PLEX_TYPES[library.kind],
                "title": name,
                "smart": 0,
                "sectionId": library.id,
                "uri": await self._metadata_uri(member_ids),
            },
        )
        rows = (data.get("MediaContainer") or {}).get("Metadata") or []
        if not rows:
            raise MediaServerError(f"plex: creating collection {name!r} returned no id")
        return str(rows[0]["ratingKey"])

    async def add_to_collection(
        self, library: RemoteLibrary, collection_id: str, member_ids: list[str]
    ) -> None:
        if member_ids:
            await self._request(
                "PUT",
                f"/library/collections/{collection_id}/items",
                params={"uri": await self._metadata_uri(member_ids)},
            )
