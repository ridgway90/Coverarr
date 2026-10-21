"""Radarr and Sonarr webhooks: when a file is imported, upgraded, renamed or deleted, only that
title is refreshed from the servers and its posters pushed, instead of a full import.

A webhook first asks every server to scan the title's folder. After a short wait Coverarr looks
for the title on each server (by id, or among the most recently added) and checks that the
server lists the new files; servers that are still scanning are tried again a few times. Then
the title is matched, new titles get a base poster from TMDb, and its changes are pushed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.clients import NotFoundError, client_for
from coverarr.clients.base import MediaServerError, RemoteItem, RemoteLibrary
from coverarr.models import Library, MediaItem, Server, ServerItem, ServerLibrary
from coverarr.services.importer import apply_remote_item, fill_show_paths, upsert_items
from coverarr.services.jobs import JobContext, JobRunner
from coverarr.services.matching import match_all
from coverarr.services.mediainfo import refresh_facts
from coverarr.services.settings_store import get_setting

log = logging.getLogger(__name__)

AUTOMATION_KEY = "automation"
AUTOMATION_DEFAULTS: dict[str, Any] = {
    # Ask each server to scan the title's folder as soon as the webhook arrives.
    "scan_servers": True,
    # Seconds to give the servers before looking for the new files.
    "wait_seconds": 30,
    # Base poster for titles Coverarr hasn't seen before: "tmdb" or "none".
    "new_base": "tmdb",
    # Push the title's changes (posters, overlays) once it's on every server.
    "auto_push": True,
    # Weekly safety-net scan (services/schedule.py). weekday: 0 = Monday.
    "weekly_scan": {"enabled": False, "weekday": 6, "hour": 4},
    # Daily sync of the collections that are switched on (services/definitions.py).
    "collections_sync": {"enabled": True, "hour": 3},
}
# Further looks for servers that haven't picked the files up yet (seconds after the last).
RETRY_DELAYS = (60, 120, 300, 900)
RECENT_LIMIT = 50


async def automation_settings(session: AsyncSession) -> dict[str, Any]:
    stored = await get_setting(session, AUTOMATION_KEY) or {}
    merged = {**AUTOMATION_DEFAULTS, **stored}
    for key in ("weekly_scan", "collections_sync"):
        merged[key] = {**AUTOMATION_DEFAULTS[key], **(stored.get(key) or {})}
    return merged


def _basename(path: str | None) -> str:
    return re.split(r"[\\/]", (path or "").rstrip("/\\"))[-1]


@dataclass
class ArrEvent:
    source: str  # radarr | sonarr
    action: str  # import | upgrade | rename | delete
    kind: str  # movie | show
    title: str
    year: int | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None
    folder: str | None = None
    # File names (not paths: the *arr and the servers may mount the media differently).
    files: list[str] = field(default_factory=list)
    episodes: list[list[int]] = field(default_factory=list)

    @property
    def key(self) -> str:
        ident = self.tmdb_id or self.tvdb_id or self.imdb_id or self.title.lower()
        return f"{self.kind}:{ident}"

    def merge(self, other: ArrEvent) -> None:
        """Fold a later webhook for the same title in (e.g. each episode of a season pack)."""
        self.files = sorted({*self.files, *other.files})
        pairs = {(e[0], e[1]) for e in (*self.episodes, *other.episodes)}
        self.episodes = [list(e) for e in sorted(pairs)]
        if other.action != "delete" or self.action == "delete":
            self.action = other.action
        self.folder = other.folder or self.folder

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


def _ids(media: dict[str, Any]) -> dict[str, str | None]:
    def text(value: Any) -> str | None:
        return str(value) if value not in (None, "", 0) else None

    return {
        "tmdb_id": text(media.get("tmdbId")),
        "tvdb_id": text(media.get("tvdbId")),
        "imdb_id": text(media.get("imdbId")),
    }


def parse_payload(payload: dict[str, Any]) -> ArrEvent | str | None:
    """An ``ArrEvent``, ``"test"`` for Radarr/Sonarr's test button, or ``None`` to ignore."""
    event = str(payload.get("eventType") or "")
    if event == "Test":
        return "test"
    actions = {
        "Download": "upgrade" if payload.get("isUpgrade") else "import",
        "Rename": "rename",
        "MovieDelete": "delete",
        "MovieFileDelete": "delete",
        "SeriesDelete": "delete",
        "EpisodeFileDelete": "delete",
    }
    action = actions.get(event)
    if action is None:
        return None
    if movie := payload.get("movie"):
        files = [payload.get("movieFile") or {}, *(payload.get("renamedMovieFiles") or [])]
        return ArrEvent(
            source="radarr",
            action=action,
            kind="movie",
            title=str(movie.get("title") or ""),
            year=movie.get("year") or None,
            folder=movie.get("folderPath") or movie.get("path"),
            files=sorted(
                {_basename(f.get("path") or f.get("relativePath")) for f in files if f} - {""}
            )
            if action != "delete"
            else [],
            **_ids(movie),
        )
    if series := payload.get("series"):
        files = [
            payload.get("episodeFile") or {},
            *(payload.get("episodeFiles") or []),
            *(payload.get("renamedEpisodeFiles") or []),
        ]
        return ArrEvent(
            source="sonarr",
            action=action,
            kind="show",
            title=str(series.get("title") or ""),
            year=series.get("year") or None,
            folder=series.get("path"),
            files=sorted(
                {_basename(f.get("path") or f.get("relativePath")) for f in files if f} - {""}
            )
            if action != "delete"
            else [],
            episodes=sorted(
                [int(e["seasonNumber"]), int(e["episodeNumber"])]
                for e in payload.get("episodes") or []
                if e.get("seasonNumber") is not None and e.get("episodeNumber") is not None
            ),
            **_ids(series),
        )
    return None


class ArrQueue:
    """Collects webhooks per title for a moment (a season pack sends one per episode) and
    schedules the follow-up looks. Timers live in memory; the weekly scan catches anything
    lost to a restart."""

    debounce = 5.0

    def __init__(self, runner: JobRunner):
        self.runner = runner
        self.pending: dict[str, ArrEvent] = {}
        self._timers: set[asyncio.TimerHandle] = set()
        self._tasks: set[asyncio.Task[Any]] = set()

    def receive(self, event: ArrEvent) -> bool:
        """Queue a webhook; False if it was folded into one already waiting."""
        if event.key in self.pending:
            self.pending[event.key].merge(event)
            return False
        self.pending[event.key] = event
        self.later(self.debounce, lambda: self._start(event.key))
        return True

    def _start(self, key: str) -> None:
        event = self.pending.pop(key, None)
        if event is not None:
            self.enqueue(event, 0)

    def enqueue(self, event: ArrEvent, attempt: int) -> None:
        task = asyncio.get_running_loop().create_task(
            self.runner.enqueue("arr_event", {"event": asdict(event), "attempt": attempt})
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def later(self, delay: float, callback: Any) -> None:
        loop = asyncio.get_running_loop()
        handle: asyncio.TimerHandle

        def fire() -> None:
            self._timers.discard(handle)
            callback()

        handle = loop.call_later(max(delay, 0), fire)
        self._timers.add(handle)

    def close(self) -> None:
        for handle in self._timers:
            handle.cancel()
        self._timers.clear()


# Set by the app at startup; the job uses it to schedule its next look.
queue: ArrQueue | None = None


async def _find_item(session: AsyncSession, event: ArrEvent) -> MediaItem | None:
    conditions = [
        getattr(MediaItem, name) == value
        for name in ("tmdb_id", "tvdb_id", "imdb_id")
        if (value := getattr(event, name))
    ]
    if not conditions:
        return None
    return await session.scalar(
        select(MediaItem)
        .where(MediaItem.kind == event.kind, or_(*conditions))
        .order_by(MediaItem.id)
        .limit(1)
    )


def _same_title(remote: RemoteItem, event: ArrEvent) -> bool:
    for name in ("tmdb_id", "tvdb_id", "imdb_id"):
        if getattr(event, name) and getattr(remote, name) == getattr(event, name):
            return True
    if event.kind == "movie" and remote.path and _basename(remote.path) in event.files:
        return True
    return bool(event.folder and remote.path and _basename(remote.path) == _basename(event.folder))


def _has_files(event: ArrEvent, remote: RemoteItem | None, children: list[RemoteItem]) -> bool:
    """Whether the server already lists the files the webhook was about."""
    if event.action == "delete":
        return True
    if remote is None:
        return False
    if event.kind == "movie":
        listed = {_basename(remote.path)}
    else:
        listed = {_basename(c.path) for c in children if c.kind == "episode" and c.path}
        have = {(c.season_number, c.episode_number) for c in children if c.kind == "episode"}
        if any((s, e) not in have for s, e in event.episodes):
            return False
    return all(f in listed for f in event.files)


async def _descendants(session: AsyncSession, item_id: int) -> set[int]:
    ids, frontier = {item_id}, [item_id]
    while frontier:
        frontier = list(
            await session.scalars(select(MediaItem.id).where(MediaItem.parent_id.in_(frontier)))
        )
        ids.update(frontier)
    return ids


Target = tuple[Server, ServerLibrary]
Found = tuple[Server, ServerLibrary, ServerItem | None, RemoteItem | None, list[RemoteItem]]


async def _scan(ctx: JobContext, event: ArrEvent, targets: list[Target]) -> int:
    """Ask each server to scan the title's folder inside each of its library folders."""
    folder = _basename(event.folder)
    asked = 0
    for server, sl in targets:
        try:
            async with client_for(server) as client:
                remote = next((r for r in await client.libraries() if r.id == sl.remote_id), None)
                if remote is None:
                    continue
                roots = [loc.rstrip("/\\") for loc in remote.locations] if folder else []
                paths = [f"{root}/{folder}" for root in roots]
                for path in paths:
                    await client.scan_folder(remote, path)
                asked += bool(paths)
        except MediaServerError as exc:
            await ctx.log(f"{server.name}: couldn't ask for a scan: {exc}")
    return asked


async def run_arr_event(ctx: JobContext) -> dict[str, Any]:
    event = ArrEvent(**ctx.params["event"])
    attempt = int(ctx.params.get("attempt", 0))
    sm = ctx.sessionmaker
    async with sm() as session:
        settings = await automation_settings(session)
        item = await _find_item(session, event)
        if item is not None:
            library_ids = [item.library_id]
        else:
            library_ids = list(
                await session.scalars(select(Library.id).where(Library.kind == event.kind))
            )
        rows = await session.execute(
            select(Server, ServerLibrary)
            .join(ServerLibrary, ServerLibrary.server_id == Server.id)
            .where(
                ServerLibrary.library_id.in_(library_ids),
                ServerLibrary.enabled.is_(True),
                Server.enabled.is_(True),
            )
            .order_by(Server.id, ServerLibrary.id)
        )
        targets: list[Target] = [(server, sl) for server, sl in rows.all()]
        existing: dict[int, ServerItem] = {}
        if item is not None:
            for si in await session.scalars(
                select(ServerItem).where(ServerItem.media_item_id == item.id)
            ):
                existing.setdefault(si.server_library_id, si)
        before = await _descendants(session, item.id) if item is not None else set()

    if not targets:
        await ctx.log(f"{event.label}: no library for {event.kind}s on any server")
        return {"title": event.label, "status": "no library"}

    if attempt == 0:
        await ctx.log(f"{event.source.capitalize()}: {event.action} of {event.label}")
        if settings["scan_servers"] and event.action != "delete":
            asked = await _scan(ctx, event, targets)
            await ctx.log(f"Asked {asked} server libraries to scan {_basename(event.folder)}")
        if queue is not None:
            queue.later(float(settings["wait_seconds"]), lambda: queue.enqueue(event, 1))
        await ctx.progress(1.0, f"Waiting for the servers to scan {event.label}")
        return {"title": event.label, "status": "scanning"}

    # Look for the title on each server.
    found: list[Found] = []
    # A server waits only if none of its libraries has the title yet: a new film is in
    # "Films" or "4K Films", never both.
    tried: dict[int, str] = {}
    ready: set[int] = set()
    for server, sl in targets:
        tried[server.id] = server.name
        si = existing.get(sl.id)
        remote: RemoteItem | None = None
        children: list[RemoteItem] = []
        try:
            async with client_for(server) as client:
                if si is not None:
                    try:
                        remote = await client.get_item(si.remote_id, event.kind)
                    except NotFoundError:
                        remote = None
                elif event.action != "delete":
                    library = RemoteLibrary(id=sl.remote_id, name=sl.name, kind=sl.kind)
                    recent = await client.recent_items(library, event.kind, RECENT_LIMIT)
                    remote = next((r for r in recent if _same_title(r, event)), None)
                if remote is not None and event.kind == "show":
                    children = await client.show_children(remote.id)
        except MediaServerError as exc:
            await ctx.log(f"{server.name}: {exc}")
            continue
        if si is None and remote is None and event.action == "delete":
            ready.add(server.id)
            continue
        if _has_files(event, remote, children):
            ready.add(server.id)
        found.append((server, sl, si, remote, children))
    waiting = [name for server_id, name in tried.items() if server_id not in ready]

    if waiting and attempt <= len(RETRY_DELAYS) and queue is not None:
        delay = RETRY_DELAYS[attempt - 1]
        await ctx.log(f"{event.label}: not on {', '.join(waiting)} yet; looking again in {delay}s")
        queue.later(delay, lambda: queue.enqueue(event, attempt + 1))
        return {"title": event.label, "status": "waiting", "servers": waiting}
    if waiting:
        await ctx.log(
            f"{event.label}: still not on {', '.join(waiting)}; the next import or weekly scan "
            "picks it up"
        )

    # Store what the servers report, match, and give new titles a base poster.
    pushed: list[int] = []
    async with sm() as session:
        for server, sl, si, remote, children in found:
            if remote is None:
                if si is not None:
                    await session.execute(
                        delete(ServerItem).where(
                            ServerItem.server_library_id == si.server_library_id,
                            ServerItem.show_remote_id == si.remote_id,
                        )
                    )
                    await session.execute(delete(ServerItem).where(ServerItem.id == si.id))
                    await ctx.log(f"{server.name}: {event.label} removed")
                continue
            run_id = (
                await session.scalar(
                    select(func.max(ServerItem.seen_run)).where(
                        ServerItem.server_library_id == sl.id
                    )
                )
            ) or 0
            if si is not None:
                row = await session.get(ServerItem, si.id)
                if row is not None:
                    apply_remote_item(row, remote)
            else:
                await upsert_items(session, server.id, sl.id, [remote], run_id)
            if event.kind == "show":
                await upsert_items(session, server.id, sl.id, children, run_id)
                await session.execute(
                    delete(ServerItem).where(
                        ServerItem.server_library_id == sl.id,
                        ServerItem.show_remote_id == remote.id,
                        ServerItem.remote_id.not_in([c.id for c in children]),
                    )
                )
                await fill_show_paths(session, sl.id)
            await ctx.log(f"{server.name}: {event.label} up to date")
        await session.flush()
        await match_all(session, library_ids)
        await refresh_facts(session, library_ids)
        item = await _find_item(session, event)
        new_bases = 0
        if item is not None:
            after = await _descendants(session, item.id)
            if settings["new_base"] == "tmdb":
                new_bases = await _tmdb_bases(ctx, session, after - before)
            pushed = sorted(after)
        from coverarr.services.review import refresh_issues

        await refresh_issues(session)
        await session.commit()

    # Fingerprint the new posters first (jobs run in order), then push just this title.
    if queue is not None:
        await queue.runner.enqueue("poster_scan", {})
        if item is not None and settings["auto_push"]:
            await queue.runner.enqueue(
                "push_posters",
                {"config_dir": _config_dir(), "media_item_ids": pushed, "collection_ids": []},
            )
    await ctx.progress(1.0, f"{event.label} refreshed")
    return {
        "title": event.label,
        "status": "done" if item is not None else "not found",
        "new_bases": new_bases,
        "waiting": waiting,
    }


def _config_dir() -> str:
    from coverarr.services.overlays import config_dir

    return str(config_dir())


async def _tmdb_bases(ctx: JobContext, session: AsyncSession, ids: set[int]) -> int:
    """TMDb's main poster as the base for titles and seasons that just appeared."""
    from pathlib import Path

    from coverarr.services.artwork import (
        TMDB_IMAGES,
        ArtworkError,
        _list_client,
        _tmdb_main_poster,
        save_artwork,
    )
    from coverarr.services.lists import ListError

    items = [
        m
        for m in await session.scalars(select(MediaItem).where(MediaItem.id.in_(ids)))
        if m.kind in ("movie", "show", "season") and not m.poster_source
    ]
    if not items or not await get_setting(session, "tmdb_api_key"):
        return 0
    shows = {m.id: m for m in items if m.kind == "show"}
    for m in items:
        if m.kind == "season" and m.parent_id not in shows and m.parent_id:
            parent = await session.get(MediaItem, m.parent_id)
            if parent is not None:
                shows[parent.id] = parent
    done = 0
    tv_ids: dict[int, str | None] = {}
    async with await _list_client(session) as client:
        for m in sorted(items, key=lambda m: m.kind == "season"):
            try:
                path = await _tmdb_main_poster(
                    client, m, shows.get(m.parent_id) if m.parent_id else None, tv_ids
                )
                if not path:
                    continue
                data = await client.download(f"{TMDB_IMAGES}/original{path}")
                await save_artwork(
                    session,
                    Path(_config_dir()),
                    source="tmdb",
                    data=data,
                    name=path.lstrip("/"),
                    media_item_id=m.id,
                    choose=True,
                )
                done += 1
            except (ListError, ArtworkError) as exc:
                await ctx.log(f"{m.title}: no TMDb poster ({exc})")
    if done:
        await ctx.log(f"Base poster from TMDb for {done} new titles and seasons")
    return done
