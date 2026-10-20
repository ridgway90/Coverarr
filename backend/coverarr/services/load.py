"""How hard Coverarr works the media servers (Settings → General → Server load)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.services.settings_store import get_setting


@dataclass(frozen=True)
class LoadProfile:
    name: str
    # Parallel requests per server, and items per listing page.
    concurrency: int
    page_size: int
    # Import all servers at once rather than one after another.
    parallel_servers: bool
    # Parallel poster downloads per server during a poster scan.
    poster_concurrency: int


PROFILES = {
    "gentle": LoadProfile(
        "gentle", concurrency=2, page_size=200, parallel_servers=False, poster_concurrency=3
    ),
    "normal": LoadProfile(
        "normal", concurrency=4, page_size=500, parallel_servers=True, poster_concurrency=6
    ),
    "fast": LoadProfile(
        "fast", concurrency=8, page_size=1000, parallel_servers=True, poster_concurrency=12
    ),
}


async def load_profile(session: AsyncSession) -> LoadProfile:
    return PROFILES.get(await get_setting(session, "server_load"), PROFILES["normal"])
