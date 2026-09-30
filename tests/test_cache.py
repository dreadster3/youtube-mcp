"""Cache TTL, expiry and JSON round-trip behaviour (§14)."""

import pytest

from collections.abc import AsyncIterator

from youtube_mcp.cache import Cache, namespaced


class FakeClock:
    """Injectable clock so TTL tests never sleep."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _returns(value: object) -> object:
    """Awaitable wrapper so a fake aiosqlite.connect can be monkeypatched in."""
    return value


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def cache(tmp_path, clock: FakeClock) -> AsyncIterator[Cache]:
    async with Cache(str(tmp_path / "cache.db"), clock=clock) as opened:
        yield opened


async def test_set_get_roundtrip(cache: Cache) -> None:
    payload = {"video_id": "abc", "segments": [{"start": 0.0, "text": "hi"}], "n": 3}
    await cache.set("k", payload, ttl=60)
    assert await cache.get("k") == payload


async def test_json_scalars_and_unicode(cache: Cache) -> None:
    await cache.set("s", "olá — transcript", ttl=60)
    await cache.set("i", 42, ttl=60)
    await cache.set("n", None, ttl=60)
    await cache.set("l", [1, "two"], ttl=60)
    assert await cache.get("s") == "olá — transcript"
    assert await cache.get("i") == 42
    assert await cache.get("n", default="absent") is None  # None round-trips, not "key missing"
    assert await cache.get("l") == [1, "two"]


async def test_missing_key_returns_default(cache: Cache) -> None:
    assert await cache.get("nope") is None
    assert await cache.get("nope", default="fallback") == "fallback"


async def test_entry_expires(cache: Cache, clock: FakeClock) -> None:
    await cache.set("k", "v", ttl=1)
    assert await cache.get("k") == "v"
    clock.advance(1.05)
    assert await cache.get("k") is None


async def test_expired_row_is_deleted(cache: Cache, clock: FakeClock) -> None:
    await cache.set("k", "v", ttl=1)
    clock.advance(1.05)
    await cache.get("k")
    assert await cache.purge_expired() == 0


async def test_purge_expired_counts_rows(cache: Cache, clock: FakeClock) -> None:
    await cache.set("live", "v", ttl=60)
    await cache.set("dead", "v", ttl=1)
    clock.advance(1.05)
    assert await cache.purge_expired() == 1
    assert await cache.get("live") == "v"


async def test_zero_ttl_means_no_expiry(cache: Cache) -> None:
    await cache.set("k", "v", ttl=0)
    assert await cache.purge_expired() == 0
    assert await cache.get("k") == "v"


async def test_set_overwrites(cache: Cache) -> None:
    await cache.set("k", "old", ttl=60)
    await cache.set("k", "new", ttl=60)
    assert await cache.get("k") == "new"


async def test_delete(cache: Cache) -> None:
    await cache.set("k", "v", ttl=60)
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_operating_on_unopened_cache_is_safe(tmp_path) -> None:
    cache = Cache(str(tmp_path / "cache.db"))
    assert await cache.get("k") is None
    assert await cache.purge_expired() == 0
    await cache.delete("k")
    await cache.close()
    with pytest.raises(RuntimeError, match="connect"):
        await cache.set("k", "v", ttl=60)


async def test_connect_failure_closes_connection_and_reraises(tmp_path, monkeypatch) -> None:
    state = {"closed": False}

    class Boom:
        async def execute(self, *_args: object) -> None:
            raise RuntimeError("disk on fire")

        async def close(self) -> None:
            state["closed"] = True

    monkeypatch.setattr("youtube_mcp.cache.aiosqlite.connect", lambda _path: _returns(Boom()))
    cache = Cache(str(tmp_path / "cache.db"))
    with pytest.raises(RuntimeError, match="disk on fire"):
        await cache.connect()
    assert state["closed"] is True
    assert cache._db is None


async def test_reconnect_is_idempotent(cache: Cache) -> None:
    await cache.set("k", "v", ttl=60)
    await cache.connect()
    assert await cache.get("k") == "v"


async def test_values_survive_reconnect(tmp_path) -> None:
    path = str(tmp_path / "cache.db")
    async with Cache(path) as first:
        await first.set("k", {"a": 1}, ttl=60)
    async with Cache(path) as second:
        assert await second.get("k") == {"a": 1}


async def test_cache_does_not_block_event_loop(cache: Cache) -> None:
    """A slow write must not stall other tasks — the whole point of aiosqlite."""
    import anyio

    ticks = 0

    async def spin() -> None:
        nonlocal ticks
        while ticks < 5:
            ticks += 1
            await anyio.sleep(0.01)

    async with anyio.create_task_group() as tg:
        tg.start_soon(spin)
        for i in range(50):
            await cache.set(f"k{i}", {"payload": "x" * 1000}, ttl=60)
    assert ticks == 5


def test_namespaced() -> None:
    assert namespaced("transcript", "dQw4w9WgXcQ", "en") == "transcript:dQw4w9WgXcQ:en"
    assert namespaced("uploads_playlist", "UCabc") == "uploads_playlist:UCabc"
