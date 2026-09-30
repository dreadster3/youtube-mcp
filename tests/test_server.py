"""Server-assembly tests (§13): tool surface, /health, factory gate, stdio dispatch, offline.

The server is driven two ways: through `build_mcp` with an in-memory FastMCP client (fast,
covers the tools and their structured output) and through `create_app` with an ASGI transport
(covers the HTTP wiring and the fail-fast API-key gate). Nothing here opens a socket.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from conftest import StubYouTubeClient
from youtube_mcp import server as server_module
from youtube_mcp.cache import Cache
from youtube_mcp.config import MissingApiKeyError, Settings
from youtube_mcp.server import (
    SERVER_NAME,
    ServerResources,
    build_client,
    build_mcp,
    create_app,
    main,
)
from youtube_mcp.youtube import models
from youtube_mcp.youtube.client import QuotaExceededError
from youtube_mcp.youtube.quota import QuotaBucket, QuotaCounter

CHANNEL_ID = "UCBR8-60-B28hp2BmDPdntcQ"
UPLOADS_ID = "UUBR8-60-B28hp2BmDPdntcQ"

EXPECTED_TOOLS = {
    "youtube_get_transcript",
    "youtube_get_timestamped_transcript",
    "youtube_list_transcript_languages",
    "youtube_search_in_transcript",
    "youtube_search_videos",
    "youtube_get_video",
    "youtube_get_video_stats",
    "youtube_get_comments",
    "youtube_get_channel",
    "youtube_list_channel_videos",
    "youtube_list_categories",
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        youtube_api_key="test-key",
        database_path=tmp_path / "cache.db",
    )


@pytest.fixture
def resources(settings: Settings) -> ServerResources:
    """A server-resource bundle over a stub client and a real (tmp) SQLite cache."""
    return ServerResources(
        cache=Cache(str(settings.database_path)),
        client=StubYouTubeClient(),  # type: ignore[arg-type]
        owns_client=False,
    )


# ------------------------------------------------------------------------ tool surface


async def test_tools_list_exposes_exactly_the_eleven_namespaced_tools(
    settings: Settings, resources: ServerResources
) -> None:
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == EXPECTED_TOOLS
    assert all(name.startswith("youtube_") for name in names)


async def test_every_tool_has_a_substantive_description(
    settings: Settings, resources: ServerResources
) -> None:
    """Descriptions are the model's only documentation (§8), so short ones are a defect."""
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        tools = await client.list_tools()

    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 200, f"{tool.name} description is too thin"
        assert tool.input_schema["type"] == "object"
        assert tool.output_schema, f"{tool.name} declares no output schema"


@pytest.mark.parametrize(
    ("tool_name", "phrase"),
    [
        ("youtube_search_videos", "100 calls/day"),
        ("youtube_search_videos", "scarce"),
        ("youtube_get_video_stats", "10,000-call/day"),
        ("youtube_get_video", "shared"),
        ("youtube_get_comments", "shared"),
        ("youtube_get_comments", "replies are not returned"),
        ("youtube_list_channel_videos", "shared"),
        ("youtube_list_categories", "shared"),
    ],
)
async def test_quota_and_limitation_wording_is_present_in_descriptions(
    settings: Settings, resources: ServerResources, tool_name: str, phrase: str
) -> None:
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert phrase in tools[tool_name].description


async def test_comments_description_does_not_promise_reply_counts(
    settings: Settings, resources: ServerResources
) -> None:
    """The Comment model has no `total_reply_count`, so the prose must not promise one."""
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    comments = tools["youtube_get_comments"]
    assert "total_reply_count" not in comments.description
    assert "replies are not returned" in comments.description
    assert "counts" in comments.description
    assert comments.output_schema is not None
    assert "reply_count" not in comments.output_schema.get("properties", {}).get("items", {}).get(
        "items", {}
    ).get("properties", {})


async def test_dislike_unavailability_is_stated_on_both_stat_tools(
    settings: Settings, resources: ServerResources
) -> None:
    """§5.2: say it plainly so the model stops asking."""
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("youtube_get_video", "youtube_get_video_stats"):
        description = tools[name].description.lower()
        assert "dislike" in description
        assert "private" in description or "not exist" in description


async def test_server_instructions_state_the_real_quota_buckets(
    settings: Settings, resources: ServerResources
) -> None:
    """§5.3: batchGetStats has its own 10,000/day bucket — the instructions must say so."""
    mcp = build_mcp(settings, resources)

    instructions = mcp.instructions

    assert "search.list allows 100 calls/day" in instructions
    assert "batchGetStats) have their own 10,000-call/day bucket" in instructions
    assert "everything else shares 10,000" not in instructions


async def test_no_tool_has_a_dislike_field(
    settings: Settings, resources: ServerResources
) -> None:
    """§5.2 is about the *field*: no schema may offer one, whatever the prose says."""
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        tools = await client.list_tools()

    for tool in tools:
        properties = {
            **tool.input_schema.get("properties", {}),
            **tool.output_schema.get("properties", {}),
        }
        assert "dislike_count" not in properties
        assert not any("dislike" in name for name in properties)


# ------------------------------------------------------------------- structured output


async def test_pydantic_return_produces_structured_content(
    settings: Settings, resources: ServerResources
) -> None:
    """§13: structured output, not pre-formatted prose."""
    resources.client.responses["search_videos"] = models.SearchResults.from_api(
        {
            "items": [
                {
                    "id": {"kind": "youtube#video", "videoId": "dQw4w9WgXcQ"},
                    "snippet": {"title": "T", "description": "D"},
                }
            ],
            "nextPageToken": "CAUQAA",
        }
    )
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        result = await client.call_tool("youtube_search_videos", {"query": "x"})

    assert result.structured_content is not None
    assert result.structured_content["items"][0]["video_id"] == "dQw4w9WgXcQ"
    assert result.structured_content["next_page_token"] == "CAUQAA"
    assert result.is_error is False


async def test_category_tool_returns_structured_content(
    settings: Settings, resources: ServerResources
) -> None:
    resources.client.responses["list_video_categories"] = models.video_categories_from_api(
        {"items": [{"id": "10", "snippet": {"title": "Music", "assignable": True}}]}
    )
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        result = await client.call_tool("youtube_list_categories", {"region_code": "PT"})

    assert result.structured_content == {
        "region_code": "PT",
        "items": [{"category_id": "10", "title": "Music", "assignable": True}],
    }


async def test_tool_error_arrives_as_an_error_result_not_a_protocol_error(
    settings: Settings, resources: ServerResources
) -> None:
    """FastMCP returns ToolError as `isError`, never as a JSON-RPC failure (research §4)."""
    resources.client.responses["search_videos"] = QuotaExceededError(
        "quotaExceeded", reason="quotaExceeded"
    )
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "youtube_search_videos", {"query": "x"}, raise_on_error=False
        )

    assert result.is_error is True
    assert result.structured_content is None
    text = result.content[0].text
    assert "do not retry today" in text
    assert "[quotaExceeded]" in text


async def test_tool_error_raises_client_side_when_asked(
    settings: Settings, resources: ServerResources
) -> None:
    resources.client.responses["search_videos"] = QuotaExceededError("quotaExceeded")
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="quota"):
            await client.call_tool("youtube_search_videos", {"query": "x"})


async def test_channel_tool_returns_structured_content(
    settings: Settings, resources: ServerResources
) -> None:
    resources.client.responses["list_channel"] = models.Channel(
        channel_id=CHANNEL_ID, title="T", uploads_playlist_id=UPLOADS_ID
    )
    mcp = build_mcp(settings, resources)

    async with Client(mcp) as client:
        result = await client.call_tool("youtube_get_channel", {"channel_id": CHANNEL_ID})

    assert result.structured_content["channel"]["channel_id"] == CHANNEL_ID


# ----------------------------------------------------------------------------- /health


async def test_health_route_returns_ok(
    settings: Settings, resources: ServerResources
) -> None:
    app = _app_with(settings, resources)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["server"] == SERVER_NAME


async def test_health_reports_quota_remaining_as_approximate(
    settings: Settings, resources: ServerResources
) -> None:
    """§5.4 + batch-2 S1: never present our counter as Google's exact accounting."""
    resources.client.quota.consume(QuotaBucket.SEARCH)
    app = _app_with(settings, resources)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = (await client.get("/health")).json()

    assert payload["quota_remaining_approximate"]["search"] == 99
    assert payload["quota_remaining_approximate"]["stats"] == 10_000
    assert "quota_remaining" not in payload


async def test_health_works_without_the_mcp_lifespan(
    settings: Settings, resources: ServerResources
) -> None:
    """A probe must answer even if the MCP session manager never started."""
    app = _app_with(settings, resources)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200


async def test_mcp_endpoint_requires_the_lifespan_when_posted_to(
    settings: Settings, resources: ServerResources
) -> None:
    """The session manager only exists inside the lifespan — posting outside it fails loudly.

    Guards the deployment note in the module docstring: `module:app` uvicorn works because
    uvicorn runs the lifespan, and mounting into another Starlette app requires forwarding it.
    """
    app = _app_with(settings, resources)

    with pytest.raises(RuntimeError, match="lifespan"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )


# ---------------------------------------------------------------------- cache lifespan


async def test_lifespan_connects_then_closes_the_cache(
    settings: Settings, resources: ServerResources
) -> None:
    """Batch-1 landmine 4: `Cache.get()` silently no-ops before `connect()`."""
    mcp = build_mcp(settings, resources)
    assert resources.cache._db is None

    async with Client(mcp):
        assert resources.cache._db is not None

    assert resources.cache._db is None


async def test_lifespan_does_not_close_an_injected_client(
    settings: Settings, resources: ServerResources
) -> None:
    """An injected client belongs to the caller; only an owned one is closed."""
    closed: list[bool] = []

    class ClosableStub(StubYouTubeClient):
        async def aclose(self) -> None:
            closed.append(True)

    resources = ServerResources(
        cache=Cache(str(settings.database_path)),
        client=ClosableStub(),  # type: ignore[arg-type]
        owns_client=False,
    )
    mcp = build_mcp(settings, resources)

    async with Client(mcp):
        pass

    assert closed == []


async def test_lifespan_closes_an_owned_client(
    settings: Settings, resources: ServerResources
) -> None:
    closed: list[bool] = []

    class ClosableStub(StubYouTubeClient):
        async def aclose(self) -> None:
            closed.append(True)

    resources = ServerResources(
        cache=Cache(str(settings.database_path)),
        client=ClosableStub(),  # type: ignore[arg-type]
        owns_client=True,
    )
    mcp = build_mcp(settings, resources)

    async with Client(mcp):
        pass

    assert closed == [True]


# ------------------------------------------------------------------ factory (create_app)


def _app_with(settings: Settings, resources: ServerResources) -> Any:
    """Build the ASGI app from already-made resources, skipping the client factory."""
    mcp = build_mcp(settings, resources)
    return mcp.http_app(stateless_http=settings.fastmcp_stateless_http)


def test_create_app_builds_a_stateless_http_app(settings: Settings, monkeypatch) -> None:
    app = create_app(settings, client=StubYouTubeClient())  # type: ignore[arg-type]

    assert str(app.state.transport_type) == "streamable-http"
    assert app.state.path == "/mcp"
    assert str(app.state.fastmcp_server.name) == SERVER_NAME
    routes = {getattr(route, "path", None) for route in app.router.routes}
    assert "/health" in routes
    assert "/mcp" in routes


def test_create_app_requires_an_api_key_at_factory_time(clean_env: None) -> None:
    """§12 fail-fast: a misconfigured pod must crash at startup, not on the first tool call."""
    settings = Settings(_env_file=None, youtube_api_key=None)

    with pytest.raises(MissingApiKeyError, match="YOUTUBE_API_KEY"):
        create_app(settings, client=StubYouTubeClient())  # type: ignore[arg-type]


def test_create_app_accepts_an_injected_client_and_cache(settings: Settings) -> None:
    stub = StubYouTubeClient()
    cache = Cache(str(settings.database_path))

    app = create_app(settings, client=stub, cache=cache)  # type: ignore[arg-type]

    server = app.state.fastmcp_server
    assert server is not None


def test_create_app_builds_its_own_client_when_none_is_injected(settings: Settings) -> None:
    """The owned client is closed by the lifespan, so the app owns its resources."""
    app = create_app(settings)

    assert app.state.fastmcp_server is not None


async def test_create_app_reaches_health_through_the_real_wiring(
    settings: Settings, tmp_path: Path
) -> None:
    """End-to-end through `create_app`: injected client, real SQLite cache, ASGI transport."""
    cache = Cache(str(tmp_path / "e2e.db"))
    app = create_app(settings, client=StubYouTubeClient(), cache=cache)  # type: ignore[arg-type]

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")
        assert cache._db is not None

    assert response.status_code == 200
    assert cache._db is None


async def test_factory_supports_uvicorn_factory_style_reference() -> None:
    """`uvicorn youtube_mcp.server:create_app --factory` calls it with no arguments."""
    with tempfile.TemporaryDirectory() as directory:
        import os

        key_before = os.environ.get("YOUTUBE_API_KEY")
        os.environ["YOUTUBE_API_KEY"] = "factory-key"
        os.environ["DATABASE_PATH"] = str(Path(directory) / "cache.db")
        server_module.get_settings.cache_clear()
        try:
            app = create_app()
            assert app.state.fastmcp_server is not None
        finally:
            if key_before is None:
                os.environ.pop("YOUTUBE_API_KEY", None)
            else:
                os.environ["YOUTUBE_API_KEY"] = key_before
            os.environ.pop("DATABASE_PATH", None)
            server_module.get_settings.cache_clear()


# ------------------------------------------------------------------------------ main()


def test_main_stdio_dispatches_to_mcp_run(monkeypatch, tmp_path: Path) -> None:
    """`MCP_TRANSPORT=stdio` runs the stdio transport without building or serving HTTP."""
    calls: list[dict] = []
    stdio_settings = Settings(
        _env_file=None,
        youtube_api_key="k",
        mcp_transport="stdio",
        database_path=tmp_path / "cache.db",
    )
    monkeypatch.setattr(server_module, "get_settings", lambda: stdio_settings)
    monkeypatch.setattr(server_module.FastMCP, "run", lambda self, **kwargs: calls.append(kwargs))

    main()

    assert calls == [{"transport": "stdio"}]


def test_main_http_uses_uvicorn_with_the_factory(monkeypatch, tmp_path: Path) -> None:
    """The http path hands uvicorn the factory reference, not a pre-built app."""
    http_settings = Settings(
        _env_file=None,
        youtube_api_key="k",
        mcp_transport="http",
        mcp_host="127.0.0.1",
        mcp_port=9999,
        database_path=tmp_path / "cache.db",
    )
    recorded: list[dict] = []
    monkeypatch.setattr(server_module, "get_settings", lambda: http_settings)
    monkeypatch.setattr(
        server_module.uvicorn,
        "run",
        lambda target, **kwargs: recorded.append({"target": target, **kwargs}),
    )

    main()

    assert recorded == [
        {
            "target": "youtube_mcp.server:create_app",
            "factory": True,
            "host": "127.0.0.1",
            "port": 9999,
            "log_level": "info",
        }
    ]


def test_main_fails_fast_without_an_api_key(monkeypatch, clean_env: None) -> None:
    """Both transports gate on the key before doing anything else (§12)."""
    keyless = Settings(_env_file=None, youtube_api_key=None)
    monkeypatch.setattr(server_module, "get_settings", lambda: keyless)

    with pytest.raises(MissingApiKeyError, match="YOUTUBE_API_KEY"):
        main()


# ------------------------------------------------------------- quota warning wiring


def test_low_remaining_warning_fires_at_ten_percent(caplog) -> None:
    """Batch-2 hook: warn once when a bucket drops to ≤10% (9 units of 100)."""
    counter = QuotaCounter(on_low_remaining=server_module._low_remaining_logger)
    with caplog.at_level("WARNING"):
        counter.consume(QuotaBucket.SEARCH, 91)

    assert "quota bucket search down to 9 units remaining" in caplog.text


def test_build_client_wires_the_low_quota_warning(settings: Settings, caplog) -> None:
    """The owned client must carry the hook, or an exhausted bucket is only found via 403."""
    client = build_client(settings)

    with caplog.at_level("WARNING"):
        client.quota.consume(QuotaBucket.SEARCH, 91)

    assert "down to 9 units remaining" in caplog.text
