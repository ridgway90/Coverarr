"""Overlays: the overlay pack, which layers each library uses, and rendering posters.

An *overlay pack* is a folder of images under ``/config/assets/overlays/<pack>/``, e.g. the
jmxd set: ``gradient_top.png``, ``resolution/4kdvhdr.png``, ``edition/directors.png``,
``network-images/images/network/color/Channel 4.png``. Each library says which *layers* to draw
at each level (movie; show, season, episode):

* ``image``      a full-size image stretched over the poster (gradients)
* ``resolution`` ``<folder>/<key>.png`` for the title's resolution/HDR/DV (``4kdvhdr``)
* ``edition``    ``<folder>/<key>.png`` for its edition (``directors``)
* ``network``    ``<folder>/<network>.png`` for a show's network

A *render plan* is the base image plus the exact overlay files for one title; its key (a hash
of all of that, file sizes and times included) changes whenever anything that affects the
picture does, which is how Coverarr knows what needs rendering and pushing again.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.services.mediainfo import edition_key, resolution_key
from coverarr.services.settings_store import get_setting, set_setting

OVERLAYS_DIR = PurePosixPath("assets") / "overlays"
SETTING = "overlays"
RENDER_VERSION = 1  # bump when the drawing code changes in a way that alters output

POSTER = (1000, 1500)
LANDSCAPE = (1920, 1080)
CANVAS = {"movie": POSTER, "show": POSTER, "season": POSTER, "episode": LANDSCAPE}
LEVELS = {"movie": ["movie"], "show": ["show", "season", "episode"]}
LAYER_TYPES = ("image", "resolution", "edition", "network")
ALIGN_H = ("left", "center", "right")
ALIGN_V = ("top", "center", "bottom")
MAX_PACK_BYTES = 500 * 1024 * 1024
PACK_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".ttf", ".otf", ".yml", ".yaml", ".txt", ".md"}


class OverlayError(ValueError):
    pass


# Where /config is, set at startup (overlay packs and rendered posters live under it).
_config_dir: Path | None = None


def set_config_dir(path: Path) -> None:
    global _config_dir
    _config_dir = path


def config_dir() -> Path:
    if _config_dir is None:
        raise RuntimeError("overlays.set_config_dir() wasn't called at startup")
    return _config_dir


@dataclass
class OverlaySetup:
    """Everything needed to plan renders: the pack folder and, per library, the layers of
    each level that has any enabled."""

    root: Path | None
    libraries: dict[int, dict[str, list[dict]]]
    # Titles with the same layers and the same facts share a plan (most of a library does).
    _plans: dict[tuple, RenderPlan] = field(default_factory=dict)

    def plan(self, level: str, layers: list[dict], facts: dict[str, Any] | None) -> RenderPlan:
        f = facts or {}
        key = (
            level,
            id(layers),
            resolution_key(f),
            edition_key(f.get("edition")),
            f.get("network"),
        )
        plan = self._plans.get(key)
        if plan is None:
            plan = self._plans[key] = build_plan(self.root, level, layers, facts)
        return plan

    def layers_for(self, library_id: int | None, kind: str) -> list[dict] | None:
        if library_id is None:
            return None
        return self.libraries.get(library_id, {}).get(kind)


async def load_setup(session: AsyncSession) -> OverlaySetup:
    from sqlalchemy import select

    from coverarr.models import Library

    config = await load_config(session)
    root = pack_dir(config_dir(), config.get("pack"))
    if root is None or not root.is_dir():
        return OverlaySetup(None, {})
    libraries: dict[int, dict[str, list[dict]]] = {}
    for lib_id, kind in (await session.execute(select(Library.id, Library.kind))).all():
        lib = config["libraries"].get(str(lib_id))
        if not lib or not lib.get("enabled") or kind not in LEVELS:
            continue
        full = library_config(config, lib_id, kind)
        levels = {
            level: layers
            for level, layers in full["levels"].items()
            if any(layer.get("enabled", True) for layer in layers)
        }
        if levels:
            libraries[lib_id] = levels
    return OverlaySetup(root, libraries)


# --- configuration ---------------------------------------------------------------------------


def _badge(kind: str, folder: str, h: str, v: str, enabled: bool = True, **extra: Any) -> dict:
    return {
        "type": kind,
        "folder": folder,
        "enabled": enabled,
        "h_align": h,
        "v_align": v,
        "h_offset": 50,
        "v_offset": 50,
        "scale": 1.0,
        **extra,
    }


def _image(file: str, enabled: bool = True) -> dict:
    return {"type": "image", "file": file, "enabled": enabled}


def default_layers(level: str) -> list[dict]:
    """The jmxd layout from a typical Kometa config: gradients top and bottom, the resolution
    badge top-left, the network logo bottom-left on shows, editions under the badge (off)."""
    if level == "episode":
        return [
            _image("gradient_episode_top.png"),
            _image("gradient_episode_bottom.png"),
            _badge("resolution", "resolution", "left", "top"),
        ]
    layers = [
        _image("gradient_top.png"),
        _image("gradient_bottom.png"),
        _badge("resolution", "resolution", "left", "top"),
    ]
    if level == "movie":
        layers.append(
            _badge("edition", "edition", "center", "bottom", enabled=False, h_offset=0, v_offset=40)
        )
    if level == "show":
        layers.append(_badge("network", "network-images/images/network/color", "left", "bottom"))
    return layers


def default_library(kind: str) -> dict:
    return {"enabled": False, "levels": {lvl: default_layers(lvl) for lvl in LEVELS[kind]}}


def clean_layers(layers: Any) -> list[dict]:
    """Validate layers coming from the UI."""
    if not isinstance(layers, list) or len(layers) > 20:
        raise OverlayError("layers must be a list (at most 20)")
    out = []
    for raw in layers:
        if not isinstance(raw, dict) or raw.get("type") not in LAYER_TYPES:
            raise OverlayError(f"unknown layer type in {raw!r}")
        layer: dict[str, Any] = {"type": raw["type"], "enabled": bool(raw.get("enabled", True))}
        if raw["type"] == "image":
            layer["file"] = _safe_rel(raw.get("file"))
        else:
            layer["folder"] = _safe_rel(raw.get("folder"))
            layer["h_align"] = raw.get("h_align") if raw.get("h_align") in ALIGN_H else "left"
            layer["v_align"] = raw.get("v_align") if raw.get("v_align") in ALIGN_V else "top"
            for key in ("h_offset", "v_offset"):
                try:
                    layer[key] = max(-2000, min(2000, int(raw.get(key, 50))))
                except (TypeError, ValueError) as exc:
                    raise OverlayError(f"{key} must be a number") from exc
            try:
                layer["scale"] = max(0.1, min(5.0, float(raw.get("scale", 1.0))))
            except (TypeError, ValueError) as exc:
                raise OverlayError("scale must be a number") from exc
        out.append(layer)
    return out


def _safe_rel(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").strip("/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise OverlayError(f"not a path inside the overlay pack: {value!r}")
    return str(path)


async def load_config(session: AsyncSession) -> dict[str, Any]:
    stored = await get_setting(session, SETTING) or {}
    return {"pack": stored.get("pack"), "libraries": dict(stored.get("libraries") or {})}


async def save_config(session: AsyncSession, config: dict[str, Any]) -> None:
    await set_setting(session, SETTING, config)


def library_config(config: dict[str, Any], library_id: int, kind: str) -> dict[str, Any]:
    """This library's overlay settings, filled in with defaults."""
    lib = copy.deepcopy(config["libraries"].get(str(library_id)) or default_library(kind))
    for level in LEVELS[kind]:
        lib["levels"].setdefault(level, default_layers(level))
    return lib


# --- overlay packs ---------------------------------------------------------------------------


def pack_dir(config_dir: Path, pack: str | None) -> Path | None:
    return config_dir / OVERLAYS_DIR / pack if pack else None


def install_pack(config_dir: Path, zip_path: Path, name: str) -> dict[str, Any]:
    """Unpack an overlay pack zip into assets/overlays/<name>, replacing an older copy."""
    name = re.sub(r"[^A-Za-z0-9._ -]", "", name).strip(" .") or "pack"
    target = config_dir / OVERLAYS_DIR / name
    with zipfile.ZipFile(zip_path) as archive:
        members = [
            m
            for m in archive.infolist()
            if not m.is_dir()
            and "__MACOSX" not in m.filename
            and not PurePosixPath(m.filename).name.startswith(".")
            and PurePosixPath(m.filename).suffix.lower() in PACK_SUFFIXES
            # Entries that would land outside the pack are skipped, never written.
            and not PurePosixPath(m.filename.replace("\\", "/")).is_absolute()
            and ".." not in PurePosixPath(m.filename.replace("\\", "/")).parts
        ]
        if not members:
            raise OverlayError("That zip has no overlay images in it")
        if sum(m.file_size for m in members) > MAX_PACK_BYTES:
            raise OverlayError("That overlay pack is larger than 500 MB unpacked")
        # Strip folders every file shares (e.g. "jmxd/"), so the pack root holds the images.
        parts = [PurePosixPath(m.filename.replace("\\", "/")).parts for m in members]
        common = 0
        while all(len(p) > common + 1 for p in parts) and len({p[common] for p in parts}) == 1:
            common += 1
        staging = Path(tempfile.mkdtemp(dir=config_dir, prefix=".overlay-pack-"))
        try:
            for member, p in zip(members, parts, strict=True):
                rel = PurePosixPath(*p[common:])
                if rel.is_absolute() or ".." in rel.parts:
                    raise OverlayError(f"Unsafe path in zip: {member.filename}")
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    return describe_pack(config_dir, name)


def describe_pack(config_dir: Path, pack: str | None) -> dict[str, Any] | None:
    root = pack_dir(config_dir, pack)
    if root is None or not root.is_dir():
        return None
    images = [p for p in root.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    folders: dict[str, int] = {}
    for p in images:
        rel = str(p.parent.relative_to(root)).replace("\\", "/")
        folders[rel] = folders.get(rel, 0) + 1
    return {
        "name": pack,
        "images": len(images),
        "top_files": sorted(str(p.relative_to(root)) for p in images if p.parent == root),
        "folders": dict(sorted(folders.items())),
    }


# --- render plans ----------------------------------------------------------------------------


@dataclass
class PlacedLayer:
    path: str
    placement: dict[str, Any] | None  # None = stretch over the whole canvas


@dataclass
class RenderPlan:
    size: tuple[int, int]
    layers: list[PlacedLayer] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # wanted but not in the pack

    _digest: str | None = None

    def digest(self) -> str:
        """Hash of the overlay files (with their sizes and times) and where they go."""
        if self._digest is None:
            files = [[layer.path, _stat(layer.path), layer.placement] for layer in self.layers]
            blob = json.dumps({"size": self.size, "layers": files}, sort_keys=True)
            self._digest = hashlib.sha1(blob.encode()).hexdigest()
        return self._digest

    def key(self, base: str) -> str:
        blob = f"{RENDER_VERSION}|{base}|{self.digest()}"
        return hashlib.sha1(blob.encode()).hexdigest()[:20]


@lru_cache(maxsize=4096)
def _stat_cached(path: str, _bucket: int) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _stat(path: str) -> tuple[int, int] | None:
    import time

    return _stat_cached(path, int(time.monotonic() // 30))  # re-check every 30 s


@lru_cache(maxsize=64)
def _folder_index(folder: str, _bucket: int) -> dict[str, str]:
    """lowercased stem and squashed stem -> file, for case/spacing-insensitive lookups."""
    out: dict[str, str] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in sorted(names):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        full = os.path.join(folder, name)
        out.setdefault(stem.lower(), full)
        out.setdefault(_squash(stem), full)
    return out


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", text.lower().replace("&", "and"))


def _find(folder: Path, names: list[str]) -> str | None:
    import time

    index = _folder_index(str(folder), int(time.monotonic() // 30))
    for name in names:
        hit = index.get(name.lower()) or index.get(_squash(name))
        if hit:
            return hit
    return None


SD_RESOLUTIONS = ("576p", "480p")


def _resolution_names(facts: dict[str, Any]) -> list[str]:
    """Image names for a title's badge, best first: the exact one, then simpler ones (a DV+HDR
    film falls back to the DV, then HDR, then plain badge). 576p and 480p also try the shared
    ``sd`` badges."""
    key = resolution_key(facts)
    if not key:
        return []
    res = facts.get("resolution") or ""

    def chain(r: str) -> list[str]:
        names = [f"{r}{key[len(res) :]}" if res else key]
        if facts.get("dv") and facts.get("hdr"):
            names.append(f"{r}dvhdr")
        if facts.get("dv"):
            names.append(f"{r}dv")
        if facts.get("hdr") or facts.get("hdr10plus"):
            names.append(f"{r}hdr")
        if r:
            names.append(r)
        return names

    names = chain(res)
    if res in SD_RESOLUTIONS:
        names += chain("sd")
    return list(dict.fromkeys(n for n in names if n))


def build_plan(
    root: Path | None, level: str, layers: list[dict], facts: dict[str, Any] | None
) -> RenderPlan:
    """The overlay files for one title at ``level`` (no I/O beyond directory listings)."""
    plan = RenderPlan(CANVAS[level])
    facts = facts or {}
    if root is None:
        return plan
    for layer in layers:
        if not layer.get("enabled", True):
            continue
        if layer["type"] == "image":
            path = root / layer["file"]
            if path.is_file():
                plan.layers.append(PlacedLayer(str(path), None))
            else:
                plan.missing.append(layer["file"])
            continue
        folder = root / layer["folder"]
        if layer["type"] == "resolution":
            names = _resolution_names(facts)
        elif layer["type"] == "edition":
            key = edition_key(facts.get("edition"))
            names = [key] if key else []
        else:
            names = [facts["network"]] if facts.get("network") else []
        if not names:
            continue  # nothing to show for this title (e.g. no edition)
        path = _find(folder, names)
        if path is None:
            plan.missing.append(f"{layer['folder']}/{names[0]}.png")
            continue
        plan.layers.append(
            PlacedLayer(
                path,
                {k: layer[k] for k in ("h_align", "v_align", "h_offset", "v_offset", "scale")},
            )
        )
    return plan


# --- drawing (runs in worker processes) ------------------------------------------------------


@lru_cache(maxsize=256)
def _overlay_image(path: str, stat: tuple[int, int] | None) -> Image.Image:
    with Image.open(path) as img:
        return img.convert("RGBA")


def _position(canvas: tuple[int, int], size: tuple[int, int], p: dict[str, Any]) -> tuple[int, int]:
    """Top-left corner for an overlay of ``size``; offsets push in from the aligned edge
    (from the middle, for "center")."""
    (cw, ch), (w, h) = canvas, size
    dx, dy = p["h_offset"], p["v_offset"]
    x = {"left": dx, "center": (cw - w) // 2 + dx, "right": cw - w - dx}[p["h_align"]]
    y = {"top": dy, "center": (ch - h) // 2 + dy, "bottom": ch - h - dy}[p["v_align"]]
    return int(x), int(y)


def render_image(base: bytes | str, size: tuple[int, int], layers: list[tuple[str, Any]]) -> bytes:
    """Draw ``layers`` over ``base`` (bytes or a file path); returns JPEG bytes."""
    src = Image.open(io.BytesIO(base) if isinstance(base, bytes) else base)
    with src:
        canvas = ImageOps.fit(src.convert("RGB"), size, Image.Resampling.LANCZOS).convert("RGBA")
    for path, placement in layers:
        try:
            st = os.stat(path)
            stamp: tuple[int, int] | None = (st.st_size, st.st_mtime_ns)
        except OSError:
            stamp = None
        overlay = _overlay_image(path, stamp)
        if placement is None:
            if overlay.size != size:
                overlay = overlay.resize(size, Image.Resampling.LANCZOS)
            canvas.alpha_composite(overlay)
            continue
        scale = placement.get("scale", 1.0)
        if scale != 1.0:
            overlay = overlay.resize(
                (max(1, round(overlay.width * scale)), max(1, round(overlay.height * scale))),
                Image.Resampling.LANCZOS,
            )
        x, y = _position(size, overlay.size, placement)
        layer = Image.new("RGBA", size)
        layer.paste(overlay, (x, y), overlay)
        canvas.alpha_composite(layer)
    out = io.BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


def render_to_file(
    base: bytes | str, size: tuple[int, int], layers: list, dest: str
) -> tuple[str, str]:
    """Worker entry point: render and write ``dest`` atomically; returns its fingerprints."""
    from coverarr.services.posters import fingerprints

    data = render_image(base, size, layers)
    tmp = f"{dest}.tmp{os.getpid()}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    return fingerprints(data)


def plan_args(plan: RenderPlan) -> list[tuple[str, Any]]:
    return [(layer.path, layer.placement) for layer in plan.layers]


def rendered_path(config_dir: Path, owner: str, key: str) -> Path:
    return config_dir / "cache" / "rendered" / owner[:2] / f"{owner}-{key}.jpg"
