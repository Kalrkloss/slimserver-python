"""
SQLite connection management for Pyrion Music Server.

Provides:
- Async connection pool using aiosqlite
- Session context managers
- Schema initialization
- Thread-safe initialization
"""

from __future__ import annotations

import asyncio
import logging
import os
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

#: Busy timeout for every pooled connection ("database is locked" → wait).
#: Matches the previous aiosqlite default (``timeout=30.0``).
BUSY_TIMEOUT_S: float = 30.0
BUSY_TIMEOUT_MS: int = int(BUSY_TIMEOUT_S * 1000)

#: WAL checkpoint threshold in pages (Perl ``Slim/Utils/SQLiteHelper.pm:104``).
#: ``200`` for the server, ``10000`` while a scan runs: a checkpoint blocks
#: readers, which is exactly the "server goes deaf during the scan" symptom.
DEFAULT_WAL_AUTOCHECKPOINT = 200
SCANNER_WAL_AUTOCHECKPOINT = 10000


def wal_autocheckpoint() -> int:
    """``200``, or ``10000`` inside the scan process (Perl ``:104``).

    The scan worker sets ``LYRION_SCANNER=1`` for itself
    (:mod:`lyrion.media.scan_worker`), the port's counterpart of Perl's
    compile-time ``main::SCANNER`` constant (``scanner.pl:23``).
    """
    if os.environ.get("LYRION_SCANNER") == "1":
        return SCANNER_WAL_AUTOCHECKPOINT
    return DEFAULT_WAL_AUTOCHECKPOINT


def _default_db_path() -> Path:
    """Where the library DB lives when no path was configured.

    The platform layer decides the data root (Windows ``%ProgramData%\\Lyrion``,
    macOS ``~/Library/Application Support/Squeezebox``, Linux/Unix
    ``~/.lyrion/Lyrion``), so this is no longer a hardcoded Linux home path.
    An existing library from an earlier version always wins — losing an 80k
    track library to a directory change would be the worst possible outcome
    (Perl migrates the same way: ``Slim/Utils/OS/Unix.pm:115-127``
    ``migratePrefsFolder``).
    """
    from lyrion.platform import paths as platform_paths

    legacy = Path.home() / ".lyrion" / "Lyrion" / "Prefs" / "lyrion.db"
    if legacy.is_file():
        return legacy
    return platform_paths.port_dir("prefs") / "lyrion.db"


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

    db_path = Path(db_path) if db_path else _default_db_path()
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
            # Busy timeout in seconds (sqlite3 → ``sqlite3_busy_timeout``): a
            # writer must wait for the running scan's short transaction instead
            # of failing with "database is locked".  Perl leaves this to
            # DBD::SQLite and only guarantees short transactions + WAL
            # (``Slim/Utils/Scanner/Local.pm:471`` "Commit for every chunk when
            # using scanner.pl", ``Slim/Utils/SQLiteHelper.pm:98-111``); the
            # explicit PRAGMA below makes the setting visible on every pooled
            # connection.
            "timeout": BUSY_TIMEOUT_S,
        },
        # Use StaticPool for SQLite (avoids connection pool issues)
        poolclass=AsyncAdaptedQueuePool,
    )

    # PRAGMA foreign_keys is per-connection — the pragma run in
    # _create_schema applies only to that throwaway connection. Without it
    # on every pooled connection the schema's ondelete CASCADE/SET NULL
    # never fire and orphaned join rows/albums accumulate after track
    # deletes (e.g. rescan deletion reconciliation).
    @event.listens_for(_db_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        # Reader latency while a scan writes: WAL lets readers proceed, but a
        # *checkpoint* still blocks them, so the scan process raises
        # ``wal_autocheckpoint`` (Perl: ``PRAGMA wal_autocheckpoint = 200``,
        # and ``10000`` for the scanner, ``Slim/Utils/SQLiteHelper.pm:104``).
        cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        cursor.execute(f"PRAGMA wal_autocheckpoint = {wal_autocheckpoint()}")
        cursor.close()

    _session_factory = async_sessionmaker(
        _db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    # Create schema
    await _create_schema(_db_engine)
    # Additive migration for DBs created before a schema change: new
    # columns/indexes are never added to existing tables by create_all.
    await migrate_legacy_db(_db_engine)

    logger.info("Database initialized at %s", db_path)
    return _db_engine


async def migrate_legacy_db(engine: AsyncEngine) -> list[str]:
    """Apply additive migrations to DBs from older server versions.

    create_all only creates MISSING tables — columns added to the schema
    later never appear on existing DBs. Each migration is idempotent and
    guarded by a PRAGMA table_info check.

    2026-09 (album identity = title + album artist): add the nullable
    ``albums.albumartist_sort`` column. The unique index is intentionally
    NOT recreated here — legacy rows have no artist key, backfilling one
    would either violate uniqueness (same-title albums split by year) or
    mislabel artists; one full rescan under the new importer converges
    (old rows lose their tracks via retag, orphan cleanup removes them).

    Returns the list of applied migration names.
    """
    applied: list[str] = []

    def _ensure_albums_artist_sort(sync_conn) -> bool:
        cols = [r[1] for r in
                sync_conn.exec_driver_sql("PRAGMA table_info(albums)")]
        changed = False
        if "albumartist_sort" not in cols:
            sync_conn.exec_driver_sql(
                "ALTER TABLE albums ADD COLUMN albumartist_sort VARCHAR(255)")
            changed = True
        # ReplayGain des Albums (Schema.pm:1299-1322); Bestands-DBs haben die
        # Spalten nicht, SQLite kann sie nur additiv nachziehen. Jede Spalte
        # einzeln prüfen — ein bereits vorhandenes albumartist_sort darf die
        # RG-Spalten nicht überspringen.
        for column in ("replay_gain", "replay_peak"):
            if column not in cols:
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE albums ADD COLUMN {column} FLOAT")
                changed = True
        return changed

    try:
        async with engine.begin() as conn:
            changed = await conn.run_sync(_ensure_albums_artist_sort)
        if changed:
            applied.append("albums.albumartist_sort")
            logger.info("Migration: added albums.albumartist_sort")
    except Exception as exc:  # noqa: BLE001 - fresh DBs already have it
        logger.debug("Migration albums.albumartist_sort skipped: %s", exc)
    return applied


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
