"""Entscheidender Test: adoptiert der Importer die Altzeile in einer DB-Kopie?

Read-only gegenüber der Live-DB (arbeitet auf /tmp/rgtest.db).
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

SRC = "/home/keiner/.lyrion/Lyrion/Prefs/lyrion.db"
DST = "/tmp/rgtest.db"


def counts(engine_db):
    db = sqlite3.connect(engine_db)
    try:
        return (db.execute("SELECT COUNT(*) FROM albums").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM albums WHERE titlesort=?",
                           ("open your mind and your trousers",)).fetchone()[0])
    finally:
        db.close()


async def main():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from lyrion.media.importer import MusicImporter

    shutil.copy(SRC, DST)
    db = sqlite3.connect(DST)
    db.execute("UPDATE albums SET albumartist_sort='anderer' WHERE id=2")
    db.commit()
    db.close()
    print("vorher (Alben, davon das Scooter-Album):", counts(DST))

    engine = create_async_engine(f"sqlite+aiosqlite:///{DST}")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    importer = MusicImporter()
    info = SimpleNamespace(title="05-scooter-test", artist="Scooter",
                           album="Open Your Mind And Your Trousers", year=2024,
                           duration=1000, genre="Rock", albumartist="Scooter")
    async with Session() as session:
        try:
            await importer._import_batch(session, [(Path("/tmp/x/05.mp3"), info)])
            await session.commit()
            print("Import: OK")
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            print("Import: FEHLER:", type(exc).__name__, str(exc)[:300])
    await engine.dispose()
    print("nachher (Alben, davon das Scooter-Album):", counts(DST))
    db = sqlite3.connect(DST)
    try:
        print("Zeile:", db.execute(
            "SELECT id, titlesort, year, albumartist_sort FROM albums "
            "WHERE titlesort=?", ("open your mind and your trousers",)).fetchall())
    finally:
        db.close()


asyncio.run(main())
