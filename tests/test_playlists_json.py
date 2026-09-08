"""Saved-playlist JSON/CLI tests (Perl playlistsQuery parity).

Regression (gap analysis, 2026-09): `playlists new` crashed with a NOT
NULL constraint error (raw insert omitted `remote`/`disabled`), and the
JSON 'playlists' command returned nothing useful — controllers couldn't
list saved playlists (Perl returns a `playlists_loop`).
"""

import asyncio
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lyrion.database.schema import Base
from lyrion.web.api import JSONRPCAPI


@pytest.fixture
def lib_db(tmp_path, monkeypatch):
    db_path = tmp_path / "lyrion.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    asyncio.run(engine.dispose())

    monkeypatch.setattr("lyrion.web.api._library_db_path", lambda: str(db_path))
    monkeypatch.setattr("lyrion.control.cli_commands._library_db_path",
                        lambda: str(db_path))
    return db_path


def _seed_playlist(db_path, name, remote=0, disabled=0, pl_type=0):
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "INSERT INTO playlists (playlist, name, changed, pl_type, remote, "
            "disabled) VALUES (?, ?, datetime('now'), ?, ?, ?)",
            (name, name, pl_type, remote, disabled))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _rows(db_path, sql, params=()):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def test_playlists_new_creates_row_without_constraint_error(lib_db):
    """The raw insert must satisfy all NOT NULL columns (remote/disabled)."""
    from lyrion.control.cli_commands import cmd_playlists

    async def run():
        out = await cmd_playlists(None, None, ["new", "Test-Playlist"])
        return out

    out = asyncio.run(run())
    rows = _rows(lib_db, "SELECT playlist, remote, disabled FROM playlists")
    assert len(rows) == 1
    assert rows[0][0] == "Test-Playlist"
    assert rows[0][1] == 0 and rows[0][2] == 0
    assert any("new:" in line for line in out)


def test_json_playlists_lists_saved(lib_db):
    _seed_playlist(lib_db, "Zulu"), _seed_playlist(lib_db, "Alpha")

    async def run():
        api = JSONRPCAPI()
        return await api._json_playlists(None, ["0", "10", "tags:u"])

    res = asyncio.run(run())
    loop = res.get("playlists_loop") or []
    assert res.get("count") == 2
    names = [it.get("playlist") for it in loop]
    assert names == ["Alpha", "Zulu"], f"order by name expected, got {names}"
    for it in loop:
        assert isinstance(it.get("id"), int)


def test_json_playlists_new_rename_delete_roundtrip(lib_db):
    async def run():
        api = JSONRPCAPI()
        created = await api._json_playlists(None, ["new", "name:Meine Liste"])
        pid = created.get("playlist_id")
        listed = await api._json_playlists(None, ["0", "10"])
        renamed = await api._json_playlists(None, ["rename", f"id:{pid}",
                                                   "name:Umbenannt"])
        row = _rows(lib_db, "SELECT name FROM playlists WHERE id = ?", (pid,))
        deleted = await api._json_playlists(None, ["delete", f"id:{pid}"])
        return pid, listed, renamed, row, deleted

    pid, listed, renamed, row, deleted = asyncio.run(run())
    assert pid is not None
    assert listed.get("count") == 1
    assert row and row[0][0] == "Umbenannt", "rename must update the row"
    assert deleted.get("deleted") == pid
    assert _rows(lib_db, "SELECT COUNT(*) FROM playlists")[0][0] == 0


def test_json_playlists_tracks_lists_items(lib_db):
    pid = _seed_playlist(lib_db, "Mix")
    con = sqlite3.connect(lib_db)
    try:
        con.execute(
            "INSERT INTO tracks (url, title, titlesort, duration, playcount, "
            "remote, audio, video, disabled, compilation, artflow_flag) "
            "VALUES (?, ?, ?, 180.0, 0, 0, 1, 0, 0, 0, 0)",
            ("file:///m/a.mp3", "Lied Eins", "lied eins"))
        con.commit()
        tid = con.execute("SELECT id FROM tracks LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO playlist_items (playlist, track, position, url, title, "
            "added) VALUES (?, ?, 0, 'file:///m/a.mp3', 'Lied Eins', "
            "datetime('now')), "
            "(?, NULL, 1, 'http://radio.example/x', 'Webradio', datetime('now'))",
            (pid, tid, pid))
        con.commit()
    finally:
        con.close()

    async def run():
        api = JSONRPCAPI()
        return await api._json_playlists(None, ["tracks", str(pid)])

    res = asyncio.run(run())
    loop = res.get("playlist_tracks_loop") or []
    assert res.get("count") == 2
    titles = [it.get("title") for it in loop]
    assert titles == ["Lied Eins", "Webradio"]
    assert loop[0].get("id") == 1  # first item is the local track id
    assert loop[1].get("url") == "http://radio.example/x"
