"""Thin async YouTube Data API v3 client (section 6 of HANDOFF.md).

One `httpx.AsyncClient` for the whole process, API-key auth as a `key=` query param,
consistent error translation (section 5.4) and a local per-bucket quota counter charged
*before* each request.

Two deliberate settings:

- `trust_env=False` on the owned client. `HTTP_PROXY`/`HTTPS_PROXY` are for the
  transcript scraper (section 9); letting them reroute Data API calls would send API-key traffic
  through a residential proxy for no benefit.
- `max_attempts=3` (default). Retries are only for `RateLimitedError` (403/429 rate-limit
  reasons) and 5xx, so a retry budget multiplied by an upstream retry loop cannot happen
  here — the Data API has no client-side retry of its own.

Local exhaustion of our own counter raises `quota.QuotaExceeded` before any socket is
opened; the API's own 403 `quotaExceeded` is translated to `QuotaExceededError`. They are
separate types on purpose: one is our bookkeeping, the other is Google saying no.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import anyio
import httpx

from youtube_mcp.config import Settings
from youtube_mcp.youtube import models
from youtube_mcp.youtube.quota import QuotaBucket, QuotaCounter

BASE_URL = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT_SECONDS = 10.0

#: `videos.list` / `videos:batchGetStats` accept at most 50 IDs per call.
MAX_IDS_PER_CALL = 50
MAX_SEARCH_RESULTS = 50
MIN_SEARCH_RESULTS = 0
MAX_COMMENT_RESULTS = 100
MIN_COMMENT_RESULTS = 1
MAX_PLAYLIST_RESULTS = 50

DEFAULT_MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0
#: Ceiling for a server-supplied `Retry-After`, so a hostile/huge value can't hang a tool.
MAX_RETRY_AFTER_SECONDS = 60.0

SearchOrder = Literal["date", "rating", "relevance", "title", "videoCount", "viewCount"]
SafeSearch = Literal["moderate", "none", "strict"]
CommentOrder = Literal["time", "relevance"]


# --------------------------------------------------------------------------- errors


class YouTubeApiError(Exception):
    """Base for every Data API failure. `code` is the stable handle for the tool layer."""

    code = "UPSTREAM_ERROR"
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        status_code: int | None = None,
        method: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.status_code = status_code
        self.method = method
        self.retryable = self.default_retryable if retryable is None else retryable
        #: Numeric `Retry-After` from the response, when the server sent one.
        self.retry_after: float | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, object]:
        """Flat, JSON-safe shape for tool error payloads."""
        return {
            "code": self.code,
            "retryable": self.retryable,
            "reason": self.reason,
            "message": self.message,
        }


class QuotaExceededError(YouTubeApiError):
    """Daily quota gone (`quotaExceeded`, `dailyLimitExceeded`, …). Never retry (section 5.4)."""

    code = "QUOTA_EXCEEDED"


class RateLimitedError(YouTubeApiError):
    """Transient rate limiting (`rateLimitExceeded`, `userRateLimitExceeded`) — back off."""

    code = "RATE_LIMITED"
    default_retryable = True


class NotFoundError(YouTubeApiError):
    """The requested resource does not exist (`videoNotFound`, `playlistNotFound`, …)."""

    code = "NOT_FOUND"


class CommentsDisabledError(YouTubeApiError):
    """403 `commentsDisabled` — comments off on the video. Expected, non-retriable."""

    code = "COMMENTS_DISABLED"


class InvalidRequestError(YouTubeApiError):
    """400-class bad parameters (`invalidPageToken`, `unknownPart`, `invalidPart`, …)."""

    code = "INVALID_REQUEST"


class UpstreamError(YouTubeApiError):
    """5xx, transport failure, unparseable body, or an unrecognised error envelope."""

    code = "UPSTREAM_ERROR"

    def __init__(self, message: str, *, retryable: bool = False, **kwargs: Any) -> None:
        super().__init__(message, retryable=retryable, **kwargs)


#: Reason → error class. Matched on `error.errors[].reason`, never on `domain` (section 5.4).
#: `limitExceeded`/`servingLimitExceeded` classify as quota (non-retriable): treating a
#: per-key daily ceiling as retriable only burns retries. `concurrentLimitExceeded` is the
#: opposite — it clears on its own, so it stays in the retryable rate-limit set.
_QUOTA_REASONS = frozenset(
    {"quotaExceeded", "dailyLimitExceeded", "limitExceeded", "servingLimitExceeded"}
)
_RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "concurrentLimitExceeded"}
)
_NOT_FOUND_REASONS = frozenset(
    {"channelNotFound", "playlistNotFound", "videoNotFound", "commentThreadNotFound"}
)
_INVALID_REQUEST_REASONS = frozenset(
    {
        "invalidPageToken",
        "unknownPart",
        "unexpectedPart",
        "invalidPart",
        "invalidSearchFilter",
        "invalidFilters",
        "invalidCriteria",
        "invalidParameter",
        "unexpectedParameter",
        "incompatibleParameters",
        "missingRequiredParameter",
        "invalidLocation",
        "invalidRegionCode",
        "invalidRelevanceLanguage",
        "invalid_channel_id",
        "unsupportedRegionCode",
        "unsupportedLanguageCode",
    }
)


def _first_reason(payload: Any) -> tuple[str | None, str | None]:
    """Pull `(reason, message)` out of an error envelope, defensively (section 5.4)."""
    if not isinstance(payload, Mapping):
        return None, None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None, None
    errors = error.get("errors")
    if isinstance(errors, Sequence) and errors:
        first = errors[0]
        if isinstance(first, Mapping):
            reason = first.get("reason")
            message = first.get("message") or error.get("message")
            return (str(reason) if reason else None, str(message) if message else None)
    message = error.get("message")
    return None, str(message) if message else None


def translate_error(response: httpx.Response, *, method: str) -> YouTubeApiError:
    """Map a non-2xx response onto the typed hierarchy.

    Reason first (`error.errors[].reason`), HTTP status second — the envelope shape varies
    by backend and is sometimes absent entirely.
    """
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None

    reason, api_message = _first_reason(payload)
    detail = api_message or response.text.strip()[:200] or "no response body"
    message = f"{method}: {reason or 'no reason'} (HTTP {response.status_code}): {detail}"
    kwargs: dict[str, Any] = {
        "reason": reason,
        "status_code": response.status_code,
        "method": method,
        "message": message,
    }

    if reason in _QUOTA_REASONS:
        return QuotaExceededError(**kwargs)
    if reason in _RATE_LIMIT_REASONS:
        return RateLimitedError(**kwargs)
    if reason == "commentsDisabled":
        return CommentsDisabledError(**kwargs)
    if reason in _NOT_FOUND_REASONS:
        return NotFoundError(**kwargs)
    if reason in _INVALID_REQUEST_REASONS:
        return InvalidRequestError(**kwargs)

    # Status fallback: the reason was missing or unknown.
    if response.status_code == 404:
        return NotFoundError(**kwargs)
    if response.status_code == 429:
        return RateLimitedError(**kwargs)
    if response.status_code == 400:
        return InvalidRequestError(**kwargs)
    if response.status_code >= 500:
        return UpstreamError(retryable=True, **kwargs)
    return UpstreamError(**kwargs)


# ------------------------------------------------------------------------ pagination


class Paged(Protocol):
    """Anything with a `next_page_token` we can walk."""

    next_page_token: str | None


async def iterate_pages[PageT: Paged](
    fetch_page: Callable[[str | None], Awaitable[PageT]],
) -> AsyncIterator[PageT]:
    """Walk a paged method: `fetch_page` takes a page token and returns one page.

    Lazy — breaking out of the loop stops issuing requests, so a tool that has enough
    results never pays for the next page.
    """
    token: str | None = None
    while True:
        page = await fetch_page(token)
        yield page
        token = page.next_page_token
        if not token:
            return


# ---------------------------------------------------------------------------- client


def _chunks(items: Sequence[str], size: int = MAX_IDS_PER_CALL) -> list[list[str]]:
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def _rfc3339(value: datetime | str) -> str:
    """Render a datetime (or pass through a string) as RFC 3339 UTC for the API."""
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _join_parts(parts: Iterable[str]) -> str:
    return ",".join(parts)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse the numeric `Retry-After` header (HTTP-date form falls back to backoff)."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")


class YouTubeClient:
    """Read-only Data API client. One instance per process; safe to share."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        quota: QuotaCounter | None = None,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """`http_client` is trusted as given: an injected client keeps *its own* `trust_env`
        setting, so a proxy the caller configured on it is used. Only an owned client is built
        with `trust_env=False` (see the module docstring) — inject an explicit transport if you
        want the Data API to bypass `HTTP_PROXY` on a client you construct yourself.
        """
        self._api_key = settings.require_api_key()
        self._quota = quota if quota is not None else QuotaCounter()
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)
        self._owns_client = http_client is None
        # trust_env=False: HTTP_PROXY/HTTPS_PROXY belong to the transcript scraper (section 9) and
        # must not reroute Data API traffic (Batch 1 review landmine).
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            trust_env=False,
        )

    @property
    def quota(self) -> QuotaCounter:
        """The injected counter — so the tool layer can report remaining budget."""
        return self._quota

    async def aclose(self) -> None:
        """Close the owned client; an injected one is the caller's to close."""
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> YouTubeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- transport ----------------------------------------------------------------

    async def _get(self, method: str, params: Mapping[str, Any]) -> Any:
        """Request `method`, retrying only on rate limits, 5xx and transport errors."""
        query = {key: value for key, value in params.items() if value is not None}
        query["key"] = self._api_key
        url = f"{BASE_URL}/{method}"

        for attempt in range(1, self._max_attempts + 1):
            outcome = await self._attempt(method, url, query)
            if not isinstance(outcome, YouTubeApiError):
                return outcome
            error: YouTubeApiError = outcome
            if error.retryable and attempt < self._max_attempts:
                await self._sleep(self._backoff_seconds(attempt, error.retry_after))
                continue
            raise error

    async def _attempt(
        self, method: str, url: str, query: Mapping[str, Any]
    ) -> Any | YouTubeApiError:
        """One request: the decoded JSON body, or the translated error to maybe retry."""
        try:
            response = await self._http.get(url, params=query)
        except httpx.HTTPError as exc:
            return UpstreamError(
                f"{method}: transport error: {exc}",
                reason="transportError",
                method=method,
                retryable=True,
            )
        if not response.is_success:
            error = translate_error(response, method=method)
            error.retry_after = _retry_after_seconds(response)
            return error
        try:
            return response.json()
        except ValueError:
            return UpstreamError(
                f"{method}: HTTP {response.status_code} body was not JSON",
                method=method,
                status_code=response.status_code,
            )

    @staticmethod
    def _backoff_seconds(attempt: int, retry_after: object = None) -> float:
        """`Retry-After` when the server sent one, else capped exponential backoff + jitter."""
        if isinstance(retry_after, (int, float)):
            return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        base = min(BASE_BACKOFF_SECONDS * 2.0 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        return min(base * random.uniform(0.5, 1.5), MAX_BACKOFF_SECONDS)

    # -- methods ------------------------------------------------------------------

    async def search_videos(
        self,
        query: str,
        *,
        max_results: int = 5,
        order: SearchOrder | None = None,
        published_after: datetime | str | None = None,
        published_before: datetime | str | None = None,
        video_category_id: str | None = None,
        region_code: str | None = None,
        safe_search: SafeSearch | None = None,
        page_token: str | None = None,
    ) -> models.SearchResults:
        """`search.list` — the scarce bucket (100 calls/day). `type=video` is always sent.

        `published_after` / `published_before` must be timezone-aware datetimes; a naive one
        is treated as UTC (`_rfc3339`), which silently shifts the window on any other host TZ.
        """
        if not query:
            raise InvalidRequestError("search_videos: query must not be empty")
        _check_range("max_results", max_results, MIN_SEARCH_RESULTS, MAX_SEARCH_RESULTS)
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "q": query,
            "maxResults": max_results,
            "order": order,
            "publishedAfter": _rfc3339(published_after) if published_after else None,
            "publishedBefore": _rfc3339(published_before) if published_before else None,
            "videoCategoryId": video_category_id,
            "regionCode": region_code,
            "safeSearch": safe_search,
            "pageToken": page_token,
        }
        self._quota.consume(QuotaBucket.SEARCH)
        return models.SearchResults.from_api(await self._get("search", params))

    async def batch_get_stats(self, video_ids: Sequence[str]) -> models.BatchStatsResponse:
        """`videos:batchGetStats` — its own bucket, and the cheap path for stats (section 5.3).

        IDs are chunked into groups of 50, one unit each, charged up front so a quota
        failure aborts before the first request. Partial failures arrive in
        `summary.failed_video_ids` and are not errors.
        """
        ids = list(video_ids)
        if not ids:
            raise ValueError("video_ids must not be empty")
        chunks = _chunks(ids)
        self._quota.consume(QuotaBucket.STATS, len(chunks))

        merged = models.BatchStatsResponse()
        for chunk in chunks:
            raw = await self._get(
                "videos:batchGetStats",
                {
                    "part": "snippet,statistics,contentDetails,id",
                    "id": ",".join(chunk),
                },
            )
            page = models.BatchStatsResponse.from_api(raw)
            merged.items.extend(page.items)
            merged.summary.requested_video_count += page.summary.requested_video_count
            merged.summary.succeeded_video_count += page.summary.succeeded_video_count
            merged.summary.failed_video_count += page.summary.failed_video_count
            merged.summary.failed_video_ids.extend(page.summary.failed_video_ids)
        return merged

    async def list_videos(
        self,
        video_ids: Sequence[str],
        parts: Sequence[str] = ("snippet", "statistics", "contentDetails"),
    ) -> list[models.Video]:
        """`videos.list` — shared pool. Use `batch_get_stats` when stats are all you need."""
        ids = list(video_ids)
        if not ids:
            raise ValueError("video_ids must not be empty")
        chunks = _chunks(ids)
        self._quota.consume(QuotaBucket.SHARED, len(chunks))

        videos: list[models.Video] = []
        for chunk in chunks:
            raw = await self._get("videos", {"part": _join_parts(parts), "id": ",".join(chunk)})
            videos.extend(models.videos_from_api(raw))
        return videos

    async def list_channel(
        self,
        *,
        channel_id: str | None = None,
        handle: str | None = None,
        parts: Sequence[str] = ("snippet", "contentDetails", "statistics"),
    ) -> models.Channel | None:
        """`channels.list` — shared pool.

        Exactly one of `channel_id` / `handle`. A handle miss is **not** a 404: the API
        returns empty `items`, so this returns `None` (research gotcha 14).
        """
        if (channel_id is None) == (handle is None):
            raise ValueError("pass exactly one of channel_id or handle")
        # Empty-but-present is a caller bug, not a valid selector — reject before spending quota.
        if channel_id is not None and not channel_id:
            raise InvalidRequestError("list_channel: channel_id must not be empty")
        if handle is not None and not handle.lstrip("@"):
            raise InvalidRequestError("list_channel: handle must not be empty")
        params: dict[str, Any] = {"part": _join_parts(parts)}
        if channel_id is not None:
            params["id"] = channel_id
        else:
            assert handle is not None  # the exactly-one guard above
            params["forHandle"] = handle.lstrip("@")

        self._quota.consume(QuotaBucket.SHARED)
        channels = models.channels_from_api(await self._get("channels", params))
        return channels[0] if channels else None

    async def list_playlist_items(
        self,
        playlist_id: str,
        *,
        parts: Sequence[str] = ("snippet",),
        max_results: int = 5,
        page_token: str | None = None,
    ) -> models.PlaylistItemPage:
        """`playlistItems.list` — shared pool, 1 unit per page. No `contentDetails` part."""
        _check_range("max_results", max_results, 0, MAX_PLAYLIST_RESULTS)
        self._quota.consume(QuotaBucket.SHARED)
        raw = await self._get(
            "playlistItems",
            {
                "part": _join_parts(parts),
                "playlistId": playlist_id,
                "maxResults": max_results,
                "pageToken": page_token,
            },
        )
        return models.PlaylistItemPage.from_api(raw)

    async def list_comment_threads(
        self,
        video_id: str,
        *,
        max_results: int = 20,
        order: CommentOrder | None = "time",
        page_token: str | None = None,
        text_format: Literal["plainText", "html"] = "plainText",
    ) -> models.CommentThreadPage:
        """`commentThreads.list` — shared pool. maxResults is 1–100 (not 0–50)."""
        _check_range("max_results", max_results, MIN_COMMENT_RESULTS, MAX_COMMENT_RESULTS)
        self._quota.consume(QuotaBucket.SHARED)
        raw = await self._get(
            "commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": max_results,
                "order": order,
                "pageToken": page_token,
                "textFormat": text_format,
            },
        )
        return models.CommentThreadPage.from_api(raw)

    async def list_video_categories(
        self,
        *,
        region_code: str | None = None,
        hl: str | None = None,
    ) -> list[models.VideoCategory]:
        """`videoCategories.list` — shared pool. Not paged: one response has everything."""
        self._quota.consume(QuotaBucket.SHARED)
        raw = await self._get(
            "videoCategories",
            {"part": "snippet", "regionCode": region_code, "hl": hl},
        )
        return models.video_categories_from_api(raw)
