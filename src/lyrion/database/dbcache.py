"""
Database cache layer (port of Slim::Utils::DbCache).

Provides a fast in-memory cache backed by the SQLite database.
Entries have TTLs, can be invalidated by category, and support
simple key patterns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Callable, Awaitable
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger("lyrion.dbcache")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

T = Any


@dataclass
class CacheEntry:
    """A single database cache entry."""
    key: str
    value: str | None
    created: float
    expires: float  # 0 = never expires
    category: str


# ---------------------------------------------------------------------------
# DbCache implementation
# ---------------------------------------------------------------------------

class DbCache:
    """
    SQLite-backed in-memory cache.

    This is a write-through cache: writes go to both the in-memory
    dict and the SQLite backing store. Reads check memory first, then DB.

    It supports:
    - TTL-based expiration
    - Category-based invalidation
    - JSON value serialization
    - Optional refresh callbacks
    - Async and sync accessors
    """

    __slots__ = (
        "_db_path",
        "_conn",
        "_memory",
        "_lock",
        "_default_ttl",
        "_category_locks",
    )

    _instance: DbCache | None = None

    def __init__(
        self,
        db_path: str | None = None,
        default_ttl: float = 3600.0,
    ) -> None:
        import aiosqlite
        self._db_path = db_path or ":memory:"
        self._conn: aiosqlite.Connection | None = None
        self._memory: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._default_ttl = default_ttl
        self._category_locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def instance(cls) -> DbCache:
        """Return the singleton DbCache instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton."""
        inst = cls._instance
        if inst is not None and inst._conn is not None:
            inst._conn.close()
        cls._instance = None

    # ---- async lifecycle ----

    async def init(self, db_path: str | None = None) -> None:
        """Initialize the cache database and load existing entries."""
        if self._conn is not None:
            return

        self._db_path = db_path or self._db_path
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row

        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS dbcache (
                key         TEXT PRIMARY KEY,
                value       TEXT,
                created     REAL NOT NULL,
                expires     REAL NOT NULL,
                category    TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_dbcache_category ON dbcache(category);
            CREATE INDEX IF NOT EXISTS idx_dbcache_expires ON dbcache(expires);
        """)
        await self._conn.commit()

        # Load non-expired entries into memory
        await self._load_from_db()

    async def _load_from_db(self) -> None:
        """Load all non-expired entries from DB into memory."""
        if self._conn is None:
            return
        now = time.time()
        async with self._conn.execute(
            "SELECT key, value, created, expires, category FROM dbcache WHERE expires = 0 OR expires > ?",
            (now,),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            entry = CacheEntry(
                key=row["key"],
                value=row["value"],
                created=row["created"],
                expires=row["expires"],
                category=row["category"],
            )
            self._memory[row["key"]] = entry

    async def _persist(self, entry: CacheEntry) -> None:
        """Write a single entry to the database."""
        if self._conn is None:
            return
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO dbcache (key, value, created, expires, category)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entry.key, entry.value, entry.created, entry.expires, entry.category),
        )
        await self._conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- get ----

    async def get(
        self,
        key: str,
        *,
        default: T = None,  # type: ignore[assignment]
        category: str = "",
        ttl: float | None = None,
    ) -> T:
        """
        Get a cached value.

        Returns `default` if the key is not found or has expired.
        The value is automatically JSON-deserialized.
        """
        async with self._lock:
            entry = self._memory.get(key)

        if entry is None:
            return default

        now = time.time()
        if entry.expires > 0 and entry.expires <= now:
            await self._expire(key)
            return default

        if entry.value is None:
            return default

        try:
            return json.loads(entry.value)
        except json.JSONDecodeError:
            return entry.value

    def get_sync(self, key: str, default: T = None) -> T:  # type: ignore[assignment]
        """Synchronous get (reads memory only, no DB round-trip)."""
        entry = self._memory.get(key)
        if entry is None:
            return default
        now = time.time()
        if entry.expires > 0 and entry.expires <= now:
            return default
        if entry.value is None:
            return default
        try:
            return json.loads(entry.value)
        except json.JSONDecodeError:
            return entry.value

    # ---- set ----

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: float | None = None,
        category: str = "",
    ) -> None:
        """
        Set a cached value.

        If ttl is None, uses the default_ttl. Pass 0 for no expiration.
        """
        ttl = ttl if ttl is not None else self._default_ttl
        expires = time.time() + ttl if ttl > 0 else 0.0

        # Serialize value
        if value is None:
            str_value: str | None = None
        elif isinstance(value, str):
            str_value = value
        else:
            str_value = json.dumps(value, separators=(",", ":"))

        entry = CacheEntry(
            key=key,
            value=str_value,
            created=time.time(),
            expires=expires,
            category=category,
        )

        async with self._lock:
            self._memory[key] = entry

        await self._persist(entry)

    async def _expire(self, key: str) -> None:
        """Remove an expired entry from memory and DB."""
        async with self._lock:
            self._memory.pop(key, None)
        if self._conn is not None:
            await self._conn.execute("DELETE FROM dbcache WHERE key = ?", (key,))
            await self._conn.commit()

    # ---- invalidate ----

    async def invalidate(self, key: str) -> None:
        """Remove a specific key from the cache."""
        async with self._lock:
            self._memory.pop(key, None)
        if self._conn is not None:
            await self._conn.execute("DELETE FROM dbcache WHERE key = ?", (key,))
            await self._conn.commit()

    async def invalidate_category(self, category: str) -> int:
        """
        Remove all entries in a category.

        Returns the number of entries removed.
        """
        if not category:
            return 0

        async with self._lock:
            # Remove from memory
            keys_to_remove = [
                k for k, v in self._memory.items()
                if v.category == category
            ]
            for k in keys_to_remove:
                self._memory.pop(k, None)

        if self._conn is not None:
            cur = await self._conn.execute(
                "DELETE FROM dbcache WHERE category = ?", (category,)
            )
            await self._conn.commit()
            return cur.rowcount

        return len(keys_to_remove)

    async def invalidate_expired(self) -> int:
        """
        Remove all expired entries from the cache.

        Returns the number of entries removed.
        """
        now = time.time()
        removed = 0

        async with self._lock:
            expired_keys = [
                k for k, v in self._memory.items()
                if v.expires > 0 and v.expires <= now
            ]
            for k in expired_keys:
                self._memory.pop(k, None)
            removed = len(expired_keys)

        if self._conn is not None and removed > 0:
            await self._conn.execute(
                "DELETE FROM dbcache WHERE expires > 0 AND expires <= ?",
                (now,),
            )
            await self._conn.commit()

        return removed

    # ---- get_or_set ----

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[T] | T],
        *,
        ttl: float | None = None,
        category: str = "",
    ) -> T:
        """
        Get a cached value, or compute it with the factory and cache the result.
        """
        value = await self.get(key)
        if value is not None:
            return value

        # Compute
        result = factory()
        if asyncio.iscoroutine(result):
            result = await result

        await self.set(key, result, ttl=ttl, category=category)
        return result

    # ---- utility ----

    def _make_key(self, *args: Any) -> str:
        """Create a cache key from arguments."""
        raw = "|".join(str(a) for a in args)
        return hashlib.md5(raw.encode()).hexdigest()

    async def clear(self) -> None:
        """Clear all entries from the cache."""
        async with self._lock:
            self._memory.clear()
        if self._conn is not None:
            await self._conn.execute("DELETE FROM dbcache")
            await self._conn.commit()

    async def stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        now = time.time()
        expired = sum(
            1 for e in self._memory.values()
            if e.expires > 0 and e.expires <= now
        )
        return {
            "total_entries": len(self._memory),
            "expired_entries": expired,
            "memory_size_bytes": sum(
                len(e.value or "") + len(e.key)
                for e in self._memory.values()
            ),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_dbcache: DbCache = DbCache.instance()


def dbcache() -> DbCache:
    """Return the global DbCache instance."""
    return _dbcache
