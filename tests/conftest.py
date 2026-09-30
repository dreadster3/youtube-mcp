"""Shared pytest fixtures."""

import pytest

ENV_VARS = [
    "YOUTUBE_API_KEY",
    "YOUTUBE_TRANSCRIPT_LANG",
    "MCP_TRANSPORT",
    "MCP_HOST",
    "MCP_PORT",
    "FASTMCP_STATELESS_HTTP",
    "RESPONSE_LIMIT",
    "CACHE_TTL_SECONDS",
    "DATABASE_PATH",
    "LOG_LEVEL",
    "WEBSHARE_PROXY_USERNAME",
    "WEBSHARE_PROXY_PASSWORD",
    "HTTP_PROXY",
    "HTTPS_PROXY",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every §12 config var so tests see the true defaults, not the host's env.

    pydantic-settings matches env names case-insensitively, so a host-level lowercase
    `youtube_api_key` would otherwise leak in — strip both spellings.
    """
    for var in ENV_VARS:
        for spelling in (var, var.lower()):
            monkeypatch.delenv(spelling, raising=False)
