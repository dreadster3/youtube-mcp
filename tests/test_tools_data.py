"""Tool-logic tests for the Data API group (§8), offline.

The stub client from `conftest` records calls and replays scripted responses, so these tests
assert what the *tool* adds on top of the Batch-2 client: argument validation, the
uploads-playlist walk with its cache, the stats cache, quota-cost wording and the mapping of
each client error class onto a model-facing message. Fixtures are the recorded response
envelopes from `tests/fixtures/`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastmcp import Client

from conftest import fixture, make_test_server
from youtube_mcp.cache import Cache
from youtube_mcp.config import Settings
from youtube_mcp.tools import data as tools_data
from youtube_mcp.youtube import models
from youtube_mcp.youtube.client import (
    CommentsDisabledError,
    InvalidRequestError,
    NotFoundError,
    QuotaExceededError,
    RateLimitedError,
    UpstreamError,
)
from youtube_mcp.youtube.quota import QuotaBucket, QuotaExceeded

CHANNEL_ID = "UCBR8-60-B28hp2BmDPdntcQ"
UPLOADS_ID = "UUBR8-60-B28hp2BmDPdntcQ"

_OPEN_CACHES: list[Cache] = []


@pytest.fixture(autouse=True)
async def close_test_caches():
    """Close every `Cache` this module opened — aiosqlite's worker thread is non-daemon, so a
    left-open connection makes pytest hang at exit."""
    yield
    for cache in list(_OPEN_CACHES):
        await cache.close()
    _OPEN_CACHES.clear()


def make_cache(tmp_path) -> Cache:
    cache = Cache(str(tmp_path / "cache.db"))
    _OPEN_CACHES.append(cache)
    return cache


async def call_tool(mcp, name: str, arguments: dict) -> Any:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
        return result.structured_content


async def call_error(mcp, name: str, arguments: dict) -> str:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
        assert result.is_error
        return result.content[0].text


# ---------------------------------------------------------------- argument validation


async def test_get_video_accepts_a_single_string_id() -> None:
    mcp, stub = make_test_server(list_videos=models.videos_from_api(fixture("search_list.json")))

    await call_tool(mcp, "youtube_get_video", {"video_ids": "dQw4w9WgXcQ"})

    assert stub.kwargs()["video_ids"] == ["dQw4w9WgXcQ"]


async def test_get_video_accepts_a_list_and_passes_parts() -> None:
    mcp, stub = make_test_server(list_videos=[])

    await call_tool(
        mcp,
        "youtube_get_video",
        {"video_ids": ["a", "b"], "parts": ["snippet"]},
    )

    assert stub.kwargs()["video_ids"] == ["a", "b"]
    assert stub.kwargs()["parts"] == ["snippet"]


async def test_get_video_rejects_more_than_50_ids() -> None:
    mcp, stub = make_test_server(list_videos=[])

    message = await call_error(
        mcp, "youtube_get_video", {"video_ids": [f"v{index}" for index in range(51)]}
    )

    assert "at most 50 video IDs" in message
    assert stub.calls == []


async def test_get_video_rejects_empty_list() -> None:
    mcp, stub = make_test_server(list_videos=[])

    message = await call_error(mcp, "youtube_get_video", {"video_ids": []})

    assert "at least one video ID" in message
    assert stub.calls == []


async def test_get_video_reports_missing_ids_as_data() -> None:
    """A deleted or misspelled ID is partial success, not an error."""
    payload = {
        "items": [
            {"id": "found", "snippet": {"title": "t"}, "statistics": {}, "contentDetails": {}}
        ]
    }
    mcp, _ = make_test_server(list_videos=models.videos_from_api(payload))

    result = await call_tool(mcp, "youtube_get_video", {"video_ids": ["found", "gone"]})

    assert [video["video_id"] for video in result["videos"]] == ["found"]
    assert result["missing_video_ids"] == ["gone"]


async def test_get_video_stats_rejects_more_than_50_ids() -> None:
    mcp, stub = make_test_server(batch_get_stats=models.BatchStatsResponse())

    message = await call_error(
        mcp, "youtube_get_video_stats", {"video_ids": [f"v{index}" for index in range(51)]}
    )

    assert "at most 50 video IDs" in message
    assert stub.calls == []


# ---------------------------------------------------------------------- search videos


async def test_search_videos_passes_every_documented_filter() -> None:
    mcp, stub = make_test_server(search_videos=models.SearchResults())
    published_after = datetime(2024, 1, 1, tzinfo=timezone.utc)

    await call_tool(
        mcp,
        "youtube_search_videos",
        {
            "query": "kubernetes",
            "max_results": 20,
            "order": "date",
            "published_after": "2024-01-01T00:00:00Z",
            "video_category_id": "28",
            "region_code": "PT",
            "safe_search": "strict",
            "page_token": "CAUQAA",
        },
    )

    kwargs = stub.kwargs()
    assert stub.calls[0][0] == "search_videos"
    assert kwargs["query"] == "kubernetes"
    assert kwargs["max_results"] == 20
    assert kwargs["order"] == "date"
    # The annotation is `datetime`, so FastMCP parses the RFC 3339 string — timezone-aware,
    # as the client requires.
    assert kwargs["published_after"] == published_after
    assert kwargs["published_before"] is None
    assert kwargs["video_category_id"] == "28"
    assert kwargs["region_code"] == "PT"
    assert kwargs["safe_search"] == "strict"
    assert kwargs["page_token"] == "CAUQAA"


async def test_search_videos_accepts_an_offset_datetime_string() -> None:
    """A model that emits a full timestamp gets an aware datetime, never a naive one."""
    mcp, stub = make_test_server(search_videos=models.SearchResults())

    await call_tool(
        mcp,
        "youtube_search_videos",
        {"query": "x", "published_before": "2024-06-01T12:00:00+00:00"},
    )

    assert stub.kwargs()["published_before"] == datetime(2024, 6, 1, 12, tzinfo=timezone.utc)


async def test_search_videos_rejects_out_of_range_max_results() -> None:
    mcp, stub = make_test_server(search_videos=models.SearchResults())

    message = await call_error(
        mcp, "youtube_search_videos", {"query": "x", "max_results": 51}
    )

    assert "less than or equal to 50" in message
    assert stub.calls == []


async def test_search_videos_rejects_blank_query_without_calling_the_client() -> None:
    """A whitespace-only query is not a query: fail loudly instead of searching for spaces."""
    mcp, stub = make_test_server(search_videos=models.SearchResults())

    message = await call_error(mcp, "youtube_search_videos", {"query": "   "})

    assert "query must not be empty" in message
    assert stub.calls == []


async def test_search_videos_returns_page_token() -> None:
    mcp, _ = make_test_server(
        search_videos=models.SearchResults.from_api(fixture("search_list.json"))
    )

    result = await call_tool(mcp, "youtube_search_videos", {"query": "x"})

    assert result["next_page_token"] == "CAUQAA"
    assert len(result["items"]) == 2


# ------------------------------------------------------------------- batch get stats


async def test_get_video_stats_surfaces_partial_failure_as_data() -> None:
    response = models.BatchStatsResponse.from_api(
        fixture("batch_get_stats_partial_failure.json")
    )
    succeeded = [item.video_id for item in response.items]
    failed = response.summary.failed_video_ids
    mcp, _ = make_test_server(batch_get_stats=response)

    result = await call_tool(mcp, "youtube_get_video_stats", {"video_ids": [*succeeded, *failed]})

    assert result["failed_video_ids"] == failed
    assert result["requested_video_count"] == len(succeeded) + len(failed)
    assert result["succeeded_video_count"] == len(succeeded)
    assert result["cached"] is False


async def test_get_video_stats_second_call_is_served_from_cache(
    tmp_path,
) -> None:
    """A repeated stats call must not spend quota again (§14)."""
    payload = models.BatchStatsResponse.from_api(fixture("batch_get_stats.json"))
    mcp, stub = make_test_server(
        settings=Settings(_env_file=None), cache=make_cache(tmp_path), batch_get_stats=payload
    )
    ids = [item.video_id for item in payload.items]

    first = await call_tool(mcp, "youtube_get_video_stats", {"video_ids": ids})
    second = await call_tool(mcp, "youtube_get_video_stats", {"video_ids": ids})

    assert len(stub.calls) == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert [item["video_id"] for item in second["items"]] == ids


async def test_get_video_stats_caches_only_the_misses(
    tmp_path,
) -> None:
    payload = models.BatchStatsResponse.from_api(fixture("batch_get_stats.json"))
    mcp, stub = make_test_server(
        settings=Settings(_env_file=None), cache=make_cache(tmp_path), batch_get_stats=payload
    )
    known = payload.items[0].video_id

    await call_tool(mcp, "youtube_get_video_stats", {"video_ids": [known]})
    stub.responses["batch_get_stats"] = models.BatchStatsResponse(items=[], summary=models.BatchStatsSummary())
    result = await call_tool(mcp, "youtube_get_video_stats", {"video_ids": [known, "brand-new"]})

    assert stub.calls[1][1]["video_ids"] == ["brand-new"]
    assert result["failed_video_ids"] == ["brand-new"]
    assert [item["video_id"] for item in result["items"]] == [known]
    assert result["cached"] is False


async def test_get_video_stats_works_without_a_cache() -> None:
    payload = models.BatchStatsResponse.from_api(fixture("batch_get_stats.json"))
    mcp, _ = make_test_server(batch_get_stats=payload)

    result = await call_tool(
        mcp, "youtube_get_video_stats", {"video_ids": [item.video_id for item in payload.items]}
    )

    assert result["cached"] is False
    assert len(result["items"]) == len(payload.items)


# -------------------------------------------------------------------------- comments


async def test_get_comments_returns_top_level_comments_only() -> None:
    page = models.CommentThreadPage.from_api(fixture("comment_threads.json"))
    mcp, stub = make_test_server(list_comment_threads=page)

    result = await call_tool(mcp, "youtube_get_comments", {"video_id": "dQw4w9WgXcQ"})

    assert result["video_id"] == "dQw4w9WgXcQ"
    assert result["next_page_token"] == page.next_page_token
    assert len(result["items"]) == 2
    # Replies are absent in v1: only the top-level comment is returned, and no reply text
    # field can exist on the model.
    assert "repl" not in str(result).lower()


async def test_get_comments_passes_order_and_page_token() -> None:
    mcp, stub = make_test_server(list_comment_threads=models.CommentThreadPage())

    await call_tool(
        mcp,
        "youtube_get_comments",
        {"video_id": "v", "max_results": 50, "order": "relevance", "page_token": "Qg8QAA"},
    )

    kwargs = stub.kwargs()
    assert kwargs["video_id"] == "v"
    assert kwargs["max_results"] == 50
    assert kwargs["order"] == "relevance"
    assert kwargs["page_token"] == "Qg8QAA"


async def test_get_comments_disabled_maps_to_a_friendly_message() -> None:
    mcp, _ = make_test_server(
        list_comment_threads=CommentsDisabledError(
            "commentThreads: commentsDisabled (HTTP 403)", reason="commentsDisabled"
        )
    )

    message = await call_error(mcp, "youtube_get_comments", {"video_id": "v"})

    assert "comments are disabled on this video" in message
    assert "[commentsDisabled]" in message
    assert "httpx" not in message


# --------------------------------------------------------------------------- channel


async def test_get_channel_by_id() -> None:
    channel = models.channels_from_api(fixture("channels_by_id.json"))[0]
    mcp, stub = make_test_server(list_channel=channel)

    result = await call_tool(mcp, "youtube_get_channel", {"channel_id": CHANNEL_ID})

    assert result["channel"]["channel_id"] == CHANNEL_ID
    assert stub.kwargs() == {"channel_id": CHANNEL_ID, "handle": None}


async def test_get_channel_by_handle() -> None:
    channel = models.channels_from_api(fixture("channels_by_id.json"))[0]
    mcp, stub = make_test_server(list_channel=channel)

    await call_tool(mcp, "youtube_get_channel", {"handle": "@GoogleDevelopers"})

    assert stub.kwargs() == {"channel_id": None, "handle": "@GoogleDevelopers"}


@pytest.mark.parametrize("arguments", [{}, {"channel_id": "x", "handle": "y"}])
async def test_get_channel_requires_exactly_one_selector(arguments: dict) -> None:
    mcp, stub = make_test_server(list_channel=None)

    message = await call_error(mcp, "youtube_get_channel", arguments)

    assert "exactly one of channel_id or handle" in message
    assert stub.calls == []


async def test_get_channel_handle_miss_is_a_clean_error() -> None:
    """A handle miss is empty `items`, not a 404 — the tool must still fail cleanly."""
    mcp, _ = make_test_server(list_channel=None)

    message = await call_error(mcp, "youtube_get_channel", {"handle": "@nope"})

    assert "no channel matches handle '@nope'" in message
    assert "channel ID instead" in message
    assert "\n" not in message


async def test_get_channel_id_miss_names_the_id_not_a_handle() -> None:
    """The miss message must name the selector the caller actually passed."""
    mcp, _ = make_test_server(list_channel=None)

    message = await call_error(mcp, "youtube_get_channel", {"channel_id": CHANNEL_ID})

    assert f"no channel matches id '{CHANNEL_ID}'" in message
    assert "pass the handle instead" in message
    assert "\n" not in message


# ---------------------------------------------------------------- channel video walk


def playlist_page(items: int, next_page_token: str | None) -> models.PlaylistItemPage:
    raw = fixture("playlist_items.json")
    return models.PlaylistItemPage(
        next_page_token=next_page_token,
        items=[
            models.PlaylistItem.from_api(raw["items"][index % len(raw["items"])])
            for index in range(items)
        ],
    )


def channel_with_uploads(uploads: str | None = UPLOADS_ID) -> models.Channel:
    return models.Channel(
        channel_id=CHANNEL_ID, title="T", uploads_playlist_id=uploads
    )


async def test_list_channel_videos_resolves_uploads_playlist_then_walks() -> None:
    mcp, stub = make_test_server(
        list_channel=channel_with_uploads(),
        list_playlist_items=lambda playlist_id, **kw: playlist_page(2, None),
    )

    result = await call_tool(mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID})

    assert stub.calls[0][0] == "list_channel"
    assert tuple(stub.calls[0][1]["parts"]) == ("contentDetails",)
    assert stub.calls[1][1]["playlist_id"] == UPLOADS_ID
    assert result["uploads_playlist_id"] == UPLOADS_ID
    assert len(result["items"]) == 2


async def test_uploads_playlist_is_cached_so_the_channel_lookup_happens_once(
    tmp_path,
) -> None:
    """The channel→uploads mapping never changes, so it is fetched once (§14)."""
    mcp, stub = make_test_server(
        settings=Settings(_env_file=None),
        cache=make_cache(tmp_path),
        list_channel=channel_with_uploads(),
        list_playlist_items=lambda playlist_id, **kw: playlist_page(1, None),
    )

    await call_tool(mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID})
    await call_tool(mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID})

    channel_calls = [call for call in stub.calls if call[0] == "list_channel"]
    assert len(channel_calls) == 1
    assert len(stub.calls) == 3  # 1 channels.list + 2 playlistItems


async def test_stale_uploads_cache_does_not_reach_the_client(tmp_path) -> None:
    """The cache hits *before* the channels.list call, so the quota is never spent."""
    cache = make_cache(tmp_path)
    await cache.connect()
    await cache.set(f"uploads_playlist:{CHANNEL_ID}", UPLOADS_ID, ttl=0)
    mcp, stub = make_test_server(
        settings=Settings(_env_file=None),
        cache=cache,
        list_channel=channel_with_uploads(),
        list_playlist_items=lambda playlist_id, **kw: playlist_page(1, None),
    )

    result = await call_tool(mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID})

    assert [call[0] for call in stub.calls] == ["list_playlist_items"]
    assert result["uploads_playlist_id"] == UPLOADS_ID


async def test_channel_walk_breaks_at_max_results_without_a_second_page() -> None:
    """Asking for 1 video must cost one playlistItems page, not a full walk."""
    mcp, stub = make_test_server(
        list_channel=channel_with_uploads(),
        list_playlist_items=lambda playlist_id, **kw: playlist_page(50, "NEXT"),
    )

    result = await call_tool(
        mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID, "max_results": 1}
    )

    assert len([call for call in stub.calls if call[0] == "list_playlist_items"]) == 1
    assert len(result["items"]) == 1


async def test_channel_walk_stops_when_the_last_page_has_no_token() -> None:
    """An exhausted playlist ends the walk instead of looping on a token."""
    mcp, stub = make_test_server(
        list_channel=channel_with_uploads(),
        list_playlist_items=lambda playlist_id, **kw: playlist_page(3, None),
    )

    result = await call_tool(mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID})

    assert len([call for call in stub.calls if call[0] == "list_playlist_items"]) == 1
    assert len(result["items"]) == 3
    assert result["next_page_token"] is None


async def test_channel_walk_pages_when_more_are_needed() -> None:
    pages = {"first": playlist_page(2, "SECOND"), None: playlist_page(2, "SECOND")}
    seen: list[str | None] = []

    def make_page(playlist_id: str, **kwargs: Any) -> models.PlaylistItemPage:
        token = kwargs.get("page_token")
        seen.append(token)
        return pages["first" if token is None else None]

    mcp, _ = make_test_server(
        list_channel=channel_with_uploads(), list_playlist_items=make_page
    )

    result = await call_tool(
        mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID, "max_results": 3}
    )

    assert seen == [None, "SECOND"]
    assert len(result["items"]) == 3


async def test_channel_walk_request_size_is_capped_at_50_per_page() -> None:
    mcp, stub = make_test_server(
        list_channel=channel_with_uploads(),
        list_playlist_items=lambda playlist_id, **kw: playlist_page(1, None),
    )

    await call_tool(
        mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID, "max_results": 50}
    )

    assert stub.calls[1][1]["max_results"] == 50


async def test_list_channel_videos_reports_a_missing_channel_cleanly() -> None:
    mcp, _ = make_test_server(list_channel=None)

    message = await call_error(
        mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID}
    )

    assert "no channel found for id" in message
    assert "youtube_get_channel" in message


async def test_list_channel_videos_reports_a_channel_without_uploads_playlist() -> None:
    mcp, _ = make_test_server(list_channel=channel_with_uploads(uploads=None))

    message = await call_error(
        mcp, "youtube_list_channel_videos", {"channel_id": CHANNEL_ID}
    )

    assert "exposes no uploads playlist" in message


# ------------------------------------------------------------------------ categories


async def test_list_categories_passes_region_and_language() -> None:
    mcp, stub = make_test_server(
        list_video_categories=models.video_categories_from_api(fixture("video_categories.json"))
    )

    result = await call_tool(mcp, "youtube_list_categories", {"region_code": "PT"})

    assert stub.kwargs() == {"region_code": "PT", "hl": "en-US"}
    assert result["region_code"] == "PT"
    assert len(result["items"]) == 3


async def test_list_categories_region_defaults_to_none() -> None:
    mcp, stub = make_test_server(list_video_categories=[])

    result = await call_tool(mcp, "youtube_list_categories", {})

    assert stub.kwargs()["region_code"] is None
    assert result["region_code"] is None


# ----------------------------------------------------------------------- error paths


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (QuotaExceededError("quotaExceeded", reason="quotaExceeded"), "quota"),
        (RateLimitedError("rateLimitExceeded", reason="rateLimitExceeded"), "transient"),
        (NotFoundError("videoNotFound", reason="videoNotFound"), "does not exist"),
        (InvalidRequestError("invalidPart", reason="invalidPart"), "rejected the request"),
        (UpstreamError("boom"), "failed upstream"),
    ],
)
async def test_client_errors_map_to_model_facing_messages(
    error: Exception, expected: str
) -> None:
    mcp, _ = make_test_server(list_video_categories=error)

    message = await call_error(mcp, "youtube_list_categories", {})

    assert expected in message
    assert "\n" not in message


async def test_quota_error_says_do_not_retry_and_names_the_bucket() -> None:
    mcp, _ = make_test_server(
        search_videos=QuotaExceededError("quotaExceeded", reason="quotaExceeded")
    )

    message = await call_error(mcp, "youtube_search_videos", {"query": "x"})

    assert "daily quota" in message
    assert "midnight Pacific time" in message
    assert "do not retry today" in message
    assert "[quotaExceeded]" in message


async def test_rate_limit_error_says_retry_shortly() -> None:
    mcp, _ = make_test_server(
        search_videos=RateLimitedError("rateLimitExceeded", reason="rateLimitExceeded")
    )

    message = await call_error(mcp, "youtube_search_videos", {"query": "x"})

    assert "rate-limited" in message
    assert "retry shortly" in message


async def test_local_quota_exhaustion_names_our_own_budget() -> None:
    """`quota.QuotaExceeded` is our accounting, not Google's — say so (§5.4)."""
    mcp, _ = make_test_server(
        search_videos=QuotaExceeded(QuotaBucket.SEARCH, 100)
    )

    message = await call_error(mcp, "youtube_search_videos", {"query": "x"})

    assert "local budget for the search bucket is exhausted" in message
    assert "our own accounting, not Google's" in message
    assert "do not retry today" in message


async def test_error_message_never_leaks_the_library_message() -> None:
    """The API's own text can be a multi-line blob; only the stable reason token survives."""
    blob = "line one\nline two\nline three with https://example.invalid"
    mcp, _ = make_test_server(list_video_categories=UpstreamError(blob, reason="backendError"))

    message = await call_error(mcp, "youtube_list_categories", {})

    assert "line two" not in message
    assert "\n" not in message
    assert "[backendError]" in message


async def test_unexpected_exception_is_masked_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp, _ = make_test_server(list_video_categories=RuntimeError("internal detail"))

    with caplog.at_level("ERROR"):
        message = await call_error(mcp, "youtube_list_categories", {})

    assert "internal detail" not in message
    assert "unexpected internal error" in message
    assert "unexpected failure" in caplog.text


async def test_batch_get_stats_partial_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = models.BatchStatsResponse.from_api(
        fixture("batch_get_stats_partial_failure.json")
    )
    mcp, _ = make_test_server(batch_get_stats=response)

    with caplog.at_level("WARNING"):
        await call_tool(mcp, "youtube_get_video_stats", {"video_ids": ["a", "b", "c"]})

    assert "returned no stats" in caplog.text


# --------------------------------------------------------------- stats cache internals


def test_stats_ttl_is_short_enough_to_not_report_stale_counts() -> None:
    assert 0 < tools_data.STATS_TTL_SECONDS <= 600


def test_uploads_playlist_ttl_means_no_expiry() -> None:
    assert tools_data.UPLOADS_PLAYLIST_TTL_SECONDS == 0
