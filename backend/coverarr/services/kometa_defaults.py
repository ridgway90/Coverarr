"""Kometa's default collections (``- default: imdb`` and friends in config.yml), as Coverarr
list collections.

Chart defaults become chart collections. Defaults with one collection per value (genre,
decade, year, resolution, network, studio, content rating) become one library-filter
collection for each value the library has now. ``franchise`` looks every film up on TMDb and
makes a TMDb-collection collection for each franchise with two or more of your films; that
takes a while, so it runs as a job. Anything else is reported as not supported.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import CollectionDefinition, Library, MediaItem
from coverarr.services.jobs import JobContext
from coverarr.services.kometa_yaml import ParsedCollection
from coverarr.services.lists import ListError

# default -> [(name, builder, value, library kinds)]
CHARTS: dict[str, list[tuple[str, str, str, tuple[str, ...]]]] = {
    "imdb": [
        ("IMDb Top 250", "imdb_chart", "top_movies", ("movie",)),
        ("IMDb Popular", "imdb_chart", "popular_movies", ("movie",)),
        ("IMDb Lowest Rated", "imdb_chart", "lowest_rated", ("movie",)),
        ("IMDb Top 250", "imdb_chart", "top_shows", ("show",)),
        ("IMDb Popular", "imdb_chart", "popular_shows", ("show",)),
    ],
    "tmdb": [
        ("TMDb Popular", "tmdb_popular", "20", ("movie", "show")),
        ("TMDb Top Rated", "tmdb_top_rated", "20", ("movie", "show")),
        ("TMDb Trending", "tmdb_trending_weekly", "20", ("movie", "show")),
        ("TMDb Now Playing", "tmdb_now_playing", "20", ("movie",)),
        ("TMDb Airing Today", "tmdb_airing_today", "20", ("show",)),
    ],
    "trakt": [
        ("Trakt Popular", "trakt_popular", "20", ("movie", "show")),
        ("Trakt Trending", "trakt_trending", "20", ("movie", "show")),
        ("Trakt Anticipated", "trakt_anticipated", "20", ("movie", "show")),
        ("Trakt Most Watched", "trakt_watched_weekly", "20", ("movie", "show")),
        ("Trakt Box Office", "trakt_boxoffice", "20", ("movie",)),
    ],
}
# default -> library filter key; content_rating_us, content_rating_uk... all map to one.
PER_VALUE = {
    "genre": "genre",
    "decade": "decade",
    "year": "year",
    "resolution": "resolution",
    "network": "network",
    "studio": "studio",
    "content_rating": "content_rating",
}
RESOLUTION_NAMES = {"4k": "4K", "1080p": "1080p", "720p": "720p", "576p": "576p", "480p": "SD"}
MIN_FRANCHISE_FILMS = 2


def _per_value_key(default: str) -> str | None:
    if default.startswith("content_rating"):
        return "content_rating"
    return PER_VALUE.get(default)


def _name(key: str, value: str, kind: str) -> str:
    noun = "Films" if kind == "movie" else "Shows"
    if key == "decade":
        return f"{value}s"
    if key == "resolution":
        return f"{RESOLUTION_NAMES.get(value, value)} {noun}"
    if key == "content_rating":
        return f"Rated {value}"
    return value


async def library_values(session: AsyncSession, library: Library, key: str) -> list[str]:
    """Every value the library's titles have for ``key``, most titles first."""
    counts: dict[str, int] = defaultdict(int)
    rows = await session.execute(
        select(MediaItem.year, MediaItem.facts).where(
            MediaItem.library_id == library.id, MediaItem.kind == library.kind
        )
    )
    for year, facts in rows:
        facts = facts or {}
        if key == "year":
            values = [str(year)] if year else []
        elif key == "decade":
            values = [str(year // 10 * 10)] if year else []
        elif key == "genre":
            values = [str(g) for g in facts.get("genres") or []]
        else:
            values = [str(facts[key])] if facts.get(key) else []
        for value in values:
            counts[value] += 1
    if key in ("year", "decade"):
        return sorted(counts, reverse=True)
    return sorted(counts, key=lambda v: (-counts[v], v.lower()))


async def expand_defaults(
    session: AsyncSession, library: Library, defaults: list[str]
) -> tuple[list[ParsedCollection], list[str], bool]:
    """(collections, defaults not supported, whether franchises were asked for)."""
    parsed: list[ParsedCollection] = []
    unsupported: list[str] = []
    franchise = False
    for default in defaults:
        source = f"default: {default}"
        if default in CHARTS:
            for name, builder, value, kinds in CHARTS[default]:
                if library.kind in kinds:
                    parsed.append(
                        ParsedCollection(
                            name=name,
                            source_file=source,
                            builders=[{"type": builder, "value": value}],
                            library_id=library.id,
                        )
                    )
        elif key := _per_value_key(default):
            if key == "network" and library.kind != "show":
                unsupported.append(default)
                continue
            for value in await library_values(session, library, key):
                parsed.append(
                    ParsedCollection(
                        name=_name(key, value, library.kind),
                        source_file=source,
                        builders=[{"type": "library_filter", "value": f"{key}={value}"}],
                        options={"collection_order": "alpha"},
                        library_id=library.id,
                    )
                )
        elif default == "franchise" and library.kind == "movie":
            franchise = True
        else:
            unsupported.append(default)
    return parsed, unsupported, franchise


async def run_kometa_franchises(ctx: JobContext) -> dict[str, Any]:
    """Kometa's ``franchise`` default: a collection for each TMDb collection with two or more
    of the library's films."""
    from coverarr.services.definitions import _lists

    library_id = int(ctx.params["library_id"])
    async with ctx.sessionmaker() as session:
        library = await session.get(Library, library_id)
        if library is None:
            raise ValueError("That library no longer exists")
        films = (
            await session.execute(
                select(MediaItem.tmdb_id).where(
                    MediaItem.library_id == library_id,
                    MediaItem.kind == "movie",
                    MediaItem.tmdb_id.is_not(None),
                )
            )
        ).all()
        lists = await _lists(session)
    groups: dict[str, tuple[str, set[str]]] = {}
    failed = 0
    sem = asyncio.Semaphore(8)
    async with lists:

        async def look(tmdb_id: str) -> None:
            nonlocal failed
            async with sem:
                try:
                    data = await lists.tmdb_json(f"/movie/{tmdb_id}")
                except ListError:
                    failed += 1
                    return
            coll = data.get("belongs_to_collection") or {}
            if coll.get("id"):
                name, members = groups.setdefault(str(coll["id"]), (coll.get("name") or "", set()))
                members.add(tmdb_id)

        done = 0
        for chunk in range(0, len(films), 200):
            await asyncio.gather(*(look(str(t)) for (t,) in films[chunk : chunk + 200]))
            done += len(films[chunk : chunk + 200])
            await ctx.progress(0.95 * done / max(len(films), 1), f"Looked up {done}/{len(films)}")
    made = 0
    async with ctx.sessionmaker() as session:
        existing = set(
            await session.scalars(
                select(CollectionDefinition.name).where(
                    CollectionDefinition.library_id == library_id
                )
            )
        )
        for coll_id, (name, members) in sorted(groups.items(), key=lambda g: g[1][0]):
            if len(members) < MIN_FRANCHISE_FILMS or not name or name in existing:
                continue
            session.add(
                CollectionDefinition(
                    library_id=library_id,
                    name=name,
                    source="kometa",
                    source_file="default: franchise",
                    builders=[{"type": "tmdb_collection", "value": coll_id}],
                    options={"collection_order": "release"},
                    unsupported=[],
                )
            )
            existing.add(name)
            made += 1
        await session.commit()
    await ctx.log(
        f"{made} franchise collections from {len(films)} films"
        + (f"; {failed} couldn't be looked up" if failed else "")
    )
    return {"franchises": made, "films": len(films), "failed": failed}
