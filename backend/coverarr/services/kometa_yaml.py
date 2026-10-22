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
    # Set for collections made from Kometa defaults, which belong to one library.
    library_id: int | None = None


@dataclass
class ParsedConfig:
    # Library name -> collection file basenames, from ``libraries.<name>.collection_files``.
    files_by_library: dict[str, list[str]] = field(default_factory=dict)
    # Library name -> Kometa defaults (``- default: imdb``), overlay defaults and
    # metadata file basenames.
    defaults_by_library: dict[str, list[str]] = field(default_factory=dict)
    overlays_by_library: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    metadata_by_library: dict[str, list[str]] = field(default_factory=dict)
    asset_directories: list[str] = field(default_factory=list)
    tmdb_key: str | None = None
    trakt_client_id: str | None = None
    trakt_client_secret: str | None = None
    trakt_access_token: str | None = None
    mdblist_key: str | None = None
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


_VAR = re.compile(r"<<(\w+)>>")
# Template keys that configure the template rather than the collection.
TEMPLATE_META = ("default", "optional", "conditionals", "move_prefix", "move_collection_prefix")


def _fill(value: Any, variables: dict[str, Any]) -> Any:
    """``value`` with every ``<<name>>`` filled in; raises KeyError(name) for a missing one.
    A string that is just one placeholder takes the variable's own value (a list stays a
    list)."""
    if isinstance(value, str):
        whole = _VAR.fullmatch(value.strip())
        if whole:
            return variables[whole.group(1)]
        return _VAR.sub(lambda m: str(variables[m.group(1)]), value)
    if isinstance(value, list):
        return [_fill(v, variables) for v in value]
    if isinstance(value, dict):
        return {_fill(k, variables): _fill(v, variables) for k, v in value.items()}
    return value


def expand_templates(
    name: str, body: dict[str, Any], templates: dict[str, Any], warnings: list[str]
) -> dict[str, Any]:
    """The collection's body with its ``template:`` (one or a list) filled in. The
    collection's own keys win, then earlier templates over later ones."""
    uses = body.get("template")
    if uses is None:
        return body
    out = {k: v for k, v in body.items() if k != "template"}
    for use in uses if isinstance(uses, list) else [uses]:
        if isinstance(use, str):
            use = {"name": use}
        if not isinstance(use, dict) or not use.get("name"):
            warnings.append("A template use without a name; skipped")
            continue
        template = templates.get(str(use["name"]))
        if not isinstance(template, dict):
            warnings.append(f"Template {use['name']!r} isn't in this file")
            continue
        if template.get("conditionals"):
            warnings.append(f"Template {use['name']!r}: conditionals aren't supported")
        variables: dict[str, Any] = {
            **{str(k): v for k, v in (template.get("default") or {}).items()},
            "collection_name": name,
            "mapping_name": name,
            **{str(k): v for k, v in use.items() if k != "name"},
        }
        # Defaults may use other variables, e.g. "<<collection_name>> Films".
        for key, value in list(variables.items()):
            try:
                variables[key] = _fill(value, variables)
            except KeyError:
                pass
        optional = {str(v) for v in template.get("optional") or []}
        for key, value in template.items():
            if key in TEMPLATE_META or key in out:
                continue
            try:
                out[key] = _fill(value, variables)
            except KeyError as missing:
                if str(missing.args[0]) not in optional:
                    warnings.append(
                        f"Template {use['name']!r}: <<{missing.args[0]}>> isn't given, so "
                        f"{key} was left out"
                    )
    return out


# Kometa smart-filter attributes -> Coverarr library filter keys.
SEARCH_KEYS = {
    "genre": "genre",
    "year": "year",
    "decade": "decade",
    "studio": "studio",
    "network": "network",
    "resolution": "resolution",
    "content_rating": "content_rating",
}
MAX_FILTER_COMBOS = 20


def smart_filters(key: str, raw: Any) -> list[str]:
    """``plex_search`` / ``plex_all`` as library filters (each one a list of conditions that
    all hold; several filters add up). Raises ListError for what can't be expressed."""
    if key == "plex_all":
        return ["year>=0"]
    if not isinstance(raw, dict):
        raise ListError("plex_search needs all: or any: conditions")
    out: list[str] = []
    for mode, conditions in raw.items():
        if mode not in ("all", "any") or not isinstance(conditions, dict):
            raise ListError(f"plex_search: {mode!r} isn't supported (use all: or any:)")
        parts: list[list[str]] = []
        for attr, value in conditions.items():
            name, _, modifier = str(attr).partition(".")
            if name not in SEARCH_KEYS:
                raise ListError(f"plex_search: filtering on {name!r} isn't supported")
            op = {"": "=", "not": "!=", "gte": ">=", "lte": "<=", "is": "="}.get(modifier)
            if op is None:
                raise ListError(f"plex_search: {attr!r} isn't supported")
            values = value if isinstance(value, list) else [value]
            options = [f"{SEARCH_KEYS[name]}{op}{v}" for v in values]
            if op == "!=":
                parts.append([", ".join(options)])  # none of them
            else:
                parts.append(options)  # any of them
        if mode == "any":
            out += [option for options in parts for option in options]
            continue
        combos = [[]]
        for options in parts:
            combos = [c + [o] for c in combos for o in options]
            if len(combos) > MAX_FILTER_COMBOS:
                raise ListError("plex_search: too many combinations of values")
        out += [", ".join(c) for c in combos if c]
    return out


def parse_collection_file(text: str, filename: str) -> list[ParsedCollection]:
    data = _load(text, filename)
    collections = data.get("collections") or {}
    if not isinstance(collections, dict):
        raise KometaYamlError(f"{filename}: 'collections' should be a mapping of names")
    templates = data.get("templates") or {}
    if not isinstance(templates, dict):
        templates = {}
    base = PurePosixPath(filename.replace("\\", "/")).name
    parsed: list[ParsedCollection] = []
    for name, body in collections.items():
        coll = ParsedCollection(name=str(name), source_file=base)
        if not isinstance(body, dict):
            coll.warnings.append("Collection body isn't a mapping; skipped")
            parsed.append(coll)
            continue
        body = expand_templates(str(name), body, templates, coll.warnings)
        for key, raw in body.items():
            if key in ("plex_search", "plex_all"):
                try:
                    for value in smart_filters(key, raw):
                        coll.builders.append(normalize_builder("library_filter", value))
                except ListError as exc:
                    coll.warnings.append(str(exc))
            elif key == "tmdb_discover":
                for value in raw if isinstance(raw, list) else [raw]:
                    try:
                        coll.builders.append(normalize_builder(key, value))
                    except ListError as exc:
                        coll.warnings.append(str(exc))
            elif key in BUILDERS:
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
        body = body or {}
        files, defaults = [], []
        for entry in body.get("collection_files") or []:
            if isinstance(entry, dict) and entry.get("file"):
                files.append(PurePosixPath(str(entry["file"]).replace("\\", "/")).name)
            elif isinstance(entry, dict) and entry.get("default"):
                defaults.append(str(entry["default"]).strip().lower())
        config.files_by_library[str(library)] = files
        config.defaults_by_library[str(library)] = defaults
        config.overlays_by_library[str(library)] = [
            entry
            for entry in body.get("overlay_files") or []
            if isinstance(entry, dict) and entry.get("default")
        ]
        config.metadata_by_library[str(library)] = [
            PurePosixPath(str(entry["file"]).replace("\\", "/")).name
            for entry in body.get("metadata_files") or body.get("metadata_path") or []
            if isinstance(entry, dict) and entry.get("file")
        ]
    assets = (data.get("settings") or {}).get("asset_directory") or []
    config.asset_directories = [str(a) for a in (assets if isinstance(assets, list) else [assets])]
    tmdb = data.get("tmdb") or {}
    trakt = data.get("trakt") or {}
    auth = trakt.get("authorization") or {}
    config.tmdb_key = _secret(tmdb.get("apikey"))
    config.mdblist_key = _secret((data.get("mdblist") or {}).get("apikey"))
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
