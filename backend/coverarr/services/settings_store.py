"""Key/value app settings stored in the database (API keys, secrets, preferences)."""

import secrets
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.models import AppSetting

# Settings exposed through the settings API. Secrets are write-only from the UI's view.
PUBLIC_KEYS = {
    "tmdb_api_key",
    "trakt_client_id",
    "trakt_client_secret",
    "mdblist_api_key",
    "watch_region",
    "poster_match_threshold",
    "server_load",
}
SECRET_KEYS = {"tmdb_api_key", "trakt_client_secret", "mdblist_api_key"}

DEFAULTS: dict[str, Any] = {
    # Max Hamming distance (of 64 bits) for two posters to count as "the same image".
    "poster_match_threshold": 10,
    # gentle | normal | fast, see services/load.py
    "server_load": "normal",
    # Country whose streaming services count (TMDb's watch providers), e.g. GB.
    "watch_region": "US",
}


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(AppSetting, key)
    if row is None:
        return DEFAULTS.get(key, default)
    return row.value


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def ensure_secret(session: AsyncSession, key: str) -> str:
    """Return a persisted random secret, creating it on first use."""
    value = await get_setting(session, key)
    if not value:
        value = secrets.token_urlsafe(32)
        await set_setting(session, key, value)
        await session.commit()
    return value
