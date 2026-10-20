from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level settings from the environment.

    Everything a user edits in the UI (servers, API keys) lives in the database instead.
    """

    model_config = SettingsConfigDict(env_prefix="COVERARR_")

    config_dir: Path = Path("/config")
    host: str = "0.0.0.0"
    port: int = 2683
    # Built frontend assets; empty/missing means API-only (dev mode with Vite proxy).
    static_dir: Path = Path(__file__).resolve().parent / "static"
    log_level: str = "INFO"

    @property
    def database_path(self) -> Path:
        return self.config_dir / "coverarr.db"

    @property
    def cache_dir(self) -> Path:
        return self.config_dir / "cache"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
