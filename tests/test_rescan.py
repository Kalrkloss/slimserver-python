"""Rescan behavior tests: abort flag, scan modes, deletion reconciliation.

Regression (gap analysis, 2026-09): rescan ignored the LMS scan mode and
never removed tracks whose files disappeared from disk; `abortscan` was a
no-op that only acknowledged. Perl semantics: a FULL rescan reconciles
deletions (and drops orphaned albums/contributors), non-full modes are
additive, and abortscan stops the running scan.
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lyrion.database.schema import Base
from lyrion.media.importer import ImportConfig, MusicImporter
from lyrion.media.scan_state import SCAN_STATE


@pytest.fixture
def lib_db(tmp_path, monkeypatch):
    """Point the global db_session at a throwaway DB + patch the scanner."""
    db_path = tmp_path / "lyrion.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    @asynccontextmanager
    async def _session_ctx():
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    # import_music() does `from lyrion.database.sqlite_helper import
    # db_session` at call time → patching the module attribute works.
    monkeypatch.setattr("lyrion.database.sqlite_helper.db_session", _session_ctx)

    async def _fake_scan(self, file_path):
        # Album varies per folder so orphan-album cleanup is observable.
        return SimpleNamespace(
            title=file_path.stem,
            artist="Test Artist",
            album=file_path.parent.name,
            compilation=False,
            mimetype="audio/mpeg",
            duration=180000,
        )

    monkeypatch.setattr(
        "lyrion.media.scanner.MediaScanner.scan_single_file", _fake_scan)

    yield db_path
    asyncio.run(engine.dispose())


def _count(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _make_lib(music_dir):
    for folder in ("AlbumA", "AlbumB"):
        (music_dir / folder).mkdir(parents=True, exist_ok=True)
    one = music_dir / "AlbumA" / "one.mp3"
    two = music_dir / "AlbumB" / "two.mp3"
    one.write_bytes(b"x")
    two.write_bytes(b"x")
    return one, two


def test_abort_flag_lifecycle():
    # request → set; a fresh start() clears it (per-scan state).
    SCAN_STATE.reset()
    assert not SCAN_STATE.abort_requested
    SCAN_STATE.request_abort()
    assert SCAN_STATE.abort_requested
    SCAN_STATE.start(total=1)
    assert not SCAN_STATE.abort_requested


def _count_where(db_path, table, where="") -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def test_full_rescan_reconciles_deletions(tmp_path, lib_db):
    music = tmp_path / "music"
    one, two = _make_lib(music)

    async def run():
        imp = MusicImporter(ImportConfig(source_path=music))
        st1 = await imp.import_music()
        assert st1.imported_files == 2, "initial scan imports both files"
        # the scan also stores a ``dir`` row per walked directory (Perl does
        # the same while scanning, Slim/Utils/Scanner/Local/AIO.pm:62-67)
        assert _count_where(lib_db, "tracks", "content_type = 'dir'") == 3
        two.unlink()
        st2 = await imp.import_music()
        assert st2.deleted_files == 1, "full rescan must remove the vanished file"

    asyncio.run(run())
    assert _count_where(lib_db, "tracks", "content_type != 'dir'") == 1
    assert _count(lib_db, "albums") == 1, "orphaned album must be cleaned up"


def test_additive_mode_keeps_missing_tracks(tmp_path, lib_db):
    music = tmp_path / "music"
    one, two = _make_lib(music)

    async def run():
        imp = MusicImporter(ImportConfig(source_path=music))
        await imp.import_music()
        assert _count_where(lib_db, "tracks", "content_type != 'dir'") == 2
        two.unlink()
        # mode != full → additive refresh, deletions NOT reconciled
        imp2 = MusicImporter(ImportConfig(source_path=music, mode="playlists"))
        st2 = await imp2.import_music()
        assert st2.deleted_files == 0
        assert _count_where(lib_db, "tracks",
                            "content_type != 'dir'") == 2, \
            "additive scan must not delete"

    asyncio.run(run())


def test_abort_stops_scan(tmp_path, lib_db, monkeypatch):
    music = tmp_path / "music"
    for i in range(8):
        f = music / "AlbumA" / f"t{i:02d}.mp3"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")

    async def _slow_scan(self, file_path):
        await asyncio.sleep(0.05)
        return SimpleNamespace(
            title=file_path.stem, artist="Test Artist", album="AlbumA",
            compilation=False, mimetype="audio/mpeg", duration=180000)

    monkeypatch.setattr(
        "lyrion.media.scanner.MediaScanner.scan_single_file", _slow_scan)

    async def run():
        imp = MusicImporter(ImportConfig(source_path=music, batch_size=1))
        task = asyncio.create_task(imp.import_music())
        # Let the scan actually start, then abort it mid-run.
        for _ in range(200):
            if SCAN_STATE.snapshot()["scanning"]:
                await asyncio.sleep(0.05)
                break
            await asyncio.sleep(0.01)
        SCAN_STATE.request_abort()
        stats = await asyncio.wait_for(task, timeout=20)
        return stats

    stats = asyncio.run(run())
    # Abort must cut the scan short (some files may have slipped in before
    # the flag landed — the point is: not all 8, no hang, state reset).
    assert 0 <= stats.imported_files < 8
    assert stats.deleted_files == 0, "aborted scan must not reconcile deletions"
    assert _count(lib_db, "tracks") == stats.imported_files
    assert not SCAN_STATE.snapshot()["scanning"], "scan state must be reset"
