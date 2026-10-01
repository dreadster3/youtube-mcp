"""Per-bucket Data API quota accounting (section 5.3, section 5.4 of HANDOFF.md).

The API never tells you which bucket tripped, so we count locally: `search.list` has its
own 100/day bucket, `videos:batchGetStats` its own 10,000/day bucket, and everything else
shares 10,000/day. Buckets reset at **midnight America/Los_Angeles** (zoneinfo, so DST is
handled), which means a quota day is a local calendar date.

**Process-local, no persistence:** counters start at zero on restart. That is deliberate —
the authoritative accounting is Google's; this only lets us warn before the API starts
returning 403 `quotaExceeded`, and a restart must never *over*-count.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


class QuotaBucket(StrEnum):
    """The three quota buckets that matter for the read-only tool surface."""

    SEARCH = "search"
    STATS = "stats"
    SHARED = "shared"


#: Daily unit caps (section 5.3). Every method in scope costs 1 unit.
CAPS: Mapping[QuotaBucket, int] = {
    QuotaBucket.SEARCH: 100,
    QuotaBucket.STATS: 10_000,
    QuotaBucket.SHARED: 10_000,
}

#: Fire `on_low_remaining` once a bucket is down to this fraction of its cap.
LOW_REMAINING_FRACTION = 0.1


class QuotaExceeded(Exception):
    """A local bucket is exhausted. Raised *before* the HTTP call is attempted."""

    def __init__(self, bucket: QuotaBucket, cap: int) -> None:
        super().__init__(
            f"local quota bucket {bucket!r} exhausted ({cap} units/day); "
            "resets at midnight America/Los_Angeles"
        )
        self.bucket = bucket
        self.cap = cap


class QuotaCounter:
    """Local per-bucket unit counter with a midnight-PT reset.

    `clock` is injected so tests can cross midnight (and both DST boundaries) without
    sleeping; `on_low_remaining(bucket, remaining)` fires once per bucket per quota day,
    when the bucket first drops to `LOW_REMAINING_FRACTION` of its cap.
    """

    def __init__(
        self,
        *,
        caps: Mapping[QuotaBucket, int] = CAPS,
        clock: Callable[[], float] = time.time,
        on_low_remaining: Callable[[QuotaBucket, int], None] | None = None,
    ) -> None:
        self._caps = dict(caps)
        self._clock = clock
        self._on_low_remaining = on_low_remaining
        self._used = dict.fromkeys(self._caps, 0)
        self._warned: set[QuotaBucket] = set()
        self._day = self._quota_day()

    def consume(self, bucket: QuotaBucket, units: int = 1) -> int:
        """Charge `units` to `bucket`; return its remaining budget.

        Raises `QuotaExceeded` when the charge would pass the cap — nothing is charged
        in that case, so a failed call never eats the caller's last unit.
        """
        cap = self._cap(bucket)
        if units < 0:
            raise ValueError(f"units must be >= 0, got {units}")
        self._rollover()
        if self._used[bucket] + units > cap:
            raise QuotaExceeded(bucket, cap)
        self._used[bucket] += units
        remaining = cap - self._used[bucket]
        if (
            self._on_low_remaining is not None
            and bucket not in self._warned
            and remaining <= cap * LOW_REMAINING_FRACTION
        ):
            self._warned.add(bucket)
            self._on_low_remaining(bucket, remaining)
        return remaining

    def remaining(self, bucket: QuotaBucket) -> int:
        """Units left in `bucket` for the current Pacific day."""
        cap = self._cap(bucket)
        self._rollover()
        return cap - self._used[bucket]

    def _cap(self, bucket: QuotaBucket) -> int:
        try:
            return self._caps[bucket]
        except KeyError:
            raise ValueError(f"unknown quota bucket: {bucket!r}") from None

    def _quota_day(self) -> date:
        return datetime.fromtimestamp(self._clock(), PACIFIC).date()

    def _rollover(self) -> None:
        today = self._quota_day()
        if today != self._day:
            self._day = today
            self._used = dict.fromkeys(self._caps, 0)
            self._warned.clear()
