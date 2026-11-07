"""Settings, read from environment variables or the project's .env file (env vars win)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Database, e.g. postgresql+psycopg://payoff:secret@localhost:5432/payoff
    database_url: str = Field(default="", alias="DATABASE_URL")

    # Market data
    data_source: str = Field(default="alpaca", alias="DATA_SOURCE")  # alpaca | tastytrade | schwab
    strikes: int = Field(default=60, alias="STRIKES")

    # Web server
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8765, alias="PORT")
    allowed_hosts: str = Field(default="", alias="ALLOWED_HOSTS")  # comma-separated public hostnames

    # Sessions: send the cookie over HTTPS only. Set true when deployed.
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    session_days: int = Field(default=30, alias="SESSION_DAYS")

    @property
    def host_allowlist(self) -> list[str]:
        extra = [h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()]
        return ["127.0.0.1", "localhost", *extra]

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if not url:
            raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
        # Hosts such as Neon and Render hand out postgres:// or postgresql:// URLs; use psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
