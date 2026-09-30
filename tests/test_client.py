"""Client tests: request shape, quota buckets, error translation, retry policy.

All offline — a `httpx.MockTransport` handler drives the real client code, so the URL,
query params and status translation are exercised for real (no respx, no network).
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from conftest import FIXTURES, fixture
from youtube_mcp.config import Settings
from youtube_mcp.youtube import client as client_module
from youtube_mcp.youtube.client import (
    MAX_IDS_PER_CALL,
    CommentsDisabledError,
    InvalidRequestError,
    NotFoundError,
    QuotaExceededError,
    RateLimitedError,
    UpstreamError,
    YouTubeClient,
    iterate_pages,
    translate_error,
)
from youtube_mcp.youtube.models import (
    BatchStatsResponse,
    CommentThreadPage,
    PlaylistItemPage,
    SearchResults,
    Channel,
)
from youtube_mcp.youtube.quota import QuotaBucket, QuotaCounter, QuotaExceeded



class Recorder:
    """MockTransport handler: records requests, replays a scripted response list."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses) or [httpx.Response(200, json={})]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]

    @property
    def attempts(self) -> int:
        return len(self.requests)

    def params(self, index: int = 0) -> dict[str, str]:
        return dict(self.requests[index].url.params)

    def path(self, index: int = 0) -> str:
        return self.requests[index].url.path


def make_client(
    handler: Recorder,
    *,
    quota: QuotaCounter | None = None,
    sleeps: list[float] | None = None,
    max_attempts: int = 3,
) -> YouTubeClient:
    recorded = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)

    return YouTubeClient(
        Settings(youtube_api_key="test-key"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        quota=quota,
        sleep=sleep,
        max_attempts=max_attempts,
    )


def ok(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


def err(payload: object, status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def quota_counter(spent: QuotaBucket | None = None, units: int = 0) -> QuotaCounter:
    counter = QuotaCounter()
    if spent is not None:
        counter.consume(spent, units)
    return counter


# ------------------------------------------------------------------ request shaping


async def test_search_videos_sends_documented_params_and_key():
    handler = Recorder(ok(fixture("search_list.json")))
    client = make_client(handler)

    results = await client.search_videos(
        "rick astley",
        max_results=20,
        order="viewCount",
        published_after=datetime(2024, 1, 1, tzinfo=timezone.utc),
        published_before=datetime(2024, 6, 1, 12, 30, tzinfo=timezone.utc),
        video_category_id="10",
        region_code="PT",
        safe_search="strict",
        page_token="CAUQAA",
    )

    assert isinstance(results, SearchResults)
    assert handler.path() == "/youtube/v3/search"
    params = handler.params()
    assert params["part"] == "snippet"
    assert params["type"] == "video"
    assert params["q"] == "rick astley"
    assert params["maxResults"] == "20"
    assert params["order"] == "viewCount"
    assert params["publishedAfter"] == "2024-01-01T00:00:00Z"
    assert params["publishedBefore"] == "2024-06-01T12:30:00Z"
    assert params["videoCategoryId"] == "10"
    assert params["regionCode"] == "PT"
    assert params["safeSearch"] == "strict"
    assert params["pageToken"] == "CAUQAA"
    assert params["key"] == "test-key"


async def test_search_videos_omits_unset_optional_params():
    handler = Recorder(ok({"items": []}))
    client = make_client(handler)

    await client.search_videos("q")

    params = handler.params()
    assert params["maxResults"] == "5"
    for absent in ("order", "publishedAfter", "publishedBefore", "videoCategoryId", "pageToken"):
        assert absent not in params


async def test_search_videos_accepts_naive_and_string_datetimes():
    handler = Recorder(ok({"items": []}))
    client = make_client(handler)

    await client.search_videos("q", published_after=datetime(2024, 1, 1))
    await client.search_videos("q", published_after="2024-01-01T00:00:00Z")

    assert handler.params(0)["publishedAfter"] == "2024-01-01T00:00:00Z"
    assert handler.params(1)["publishedAfter"] == "2024-01-01T00:00:00Z"


@pytest.mark.parametrize("max_results", [-1, 51])
async def test_search_videos_rejects_out_of_range_max_results(max_results):
    handler = Recorder(ok({"items": []}))
    client = make_client(handler)

    with pytest.raises(ValueError, match="max_results must be between 0 and 50"):
        await client.search_videos("q", max_results=max_results)

    assert handler.attempts == 0


async def test_search_videos_rejects_empty_query_before_spending_quota():
    handler = Recorder(ok({"items": []}))
    client = make_client(handler)

    with pytest.raises(InvalidRequestError, match="query must not be empty"):
        await client.search_videos("")

    assert handler.attempts == 0
    assert client.quota.remaining(QuotaBucket.SEARCH) == 100


async def test_batch_get_stats_parts_and_ids():
    handler = Recorder(ok(fixture("batch_get_stats.json")))
    client = make_client(handler)

    await client.batch_get_stats(["dQw4w9WgXcQ", "aqz-KE-bpKQ"])

    assert handler.path() == "/youtube/v3/videos:batchGetStats"
    params = handler.params()
    assert params["part"] == "snippet,statistics,contentDetails,id"
    assert params["id"] == "dQw4w9WgXcQ,aqz-KE-bpKQ"


async def test_batch_get_stats_chunks_at_50_ids_and_charges_per_chunk():
    handler = Recorder(ok(fixture("batch_get_stats.json")))
    quota = quota_counter()
    client = make_client(handler, quota=quota)
    ids = [f"video{i:03d}" for i in range(120)]

    await client.batch_get_stats(ids)

    assert handler.attempts == 3
    assert [len(handler.params(i)["id"].split(",")) for i in range(3)] == [50, 50, 20]
    assert quota.remaining(QuotaBucket.STATS) == 10_000 - 3
    assert quota.remaining(QuotaBucket.SHARED) == 10_000


async def test_batch_get_stats_merges_chunks_and_surfaces_partial_failure():
    handler = Recorder(ok(fixture("batch_get_stats_partial_failure.json")))
    client = make_client(handler)

    response = await client.batch_get_stats(["dQw4w9WgXcQ", "aqz-KE-bpKQ", "zzz_missing_id"])

    assert isinstance(response, BatchStatsResponse)
    assert len(response.items) == 2
    assert response.summary.requested_video_count == 3
    assert response.summary.succeeded_video_count == 2
    assert response.summary.failed_video_count == 1
    assert response.summary.failed_video_ids == ["zzz_missing_id"]


async def test_batch_get_stats_rejects_empty_ids():
    handler = Recorder(ok({}))
    client = make_client(handler)

    with pytest.raises(ValueError, match="video_ids must not be empty"):
        await client.batch_get_stats([])

    assert handler.attempts == 0


async def test_list_videos_rejects_empty_ids():
    handler = Recorder(ok({}))
    client = make_client(handler)

    with pytest.raises(ValueError, match="video_ids must not be empty"):
        await client.list_videos([])

    assert handler.attempts == 0


async def test_list_videos_sends_parts_and_chunks():
    handler = Recorder(ok({"items": []}))
    quota = quota_counter()
    client = make_client(handler, quota=quota)

    await client.list_videos([f"v{i}" for i in range(51)], parts=("snippet", "statistics"))

    assert handler.attempts == 2
    assert handler.path(0) == "/youtube/v3/videos"
    assert handler.params(0)["part"] == "snippet,statistics"
    assert len(handler.params(0)["id"].split(",")) == 50
    assert len(handler.params(1)["id"].split(",")) == 1
    assert quota.remaining(QuotaBucket.SHARED) == 10_000 - 2


async def test_list_channel_by_id_uses_id_param():
    handler = Recorder(ok(fixture("channels_by_id.json")))
    client = make_client(handler)

    channel = await client.list_channel(channel_id="UCBR8-60-B28hp2BmDPdntcQ")

    assert isinstance(channel, Channel)
    assert channel.uploads_playlist_id == "UUx7I4zH7kz2Q4tU9mB0pX8aLw1Q"
    assert handler.path() == "/youtube/v3/channels"
    params = handler.params()
    assert params["id"] == "UCBR8-60-B28hp2BmDPdntcQ"
    assert params["part"] == "snippet,contentDetails,statistics"
    assert "forHandle" not in params


async def test_list_channel_by_handle_sends_forHandle_and_strips_at_sign():
    handler = Recorder(ok(fixture("channels_by_id.json")))
    client = make_client(handler)

    await client.list_channel(handle="@GoogleDevelopers")

    params = handler.params()
    assert params["forHandle"] == "GoogleDevelopers"
    assert "id" not in params


async def test_list_channel_returns_none_when_handle_misses():
    """A handle miss is an empty items list, never a 404 (research gotcha 14)."""
    handler = Recorder(ok(fixture("channels_by_handle_miss.json")))
    client = make_client(handler)

    assert await client.list_channel(handle="@does-not-exist") is None
    assert handler.attempts == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"channel_id": "UC1", "handle": "@both"}, {}],
)
async def test_list_channel_requires_exactly_one_selector(kwargs):
    handler = Recorder(ok({}))
    client = make_client(handler)

    with pytest.raises(ValueError, match="exactly one of channel_id or handle"):
        await client.list_channel(**kwargs)

    assert handler.attempts == 0


@pytest.mark.parametrize(
    "kwargs",
    [{"channel_id": ""}, {"handle": ""}, {"handle": "@"}],
)
async def test_list_channel_rejects_empty_selector_before_spending_quota(kwargs):
    """Empty-but-present is a caller bug, and the API would only charge us to say so."""
    handler = Recorder(ok({}))
    client = make_client(handler)

    with pytest.raises(InvalidRequestError, match="must not be empty"):
        await client.list_channel(**kwargs)

    assert handler.attempts == 0
    assert client.quota.remaining(QuotaBucket.SHARED) == 10_000


async def test_list_playlist_items_params():
    handler = Recorder(ok(fixture("playlist_items.json")))
    client = make_client(handler)

    page = await client.list_playlist_items(
        "UUx7I4zH7kz2Q4tU9mB0pX8aLw1Q", max_results=50, page_token="CAEQAA"
    )

    assert isinstance(page, PlaylistItemPage)
    assert handler.path() == "/youtube/v3/playlistItems"
    params = handler.params()
    assert params["part"] == "snippet"
    assert params["playlistId"] == "UUx7I4zH7kz2Q4tU9mB0pX8aLw1Q"
    assert params["maxResults"] == "50"
    assert params["pageToken"] == "CAEQAA"
    assert page.next_page_token == "CAEQAA"


async def test_list_comment_threads_params():
    handler = Recorder(ok(fixture("comment_threads.json")))
    client = make_client(handler)

    page = await client.list_comment_threads(
        "dQw4w9WgXcQ", max_results=100, order="relevance", page_token="Qg8QAA"
    )

    assert isinstance(page, CommentThreadPage)
    assert handler.path() == "/youtube/v3/commentThreads"
    params = handler.params()
    assert params["part"] == "snippet"
    assert params["videoId"] == "dQw4w9WgXcQ"
    assert params["maxResults"] == "100"
    assert params["order"] == "relevance"
    assert params["textFormat"] == "plainText"
    assert len(page.items) == 2


@pytest.mark.parametrize("max_results", [0, 101])
async def test_list_comment_threads_rejects_out_of_range_max_results(max_results):
    """commentThreads is 1–100, unlike search's 0–50 (research gotcha 7)."""
    handler = Recorder(ok({}))
    client = make_client(handler)

    with pytest.raises(ValueError, match="max_results must be between 1 and 100"):
        await client.list_comment_threads("v", max_results=max_results)

    assert handler.attempts == 0


async def test_list_video_categories_params():
    handler = Recorder(ok(fixture("video_categories.json")))
    client = make_client(handler)

    categories = await client.list_video_categories(region_code="PT", hl="pt-PT")

    assert handler.path() == "/youtube/v3/videoCategories"
    params = handler.params()
    assert params["part"] == "snippet"
    assert params["regionCode"] == "PT"
    assert params["hl"] == "pt-PT"
    assert [category.title for category in categories] == ["Film & Animation", "Music", "Trailers"]


# ------------------------------------------------------------------- quota buckets


async def test_search_consumes_search_bucket_not_shared():
    handler = Recorder(ok({"items": []}))
    quota = quota_counter()
    client = make_client(handler, quota=quota)

    await client.search_videos("q")

    assert quota.remaining(QuotaBucket.SEARCH) == 99
    assert quota.remaining(QuotaBucket.SHARED) == 10_000
    assert quota.remaining(QuotaBucket.STATS) == 10_000


async def test_stats_methods_consume_their_own_buckets():
    """`batchGetStats` must not touch the shared pool (§5.3 quota-efficiency requirement)."""
    handler = Recorder(ok(fixture("batch_get_stats.json")))
    quota = quota_counter()
    client = make_client(handler, quota=quota)

    await client.batch_get_stats(["dQw4w9WgXcQ"])

    assert quota.remaining(QuotaBucket.STATS) == 9_999
    assert quota.remaining(QuotaBucket.SHARED) == 10_000


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.search_videos("q"),
        lambda client: client.batch_get_stats(["v"]),
        lambda client: client.list_videos(["v"]),
        lambda client: client.list_channel(channel_id="UC1"),
        lambda client: client.list_playlist_items("PL1"),
        lambda client: client.list_comment_threads("v"),
        lambda client: client.list_video_categories(),
    ],
)
async def test_every_method_consumes_exactly_one_unit(call):
    handler = Recorder(ok({"items": []}))
    quota = quota_counter()
    client = make_client(handler, quota=quota)
    spent_before = {
        bucket: quota.remaining(bucket)
        for bucket in (QuotaBucket.SEARCH, QuotaBucket.STATS, QuotaBucket.SHARED)
    }

    await call(client)

    spent_after = {
        bucket: quota.remaining(bucket)
        for bucket in (QuotaBucket.SEARCH, QuotaBucket.STATS, QuotaBucket.SHARED)
    }
    assert sum(spent_before[b] - spent_after[b] for b in spent_before) == 1


async def test_local_exhaustion_aborts_before_any_request():
    handler = Recorder(ok({"items": []}))
    quota = quota_counter(QuotaBucket.SEARCH, 100)
    client = make_client(handler, quota=quota)

    with pytest.raises(QuotaExceeded) as excinfo:
        await client.search_videos("q")

    assert excinfo.value.bucket is QuotaBucket.SEARCH
    assert handler.attempts == 0


async def test_quota_counter_uses_injected_clock():
    handler = Recorder(ok({"items": []}))
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    quota = QuotaCounter(clock=lambda: now.timestamp())
    client = make_client(handler, quota=quota)

    await client.search_videos("q")

    assert client.quota.remaining(QuotaBucket.SEARCH) == 99


# ----------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    ("fixture_name", "status", "expected"),
    [
        ("quota_exceeded_403.json", 403, QuotaExceededError),
        ("daily_limit_exceeded_403.json", 403, QuotaExceededError),
        ("rate_limit_exceeded_429.json", 429, RateLimitedError),
        ("user_rate_limit_exceeded_403.json", 403, RateLimitedError),
        ("comments_disabled_403.json", 403, CommentsDisabledError),
        ("channel_not_found_404.json", 404, NotFoundError),
        ("playlist_not_found_404.json", 404, NotFoundError),
        ("video_not_found_404.json", 404, NotFoundError),
        ("invalid_page_token_400.json", 400, InvalidRequestError),
        ("unknown_part_400.json", 400, InvalidRequestError),
        ("unexpected_part_400.json", 400, InvalidRequestError),
        ("invalid_search_filter_400.json", 400, InvalidRequestError),
        ("invalid_criteria_400.json", 400, InvalidRequestError),
        ("unsupported_region_code_400.json", 400, InvalidRequestError),
        ("missing_required_parameter_400.json", 400, InvalidRequestError),
        ("backend_error_503.json", 503, UpstreamError),
        ("forbidden_no_reason_403.json", 403, UpstreamError),
    ],
)
async def test_error_translation_by_reason(fixture_name, status, expected):
    handler = Recorder(err(fixture(fixture_name), status))
    client = make_client(handler, max_attempts=1)

    with pytest.raises(expected):
        await client.list_video_categories()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(400, InvalidRequestError), (404, NotFoundError), (429, RateLimitedError), (500, UpstreamError)],
)
def test_translate_error_falls_back_to_http_status(status, expected):
    """No `errors[]` array at all — the status decides (research A2.5 envelope variance)."""
    response = httpx.Response(status, json={"error": {"code": status, "message": "nope"}})

    error = translate_error(response, method="videos")

    assert isinstance(error, expected)
    assert error.status_code == status
    assert error.method == "videos"
    assert "videos" in str(error)


def test_translate_error_handles_non_json_and_empty_bodies():
    html = httpx.Response(500, text=(FIXTURES / "not_json_500.html").read_text())
    empty = httpx.Response(403)

    assert isinstance(translate_error(html, method="videos"), UpstreamError)
    forbidden = translate_error(empty, method="videos")
    assert isinstance(forbidden, UpstreamError)
    assert forbidden.retryable is False


def test_translate_error_handles_malformed_error_envelope():
    """`error` present but not an object — fall back to the HTTP status (research A2.5)."""
    response = httpx.Response(404, json={"error": "something odd"})

    error = translate_error(response, method="videos")

    assert isinstance(error, NotFoundError)
    assert error.reason is None


def test_translate_error_ignores_http_date_retry_after():
    response = httpx.Response(
        429, json=fixture("rate_limit_exceeded_429.json"), headers={"Retry-After": "soon"}
    )

    error = translate_error(response, method="videos")

    assert error.retry_after is None
    assert YouTubeClient._backoff_seconds(1, None) > 0


async def test_http_date_retry_after_falls_back_to_computed_backoff():
    """`Retry-After` is only honoured in its numeric (delta-seconds) form."""
    handler = Recorder(
        httpx.Response(
            429,
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
            json=fixture("rate_limit_exceeded_429.json"),
        ),
        ok({"items": []}),
    )
    sleeps: list[float] = []
    client = make_client(handler, sleeps=sleeps)

    await client.list_video_categories()

    assert handler.attempts == 2
    assert 0 < sleeps[0] <= client_module.MAX_BACKOFF_SECONDS


def test_translate_error_preserves_reason_and_message():
    response = err(fixture("quota_exceeded_403.json"), 403)

    error = translate_error(response, method="search")

    assert error.reason == "quotaExceeded"
    assert error.code == "QUOTA_EXCEEDED"
    assert error.retryable is False
    assert "quota" in error.to_dict()["message"]  # type: ignore[operator]


async def test_not_json_success_body_is_upstream_error():
    handler = Recorder(httpx.Response(200, text="<html>not json</html>"))
    client = make_client(handler, max_attempts=1)

    with pytest.raises(UpstreamError, match="was not JSON"):
        await client.list_video_categories()


async def test_transport_failure_becomes_retryable_upstream_error():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    async def noop_sleep(seconds: float) -> None:
        return None

    client = YouTubeClient(
        Settings(youtube_api_key="test-key"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=noop_sleep,
        max_attempts=2,
    )

    with pytest.raises(UpstreamError) as excinfo:
        await client.list_video_categories()

    assert excinfo.value.retryable is True
    assert calls["count"] == 2


# ------------------------------------------------------------------------ retrying


async def test_429_then_200_retries_once_and_succeeds():
    handler = Recorder(
        httpx.Response(429, headers={"Retry-After": "7"}, json=fixture("rate_limit_exceeded_429.json")),
        ok(fixture("video_categories.json")),
    )
    sleeps: list[float] = []
    client = make_client(handler, sleeps=sleeps)

    categories = await client.list_video_categories()

    assert handler.attempts == 2
    assert sleeps == [7.0]  # Retry-After honoured verbatim
    assert len(categories) == 3


async def test_retry_after_is_capped():
    handler = Recorder(
        httpx.Response(429, headers={"Retry-After": "9999"}, json=fixture("rate_limit_exceeded_429.json")),
        ok({"items": []}),
    )
    sleeps: list[float] = []
    client = make_client(handler, sleeps=sleeps)

    await client.list_video_categories()

    assert sleeps == [client_module.MAX_RETRY_AFTER_SECONDS]


async def test_rate_limit_403_without_retry_after_backs_off_exponentially():
    handler = Recorder(
        httpx.Response(403, json=fixture("user_rate_limit_exceeded_403.json")),
        httpx.Response(403, json=fixture("user_rate_limit_exceeded_403.json")),
        ok({"items": []}),
    )
    sleeps: list[float] = []
    client = make_client(handler, sleeps=sleeps)

    await client.list_video_categories()

    assert handler.attempts == 3
    assert len(sleeps) == 2
    assert 0 < sleeps[0] <= client_module.MAX_BACKOFF_SECONDS
    assert 0 < sleeps[1] <= client_module.MAX_BACKOFF_SECONDS


async def test_quota_exceeded_is_never_retried():
    handler = Recorder(err(fixture("quota_exceeded_403.json"), 403))
    sleeps: list[float] = []
    client = make_client(handler, sleeps=sleeps)

    with pytest.raises(QuotaExceededError):
        await client.search_videos("q")

    assert handler.attempts == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("fixture_name", "status", "expected"),
    [
        ("comments_disabled_403.json", 403, CommentsDisabledError),
        ("invalid_page_token_400.json", 400, InvalidRequestError),
        ("video_not_found_404.json", 404, NotFoundError),
    ],
)
async def test_client_errors_are_not_retried(fixture_name, status, expected):
    handler = Recorder(err(fixture(fixture_name), status))
    client = make_client(handler)

    with pytest.raises(expected):
        await client.list_comment_threads("v")

    assert handler.attempts == 1


async def test_5xx_is_retried_then_gives_up_after_max_attempts():
    handler = Recorder(err(fixture("backend_error_503.json"), 503))
    sleeps: list[float] = []
    client = make_client(handler, sleeps=sleeps, max_attempts=3)

    with pytest.raises(UpstreamError) as excinfo:
        await client.list_video_categories()

    assert excinfo.value.retryable is True
    assert handler.attempts == 3
    assert len(sleeps) == 2


async def test_retry_budget_is_small_and_explicit():
    """Capped attempts — the Data API has no client-side retry loop to multiply (§B4/B6)."""
    assert client_module.DEFAULT_MAX_ATTEMPTS == 3
    handler = Recorder(err(fixture("backend_error_503.json"), 503))
    client = make_client(handler)

    with pytest.raises(UpstreamError):
        await client.list_video_categories()

    assert handler.attempts == client_module.DEFAULT_MAX_ATTEMPTS


# ------------------------------------------------------------- lifecycle / paging


def test_owned_client_disables_env_proxy_trust():
    """`HTTP_PROXY` is for the transcript scraper, not Data API calls (Batch 1 landmine)."""
    client = YouTubeClient(Settings(youtube_api_key="test-key"))

    assert client._http.trust_env is False
    assert str(client._http.base_url) == ""


async def test_aclose_leaves_injected_client_open():
    handler = Recorder(ok({"items": []}))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = YouTubeClient(Settings(youtube_api_key="test-key"), http_client=http_client)

    await client.aclose()

    assert not http_client.is_closed
    await http_client.aclose()


async def test_aclose_closes_owned_client():
    client = YouTubeClient(Settings(youtube_api_key="test-key"))

    await client.aclose()

    assert client._http.is_closed


async def test_context_manager_closes_owned_client():
    async with YouTubeClient(Settings(youtube_api_key="test-key")) as client:
        assert not client._http.is_closed

    assert client._http.is_closed


async def test_missing_api_key_fails_fast():
    from youtube_mcp.config import MissingApiKeyError

    # `_env_file=None`: a developer's own .env must not satisfy this test spuriously.
    with pytest.raises(MissingApiKeyError):
        YouTubeClient(Settings(_env_file=None))


async def test_iterate_pages_walks_tokens_and_stops():
    handler = Recorder(
        ok(fixture("playlist_items.json")),
        ok({"items": [], "nextPageToken": None}),
    )
    client = make_client(handler)
    seen: list[str | None] = []

    async for page in iterate_pages(lambda token: client.list_playlist_items("PL1", page_token=token)):
        seen.append(page.next_page_token)

    assert seen == ["CAEQAA", None]
    assert handler.attempts == 2
    assert client.quota.remaining(QuotaBucket.SHARED) == 9_998


async def test_iterate_pages_is_lazy_so_tools_can_stop_early():
    handler = Recorder(ok(fixture("playlist_items.json")))
    client = make_client(handler)
    pages = 0

    async for _ in iterate_pages(lambda token: client.list_playlist_items("PL1", page_token=token)):
        pages += 1
        break

    assert pages == 1
    assert handler.attempts == 1  # the second page was never fetched
