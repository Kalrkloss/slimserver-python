"""Tests for the additive legacy-DB migration (migrate_legacy_db).

Regression (2026-09): schema.py gained ``albums.albumartist_sort`` for the
new album identity (title + album artist), but create_all never adds
columns to EXISTING tables — an old DB would crash the importer's Album
select on the next rescan. The migration must add the column idempotently.
"""

import asyncio
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lyrion.database.sqlite_helper import migrate_legacy_db


def _legacy_db(tmp_path):
    """Create a DB with the PRE-2026-09 albums table (no artist column)."""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titlesort VARCHAR(255) NOT NULL,
            title VARCHAR(255) NOT NULL,
            year INTEGER,
            compilation INTEGER NOT NULL DEFAULT 0,
            artwork VARCHAR(1000),
            UNIQUE (titlesort, year)
        );
        CREATE TABLE tracks (id INTEGER PRIMARY KEY);
        INSERT INTO albums (titlesort, title, year) VALUES
            ('greatest hits', 'Greatest Hits', 2001),
            ('greatest hits', 'Greatest Hits', 2005);
        """
    )
    con.commit()
    con.close()
    return db


def test_migration_adds_column_idempotently(tmp_path):
    db = _legacy_db(tmp_path)

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        try:
            first = await migrate_legacy_db(engine)
            second = await migrate_legacy_db(engine)
        finally:
            await engine.dispose()
        return first, second

    first, second = asyncio.run(run())
    assert "albums.albumartist_sort" in first
    assert second == [], "second run must be a no-op (idempotent)"

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cols = [r[1] for r in con.execute("PRAGMA table_info(albums)")]
    con.close()
    assert "albumartist_sort" in cols


def test_migration_keeps_legacy_rows(tmp_path):
    db = _legacy_db(tmp_path)

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        try:
            await migrate_legacy_db(engine)
        finally:
            await engine.dispose()

    asyncio.run(run())
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT titlesort, year, albumartist_sort FROM albums "
        "ORDER BY year").fetchall()
    con.close()
    assert len(rows) == 2
    assert rows[0][2] is None, "legacy rows keep NULL artist sort"
