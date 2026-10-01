"""Model tests: exact wire shapes, tolerant parsing, no dislike counts.

Fixtures are the recorded JSON in `tests/fixtures/` — same files the client tests use, so
a shape change breaks both in one place.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import fixture
from youtube_mcp.youtube import models
from youtube_mcp.youtube.models import (
    BatchStatsResponse,
    Channel,
    CommentThreadPage,
    PlaylistItemPage,
    SearchResults,
    Video,
    VideoCategory,
    VideoStats,
    parse_duration_seconds,
    to_int,
)


def test_to_int_tolerates_strings_ints_and_garbage():
    assert to_int("1620000000") == 1620000000
    assert to_int(42) == 42
    assert to_int(None) is None
    assert to_int("") is None
    assert to_int("not a number") is None
    assert to_int(object()) is None


@pytest.mark.parametrize(
    ("duration", "millis", "expected"),
    [
        ("213s", 213000, 213),  # protobuf form + millis: millis wins
        ("213s", None, 213),  # protobuf seconds (batchGetStats)
        ("PT3M33S", None, 213),  # ISO-8601 (videos.list)
        ("PT1H2M3S", None, 3723),
        ("PT45S", None, 45),
        ("P1DT1S", None, 86401),
        (None, 596000, 596),  # millis only
        (None, "596000", 596),  # millis as a string
        (300, None, 300),  # already a number
        ("PT3M33.5S", None, 213),  # fractional seconds truncated to whole seconds
        ("garbage", None, None),
        (None, None, None),
        ("", None, None),
        ("PT", None, 0),
    ],
)
def test_parse_duration_seconds_normalizes_every_wire_form(duration, millis, expected):
    assert parse_duration_seconds(duration, duration_millis=millis) == expected


def test_search_results_shape():
    results = SearchResults.from_api(fixture("search_list.json"))

    assert isinstance(results, SearchResults)
    assert results.next_page_token == "CAUQAA"
    assert len(results.items) == 2
    first = results.items[0]
    assert first.video_id == "dQw4w9WgXcQ"  # read from id.videoId, not id
    assert first.channel_title == "Rick Astley"
    assert first.published_at == datetime(2009, 10, 25, 6, 57, 33, tzinfo=UTC)


def test_search_result_without_next_page_token_has_none():
    results = SearchResults.from_api({"items": []})

    assert results.next_page_token is None
    assert results.items == []


def test_search_result_tolerates_string_id():
    """`search.list` returns an object id, but be defensive if a backend sends a string."""
    result = SearchResults.from_api({"items": [{"id": "dQw4w9WgXcQ", "snippet": {}}]})

    assert result.items[0].video_id == "dQw4w9WgXcQ"
    assert result.items[0].title == ""


def test_channel_shape_including_uploads_playlist():
    channel = Channel.from_api(fixture("channels_by_id.json")["items"][0])

    assert channel.channel_id == "UCBR8-60-B28hp2BmDPdntcQ"
    assert channel.title == "Google Developers"
    assert channel.custom_url == "@GoogleDevelopers"
    assert channel.country == "US"
    assert channel.subscriber_count == 2600000
    assert channel.video_count == 5900
    assert channel.view_count == 287000000
    assert channel.hidden_subscriber_count is False
    # The whole point of contentDetails: the cheap channel→videos path (section 5.3).
    assert channel.uploads_playlist_id == "UUx7I4zH7kz2Q4tU9mB0pX8aLw1Q"


def test_channel_tolerates_empty_statistics():
    channel = Channel.from_api({"id": "UC1", "snippet": {"title": "x"}})

    assert channel.subscriber_count is None
    assert channel.hidden_subscriber_count is False
    assert channel.uploads_playlist_id is None


def test_playlist_item_distinguishes_item_id_from_video_id():
    page = PlaylistItemPage.from_api(fixture("playlist_items.json"))

    assert page.next_page_token == "CAEQAA"
    item = page.items[0]
    assert item.item_id == "UExYYWJjZGVm_abCDEFG"  # playlist-item namespace
    assert item.video_id == "dQw4w9WgXcQ"  # from snippet.resourceId.videoId
    assert item.item_id != item.video_id
    assert item.position == 0
    assert item.video_published_at == datetime(2024, 4, 30, 16, 0, tzinfo=UTC)
    assert item.published_at == datetime(2024, 5, 1, 12, 34, 56, tzinfo=UTC)


def test_playlist_item_without_resource_id_is_not_an_error():
    page = PlaylistItemPage.from_api({"items": [{"id": "PI1", "snippet": {"title": "t"}}]})

    assert page.items[0].video_id is None
    assert page.items[0].item_id == "PI1"


def test_video_shape_from_videos_list():
    raw = {
        "id": "dQw4w9WgXcQ",
        "snippet": {
            "title": "Rick Astley",
            "description": "d",
            "channelId": "UC1",
            "channelTitle": "Rick Astley",
            "publishedAt": "2009-10-25T06:57:33Z",
        },
        "statistics": {"viewCount": "1", "likeCount": "2", "commentCount": "3"},
        "contentDetails": {"duration": "PT3M33S"},
    }

    video = Video.from_api(raw)

    assert video.duration_seconds == 213
    assert (video.view_count, video.like_count, video.comment_count) == (1, 2, 3)
    assert video.published_at == datetime(2009, 10, 25, 6, 57, 33, tzinfo=UTC)


def test_batch_stats_shape():
    response = BatchStatsResponse.from_api(fixture("batch_get_stats.json"))

    assert len(response.items) == 2
    stats = response.items[0]
    assert isinstance(stats, VideoStats)
    assert stats.video_id == "dQw4w9WgXcQ"
    assert stats.view_count == 1620000000
    assert stats.like_count == 17000000
    assert stats.comment_count == 2400000
    assert stats.duration_seconds == 213
    assert stats.published_at == datetime(2009, 10, 25, 6, 57, 33, tzinfo=UTC)
    assert response.summary.failed_video_count == 0


def test_batch_stats_partial_failure_exposes_both_sides():
    """A missing ID is data, not an exception (research A1.2)."""
    response = BatchStatsResponse.from_api(fixture("batch_get_stats_partial_failure.json"))

    assert [item.video_id for item in response.items] == ["dQw4w9WgXcQ", "aqz-KE-bpKQ"]
    assert response.summary.requested_video_count == 3
    assert response.summary.succeeded_video_count == 2
    assert response.summary.failed_video_count == 1
    assert response.summary.failed_video_ids == ["zzz_missing_id"]
    # items and failedVideoIds never overlap.
    assert not set(response.summary.failed_video_ids) & {i.video_id for i in response.items}


def test_batch_stats_defaults_when_summary_missing():
    """Discovery rev 20260924 omits `summary`; a body without it must still parse."""
    response = BatchStatsResponse.from_api({"items": []})

    assert response.summary.requested_video_count == 0
    assert response.summary.failed_video_ids == []


def test_batch_stats_summary_tolerates_string_counts():
    response = BatchStatsResponse.from_api(
        {
            "items": [],
            "summary": {
                "requestedVideoCount": "3",
                "succeededVideoCount": "2",
                "failedVideoCount": "1",
                "failedVideoIds": ["x"],
            },
        }
    )

    assert response.summary.requested_video_count == 3
    assert response.summary.failed_video_count == 1


def test_comment_thread_page_shape():
    page = CommentThreadPage.from_api(fixture("comment_threads.json"))

    assert page.next_page_token == "Qg8QAA"
    assert len(page.items) == 2
    thread = page.items[0]
    assert thread.thread_id == "UgxKZ0nQ_1a2b3c4d5e6f7g8"
    assert thread.comment.comment_id == "UgxKZ0nQ_1a2b3c4d5e6f7g8"
    assert thread.comment.text == "Best song ever written."
    assert thread.comment.author_name == "Alice"
    assert thread.comment.author_channel_id == "UC_alice_channel_id"
    assert thread.comment.like_count == 42
    assert thread.total_reply_count == 3


def test_comment_thread_uses_total_reply_count_not_reply_array_length():
    """The `replies` part is truncated; `totalReplyCount` is the count (research A6)."""
    raw = {
        "id": "T1",
        "snippet": {
            "totalReplyCount": 500,
            "topLevelComment": {"id": "C1", "snippet": {"textDisplay": "hi"}},
        },
        "replies": {"comments": [{"id": "R1"}, {"id": "R2"}]},
    }

    thread = CommentThreadPage.from_api({"items": [raw]}).items[0]

    assert thread.total_reply_count == 500
    assert len(raw["replies"]["comments"]) == 2


def test_comment_without_author_channel_or_likes_defaults():
    page = CommentThreadPage.from_api(
        {"items": [{"id": "T1", "snippet": {"topLevelComment": {"id": "C1", "snippet": {}}}}]}
    )

    comment = page.items[0].comment
    assert comment.author_channel_id is None
    assert comment.like_count == 0
    assert comment.text == ""
    assert comment.author_name is None


def test_comment_handles_author_channel_id_as_string():
    page = CommentThreadPage.from_api(
        {
            "items": [
                {
                    "id": "T1",
                    "snippet": {
                        "topLevelComment": {
                            "id": "C1",
                            "snippet": {"authorChannelId": "UC1"},
                        }
                    },
                }
            ]
        }
    )

    assert page.items[0].comment.author_channel_id is None


def test_video_categories_shape():
    categories = models.video_categories_from_api(fixture("video_categories.json"))

    assert [c.category_id for c in categories] == ["1", "10", "44"]
    assert categories[1].title == "Music"
    assert categories[1].assignable is True
    assert categories[2].assignable is False
    assert all(isinstance(c, VideoCategory) for c in categories)


def test_videos_and_channels_list_helpers():
    assert models.videos_from_api({"items": []}) == []
    assert models.channels_from_api({"items": []}) == []
    assert models.video_categories_from_api({}) == []


def test_unparseable_datetimes_become_none_not_errors():
    channel = Channel.from_api({"id": "UC1", "snippet": {"publishedAt": "not-a-date"}})

    assert channel.published_at is None


def test_items_skips_non_mapping_entries():
    """Defensive: a null/garbage entry must not blow up a whole page."""
    page = SearchResults.from_api({"items": [None, "garbage", {"id": {"videoId": "v1"}}]})

    assert [item.video_id for item in page.items] == ["v1"]


@pytest.mark.parametrize(
    "model",
    [
        SearchResults,
        Channel,
        PlaylistItemPage,
        Video,
        VideoStats,
        BatchStatsResponse,
        CommentThreadPage,
        VideoCategory,
    ],
)
def test_no_model_exposes_dislike_count(model):
    """dislikeCount is private since 2021 (section 5.2) — never model it, never infer it."""
    fields = set(model.model_json_schema().get("properties", {}))
    assert not [field for field in fields if "dislike" in field.lower()]
    assert not [field for field in model.model_fields if "dislike" in field.lower()]
