"""Shared pytest fixtures."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP

from youtube_mcp.config import Settings
from youtube_mcp.tools import Deps, register_all
from youtube_mcp.youtube.quota import QuotaCounter

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    """Load a recorded response — success and error envelopes share one directory."""
    return json.loads((FIXTURES / name).read_text())


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
    """Drop every section 12 config var so tests see the true defaults, not the host's env.

    pydantic-settings matches env names case-insensitively, so a host-level lowercase
    `youtube_api_key` would otherwise leak in — strip both spellings.
    """
    for var in ENV_VARS:
        for spelling in (var, var.lower()):
            monkeypatch.delenv(spelling, raising=False)


class StubYouTubeClient:
    """Offline stand-in for `YouTubeClient`: scripted responses, recorded calls, no sockets.

    Each method's response is looked up by method name in `responses` and may be:
    a value, an exception to raise, or a callable receiving the keyword arguments. A
    `list_playlist_items` script is naturally a callable keyed on `page_token`.
    """

    def __init__(self, **responses: Any) -> None:
        self.quota = QuotaCounter()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses

    async def _respond(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, kwargs))
        response = self.responses.get(method)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, Callable):
            return response(**kwargs)
        return response

    def kwargs(self, index: int = -1) -> dict[str, Any]:
        return self.calls[index][1]

    async def search_videos(self, query: str, **kwargs: Any) -> Any:
        return await self._respond("search_videos", {"query": query, **kwargs})

    async def batch_get_stats(self, video_ids: Any) -> Any:
        return await self._respond("batch_get_stats", {"video_ids": list(video_ids)})

    async def list_videos(self, video_ids: Any, parts: Any = ()) -> Any:
        return await self._respond(
            "list_videos", {"video_ids": list(video_ids), "parts": list(parts)}
        )

    async def list_channel(self, **kwargs: Any) -> Any:
        return await self._respond("list_channel", kwargs)

    async def list_playlist_items(self, playlist_id: str, **kwargs: Any) -> Any:
        return await self._respond("list_playlist_items", {"playlist_id": playlist_id, **kwargs})

    async def list_comment_threads(self, video_id: str, **kwargs: Any) -> Any:
        return await self._respond("list_comment_threads", {"video_id": video_id, **kwargs})

    async def list_video_categories(self, **kwargs: Any) -> Any:
        return await self._respond("list_video_categories", kwargs)


def make_test_server(
    *,
    settings: Settings | None = None,
    cache: Any = None,
    **responses: Any,
) -> tuple[FastMCP, StubYouTubeClient]:
    """A `FastMCP` with all 12 tools registered over a stub client (no ASGI app, no sockets).

    `cache` defaults to `None`, which is also a real code path: every tool has to work when
    the cache is absent.
    """
    stub = StubYouTubeClient(**responses)
    resolved = settings or Settings(_env_file=None)
    mcp = FastMCP("test-server")
    register_all(mcp, Deps(client=stub, cache=cache, settings=resolved))  # type: ignore[arg-type]
    return mcp, stub
