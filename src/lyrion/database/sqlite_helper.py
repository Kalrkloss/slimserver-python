"""
SQLite connection management for Lyrion Music Server.

Provides:
- Async connection pool using aiosqlite
- Session context managers
- Schema initialization
- Thread-safe initialization
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, TYPE_CHECKING

import aiosqlite
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import event, text
from sqlalchemy.pool import AsyncAdaptedQueuePool

from lyrion.database.schema import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("lyrion.db")


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_db_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_init_lock = threading.Lock()

#: Default database path
DEFAULT_DB_PATH: Path | None = None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

async def init_db(
    db_path: Path | str | None = None,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
) -> AsyncEngine:
    """
    Initialize the database engine and create schema if needed.

    This function is idempotent — calling it multiple times is safe.
    """
    global _db_engine, _session_factory, DEFAULT_DB_PATH

    if _db_engine is not None:
        return _db_engine

    db_path = Path(db_path) if db_path else (
        Path.home() / ".lyrion" / "Lyrion" / "Prefs" / "lyrion.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_DB_PATH = db_path

    # SQLite URL for SQLAlchemy asyncio
    db_url = f"sqlite+aiosqlite:///{db_path}"

    _db_engine = create_async_engine(
        db_url,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        # SQLite-specific options
        connect_args={
            "check_same_thread": False,
            # Busy timeout: the background library scan holds long write
            # transactions; other writers (playlist save, radio add) must
            # wait for the next scan commit instead of failing immediately.
            "timeout": 30.0,
        },
        # Use StaticPool for SQLite (avoids connection pool issues)
        poolclass=AsyncAdaptedQueuePool,
    )

    _session_factory = async_sessionmaker(
        _db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    # Create schema
    await _create_schema(_db_engine)

    logger.info("Database initialized at %s", db_path)
    return _db_engine


async def _create_schema(engine: AsyncEngine) -> None:
    """Create all tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Set SQLite pragmas for performance
    async with aiosqlite.connect(engine.url.database) as db:
        await db.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            PRAGMA foreign_keys = ON;
            PRAGMA cache_size = -64000;  -- 64MB
            PRAGMA temp_store = MEMORY;
            PRAGMA mmap_size = 268435456;  -- 256MB
        """)
        await db.commit()


async def close_db() -> None:
    """Close the database engine and all connections."""
    global _db_engine, _session_factory

    if _db_engine is not None:
        await _db_engine.dispose()
        _db_engine = None
        _session_factory = None
        logger.info("Database closed")


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def get_engine() -> AsyncEngine:
    """Return the active database engine."""
    if _db_engine is None:
        raise RuntimeError(
            "Database not initialized. Call init_db() first."
        )
    return _db_engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the active session factory."""
    if _session_factory is None:
        raise RuntimeError(
            "Database not initialized. Call init_db() first."
        )
    return _session_factory


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    """
    Async context manager for a database session.

    Usage:
        async with db_session() as session:
            result = await session.execute(select(Track))
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    session: AsyncSession = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def db_readonly_session() -> AsyncIterator[AsyncSession]:
    """
    Async context manager for a read-only database session.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    session: AsyncSession = _session_factory()
    try:
        # Set read-only transaction mode
        await session.execute(text("PRAGMA read_uncommitted = 1"))
        yield session
        await session.rollback()  # Don't commit reads
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Direct SQLite access (for high-performance batch operations)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def raw_connection() -> AsyncIterator[aiosqlite.Connection]:
    """
    Get a raw aiosqlite connection (outside of SQLAlchemy).
    
    Use this for bulk inserts, transactions, or when you need
    raw SQLite control.
    """
    if DEFAULT_DB_PATH is None:
        raise RuntimeError("Database not initialized.")
    conn = await aiosqlite.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


async def execute_raw(sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Execute a raw SQL query and return results as dicts."""
    async with raw_connection() as conn:
        async with conn.execute(sql, parameters) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def execute_many_raw(sql: str, parameters: list[tuple[Any, ...]]) -> int:
    """Execute a raw SQL statement with multiple parameter sets."""
    async with raw_connection() as conn:
        await conn.executemany(sql, parameters)
        await conn.commit()
        return len(parameters)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def db_health_check() -> bool:
    """Return True if the database is accessible."""
    try:
        async with db_session() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("Database health check failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Migration support
# ---------------------------------------------------------------------------

async def get_schema_version() -> int:
    """Get the current schema version from the DB."""
    try:
        result = await execute_raw(
            "SELECT value FROM meta WHERE key = 'schema_version' LIMIT 1"
        )
        if result:
            return int(result[0]["value"])
    except Exception:
        pass
    return 0


async def set_schema_version(version: int) -> None:
    """Set the schema version in the DB."""
    async with raw_connection() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
        await conn.commit()
