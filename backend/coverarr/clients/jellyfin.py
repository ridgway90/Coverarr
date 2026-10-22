"""Jellyfin and Emby share most of their API (Jellyfin is an Emby fork)."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

from coverarr import __version__
from coverarr.clients.base import (
    CollectionOrder,
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
from coverarr.services.mediainfo import resolution_from_size

ITEM_TYPES: dict[str, str] = {
    "movie": "Movie",
    "show": "Series",
    "season": "Season",
    "episode": "Episode",
}
LIBRARY_KINDS = {"movies": "movie", "tvshows": "show"}
FIELDS = "ProviderIds,SortName,ProductionYear,Tags,Overview,Path,Studios,Width,Height"


def _media_facts(row: dict[str, Any], kind: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if kind in ("movie", "episode"):
        resolution = resolution_from_size(row.get("Width"), row.get("Height"))
        if resolution:
            facts["resolution"] = resolution
    elif kind == "show":
        studios = [s.get("Name") for s in row.get("Studios") or [] if s.get("Name")]
        if studios:
            facts["network"] = studios[0]
    return facts


def provider_ids(item: dict[str, Any]) -> dict[str, str]:
    ids = {str(k).lower(): str(v) for k, v in (item.get("ProviderIds") or {}).items() if v}
    return {k: ids[k] for k in ("tmdb", "tvdb", "imdb") if k in ids}


class JellyfinClient(MediaServerClient):
    kind = "jellyfin"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._user_id: str | None = None

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": (
                f'MediaBrowser Client="Coverarr", Device="Coverarr", DeviceId="coverarr", '
                f'Version="{__version__}", Token="{self.token}"'
            ),
            "Accept": "application/json",
        }

    async def server_info(self) -> ServerInfo:
        info = await self._json("GET", "/System/Info")
        return ServerInfo(
            name=info.get("ServerName") or self.kind.title(),
            version=info.get("Version"),
            remote_id=info.get("Id"),
        )

    async def user_id(self) -> str:
        """An administrator user to browse as.

        Listing items through ``/Users/{id}/Items`` behaves the same on Jellyfin and Emby,
        unlike the user-less ``/Items`` endpoint.
        """
        if self._user_id is None:
            users = await self._json("GET", "/Users")
            admins = [u for u in users if (u.get("Policy") or {}).get("IsAdministrator")]
            pick = (admins or users or [None])[0]
            if not pick:
                raise MediaServerError(f"{self.kind}: no users found to browse libraries as")
            self._user_id = pick["Id"]
        return self._user_id

    async def libraries(self) -> list[RemoteLibrary]:
        folders = await self._json("GET", "/Library/VirtualFolders")
        libs = []
        for f in folders or []:
            kind = LIBRARY_KINDS.get(str(f.get("CollectionType", "")).lower())
            lib_id = f.get("ItemId") or f.get("Id")
            if kind and lib_id:
                libs.append(
                    RemoteLibrary(
                        id=str(lib_id),
                        name=f.get("Name", ""),
                        kind=kind,
                        locations=[str(p) for p in f.get("Locations") or []],
                    )
                )
        return libs

    async def _paged(
        self, params: dict[str, Any], *, count: bool = False
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Pages of rows; with ``count``, the server's total goes in ``listed_total``."""
        uid = await self.user_id()
        start = 0
        if count:
            self.listed_total = None
        while True:
            data = await self._json(
                "GET",
                f"/Users/{uid}/Items",
                params={**params, "StartIndex": start, "Limit": self.page_size},
            )
            rows = data.get("Items") or []
            if rows:
                yield rows
            total = to_int(data.get("TotalRecordCount"))
            if count and start == 0:
                self.listed_total = total
            start += len(rows)
            # A short page only ends the listing when the server gives no total.
            if not rows or (start >= total if total is not None else len(rows) < self.page_size):
                return

    async def items(
        self, library: RemoteLibrary, kind: ItemKind
    ) -> AsyncIterator[list[RemoteItem]]:
        params = {
            "ParentId": library.id,
            "Recursive": "true",
            "IncludeItemTypes": ITEM_TYPES[kind],
            "Fields": FIELDS,
            "EnableImageTypes": "Primary",
            # Oldest first, so titles added during the listing land at the end rather than
            # shifting the pages still to come.
            "SortBy": "DateCreated,SortName",
            "SortOrder": "Ascending",
        }
        async for rows in self._paged(params, count=True):
            yield [self._to_item(r, kind) for r in rows]

    def _to_item(self, row: dict[str, Any], kind: ItemKind) -> RemoteItem:
        ids = provider_ids(row) if kind in ("movie", "show") else {}
        item = RemoteItem(
            id=str(row["Id"]),
            kind=kind,
            title=row.get("Name") or "",
            sort_title=row.get("SortName"),
            year=to_int(row.get("ProductionYear")),
            tmdb_id=ids.get("tmdb"),
            tvdb_id=ids.get("tvdb"),
            imdb_id=ids.get("imdb"),
            poster_ref=(row.get("ImageTags") or {}).get("Primary"),
            labels=[str(t) for t in row.get("Tags") or []],
            path=row.get("Path"),
            media=_media_facts(row, kind),
        )
        if kind == "season":
            item.show_id = row.get("SeriesId")
            item.season_number = to_int(row.get("IndexNumber"))
        elif kind == "episode":
            item.show_id = row.get("SeriesId")
            item.season_id = row.get("SeasonId")
            item.season_number = to_int(row.get("ParentIndexNumber"))
            item.episode_number = to_int(row.get("IndexNumber"))
        return item

    async def get_item(self, item_id: str, kind: ItemKind) -> RemoteItem:
        uid = await self.user_id()
        row = await self._json("GET", f"/Users/{uid}/Items/{item_id}")
        if not row.get("Id"):
            raise MediaServerError(f"{self.kind}: item {item_id} not found")
        return self._to_item(row, kind)

    async def fix_match(self, item_id: str, kind: ItemKind, target: MatchTarget) -> str:
        wanted = target.ids()
        info: dict[str, Any] = {
            "ProviderIds": {k.capitalize(): v for k, v in wanted.items()},
        }
        if target.title:
            info["Name"] = target.title
        if target.year:
            info["Year"] = target.year
        search_type = "Movie" if kind == "movie" else "Series"
        results = await self._json(
            "POST",
            f"/Items/RemoteSearch/{search_type}",
            json={"SearchInfo": info, "ItemId": item_id},
        )
        results = results if isinstance(results, list) else []
        exact = [r for r in results if wanted.items() & provider_ids(r).items()]
        pick = (exact or (results if len(results) == 1 else []) or [None])[0]
        if pick is None:
            raise MediaServerError(
                f"{self.kind}: no unambiguous search result for {target.title or wanted}"
            )
        await self._request(
            "POST",
            f"/Items/RemoteSearch/Apply/{item_id}",
            params={"ReplaceAllImages": "false"},
            json=pick,
        )
        return f"{pick.get('Name')} ({pick.get('ProductionYear')})"

    async def collections(self, libraries: list[RemoteLibrary]) -> list[RemoteCollection]:
        # Box sets aren't tied to a library; the importer infers that from the members.
        result: list[RemoteCollection] = []
        async for rows in self._paged(
            {"IncludeItemTypes": "BoxSet", "Recursive": "true", "Fields": FIELDS}
        ):
            for row in rows:
                result.append(
                    RemoteCollection(
                        id=str(row["Id"]),
                        name=row.get("Name") or "",
                        library_id=None,
                        sort_title=row.get("SortName"),
                        summary=row.get("Overview"),
                        poster_ref=(row.get("ImageTags") or {}).get("Primary"),
                        labels=[str(t) for t in row.get("Tags") or []],
                    )
                )

        async def load_members(coll: RemoteCollection) -> None:
            async for rows in self._paged({"ParentId": coll.id, "Fields": "ProviderIds"}):
                coll.member_ids.extend(str(r["Id"]) for r in rows)

        await asyncio.gather(*(load_members(c) for c in result))
        return result

    async def fetch_poster(
        self, item_id: str, poster_ref: str, max_width: int | None = None
    ) -> bytes:
        params: dict[str, Any] = {"tag": poster_ref, "quality": 90}
        if max_width:
            params["maxWidth"] = max_width
        resp = await self._request(
            "GET", f"/Items/{item_id}/Images/Primary", params=params, headers={"Accept": "image/*"}
        )
        return resp.content

    async def create_collection(
        self, library: RemoteLibrary, name: str, member_ids: list[str]
    ) -> str:
        data = await self._json(
            "POST", "/Collections", params={"Name": name, "Ids": ",".join(member_ids)}
        )
        if not data.get("Id"):
            raise MediaServerError(f"{self.kind}: creating collection {name!r} returned no id")
        return str(data["Id"])

    async def add_to_collection(
        self, library: RemoteLibrary, collection_id: str, member_ids: list[str]
    ) -> None:
        if member_ids:
            await self._request(
                "POST", f"/Collections/{collection_id}/Items", params={"Ids": ",".join(member_ids)}
            )

    async def rename_collection(
        self, library_remote_id: str | None, collection_id: str, name: str
    ) -> None:
        # Item updates take the whole item back, so fetch it, change the name and lock it.
        uid = await self.user_id()
        item = await self._json("GET", f"/Users/{uid}/Items/{collection_id}")
        item["Name"] = name
        locked = set(item.get("LockedFields") or [])
        item["LockedFields"] = sorted(locked | {"Name"})
        await self._request("POST", f"/Items/{collection_id}", json=item)

    async def get_collection(
        self, library_remote_id: str | None, collection_id: str
    ) -> RemoteCollection:
        uid = await self.user_id()
        row = await self._json("GET", f"/Users/{uid}/Items/{collection_id}")
        coll = RemoteCollection(
            id=str(row["Id"]),
            name=row.get("Name") or "",
            library_id=None,
            sort_title=row.get("SortName"),
            summary=row.get("Overview"),
            poster_ref=(row.get("ImageTags") or {}).get("Primary"),
            labels=[str(t) for t in row.get("Tags") or []],
        )
        async for rows in self._paged({"ParentId": collection_id, "Fields": "ProviderIds"}):
            coll.member_ids.extend(str(r["Id"]) for r in rows)
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
        uid = await self.user_id()
        item = await self._json("GET", f"/Users/{uid}/Items/{collection_id}")
        before = dict(item)
        locked = list(item.get("LockedFields") or [])
        if sort_title is not None:
            # A forced sort name stays put by itself; "SortName" isn't a lockable field
            # (only Name, Overview, Genres... are), and the server rejects the whole update.
            item["SortName"] = item["ForcedSortName"] = sort_title
        if summary is not None:
            item["Overview"] = summary
            if "Overview" not in locked:
                locked.append("Overview")
        notes = []
        if order in ("release", "alpha"):
            item["DisplayOrder"] = "PremiereDate" if order == "release" else "SortName"
        elif order == "custom":
            notes.append(f"{self.kind} collections can't keep a custom order; sorted by name")
            item["DisplayOrder"] = "SortName"
        item["LockedFields"] = locked
        if item != before:
            await self._request("POST", f"/Items/{collection_id}", json=item)
        return notes

    async def recent_items(
        self, library: RemoteLibrary, kind: ItemKind, limit: int = 50
    ) -> list[RemoteItem]:
        uid = await self.user_id()
        data = await self._json(
            "GET",
            f"/Users/{uid}/Items",
            params={
                "ParentId": library.id,
                "Recursive": "true",
                "IncludeItemTypes": ITEM_TYPES[kind],
                "Fields": FIELDS,
                "EnableImageTypes": "Primary",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": limit,
            },
        )
        return [self._to_item(r, kind) for r in data.get("Items") or []]

    async def scan_folder(self, library: RemoteLibrary, path: str) -> None:
        await self._request(
            "POST",
            "/Library/Media/Updated",
            json={"Updates": [{"Path": path, "UpdateType": "Created"}]},
        )

    async def show_children(self, show_id: str) -> list[RemoteItem]:
        kinds = {"Season": "season", "Episode": "episode"}
        out: list[RemoteItem] = []
        async for rows in self._paged(
            {
                "ParentId": show_id,
                "Recursive": "true",
                "IncludeItemTypes": "Season,Episode",
                "Fields": FIELDS,
                "EnableImageTypes": "Primary",
            }
        ):
            out += [self._to_item(r, kinds[r["Type"]]) for r in rows if r.get("Type") in kinds]
        return out

    async def remove_from_collection(
        self, library_remote_id: str | None, collection_id: str, member_ids: list[str]
    ) -> None:
        if member_ids:
            await self._request(
                "DELETE",
                f"/Collections/{collection_id}/Items",
                params={"Ids": ",".join(member_ids)},
            )

    async def upload_poster(self, item_id: str, data: bytes, content_type: str) -> str | None:
        # Jellyfin and Emby both take the image base64-encoded in the request body.
        await self._request(
            "POST",
            f"/Items/{item_id}/Images/Primary",
            content=base64.b64encode(data),
            headers={"Content-Type": content_type},
        )
        uid = await self.user_id()
        row = await self._json("GET", f"/Users/{uid}/Items/{item_id}")
        return (row.get("ImageTags") or {}).get("Primary")


class EmbyClient(JellyfinClient):
    kind = "emby"

    def api_root(self) -> str:
        return f"{self.base_url}/emby"

    def auth_headers(self) -> dict[str, str]:
        return {"X-Emby-Token": self.token, "Accept": "application/json"}
