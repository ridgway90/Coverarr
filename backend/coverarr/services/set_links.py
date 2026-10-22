"""Artwork from Kometa-style YAML with image URLs: what MediUX's "Kometa YAML" button gives,
and what Kometa metadata files use.

Entries are keyed by TMDb/TVDb id or "Title (Year)", with ``url_poster`` and ``url_background``
and, for shows, ``seasons`` and their ``episodes``. Each image is downloaded as the YAML says
(nothing is crawled), and only from the poster sites, TMDb, GitHub and Imgur.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from sqlalchemy import select

from coverarr.clients.base import USER_AGENT
from coverarr.models import MediaItem
from coverarr.services.artwork import (
    MAX_BYTES,
    ArtworkError,
    SetMatcher,
    parse_set_name,
    save_artwork,
)
from coverarr.services.jobs import JobContext
from coverarr.services.kometa_yaml import KometaYamlError, _check_size

# Hosts images are downloaded from (https only): the poster sites, TMDb and GitHub.
ALLOWED_HOSTS = (
    "mediux.pro",
    "mediux.io",
    "theposterdb.com",
    "image.tmdb.org",
    "githubusercontent.com",
    "imgur.com",
)


class SetLinkError(Exception):
    pass


def _allowed(url: str) -> bool:
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_HOSTS
    )


async def _download(http: httpx.AsyncClient, url: str) -> bytes:
    if not _allowed(url):
        raise SetLinkError(
            f"{url}: Coverarr only downloads images over https from MediUX, ThePosterDB, TMDb, "
            "GitHub and Imgur"
        )
    try:
        async with http.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
            if resp.status_code >= 400:
                raise SetLinkError(f"{url}: HTTP {resp.status_code}")
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    raise SetLinkError(f"{url}: larger than 25 MB")
            return bytes(data)
    except httpx.HTTPError as exc:
        raise SetLinkError(f"{url}: {exc}") from exc


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=60, follow_redirects=True, transport=transport)


# Tests swap in a mock transport.
transport: httpx.AsyncBaseTransport | None = None


# --- Kometa / MediUX YAML ------------------------------------------------------------------


@dataclass
class UrlImage:
    key: Any  # TMDb/TVDb id, or "Title (Year)"
    what: str  # poster | background
    url: str
    season: int | None = None
    episode: int | None = None


# Kometa metadata-file fields Coverarr manages (services/metadata.py).
EDIT_FIELDS = {"title": "title", "sort_title": "sort_title", "summary": "summary"}
LABEL_FIELDS = ("label", "label.sync", "labels")


def _edits(body: dict[str, Any]) -> dict[str, Any]:
    from coverarr.services.metadata import clean_edits

    raw: dict[str, Any] = {k: body.get(f) for f, k in EDIT_FIELDS.items()}
    for field in LABEL_FIELDS:
        if body.get(field) is not None:
            raw["labels"] = body[field]
    return clean_edits(raw)


def parse_metadata_edits(text: str) -> list[tuple[Any, dict[str, Any]]]:
    """(key, edits) for each title in a Kometa metadata file that sets a title, sort title,
    summary or labels."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("metadata"), dict):
        data = data["metadata"]
    out = []
    for key, body in data.items():
        if isinstance(body, dict) and (edits := _edits(body)):
            out.append((key, edits))
    return out


def parse_artwork_yaml(text: str, allow_empty: bool = False) -> list[UrlImage]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KometaYamlError(f"Not valid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise KometaYamlError("Expected entries keyed by id or title")
    _check_size(data, "the YAML")
    if isinstance(data.get("metadata"), dict):
        data = data["metadata"]
    out: list[UrlImage] = []

    def images(key: Any, body: Any, season: int | None = None, episode: int | None = None):
        if not isinstance(body, dict):
            return
        for field, what in (("url_poster", "poster"), ("url_background", "background")):
            if isinstance(body.get(field), str) and body[field].strip():
                out.append(UrlImage(key, what, body[field].strip(), season, episode))

    for key, body in data.items():
        images(key, body)
        seasons = body.get("seasons") if isinstance(body, dict) else None
        for s_num, s_body in (seasons or {}).items() if isinstance(seasons, dict) else []:
            if not str(s_num).isdigit():
                continue
            images(key, s_body, int(s_num))
            episodes = s_body.get("episodes") if isinstance(s_body, dict) else None
            for e_num, e_body in (episodes or {}).items() if isinstance(episodes, dict) else []:
                if str(e_num).isdigit():
                    images(key, e_body, int(s_num), int(e_num))
    if not out and not allow_empty:
        raise KometaYamlError("Found no url_poster or url_background in it")
    return out


async def run_url_artwork(ctx: JobContext) -> dict[str, Any]:
    """Download the images in Kometa-style YAML and use them as bases (posters, title cards
    and backgrounds)."""
    from coverarr.services.backgrounds import save_background
    from coverarr.services.review import refresh_issues

    config_dir = Path(ctx.params["config_dir"])
    keep_choices = bool(ctx.params.get("keep_choices"))
    wanted = parse_artwork_yaml(ctx.params["yaml"], allow_empty=True)
    meta_edits = parse_metadata_edits(ctx.params["yaml"])
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
        children = (
            await session.execute(
                select(
                    MediaItem.id,
                    MediaItem.kind,
                    MediaItem.parent_id,
                    MediaItem.season_number,
                    MediaItem.episode_number,
                ).where(MediaItem.kind.in_(("season", "episode")))
            )
        ).all()
    matcher = SetMatcher(items, [c for c in children if c.kind == "season"], [])
    seasons = {(c.parent_id, c.season_number): c.id for c in children if c.kind == "season"}
    episodes = {(c.parent_id, c.episode_number): c.id for c in children if c.kind == "episode"}

    def owners(image: UrlImage) -> list[int]:
        if str(image.key).isdigit():
            key = str(image.key)
            show_first = image.season is not None
            found = [
                i for i in items if (i.tvdb_id == key and i.kind == "show") or (i.tmdb_id == key)
            ]
            if show_first and any(i.kind == "show" for i in found):
                found = [i for i in found if i.kind == "show"]
            tops = [i.id for i in found]
        else:
            parsed = parse_set_name(f"{image.key}.jpg")
            tops = [t.id for t in matcher._titles(parsed)] if parsed else []
        if image.season is None:
            return tops
        out = []
        for top in tops:
            season = seasons.get((top, image.season))
            if season is None:
                continue
            if image.episode is None:
                out.append(season)
            elif (ep := episodes.get((season, image.episode))) is not None:
                out.append(ep)
        return out

    counts = {"imported": 0, "kept_choice": 0, "unmatched": 0, "failed": 0, "edited": 0}
    unmatched: list[str] = []
    if meta_edits:
        # Title / sort title / summary / labels: merged over what's set in Coverarr already,
        # and pushed with the other pending changes.
        async with ctx.sessionmaker() as session:
            for key, edits in meta_edits:
                targets = owners(UrlImage(key, "edit", ""))
                if not targets:
                    counts["unmatched"] += 1
                    unmatched.append(str(key))
                for target_id in targets:
                    item = await session.get(MediaItem, target_id)
                    if item is not None:
                        item.metadata_edits = {**(item.metadata_edits or {}), **edits}
                        counts["edited"] += 1
            await session.commit()
    cache: dict[str, bytes | None] = {}
    async with _client() as http:
        for n, image in enumerate(wanted, 1):
            label = (
                f"{image.key}"
                + (f" S{image.season:02d}" if image.season is not None else "")
                + (f"E{image.episode:02d}" if image.episode is not None else "")
            )
            targets = owners(image)
            if not targets:
                counts["unmatched"] += 1
                unmatched.append(label)
                continue
            if image.url not in cache:
                try:
                    cache[image.url] = await _download(http, image.url)
                except SetLinkError as exc:
                    cache[image.url] = None
                    await ctx.log(f"{label}: {exc}")
            data = cache[image.url]
            if data is None:
                counts["failed"] += 1
                continue
            async with ctx.sessionmaker() as session:
                for target_id in targets:
                    item = await session.get(MediaItem, target_id)
                    if item is None:
                        continue
                    try:
                        if image.what == "background":
                            if item.kind not in ("movie", "show"):
                                continue
                            choose = not (keep_choices and item.background_source)
                            await save_background(
                                session,
                                config_dir,
                                media_item_id=item.id,
                                source="set",
                                data=data,
                                name=image.url.rsplit("/", 1)[-1],
                                choose=choose,
                            )
                        else:
                            choose = not (keep_choices and item.poster_source)
                            await save_artwork(
                                session,
                                config_dir,
                                source="set",
                                data=data,
                                name=image.url.rsplit("/", 1)[-1],
                                media_item_id=item.id,
                                choose=choose,
                            )
                    except ArtworkError as exc:
                        counts["failed"] += 1
                        await ctx.log(f"{label}: {exc}")
                        continue
                    counts["imported" if choose else "kept_choice"] += 1
                await session.commit()
            await ctx.progress(0.95 * n / len(wanted), f"Imported {n}/{len(wanted)}")
        async with ctx.sessionmaker() as session:
            await refresh_issues(session)
            await session.commit()
    await ctx.log(
        f"Imported {counts['imported']} images"
        + (f", edited {counts['edited']} titles" if counts["edited"] else "")
        + f"; {counts['unmatched']} entries matched nothing"
        + (f"; {counts['failed']} failed" if counts["failed"] else "")
    )
    if unmatched:
        await ctx.log("No match for: " + ", ".join(unmatched[:30]))
    await ctx.progress(1.0, "Images imported")
    return {**counts, "unmatched_entries": unmatched[:100]}
