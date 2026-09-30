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
    "http_proxy",
    "https_proxy",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every config var so tests see the true defaults, not the host's env."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
