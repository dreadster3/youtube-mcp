"""Transcript error taxonomy (section 11 of HANDOFF.md).

Closed set: every transcript failure maps to exactly one code, and the code decides
whether a retry is worth attempting. Keep the string values stable — the MCP error
surface depends on them.
"""

from enum import StrEnum


class TranscriptErrorCode(StrEnum):
    """Stable error codes for transcript failures."""

    DISABLED = "TRANSCRIPT_DISABLED"
    NOT_FOUND = "TRANSCRIPT_NOT_FOUND"
    IP_BLOCKED = "TRANSCRIPT_IP_BLOCKED"
    AGE_RESTRICTED = "TRANSCRIPT_AGE_RESTRICTED"
    UPSTREAM_ERROR = "TRANSCRIPT_UPSTREAM_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"


#: Codes worth retrying later. Everything else is terminal — the model should stop
#: asking rather than burn a retry loop.
RETRYABLE_CODES = frozenset(
    {TranscriptErrorCode.IP_BLOCKED, TranscriptErrorCode.UPSTREAM_ERROR}
)


class TranscriptError(Exception):
    """A transcript failure carrying a stable code and its retryability.

    The message is for the model: it must distinguish "captions are off" (give up)
    from "IP blocked" (transient), so callers should pass a specific one.
    """

    def __init__(self, code: TranscriptErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in RETRYABLE_CODES

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def to_dict(self) -> dict[str, object]:
        """Flat, JSON-safe shape for tool error payloads."""
        return {"code": self.code.value, "retryable": self.retryable, "message": self.message}
