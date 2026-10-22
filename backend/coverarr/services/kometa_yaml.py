"""Parsing Kometa collection files and config.yml into Coverarr collection definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml

from coverarr.services.lists import BUILDERS, ListError, normalize_builder

OPTIONS = ("sync_mode", "collection_order", "sort_title", "label", "item_label", "summary")


@dataclass
class ParsedCollection:
    name: str
    source_file: str
    builders: list[dict[str, str]] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedConfig:
    # Library name -> collection file basenames, from ``libraries.<name>.collection_files``.
    files_by_library: dict[str, list[str]] = field(default_factory=dict)
    tmdb_key: str | None = None
    trakt_client_id: str | None = None
    trakt_client_secret: str | None = None
    trakt_access_token: str | None = None
    trakt_refresh_token: str | None = None


class KometaYamlError(Exception):
    pass


def _load(text: str, filename: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KometaYamlError(f"{filename}: not valid YAML ({exc})") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise KometaYamlError(f"{filename}: expected a mapping at the top level")
    _check_size(data, filename)
    return data


# Values a file may hold once its anchors are expanded. Real configs are a few thousand; a
# few hundred bytes of nested aliases can expand to billions ("billion laughs").
MAX_EXPANDED = 200_000


def _check_size(data: Any, filename: str) -> None:
    budget = MAX_EXPANDED
    stack = [data]
    while stack:
        node = stack.pop()
        budget -= 1
        if budget < 0:
            raise KometaYamlError(f"{filename}: too large once its anchors (*aliases) are expanded")
        if isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _values(raw: Any) -> list[str]:
    """Kometa accepts a single value, a list, or a comma-separated string."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        # Comma-separated ids, but never split URLs.
        parts = [text] if "://" in text else [p.strip() for p in text.split(",")]
        out.extend(p for p in parts if p)
    return out


def _secret(raw: Any) -> str | None:
    """Ignore blanks and redacted placeholders such as ``[key]``."""
    text = str(raw).strip() if raw is not None else ""
    return None if not text or re.fullmatch(r"\[.*\]|<.*>|x+|\*+", text, re.I) else text


def parse_collection_file(text: str, filename: str) -> list[ParsedCollection]:
    data = _load(text, filename)
    collections = data.get("collections") or {}
    if not isinstance(collections, dict):
        raise KometaYamlError(f"{filename}: 'collections' should be a mapping of names")
    base = PurePosixPath(filename.replace("\\", "/")).name
    parsed: list[ParsedCollection] = []
    for name, body in collections.items():
        coll = ParsedCollection(name=str(name), source_file=base)
        if not isinstance(body, dict):
            coll.warnings.append("Collection body isn't a mapping; skipped")
            parsed.append(coll)
            continue
        for key, raw in body.items():
            if key in BUILDERS:
                for value in _values(raw):
                    try:
                        coll.builders.append(normalize_builder(key, value))
                    except ListError as exc:
                        coll.warnings.append(str(exc))
            elif key in OPTIONS:
                coll.options[key] = (
                    [str(v) for v in raw]
                    if key in ("label", "item_label") and isinstance(raw, list)
                    else raw
                )
            else:
                coll.unsupported.append(str(key))
        if not coll.builders:
            coll.warnings.append(
                "No supported list source (TMDb/Trakt lists, collections or charts)"
            )
        parsed.append(coll)
    return parsed


def parse_config(text: str, filename: str = "config.yml") -> ParsedConfig:
    data = _load(text, filename)
    config = ParsedConfig()
    for library, body in (data.get("libraries") or {}).items():
        files = []
        for entry in (body or {}).get("collection_files") or []:
            if isinstance(entry, dict) and entry.get("file"):
                files.append(PurePosixPath(str(entry["file"]).replace("\\", "/")).name)
        config.files_by_library[str(library)] = files
    tmdb = data.get("tmdb") or {}
    trakt = data.get("trakt") or {}
    auth = trakt.get("authorization") or {}
    config.tmdb_key = _secret(tmdb.get("apikey"))
    config.trakt_client_id = _secret(trakt.get("client_id"))
    config.trakt_client_secret = _secret(trakt.get("client_secret"))
    config.trakt_access_token = _secret(auth.get("access_token"))
    config.trakt_refresh_token = _secret(auth.get("refresh_token"))
    return config


def is_config_file(text: str) -> bool:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(data, dict) and "libraries" in data and "collections" not in data
