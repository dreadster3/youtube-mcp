"""Configuration (§12 of HANDOFF.md) — one pydantic-settings model, read once at startup.

Environment variables map case-insensitively onto field names (`MCP_HOST` -> `mcp_host`).
A `.env` file is read if present; unset proxy vars stay `None`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MissingApiKeyError(ValueError):
    """Raised when the YouTube API key is required but unset (fail fast, §12)."""


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # YouTube Data API — a single key. Multi-key rotation is a policy violation (§5.3).
    youtube_api_key: SecretStr | None = Field(
        default=None,
        description="YouTube Data API key. Required at startup.",
    )
    youtube_transcript_lang: str = Field(
        default="en",
        min_length=2,
        description="Default transcript language",
    )

    # MCP server transport.
    mcp_transport: Literal["http", "stdio"] = "http"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = Field(default=8088, ge=1, le=65535)
    fastmcp_stateless_http: bool = Field(
        default=True,
        description="Stateless HTTP so any replica can serve a request (§13)",
    )

    # Behaviour limits.
    response_limit: int = Field(default=50_000, gt=0, description="Transcript truncation threshold")
    cache_ttl_seconds: int = Field(default=3600, ge=0)
    database_path: Path = Field(default=Path("cache.db"), description="SQLite cache file")
    log_level: str = "INFO"

    # Proxies — unset by default (residential connection, §9).
    webshare_proxy_username: str | None = None
    webshare_proxy_password: SecretStr | None = None
    http_proxy: str | None = None
    https_proxy: str | None = None

    def require_api_key(self) -> str:
        """Return the API key, or raise if it is missing.

        Called on the startup path so tests can build a `Settings` without a key.
        """
        if self.youtube_api_key is None:
            raise MissingApiKeyError("YOUTUBE_API_KEY is required")
        return self.youtube_api_key.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read configuration once (§12)."""
    return Settings()
