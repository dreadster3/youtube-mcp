"""Transcript fetching: taxonomy mapping, timeout, offload, caching, proxy wiring (section 7/11/14).

The library is faked at the module boundary (`youtube_mcp.transcript.fetch.YouTubeTranscriptApi`)
and `get_settings` is replaced with a default-configured `Settings`, so nothing here touches
youtube.com or the host environment.
"""

import re
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import anyio
import pytest
import requests
from youtube_transcript_api import (
    AgeRestricted,
    CookieError,
    CouldNotRetrieveTranscript,
    FailedToCreateConsentCookie,
    FetchedTranscript,
    FetchedTranscriptSnippet,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    NotTranslatable,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    TranslationLanguageNotAvailable,
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

import youtube_mcp.transcript.fetch as fetch_module
from youtube_mcp.cache import Cache
from youtube_mcp.config import Settings
from youtube_mcp.transcript.errors import (
    RETRYABLE_CODES,
    TranscriptError,
    TranscriptErrorCode,
)
from youtube_mcp.transcript.fetch import (
    TRANSCRIPT_TIMEOUT_SECONDS,
    Transcript,
    TranscriptTrackList,
    build_proxy_config,
    fetch_transcript,
    fetch_translated_transcript,
    list_transcript_tracks,
)

VIDEO_ID = "dQw4w9WgXcQ"


class UnmappedLibraryError(CouldNotRetrieveTranscript):
    """A library failure mode this wrapper has never seen (e.g. a new exception in 1.3)."""

    CAUSE_MESSAGE = "a brand new upstream failure"


# --- fakes ---------------------------------------------------------------------------------


class _FakeTranslationLanguage:
    """Shape of the library's private `_TranslationLanguage` dataclass."""

    def __init__(self, language_code: str) -> None:
        self.language = language_code.upper()
        self.language_code = language_code


class FakeTranslatedTrack:
    """What the library's `Transcript.translate(target)` returns: a track that only `fetch`es."""

    def __init__(
        self, target: str, name: str, snippets: Sequence[tuple[str, float, float]]
    ) -> None:
        self.target = target
        self._name = name
        self._snippets = snippets
        self.fetch_calls = 0

    def fetch(self, preserve_formatting: bool = False) -> FetchedTranscript:
        self.fetch_calls += 1
        return FetchedTranscript(
            snippets=[
                FetchedTranscriptSnippet(text, start, duration)
                for text, start, duration in self._snippets
            ],
            video_id=VIDEO_ID,
            language=self._name,
            language_code=self.target,
            # 1.2.4 hard-codes `is_generated=True` in `Transcript.translate`, so a translation of
            # an uploaded track still reports as generated.
            is_generated=True,
        )


class FakeTrack:
    """Stand-in for the library's `Transcript`: only the fields we map are present."""

    def __init__(
        self,
        language: str,
        language_code: str,
        *,
        is_generated: bool = False,
        translatable_to: Sequence[str] = (),
        translated_snippets: Sequence[tuple[str, float, float]] = (("translated", 0.0, 1.0),),
    ) -> None:
        self.language = language
        self.language_code = language_code
        self.is_generated = is_generated
        self.translation_languages = [_FakeTranslationLanguage(code) for code in translatable_to]
        self._translation_names = {
            language.language_code: language.language for language in self.translation_languages
        }
        self._translated_snippets = translated_snippets

    @property
    def is_translatable(self) -> bool:
        return len(self.translation_languages) > 0

    def translate(self, language_code: str) -> FakeTranslatedTrack:
        """Same guard order as the library: untranslatable track first, then unknown target."""
        if not self.is_translatable:
            raise NotTranslatable(VIDEO_ID)
        if language_code not in self._translation_names:
            raise TranslationLanguageNotAvailable(VIDEO_ID)
        return FakeTranslatedTrack(
            language_code, self._translation_names[language_code], self._translated_snippets
        )


def fetched(*snippets: tuple[str, float, float], language_code: str = "en") -> FetchedTranscript:
    """Build a real `FetchedTranscript` so the mapped fields are the library's own."""
    return FetchedTranscript(
        snippets=[
            FetchedTranscriptSnippet(text, start, duration) for text, start, duration in snippets
        ],
        video_id=VIDEO_ID,
        language="English",
        language_code=language_code,
        is_generated=False,
    )


class FakeTranscriptList:
    """Stand-in for the library's `TranscriptList`: iterable, and `find_transcript` picks one."""

    def __init__(self, tracks: Sequence[FakeTrack]) -> None:
        self._tracks = list(tracks)

    def __iter__(self) -> Iterator[FakeTrack]:
        return iter(self._tracks)

    def find_transcript(self, language_codes: Sequence[str]) -> FakeTrack:
        """Library order: manually created tracks first, then generated ones, then NOT_FOUND."""
        for is_generated in (False, True):
            for track in self._tracks:
                if track.language_code in language_codes and track.is_generated is is_generated:
                    return track
        raise NoTranscriptFound(VIDEO_ID, list(language_codes), self)


class FakeApi:
    """Fake `YouTubeTranscriptApi` that records calls and runs a scripted outcome."""

    def __init__(
        self,
        result: Any = None,
        error: BaseException | None = None,
        delay: float = 0.0,
        list_result: Any = None,
    ) -> None:
        self._result = result
        self._list_result = FakeTranscriptList([] if list_result is None else list_result)
        self._error = error
        self._delay = delay
        self.fetch_calls: list[tuple[str, tuple[str, ...], bool]] = []
        self.list_calls: list[str] = []

    def fetch(
        self, video_id: str, languages: Any = ("en",), preserve_formatting: bool = False
    ) -> FetchedTranscript:
        self.fetch_calls.append((video_id, tuple(languages), preserve_formatting))
        if self._delay:
            time.sleep(self._delay)  # blocking on purpose: this is the sync library call
        if self._error is not None:
            raise self._error
        return self._result

    def list(self, video_id: str) -> Any:
        self.list_calls.append(video_id)
        if self._delay:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._list_result


class ApiFactory:
    """Records every `YouTubeTranscriptApi(...)` construction and hands out queued instances."""

    def __init__(self, *instances: FakeApi) -> None:
        self._instances = list(instances)
        self.constructions: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeApi:
        self.constructions.append(kwargs)
        return self._instances.pop(0) if self._instances else FakeApi()


def patch_api(monkeypatch: pytest.MonkeyPatch, *instances: FakeApi) -> ApiFactory:
    factory = ApiFactory(*instances)
    monkeypatch.setattr(fetch_module, "YouTubeTranscriptApi", factory)
    return factory


# --- fixtures ------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def default_settings(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    """Deterministic, proxy-free settings for every test (reuses conftest's env scrubber)."""
    monkeypatch.setattr(fetch_module, "get_settings", lambda: Settings())


@pytest.fixture
async def opened_cache(tmp_path) -> AsyncIterator[Cache]:
    async with Cache(str(tmp_path / "cache.db")) as opened:
        yield opened


def _assert_short_message(exc: TranscriptError, video_id: str = VIDEO_ID) -> None:
    """Messages must be one short line: no referral blob, no multi-line library noise."""
    message = str(exc)
    assert exc.code.value in message
    assert video_id in message
    assert "GitHub" not in message
    assert "github.com" not in message
    assert "\n" not in message
    assert len(message) < 300


# --- taxonomy mapping (section 11) ----------------------------------------------------------------


def _library_failures() -> list[tuple[str, BaseException, TranscriptErrorCode]]:
    """One constructed instance of every library failure we map, with its expected code."""
    http_error = requests.HTTPError("500 Server Error")
    return [
        ("transcripts_disabled", TranscriptsDisabled(VIDEO_ID), TranscriptErrorCode.DISABLED),
        (
            "no_transcript_found",
            NoTranscriptFound(VIDEO_ID, ["en"], object()),
            TranscriptErrorCode.NOT_FOUND,
        ),
        ("request_blocked", RequestBlocked(VIDEO_ID), TranscriptErrorCode.IP_BLOCKED),
        ("ip_blocked_429", IpBlocked(VIDEO_ID), TranscriptErrorCode.IP_BLOCKED),
        ("age_restricted", AgeRestricted(VIDEO_ID), TranscriptErrorCode.AGE_RESTRICTED),
        ("invalid_video_id", InvalidVideoId(VIDEO_ID), TranscriptErrorCode.INVALID_REQUEST),
        (
            "invalid_proxy_config",
            InvalidProxyConfig("GenericProxyConfig requires at least one URL"),
            TranscriptErrorCode.INVALID_REQUEST,
        ),
        ("po_token_required", PoTokenRequired(VIDEO_ID), TranscriptErrorCode.UPSTREAM_ERROR),
        (
            "youtube_data_unparsable",
            YouTubeDataUnparsable(VIDEO_ID),
            TranscriptErrorCode.UPSTREAM_ERROR,
        ),
        (
            "video_unplayable",
            VideoUnplayable(VIDEO_ID, "private video", []),
            TranscriptErrorCode.UPSTREAM_ERROR,
        ),
        ("video_unavailable", VideoUnavailable(VIDEO_ID), TranscriptErrorCode.UPSTREAM_ERROR),
        (
            "failed_to_create_consent_cookie",
            FailedToCreateConsentCookie(VIDEO_ID),
            TranscriptErrorCode.UPSTREAM_ERROR,
        ),
        (
            "youtube_request_failed_500",
            YouTubeRequestFailed(VIDEO_ID, http_error),
            TranscriptErrorCode.UPSTREAM_ERROR,
        ),
        (
            "not_translatable",
            NotTranslatable(VIDEO_ID),
            TranscriptErrorCode.INVALID_REQUEST,
        ),
        (
            "translation_language_not_available",
            TranslationLanguageNotAvailable(VIDEO_ID),
            TranscriptErrorCode.INVALID_REQUEST,
        ),
        # Unmapped `CouldNotRetrieveTranscript` subclass: the catch-all bucket, not INVALID_REQUEST.
        (
            "unmapped_could_not_retrieve",
            UnmappedLibraryError(VIDEO_ID),
            TranscriptErrorCode.UPSTREAM_ERROR,
        ),
        # A non-library exception is equally upstream, just logged with a traceback.
        ("unexpected_runtime_error", RuntimeError("boom"), TranscriptErrorCode.UPSTREAM_ERROR),
    ]


@pytest.mark.parametrize(
    ("error", "code"),
    [(error, code) for _, error, code in _library_failures()],
    ids=[name for name, _, _ in _library_failures()],
)
async def test_library_error_maps_to_taxonomy(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, code: TranscriptErrorCode
) -> None:
    patch_api(monkeypatch, FakeApi(error=error))

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(VIDEO_ID)

    assert raised.value.code is code
    assert raised.value.retryable is (code in RETRYABLE_CODES)
    _assert_short_message(raised.value)


@pytest.mark.parametrize(
    ("error", "code"),
    [(error, code) for _, error, code in _library_failures()],
    ids=[name for name, _, _ in _library_failures()],
)
async def test_track_listing_maps_the_same_taxonomy(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, code: TranscriptErrorCode
) -> None:
    patch_api(monkeypatch, FakeApi(error=error))

    with pytest.raises(TranscriptError) as raised:
        await list_transcript_tracks(VIDEO_ID)

    assert raised.value.code is code


async def test_429_ip_block_is_the_retryable_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_raise_http_errors` turns HTTP 429 into `IpBlocked` (research brief B4, gotcha 11)."""
    assert issubclass(IpBlocked, RequestBlocked)  # the fold the taxonomy depends on
    patch_api(monkeypatch, FakeApi(error=IpBlocked(VIDEO_ID)))

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(VIDEO_ID)

    assert raised.value.code is TranscriptErrorCode.IP_BLOCKED
    assert raised.value.retryable is True
    _assert_short_message(raised.value)


async def test_age_restricted_message_notes_broken_cookie_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_api(monkeypatch, FakeApi(error=AgeRestricted(VIDEO_ID)))

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(VIDEO_ID)

    assert raised.value.code is TranscriptErrorCode.AGE_RESTRICTED
    assert raised.value.retryable is False
    assert "cookie auth" in raised.value.message
    _assert_short_message(raised.value)


async def test_video_unavailable_message_says_retrying_wont_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_api(monkeypatch, FakeApi(error=VideoUnavailable(VIDEO_ID)))

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(VIDEO_ID)

    assert raised.value.code is TranscriptErrorCode.UPSTREAM_ERROR
    assert "unavailable" in raised.value.message and "retrying" in raised.value.message


async def test_unmapped_library_error_maps_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unknown `CouldNotRetrieveTranscript` is upstream, and says so in the log."""
    patch_api(monkeypatch, FakeApi(error=UnmappedLibraryError(VIDEO_ID)))

    with (
        caplog.at_level("WARNING", logger="youtube_mcp.transcript.fetch"),
        pytest.raises(TranscriptError) as raised,
    ):
        await fetch_transcript(VIDEO_ID)

    assert raised.value.code is TranscriptErrorCode.UPSTREAM_ERROR
    assert "unmapped" in raised.value.message
    _assert_short_message(raised.value)
    assert any("unmapped UnmappedLibraryError" in record.message for record in caplog.records)


async def test_cookie_error_is_upstream_not_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CookieError` is outside `CouldNotRetrieveTranscript` but still an upstream failure."""
    patch_api(monkeypatch, FakeApi(error=CookieError("cookie file is unreadable")))

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(VIDEO_ID)

    assert raised.value.code is TranscriptErrorCode.UPSTREAM_ERROR
    _assert_short_message(raised.value)


async def test_request_exception_maps_to_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """`requests` connection errors escape the library unwrapped (research brief section B9)."""
    patch_api(monkeypatch, FakeApi(error=requests.ConnectionError("connection refused")))

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(VIDEO_ID)

    assert raised.value.code is TranscriptErrorCode.UPSTREAM_ERROR
    assert raised.value.retryable is True
    assert "cannot reach YouTube" in raised.value.message


async def test_truly_unexpected_error_logged_and_mapped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    patch_api(
        monkeypatch,
        FakeApi(error=RuntimeError("boom\nwith a newline and a GitHub link")),
    )

    with (
        caplog.at_level("ERROR", logger="youtube_mcp.transcript.fetch"),
        pytest.raises(TranscriptError) as raised,
    ):
        await fetch_transcript(VIDEO_ID)

    assert raised.value.code is TranscriptErrorCode.UPSTREAM_ERROR
    assert "unexpected" in raised.value.message
    _assert_short_message(raised.value)
    assert any("unexpected transcript failure" in record.message for record in caplog.records)


@pytest.mark.parametrize("bad_id", ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "short", ""])
async def test_invalid_video_id_rejected_without_a_network_call(
    monkeypatch: pytest.MonkeyPatch, bad_id: str
) -> None:
    factory = patch_api(monkeypatch, FakeApi())

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(bad_id)

    assert raised.value.code is TranscriptErrorCode.INVALID_REQUEST
    assert raised.value.retryable is False
    assert factory.constructions == []
    # The echoed input is truncated for long input, so compare against the display form.
    _assert_short_message(raised.value, bad_id[:15])


async def test_rejected_video_id_is_truncated_in_the_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 10 kB paste must not become a 10 kB error message."""
    patch_api(monkeypatch, FakeApi())
    huge = "x" * 10_000

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(huge)

    message = str(raised.value)
    assert huge not in message
    assert "x" * 15 + "…" in message
    assert len(message) < 300
    assert "\n" not in message


# --- happy paths ---------------------------------------------------------------------------


async def test_fetch_returns_the_full_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi(result=fetched(("hello", 0.0, 1.5), ("world", 1.5, 0.0)))
    patch_api(monkeypatch, api)

    transcript = await fetch_transcript(VIDEO_ID, languages=["pt", "en"])

    assert isinstance(transcript, Transcript)
    assert (transcript.video_id, transcript.language, transcript.language_code) == (
        VIDEO_ID,
        "English",
        "en",
    )
    assert transcript.is_generated is False
    assert [(s.text, s.start, s.duration) for s in transcript.snippets] == [
        ("hello", 0.0, 1.5),
        ("world", 1.5, 0.0),  # duration is 0.0 when YouTube omits `dur` (research brief section B2)
    ]
    assert api.fetch_calls == [(VIDEO_ID, ("pt", "en"), False)]


async def test_language_defaults_and_preserve_formatting_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fetch_module, "get_settings", lambda: Settings(youtube_transcript_lang="de")
    )
    api = FakeApi(result=fetched(("hallo", 0.0, 1.0)))
    patch_api(monkeypatch, api)

    await fetch_transcript(VIDEO_ID, preserve_formatting=True)

    assert api.fetch_calls == [(VIDEO_ID, ("de",), True)]


async def test_list_tracks_maps_metadata_and_translation_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi(
        list_result=[
            FakeTrack("English", "en", translatable_to=["pt", "es"]),
            FakeTrack("Portuguese", "pt", is_generated=True),
        ]
    )
    patch_api(monkeypatch, api)

    tracks = await list_transcript_tracks(VIDEO_ID)

    assert isinstance(tracks, TranscriptTrackList)
    assert tracks.video_id == VIDEO_ID
    assert [
        (t.language, t.language_code, t.is_generated, t.is_translatable) for t in tracks.tracks
    ] == [
        ("English", "en", False, True),
        ("Portuguese", "pt", True, False),
    ]
    assert tracks.tracks[0].translatable_to == ["pt", "es"]
    assert tracks.tracks[1].translatable_to == []
    assert api.list_calls == [VIDEO_ID]


async def test_transcript_model_is_json_persistable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache round-trips `model_dump()`; the model must validate back cleanly."""
    patch_api(monkeypatch, FakeApi(result=fetched(("hi", 0.0, 1.0))))
    transcript = await fetch_transcript(VIDEO_ID)
    assert Transcript.model_validate(transcript.model_dump()) == transcript
    assert (
        TranscriptTrackList.model_validate(
            TranscriptTrackList(video_id=VIDEO_ID, tracks=[]).model_dump()
        ).tracks
        == []
    )


# --- translation --------------------------------------------------------------------------


async def test_translate_maps_provenance_and_the_target_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi(
        list_result=[
            FakeTrack(
                "English",
                "en",
                translatable_to=["de"],
                translated_snippets=(("hallo", 0.0, 1.5),),
            )
        ]
    )
    patch_api(monkeypatch, api)

    transcript = await fetch_translated_transcript(VIDEO_ID, "de", ["en"])

    assert (transcript.video_id, transcript.language_code) == (VIDEO_ID, "de")
    assert transcript.is_translated is True
    assert transcript.translated_from == "en"
    # The library reports a translation as generated even off a hand-uploaded track.
    assert transcript.is_generated is True
    assert [(s.text, s.start, s.duration) for s in transcript.snippets] == [("hallo", 0.0, 1.5)]
    assert api.list_calls == [VIDEO_ID]
    assert api.fetch_calls == []  # the untranslated track was never fetched


async def test_translate_prefers_the_first_source_language_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi(
        list_result=[
            FakeTrack("German", "de", translatable_to=["en"]),
            FakeTrack("English", "en", translatable_to=["de"]),
        ]
    )
    patch_api(monkeypatch, api)

    transcript = await fetch_translated_transcript(VIDEO_ID, "en", ["de", "en"])

    assert transcript.translated_from == "de"
    assert transcript.language_code == "en"


async def test_en_to_en_is_served_but_not_claimed_as_a_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target == source track language: served (YouTube allows it) but no provenance claim."""
    patch_api(
        monkeypatch, FakeApi(list_result=[FakeTrack("English", "en", translatable_to=["en"])])
    )

    transcript = await fetch_translated_transcript(VIDEO_ID, "en", ["en"])

    assert transcript.language_code == "en"
    assert transcript.is_translated is False
    assert transcript.translated_from is None


async def test_translation_language_not_available_is_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_api(
        monkeypatch, FakeApi(list_result=[FakeTrack("English", "en", translatable_to=["de"])])
    )

    with pytest.raises(TranscriptError) as raised:
        await fetch_translated_transcript(VIDEO_ID, "fr", ["en"])

    assert raised.value.code is TranscriptErrorCode.INVALID_REQUEST
    assert raised.value.retryable is False
    assert "youtube_list_transcript_languages" in raised.value.message
    _assert_short_message(raised.value)


async def test_untranslatable_track_is_invalid_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A track with no translation languages at all: terminal, not a retryable upstream blip."""
    patch_api(monkeypatch, FakeApi(list_result=[FakeTrack("English", "en")]))

    with pytest.raises(TranscriptError) as raised:
        await fetch_translated_transcript(VIDEO_ID, "de", ["en"])

    assert raised.value.code is TranscriptErrorCode.INVALID_REQUEST
    assert raised.value.retryable is False
    assert "cannot be translated" in raised.value.message
    _assert_short_message(raised.value)


async def test_translated_fetch_times_out_like_the_native_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_module, "TRANSCRIPT_TIMEOUT_SECONDS", 0.05)
    patch_api(
        monkeypatch,
        FakeApi(list_result=[FakeTrack("English", "en", translatable_to=["de"])], delay=0.3),
    )

    with pytest.raises(TranscriptError) as raised:
        await fetch_translated_transcript(VIDEO_ID, "de", ["en"])

    assert raised.value.code is TranscriptErrorCode.UPSTREAM_ERROR
    assert "timed out" in raised.value.message


async def test_translated_transcript_is_cached_forever_under_its_own_key(
    monkeypatch: pytest.MonkeyPatch, opened_cache: Cache
) -> None:
    api = FakeApi(list_result=[FakeTrack("English", "en", translatable_to=["de"])])
    patch_api(monkeypatch, api)

    first = await fetch_translated_transcript(VIDEO_ID, "de", ["en"], cache=opened_cache)
    second = await fetch_translated_transcript(VIDEO_ID, "de", ["en"], cache=opened_cache)

    assert first == second
    assert api.list_calls == [VIDEO_ID]  # one listing, both calls
    stored = await opened_cache.get("transcript:dQw4w9WgXcQ:en:translated:de")
    assert stored["is_translated"] is True
    assert stored["translated_from"] == "en"
    assert await opened_cache.purge_expired() == 0  # ttl=0: never expires


async def test_translated_entry_never_satisfies_a_native_request(
    monkeypatch: pytest.MonkeyPatch, opened_cache: Cache
) -> None:
    """The correctness core: a translated en->de payload must not answer a native `de` call."""
    translated_api = FakeApi(
        list_result=[
            FakeTrack(
                "English",
                "en",
                translatable_to=["de"],
                translated_snippets=(("hello world", 0.0, 1.0),),
            )
        ]
    )
    native_api = FakeApi(result=fetched(("hallo welt", 0.0, 1.0), language_code="de"))
    patch_api(monkeypatch, translated_api, native_api)

    translated = await fetch_translated_transcript(VIDEO_ID, "de", ["en"], cache=opened_cache)

    assert translated.snippets[0].text == "hello world"
    # Nothing was written into the native `transcript:<id>:<lang>` namespace.
    assert await opened_cache.get("transcript:dQw4w9WgXcQ:de") is None

    native = await fetch_transcript(VIDEO_ID, languages=["de"], cache=opened_cache)

    assert native_api.fetch_calls == [(VIDEO_ID, ("de",), False)]  # a real fetch happened
    assert native.snippets[0].text == "hallo welt"
    assert native.is_translated is False
    assert native.translated_from is None


async def test_native_entry_never_satisfies_a_translated_request(
    monkeypatch: pytest.MonkeyPatch, opened_cache: Cache
) -> None:
    """The other direction: a native `de` entry must not short-circuit a de-translation."""
    native_api = FakeApi(result=fetched(("hallo welt", 0.0, 1.0), language_code="de"))
    translated_api = FakeApi(list_result=[FakeTrack("English", "en", translatable_to=["de"])])
    patch_api(monkeypatch, native_api, translated_api)

    await fetch_transcript(VIDEO_ID, languages=["de"], cache=opened_cache)

    translated = await fetch_translated_transcript(VIDEO_ID, "de", ["en"], cache=opened_cache)

    assert translated_api.list_calls == [VIDEO_ID]  # listed and translated, not read back
    assert translated.is_translated is True
    assert translated.translated_from == "en"
    assert translated.snippets[0].text == "translated"
    # Both entries coexist under distinct keys.
    assert await opened_cache.get("transcript:dQw4w9WgXcQ:de") is not None
    assert await opened_cache.get("transcript:dQw4w9WgXcQ:en:translated:de") is not None


async def test_translated_cache_hit_under_a_later_source_language_code(
    monkeypatch: pytest.MonkeyPatch, opened_cache: Cache
) -> None:
    """The source track is always one of the requested codes, so a hit needs no listing."""
    api = FakeApi(
        list_result=[
            FakeTrack("German", "de", translatable_to=["en"]),
            FakeTrack("English", "en", translatable_to=["de"]),
        ]
    )
    patch_api(monkeypatch, api)

    await fetch_translated_transcript(VIDEO_ID, "en", ["de"], cache=opened_cache)
    again = await fetch_translated_transcript(VIDEO_ID, "en", ["en", "de"], cache=opened_cache)

    assert again.translated_from == "de"
    assert api.list_calls == [VIDEO_ID]


# --- timeout + offload (section 7.1) ----------------------------------------------------------


async def test_timeout_fires_and_no_long_sleep_is_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as _time

    monkeypatch.setattr(fetch_module, "TRANSCRIPT_TIMEOUT_SECONDS", 0.05)
    patch_api(monkeypatch, FakeApi(result=fetched(), delay=0.3))

    started = _time.monotonic()
    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(VIDEO_ID)
    elapsed = _time.monotonic() - started

    assert raised.value.code is TranscriptErrorCode.UPSTREAM_ERROR
    assert raised.value.retryable is True
    assert "timed out" in raised.value.message
    assert elapsed < 0.25, f"deadline did not fire: waited {elapsed:.3f}s"
    _assert_short_message(raised.value)


async def test_track_listing_times_out_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_module, "TRANSCRIPT_TIMEOUT_SECONDS", 0.05)
    patch_api(monkeypatch, FakeApi(delay=0.3))

    with pytest.raises(TranscriptError) as raised:
        await list_transcript_tracks(VIDEO_ID)

    assert raised.value.code is TranscriptErrorCode.UPSTREAM_ERROR
    assert "timed out" in raised.value.message


async def test_fetch_is_offloaded_so_the_loop_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tick while the blocking fetch is in flight; a loop-blocking implementation ticks zero."""
    patch_api(monkeypatch, FakeApi(result=fetched(("hi", 0.0, 1.0)), delay=0.08))

    ticks = 0
    fetch_done = anyio.Event()
    result: list[Transcript] = []

    async def do_fetch() -> None:
        result.append(await fetch_transcript(VIDEO_ID, languages=["en"]))
        fetch_done.set()

    async def spin() -> None:
        nonlocal ticks
        while not fetch_done.is_set() and ticks < 1000:
            ticks += 1
            await anyio.sleep(0.005)

    async with anyio.create_task_group() as tg:
        tg.start_soon(do_fetch)
        tg.start_soon(spin)
        await fetch_done.wait()
        assert ticks > 0  # counted while the offloaded fetch was still in flight

    assert result[0].snippets[0].text == "hi"


async def test_fresh_api_instance_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instances own a `requests.Session` and are not thread-safe — one per call (brief B1)."""
    factory = patch_api(monkeypatch, FakeApi(result=fetched()), FakeApi(result=fetched()))

    await fetch_transcript(VIDEO_ID)
    await fetch_transcript(VIDEO_ID)
    await list_transcript_tracks(VIDEO_ID)

    assert len(factory.constructions) == 3
    assert all((kwargs.get("proxy_config", None) is None) for kwargs in factory.constructions)


async def test_concurrent_fetches_do_not_share_an_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[FakeApi] = []

    def make_api(**_kwargs: Any) -> FakeApi:
        api = FakeApi(result=fetched(), delay=0.03)
        instances.append(api)
        return api

    monkeypatch.setattr(fetch_module, "YouTubeTranscriptApi", make_api)

    async with anyio.create_task_group() as tg:
        tg.start_soon(fetch_transcript, VIDEO_ID)
        tg.start_soon(fetch_transcript, VIDEO_ID)

    assert len(instances) == 2
    assert all(len(api.fetch_calls) == 1 for api in instances)


async def test_external_cancellation_is_never_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our own deadline maps to an error; a caller's cancellation must stay a cancellation."""
    patch_api(monkeypatch, FakeApi(result=fetched(), delay=0.5))
    in_flight = anyio.Event()
    outcomes: list[BaseException | None] = []

    async def do_fetch() -> None:
        in_flight.set()
        try:
            await fetch_transcript(VIDEO_ID)
        except BaseException as exc:  # noqa: BLE001 - we want to see exactly what surfaced
            outcomes.append(exc)
        else:
            outcomes.append(None)

    async with anyio.create_task_group() as tg:
        tg.start_soon(do_fetch)
        await in_flight.wait()
        await anyio.sleep(0.05)  # let the offload reach the blocking call
        tg.cancel_scope.cancel()

    assert len(outcomes) == 1
    assert not isinstance(outcomes[0], TranscriptError)
    assert isinstance(outcomes[0], anyio.get_cancelled_exc_class())


# --- caching (section 14) -------------------------------------------------------------------------


async def test_transcript_cached_and_second_call_skips_the_library(
    monkeypatch: pytest.MonkeyPatch, opened_cache: Cache
) -> None:
    api = FakeApi(result=fetched(("hi", 0.0, 1.0)))
    factory = patch_api(monkeypatch, api)

    first = await fetch_transcript(VIDEO_ID, languages=["en"], cache=opened_cache)
    second = await fetch_transcript(VIDEO_ID, languages=["en"], cache=opened_cache)

    assert first == second
    assert len(api.fetch_calls) == 1
    assert len(factory.constructions) == 1
    stored = await opened_cache.get("transcript:dQw4w9WgXcQ:en")
    assert stored["snippets"][0]["text"] == "hi"
    assert await opened_cache.purge_expired() == 0  # ttl=0: never expires


async def test_cache_hit_under_a_later_language_code(
    monkeypatch: pytest.MonkeyPatch, opened_cache: Cache
) -> None:
    """A cached fallback-language transcript must satisfy a request that also lists it."""
    api = FakeApi(result=fetched(("olá", 0.0, 1.0), language_code="pt"))
    patch_api(monkeypatch, api)

    await fetch_transcript(VIDEO_ID, languages=["pt"], cache=opened_cache)
    again = await fetch_transcript(VIDEO_ID, languages=["en", "pt"], cache=opened_cache)

    assert again.language_code == "pt"
    assert len(api.fetch_calls) == 1


async def test_styled_variant_is_not_served_from_a_default_variant_cache_hit(
    monkeypatch: pytest.MonkeyPatch, opened_cache: Cache
) -> None:
    """`preserve_formatting=True` returns different text, so it needs its own cache entry."""
    default_api = FakeApi(result=fetched(("hi", 0.0, 1.0)))
    styled_api = FakeApi(result=fetched(("hi\n\n", 0.0, 1.0)))
    patch_api(monkeypatch, default_api, styled_api)

    await fetch_transcript(VIDEO_ID, languages=["en"], cache=opened_cache)
    styled = await fetch_transcript(
        VIDEO_ID, languages=["en"], preserve_formatting=True, cache=opened_cache
    )

    # The stripped entry must not satisfy the styled call: a second library call happened.
    assert default_api.fetch_calls == [(VIDEO_ID, ("en",), False)]
    assert styled_api.fetch_calls == [(VIDEO_ID, ("en",), True)]
    assert styled.snippets[0].text == "hi\n\n"
    assert await opened_cache.get("transcript:dQw4w9WgXcQ:en:styled") is not None

    # And the styled entry is what a second styled call reads back.
    again = await fetch_transcript(
        VIDEO_ID, languages=["en"], preserve_formatting=True, cache=opened_cache
    )
    assert again == styled
    assert styled_api.fetch_calls == [(VIDEO_ID, ("en",), True)]


async def test_tracks_are_cached(monkeypatch: pytest.MonkeyPatch, opened_cache: Cache) -> None:
    api = FakeApi(list_result=[FakeTrack("English", "en")])
    patch_api(monkeypatch, api)

    await list_transcript_tracks(VIDEO_ID, cache=opened_cache)
    await list_transcript_tracks(VIDEO_ID, cache=opened_cache)

    assert api.list_calls == [VIDEO_ID]
    stored = await opened_cache.get("transcript_tracks:dQw4w9WgXcQ")
    assert stored["tracks"][0]["language_code"] == "en"


async def test_errors_are_not_cached(monkeypatch: pytest.MonkeyPatch, opened_cache: Cache) -> None:
    """A failed fetch must not poison the cache with an empty transcript."""
    patch_api(monkeypatch, FakeApi(error=TranscriptsDisabled(VIDEO_ID)))

    with pytest.raises(TranscriptError):
        await fetch_transcript(VIDEO_ID, cache=opened_cache)

    assert await opened_cache.get("transcript:dQw4w9WgXcQ:en") is None


async def test_cacheless_calls_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two instances queued: each call constructs its own API (fresh instance per call).
    first, second = FakeApi(result=fetched(("hi", 0.0, 1.0))), FakeApi(list_result=[])
    patch_api(monkeypatch, first, second)

    await fetch_transcript(VIDEO_ID)
    await list_transcript_tracks(VIDEO_ID)

    assert len(first.fetch_calls) == 1
    assert len(second.list_calls) == 1


# --- proxy wiring (section 9, research brief B6) ----------------------------------------------


def test_build_proxy_config_is_none_by_default() -> None:
    assert build_proxy_config(Settings()) is None


def test_build_proxy_config_webshare_wins_when_both_are_set() -> None:
    config = build_proxy_config(
        Settings(
            webshare_proxy_username="dreadster",
            webshare_proxy_password="s3cret",
            http_proxy="http://generic.example:8080",
        )
    )

    assert isinstance(config, WebshareProxyConfig)
    assert config.retries_when_blocked == 10
    url = config.to_requests_dict()["https"]
    assert url.startswith("http://dreadster-PT-ES-rotate:s3cret@")
    assert "p.webshare.io" in url


def test_build_proxy_config_warns_on_a_partial_webshare_pair(caplog) -> None:
    """One credential alone silently degrades to a direct connection — say so."""
    with caplog.at_level("WARNING", logger="youtube_mcp.transcript.fetch"):
        assert build_proxy_config(Settings(webshare_proxy_username="dreadster")) is None
        assert build_proxy_config(Settings(webshare_proxy_password="s3cret")) is None

    assert sum("webshare" in record.message for record in caplog.records) == 2


def test_build_proxy_config_is_silent_when_webshare_is_absent_or_complete(caplog) -> None:
    with caplog.at_level("WARNING", logger="youtube_mcp.transcript.fetch"):
        assert build_proxy_config(Settings()) is None
        build_proxy_config(
            Settings(webshare_proxy_username="dreadster", webshare_proxy_password="s3cret")
        )

    assert caplog.records == []


def test_build_proxy_config_generic_http_only() -> None:
    config = build_proxy_config(Settings(http_proxy="http://proxy.example:3128"))

    assert isinstance(config, GenericProxyConfig)
    assert not isinstance(config, WebshareProxyConfig)
    assert config.to_requests_dict() == {
        "http": "http://proxy.example:3128",
        "https": "http://proxy.example:3128",
    }
    assert config.retries_when_blocked == 0  # no built-in retry doubling our own


def test_build_proxy_config_generic_https_only() -> None:
    config = build_proxy_config(Settings(https_proxy="http://proxy.example:3128"))

    assert config.to_requests_dict() == {
        "http": "http://proxy.example:3128",
        "https": "http://proxy.example:3128",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"webshare_proxy_username": "u", "webshare_proxy_password": "p"},
        {"http_proxy": "http://proxy.example:3128"},
    ],
)
async def test_constructed_api_receives_the_proxy_config(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, str]
) -> None:
    monkeypatch.setattr(fetch_module, "get_settings", lambda: Settings(**kwargs))
    factory = patch_api(monkeypatch, FakeApi(result=fetched()))

    await fetch_transcript(VIDEO_ID)

    assert isinstance(factory.constructions[0]["proxy_config"], ProxyConfig)


async def test_proxy_is_not_leaked_into_transcripts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy secret must not surface in any returned model or error message."""
    monkeypatch.setattr(
        fetch_module,
        "get_settings",
        lambda: Settings(webshare_proxy_username="dreadster", webshare_proxy_password="s3cret"),
    )
    patch_api(monkeypatch, FakeApi(error=RequestBlocked(VIDEO_ID)))

    with pytest.raises(TranscriptError) as raised:
        await fetch_transcript(VIDEO_ID)

    assert "s3cret" not in str(raised.value)
    assert re.search(r"\d+\.\d+\.\d+\.\d+", str(raised.value)) is None


def test_timeout_default_is_30_seconds() -> None:
    assert TRANSCRIPT_TIMEOUT_SECONDS == 30.0


def test_library_shape_is_1x_instance_methods() -> None:
    """Guard the 1.x API shape: no `list_transcripts`, methods are instance methods (B9)."""
    assert not hasattr(YouTubeTranscriptApi, "list_transcripts")
    assert not hasattr(YouTubeTranscriptApi, "get_transcript")
    assert callable(YouTubeTranscriptApi.fetch)
    assert callable(YouTubeTranscriptApi.list)
