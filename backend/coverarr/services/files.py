"""Media file paths: mount-independent keys and the hints file names carry.

Servers mount the same media at different places (``/data/movies`` on Plex,
``/media/movies`` on Jellyfin), so files are compared by their trailing path components.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEASON_DIR = re.compile(r"^(season\s*\d+|specials|s\d{1,3})$", re.IGNORECASE)
_ID_PATTERNS = {
    # {tmdb-123} (Radarr/Sonarr), [tmdbid-123] / [tmdbid=123] (Jellyfin), {imdb-tt123}
    "tmdb_id": re.compile(r"[\[{]tmdb(?:id)?[-=](\d+)[\]}]", re.IGNORECASE),
    "tvdb_id": re.compile(r"[\[{]tvdb(?:id)?[-=](\d+)[\]}]", re.IGNORECASE),
    "imdb_id": re.compile(r"[\[{]imdb(?:id)?[-=](tt\d+)[\]}]", re.IGNORECASE),
}
_TITLE_YEAR = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)")


def path_parts(path: str | None) -> list[str]:
    return [p for p in re.split(r"[\\/]+", path or "") if p]


def file_key(path: str | None, kind: str) -> str | None:
    """Movies: ``folder/file``; shows: the show folder. Lowercased."""
    parts = path_parts(path)
    if not parts:
        return None
    tail = parts[-2:] if kind == "movie" else parts[-1:]
    return "/".join(tail).lower()


def show_folder(episode_path: str | None) -> str | None:
    """The show folder an episode file lives in (skipping a season folder)."""
    if not episode_path:
        return None
    sep = "\\" if "\\" in episode_path and "/" not in episode_path else "/"
    folder = episode_path.rsplit(sep, 1)[0] if sep in episode_path else ""
    if _SEASON_DIR.match(folder.rsplit(sep, 1)[-1]):
        folder = folder.rsplit(sep, 1)[0] if sep in folder else ""
    return folder or None


@dataclass
class FileHints:
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None
    title: str | None = None
    year: int | None = None

    def ids(self) -> dict[str, str]:
        return {k: v for k in ("tmdb_id", "tvdb_id", "imdb_id") if (v := getattr(self, k))}


def file_hints(path: str | None, kind: str) -> FileHints:
    """IDs and title/year that the folder and file names claim."""
    parts = path_parts(path)
    relevant = parts[-2:] if kind == "movie" else parts[-1:]
    hints = FileHints()
    text = " ".join(relevant)
    for key, pattern in _ID_PATTERNS.items():
        if m := pattern.search(text):
            setattr(hints, key, m.group(1))
    # The folder name is usually the cleanest "Title (Year)"; fall back to the file name.
    for name in relevant:
        cleaned = re.sub(r"[\[{].*?[\]}]", " ", re.sub(r"\.\w{2,4}$", "", name))
        if m := _TITLE_YEAR.match(cleaned.strip()):
            hints.title = m.group("title").strip()
            hints.year = int(m.group("year"))
            break
    return hints
