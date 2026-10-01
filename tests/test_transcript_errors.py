"""Transcript error taxonomy (section 11): every code, its retryability, and serialization."""

import json

import pytest

from youtube_mcp.transcript.errors import (
    RETRYABLE_CODES,
    TranscriptError,
    TranscriptErrorCode,
)

EXPECTED_RETRYABLE = {
    TranscriptErrorCode.DISABLED: False,
    TranscriptErrorCode.NOT_FOUND: False,
    TranscriptErrorCode.IP_BLOCKED: True,
    TranscriptErrorCode.AGE_RESTRICTED: False,
    TranscriptErrorCode.UPSTREAM_ERROR: True,
    TranscriptErrorCode.INVALID_REQUEST: False,
}


def test_codes_are_stable_strings() -> None:
    assert [code.value for code in TranscriptErrorCode] == [
        "TRANSCRIPT_DISABLED",
        "TRANSCRIPT_NOT_FOUND",
        "TRANSCRIPT_IP_BLOCKED",
        "TRANSCRIPT_AGE_RESTRICTED",
        "TRANSCRIPT_UPSTREAM_ERROR",
        "INVALID_REQUEST",
    ]


@pytest.mark.parametrize(("code", "retryable"), EXPECTED_RETRYABLE.items())
def test_retryability(code: TranscriptErrorCode, retryable: bool) -> None:
    error = TranscriptError(code, "detail")
    assert error.retryable is retryable
    assert error.code is code
    assert error.message == "detail"


def test_retryable_codes_set_matches_table() -> None:
    assert RETRYABLE_CODES == {
        code for code, retryable in EXPECTED_RETRYABLE.items() if retryable
    }


def test_is_an_exception_with_code_in_str() -> None:
    error = TranscriptError(TranscriptErrorCode.IP_BLOCKED, "blocked by YouTube")
    assert isinstance(error, Exception)
    assert str(error) == "TRANSCRIPT_IP_BLOCKED: blocked by YouTube"


def test_to_dict_is_json_safe() -> None:
    payload = TranscriptError(TranscriptErrorCode.DISABLED, "captions off").to_dict()
    assert payload == {
        "code": "TRANSCRIPT_DISABLED",
        "retryable": False,
        "message": "captions off",
    }
    assert json.loads(json.dumps(payload)) == payload
