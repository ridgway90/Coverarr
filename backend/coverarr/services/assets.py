"""Where Coverarr keeps the artwork it holds itself.

    <config>/assets/
      movies/<Title (Year) {tmdb-N}>/poster-<source>.<ext>
      tv/<Show (Year) {tvdb-N}>/poster-<source>.<ext>
                               season01-<source>.<ext>
                               s01e02-<source>.<ext>
      collections/<Name>/poster-<source>.<ext>
      overlays/                       overlay images (gradients, badges, logos)

Folder names follow Radarr/Sonarr conventions so the tree is easy to browse. The database
records each file's path, so renames on the servers never orphan a file.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coverarr.models import Artwork, Collection, MediaItem
from coverarr.services.settings_store import get_setting, set_setting

log = logging.getLogger(__name__)

ASSETS = PurePosixPath("assets")
LAYOUT_VERSION = 2


def safe_filename(text: str, limit: int = 120) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].rstrip(" .") or "untitled"


def _title_folder(title: str, year: int | None, key: str | None, value: str | None) -> str:
    name = f"{title} ({year})" if year else title
    if value:
        name += f" {{{key}-{value}}}"
    return safe_filename(name)


async def describe_owners(
    session: AsyncSession, item_ids: list[int], collection_ids: list[int]
) -> dict[str, tuple[PurePosixPath, str]]:
    """``m<id>`` / ``c<id>`` -> (folder under assets, file stem)."""
    out: dict[str, tuple[PurePosixPath, str]] = {}
    if item_ids:
        rows = {
            r.id: r
            for r in (
                await session.execute(select(MediaItem).where(MediaItem.id.in_(item_ids)))
            ).scalars()
        }
        # Seasons hang off shows and episodes off seasons: load up to two levels of parents.
        parents: dict[int, MediaItem] = {}
        wanted = {r.parent_id for r in rows.values() if r.parent_id}
        for _ in range(2):
            wanted -= set(parents) | set(rows)
            if not wanted:
                break
            for p in (
                await session.execute(select(MediaItem).where(MediaItem.id.in_(wanted)))
            ).scalars():
                parents[p.id] = p
            wanted = {p.parent_id for p in parents.values() if p.parent_id}
        everything = {**parents, **rows}

        def show_folder(show: MediaItem | None) -> PurePosixPath:
            if show is None:
                return ASSETS / "tv" / "Unknown show"
            return ASSETS / "tv" / _title_folder(show.title, show.year, "tvdb", show.tvdb_id)

        for r in rows.values():
            if r.kind == "movie":
                key, value = ("tmdb", r.tmdb_id) if r.tmdb_id else ("imdb", r.imdb_id)
                folder = ASSETS / "movies" / _title_folder(r.title, r.year, key, value)
                out[f"m{r.id}"] = (folder, "poster")
            elif r.kind == "show":
                out[f"m{r.id}"] = (show_folder(r), "poster")
            elif r.kind == "season":
                show = everything.get(r.parent_id) if r.parent_id else None
                out[f"m{r.id}"] = (show_folder(show), f"season{r.season_number or 0:02d}")
            else:
                season = everything.get(r.parent_id) if r.parent_id else None
                show = everything.get(season.parent_id) if season and season.parent_id else None
                number = (season.season_number if season else 0) or 0
                stem = f"s{number:02d}e{r.episode_number or 0:02d}"
                out[f"m{r.id}"] = (show_folder(show), stem)
    if collection_ids:
        for cid, name in (
            await session.execute(
                select(Collection.id, Collection.name).where(Collection.id.in_(collection_ids))
            )
        ).all():
            out[f"c{cid}"] = (ASSETS / "collections" / safe_filename(name), "poster")
    return out


def owner_key(art: Artwork) -> str:
    return f"m{art.media_item_id}" if art.media_item_id else f"c{art.collection_id}"


def artwork_path(
    owner: str,
    folder: PurePosixPath,
    stem: str,
    source: str,
    suffix: str,
    taken: dict[str, str],
) -> PurePosixPath:
    """The asset path for ``owner``; disambiguated if another title already uses it
    (e.g. two collections with the same name in different libraries)."""
    path = folder / f"{stem}-{source}.{suffix}"
    if taken.get(str(path), owner) != owner:
        path = folder.with_name(f"{folder.name} [{owner}]") / path.name
    taken[str(path)] = owner
    return path


def write_asset(config_dir: Path, rel: PurePosixPath, data: bytes, old_rel: str | None) -> None:
    dest = config_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    if old_rel and old_rel != str(rel):
        remove_asset(config_dir, old_rel)


def remove_asset(config_dir: Path, rel: str) -> None:
    path = config_dir / rel
    path.unlink(missing_ok=True)
    _prune(config_dir, path.parent)


def _prune(config_dir: Path, folder: Path) -> None:
    """Remove now-empty folders up to (not including) the assets root."""
    root = config_dir / ASSETS
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


async def taken_paths(session: AsyncSession) -> dict[str, str]:
    """Every stored file -> its owner (just the columns: this runs on each upload)."""
    rows = await session.execute(
        select(Artwork.file_path, Artwork.media_item_id, Artwork.collection_id)
    )
    return {row.file_path: owner_key(row) for row in rows}


async def relocate_artwork(
    sm: async_sessionmaker[AsyncSession], config_dir: Path, *, force: bool = False
) -> int:
    """Move artwork from older layouts (e.g. ``assets/kometa/m12 - Title.jpg``) into the
    current one. Runs once at startup; ``force`` (housekeeping) checks every file again, e.g.
    after titles were renamed. Returns how many files moved."""
    async with sm() as session:
        if not force and (await get_setting(session, "artwork_layout", 1)) >= LAYOUT_VERSION:
            return 0
        arts = list(await session.scalars(select(Artwork)))
        owners = await describe_owners(
            session,
            [a.media_item_id for a in arts if a.media_item_id],
            [a.collection_id for a in arts if a.collection_id],
        )
        # Start from where every file is now, so a move never lands on a file another
        # title still uses (it gets a "[c12]" folder instead, and moves on a later tidy).
        taken = {art.file_path: owner_key(art) for art in arts}
        moved = 0
        for art in arts:
            key = owner_key(art)
            if key not in owners:
                continue
            folder, stem = owners[key]
            suffix = PurePosixPath(art.file_path).suffix.lstrip(".") or "jpg"
            if taken.get(art.file_path) == key:
                del taken[art.file_path]
            new = artwork_path(key, folder, stem, art.source, suffix, taken)
            if str(new) == art.file_path:
                continue
            src, dest = config_dir / art.file_path, config_dir / new
            if src.exists():
                await asyncio.to_thread(_move, src, dest)
                await asyncio.to_thread(_prune, config_dir, src.parent)
                moved += 1
            art.file_path = str(new)
        await set_setting(session, "artwork_layout", LAYOUT_VERSION)
        await session.commit()
    if moved:
        log.info("Moved %d artwork files into the assets/movies|tv|collections layout", moved)
    return moved


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dest)
