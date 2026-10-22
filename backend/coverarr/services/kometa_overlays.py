"""Kometa's default overlay files (``overlay_files: - default: resolution``) as Coverarr overlay
layers.

``resolution`` turns on the resolution badge (and the edition badge on films, unless
``use_edition: false``), ``network`` the network logo on shows, ``episode_info`` the episode
number on title cards. ``builder_level: season`` / ``episode`` puts them on that level instead
of the film or show. Kometa's other defaults (ratings, ribbons, streaming, ...) aren't
supported and are reported. A library that already has overlay settings in Coverarr is left as
it is, and overlays stay off until you turn them on.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import Library
from coverarr.services.overlays import LEVELS, default_library, load_config, save_config

# Kometa default -> Coverarr layer types it turns on.
LAYERS = {
    "resolution": ("resolution", "edition"),
    "network": ("network",),
    "episode_info": ("text",),
}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() not in ("false", "no", "0", "off")


async def apply_overlay_defaults(
    session: AsyncSession, library: Library, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """What was turned on, what isn't supported, and whether the library was skipped."""
    config = await load_config(session)
    unsupported = sorted(
        {str(e.get("default")) for e in entries if str(e.get("default")) not in LAYERS}
    )
    wanted = [e for e in entries if str(e.get("default")) in LAYERS]
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
        for layer in layers:
            if layer["type"] != "image":
                layer["enabled"] = layer["type"] in on[level]
                if layer["enabled"]:
                    applied.append(f"{level} {layer['type']}")
    config["libraries"][str(library.id)] = lib
    await save_config(session, config)
    return {"applied": applied, "unsupported": unsupported, "kept_existing": False}
