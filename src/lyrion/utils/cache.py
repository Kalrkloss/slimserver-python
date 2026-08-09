"""
In-memory cache utilities for Lyrion Music Server.

Ported from Slim::Utils::Cache. Provides a fast in-memory cache with TTL,
size limits, and async support.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

logger = logging.getLogger("lyrion.cache")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

T = Any


@dataclass
class CacheEntry:
    """A single cache entry."""
    key: str
    value: Any
    created: float
    expires: float  # 0 = never
    size: int  # approximate byte size


# ---------------------------------------------------------------------------
# LyrionCache
# ---------------------------------------------------------------------------

class LyrionCache:
    """
    Fast in-memory cache with LRU eviction, TTL, and size limits.

    Features:
    - TTL-based expiration
    - Maximum entry count limit
    - Maximum total size limit
    - LRU eviction when limits are reached
    - Thread-safe operations
    - Async get/set
    - Category-based invalidation
    - Stats tracking
    """

    __slots__ = (
        "_data",
        "_lock",
        "_max_entries",
        "_max_size_bytes",
        "_default_ttl",
        "_stats",
        "_category_index",
    )

    _instance: LyrionCache | None = None

    def __init__(
        self,
        max_entries: int = 10000,
        max_size_bytes: int = 100 * 1024 * 1024,  # 100 MB
        default_ttl: float = 3600.0,
    ) -> None:
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._max_size_bytes = max_size_bytes
        self._default_ttl = default_ttl
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "expired": 0,
        }
        self._category_index: dict[str, set[str]] = {}

    @classmethod
    def instance(cls) -> LyrionCache:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton."""
        cls._instance = None

    # ---- internal ----

    def _estimate_size(self, value: Any) -> int:
        """Estimate the memory size of a value in bytes."""
        try:
            import sys
            return len(sys.getsizeof(value))
        except Exception:
            return 64

    def _evict_if_needed(self) -> None:
        """Evict oldest entries if over limits."""
        while (
            len(self._data) >= self._max_entries
            or self._total_size() >= self._max_size_bytes
        ) and self._data:
            # Remove oldest (first) entry
            _, entry = self._data.popitem(last=False)
            self._stats["evictions"] += 1
            # Remove from category index
            for cat_entries in self._category_index.values():
                cat_entries.discard(entry.key)

    def _total_size(self) -> int:
        """Return approximate total size of all entries."""
        return sum(e.size for e in self._data.values())

    def _is_expired(self, entry: CacheEntry) -> bool:
        """Return True if entry has expired."""
        if entry.expires <= 0:
            return False
        return time.monotonic() >= entry.expires

    # ---- get ----

    def get(self, key: str, default: T = None) -> T:  # type: ignore[assignment]
        """
        Synchronous get from cache.

        Returns `default` if key not found or expired.
        Moves entry to end of LRU order on hit.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._stats["misses"] += 1
                return default
            if self._is_expired(entry):
                del self._data[key]
                self._stats["expired"] += 1
                self._stats["misses"] += 1
                return default
            # Move to end (most recently used)
            self._data.move_to_end(key)
            self._stats["hits"] += 1
            return entry.value

    async def get_async(self, key: str, default: T = None) -> T:  # type: ignore[assignment]
        """Async get — wraps synchronous get."""
        return self.get(key, default)

    def peek(self, key: str, default: T = None) -> T:  # type: ignore[assignment]
        """Get without updating LRU order or stats."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None or self._is_expired(entry):
                return default
            return entry.value

    # ---- set ----

    def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: float | None = None,
        category: str = "",
    ) -> None:
        """
        Store a value in the cache.

        If ttl is None, uses the default_ttl. Pass 0 for no expiration.
        """
        ttl = ttl if ttl is not None else self._default_ttl
        expires = time.monotonic() + ttl if ttl > 0 else 0.0
        size = self._estimate_size(value)

        with self._lock:
            # Evict if needed
            self._evict_if_needed()

            # Remove old entry if updating
            if key in self._data:
                old_entry = self._data.pop(key)
                for cat_entries in self._category_index.values():
                    cat_entries.discard(key)

            entry = CacheEntry(
                key=key,
                value=value,
                created=time.monotonic(),
                expires=expires,
                size=size,
            )
            self._data[key] = entry
            self._data.move_to_end(key)

            # Index by category
            if category:
                if category not in self._category_index:
                    self._category_index[category] = set()
                self._category_index[category].add(key)

    async def set_async(
        self,
        key: str,
        value: Any,
        *,
        ttl: float | None = None,
        category: str = "",
    ) -> None:
        """Async set — wraps synchronous set."""
        self.set(key, value, ttl=ttl, category=category)

    # ---- get_or_set ----

    def get_or_set(
        self,
        key: str,
        factory: Callable[[], T],
        *,
        ttl: float | None = None,
        category: str = "",
    ) -> T:
        """Get from cache, or compute and cache the result."""
        value = self.get(key)
        if value is not None:
            return value
        result = factory()
        self.set(key, result, ttl=ttl, category=category)
        return result

    async def get_or_set_async(
        self,
        key: str,
        factory: Callable[[], Awaitable[T] | T],
        *,
        ttl: float | None = None,
        category: str = "",
    ) -> T:
        """Async get-or-set."""
        value = self.get(key)
        if value is not None:
            return value
        result = factory()
        if asyncio.iscoroutine(result):
            result = await result
        self.set(key, result, ttl=ttl, category=category)
        return result

    # ---- invalidate ----

    def invalidate(self, key: str) -> bool:
        """Remove a key from the cache. Returns True if found."""
        with self._lock:
            if key in self._data:
                del self._data[key]
                for cat_entries in self._category_index.values():
                    cat_entries.discard(key)
                return True
            return False

    def invalidate_category(self, category: str) -> int:
        """Remove all keys in a category. Returns count removed."""
        if not category or category not in self._category_index:
            return 0
        with self._lock:
            keys = list(self._category_index.pop(category))
            for key in keys:
                self._data.pop(key, None)
            return len(keys)

    def invalidate_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        removed = 0
        now = time.monotonic()
        with self._lock:
            expired_keys = [
                k for k, e in self._data.items()
                if e.expires > 0 and e.expires <= now
            ]
            for key in expired_keys:
                del self._data[key]
                for cat_entries in self._category_index.values():
                    cat_entries.discard(key)
            removed = len(expired_keys)
        self._stats["expired"] += removed
        return removed

    # ---- utility ----

    def clear(self) -> None:
        """Clear all entries."""
        with self._lock:
            self._data.clear()
            self._category_index.clear()

    def __contains__(self, key: str) -> bool:
        entry = self._data.get(key)
        if entry is None:
            return False
        if self._is_expired(entry):
            return False
        return True

    def __len__(self) -> int:
        return len(self._data)

    def keys(self) -> list[str]:
        """Return all cache keys."""
        return list(self._data.keys())

    def stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        return {
            **self._stats,
            "entries": len(self._data),
            "total_size_bytes": self._total_size(),
            "max_entries": self._max_entries,
            "max_size_bytes": self._max_size_bytes,
            "hit_rate": (
                self._stats["hits"] / (self._stats["hits"] + self._stats["misses"])
                if (self._stats["hits"] + self._stats["misses"]) > 0
                else 0.0
            ),
        }

    def make_key(self, *args: Any) -> str:
        """Create a cache key from arguments."""
        raw = "|".join(str(a) for a in args)
        return hashlib.md5(raw.encode()).hexdigest()

    # ---- decorator ----

    def cached(
        self,
        *,
        ttl: float | None = None,
        category: str = "",
        key_func: Callable[..., str] | None = None,
    ):
        """Decorator to cache function results."""
        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            def wrapper(*args: Any, **kwargs: Any) -> T:
                if key_func:
                    cache_key = key_func(*args, **kwargs)
                else:
                    cache_key = self.make_key(func.__name__, args, tuple(sorted(kwargs.items())))
                value = self.get(cache_key)
                if value is not None:
                    return value
                result = func(*args, **kwargs)
                self.set(cache_key, result, ttl=ttl, category=category)
                return result
            return wrapper  # type: ignore[return-value]
        return decorator


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_cache: LyrionCache = LyrionCache.instance()


def Cache() -> LyrionCache:
    """Return the global LyrionCache instance."""
    return _cache


def get(key: str, default: T = None) -> T:  # type: ignore[assignment]
    return _cache.get(key, default)


def set(key: str, value: Any, **kwargs: Any) -> None:
    _cache.set(key, value, **kwargs)


def invalidate(key: str) -> bool:
    return _cache.invalidate(key)


def clear() -> None:
    _cache.clear()
