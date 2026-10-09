"""Tool tests for `youtube_get_quota_status` (section 8), offline.

The tool reads `deps.client.quota` — the stub client from `conftest` already carries a real
`QuotaCounter` — so these tests clock that counter with the injected-clock style of `test_quota.py`
and assert what the tool adds: a row per bucket at its cap, that reading status spends nothing and
refuses nothing, that an exhausted bucket comes back as data instead of a `QuotaExceeded`, and
that `resets_at` is the next midnight in `America/Los_Angeles` (both DST boundaries included).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastmcp import Client

from conftest import make_test_server
from youtube_mcp.youtube.quota import CAPS, PACIFIC, QuotaBucket, QuotaCounter

TOOL = "youtube_get_quota_status"


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


def clocked_server(clock: FakeClock) -> tuple[Any, QuotaCounter]:
    """A registered server whose client counts quota on `clock`."""
    counter = QuotaCounter(clock=clock)
    mcp, stub = make_test_server()
    stub.quota = counter
    return mcp, counter


async def status(mcp: Any) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(TOOL, {})
        return result.structured_content


def by_bucket(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["bucket"]: entry for entry in payload["buckets"]}


async def test_fresh_counter_reports_every_bucket_full_at_its_cap():
    mcp, _ = clocked_server(FakeClock(pt(2026, 5, 4)))

    payload = await status(mcp)

    buckets = by_bucket(payload)
    assert set(buckets) == {bucket.value for bucket in QuotaBucket}
    for bucket in QuotaBucket:
        entry = buckets[bucket.value]
        assert entry["used"] == 0
        assert entry["remaining"] == CAPS[bucket]
        assert entry["cap"] == CAPS[bucket]
    assert payload["approximate"] is True


async def test_status_does_not_spend_the_quota_it_reports():
    mcp, counter = clocked_server(FakeClock(pt(2026, 5, 4)))

    first = await status(mcp)
    second = await status(mcp)

    assert first == second
    for bucket in QuotaBucket:
        assert counter.remaining(bucket) == CAPS[bucket]


async def test_exhausted_search_bucket_is_reported_not_refused():
    """A bucket at 0 is data: the tool has to answer, and it must not raise `QuotaExceeded`."""
    clock = FakeClock(pt(2026, 5, 4))
    mcp, counter = clocked_server(clock)
    counter.consume(QuotaBucket.SEARCH, CAPS[QuotaBucket.SEARCH])

    payload = await status(mcp)
    search = by_bucket(payload)["search"]

    assert search["remaining"] == 0
    assert search["used"] == CAPS[QuotaBucket.SEARCH]
    assert search["cap"] == CAPS[QuotaBucket.SEARCH]
    # Still exhausted *after* the call: reading status charged nothing.
    assert counter.remaining(QuotaBucket.SEARCH) == 0
    assert by_bucket(payload)["shared"]["remaining"] == CAPS[QuotaBucket.SHARED]


async def test_resets_at_is_the_next_pacific_midnight():
    mcp, _ = clocked_server(FakeClock(pt(2026, 5, 4, 12)))

    payload = await status(mcp)

    assert payload["resets_at"] == "2026-05-05T00:00:00-07:00"
    assert datetime.fromisoformat(payload["resets_at"]).tzinfo is not None


async def test_past_midnight_the_counters_reset_and_resets_at_advances_one_day():
    clock = FakeClock(pt(2026, 5, 4, 23, 59))
    mcp, counter = clocked_server(clock)
    counter.consume(QuotaBucket.SEARCH, CAPS[QuotaBucket.SEARCH])

    before = await status(mcp)
    clock.now = pt(2026, 5, 5, 0, 1)
    after = await status(mcp)

    assert by_bucket(before)["search"]["remaining"] == 0
    assert by_bucket(after)["search"] == {
        "bucket": "search",
        "used": 0,
        "remaining": CAPS[QuotaBucket.SEARCH],
        "cap": CAPS[QuotaBucket.SEARCH],
    }
    assert after["resets_at"] == "2026-05-06T00:00:00-07:00"


async def test_spring_forward_resets_at_is_the_next_local_midnight():
    """US DST starts 2026-03-08 (02:00 PST -> 03:00 PDT): midnight the next day is PDT."""
    clock = FakeClock(pt(2026, 3, 7, 23, 30))
    mcp, counter = clocked_server(clock)
    counter.consume(QuotaBucket.SEARCH, 5)

    clock.now = pt(2026, 3, 8, 0, 30)  # next local date, still PST
    payload = await status(mcp)

    assert by_bucket(payload)["search"]["remaining"] == CAPS[QuotaBucket.SEARCH]
    assert payload["resets_at"] == "2026-03-09T00:00:00-07:00"


async def test_fall_back_resets_at_is_the_next_local_midnight():
    """US DST ends 2026-11-01 (a 25-hour local day): one reset, at local midnight PST."""
    clock = FakeClock(pt(2026, 11, 1, 23, 0))
    mcp, counter = clocked_server(clock)
    counter.consume(QuotaBucket.STATS, 500)

    payload = await status(mcp)
    assert payload["resets_at"] == "2026-11-02T00:00:00-08:00"

    clock.now = pt(2026, 11, 2, 0, 0, 30)
    payload = await status(mcp)

    assert by_bucket(payload)["stats"]["remaining"] == CAPS[QuotaBucket.STATS]
    assert payload["resets_at"] == "2026-11-03T00:00:00-08:00"


async def test_description_states_cost_approximation_and_what_to_do():
    mcp, _ = clocked_server(FakeClock(pt(2026, 5, 4)))

    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    description = tools[TOOL].description
    # The cost of the tool itself, and the fact that the numbers are not Google's.
    assert "Costs no quota" in description
    assert "approximate" in description
    assert "restarts" in description
    # The action: route around a low search bucket instead of spending it.
    assert "youtube_search_videos" in description
    assert "youtube_list_channel_videos" in description
    assert "youtube_search_in_transcript" in description
    assert "midnight Pacific Time" in description
    assert tools[TOOL].output_schema is not None
