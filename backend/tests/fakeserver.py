"""In-memory fake Plex, Jellyfin and Emby servers for tests and local UI development.

Run standalone with ``python -m tests.fakeserver`` (serves on :32499):
  Plex      http://127.0.0.1:32499/plex       token plex-token
  Jellyfin  http://127.0.0.1:32499/jellyfin   key   jf-key
  Emby      http://127.0.0.1:32499/embysrv    key   emby-key

The default scenario mirrors a real migration: Plex posters carry Kometa overlays, the
Cornetto collection only exists on Plex, Jellyfin's Christmas collection is missing a film,
Plex has different artwork for Arrival, and Emby is missing a film and an episode.
"""

from __future__ import annotations

import io
import itertools
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from PIL import Image, ImageDraw

# --- scenario ---------------------------------------------------------------------------------

MOVIES = [
    # title, year, tmdb, imdb
    ("Shaun of the Dead", 2004, "747", "tt0365748"),
    ("Hot Fuzz", 2007, "4638", "tt0425112"),
    ("The World's End", 2013, "107985", "tt1213663"),
    ("Elf", 2003, "10719", "tt0319343"),
    ("Home Alone", 1990, "771", "tt0099785"),
    ("The Holiday", 2006, "1581", "tt0457939"),
    ("Paddington", 2014, "116149", "tt1109624"),
    ("Paddington 2", 2017, "346648", "tt4468740"),
    ("Arrival", 2016, "329865", "tt2543164"),
    ("9", 2009, "12244", "tt0472033"),  # Plex has it matched to "9 Songs"
    ("Family Holiday 2019", 2019, None, None),  # home video: no provider ids anywhere
]
# What the fake metadata agents know: tmdb -> (title, year, imdb), tvdb -> (title, year).
MOVIE_CATALOG = {tmdb: (t, y, imdb) for t, y, tmdb, imdb in MOVIES if tmdb}
MOVIE_CATALOG["27"] = ("9 Songs", 2004, "tt0411705")
SHOW_CATALOG = {"75592": ("Spaced", 1999), "72216": ("Black Books", 2000)}
# Each server mounts the same media somewhere different.
MOUNTS = {"plex": "/data/media", "jellyfin": "/media", "emby": "/mnt/media"}
SHOWS = [
    # title, year, tvdb, seasons x episodes
    ("Spaced", 1999, "75592", 2, 7),
    ("Black Books", 2000, "72216", 3, 6),
]


def poster_png(seed: str, *, width: int = 400, overlay: bool = False, alt: bool = False) -> bytes:
    """Deterministic artwork per seed; ``overlay`` adds Kometa-style gradients and a badge."""
    h = uuid.uuid5(uuid.NAMESPACE_OID, f"{seed}{alt}").int
    height = int(width * 1.5)
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    rng = [(h >> (i * 8)) & 0xFF for i in range(16)]
    # Blocky composition so difference hashes are distinctive per seed.
    cols, rows = 4, 6
    for r in range(rows):
        for c in range(cols):
            v = rng[(r * cols + c) % 16]
            color = (v, (v * 3 + rng[c]) % 256, (v * 7 + rng[r]) % 256)
            draw.rectangle(
                [
                    c * width // cols,
                    r * height // rows,
                    (c + 1) * width // cols,
                    (r + 1) * height // rows,
                ],
                fill=color,
            )
    draw.text((20, height // 2), seed[:24], fill=(255, 255, 255))
    if overlay:
        band = int(height * 0.17)
        shade = Image.new("L", (width, band))
        for y in range(band):
            ImageDraw.Draw(shade).line([(0, y), (width, y)], fill=int(200 * (1 - y / band)))
        black = Image.new("RGB", (width, band))
        img.paste(black, (0, 0), shade)
        img.paste(black, (0, height - band), shade.transpose(Image.Transpose.FLIP_TOP_BOTTOM))
        draw.rectangle([20, 20, 120, 50], fill=(230, 230, 230))
        draw.text((30, 28), "4K DV", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@dataclass
class Item:
    id: str
    kind: str  # movie | show | season | episode
    title: str
    library: str
    year: int | None = None
    ids: dict[str, str] = field(default_factory=dict)
    parent: str | None = None  # season -> show, episode -> season
    show: str | None = None
    index: int | None = None
    parent_index: int | None = None
    labels: list[str] = field(default_factory=list)
    art_seed: str | None = None
    art_version: int = 1
    overlay: bool = False
    alt_art: bool = False
    file: str | None = None  # media file (movie/episode) or folder (show)

    def rematch(self, title: str, year: int | None, ids: dict[str, str]) -> None:
        """What a server does on "Fix match": new identity and artwork, same file."""
        self.title, self.year, self.ids = title, year, ids
        self.art_seed = f"{self.kind}:{title}"
        self.art_version += 1


@dataclass
class Coll:
    id: str
    name: str
    library: str
    members: list[str]
    smart: bool = False
    art_version: int = 1


class FakeServer:
    def __init__(self, kind: str, token: str, name: str):
        self.kind, self.token, self.name = kind, token, name
        self.machine_id = uuid.uuid5(uuid.NAMESPACE_DNS, name).hex
        self.libraries: list[tuple[str, str, str]] = []  # id, name, kind
        self.items: dict[str, Item] = {}
        self.collections: dict[str, Coll] = {}
        self._ids = itertools.count(1000)
        self.requests = 0

    def new_id(self) -> str:
        n = next(self._ids)
        return str(n) if self.kind == "plex" else uuid.UUID(int=n).hex

    def add_library(self, name: str, kind: str) -> str:
        lib_id = str(len(self.libraries) + 1) if self.kind == "plex" else uuid.uuid4().hex
        self.libraries.append((lib_id, name, kind))
        return lib_id

    def add(self, item: Item) -> Item:
        self.items[item.id] = item
        return item

    def by_title(self, title: str) -> Item:
        return next(
            i for i in self.items.values() if i.title == title and i.kind in ("movie", "show")
        )

    def add_collection(
        self, name: str, library: str, titles: list[str], smart: bool = False
    ) -> Coll:
        coll = Coll(self.new_id(), name, library, [self.by_title(t).id for t in titles], smart)
        self.collections[coll.id] = coll
        return coll

    def image(self, item_id: str, width: int | None) -> bytes:
        if item_id in self.collections:
            return poster_png(f"coll:{self.collections[item_id].name}", width=width or 400)
        item = self.items.get(item_id)
        if item is None or not item.art_seed:
            raise HTTPException(404)
        return poster_png(item.art_seed, width=width or 400, overlay=item.overlay, alt=item.alt_art)


def populate(
    server: FakeServer,
    *,
    skip_movies=(),
    skip_episodes=(),
    overlays=False,
    alt_art=(),
    mismatched: dict[str, str] | None = None,
):
    """``mismatched`` maps a title to the tmdb id the server has wrongly matched it to."""
    mount = MOUNTS[server.kind]
    movies_lib = server.add_library("Films" if server.kind == "plex" else "Movies", "movie")
    tv_lib = server.add_library("TV Programmes" if server.kind == "plex" else "Shows", "show")
    for title, year, tmdb, imdb in MOVIES:
        if title in skip_movies:
            continue
        ids = {k: v for k, v in (("tmdb", tmdb), ("imdb", imdb)) if v}
        name = f"{title} ({year})" + (f" {{tmdb-{tmdb}}}" if tmdb else "")
        shown_title, shown_year = title, year
        if mismatched and title in mismatched:
            wrong = mismatched[title]
            shown_title, shown_year, wrong_imdb = MOVIE_CATALOG[wrong]
            ids = {"tmdb": wrong, "imdb": wrong_imdb}
        server.add(
            Item(
                server.new_id(),
                "movie",
                shown_title,
                movies_lib,
                shown_year,
                ids,
                labels=["Overlay"] if overlays else [],
                art_seed=f"movie:{shown_title}",
                overlay=overlays,
                alt_art=title in alt_art,
                file=f"{mount}/Movies/{name}/{name} [WEBDL-1080p].mkv",
            )
        )
    for title, year, tvdb, seasons, episodes in SHOWS:
        show = server.add(
            Item(
                server.new_id(),
                "show",
                title,
                tv_lib,
                year,
                {"tvdb": tvdb},
                art_seed=f"show:{title}",
                file=f"{mount}/TV/{title} ({year}) {{tvdb-{tvdb}}}",
            )
        )
        for s in range(1, seasons + 1):
            season = server.add(
                Item(
                    server.new_id(),
                    "season",
                    f"Season {s}",
                    tv_lib,
                    parent=show.id,
                    show=show.id,
                    index=s,
                    art_seed=f"season:{title}:{s}",
                )
            )
            for e in range(1, episodes + 1):
                if (title, s, e) in skip_episodes:
                    continue
                server.add(
                    Item(
                        server.new_id(),
                        "episode",
                        f"{title} {s}x{e}",
                        tv_lib,
                        parent=season.id,
                        show=show.id,
                        index=e,
                        parent_index=s,
                        art_seed=f"ep:{title}:{s}:{e}",
                        file=f"{show.file}/Season {s:02d}/{title} - S{s:02d}E{e:02d}.mkv",
                    )
                )
    return movies_lib, tv_lib


def default_servers() -> dict[str, FakeServer]:
    plex = FakeServer("plex", "plex-token", "Fake Plex")
    films, _ = populate(plex, overlays=True, alt_art={"Arrival"}, mismatched={"9": "27"})
    plex.add_collection(
        "Cornetto Collection", films, ["Shaun of the Dead", "Hot Fuzz", "The World's End"]
    )
    plex.add_collection("Christmas Movies", films, ["Elf", "Home Alone", "The Holiday"])
    plex.add_collection("Paddington Collection", films, ["Paddington", "Paddington 2"])
    plex.add_collection("Comfort J Films", films, ["Paddington", "Paddington 2", "Elf"])

    jf = FakeServer("jellyfin", "jf-key", "Fake Jellyfin")
    populate(jf)
    jf.add_collection("Paddington Collection", "", ["Paddington", "Paddington 2"])
    jf.add_collection("Christmas Movies", "", ["Elf", "Home Alone"])
    jf.add_collection("Comfort J Films", "", ["Paddington", "Paddington 2", "Elf"])

    emby = FakeServer("emby", "emby-key", "Fake Emby")
    populate(emby, skip_movies={"Arrival"}, skip_episodes={("Black Books", 3, 6)})
    emby.add_collection("Paddington Collection", "", ["Paddington", "Paddington 2"])
    return {"plex": plex, "jellyfin": jf, "emby": emby}


# --- shared helpers -------------------------------------------------------------------------


def _missing():
    raise HTTPException(404)


def search_catalog(kind: str, query: str) -> list[tuple[str, tuple[str, int | None]]]:
    """Agent search: "tmdb-123"/"tvdb-123"/"imdb-tt123", or a title. Keys are tmdb/tvdb ids."""
    query = query.strip()
    if kind == "movie":
        entries = {k: (t, y) for k, (t, y, _) in MOVIE_CATALOG.items()}
        by_imdb = {imdb: k for k, (_, _, imdb) in MOVIE_CATALOG.items()}
    else:
        entries, by_imdb = dict(SHOW_CATALOG), {}
    scheme, _, value = query.partition("-")
    if scheme in ("tmdb", "tvdb") and value in entries:
        return [(value, entries[value])]
    if scheme == "imdb" and value in by_imdb:
        return [(by_imdb[value], entries[by_imdb[value]])]
    return [(k, v) for k, v in entries.items() if v[0].lower() == query.lower()]


def apply_catalog(item: Item, key: str) -> None:
    if item.kind == "movie":
        title, year, imdb = MOVIE_CATALOG[key]
        item.rematch(title, year, {"tmdb": key, "imdb": imdb})
    else:
        title, year = SHOW_CATALOG[key]
        item.rematch(title, year, {"tvdb": key})


# --- Plex API ---------------------------------------------------------------------------------

PLEX_TYPE = {1: "movie", 2: "show", 3: "season", 4: "episode"}


def plex_router(srv: FakeServer) -> APIRouter:
    r = APIRouter()

    def auth(request: Request) -> None:
        srv.requests += 1
        token = request.headers.get("X-Plex-Token") or request.query_params.get("X-Plex-Token")
        if token != srv.token:
            raise HTTPException(401)

    def page(request: Request, rows: list[dict]) -> dict:
        start = int(request.query_params.get("X-Plex-Container-Start", 0))
        size = int(request.query_params.get("X-Plex-Container-Size", 50))
        return {
            "MediaContainer": {
                "size": len(rows[start : start + size]),
                "totalSize": len(rows),
                "Metadata": rows[start : start + size],
            }
        }

    def meta(item: Item) -> dict:
        out: dict[str, Any] = {
            "ratingKey": item.id,
            "title": item.title,
            "type": item.kind,
            "thumb": f"/library/metadata/{item.id}/thumb/{1700000000 + item.art_version}",
        }
        if item.year:
            out["year"] = item.year
        if item.kind in ("movie", "show"):
            out["guid"] = f"plex://{item.kind}/{item.id}"
            out["Guid"] = [{"id": f"{k}://{v}"} for k, v in item.ids.items()]
        if item.file and item.kind in ("movie", "episode"):
            # Shows deliberately get no Location: real listings often omit it.
            out["Media"] = [{"Part": [{"file": item.file}]}]
        if item.kind == "season":
            out.update(parentRatingKey=item.show, index=item.index)
        if item.kind == "episode":
            out.update(
                grandparentRatingKey=item.show,
                parentRatingKey=item.parent,
                parentIndex=item.parent_index,
                index=item.index,
            )
        return out

    @r.get("/")
    async def root(request: Request):
        auth(request)
        return {
            "MediaContainer": {
                "friendlyName": srv.name,
                "machineIdentifier": srv.machine_id,
                "version": "1.41.0",
            }
        }

    @r.get("/library/sections")
    async def sections(request: Request):
        auth(request)
        return {
            "MediaContainer": {
                "Directory": [
                    {"key": lid, "title": name, "type": kind} for lid, name, kind in srv.libraries
                ]
            }
        }

    @r.get("/library/sections/{lib}/label")
    async def labels(lib: str, request: Request):
        auth(request)
        kind = PLEX_TYPE[int(request.query_params.get("type", 1))]
        has = any(
            i.library == lib and i.kind == kind and "Overlay" in i.labels
            for i in srv.items.values()
        )
        return {
            "MediaContainer": {"Directory": [{"key": "900", "title": "Overlay"}] if has else []}
        }

    @r.get("/library/sections/{lib}/all")
    async def section_all(lib: str, request: Request):
        auth(request)
        kind = PLEX_TYPE[int(request.query_params["type"])]
        rows = [i for i in srv.items.values() if i.library == lib and i.kind == kind]
        if request.query_params.get("label") == "900":
            rows = [i for i in rows if "Overlay" in i.labels]
        return page(request, [meta(i) for i in rows])

    @r.get("/library/sections/{lib}/collections")
    async def collections(lib: str, request: Request):
        auth(request)
        rows = [
            {
                "ratingKey": c.id,
                "title": c.name,
                "smart": "1" if c.smart else "0",
                "thumb": f"/library/metadata/{c.id}/thumb/{1700000000 + c.art_version}",
                "childCount": len(c.members),
            }
            for c in srv.collections.values()
            if c.library == lib
        ]
        return page(request, rows)

    @r.get("/library/collections/{cid}/children")
    async def children(cid: str, request: Request):
        auth(request)
        coll = srv.collections.get(cid)
        if coll is None:
            raise HTTPException(404)
        return page(request, [meta(srv.items[m]) for m in coll.members])

    @r.get("/library/metadata/{rk}")
    async def metadata(rk: str, request: Request):
        auth(request)
        item = srv.items.get(rk) or _missing()
        out = meta(item)
        out["Label"] = [{"tag": t} for t in item.labels]
        return {"MediaContainer": {"Metadata": [out]}}

    @r.get("/library/metadata/{rk}/matches")
    async def matches(rk: str, request: Request):
        auth(request)
        item = srv.items.get(rk) or _missing()
        return {
            "MediaContainer": {
                "SearchResult": [
                    {"guid": f"plex://{item.kind}/{key}", "name": t, "year": y, "score": 100}
                    for key, (t, y) in search_catalog(
                        item.kind, request.query_params.get("title", "")
                    )
                ]
            }
        }

    @r.put("/library/metadata/{rk}/match")
    async def match(rk: str, request: Request):
        auth(request)
        item = srv.items.get(rk) or _missing()
        key = request.query_params["guid"].rsplit("/", 1)[1]
        apply_catalog(item, key)
        return {}

    @r.put("/library/sections/{lib}/all")
    async def edit(lib: str, request: Request):
        auth(request)
        qp = request.query_params
        coll = srv.collections.get(qp["id"]) or _missing()
        if qp.get("type") != "18" or coll.library != lib:
            raise HTTPException(400)
        coll.name = qp["title.value"]
        return {}

    @r.get("/library/metadata/{rk}/thumb/{ts}")
    async def thumb(rk: str, ts: str, request: Request):
        auth(request)
        return Response(srv.image(rk, None), media_type="image/png")

    @r.get("/photo/:/transcode")
    async def transcode(request: Request):
        auth(request)
        rk = request.query_params["url"].split("/")[3]
        return Response(
            srv.image(rk, int(request.query_params.get("width", 300))), media_type="image/png"
        )

    def uri_ids(uri: str) -> list[str]:
        assert uri.startswith(
            f"server://{srv.machine_id}/com.plexapp.plugins.library/library/metadata/"
        )
        return uri.rsplit("/", 1)[1].split(",")

    @r.post("/library/collections")
    async def create(request: Request):
        auth(request)
        qp = request.query_params
        coll = Coll(srv.new_id(), qp["title"], qp["sectionId"], uri_ids(qp["uri"]))
        srv.collections[coll.id] = coll
        return {"MediaContainer": {"Metadata": [{"ratingKey": coll.id, "title": coll.name}]}}

    @r.put("/library/collections/{cid}/items")
    async def add(cid: str, request: Request):
        auth(request)
        srv.collections[cid].members += uri_ids(request.query_params["uri"])
        return {"MediaContainer": {}}

    return r


# --- Jellyfin / Emby API ----------------------------------------------------------------------

JF_TYPE = {"Movie": "movie", "Series": "show", "Season": "season", "Episode": "episode"}
JF_TYPE_OUT = {v: k for k, v in JF_TYPE.items()}
ADMIN = "a" * 32


def jellyfin_router(srv: FakeServer) -> APIRouter:
    r = APIRouter()

    def auth(request: Request) -> None:
        srv.requests += 1
        if srv.kind == "emby":
            ok = request.headers.get("X-Emby-Token") == srv.token
        else:
            ok = f'Token="{srv.token}"' in request.headers.get("Authorization", "")
        if not ok:
            raise HTTPException(401)

    def tag(version: int, item_id: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"{item_id}:{version}").hex

    def row(item: Item) -> dict:
        out: dict[str, Any] = {
            "Id": item.id,
            "Name": item.title,
            "Type": JF_TYPE_OUT[item.kind],
            "SortName": item.title.lower(),
            "ImageTags": {"Primary": tag(item.art_version, item.id)},
        }
        if item.year:
            out["ProductionYear"] = item.year
        if item.file:
            out["Path"] = item.file
        if item.kind in ("movie", "show"):
            out["ProviderIds"] = {k.capitalize(): v for k, v in item.ids.items()}
        if item.kind == "season":
            out.update(SeriesId=item.show, IndexNumber=item.index)
        if item.kind == "episode":
            out.update(
                SeriesId=item.show,
                SeasonId=item.parent,
                IndexNumber=item.index,
                ParentIndexNumber=item.parent_index,
            )
        return out

    def coll_row(c: Coll) -> dict:
        return {
            "Id": c.id,
            "Name": c.name,
            "Type": "BoxSet",
            "SortName": c.name.lower(),
            "ImageTags": {"Primary": tag(c.art_version, c.id)},
        }

    @r.get("/System/Info")
    async def info(request: Request):
        auth(request)
        return {"ServerName": srv.name, "Version": "10.10.0", "Id": srv.machine_id}

    @r.get("/Users")
    async def users(request: Request):
        auth(request)
        return [
            {"Id": "b" * 32, "Name": "kid", "Policy": {"IsAdministrator": False}},
            {"Id": ADMIN, "Name": "admin", "Policy": {"IsAdministrator": True}},
        ]

    @r.get("/Library/VirtualFolders")
    async def folders(request: Request):
        auth(request)
        types = {"movie": "movies", "show": "tvshows"}
        return [
            {"Name": name, "ItemId": lid, "CollectionType": types[kind]}
            for lid, name, kind in srv.libraries
        ]

    @r.get("/Users/{uid}/Items")
    async def items(uid: str, request: Request):
        auth(request)
        if uid != ADMIN:
            raise HTTPException(400)
        qp = request.query_params
        parent = qp.get("ParentId")
        types = qp.get("IncludeItemTypes", "")
        if types == "BoxSet":
            rows = [coll_row(c) for c in srv.collections.values()]
        elif parent in srv.collections:
            rows = [row(srv.items[m]) for m in srv.collections[parent].members]
        else:
            kinds = {JF_TYPE[t] for t in types.split(",") if t}
            rows = [
                row(i)
                for i in srv.items.values()
                if i.library == parent and (not kinds or i.kind in kinds)
            ]
        start, limit = int(qp.get("StartIndex", 0)), int(qp.get("Limit", 100))
        return {"Items": rows[start : start + limit], "TotalRecordCount": len(rows)}

    @r.get("/Items/{item_id}/Images/Primary")
    async def image(item_id: str, request: Request):
        auth(request)
        width = request.query_params.get("maxWidth")
        return Response(srv.image(item_id, int(width) if width else None), media_type="image/png")

    @r.get("/Users/{uid}/Items/{item_id}")
    async def item(uid: str, item_id: str, request: Request):
        auth(request)
        if item_id in srv.collections:
            return coll_row(srv.collections[item_id])
        found = srv.items.get(item_id) or _missing()
        return row(found)

    @r.post("/Items/{item_id}")
    async def update_item(item_id: str, request: Request):
        auth(request)
        body = await request.json()
        coll = srv.collections.get(item_id) or _missing()
        if body.get("Id") != item_id:
            raise HTTPException(400)  # the real API wants the whole item back
        coll.name = body["Name"]
        return Response(status_code=204)

    @r.post("/Items/RemoteSearch/{search_type}")
    async def remote_search(search_type: str, request: Request):
        auth(request)
        body = await request.json()
        info = body.get("SearchInfo", {})
        kind = "movie" if search_type == "Movie" else "show"
        ids = {k.lower(): v for k, v in (info.get("ProviderIds") or {}).items()}
        query = next((f"{k}-{v}" for k, v in ids.items()), info.get("Name", ""))
        out = []
        for key, (title, year) in search_catalog(kind, query):
            provider = (
                {"Tmdb": key, "Imdb": MOVIE_CATALOG[key][2]} if kind == "movie" else {"Tvdb": key}
            )
            out.append({"Name": title, "ProductionYear": year, "ProviderIds": provider})
        return out

    @r.post("/Items/RemoteSearch/Apply/{item_id}")
    async def remote_apply(item_id: str, request: Request):
        auth(request)
        found = srv.items.get(item_id) or _missing()
        body = await request.json()
        ids = body["ProviderIds"]
        apply_catalog(found, ids.get("Tmdb") or ids["Tvdb"])
        return Response(status_code=204)

    @r.post("/Collections")
    async def create(request: Request):
        auth(request)
        qp = request.query_params
        coll = Coll(srv.new_id(), qp["Name"], "", [i for i in qp.get("Ids", "").split(",") if i])
        srv.collections[coll.id] = coll
        return {"Id": coll.id}

    @r.post("/Collections/{cid}/Items")
    async def add(cid: str, request: Request):
        auth(request)
        srv.collections[cid].members += request.query_params["Ids"].split(",")
        return Response(status_code=204)

    return r


def create_fake_app(servers: dict[str, FakeServer] | None = None) -> FastAPI:
    servers = servers or default_servers()
    app = FastAPI()
    app.include_router(plex_router(servers["plex"]), prefix="/plex")
    app.include_router(jellyfin_router(servers["jellyfin"]), prefix="/jellyfin")
    app.include_router(jellyfin_router(servers["emby"]), prefix="/embysrv/emby")
    app.state.servers = servers
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_fake_app(), host="127.0.0.1", port=32499)
