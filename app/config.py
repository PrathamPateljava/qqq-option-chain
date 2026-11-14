"""Settings, read from environment variables or the project's .env file (env vars win)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

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
    # Public address of the site for links in emails, e.g. https://qqq-payoff.onrender.com.
    # Blank: links use the address the site was opened at.
    app_url: str = Field(default="", alias="APP_URL")

    # Sessions: send the cookie over HTTPS only. Set true when deployed.
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    session_days: int = Field(default=30, alias="SESSION_DAYS")

    # Email: "console" prints messages in the terminal (local development); "brevo" sends them.
    email_backend: str = Field(default="console", alias="EMAIL_BACKEND")
    email_from: str = Field(default="", alias="EMAIL_FROM")  # a sender address verified with the provider
    email_from_name: str = Field(default="Payoff", alias="EMAIL_FROM_NAME")
    brevo_api_key: str = Field(default="", alias="BREVO_API_KEY")

    @property
    def host_allowlist(self) -> list[str]:
        extra = [h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()]
        app_host = urlparse(self.app_url.strip()).hostname if self.app_url.strip() else None
        return ["127.0.0.1", "localhost", *extra, *([app_host] if app_host else [])]

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
