"""Tests for browse filter fixes.

Regression: ``albums 0 10 artist_id:1`` returned BOTH artists' albums
because the artist contributor was JOINed but never added as a WHERE
predicate.
"""

import asyncio
import sqlite3

from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI


def _db(tmp_path):
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER, artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT,
                             audio INTEGER DEFAULT 1, content_type TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        INSERT INTO albums (id, title) VALUES (10, 'Artist One Album'), (20, 'Artist Two Album');
        INSERT INTO contributors (id, name) VALUES (1, 'Artist One'), (2, 'Artist Two');
        INSERT INTO tracks (id, title) VALUES (1, 't1'), (2, 't2');
        INSERT INTO tracks_albums (track, album) VALUES (1, 10), (2, 20);
        INSERT INTO tracks_contributors (track, contributor, role) VALUES (1, 1, 1), (2, 2, 1);
        """
    )
    con.commit()
    con.close()
    return str(db)


def test_browselibrary_bmf_returns_child_folder_names(tmp_path, monkeypatch):
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, url TEXT,
                             audio INTEGER DEFAULT 1, content_type TEXT);
        INSERT INTO tracks (id, url) VALUES
            (1, 'file:///music/Rock/a.mp3'), (2, 'file:///music/Jazz/b.mp3');
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(db))

    async def run():
        api = JSONRPCAPI()
        return await api._json_browselibrary(
            "browselibrary", ["items", "0", "10", "mode:bmf",
                              "search:file:///music"])

    result = asyncio.run(run())
    loop = result.get("loop_loop") or []
    names = {it.get("name") for it in loop}
    assert names == {"Rock", "Jazz"}, f"bmf child names wrong: {names}"


def test_albums_artist_id_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))

    async def run():
        api = JSONRPCAPI()
        return await api._json_browse("albums", ["0", "10", "artist_id:1"])

    result = asyncio.run(run())
    loop = result.get("albums_loop") or result.get("loop_loop") or []
    titles = {it.get("album") or it.get("title") or it.get("name") for it in loop}
    assert titles == {"Artist One Album"}, f"artist_id filter leaked: {titles}"
