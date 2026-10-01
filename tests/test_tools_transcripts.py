"""Tool-logic tests for the transcript group (section 8), offline.

Every case drives the real tool functions through the `FastMCP` call path, with the
Batch-3 layer stubbed at the `youtube_mcp.tools.transcripts` module boundary (that is where
the tools bound `fetch_transcript` / `list_transcript_tracks`), so what is exercised is the
truncation/window arithmetic, the cursor contract and the error translation — not the scraper.
"""

from __future__ import annotations

from typing import Callable

import pytest
from fastmcp import Client

from conftest import make_test_server
from youtube_mcp.config import Settings
from youtube_mcp.tools import transcripts as tools_transcripts
from youtube_mcp.transcript.errors import TranscriptError, TranscriptErrorCode
from youtube_mcp.transcript.fetch import (
    Transcript,
    TranscriptSnippet,
    TranscriptTrack,
    TranscriptTrackList,
)

VIDEO_ID = "dQw4w9WgXcQ"

#: Every `Cache` this module hands to a tool, so the autouse fixture can close them.
_OPEN_CACHES: list = []


def make_transcript(*texts: str, starts: list[float] | None = None) -> Transcript:
    """A transcript whose snippets are `texts`, one per segment."""
    starts = starts or [float(index * 2) for index in range(len(texts))]
    return Transcript(
        video_id=VIDEO_ID,
        language="English",
        language_code="en",
        is_generated=False,
        snippets=[
            TranscriptSnippet(text=text, start=starts[index], duration=1.5)
            for index, text in enumerate(texts)
        ],
    )


def patch_fetch(
    monkeypatch: pytest.MonkeyPatch,
    transcript: Transcript | None = None,
    error: Exception | None = None,
) -> list[dict]:
    """Replace `fetch_transcript` at the tools boundary; return the recorded call kwargs."""
    calls: list[dict] = []

    async def fake_fetch(video_id: str, languages=None, preserve_formatting=False, *, cache=None):
        calls.append(
            {
                "video_id": video_id,
                "languages": languages,
                "preserve_formatting": preserve_formatting,
                "cache": cache,
            }
        )
        if error is not None:
            raise error
        return transcript

    monkeypatch.setattr(tools_transcripts, "fetch_transcript", fake_fetch)
    return calls


def patch_tracks(
    monkeypatch: pytest.MonkeyPatch,
    tracks: TranscriptTrackList | None = None,
    error: Exception | None = None,
) -> list[dict]:
    calls: list[dict] = []

    async def fake_list(video_id: str, *, cache=None):
        calls.append({"video_id": video_id, "cache": cache})
        if error is not None:
            raise error
        return tracks

    monkeypatch.setattr(tools_transcripts, "list_transcript_tracks", fake_list)
    return calls


@pytest.fixture(autouse=True)
async def close_test_caches():
    """Close every `Cache` a test opened.

    `Cache.connect()` starts aiosqlite's non-daemon worker thread; leaving one open at
    interpreter exit makes pytest hang forever waiting to join it. Tools connect injected
    caches themselves (deliberately), so the suite must close them after each test.
    """
    yield
    for cache in list(_OPEN_CACHES):
        await cache.close()
    _OPEN_CACHES.clear()


async def call_tool(mcp, name: str, arguments: dict) -> object:
    """Call a tool through the in-memory client and return its structured content."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
        return result.structured_content


async def call_error(mcp, name: str, arguments: dict) -> str:
    """Call a tool expected to fail, and return the model-facing message."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
        assert result.is_error
        return result.content[0].text


# --------------------------------------------------------------- plain-text truncation


async def test_get_transcript_returns_whole_text_when_under_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript("one", "two", "three"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=100))

    result = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert result["text"] == "one two three"
    assert result["truncated"] is False
    assert result["next_cursor"] is None
    assert result["language"] == "English"
    assert result["language_code"] == "en"
    assert result["is_generated"] is False


async def test_get_transcript_pages_at_segment_boundaries_and_cursor_continues_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core contract: every page is a prefix, and the concatenation is the transcript."""
    patch_fetch(monkeypatch, make_transcript("aaaaa", "bbbbb", "ccccc", "ddddd"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=11))

    pages = []
    cursor = None
    for _ in range(10):  # bounded loop: a cursor bug must fail, not hang
        arguments = {"video_id": VIDEO_ID} | ({"cursor": cursor} if cursor is not None else {})
        page = await call_tool(mcp, "youtube_get_transcript", arguments)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert [page["text"] for page in pages] == ["aaaaa bbbbb", "ccccc ddddd"]
    assert [page["truncated"] for page in pages] == [True, False]
    assert pages[1]["next_cursor"] is None
    assert " ".join(page["text"] for page in pages) == "aaaaa bbbbb ccccc ddddd"


async def test_get_transcript_never_splits_a_segment_even_when_it_exceeds_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized segment is returned whole: a page must always make progress."""
    patch_fetch(monkeypatch, make_transcript("x" * 100, "tail"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=10))

    first = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})
    second = await call_tool(
        mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "cursor": first["next_cursor"]}
    )

    assert first["text"] == "x" * 100
    assert first["next_cursor"] == 101
    assert second["text"] == "tail"
    assert second["next_cursor"] is None


async def test_get_transcript_honours_the_char_limit_across_many_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript(*["word"] * 50))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=20))

    page = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    # Four "word" segments plus three separators is 19 chars; a fifth would be 24 > 20.
    assert len(page["text"]) <= 20
    assert page["text"] == "word word word word"
    assert page["truncated"] is True


async def test_get_transcript_cursor_off_by_one_snaps_back_to_a_segment_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An LLM doing arithmetic on the cursor must not get a split segment."""
    patch_fetch(monkeypatch, make_transcript("aaaaa", "bbbbb", "ccccc"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=11))

    page = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "cursor": 3})

    assert page["text"] == "aaaaa bbbbb"
    assert page["next_cursor"] == 12


async def test_get_transcript_cursor_past_the_end_returns_empty_untruncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-page semantics, unified across the three cursor tools: past the end is empty."""
    patch_fetch(monkeypatch, make_transcript("aaaaa", "bbbbb"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=11))

    result = await call_tool(
        mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "cursor": 999}
    )

    assert result["text"] == ""
    assert result["next_cursor"] is None
    assert result["truncated"] is False


async def test_get_transcript_cursor_at_the_last_segment_is_the_last_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only cursor the tools emit for a final page is the last segment's offset."""
    patch_fetch(monkeypatch, make_transcript("aaaaa", "bbbbb", "ccccc"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=11))

    first = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})
    last = await call_tool(
        mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "cursor": first["next_cursor"]}
    )

    assert first["next_cursor"] == 12  # offset of segment 2, the last one
    assert last["text"] == "ccccc"
    assert last["next_cursor"] is None


async def test_get_transcript_empty_transcript_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript())
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=10))

    result = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert result["text"] == ""
    assert result["next_cursor"] is None
    assert result["truncated"] is False


# ------------------------------------------------------------------------ timestamps


async def test_get_transcript_embeds_markers_only_on_minute_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(
        monkeypatch,
        make_transcript("first", "second", "third", "fourth", starts=[0.0, 5.0, 61.0, 70.0]),
    )
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=500))

    result = await call_tool(
        mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "include_timestamps": True}
    )

    assert result["text"] == "[00:00] first second [01:01] third fourth"


async def test_get_transcript_without_timestamps_has_no_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript("first", "second", starts=[0.0, 61.0]))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=500))

    result = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert result["text"] == "first second"


async def test_timestamped_pages_include_marker_overhead_in_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Markers count against `RESPONSE_LIMIT`: the limit bounds the returned text, so a
    timestamped page fits fewer segments than the same call without timestamps."""
    patch_fetch(monkeypatch, make_transcript("aaaaa", "bbbbb", "ccccc"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=20))

    with_ts = await call_tool(
        mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "include_timestamps": True}
    )
    without_ts = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert with_ts["text"] == "[00:00] aaaaa bbbbb"
    assert with_ts["next_cursor"] == 12
    assert with_ts["truncated"] is True
    assert without_ts["text"] == "aaaaa bbbbb ccccc"
    assert without_ts["next_cursor"] is None


async def test_timestamped_cursor_continues_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript("aaaaa", "bbbbb", "ccccc"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=20))

    first = await call_tool(
        mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "include_timestamps": True}
    )
    second = await call_tool(
        mcp,
        "youtube_get_transcript",
        {"video_id": VIDEO_ID, "cursor": first["next_cursor"], "include_timestamps": True},
    )

    assert second["text"] == "[00:04] ccccc"
    assert second["next_cursor"] is None
    assert second["truncated"] is False


async def test_page_start_marker_is_the_segments_own_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page cut mid-minute opens with its first segment's real timestamp, not a stale one."""
    patch_fetch(
        monkeypatch,
        make_transcript("aaaaa", "bbbbb", "ccccc", starts=[0.0, 5.0, 10.0]),
    )
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=20))

    first = await call_tool(
        mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "include_timestamps": True}
    )
    second = await call_tool(
        mcp,
        "youtube_get_transcript",
        {"video_id": VIDEO_ID, "cursor": first["next_cursor"], "include_timestamps": True},
    )

    assert first["text"] == "[00:00] aaaaa bbbbb"
    # The minute did not change, so this segment carries no inline marker — but it opens the
    # page, so it is anchored at its own 10s start.
    assert second["text"] == "[00:10] ccccc"


# ------------------------------------------------------------- timestamped segments


async def test_timestamped_transcript_returns_segments_and_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(
        monkeypatch, make_transcript("aaaaa", "bbbbb", "ccccc", starts=[0.0, 2.0, 4.0])
    )
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=11))

    page = await call_tool(mcp, "youtube_get_timestamped_transcript", {"video_id": VIDEO_ID})
    rest = await call_tool(
        mcp,
        "youtube_get_timestamped_transcript",
        {"video_id": VIDEO_ID, "cursor": page["next_cursor"]},
    )

    assert page["segments"] == [
        {"text": "aaaaa", "start": 0.0, "duration": 1.5},
        {"text": "bbbbb", "start": 2.0, "duration": 1.5},
    ]
    assert page["truncated"] is True
    assert page["next_cursor"] == 2
    assert [segment["text"] for segment in rest["segments"]] == ["ccccc"]
    assert rest["truncated"] is False
    assert rest["next_cursor"] is None


async def test_timestamped_transcript_negative_cursor_starts_at_the_first_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative cursor must page from segment 0 — not re-emit the last segment first."""
    patch_fetch(
        monkeypatch, make_transcript("aaaaa", "bbbbb", "ccccc", starts=[0.0, 2.0, 4.0])
    )
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=6))

    page = await call_tool(
        mcp, "youtube_get_timestamped_transcript", {"video_id": VIDEO_ID, "cursor": -1}
    )

    assert [segment["text"] for segment in page["segments"]] == ["aaaaa"]
    assert page["next_cursor"] == 1


async def test_timestamped_transcript_cursor_past_the_end_returns_empty_untruncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript("only"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=100))

    result = await call_tool(
        mcp, "youtube_get_timestamped_transcript", {"video_id": VIDEO_ID, "cursor": 99}
    )

    assert result["segments"] == []
    assert result["truncated"] is False


# ---------------------------------------------------------------- languages + caching


async def test_default_language_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = patch_fetch(monkeypatch, make_transcript("hi"))
    mcp, _ = make_test_server(
        settings=Settings(_env_file=None, youtube_transcript_lang="pt", response_limit=100)
    )

    await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert calls[0]["languages"] == ["pt"]


async def test_requested_languages_pass_through_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = patch_fetch(monkeypatch, make_transcript("hi"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=100))

    await call_tool(
        mcp, "youtube_get_transcript", {"video_id": VIDEO_ID, "languages": ["de", "en"]}
    )

    assert calls[0]["languages"] == ["de", "en"]


async def test_transcript_cache_is_connected_before_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Batch-1 landmine 4 + batch-3 watch list: an unconnected cache must be connected."""
    from youtube_mcp.cache import Cache

    calls = patch_fetch(monkeypatch, make_transcript("hi"))
    cache = Cache(str(tmp_path / "cache.db"))
    _OPEN_CACHES.append(cache)
    assert cache._db is None
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=100), cache=cache)

    await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert calls[0]["cache"] is cache
    assert cache._db is not None


async def test_transcript_tools_work_without_a_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_fetch(monkeypatch, make_transcript("hi"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, response_limit=100))

    result = await call_tool(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert calls[0]["cache"] is None
    assert result["text"] == "hi"


async def test_list_transcript_languages_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = TranscriptTrackList(
        video_id=VIDEO_ID,
        tracks=[
            TranscriptTrack(
                language="English",
                language_code="en",
                is_generated=True,
                is_translatable=True,
                translatable_to=["de"],
            )
        ],
    )
    patch_tracks(monkeypatch, listing)
    mcp, _ = make_test_server()

    result = await call_tool(
        mcp, "youtube_list_transcript_languages", {"video_id": VIDEO_ID}
    )

    assert result["tracks"] == [
        {
            "language": "English",
            "language_code": "en",
            "is_generated": True,
            "is_translatable": True,
            "translatable_to": ["de"],
        }
    ]


# --------------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (TranscriptErrorCode.DISABLED, "TRANSCRIPT_DISABLED"),
        (TranscriptErrorCode.NOT_FOUND, "TRANSCRIPT_NOT_FOUND"),
        (TranscriptErrorCode.AGE_RESTRICTED, "TRANSCRIPT_AGE_RESTRICTED"),
        (TranscriptErrorCode.INVALID_REQUEST, "INVALID_REQUEST"),
        (TranscriptErrorCode.IP_BLOCKED, "TRANSCRIPT_IP_BLOCKED"),
        (TranscriptErrorCode.UPSTREAM_ERROR, "TRANSCRIPT_UPSTREAM_ERROR"),
    ],
)
async def test_transcript_errors_render_code_and_message(
    monkeypatch: pytest.MonkeyPatch, code: TranscriptErrorCode, expected: str
) -> None:
    error = TranscriptError(code, f"video {VIDEO_ID}: captions are disabled")
    patch_fetch(monkeypatch, error=error)
    mcp, _ = make_test_server()

    message = await call_error(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert message.startswith(f"{expected}: video {VIDEO_ID}:")
    assert "\n" not in message


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        (TranscriptErrorCode.IP_BLOCKED, True),
        (TranscriptErrorCode.UPSTREAM_ERROR, True),
        (TranscriptErrorCode.DISABLED, False),
        (TranscriptErrorCode.NOT_FOUND, False),
    ],
)
async def test_retry_hint_appears_only_for_retryable_codes(
    monkeypatch: pytest.MonkeyPatch, code: TranscriptErrorCode, retryable: bool
) -> None:
    """The model must be able to tell "captions off" from "IP blocked — try later" (section 11)."""
    patch_fetch(monkeypatch, error=TranscriptError(code, "cause"))
    mcp, _ = make_test_server()

    message = await call_error(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert ("transient" in message) is retryable


async def test_unexpected_exception_is_masked_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in the layer must not leak a traceback or an internal repr to the model."""
    patch_fetch(monkeypatch, error=RuntimeError("secret internal detail"))
    mcp, _ = make_test_server()

    with caplog.at_level("ERROR"):
        message = await call_error(mcp, "youtube_get_transcript", {"video_id": VIDEO_ID})

    assert "secret internal detail" not in message
    assert "unexpected internal error" in message
    assert "RuntimeError" in message
    assert "unexpected failure" in caplog.text


async def test_transcript_error_on_track_listing_also_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_tracks(monkeypatch, error=TranscriptError(TranscriptErrorCode.NOT_FOUND, "no tracks"))
    mcp, _ = make_test_server()

    message = await call_error(
        mcp, "youtube_list_transcript_languages", {"video_id": VIDEO_ID}
    )

    assert message == "TRANSCRIPT_NOT_FOUND: no tracks"


# ---------------------------------------------------------------- in-transcript search


async def test_search_in_transcript_is_case_insensitive_and_reports_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(
        monkeypatch,
        make_transcript(
            "Kubernetes is great",
            "we use kubernetes daily",
            "unrelated",
            "KUBERNETES again",
            starts=[0.0, 3.0, 6.0, 9.0],
        ),
    )
    mcp, _ = make_test_server()

    result = await call_tool(
        mcp, "youtube_search_in_transcript", {"video_id": VIDEO_ID, "query": "kubERnetes"}
    )

    assert result["total_matches"] == 3
    assert [match["start"] for match in result["matches"]] == [0.0, 3.0, 9.0]
    assert result["query"] == "kubERnetes"
    assert result["truncated"] is False
    assert result["next_cursor"] is None


async def test_search_in_transcript_pages_at_20_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript(*["hit"] * 45))
    mcp, _ = make_test_server()

    first = await call_tool(
        mcp, "youtube_search_in_transcript", {"video_id": VIDEO_ID, "query": "hit"}
    )
    second = await call_tool(
        mcp,
        "youtube_search_in_transcript",
        {"video_id": VIDEO_ID, "query": "hit", "cursor": first["next_cursor"]},
    )
    third = await call_tool(
        mcp,
        "youtube_search_in_transcript",
        {"video_id": VIDEO_ID, "query": "hit", "cursor": second["next_cursor"]},
    )

    assert len(first["matches"]) == 20
    assert first["total_matches"] == 45
    assert first["next_cursor"] == 20
    assert second["next_cursor"] == 40
    assert len(third["matches"]) == 5
    assert third["next_cursor"] is None
    assert third["truncated"] is False


async def test_search_in_transcript_no_matches_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript("nothing here"))
    mcp, _ = make_test_server()

    result = await call_tool(
        mcp, "youtube_search_in_transcript", {"video_id": VIDEO_ID, "query": "kubernetes"}
    )

    assert result["matches"] == []
    assert result["total_matches"] == 0
    assert result["next_cursor"] is None


async def test_search_in_transcript_rejects_blank_query_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = patch_fetch(monkeypatch, make_transcript("hi"))
    mcp, _ = make_test_server()

    message = await call_error(
        mcp, "youtube_search_in_transcript", {"video_id": VIDEO_ID, "query": "   "}
    )

    assert "query must not be empty" in message
    assert calls == []


async def test_search_in_transcript_cursor_past_the_end_returns_empty_untruncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript("hit", "nothing"))
    mcp, _ = make_test_server()

    result = await call_tool(
        mcp, "youtube_search_in_transcript", {"video_id": VIDEO_ID, "query": "hit", "cursor": 99}
    )

    assert result["matches"] == []
    assert result["next_cursor"] is None
    assert result["truncated"] is False


async def test_search_in_transcript_negative_cursor_starts_at_the_first_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, make_transcript("hit", "nothing"))
    mcp, _ = make_test_server()

    result = await call_tool(
        mcp, "youtube_search_in_transcript", {"video_id": VIDEO_ID, "query": "hit", "cursor": -3}
    )

    assert [match["text"] for match in result["matches"]] == ["hit"]


async def test_search_in_transcript_language_selects_one_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = patch_fetch(monkeypatch, make_transcript("hi"))
    mcp, _ = make_test_server(settings=Settings(_env_file=None, youtube_transcript_lang="en"))

    await call_tool(
        mcp, "youtube_search_in_transcript", {"video_id": VIDEO_ID, "query": "hi", "language": "de"}
    )
    await call_tool(mcp, "youtube_search_in_transcript", {"video_id": VIDEO_ID, "query": "hi"})

    assert calls[0]["languages"] == ["de"]
    assert calls[1]["languages"] == ["en"]


# --------------------------------------------------------------------------- rendering


def test_render_marks_only_minute_changes() -> None:
    rendered = tools_transcripts._render(
        [
            TranscriptSnippet(text="a", start=0.0, duration=1),
            TranscriptSnippet(text="b", start=59.0, duration=1),
            TranscriptSnippet(text="c", start=60.0, duration=1),
            TranscriptSnippet(text="d", start=125.0, duration=1),
        ],
        timestamps=True,
    )

    assert rendered.inline == [True, False, True, True]
    assert rendered.markers == ["[00:00]", "[00:59]", "[01:00]", "[02:05]"]
