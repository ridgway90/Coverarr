"""Kometa's default overlay files (``overlay_files: - default: resolution``) as Coverarr overlay
layers.

``resolution`` turns on the resolution badge (and the edition badge on films, unless
``use_edition: false``), ``network`` the network logo on shows, ``episode_info`` the episode
number on title cards, ``streaming`` / ``ribbon`` / ``status`` / ``audio_codec`` add those
badges, ``ratings`` adds IMDb and TMDb rating lines and ``runtimes`` the running time.
``builder_level: season`` / ``episode`` puts them on that level instead of the film or show.
Kometa's other defaults (languages, versions, ...) aren't supported and are reported. A
library that already has overlay settings in Coverarr is left as it is, and overlays stay off
until you turn them on.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import Library
from coverarr.services.overlays import (
    LEVELS,
    _badge,
    _text,
    default_library,
    load_config,
    save_config,
)

# Kometa default -> Coverarr layer types it turns on.
LAYERS = {
    "resolution": ("resolution", "edition"),
    "network": ("network",),
    "episode_info": ("text",),
    "streaming": ("streaming",),
    "ribbon": ("ribbon",),
    "status": ("status",),
    "audio_codec": ("audio",),
    "ratings": ("rating",),
    "runtimes": ("runtime",),
}
# Defaults only shows have.
SHOW_ONLY = ("network", "status", "episode_info")


def _new_layers(kind: str) -> list[dict]:
    """Layers a default adds when the level doesn't have one of that type."""
    if kind == "streaming":
        return [_badge("streaming", "streaming", "right", "bottom")]
    if kind == "ribbon":
        ribbons = ["imdb_top_250", "oscars_best_picture"]
        return [
            _badge("ribbon", "ribbon", "right", "bottom", h_offset=0, v_offset=0, ribbons=ribbons)
        ]
    if kind == "status":
        return [_badge("status", "status", "center", "top", h_offset=0)]
    if kind == "audio":
        return [_badge("audio", "audio_codec", "right", "top")]
    if kind == "rating":
        imdb = _text("IMDb {imdb_rating}", "right", "top")
        tmdb = _text("TMDb {tmdb_rating}", "right", "top")
        imdb.update(size=56, color="#F5C518", h_offset=50, v_offset=50)
        tmdb.update(size=56, color="#01D277", h_offset=50, v_offset=120)
        return [imdb, tmdb]
    if kind == "runtime":
        layer = _text("{runtime}", "left", "bottom")
        layer.update(size=50, h_offset=50, v_offset=50)
        return [layer]
    return []


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() not in ("false", "no", "0", "off")


def _is_rating(layer: dict) -> bool:
    return layer["type"] == "text" and "_rating}" in (layer.get("template") or "")


def _is_runtime(layer: dict) -> bool:
    return layer["type"] == "text" and "{runtime}" in (layer.get("template") or "")


async def apply_overlay_defaults(
    session: AsyncSession, library: Library, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """What was turned on, what isn't supported, and whether the library was skipped."""
    config = await load_config(session)
    unsupported = sorted(
        {str(e.get("default")) for e in entries if str(e.get("default")) not in LAYERS}
    )
    wanted = [e for e in entries if str(e.get("default")) in LAYERS]
    if library.kind != "show":
        unsupported += sorted({str(e["default"]) for e in wanted if e["default"] in SHOW_ONLY})
        wanted = [e for e in wanted if e["default"] not in SHOW_ONLY]
    if str(library.id) in config["libraries"]:
        return {"applied": [], "unsupported": unsupported, "kept_existing": bool(wanted)}
    if not wanted:
        return {"applied": [], "unsupported": unsupported, "kept_existing": False}
    lib = default_library(library.kind)
    on: dict[str, set[str]] = {level: set() for level in LEVELS[library.kind]}
    for entry in wanted:
        name = str(entry["default"])
        variables = entry.get("template_variables") or {}
        level = str(variables.get("builder_level") or "").lower()
        if name == "episode_info":
            level = "episode"
        if level not in on:
            level = library.kind
        if level not in on:
            continue
        types = set(LAYERS[name])
        if "edition" in types and not _truthy(variables.get("use_edition", True)):
            types.discard("edition")
        on[level] |= types
    applied = []
    for level, layers in lib["levels"].items():
        for kind in sorted(on[level]):
            present = any(
                layer["type"] == kind
                or (kind == "rating" and _is_rating(layer))
                or (kind == "runtime" and _is_runtime(layer))
                for layer in layers
            )
            if not present:
                layers.extend(_new_layers(kind))
        for layer in layers:
            if layer["type"] == "image":
                continue
            kind = (
                "rating"
                if _is_rating(layer)
                else "runtime"
                if _is_runtime(layer)
                else layer["type"]
            )
            layer["enabled"] = kind in on[level]
            if layer["enabled"]:
                applied.append(f"{level} {kind}")
    config["libraries"][str(library.id)] = lib
    await save_config(session, config)
    return {
        "applied": list(dict.fromkeys(applied)),
        "unsupported": unsupported,
        "kept_existing": False,
    }
