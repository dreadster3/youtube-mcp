"""Quota counter tests (section 5.3/5.4, section 16: "unit-test the quota counter exhaustively")."""

from __future__ import annotations

from datetime import datetime

import pytest

from youtube_mcp.youtube.quota import (
    CAPS,
    LOW_REMAINING_FRACTION,
    PACIFIC,
    QuotaBucket,
    QuotaCounter,
    QuotaExceeded,
)


class FakeClock:
    """Epoch-seconds clock we can set to any Pacific-local instant."""

    def __init__(self, when: datetime) -> None:
        self.now = when

    def __call__(self) -> float:
        return self.now.timestamp()


def pt(
    year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=PACIFIC)


def test_caps_match_handoff():
    assert CAPS[QuotaBucket.SEARCH] == 100
    assert CAPS[QuotaBucket.STATS] == 10_000
    assert CAPS[QuotaBucket.SHARED] == 10_000


def test_buckets_start_full_and_consume_one_unit_by_default():
    counter = QuotaCounter(clock=FakeClock(pt(2026, 5, 4)))
    assert counter.remaining(QuotaBucket.SEARCH) == 100

    counter.consume(QuotaBucket.SEARCH)

    assert counter.remaining(QuotaBucket.SEARCH) == 99


def test_buckets_are_independent():
    counter = QuotaCounter(clock=FakeClock(pt(2026, 5, 4)))
    counter.consume(QuotaBucket.STATS, 4000)

    assert counter.remaining(QuotaBucket.STATS) == 6000
    assert counter.remaining(QuotaBucket.SHARED) == 10_000
    assert counter.remaining(QuotaBucket.SEARCH) == 100


def test_exhausting_search_bucket_does_not_touch_shared():
    counter = QuotaCounter(clock=FakeClock(pt(2026, 5, 4)))
    for _ in range(100):
        counter.consume(QuotaBucket.SEARCH)

    assert counter.remaining(QuotaBucket.SEARCH) == 0
    assert counter.remaining(QuotaBucket.SHARED) == 10_000

    with pytest.raises(QuotaExceeded) as excinfo:
        counter.consume(QuotaBucket.SEARCH)

    assert excinfo.value.bucket == QuotaBucket.SEARCH
    assert excinfo.value.cap == 100
    assert "search" in str(excinfo.value)
    assert "Los_Angeles" in str(excinfo.value)


def test_failed_consume_charges_nothing():
    counter = QuotaCounter(clock=FakeClock(pt(2026, 5, 4)))
    for _ in range(100):
        counter.consume(QuotaBucket.SEARCH)

    for _ in range(3):
        with pytest.raises(QuotaExceeded):
            counter.consume(QuotaBucket.SEARCH)

    assert counter.remaining(QuotaBucket.SEARCH) == 0


def test_multi_unit_consume_checks_cap_for_the_whole_charge():
    counter = QuotaCounter(clock=FakeClock(pt(2026, 5, 4)))

    counter.consume(QuotaBucket.SEARCH, 95)
    with pytest.raises(QuotaExceeded):
        counter.consume(QuotaBucket.SEARCH, 6)
    # The rejected 6 units were not charged — only the accepted 95.
    assert counter.remaining(QuotaBucket.SEARCH) == 5


def test_zero_units_is_allowed_and_cannot_exhaust():
    counter = QuotaCounter(clock=FakeClock(pt(2026, 5, 4)))
    for _ in range(100):
        counter.consume(QuotaBucket.SEARCH)

    assert counter.consume(QuotaBucket.SEARCH, 0) == 0


def test_negative_units_rejected():
    counter = QuotaCounter(clock=FakeClock(pt(2026, 5, 4)))
    with pytest.raises(ValueError, match="units must be >= 0"):
        counter.consume(QuotaBucket.SHARED, -1)


def test_unknown_bucket_rejected():
    counter = QuotaCounter(clock=FakeClock(pt(2026, 5, 4)))
    with pytest.raises(ValueError, match="unknown quota bucket"):
        counter.remaining("nope")  # type: ignore[arg-type]


def test_custom_caps_are_honoured():
    counter = QuotaCounter(caps={QuotaBucket.SHARED: 2}, clock=FakeClock(pt(2026, 5, 4)))
    counter.consume(QuotaBucket.SHARED, 2)
    with pytest.raises(QuotaExceeded) as excinfo:
        counter.consume(QuotaBucket.SHARED)
    assert excinfo.value.cap == 2


def test_low_remaining_hook_fires_once_per_bucket_per_day():
    warnings: list[tuple[QuotaBucket, int]] = []
    counter = QuotaCounter(
        clock=FakeClock(pt(2026, 5, 4)),
        on_low_remaining=lambda bucket, remaining: warnings.append((bucket, remaining)),
    )

    for _ in range(100):
        counter.consume(QuotaBucket.SEARCH)

    assert warnings == [(QuotaBucket.SEARCH, 10)]
    assert 100 * LOW_REMAINING_FRACTION >= 10


def test_low_remaining_hook_ignores_other_buckets():
    warnings: list[QuotaBucket] = []
    counter = QuotaCounter(
        clock=FakeClock(pt(2026, 5, 4)),
        on_low_remaining=lambda bucket, remaining: warnings.append(bucket),
    )
    counter.consume(QuotaBucket.SHARED, 5_000)

    assert warnings == []


def test_reset_at_midnight_pacific():
    clock = FakeClock(pt(2026, 5, 4, 23, 59))
    counter = QuotaCounter(clock=clock)
    for _ in range(100):
        counter.consume(QuotaBucket.SEARCH)

    clock.now = pt(2026, 5, 5, 0, 1)

    assert counter.remaining(QuotaBucket.SEARCH) == 100
    counter.consume(QuotaBucket.SEARCH)
    assert counter.remaining(QuotaBucket.SEARCH) == 99


def test_no_reset_within_the_same_pacific_day():
    clock = FakeClock(pt(2026, 5, 4, 0, 1))
    counter = QuotaCounter(clock=clock)
    counter.consume(QuotaBucket.SEARCH, 3)

    clock.now = pt(2026, 5, 4, 23, 59)

    assert counter.remaining(QuotaBucket.SEARCH) == 97


def test_dst_spring_forward_day_boundary():
    """US DST starts 2026-03-08 (02:00 PST -> 03:00 PDT); one local date, one reset."""
    clock = FakeClock(pt(2026, 3, 8, 1, 30))
    counter = QuotaCounter(clock=clock)
    counter.consume(QuotaBucket.SHARED, 9_999)

    clock.now = pt(2026, 3, 8, 3, 30)  # same local date, now PDT
    assert counter.remaining(QuotaBucket.SHARED) == 1

    clock.now = pt(2026, 3, 9, 0, 30)  # next local midnight
    assert counter.remaining(QuotaBucket.SHARED) == 10_000


def test_dst_spring_forward_day_is_23_hours_not_24():
    """23:30 PST on 03-07 and 00:30 PDT on 03-08 are 2h apart but different quota days."""
    clock = FakeClock(pt(2026, 3, 7, 23, 30))
    counter = QuotaCounter(clock=clock)
    counter.consume(QuotaBucket.SEARCH, 5)

    clock.now = pt(2026, 3, 8, 0, 30)

    assert counter.remaining(QuotaBucket.SEARCH) == 100


def test_dst_fall_back_day_boundary():
    """US DST ends 2026-11-01: a 25-hour local day still resets once, at local midnight."""
    clock = FakeClock(pt(2026, 11, 1, 0, 30))
    counter = QuotaCounter(clock=clock)
    counter.consume(QuotaBucket.STATS, 500)

    clock.now = pt(2026, 11, 1, 23, 0)  # still the same 25h local day
    assert counter.remaining(QuotaBucket.STATS) == 9_500

    clock.now = pt(2026, 11, 2, 0, 0, 30)
    assert counter.remaining(QuotaBucket.STATS) == 10_000


def test_low_remaining_hook_refires_next_day():
    warnings: list[int] = []
    clock = FakeClock(pt(2026, 5, 4, 12, 0))
    counter = QuotaCounter(
        clock=clock,
        on_low_remaining=lambda bucket, remaining: warnings.append(remaining),
    )
    for _ in range(100):
        counter.consume(QuotaBucket.SEARCH)

    clock.now = pt(2026, 5, 5, 12, 0)
    for _ in range(100):
        counter.consume(QuotaBucket.SEARCH)

    assert warnings == [10, 10]
