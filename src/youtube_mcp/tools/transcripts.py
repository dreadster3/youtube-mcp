"""Transcript tools (section 8): fetch, timestamped segments, track listing, in-transcript search.

Four tools over the Batch-3 transcript layer. The layer returns a whole `Transcript`; this
module owns presentation — plain text, optional `[mm:ss]` markers, pagination by
`Settings.response_limit` and the cursor that continues exactly where the last page stopped.

**Truncation contract** (shared by every paginated tool here):

- Pages are cut at caption-segment boundaries, never mid-segment, so a cursor always resumes
  exactly and a page never ends with half a sentence.
- `next_cursor` is a character offset derived from those segment boundaries. Pass it back
  unchanged; `None` means the transcript is complete.
- When one segment on its own exceeds the limit it is still returned (a page must make
  progress, otherwise the cursor would never advance).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from youtube_mcp.cache import ensure_connected
from youtube_mcp.tools import Deps
from youtube_mcp.tools.errors import as_tool_error
from youtube_mcp.transcript.fetch import (
    Transcript,
    TranscriptSnippet,
    TranscriptTrackList,
    fetch_transcript,
    fetch_translated_transcript,
    list_transcript_tracks,
)

logger = logging.getLogger(__name__)

#: Separator between two rendered segments inside one page.
_SEPARATOR = " "
#: Matches returned per page by `youtube_search_in_transcript`.
MATCH_PAGE_SIZE = 20


class TranscriptResult(BaseModel):
    """A slice of a transcript as plain text.

    `is_translated` / `translated_from` record provenance: when set, the text is YouTube's
    translation of the `translated_from` track, not the creator's words.
    """

    video_id: str
    text: str
    language: str
    language_code: str
    is_generated: bool
    is_translated: bool = False
    translated_from: str | None = None
    truncated: bool
    next_cursor: int | None


class TranscriptSegment(BaseModel):
    """One caption segment with its on-screen timing."""

    text: str
    start: float
    duration: float


class TimestampedSegments(BaseModel):
    """A slice of a transcript as timed segments. `next_cursor` is a segment index.

    `is_translated` / `translated_from` record provenance: when set, the segments are YouTube's
    translation of the `translated_from` track, not the creator's words.
    """

    video_id: str
    language: str
    language_code: str
    is_generated: bool
    is_translated: bool = False
    translated_from: str | None = None
    segments: list[TranscriptSegment]
    truncated: bool
    next_cursor: int | None


class TranscriptMatch(BaseModel):
    """One caption segment containing the search query."""

    text: str
    start: float
    duration: float


class TranscriptMatches(BaseModel):
    """Matching segments for a query. `next_cursor` is an index into the match list."""

    video_id: str
    language_code: str
    query: str
    matches: list[TranscriptMatch]
    total_matches: int
    truncated: bool
    next_cursor: int | None


@dataclass(frozen=True)
class _Rendered:
    """A full transcript prepared for windowing.

    `offsets[i]` is the cursor that resumes at segment `i`; `markers[i]` is the `[mm:ss]`
    anchor for that segment, trailing space included, when timestamps are on; `inline[i]`
    says whether that anchor belongs on the canonical rendering — true where the caption
    minute changes, so a long run inside one minute carries a single anchor.
    """

    texts: list[str]
    markers: list[str]
    inline: list[bool]
    offsets: list[int]


def _minute(seconds: float) -> int:
    """Minute bucket of a segment start, so a page never re-emits the same `[mm:ss]`."""
    return int(seconds) // 60


def _marker(seconds: float) -> str:
    total = int(seconds)
    return f"[{total // 60:02d}:{total % 60:02d}]"


def _render(snippets: list[TranscriptSnippet], *, timestamps: bool) -> _Rendered:
    """Render segment texts, their optional time anchors and the cursor offsets."""
    texts: list[str] = []
    markers: list[str] = []
    inline: list[bool] = []
    offsets: list[int] = []
    position = 0
    last_minute: int | None = None
    for snippet in snippets:
        minute = _minute(snippet.start)
        offsets.append(position)
        texts.append(snippet.text)
        markers.append(_marker(snippet.start) if timestamps else "")
        # Only the minute bucket is remembered: two segments in the same minute share one
        # anchor, whatever the requested style.
        inline.append(minute != last_minute)
        # Only the text participates in the offset arithmetic, so a cursor is stable whether
        # or not timestamps are requested.
        position += len(snippet.text) + len(_SEPARATOR)
        last_minute = minute
    return _Rendered(texts=texts, markers=markers, inline=inline, offsets=offsets)


def _start_index(offsets: list[int], cursor: int | None) -> int:
    """Map a cursor onto the segment it resumes at (`bisect` over the boundary offsets).

    A cursor that is not exactly on a boundary — an LLM doing arithmetic on it — snaps back
    to the start of the segment containing it rather than splitting a segment. A cursor past
    the end returns `len(offsets)`, i.e. an empty page: the last page's cursor is exactly
    `offsets[-1]`.
    """
    if cursor is None or not offsets:
        return 0
    if cursor > offsets[-1]:
        return len(offsets)
    from bisect import bisect_right

    return max(0, min(bisect_right(offsets, cursor) - 1, len(offsets) - 1))


def _window(
    rendered: _Rendered, *, start: int, limit: int, timestamps: bool
) -> tuple[str, int | None, int]:
    """Build one page from `start`, honouring `limit`. Returns `(text, next_cursor, end)`."""
    parts: list[str] = []
    used = 0
    index = start
    while index < len(rendered.texts):
        text = rendered.texts[index]
        # A marker is emitted where the caption minute changes, and always on the first
        # segment of a page so a resumed page is not left without a time reference.
        anchor = (
            rendered.markers[index]
            if timestamps and (index == start or rendered.inline[index])
            else ""
        )
        gap = _SEPARATOR if parts else ""
        piece = f"{gap}{anchor} {text}" if anchor else f"{gap}{text}"
        if parts and used + len(piece) > limit:
            break
        parts.append(piece)
        used += len(piece)
        index += 1
    text = "".join(parts)
    next_cursor = rendered.offsets[index] if index < len(rendered.texts) else None
    return text, next_cursor, index


def _segment_window(
    snippets: list[TranscriptSnippet], *, start: int, limit: int
) -> tuple[list[TranscriptSnippet], int | None]:
    """Slice timed segments by cumulative text length, same policy as `_window`."""
    chosen: list[TranscriptSnippet] = []
    used = 0
    index = start
    while index < len(snippets):
        cost = len(snippets[index].text) + (len(_SEPARATOR) if chosen else 0)
        if chosen and used + cost > limit:
            break
        chosen.append(snippets[index])
        used += cost
        index += 1
    next_cursor = index if index < len(snippets) else None
    return chosen, next_cursor


def _languages(deps: Deps, languages: list[str] | None) -> list[str]:
    """Requested languages, or the configured default when the caller passed none."""
    return list(languages) if languages else [deps.settings.youtube_transcript_lang]


async def _fetch_maybe_translated(
    deps: Deps,
    video_id: str,
    languages: list[str] | None,
    translate_to: str | None,
) -> Transcript:
    """The native transcript, or YouTube's translation of another track when asked for one."""
    cache = await ensure_connected(deps.cache)
    if translate_to is None:
        return await fetch_transcript(video_id, _languages(deps, languages), cache=cache)
    return await fetch_translated_transcript(
        video_id, translate_to, _languages(deps, languages), cache=cache
    )


def _transcript_result(
    deps: Deps, transcript: Transcript, cursor: int | None, *, timestamps: bool
) -> TranscriptResult:
    """Render one page of `transcript` as text with the shared truncation policy."""
    rendered = _render(transcript.snippets, timestamps=timestamps)
    text, next_cursor, _ = _window(
        rendered,
        start=_start_index(rendered.offsets, cursor),
        limit=deps.settings.response_limit,
        timestamps=timestamps,
    )
    return TranscriptResult(
        video_id=transcript.video_id,
        text=text,
        language=transcript.language,
        language_code=transcript.language_code,
        is_generated=transcript.is_generated,
        is_translated=transcript.is_translated,
        translated_from=transcript.translated_from,
        truncated=next_cursor is not None,
        next_cursor=next_cursor,
    )


def register(mcp: FastMCP, deps: Deps) -> None:
    """Register the four transcript tools on `mcp`."""

    @mcp.tool
    @as_tool_error
    async def youtube_get_transcript(
        video_id: str,
        languages: list[str] | None = None,
        include_timestamps: bool = False,
        translate_to: str | None = None,
        cursor: int | None = None,
    ) -> TranscriptResult:
        """Fetch a YouTube video's transcript as plain text, optionally translated.

        Returns the caption text for one 11-character video ID, with the language that was
        actually used, whether it is auto-generated, and `next_cursor`.

        Timestamps are OFF by default because each `[mm:ss]` marker costs tokens; when
        `include_timestamps` is true a marker is inserted wherever the caption minute changes
        (a word-level auto-generated track gets far fewer markers than segments).

        `translate_to` asks YouTube to translate the caption track that `languages` selects into
        that language code, so a caller can read a video that has no captions of its own
        language. Get valid targets from `youtube_list_transcript_languages`
        (`translatable_to`). The text returned is **YouTube's machine translation, not the
        creator's words**: it is not a translation this server performed, and it can be wrong.
        `is_translated` is true when the text differs in language from the track it came from,
        `translated_from` carries that track's language code, and `is_generated` is always true
        for a translation because YouTube's translation is machine output. Paging, truncation
        and the cursor contract are the same as for an untranslated transcript.

        Long transcripts are truncated at the server's `RESPONSE_LIMIT` and cut on caption
        boundaries: read `next_cursor` and call again with it to get the following page —
        `next_cursor: null` (and `truncated: false`) means you have the whole thing. Each
        page repeats the time anchor of its first segment.

        Costs no YouTube Data API quota: transcripts are scraped from YouTube's internal
        caption endpoint, not the Data API, and translation is a parameter on that same
        endpoint. They are cached forever, so a repeat call is free — translated pages under
        their own cache key, never mixed with the untranslated ones. Failures are expected and
        specific — captions may be disabled, absent in the requested languages, or the IP may
        be blocked (transient). A language the video cannot be translated into is an
        `INVALID_REQUEST` error: check the listing, do not retry. Age-restricted videos cannot
        be read.
        """
        transcript = await _fetch_maybe_translated(deps, video_id, languages, translate_to)
        return _transcript_result(deps, transcript, cursor, timestamps=include_timestamps)

    @mcp.tool
    @as_tool_error
    async def youtube_get_timestamped_transcript(
        video_id: str,
        languages: list[str] | None = None,
        translate_to: str | None = None,
        cursor: int | None = None,
    ) -> TimestampedSegments:
        """Fetch a YouTube video's transcript as timed segments, optionally translated.

        Returns every caption segment as `{text, start, duration}`, where `start` is seconds
        from the video's beginning and `duration` is on-screen time (segments overlap, so
        `duration` is not speech length). Use this to build chapter lists or deep links
        (`https://youtu.be/<video_id>?t=<start>`); use `youtube_get_transcript` when you only
        need the words.

        `translate_to` returns YouTube's translation into that language code (valid targets:
        `youtube_list_transcript_languages`), with the segment timings of the track it was made
        from. The words are **YouTube's machine translation, not the creator's words** — use it
        to read a video whose captions are in another language, and quote it as such.
        `is_translated` and `translated_from` report that provenance; `is_generated` is always
        true for a translation.

        Long transcripts are truncated at `RESPONSE_LIMIT` by cumulative text length.
        `next_cursor` is the index of the next segment — pass it back to continue;
        `null` means you have all segments. Same caching and failure modes as
        `youtube_get_transcript`; this costs no Data API quota, translated or not, and a target
        language the video cannot be translated into is an `INVALID_REQUEST` error, not a
        transient one.
        """
        transcript = await _fetch_maybe_translated(deps, video_id, languages, translate_to)
        snippets, next_cursor = _segment_window(
            transcript.snippets,
            start=max(0, cursor or 0),
            limit=deps.settings.response_limit,
        )
        return TimestampedSegments(
            video_id=transcript.video_id,
            language=transcript.language,
            language_code=transcript.language_code,
            is_generated=transcript.is_generated,
            is_translated=transcript.is_translated,
            translated_from=transcript.translated_from,
            segments=[
                TranscriptSegment(text=s.text, start=s.start, duration=s.duration) for s in snippets
            ],
            truncated=next_cursor is not None,
            next_cursor=next_cursor,
        )

    @mcp.tool
    @as_tool_error
    async def youtube_list_transcript_languages(video_id: str) -> TranscriptTrackList:
        """List the caption tracks available for a YouTube video.

        Returns one entry per track: `language`, `language_code`, whether it is
        auto-generated (`is_generated`), and which languages it can be translated to. Use this
        before `youtube_get_transcript` when you need a specific language, or to tell the user
        which languages exist. Generated tracks are usually less accurate than uploaded ones.

        Cached forever, costs no Data API quota. Fails cleanly when a video has captions
        disabled or no caption track at all — that is a normal outcome, not an outage.
        """
        return await list_transcript_tracks(video_id, cache=await ensure_connected(deps.cache))

    @mcp.tool
    @as_tool_error
    async def youtube_search_in_transcript(
        video_id: str,
        query: str,
        language: str | None = None,
        cursor: int | None = None,
    ) -> TranscriptMatches:
        """Search inside a video's transcript and return only the matching segments.

        A case-insensitive substring search over the caption segments, done server-side, so a
        narrow question ("does this video mention Kubernetes?") costs a few hundred tokens
        instead of the whole transcript. Returns matching segments with their `start` seconds
        (deep-link ready), `total_matches` across the whole transcript, and up to 20 matches
        per page.

        Limitations: matching is per caption segment, so a phrase spanning a segment boundary
        will not match; the query is a literal substring, not a boolean/regex expression. Pass
        `cursor` back to page through more than 20 matches; `next_cursor: null` means you have
        the last page. `language` selects the caption track (defaults to the configured
        language). Costs no Data API quota; cached like the other transcript tools.
        """
        if not query.strip():
            raise ToolError(
                "youtube_search_in_transcript: query must not be empty; pass the text "
                "to look for inside the transcript"
            )
        transcript = await fetch_transcript(
            video_id,
            [language] if language else _languages(deps, None),
            cache=await ensure_connected(deps.cache),
        )
        needle = query.casefold()
        found = [snippet for snippet in transcript.snippets if needle in snippet.text.casefold()]
        start = max(0, cursor or 0)
        page = found[start : start + MATCH_PAGE_SIZE]
        next_cursor = start + MATCH_PAGE_SIZE if start + MATCH_PAGE_SIZE < len(found) else None
        return TranscriptMatches(
            video_id=transcript.video_id,
            language_code=transcript.language_code,
            query=query,
            matches=[
                TranscriptMatch(text=s.text, start=s.start, duration=s.duration) for s in page
            ],
            total_matches=len(found),
            truncated=next_cursor is not None,
            next_cursor=next_cursor,
        )
