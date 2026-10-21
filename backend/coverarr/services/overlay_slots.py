"""Overlay images one at a time: every slot the layers can use (gradients, a badge for each
resolution and HDR type, editions, the networks your shows use), what fills each now, and
uploading a single image into a slot under the name and folder the renderer looks for.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

from coverarr.services import overlays
from coverarr.services.mediainfo import EDITIONS
from coverarr.services.overlays import (
    SD_RESOLUTIONS,
    OverlayError,
    _find,
    _resolution_names,
    _safe_rel,
    _squash,
    default_layers,
)

RESOLUTION_ROWS = [("4k", "4K"), ("1080p", "1080p"), ("720p", "720p"), ("sd", "SD")]
# Suffix -> (label, facts it stands for)
HDR_COLUMNS: list[tuple[str, str, dict[str, bool]]] = [
    ("", "SDR", {}),
    ("hdr", "HDR", {"hdr": True}),
    ("plus", "HDR10+", {"hdr10plus": True}),
    ("dv", "Dolby Vision", {"dv": True}),
    ("dvhdr", "DV + HDR", {"dv": True, "hdr": True}),
    ("dvhdrplus", "DV + HDR10+", {"dv": True, "hdr10plus": True}),
]
EDITION_LABELS = {
    "directors": "Director's Cut",
    "richarddonner": "Richard Donner Cut",
    "blackchrome": "Black & Chrome",
    "final": "Final Cut",
    "special": "Special Edition",
    "collector": "Collector's Edition",
}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_FOLDERS = {
    "resolution": "resolution",
    "edition": "edition",
    "network": "network-images/images/network/color",
}


@dataclass
class Slot:
    group: str  # gradient | resolution | edition | network
    path: str  # where an upload goes, relative to the pack
    label: str
    names: list[str]  # what the renderer looks up, best first (stems, in ``folder``)
    folder: str
    row: str | None = None
    column: str | None = None
    # Files the renderer would pick over ``path`` for the same titles; removed on upload.
    shadows: tuple[str, ...] = ()


def folders(config: dict[str, Any]) -> dict[str, str]:
    """The folder each badge type reads from, as the libraries' layers have it."""
    out = dict(DEFAULT_FOLDERS)
    seen: set[str] = set()
    for lib in config["libraries"].values():
        for layers in (lib.get("levels") or {}).values():
            for layer in layers:
                kind = layer.get("type")
                if kind in out and kind not in seen and layer.get("folder"):
                    out[kind] = layer["folder"]
                    seen.add(kind)
    return out


def gradient_files(config: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for level in ("movie", "show", "season", "episode"):
        files += [layer["file"] for layer in default_layers(level) if layer["type"] == "image"]
    for lib in config["libraries"].values():
        for layers in (lib.get("levels") or {}).values():
            files += [layer["file"] for layer in layers if layer.get("type") == "image"]
    return list(dict.fromkeys(files))


def catalogue(config: dict[str, Any], networks: list[str]) -> list[Slot]:
    f = folders(config)
    slots: list[Slot] = []
    for file in gradient_files(config):
        label = PurePosixPath(file).stem.replace("_", " ").replace("-", " ").capitalize()
        slots.append(Slot("gradient", file, label, [], ""))
    for row, row_label in RESOLUTION_ROWS:
        for suffix, col_label, facts in HDR_COLUMNS:
            stem = f"{row}{suffix}"
            sample = {**facts, "resolution": "480p" if row == "sd" else row}
            shadows = tuple(f"{r}{suffix}" for r in SD_RESOLUTIONS) if row == "sd" else ()
            slots.append(
                Slot(
                    "resolution",
                    f"{f['resolution']}/{stem}.png",
                    f"{row_label} {col_label}" if suffix else row_label,
                    _resolution_names(sample),
                    f["resolution"],
                    row,
                    suffix,
                    shadows,
                )
            )
    for key, _ in EDITIONS:
        label = EDITION_LABELS.get(key, key.capitalize())
        slots.append(Slot("edition", f"{f['edition']}/{key}.png", label, [key], f["edition"]))
    for name in sorted(set(networks), key=str.lower):
        safe = re.sub(r"[\\/:*?\"<>|]", "", name).strip() or "network"
        slots.append(Slot("network", f"{f['network']}/{safe}.png", name, [name], f["network"]))
    return slots


def describe(root: Path | None, slots: list[Slot]) -> list[dict[str, Any]]:
    """Each slot with the file the renderer would use now (``current``) and whether that's the
    slot's own image or a fallback (e.g. the plain 4K badge for 4K DV)."""
    out = []
    for slot in slots:
        current = None
        if root is not None:
            if slot.group == "gradient":
                current = slot.path if (root / slot.path).is_file() else None
            else:
                hit = _find(root / slot.folder, slot.names)
                if hit:
                    current = str(Path(hit).relative_to(root)).replace("\\", "/")
        exact = current is not None and _squash(PurePosixPath(current).stem) == _squash(
            PurePosixPath(slot.path).stem
        )
        version = None
        if current and root is not None:
            try:
                version = int((root / current).stat().st_mtime)
            except OSError:
                version = None
        out.append(
            {
                "group": slot.group,
                "path": slot.path,
                "label": slot.label,
                "row": slot.row,
                "column": slot.column,
                "current": current,
                "exact": exact,
                "version": version,
            }
        )
    return out


def _clear_caches() -> None:
    overlays._folder_index.cache_clear()
    overlays._stat_cached.cache_clear()


def save(root: Path, slot: Slot, data: bytes) -> str:
    """Store ``data`` as the slot's PNG, replacing whatever else the renderer would pick for it
    (the same name in another format or case, and for SD the 480p/576p badges)."""
    if len(data) > MAX_IMAGE_BYTES:
        raise OverlayError("That image is larger than 20 MB")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            converted = img.convert("RGBA")
    except Exception as exc:
        raise OverlayError("That file isn't an image Coverarr can read") from exc
    dest = root / _safe_rel(slot.path)
    folder = dest.parent
    folder.mkdir(parents=True, exist_ok=True)
    replace = {_squash(dest.stem), *(_squash(s) for s in slot.shadows)}
    for name in os.listdir(folder):
        stem, ext = os.path.splitext(name)
        if ext.lower() in IMAGE_SUFFIXES and _squash(stem) in replace and name != dest.name:
            (folder / name).unlink(missing_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    converted.save(tmp, "PNG", optimize=True)
    os.replace(tmp, dest)
    _clear_caches()
    return str(dest.relative_to(root)).replace("\\", "/")


def remove(root: Path, rel: str) -> None:
    path = root / _safe_rel(rel)
    if path.suffix.lower() in IMAGE_SUFFIXES:
        path.unlink(missing_ok=True)
    _clear_caches()


# --- guessing which slot a file is for --------------------------------------------------------

_RES_TOKENS = [
    ("4k", r"2160p?|4k|uhd"),
    ("1080p", r"1080[pi]?|fhd|fullhd"),
    ("720p", r"720p?"),
    ("sd", r"576[pi]?|480[pi]?|sd|dvd"),
]


def guess(filename: str, slots: list[Slot]) -> str | None:
    """The slot a file is probably for, from its name: "4K Dolby Vision.png" -> 4K DV,
    "gradient_bottom.png" -> that gradient, "Directors Cut.png" -> that edition."""
    stem = PurePosixPath(filename.replace("\\", "/")).stem
    flat = _squash(stem)
    by_squash = {_squash(PurePosixPath(s.path).stem): s for s in slots}
    if flat in by_squash:
        return by_squash[flat].path
    words = re.sub(r"[^a-z0-9+]+", " ", stem.lower())
    if "gradient" in flat:
        for s in slots:
            if s.group == "gradient" and _squash(PurePosixPath(s.path).stem).replace(
                "gradient", ""
            ) in flat.replace("gradient", ""):
                return s.path
        return None
    for s in slots:
        if s.group == "network" and _squash(s.label) == flat:
            return s.path
    for key, pattern in EDITIONS:
        if pattern.search(stem):
            return next((s.path for s in slots if s.group == "edition" and s.names == [key]), None)
    row = next(
        (r for r, rx in _RES_TOKENS if re.search(rf"(?<![a-z0-9])({rx})(?![a-z])", words)), None
    )
    dv = bool(re.search(r"dolby\s*vision|(?<![a-z])(dv|dovi)(?![a-z])", words))
    plus = bool(re.search(r"hdr\s*10\s*\+|hdr10plus|hdr\s*10\s*plus|(?<![a-z])plus", words))
    hdr = bool(re.search(r"hdr", words)) and not plus
    if dv and plus:
        suffix = "dvhdrplus"
    elif dv and hdr:
        suffix = "dvhdr"
    elif dv:
        suffix = "dv"
    elif plus:
        suffix = "plus"
    elif hdr:
        suffix = "hdr"
    else:
        suffix = ""
    if row is None:
        return None
    return next(
        (s.path for s in slots if s.group == "resolution" and s.row == row and s.column == suffix),
        None,
    )
