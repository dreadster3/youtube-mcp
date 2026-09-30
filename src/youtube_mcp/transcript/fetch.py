"""Transcript fetching: async wrapper over `youtube-transcript-api` 1.2.4 (§7, §11).

The library is synchronous, builds its own `requests.Session` per instance (so an instance is
not thread-safe), passes no HTTP timeout, and reports failures through a deep exception
hierarchy. This module owns all three: one fresh API instance per offloaded call, an explicit
deadline around the await, and translation of every library failure into the closed taxonomy
defined in `errors.py`.

Nothing here formats or truncates transcripts — tools (Batch 4) slice the full `Transcript`.
"""

import logging
import re
from collections.abc import Callable, Sequence
from typing import TypeVar

import anyio
import requests
from pydantic import BaseModel, Field
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    FailedToCreateConsentCookie,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)
from youtube_transcript_api.proxies import (
    GenericProxyConfig,
    InvalidProxyConfig,
    ProxyConfig,
    WebshareProxyConfig,
)

from youtube_mcp.cache import Cache, namespaced
from youtube_mcp.config import Settings, get_settings
from youtube_mcp.transcript.errors import TranscriptError, TranscriptErrorCode

logger = logging.getLogger(__name__)

#: Deadline for one offloaded library call. The library has no timeout of its own, so without
#: this an unresponsive YouTube hangs the tool call forever (§7.1).
TRANSCRIPT_TIMEOUT_SECONDS = 30.0

#: Bias the Webshare rotation towards the operator's region to limit added latency (§9).
WEBSHARE_FILTER_IP_LOCATIONS = ["pt", "es"]

#: YouTube video IDs are 11 characters of [A-Za-z0-9_-].
_VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")

#: Cap on the rejected input echoed back in an error message, so a 10 kB paste cannot break
#: the one-line message discipline (§11).
_REJECTED_ID_MAX_CHARS = 15

T = TypeVar("T")


class TranscriptSnippet(BaseModel):
    """One caption segment. `duration` is on-screen time, not speech length, so segments overlap."""

    text: str
    start: float
    duration: float


class Transcript(BaseModel):
    """A fetched transcript, entire. Tools slice it for RESPONSE_LIMIT/cursor handling."""

    video_id: str
    language: str
    language_code: str
    is_generated: bool
    snippets: list[TranscriptSnippet]


class TranscriptTrack(BaseModel):
    """One available caption track on a video."""

    language: str
    language_code: str
    is_generated: bool
    is_translatable: bool
    translatable_to: list[str] = Field(default_factory=list)


class TranscriptTrackList(BaseModel):
    """Every caption track available for a video."""

    video_id: str
    tracks: list[TranscriptTrack]


def build_proxy_config(settings: Settings) -> ProxyConfig | None:
    """Map proxy settings onto a library proxy config, or `None` for a direct connection (§9).

    Webshare wins when both its credentials are set; otherwise a generic proxy is used if either
    proxy URL is set.
    """
    if bool(settings.webshare_proxy_username) != bool(settings.webshare_proxy_password):
        logger.warning(
            "webshare proxy needs both the username and the password; ignoring the partial pair"
        )
    if settings.webshare_proxy_username and settings.webshare_proxy_password:
        return WebshareProxyConfig(
            proxy_username=settings.webshare_proxy_username,
            proxy_password=settings.webshare_proxy_password.get_secret_value(),
            filter_ip_locations=WEBSHARE_FILTER_IP_LOCATIONS,
        )
    if settings.http_proxy or settings.https_proxy:
        return GenericProxyConfig(http_url=settings.http_proxy, https_url=settings.https_proxy)
    return None


def _error(code: TranscriptErrorCode, video_id: str, cause: str) -> TranscriptError:
    """Build a short, model-facing error: one line, video ID, cause.

    `str(error)` prefixes the taxonomy code. The library's own messages are multi-line blobs
    ending in a GitHub-issue referral, so they are never surfaced (§11, research §B4). The
    echoed input is truncated: rejected input can be arbitrarily long (a pasted URL, a blob).
    """
    return TranscriptError(code, f"video {_display_id(video_id)}: {cause}")


def _display_id(video_id: str) -> str:
    """Shorten a caller-supplied ID for an error message, keeping it one line."""
    if len(video_id) <= _REJECTED_ID_MAX_CHARS:
        return video_id
    return video_id[:_REJECTED_ID_MAX_CHARS] + "…"


#: Library exception -> taxonomy. First match wins; anything absent falls through to
#: TRANSCRIPT_UPSTREAM_ERROR. `IpBlocked` also covers HTTP 429, which `_raise_http_errors`
#: folds into it directly (research §B4) — that is the one genuinely retryable block.
_ERROR_TABLE: tuple[tuple[type[Exception], TranscriptErrorCode, str], ...] = (
    (TranscriptsDisabled, TranscriptErrorCode.DISABLED, "captions are disabled for this video"),
    (
        NoTranscriptFound,
        TranscriptErrorCode.NOT_FOUND,
        "no caption track exists in the requested languages",
    ),
    (
        IpBlocked,
        TranscriptErrorCode.IP_BLOCKED,
        "YouTube blocked this IP — transient, retry later or configure a proxy",
    ),
    (
        RequestBlocked,
        TranscriptErrorCode.IP_BLOCKED,
        "YouTube blocked this IP — transient, retry later or configure a proxy",
    ),
    (
        AgeRestricted,
        TranscriptErrorCode.AGE_RESTRICTED,
        "age-restricted video, and cookie auth is broken upstream in youtube-transcript-api 1.2.4",
    ),
    (
        InvalidVideoId,
        TranscriptErrorCode.INVALID_REQUEST,
        "not a valid video ID — pass the 11-character ID, not a URL",
    ),
    (
        InvalidProxyConfig,
        TranscriptErrorCode.INVALID_REQUEST,
        "the configured proxy is invalid",
    ),
    (
        PoTokenRequired,
        TranscriptErrorCode.UPSTREAM_ERROR,
        "YouTube requires a PO token for this video; no workaround exists yet",
    ),
    (
        YouTubeDataUnparsable,
        TranscriptErrorCode.UPSTREAM_ERROR,
        "YouTube returned data the library could not parse",
    ),
    (
        VideoUnplayable,
        TranscriptErrorCode.UPSTREAM_ERROR,
        "the video is unplayable",
    ),
    (
        VideoUnavailable,
        TranscriptErrorCode.UPSTREAM_ERROR,
        "the video is unavailable or private, so no transcript exists — retrying will not help",
    ),
    (
        FailedToCreateConsentCookie,
        TranscriptErrorCode.UPSTREAM_ERROR,
        "could not get past YouTube's consent wall",
    ),
    (
        YouTubeRequestFailed,
        TranscriptErrorCode.UPSTREAM_ERROR,
        "the request to YouTube failed",
    ),
)


def _error_for(video_id: str, exc: Exception) -> TranscriptError | None:
    """Map a library exception onto the §11 taxonomy, or `None` if it is not one we know."""
    for exc_type, code, cause in _ERROR_TABLE:
        if isinstance(exc, exc_type):
            return _error(code, video_id, cause)
    return None


def _validate_video_id(video_id: str) -> None:
    """Reject non-video-ID input without a network call (a URL here is a caller bug)."""
    if not _VIDEO_ID_PATTERN.fullmatch(video_id):
        raise _error(
            TranscriptErrorCode.INVALID_REQUEST,
            video_id,
            "not a YouTube video ID — pass the 11-character ID, not a URL",
        )


def _transcript_key(video_id: str, language_code: str, *, styled: bool) -> str:
    """Cache key for one transcript variant.

    `preserve_formatting` output is a different payload from the default (stripped) one, so the
    style variant is part of the key: a stripped entry must never satisfy a styled request. Both
    directions are kept apart — see the cache tests.
    """
    if styled:
        return namespaced("transcript", video_id, language_code, "styled")
    return namespaced("transcript", video_id, language_code)


def _api() -> YouTubeTranscriptApi:
    """A fresh client per call: each instance owns a `requests.Session` and is not thread-safe."""
    return YouTubeTranscriptApi(proxy_config=build_proxy_config(get_settings()))


async def _offloaded(worker: Callable[[], T], *, video_id: str, what: str) -> T:
    """Run a blocking library call in a worker thread under an explicit deadline.

    `abandon_on_cancel=True` is required, not cosmetic: the default shields the await from
    cancellation, so the deadline could neither fire nor return early — the thread keeps running
    in the background while the tool call gives up (anyio documents the thread leak).
    """
    try:
        with anyio.fail_after(TRANSCRIPT_TIMEOUT_SECONDS):
            return await anyio.to_thread.run_sync(worker, abandon_on_cancel=True)
    except TimeoutError:
        raise _error(TranscriptErrorCode.UPSTREAM_ERROR, video_id, f"{what} timed out") from None
    except requests.RequestException as exc:
        # Connection errors escape the library unwrapped (research §B9).
        raise _error(
            TranscriptErrorCode.UPSTREAM_ERROR, video_id, f"{what} failed: cannot reach YouTube"
        ) from exc
    except Exception as exc:
        # Only `Exception` is caught: cancellation is a BaseException and must propagate, so a
        # caller cancelling the tool call is never turned into a transcript error.
        mapped = _error_for(video_id, exc)
        if mapped is not None:
            raise mapped from exc
        if isinstance(exc, CouldNotRetrieveTranscript):
            # A library failure mode we do not know yet is still upstream, never a caller bug.
            logger.warning("unmapped %s for video %s", type(exc).__name__, video_id)
            cause = "unmapped youtube-transcript-api error"
        else:
            logger.exception("unexpected transcript failure for video %s", video_id)
            cause = "unexpected youtube-transcript-api error"
        raise _error(TranscriptErrorCode.UPSTREAM_ERROR, video_id, f"{what} failed: {cause}") from exc


def _fetch_sync(
    video_id: str, languages: tuple[str, ...], preserve_formatting: bool
) -> Transcript:
    """Blocking library call: fetch and convert to the public model."""
    fetched = _api().fetch(
        video_id, languages=languages, preserve_formatting=preserve_formatting
    )
    return Transcript(
        video_id=fetched.video_id,
        language=fetched.language,
        language_code=fetched.language_code,
        is_generated=fetched.is_generated,
        snippets=[
            TranscriptSnippet(text=snippet.text, start=snippet.start, duration=snippet.duration)
            for snippet in fetched
        ],
    )


def _list_sync(video_id: str) -> TranscriptTrackList:
    """Blocking library call: enumerate tracks and convert to the public model."""
    listing = _api().list(video_id)
    return TranscriptTrackList(
        video_id=video_id,
        tracks=[
            TranscriptTrack(
                language=track.language,
                language_code=track.language_code,
                is_generated=track.is_generated,
                is_translatable=track.is_translatable,
                translatable_to=[lang.language_code for lang in track.translation_languages],
            )
            for track in listing
        ],
    )


async def fetch_transcript(
    video_id: str,
    languages: Sequence[str] | None = None,
    preserve_formatting: bool = False,
    *,
    cache: Cache | None = None,
) -> Transcript:
    """Fetch a video's transcript, preferring `languages` in order.

    `languages` defaults to the configured transcript language. A cached transcript never
    expires (a published transcript does not change, §14) and is keyed by the language that was
    actually returned plus a `styled` marker when `preserve_formatting` is set, so a caller can
    never be served the other style variant. Raises `TranscriptError` for every failure mode —
    never the library's own.
    """
    _validate_video_id(video_id)
    codes = tuple(languages) if languages else (get_settings().youtube_transcript_lang,)

    if cache is not None:
        # A hit in any requested language is accepted even when a fresh call could prefer an
        # earlier requested code: transcripts are immutable and the entry is always a correctly
        # labelled requested language, so serving it is benign.
        for code in codes:
            cached = await cache.get(_transcript_key(video_id, code, styled=preserve_formatting))
            if cached is not None:
                return Transcript.model_validate(cached)

    transcript = await _offloaded(
        lambda: _fetch_sync(video_id, codes, preserve_formatting),
        video_id=video_id,
        what="transcript fetch",
    )
    if cache is not None:
        await cache.set(
            _transcript_key(video_id, transcript.language_code, styled=preserve_formatting),
            transcript.model_dump(),
            ttl=0,
        )
    return transcript


async def list_transcript_tracks(
    video_id: str, *, cache: Cache | None = None
) -> TranscriptTrackList:
    """List every caption track available for a video, including translation targets.

    Cached forever under `transcript_tracks:<video_id>` (§14). Raises `TranscriptError`.
    """
    _validate_video_id(video_id)

    cached = (
        await cache.get(namespaced("transcript_tracks", video_id))
        if cache is not None
        else None
    )
    if cached is not None:
        return TranscriptTrackList.model_validate(cached)

    tracks = await _offloaded(
        lambda: _list_sync(video_id), video_id=video_id, what="transcript track listing"
    )
    if cache is not None:
        await cache.set(namespaced("transcript_tracks", video_id), tracks.model_dump(), ttl=0)
    return tracks
