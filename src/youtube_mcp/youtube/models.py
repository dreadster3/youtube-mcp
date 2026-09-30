"""Pydantic models for the YouTube Data API responses we consume.

Shapes follow `.pi/research/youtube-apis.md` (the [DOC] pages, not the discovery
document — discovery rev 20260924 is missing `summary` on `BatchGetStatsResponse`).
Each model exposes `from_api(raw)` for the wire shape; the model itself is flat so the
tool layer reads one spelling of each field.

Parsing is deliberately tolerant: YouTube encodes counts as JSON strings, and duration
arrives as protobuf seconds (`"213s"`), ISO-8601 (`"PT3M33S"`) or `durationMillis`, so a
single odd field never fails a whole response.

No `dislikeCount` anywhere — private since 2021-12-13 (§5.2).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Self, TypeVar

from pydantic import BaseModel, Field

__all__ = [
    "BatchStatsResponse",
    "BatchStatsSummary",
    "Channel",
    "Comment",
    "CommentThread",
    "CommentThreadPage",
    "PlaylistItem",
    "PlaylistItemPage",
    "SearchResult",
    "SearchResults",
    "Video",
    "VideoCategory",
    "VideoStats",
    "parse_duration_seconds",
    "to_int",
]

T = TypeVar("T")

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_PROTOBUF_SECONDS = re.compile(r"^(?P<seconds>\d+(?:\.\d+)?)s$")


def to_int(value: Any) -> int | None:
    """Tolerate YouTube's string-encoded counts (`"1620000000"`) and plain ints."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_duration_seconds(
    duration: Any = None, *, duration_millis: Any = None
) -> int | None:
    """Normalize a video duration to whole seconds.

    `durationMillis` wins when present (it is a plain integer); otherwise `duration` is
    accepted as protobuf seconds (`"213s"`) or ISO-8601 (`"PT3M33S"`). Unknown formats
    return `None` rather than raising.
    """
    millis = to_int(duration_millis)
    if millis is not None:
        return millis // 1000

    if duration is None:
        return None
    if isinstance(duration, (int, float)):
        return int(duration)

    text = str(duration).strip()
    protobuf = _PROTOBUF_SECONDS.match(text)
    if protobuf:
        return int(float(protobuf.group("seconds")))

    iso = _ISO_DURATION.match(text)
    if not iso:
        return None
    parts = {name: float(value or 0) for name, value in iso.groupdict().items()}
    total = (
        parts["days"] * 86_400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )
    return int(total)


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _items(raw: Mapping[str, Any], factory: Callable[[Mapping[str, Any]], T]) -> list[T]:
    return [factory(item) for item in raw.get("items") or [] if isinstance(item, Mapping)]


def _video_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the `snippet`/`statistics`/`contentDetails` triple of a video resource.

    Used for both `videos.list` and `videos:batchGetStats` items, which nest the same
    properties under different wrappers.
    """
    snippet = raw.get("snippet") or {}
    statistics = raw.get("statistics") or {}
    details = raw.get("contentDetails") or {}
    return {
        "title": _as_text(snippet.get("title")),
        "description": _as_text(snippet.get("description")),
        "channel_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle"),
        "published_at": _as_datetime(snippet.get("publishTime") or snippet.get("publishedAt")),
        "view_count": to_int(statistics.get("viewCount")),
        "like_count": to_int(statistics.get("likeCount")),
        "comment_count": to_int(statistics.get("commentCount")),
        "duration_seconds": parse_duration_seconds(
            details.get("duration"), duration_millis=details.get("durationMillis")
        ),
    }


class SearchResult(BaseModel):
    """One `searchResult` item. `id` is a `{kind, videoId}` object, not a string."""

    video_id: str
    title: str = ""
    description: str = ""
    channel_id: str | None = None
    channel_title: str | None = None
    published_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        resource_id = raw.get("id")
        if isinstance(resource_id, Mapping):
            video_id = resource_id.get("videoId") or ""
        else:
            video_id = _as_text(resource_id)
        snippet = raw.get("snippet") or {}
        return cls(
            video_id=video_id,
            title=_as_text(snippet.get("title")),
            description=_as_text(snippet.get("description")),
            channel_id=snippet.get("channelId"),
            channel_title=snippet.get("channelTitle"),
            published_at=_as_datetime(snippet.get("publishedAt")),
        )


class SearchResults(BaseModel):
    """A page of `search.list` results. Paginate on `next_page_token`, never on totals."""

    next_page_token: str | None = None
    items: list[SearchResult] = Field(default_factory=list)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        return cls(
            next_page_token=raw.get("nextPageToken"),
            items=_items(raw, SearchResult.from_api),
        )


class Channel(BaseModel):
    """A `channels.list` item. `uploads_playlist_id` is the cheap path to channel videos."""

    channel_id: str
    title: str = ""
    description: str = ""
    custom_url: str | None = None
    published_at: datetime | None = None
    country: str | None = None
    subscriber_count: int | None = None
    video_count: int | None = None
    view_count: int | None = None
    hidden_subscriber_count: bool = False
    uploads_playlist_id: str | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        snippet = raw.get("snippet") or {}
        statistics = raw.get("statistics") or {}
        playlists = ((raw.get("contentDetails") or {}).get("relatedPlaylists")) or {}
        return cls(
            channel_id=_as_text(raw.get("id")),
            title=_as_text(snippet.get("title")),
            description=_as_text(snippet.get("description")),
            custom_url=snippet.get("customUrl"),
            published_at=_as_datetime(snippet.get("publishedAt")),
            country=snippet.get("country"),
            subscriber_count=to_int(statistics.get("subscriberCount")),
            video_count=to_int(statistics.get("videoCount")),
            view_count=to_int(statistics.get("viewCount")),
            hidden_subscriber_count=bool(statistics.get("hiddenSubscriberCount", False)),
            uploads_playlist_id=playlists.get("uploads"),
        )


class PlaylistItem(BaseModel):
    """One `playlistItems.list` item.

    `item_id` is the *playlist-item* ID — a different namespace from `video_id`, which
    lives at `snippet.resourceId.videoId` (there is no `contentDetails` part on
    playlistItems; `video_published_at` arrives implicitly alongside `snippet`).
    """

    item_id: str
    video_id: str | None = None
    title: str = ""
    description: str = ""
    channel_id: str | None = None
    channel_title: str | None = None
    playlist_id: str | None = None
    position: int | None = None
    published_at: datetime | None = None
    video_published_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        snippet = raw.get("snippet") or {}
        resource_id = snippet.get("resourceId") or {}
        details = raw.get("contentDetails") or {}
        return cls(
            item_id=_as_text(raw.get("id")),
            video_id=resource_id.get("videoId") if isinstance(resource_id, Mapping) else None,
            title=_as_text(snippet.get("title")),
            description=_as_text(snippet.get("description")),
            channel_id=snippet.get("channelId"),
            channel_title=snippet.get("channelTitle"),
            playlist_id=snippet.get("playlistId"),
            position=to_int(snippet.get("position")),
            published_at=_as_datetime(snippet.get("publishedAt")),
            video_published_at=_as_datetime(details.get("videoPublishedAt")),
        )


class PlaylistItemPage(BaseModel):
    """A page of `playlistItems.list`. Each page costs a full shared-pool unit."""

    next_page_token: str | None = None
    items: list[PlaylistItem] = Field(default_factory=list)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        return cls(
            next_page_token=raw.get("nextPageToken"),
            items=_items(raw, PlaylistItem.from_api),
        )


class Video(BaseModel):
    """A `videos.list` item (public metadata; no dislike count — §5.2)."""

    video_id: str
    title: str = ""
    description: str = ""
    channel_id: str | None = None
    channel_title: str | None = None
    published_at: datetime | None = None
    duration_seconds: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        return cls(video_id=_as_text(raw.get("id")), **_video_fields(raw))


class VideoStats(BaseModel):
    """A `videos:batchGetStats` item — the minimal stats resource."""

    video_id: str
    published_at: datetime | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    duration_seconds: int | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        # `_video_fields` also returns `title`/`description`/`channel_*`, which this model does
        # not declare; construction relies on pydantic's default `extra='ignore'` to drop them.
        return cls(video_id=_as_text(raw.get("id")), **_video_fields(raw))


class BatchStatsSummary(BaseModel):
    """Partial-failure accounting. `failed_video_count > 0` is not an error."""

    requested_video_count: int = 0
    succeeded_video_count: int = 0
    failed_video_count: int = 0
    failed_video_ids: list[str] = Field(default_factory=list)


class BatchStatsResponse(BaseModel):
    """The `videos:batchGetStats` envelope: succeeded items plus what failed.

    `items` and `summary.failed_video_ids` never overlap — a missing ID is a partial
    failure, not an exception.
    """

    items: list[VideoStats] = Field(default_factory=list)
    summary: BatchStatsSummary = Field(default_factory=BatchStatsSummary)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        summary = raw.get("summary") or {}
        return cls(
            items=_items(raw, VideoStats.from_api),
            summary=BatchStatsSummary(
                requested_video_count=to_int(summary.get("requestedVideoCount")) or 0,
                succeeded_video_count=to_int(summary.get("succeededVideoCount")) or 0,
                failed_video_count=to_int(summary.get("failedVideoCount")) or 0,
                failed_video_ids=list(summary.get("failedVideoIds") or []),
            ),
        )


class Comment(BaseModel):
    """A top-level comment (`commentThreads` `topLevelComment` / a `comments.list` item)."""

    comment_id: str
    text: str = ""
    author_name: str | None = None
    author_channel_id: str | None = None
    like_count: int = 0
    published_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        snippet = raw.get("snippet") or {}
        author_channel = snippet.get("authorChannelId") or {}
        return cls(
            comment_id=_as_text(raw.get("id")),
            # `textDisplay` with textFormat=plainText. `textOriginal` is only returned
            # for the authorized user, so it is always absent under API-key auth.
            text=_as_text(snippet.get("textDisplay")),
            author_name=snippet.get("authorDisplayName"),
            author_channel_id=(
                author_channel.get("value") if isinstance(author_channel, Mapping) else None
            ),
            like_count=to_int(snippet.get("likeCount")) or 0,
            published_at=_as_datetime(snippet.get("publishedAt")),
        )


class CommentThread(BaseModel):
    """One `commentThreads.list` item.

    `total_reply_count` comes from the thread snippet, not `len(replies)` — the `replies`
    part is truncated by the API.
    """

    thread_id: str
    video_id: str | None = None
    total_reply_count: int = 0
    comment: Comment

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        snippet = raw.get("snippet") or {}
        top_level = snippet.get("topLevelComment") or {}
        return cls(
            thread_id=_as_text(raw.get("id")),
            video_id=snippet.get("videoId"),
            total_reply_count=to_int(snippet.get("totalReplyCount")) or 0,
            comment=Comment.from_api(top_level),
        )


class CommentThreadPage(BaseModel):
    """A page of `commentThreads.list`."""

    next_page_token: str | None = None
    items: list[CommentThread] = Field(default_factory=list)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        return cls(
            next_page_token=raw.get("nextPageToken"),
            items=_items(raw, CommentThread.from_api),
        )


class VideoCategory(BaseModel):
    """A `videoCategories.list` item — not paged, the whole list returns at once."""

    category_id: str
    title: str = ""
    assignable: bool = True

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        snippet = raw.get("snippet") or {}
        return cls(
            category_id=_as_text(raw.get("id")),
            title=_as_text(snippet.get("title")),
            assignable=bool(snippet.get("assignable", True)),
        )


def video_categories_from_api(raw: Mapping[str, Any]) -> list[VideoCategory]:
    return _items(raw, VideoCategory.from_api)


def videos_from_api(raw: Mapping[str, Any]) -> list[Video]:
    return _items(raw, Video.from_api)


def channels_from_api(raw: Mapping[str, Any]) -> list[Channel]:
    return _items(raw, Channel.from_api)
