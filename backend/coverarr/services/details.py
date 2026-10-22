"""Details about each film and show that the media servers don't have, from TMDb (and MDBList
when there's a key): where it streams in your region, ratings, a show's status, directors,
cast, countries, and which charts or awards it's on (for ribbons).

They feed library-filter collections (``streaming=Netflix``, ``director=...``), Kometa's
streaming / people / country defaults, and the streaming, status, ribbon and rating overlays.
A title is fetched again after a week, as streaming services come and go.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from coverarr.models import MediaItem
from coverarr.services.jobs import JobContext
from coverarr.services.lists import MDBLIST_API, ListClient, ListError
from coverarr.services.settings_store import get_setting

STALE_DAYS = 7
MAX_CAST = 10
# MDBList's free tier allows 1,000 requests a day.
MDBLIST_PER_RUN = 900
DEFAULT_REGION = "US"
# MDBList rating source -> details key.
MDBLIST_RATINGS = {
    "imdb": "imdb_rating",
    "tomatoes": "rt_rating",
    "tomatoesaudience": "rt_audience",
    "metacritic": "metacritic",
    "letterboxd": "letterboxd_rating",
}
# Ribbon -> the list it marks, per library kind. Kometa's ribbon overlay keys.
RIBBONS: dict[str, dict[str, tuple[str, str]]] = {
    "imdb_top_250": {"movie": ("imdb_chart", "top_movies"), "show": ("imdb_chart", "top_shows")},
    "imdb_lowest": {"movie": ("imdb_chart", "lowest_rated")},
    "oscars_best_picture": {"movie": ("wikidata_award", "oscars_best_picture")},
    "oscars": {"movie": ("wikidata_award", "oscars")},
    "bafta_best_film": {"movie": ("wikidata_award", "bafta_best_film")},
    "golden_globe_best_drama": {"movie": ("wikidata_award", "golden_globe_best_drama")},
    "cannes_palme_dor": {"movie": ("wikidata_award", "cannes_palme_dor")},
    "berlinale_golden_bear": {"movie": ("wikidata_award", "berlinale_golden_bear")},
    "venice_golden_lion": {"movie": ("wikidata_award", "venice_golden_lion")},
    "emmys_best_drama": {"show": ("wikidata_award", "emmys_best_drama")},
    "razzie_worst_picture": {"movie": ("wikidata_award", "razzie_worst_picture")},
}
STATUS = {
    "Returning Series": "returning",
    "In Production": "returning",
    "Planned": "returning",
    "Pilot": "returning",
    "Ended": "ended",
    "Canceled": "canceled",
    "Cancelled": "canceled",
}


def parse_tmdb(data: dict[str, Any], kind: str, region: str, today: date) -> dict[str, Any]:
    """The details kept from TMDb's answer (with watch/providers and credits appended)."""
    out: dict[str, Any] = {}
    if data.get("vote_count"):
        out["tmdb_rating"] = round(float(data.get("vote_average") or 0), 1)
        out["tmdb_votes"] = int(data["vote_count"])
    providers = ((data.get("watch/providers") or {}).get("results") or {}).get(region.upper())
    streaming = sorted(
        (providers or {}).get("flatrate") or [], key=lambda p: p.get("display_priority", 999)
    )
    out["streaming"] = list(
        dict.fromkeys(p["provider_name"] for p in streaming if p.get("provider_name"))
    )
    countries = [c.get("name") for c in data.get("production_countries") or [] if c.get("name")]
    if countries:
        out["countries"] = countries
    if kind == "movie":
        if data.get("runtime"):
            out["runtime"] = int(data["runtime"])
        crew = (data.get("credits") or {}).get("crew") or []
        out["directors"] = [c["name"] for c in crew if c.get("job") == "Director" and c.get("name")]
        cast = (data.get("credits") or {}).get("cast") or []
    else:
        runtimes = data.get("episode_run_time") or []
        if runtimes:
            out["runtime"] = int(runtimes[0])
        out["directors"] = [c["name"] for c in data.get("created_by") or [] if c.get("name")]
        cast = (data.get("aggregate_credits") or {}).get("cast") or []
        status = STATUS.get(str(data.get("status")))
        upcoming = (data.get("next_episode_to_air") or {}).get("air_date")
        if status == "returning" and upcoming:
            try:
                if date.fromisoformat(upcoming) <= today + timedelta(days=7):
                    status = "airing"
            except ValueError:
                pass
        if status:
            out["status"] = status
    out["cast"] = [c["name"] for c in cast[:MAX_CAST] if c.get("name")]
    return out


def parse_mdblist(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rating in data.get("ratings") or []:
        key = MDBLIST_RATINGS.get(str(rating.get("source")))
        value = rating.get("value")
        if key and value not in (None, "", 0):
            out[key] = value
    return out


def _stale(details: dict[str, Any] | None, now: datetime) -> bool:
    fetched = (details or {}).get("fetched")
    if not fetched:
        return True
    try:
        return datetime.fromisoformat(fetched) < now - timedelta(days=STALE_DAYS)
    except ValueError:
        return True


async def _fetch_one(
    lists: ListClient, item: Any, region: str, today: date, use_mdblist: bool
) -> dict[str, Any] | None:
    if item.kind == "movie":
        if not item.tmdb_id:
            return None
        tmdb_id = item.tmdb_id
        path = f"/movie/{tmdb_id}"
        append = "watch/providers,credits"
    else:
        tmdb_id = item.tmdb_id
        for source, value in (("tvdb_id", item.tvdb_id), ("imdb_id", item.imdb_id)):
            if tmdb_id or not value:
                continue
            found = await lists.tmdb_json(f"/find/{value}", external_source=source)
            tmdb_id = next((str(r["id"]) for r in found.get("tv_results") or []), None)
        if not tmdb_id:
            return None
        path = f"/tv/{tmdb_id}"
        append = "watch/providers,aggregate_credits"
    data = await lists.tmdb_json(path, append_to_response=append)
    details = parse_tmdb(data, item.kind, region, today)
    if use_mdblist:
        media = "movie" if item.kind == "movie" else "show"
        try:
            resp = await lists._get(
                f"{MDBLIST_API}/tmdb/{media}/{tmdb_id}", params={"apikey": lists.mdblist_key}
            )
            details.update(parse_mdblist(resp.json()))
        except ListError:
            pass  # ratings are a bonus; the rest still counts
    return details


async def ribbon_lists(session: Any) -> list[str]:
    """The ribbons any overlay layer uses."""
    from coverarr.services.overlays import load_config

    config = await load_config(session)
    wanted: list[str] = []
    for lib in (config.get("libraries") or {}).values():
        for layers in (lib.get("levels") or {}).values():
            for layer in layers:
                if layer.get("type") == "ribbon" and layer.get("enabled", True):
                    wanted += [r for r in layer.get("ribbons") or [] if r in RIBBONS]
    return list(dict.fromkeys(wanted))


async def run_fetch_details(ctx: JobContext) -> dict[str, Any]:
    """Fetch details for every film and show that has none yet or whose are a week old."""
    from coverarr.services.definitions import _lists

    now = datetime.now(UTC)
    force = bool(ctx.params.get("force"))
    async with ctx.sessionmaker() as session:
        if not await get_setting(session, "tmdb_api_key"):
            await ctx.log("No TMDb API key, so no details were fetched")
            return {"fetched": 0, "skipped": "no TMDb key"}
        region = str(await get_setting(session, "watch_region") or DEFAULT_REGION).upper()
        if not re.fullmatch(r"[A-Z]{2}", region):
            region = DEFAULT_REGION
        query = select(
            MediaItem.id,
            MediaItem.kind,
            MediaItem.library_id,
            MediaItem.tmdb_id,
            MediaItem.tvdb_id,
            MediaItem.imdb_id,
            MediaItem.details,
        ).where(MediaItem.kind.in_(("movie", "show")))
        if ctx.params.get("media_item_ids"):
            query = query.where(MediaItem.id.in_(ctx.params["media_item_ids"]))
        items = (await session.execute(query)).all()
        ribbons = await ribbon_lists(session)
        lists = await _lists(session)
    todo = [i for i in items if force or _stale(i.details, now)]
    counts = {"fetched": 0, "failed": 0, "no_id": 0}
    mdblist_left = MDBLIST_PER_RUN if lists.mdblist_key else 0
    sem = asyncio.Semaphore(8)
    async with lists:

        async def one(item: Any) -> tuple[Any, dict[str, Any] | None]:
            nonlocal mdblist_left
            use_mdblist = mdblist_left > 0
            mdblist_left -= 1 if use_mdblist else 0
            async with sem:
                try:
                    details = await _fetch_one(lists, item, region, now.date(), use_mdblist)
                except ListError:
                    counts["failed"] += 1
                    return item, None
            if details is None:
                counts["no_id"] += 1
            return item, details

        for start in range(0, len(todo), 100):
            batch = await asyncio.gather(*(one(i) for i in todo[start : start + 100]))
            async with ctx.sessionmaker() as session:
                for item, details in batch:
                    row = await session.get(MediaItem, item.id)
                    if row is None:
                        continue
                    keep = {"ribbons": (row.details or {}).get("ribbons", [])}
                    if details is None:
                        # Nothing on TMDb for it: try again in a week, not every run.
                        row.details = {
                            **(row.details or {}),
                            "fetched": now.isoformat(timespec="seconds"),
                        }
                        continue
                    row.details = {**keep, **details, "fetched": now.isoformat(timespec="seconds")}
                    counts["fetched"] += 1
                await session.commit()
            done = min(start + 100, len(todo))
            await ctx.progress(0.8 * done / max(len(todo), 1), f"Details for {done}/{len(todo)}")

        marked = await _mark_ribbons(ctx, lists, ribbons) if ribbons else 0
    await ctx.log(
        f"Fetched details for {counts['fetched']} of {len(todo)} titles"
        + (f"; {counts['failed']} failed" if counts["failed"] else "")
        + (f"; {marked} titles carry a ribbon" if ribbons else "")
    )
    await ctx.progress(1.0, "Details fetched")
    return {**counts, "considered": len(todo), "ribbons": marked}


async def _mark_ribbons(ctx: JobContext, lists: ListClient, ribbons: list[str]) -> int:
    """Set each title's ``ribbons``: which of the wanted charts and awards it's on."""
    ids: dict[tuple[str, str], set[str]] = {}
    failed: set[str] = set()
    for ribbon in ribbons:
        for kind, (builder, value) in RIBBONS[ribbon].items():
            try:
                entries = await lists.resolve({"type": builder, "value": value}, kind)
            except ListError as exc:
                await ctx.log(f"Ribbon {ribbon}: {exc}; titles keep the ribbon they had")
                failed.add(ribbon)
                continue
            ids[(ribbon, kind)] = {f"imdb:{e.imdb_id}" for e in entries if e.imdb_id} | {
                f"tmdb:{e.tmdb_id}" for e in entries if e.tmdb_id
            }
    marked = 0
    async with ctx.sessionmaker() as session:
        for row in await session.scalars(
            select(MediaItem).where(MediaItem.kind.in_(("movie", "show")))
        ):
            keys = {f"imdb:{row.imdb_id}", f"tmdb:{row.tmdb_id}"}
            old = (row.details or {}).get("ribbons", [])
            have = [
                r
                for r in ribbons
                if (r in failed and r in old) or keys & ids.get((r, row.kind), set())
            ]
            if have != old:
                row.details = {**(row.details or {}), "ribbons": have}
            marked += bool(have)
        await session.commit()
    return marked
