"""Quota status tool (section 8): what each local bucket has left, and when it refills.

Read-only and free. It reads the in-process `QuotaCounter` the client already exposes — the same
accounting `/health` reports — so it never calls the API, never charges a bucket and can never
raise `QuotaExceeded`: an exhausted bucket is *reported*, not refused. That is the whole point of
the tool: a model that can see `search` is down to its last few calls can route around it
(`youtube_list_channel_videos`, `youtube_search_in_transcript`) instead of spending them.

The numbers are ours, not Google's (section 5.4), and they are process-local: they start at zero
on every restart, so they are a lower bound, never an over-count. The description says both, since
a model that reads them as authoritative would plan the day wrong.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta

from fastmcp import FastMCP
from pydantic import BaseModel

from youtube_mcp.tools import Deps
from youtube_mcp.tools.errors import as_tool_error
from youtube_mcp.youtube.quota import CAPS, PACIFIC, QuotaBucket


class BucketStatus(BaseModel):
    """One bucket's spend for the current Pacific quota day."""

    bucket: QuotaBucket
    used: int
    remaining: int
    cap: int


class QuotaStatus(BaseModel):
    """Every bucket, plus when the current quota day ends and the standing caveat."""

    buckets: list[BucketStatus]
    resets_at: str
    approximate: bool = True


def _resets_at(clock: Callable[[], float]) -> str:
    """ISO 8601 instant of the next quota-day reset: the following midnight in `PACIFIC`.

    Takes the counter's *own* clock rather than the wall clock, because `resets_at` describes
    when that counter rolls over — the instant its buckets actually refill. All timezone and DST
    handling stays in `PACIFIC` (zoneinfo); nothing is re-derived here.
    """
    tomorrow = datetime.fromtimestamp(clock(), PACIFIC).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=PACIFIC).isoformat()


def register(mcp: FastMCP, deps: Deps) -> None:
    """Register the quota status tool on `mcp`."""

    @mcp.tool
    @as_tool_error
    async def youtube_get_quota_status() -> QuotaStatus:
        """Report how much of today's YouTube Data API quota is left, and when it resets.

        Returns one entry per bucket — `search`, `stats`, `shared` — with `used`, `remaining`
        and `cap` for the current quota day, `resets_at` as an ISO 8601 timestamp, and
        `approximate: true`.

        **Check this before a deliberate search.** `search` is only 100 calls/day and does not
        recover until `resets_at`; when it is low it is worth spending a call on a cheaper route
        instead — `youtube_list_channel_videos` (2 units from the shared pool) or
        `youtube_search_in_transcript` (no Data API quota at all). Treat `youtube_search_videos`
        as scarce and deliberate. `stats` (`videos:batchGetStats`) and `shared` are 10,000
        units/day each, and quota resets at midnight Pacific Time.

        **Costs no quota.** This reads our own in-process counter, never the API, so it is free
        to call and cannot itself fail with `quotaExceeded` — a bucket at 0 is reported, not
        refused.

        **The numbers are approximate, and they are ours — not Google's.** They count what this
        server process has spent, so they reset to zero whenever it restarts, and they can
        under-count calls made before a restart or by another process using the same API key.
        Google can still refuse a call this tool says is affordable, and a bucket shown as
        exhausted is exhausted for this process only.
        """
        counter = deps.client.quota
        buckets: list[BucketStatus] = []
        for bucket in QuotaBucket:
            # Asked, not charged: `remaining` never consumes and never raises (section 5.4).
            remaining = counter.remaining(bucket)
            buckets.append(
                BucketStatus(
                    bucket=bucket,
                    used=CAPS[bucket] - remaining,
                    remaining=remaining,
                    cap=CAPS[bucket],
                )
            )
        return QuotaStatus(buckets=buckets, resets_at=_resets_at(counter._clock))
