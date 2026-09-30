"""SQLite-backed async TTL cache (§14 of HANDOFF.md).

One file, one connection, JSON values. Transcripts, video stats and channel→uploads
playlist mappings all go through here — no Redis, no extra service.
"""

import json
import time
from collections.abc import Callable
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


class Cache:
    """Async TTL key/value cache over aiosqlite.

    Values are JSON-encoded, so only JSON-serializable data round-trips. Expiry is
    per entry (`expires_at` epoch seconds); expired rows are deleted on read.
    """

    def __init__(self, database_path: str, *, clock: Callable[[], float] = time.time) -> None:
        self._database_path = database_path
        self._clock = clock
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the connection and create the schema. Idempotent."""
        if self._db is not None:
            return
        db = await aiosqlite.connect(self._database_path)
        try:
            await db.execute(_SCHEMA)
            await db.commit()
        except Exception:
            await db.close()
            raise
        self._db = db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "Cache":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def get(self, key: str, *, default: Any = None) -> Any:
        """Return the cached value, or `default` if missing or expired."""
        if self._db is None:
            return default
        async with self._db.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return default
        value, expires_at = row
        if expires_at <= self._clock():
            await self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
            await self._db.commit()
            return default
        return json.loads(value)

    async def set(self, key: str, value: Any, *, ttl: int) -> None:
        """Store `value` under `key`, expiring after `ttl` seconds (0 = no expiry)."""
        if self._db is None:
            raise RuntimeError("Cache.connect() must be awaited before use")
        expires_at = float("inf") if ttl == 0 else self._clock() + ttl
        await self._db.execute(
            "INSERT INTO cache (key, value, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, expires_at = excluded.expires_at",
            (key, json.dumps(value, ensure_ascii=False), expires_at),
        )
        await self._db.commit()

    async def delete(self, key: str) -> None:
        if self._db is None:
            return
        await self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
        await self._db.commit()

    async def purge_expired(self) -> int:
        """Drop every expired row; returns how many went."""
        if self._db is None:
            return 0
        cursor = await self._db.execute("DELETE FROM cache WHERE expires_at <= ?", (self._clock(),))
        await self._db.commit()
        return cursor.rowcount


def namespaced(namespace: str, *parts: str) -> str:
    """Build a cache key: `namespaced("transcript", video_id, lang)` -> `transcript:id:lang`.

    One spelling for every key so later batches cannot invent their own scheme.
    """
    return ":".join((namespace, *parts))
