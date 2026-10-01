"""Tool-facing error translation.

One place turns every internal failure into a `ToolError` whose text is a single line an
LLM can act on. Two rules hold everywhere here:

- **`ToolError` is the only thing that escapes a tool body.** Anything unmapped is caught
  by `as_tool_error` and logged at the call site; the model gets a generic message
  instead of a traceback or a library blob.
- **Messages carry the distinction that decides behaviour** — "captions are off, give up"
  versus "IP blocked, retry later" — never the raw exception text (section 11).

The Data API error classes live in `youtube.client` and the transcript ones in
`transcript.errors`; both are mapped here so the tools stay free of error plumbing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from fastmcp.exceptions import ToolError

from youtube_mcp.transcript.errors import TranscriptError
from youtube_mcp.youtube import client as client_module
from youtube_mcp.youtube.quota import QuotaExceeded

logger = logging.getLogger(__name__)

#: Every client failure gets a fixed, actionable one-liner. Quota and rate limiting must
#: say what to *do* (section 5.4): a model that retries an exhausted daily bucket burns the rest
#: of the budget for nothing.
CLIENT_ERROR_MESSAGES: dict[type[client_module.YouTubeApiError], str] = {
    client_module.QuotaExceededError: (
        "the YouTube API daily quota for this method's bucket is exhausted; it resets at "
        "midnight Pacific time; do not retry today"
    ),
    client_module.RateLimitedError: (
        "the YouTube API rate-limited this request; it is transient — retry shortly"
    ),
    client_module.NotFoundError: "the requested resource does not exist on YouTube",
    client_module.CommentsDisabledError: "comments are disabled on this video",
    client_module.InvalidRequestError: "the YouTube API rejected the request parameters",
    client_module.UpstreamError: (
        "the YouTube API call failed upstream (server error or transport failure)"
    ),
}

#: Order matters: a subclass must be matched before its base.
_CLIENT_ERROR_ORDER: tuple[type[client_module.YouTubeApiError], ...] = (
    client_module.QuotaExceededError,
    client_module.RateLimitedError,
    client_module.CommentsDisabledError,
    client_module.NotFoundError,
    client_module.InvalidRequestError,
    client_module.UpstreamError,
)

_RETRY_HINT = "This is transient: retrying later may work."


def transcript_error_message(error: TranscriptError) -> str:
    """Render a `TranscriptError` as one line: `CODE: message` plus a retry hint if useful."""
    message = f"{error.code}: {error.message}"
    if error.retryable:
        message = f"{message}. {_RETRY_HINT}"
    return message


def client_error_message(error: client_module.YouTubeApiError, *, tool: str) -> str:
    """Render a Data API failure as one line naming the tool and the reason.

    The upstream `reason` is included because it is a stable machine-readable token
    (`quotaExceeded`, `invalidPageToken`, …); the API's own message text is **not**, since it
    can be a multi-line blob.
    """
    for error_type in _CLIENT_ERROR_ORDER:
        if isinstance(error, error_type):
            detail = CLIENT_ERROR_MESSAGES[error_type]
            break
    else:  # pragma: no cover - YouTubeApiError subclasses are covered by _CLIENT_ERROR_ORDER
        detail = "the YouTube API call failed"
    reason = f" [{error.reason}]" if error.reason else ""
    return f"{tool}: {detail}{reason}"


def local_quota_message(error: QuotaExceeded, *, tool: str) -> str:
    """Our own counter refused the call before spending anything (`quota.QuotaExceeded`)."""
    return (
        f"{tool}: local budget for the {error.bucket} bucket is exhausted "
        f"({error.cap} units/day, our own accounting, not Google's); it resets at midnight "
        "Pacific time; do not retry today"
    )


def as_tool_error(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Wrap an async tool body so only `ToolError` can escape it.

    `ToolError` passes through untouched (the bodies raise it for their own cases). Every
    known error class becomes a mapped one-liner; anything else is logged with its traceback
    and reduced to a generic message, because an unexpected exception is a bug report for the
    operator, not documentation for the model.
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except ToolError:
            raise
        except TranscriptError as error:
            raise ToolError(transcript_error_message(error)) from error
        except QuotaExceeded as error:
            raise ToolError(local_quota_message(error, tool=func.__name__)) from error
        except client_module.YouTubeApiError as error:
            raise ToolError(client_error_message(error, tool=func.__name__)) from error
        except Exception as error:
            logger.exception("%s: unexpected failure", func.__name__)
            raise ToolError(
                f"{func.__name__}: unexpected internal error "
                f"({type(error).__name__}); this is a server bug, reporting it is worthwhile"
            ) from error

    return wrapper
