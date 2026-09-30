"""Config parsing, defaults and fail-fast validation (§12)."""

import pytest
from pydantic import ValidationError

from youtube_mcp.config import MissingApiKeyError, Settings, get_settings


def test_defaults(clean_env: None) -> None:
    settings = Settings()
    assert settings.youtube_transcript_lang == "en"
    assert settings.mcp_transport == "http"
    assert (settings.mcp_host, settings.mcp_port) == ("0.0.0.0", 8088)
    assert settings.fastmcp_stateless_http is True
    assert settings.response_limit == 50_000
    assert settings.cache_ttl_seconds == 3600
    assert settings.database_path.parts[-1] == "cache.db"
    assert settings.log_level == "INFO"
    assert settings.webshare_proxy_username is None
    assert settings.webshare_proxy_password is None
    assert settings.http_proxy is None
    assert settings.https_proxy is None


def test_env_overrides(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("MCP_PORT", "9000")
    monkeypatch.setenv("RESPONSE_LIMIT", "1000")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "10")
    monkeypatch.setenv("DATABASE_PATH", "/tmp/other.db")
    monkeypatch.setenv("FASTMCP_STATELESS_HTTP", "false")
    settings = Settings()
    assert settings.mcp_transport == "stdio"
    assert settings.mcp_port == 9000
    assert settings.response_limit == 1000
    assert settings.cache_ttl_seconds == 10
    assert settings.database_path.as_posix() == "/tmp/other.db"
    assert settings.fastmcp_stateless_http is False
    assert settings.require_api_key() == "test-key"


def test_api_key_never_leaks_in_repr(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "super-secret")
    monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "proxy-secret")
    settings = Settings()
    dumped = repr(settings)
    assert "super-secret" not in dumped
    assert "proxy-secret" not in dumped


def test_api_key_may_be_absent_for_tests(clean_env: None) -> None:
    settings = Settings()
    assert settings.youtube_api_key is None
    with pytest.raises(MissingApiKeyError, match="YOUTUBE_API_KEY"):
        settings.require_api_key()


def test_invalid_transport_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_port_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PORT", "70000")
    with pytest.raises(ValidationError):
        Settings()


def test_get_settings_is_cached(clean_env: None) -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()
