from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from coverarr.clients.base import (
    CollectionOrder,
    ItemKind,
    MatchTarget,
    MediaServerClient,
    MediaServerError,
    NotFoundError,
    RemoteCollection,
    RemoteItem,
    RemoteLibrary,
    ServerInfo,
    to_int,
)
from coverarr.services.mediainfo import normalize_resolution

PLEX_TYPES: dict[str, int] = {"movie": 1, "show": 2, "season": 3, "episode": 4}
PLEX_COLLECTION_SORT: dict[str, int] = {"release": 0, "alpha": 1, "custom": 2}
EDIT_TYPES: dict[str, int] = {**PLEX_TYPES, "collection": 18}

log = logging.getLogger(__name__)

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


def _media_facts(
    meta: dict[str, Any], kind: str, rating_key: str, flags: dict[str, set[str]]
) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if kind in ("movie", "episode"):
        media = (meta.get("Media") or [{}])[0]
        resolution = normalize_resolution(media.get("videoResolution"))
        if resolution:
            facts["resolution"] = resolution
        for flag, ids in flags.items():
            if rating_key in ids:
                facts[flag] = True
        # A single item's details list its streams; the library listing doesn't.
        for part in media.get("Part") or []:
            for stream in part.get("Stream") or []:
                if to_int(stream.get("streamType")) != 1:
                    continue
                if stream.get("DOVIPresent") in (True, 1, "1"):
                    facts["dv"] = True
                if stream.get("colorTrc") in ("smpte2084", "arib-std-b67"):
                    facts["hdr"] = True
        if meta.get("editionTitle"):
            facts["edition"] = meta["editionTitle"]
    elif kind == "show":
        network = meta.get("network") or meta.get("studio")
        if network:
            facts["network"] = network
    return facts


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
        self, path: str, params: dict[str, Any] | None = None, *, count: bool = False
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Pages of rows; with ``count``, the server's total goes in ``listed_total``."""
        start = 0
        if count:
            self.listed_total = None
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
            if count and start == 0:
                self.listed_total = total
            start += len(rows)
            # A short page only ends the listing when the server gives no total.
            if not rows or (start >= total if total is not None else len(rows) < self.page_size):
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
                    RemoteLibrary(
                        id=str(d["key"]),
                        name=d.get("title", ""),
                        kind=d["type"],
                        locations=[
                            loc["path"] for loc in d.get("Location") or [] if loc.get("path")
                        ],
                    )
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

    async def _count(self, library: RemoteLibrary, kind: ItemKind, **filters: Any) -> int | None:
        container = await self._container(
            f"/library/sections/{library.id}/all",
            {
                "type": PLEX_TYPES[kind],
                **filters,
                "X-Plex-Container-Start": 0,
                "X-Plex-Container-Size": 0,
            },
        )
        return to_int(container.get("totalSize"))

    async def _filter_ids(self, library: RemoteLibrary, kind: ItemKind, field: str) -> set[str]:
        """Rating keys matching a boolean filter such as ``hdr`` or ``dovi`` (listings don't
        say whether a file is HDR or Dolby Vision). Empty if this Plex can't filter by it."""
        ids: set[str] = set()
        try:
            # A Plex that doesn't know the filter ignores it and lists everything, which would
            # put an HDR badge on every title. Only trust it if =1 and =0 split the library.
            total = await self._count(library, kind)
            yes = await self._count(library, kind, **{field: 1})
            no = await self._count(library, kind, **{field: 0})
            if total is None or yes is None or no is None or yes + no != total or yes == total:
                if yes:
                    log.info("Plex ignores the %s filter on %s; not using it", field, library.name)
                return ids
            if not yes:
                return ids
            async for rows in self._paged(
                f"/library/sections/{library.id}/all", {"type": PLEX_TYPES[kind], field: 1}
            ):
                ids.update(str(r["ratingKey"]) for r in rows)
        except MediaServerError as exc:
            log.info("Plex can't filter %s by %s: %s", library.name, field, exc)
        return ids

    async def items(
        self, library: RemoteLibrary, kind: ItemKind
    ) -> AsyncIterator[list[RemoteItem]]:
        overlay_ids = await self._label_ids(library, kind, "Overlay")
        flags: dict[str, set[str]] = {}
        if kind in ("movie", "episode"):
            flags = {
                "hdr": await self._filter_ids(library, kind, "hdr"),
                "dv": await self._filter_ids(library, kind, "dovi"),
            }
        async for rows in self._paged(
            f"/library/sections/{library.id}/all",
            # Oldest first, so titles added during the listing land at the end rather than
            # shifting the pages still to come.
            {"type": PLEX_TYPES[kind], "includeGuids": 1, "sort": "addedAt:asc"},
            count=True,
        ):
            yield [self._to_item(r, kind, overlay_ids, flags) for r in rows]

    def _to_item(
        self,
        meta: dict[str, Any],
        kind: ItemKind,
        overlay_ids: set[str],
        flags: dict[str, set[str]] | None = None,
    ) -> RemoteItem:
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
        item.media = _media_facts(meta, kind, rating_key, flags or {})
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

    async def rename_collection(
        self, library_remote_id: str | None, collection_id: str, name: str
    ) -> None:
        if not library_remote_id:
            raise MediaServerError("plex: collection has no library section")
        # type 18 = collection; locking stops agents or tools from renaming it back.
        await self._request(
            "PUT",
            f"/library/sections/{library_remote_id}/all",
            params={"type": 18, "id": collection_id, "title.value": name, "title.locked": 1},
        )

    async def get_collection(
        self, library_remote_id: str | None, collection_id: str
    ) -> RemoteCollection:
        container = await self._container(f"/library/metadata/{collection_id}")
        rows = container.get("Metadata") or []
        if not rows:
            raise NotFoundError(f"plex: collection {collection_id} not found")
        meta = rows[0]
        coll = RemoteCollection(
            id=str(meta["ratingKey"]),
            name=meta.get("title") or "",
            library_id=str(meta.get("librarySectionID") or library_remote_id or "") or None,
            sort_title=meta.get("titleSort"),
            summary=meta.get("summary"),
            smart=str(meta.get("smart", "0")) in ("1", "true", "True"),
            poster_ref=meta.get("thumb"),
            labels=_tags(meta, "Label"),
        )
        async for page in self._paged(f"/library/collections/{collection_id}/children"):
            coll.member_ids.extend(str(r["ratingKey"]) for r in page)
        return coll

    async def update_collection(
        self,
        library_remote_id: str | None,
        collection_id: str,
        *,
        sort_title: str | None = None,
        summary: str | None = None,
        order: CollectionOrder | None = None,
        member_order: list[str] | None = None,
    ) -> list[str]:
        fields: dict[str, Any] = {}
        if sort_title is not None:
            fields.update({"titleSort.value": sort_title, "titleSort.locked": 1})
        if summary is not None:
            fields.update({"summary.value": summary, "summary.locked": 1})
        if fields:
            if not library_remote_id:
                raise MediaServerError("plex: collection has no library section")
            await self._request(
                "PUT",
                f"/library/sections/{library_remote_id}/all",
                params={"type": 18, "id": collection_id, **fields},
            )
        if order is not None:
            await self._request(
                "PUT",
                f"/library/metadata/{collection_id}/prefs",
                params={"collectionSort": PLEX_COLLECTION_SORT[order]},
            )
        if order == "custom" and member_order:
            current = (await self.get_collection(library_remote_id, collection_id)).member_ids
            wanted = [m for m in member_order if m in set(current)]
            if [m for m in current if m in set(wanted)] != wanted:
                previous: str | None = None
                for rating_key in wanted:
                    params = {"after": previous} if previous else {}
                    await self._request(
                        "PUT",
                        f"/library/collections/{collection_id}/items/{rating_key}/move",
                        params=params,
                    )
                    previous = rating_key
        return []

    async def recent_items(
        self, library: RemoteLibrary, kind: ItemKind, limit: int = 50
    ) -> list[RemoteItem]:
        container = await self._container(
            f"/library/sections/{library.id}/all",
            {
                "type": PLEX_TYPES[kind],
                "includeGuids": 1,
                "sort": "addedAt:desc",
                "X-Plex-Container-Start": 0,
                "X-Plex-Container-Size": limit,
            },
        )
        return [self._to_item(r, kind, set()) for r in container.get("Metadata") or []]

    async def scan_folder(self, library: RemoteLibrary, path: str) -> None:
        await self._request("GET", f"/library/sections/{library.id}/refresh", params={"path": path})

    async def show_children(self, show_id: str) -> list[RemoteItem]:
        out: list[RemoteItem] = []
        async for page in self._paged(f"/library/metadata/{show_id}/children"):
            out += [self._to_item(r, "season", set()) for r in page if r.get("type") == "season"]
        async for page in self._paged(f"/library/metadata/{show_id}/allLeaves"):
            out += [self._to_item(r, "episode", set()) for r in page]
        return out

    async def remove_from_collection(
        self, library_remote_id: str | None, collection_id: str, member_ids: list[str]
    ) -> None:
        for member in member_ids:
            await self._request("DELETE", f"/library/collections/{collection_id}/items/{member}")

    async def upload_poster(self, item_id: str, data: bytes, content_type: str) -> str | None:
        # Plex selects (and locks) an uploaded poster straight away.
        await self._request(
            "POST",
            f"/library/metadata/{item_id}/posters",
            content=data,
            headers={"Content-Type": content_type},
        )
        container = await self._container(f"/library/metadata/{item_id}")
        meta = (container.get("Metadata") or [{}])[0]
        # Kometa's "Overlay" label means "this poster has overlays, keep the original". The new
        # poster has none of Kometa's, so drop the label or Kometa would think otherwise.
        overlay = next((t for t in _tags(meta, "Label") if t.lower() == "overlay"), None)
        type_id, section = EDIT_TYPES.get(meta.get("type", "")), meta.get("librarySectionID")
        if overlay and type_id and section:
            try:
                await self._request(
                    "PUT",
                    f"/library/sections/{section}/all",
                    params={"type": type_id, "id": item_id, "label[].tag.tag-": overlay},
                )
            except MediaServerError as exc:
                log.warning("Could not remove the Overlay label from %s: %s", item_id, exc)
        return meta.get("thumb")

    async def maintenance(self, action: str) -> str:
        """Plex's own clean-up tasks (they run in the background on the server)."""
        if action == "clean_bundles":
            # Deletes metadata bundles (artwork and all) of items no longer in the library.
            await self._request("PUT", "/library/clean/bundles", params={"async": 1})
            return "Clean Bundles started"
        if action == "optimize":
            await self._request("PUT", "/library/optimize", params={"async": 1})
            return "Optimize Database started"
        if action == "empty_trash":
            libraries = await self.libraries()
            for lib in libraries:
                await self._request("PUT", f"/library/sections/{lib.id}/emptyTrash")
            return f"Emptied the trash of {len(libraries)} libraries"
        raise ValueError(f"Unknown Plex maintenance task {action!r}")
