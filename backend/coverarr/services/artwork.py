"""Artwork Coverarr keeps for a title or collection: your own uploads, posters picked from TMDb
and poster sets (e.g. a set downloaded from ThePosterDB), stored in the assets tree as
``poster-upload.jpg``, ``poster-tmdb.jpg`` and ``poster-set.jpg``.

Saving artwork makes it the chosen base poster, so it shows up under Changes until pushed.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import Artwork, Collection, MediaItem
from coverarr.services.assets import (
    artwork_path,
    describe_owners,
    remove_asset,
    taken_paths,
    write_asset,
)
from coverarr.services.jobs import JobContext
from coverarr.services.kometa import IMAGE_SUFFIXES, Source, prepare
from coverarr.services.lists import ListClient, ListError
from coverarr.services.matching import normalize_collection_name, normalize_title
from coverarr.services.settings_store import get_setting

MAX_BYTES = 25 * 1024 * 1024
TMDB_IMAGES = "https://image.tmdb.org/t/p"


class ArtworkError(ValueError):
    pass


async def save_artwork(
    session: AsyncSession,
    config_dir: Path,
    *,
    source: str,
    data: bytes,
    name: str | None = None,
    media_item_id: int | None = None,
    collection_id: int | None = None,
    choose: bool = True,
    taken: dict[str, str] | None = None,
) -> Artwork:
    """Store ``data`` as this owner's ``source`` artwork (replacing any earlier one) and,
    with ``choose``, make it the base poster."""
    if len(data) > MAX_BYTES:
        raise ArtworkError("That image is larger than 25 MB")
    prepared = await asyncio.to_thread(prepare, name or "poster", data)
    if prepared is None:
        raise ArtworkError("That file isn't a JPEG, PNG or WebP image")
    owner_filter = (
        Artwork.media_item_id == media_item_id
        if media_item_id
        else Artwork.collection_id == collection_id
    )
    old = await session.scalar(select(Artwork).where(Artwork.source == source, owner_filter))
    target = (
        await session.get(MediaItem, media_item_id)
        if media_item_id
        else await session.get(Collection, collection_id)
    )
    if target is None:
        raise ArtworkError("That title or collection no longer exists")

    key = f"m{media_item_id}" if media_item_id else f"c{collection_id}"
    owners = await describe_owners(
        session, [media_item_id] if media_item_id else [], [collection_id] if collection_id else []
    )
    folder, stem = owners[key]
    if taken is None:
        taken = await taken_paths(session)
    if old is not None:
        taken.pop(old.file_path, None)
    rel = artwork_path(key, folder, stem, source, prepared.suffix, taken)
    await asyncio.to_thread(
        write_asset, config_dir, rel, prepared.data, old.file_path if old else None
    )

    # A fresh row (new id) each time, so cached thumbnails never show the old image.
    previous_choice = old is not None and target.poster_source == f"artwork:{old.id}"
    art = Artwork(
        source=source,
        media_item_id=media_item_id,
        collection_id=collection_id,
        file_path=str(rel),
        original_name=name,
        width=prepared.width,
        height=prepared.height,
        poster_hash=prepared.hash,
        poster_edges=prepared.edges,
    )
    session.add(art)
    await session.flush()  # before deleting the old row, so SQLite can't reuse its id
    if old is not None:
        await session.delete(old)
    if choose or previous_choice:
        target.poster_source = f"artwork:{art.id}"
    return art


async def delete_artwork(session: AsyncSession, config_dir: Path, art: Artwork) -> None:
    """Remove artwork Coverarr holds; if it was the chosen base, the choice is cleared."""
    choice = f"artwork:{art.id}"
    for model, owner in ((MediaItem, art.media_item_id), (Collection, art.collection_id)):
        if owner:
            target = await session.get(model, owner)
            if target is not None and target.poster_source == choice:
                target.poster_source = None
    await asyncio.to_thread(remove_asset, config_dir, art.file_path)
    await session.delete(art)


# --- TMDb ------------------------------------------------------------------------------------


async def _list_client(session: AsyncSession) -> ListClient:
    return ListClient(tmdb_key=await get_setting(session, "tmdb_api_key"), trakt_client_id=None)


async def _tmdb_tv_id(client: ListClient, show: MediaItem) -> str | None:
    if show.tmdb_id:
        return show.tmdb_id
    for source, value in (("tvdb_id", show.tvdb_id), ("imdb_id", show.imdb_id)):
        if value:
            found = await client.tmdb_json(f"/find/{value}", external_source=source)
            for row in found.get("tv_results") or []:
                return str(row["id"])
    return None


async def tmdb_posters(
    session: AsyncSession,
    *,
    media_item_id: int | None = None,
    collection_id: int | None = None,
) -> dict[str, Any]:
    """Poster candidates from TMDb for a title, season or collection, best first."""
    client = await _list_client(session)
    async with client:
        try:
            path, label = await _tmdb_images_path(session, client, media_item_id, collection_id)
            data = await client.tmdb_json(path, include_image_language="en,null")
        except ListError as exc:
            raise ArtworkError(str(exc)) from exc
    language = "en"
    posters = sorted(
        data.get("posters") or [],
        key=lambda p: (
            # your language, then textless, then the rest
            {language: 0, None: 1}.get(p.get("iso_639_1"), 2),
            -(p.get("vote_average") or 0),
            -(p.get("width") or 0),
        ),
    )
    return {
        "source": label,
        "posters": [
            {
                "file_path": p["file_path"],
                "thumb": f"{TMDB_IMAGES}/w342{p['file_path']}",
                "width": p.get("width"),
                "height": p.get("height"),
                "language": p.get("iso_639_1"),
                "votes": p.get("vote_count") or 0,
                "rating": round(p.get("vote_average") or 0, 1),
            }
            for p in posters[:60]
        ],
    }


async def _tmdb_images_path(
    session: AsyncSession,
    client: ListClient,
    media_item_id: int | None,
    collection_id: int | None,
) -> tuple[str, str]:
    if collection_id is not None:
        coll = await session.get(Collection, collection_id)
        if coll is None:
            raise ArtworkError("Collection not found")
        found = await client.tmdb_json("/search/collection", query=coll.name)
        wanted = normalize_collection_name(coll.name)
        results = found.get("results") or []
        best = next(
            (r for r in results if normalize_collection_name(r.get("name")) == wanted),
            results[0] if results else None,
        )
        if best is None:
            raise ArtworkError(f"TMDb has no collection called {coll.name!r}")
        return f"/collection/{best['id']}/images", f"TMDb collection: {best.get('name')}"

    item = await session.get(MediaItem, media_item_id)
    if item is None:
        raise ArtworkError("Title not found")
    if item.kind == "movie":
        if not item.tmdb_id:
            raise ArtworkError("This film has no TMDb id, so TMDb can't be searched for it")
        return f"/movie/{item.tmdb_id}/images", "TMDb"
    show, season = item, None
    if item.kind == "season":
        show, season = await session.get(MediaItem, item.parent_id), item
    elif item.kind == "episode":
        raise ArtworkError("TMDb has stills, not posters, for episodes; upload one instead")
    if show is None:
        raise ArtworkError("Show not found")
    tv_id = await _tmdb_tv_id(client, show)
    if tv_id is None:
        raise ArtworkError("Couldn't find this show on TMDb (no TMDb, TVDB or IMDb id)")
    if season is not None:
        return f"/tv/{tv_id}/season/{season.season_number or 0}/images", "TMDb"
    return f"/tv/{tv_id}/images", "TMDb"


async def download_tmdb(session: AsyncSession, file_path: str) -> bytes:
    if not re.fullmatch(r"/[A-Za-z0-9_\-]+\.(jpg|jpeg|png|webp)", file_path):
        raise ArtworkError("Not a TMDb image path")
    client = await _list_client(session)
    async with client:
        try:
            return await client.download(f"{TMDB_IMAGES}/original{file_path}")
        except ListError as exc:
            raise ArtworkError(str(exc)) from exc


# --- poster sets -----------------------------------------------------------------------------

# "Title (2009)", "Show (2008) - Season 1", "Show - Specials", "Name Collection",
# optionally with "{tmdb-123}" / "{tvdb-456}" / "{imdb-tt789}" anywhere.
SET_NAME = re.compile(
    r"^(?P<title>.*?)(?:\s*\((?P<year>\d{4})\))?"
    r"(?:\s*-\s*(?:(?:Season|Series)\s*(?P<season>\d+)|(?P<specials>Specials)))?\s*$",
    re.IGNORECASE,
)
ID_TAG = re.compile(r"[\{\[](tmdb|tvdb|imdb)(?:id)?[-=]([\w]+)[\}\]]", re.IGNORECASE)


@dataclass
class SetFile:
    name: str
    title: str
    year: int | None
    season: int | None  # 0 = specials
    ids: dict[str, str]


def parse_set_name(name: str) -> SetFile | None:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.suffix.lower() not in IMAGE_SUFFIXES or path.name.startswith("."):
        return None
    stem = path.stem
    ids = {k.lower(): v for k, v in ID_TAG.findall(stem)}
    stem = re.sub(r"\s+", " ", ID_TAG.sub("", stem)).strip()
    m = SET_NAME.match(stem)
    if not m or not m.group("title").strip():
        return None
    season = 0 if m.group("specials") else (int(m.group("season")) if m.group("season") else None)
    return SetFile(
        path.name,
        m.group("title").strip(),
        int(m.group("year")) if m.group("year") else None,
        season,
        ids,
    )


class SetMatcher:
    """Finds what each file in a poster set belongs to."""

    def __init__(self, items: list[Any], seasons: list[Any], collections: list[Any]):
        self.by_title: dict[str, list[Any]] = defaultdict(list)
        self.by_id: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for it in items:
            self.by_title[normalize_title(it.title)].append(it)
            for key in ("tmdb", "tvdb", "imdb"):
                if getattr(it, f"{key}_id"):
                    self.by_id[(key, str(getattr(it, f"{key}_id")))].append(it)
        self.seasons = {(s.parent_id, s.season_number): s.id for s in seasons}
        self.collections: dict[str, list[int]] = defaultdict(list)
        for c in collections:
            self.collections[c.normalized_name].append(c.id)

    def _titles(self, f: SetFile) -> list[Any]:
        for key, value in f.ids.items():
            if self.by_id.get((key, value)):
                return self.by_id[(key, value)]
        candidates = self.by_title.get(normalize_title(f.title), [])
        if f.year is None:
            return candidates
        exact = [c for c in candidates if c.year == f.year]
        return exact or [c for c in candidates if c.year and abs(c.year - f.year) == 1]

    def match(self, f: SetFile) -> tuple[list[int], list[int]]:
        """(media item ids, collection ids) the file belongs to."""
        if f.season is not None:
            shows = [s for s in self._titles(f) if s.kind == "show"]
            ids = [
                self.seasons[(s.id, f.season)] for s in shows if (s.id, f.season) in self.seasons
            ]
            return ids, []
        is_collection = f.year is None and re.search(r"\bcollection$", f.title, re.IGNORECASE)
        colls = self.collections.get(normalize_collection_name(f.title), [])
        if is_collection and colls:
            return [], colls
        titles = [t.id for t in self._titles(f)]
        if titles:
            return titles, []
        return [], colls if f.year is None else []


async def run_poster_set(ctx: JobContext) -> dict[str, Any]:
    """Import a folder or zip of posters named by title (e.g. a ThePosterDB set)."""
    config_dir = Path(ctx.params["config_dir"])
    source_path = Path(ctx.params["source"])
    keep_choices = bool(ctx.params.get("keep_choices"))
    async with ctx.sessionmaker() as session:
        items = (
            await session.execute(
                select(
                    MediaItem.id,
                    MediaItem.kind,
                    MediaItem.title,
                    MediaItem.year,
                    MediaItem.tmdb_id,
                    MediaItem.tvdb_id,
                    MediaItem.imdb_id,
                ).where(MediaItem.kind.in_(("movie", "show")))
            )
        ).all()
        seasons = (
            await session.execute(
                select(MediaItem.id, MediaItem.parent_id, MediaItem.season_number).where(
                    MediaItem.kind == "season"
                )
            )
        ).all()
        colls = (await session.execute(select(Collection.id, Collection.normalized_name))).all()
    matcher = SetMatcher(items, seasons, colls)

    counts: dict[str, int] = defaultdict(int)
    unmatched: list[str] = []
    matched: list[str] = []
    source = await asyncio.to_thread(Source, source_path)
    try:
        entries = await asyncio.to_thread(source.entries)
        async with ctx.sessionmaker() as session:
            taken = await taken_paths(session)
            for idx, (name, reader) in enumerate(entries):
                parsed = parse_set_name(name)
                if parsed is None:
                    counts["skipped"] += 1
                    continue
                item_ids, coll_ids = matcher.match(parsed)
                if not item_ids and not coll_ids:
                    counts["unmatched"] += 1
                    unmatched.append(parsed.name)
                    continue
                data = await asyncio.to_thread(reader)
                for owner_items, owner_colls in [([i], []) for i in item_ids] + [
                    ([], [c]) for c in coll_ids
                ]:
                    target = (
                        await session.get(MediaItem, owner_items[0])
                        if owner_items
                        else await session.get(Collection, owner_colls[0])
                    )
                    choose = not (keep_choices and target.poster_source)
                    try:
                        await save_artwork(
                            session,
                            config_dir,
                            source="set",
                            data=data,
                            name=parsed.name,
                            media_item_id=owner_items[0] if owner_items else None,
                            collection_id=owner_colls[0] if owner_colls else None,
                            choose=choose,
                            taken=taken,
                        )
                    except ArtworkError as exc:
                        counts["unreadable"] += 1
                        await ctx.log(f"{parsed.name}: {exc}")
                        break
                    counts["imported"] += 1
                    counts["applied" if choose else "kept_choice"] += 1
                else:
                    matched.append(parsed.name)
                if idx % 20 == 0:
                    await session.commit()
                    await ctx.progress(
                        0.95 * idx / max(len(entries), 1), f"Read {idx}/{len(entries)}"
                    )
            from coverarr.services.review import refresh_issues

            await refresh_issues(session)
            await session.commit()
    finally:
        source.close()
        if ctx.params.get("remove_after"):
            await asyncio.to_thread(source_path.unlink, missing_ok=True)

    await ctx.log(
        f"Imported {counts['imported']} posters from {len(matched)} files; "
        f"{counts['unmatched']} files matched nothing"
    )
    if unmatched:
        await ctx.log("No match for: " + ", ".join(unmatched[:30]))
    await ctx.progress(1.0, "Poster set imported")
    return {**counts, "unmatched_files": unmatched[:100]}
