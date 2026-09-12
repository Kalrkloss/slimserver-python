"""Migration: additive Spalten auf Bestands-DBs (sqlite_helper.migrate_legacy_db).

Regression: die RG-Spalten waren in einem Tuple-Ausdruck nach einem
abschließenden Komma gelandet — sie wurden nie ausgeführt, und ein bereits
vorhandenes ``albumartist_sort`` hat den ganzen Block übersprungen. Ein
Bestands-DB-Schema ohne diese Spalten deckt beides auf.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lyrion.database.sqlite_helper import migrate_legacy_db


def _legacy_db(path: str) -> None:
    """Alte albums-Tabelle OHNE albumartist_sort/replay_gain/replay_peak."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY,
            title VARCHAR(255),
            year INTEGER
        );
        INSERT INTO albums (id, title, year) VALUES (1, 'Alt', 1999);
        """
    )
    db.commit()
    db.close()


def _columns(path: str) -> set[str]:
    db = sqlite3.connect(path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(albums)")}
    db.close()
    return cols


def _run_migration(path: str) -> list[str]:
    import asyncio

    async def _go() -> list[str]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            return await migrate_legacy_db(engine)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def test_legacy_db_gets_all_three_columns(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    applied = _run_migration(path)
    cols = _columns(path)
    assert {"albumartist_sort", "replay_gain", "replay_peak"} <= cols
    assert applied, "die Migration muss etwas gemeldet haben"


def test_replaygain_columns_are_added_even_when_artist_sort_exists(tmp_path):
    # Genau dieser Fall schlug fehl: albumartist_sort vorhanden → Block
    # übersprungen → RG-Spalten fehlten.
    path = str(tmp_path / "half.db")
    _legacy_db(path)
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE albums ADD COLUMN albumartist_sort VARCHAR(255)")
    db.commit()
    db.close()

    _run_migration(path)
    cols = _columns(path)
    assert "replay_gain" in cols and "replay_peak" in cols


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "twice.db")
    _legacy_db(path)
    _run_migration(path)
    cols_before = _columns(path)
    _run_migration(path)                     # zweiter Lauf darf nicht werfen
    assert _columns(path) == cols_before


def test_data_survives_the_migration(tmp_path):
    path = str(tmp_path / "data.db")
    _legacy_db(path)
    _run_migration(path)
    db = sqlite3.connect(path)
    row = db.execute("SELECT title, year FROM albums WHERE id = 1").fetchone()
    db.close()
    assert row == ("Alt", 1999)
