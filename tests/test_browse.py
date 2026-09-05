"""Tests for browselibrary navigation fixes.

Regression: ``browselibrary items mode:genres`` crashed with a JSON -32603
('id') because the genres query selected only ``genre`` while the renderer
read ``r["id"]``.
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
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT, year INTEGER, url TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        INSERT INTO tracks (id, title, genre, year) VALUES
            (1, 'A', 'Rock', 2000), (2, 'B', 'Jazz', 1999), (3, 'C', 'Rock', 2001);
        INSERT INTO contributors (id, name) VALUES (1, 'Artist One');
        INSERT INTO tracks_contributors (track, contributor, role) VALUES (1, 1, 1);
        """
    )
    con.commit()
    con.close()
    return str(db)


def test_browselibrary_search_returns_matching_tracks(tmp_path, monkeypatch):
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT);
        INSERT INTO tracks (id, title) VALUES (1, 'Sunset Orion'), (2, 'Other Song');
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(db))

    async def run():
        api = JSONRPCAPI()
        return await api._json_browselibrary(
            "browselibrary", ["items", "0", "10", "mode:search", "search:Sunset"])

    result = asyncio.run(run())
    loop = result.get("loop_loop") or []
    assert len(loop) == 1, f"search must return only matches, got {loop}"
    assert loop[0]["name"] == "Sunset Orion"


def test_browselibrary_genres_returns_items(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db_path)

    async def run():
        api = JSONRPCAPI()
        return await api._json_browselibrary(
            "browselibrary", ["items", "0", "10", "mode:genres"])

    result = asyncio.run(run())
    loop = result.get("loop_loop") or []
    assert loop, "genres browse must return items, not crash"
    # every item must have a string id and name
    for it in loop:
        assert "id" in it and "name" in it
    names = {it["name"] for it in loop}
    assert names == {"Rock", "Jazz"}


def test_albums_artist_id_filters(tmp_path, monkeypatch):
    """albums 0 10 artist_id:<id> must return only that artist's albums."""
    db_path = _lib_db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db_path)

    async def run():
        api = JSONRPCAPI()
        r1 = await api._json_browse("albums", ["0", "10", "artist_id:1"])
        r2 = await api._json_browse("albums", ["0", "10", "artist_id:2"])
        return r1, r2

    r1, r2 = asyncio.run(run())

    def names(resp):
        loop = resp.get("albums_loop") or resp.get("loop_loop") or []
        return [a.get("album") for a in loop]

    assert names(r1) == ["Greatest Hits"]
    assert names(r2) == ["Other Album"]


def test_browselibrary_search_nomatch_returns_empty(tmp_path, monkeypatch):
    """A search that matches nothing must NOT fall back to listing all albums."""
    db_path = _lib_db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db_path)

    async def run():
        api = JSONRPCAPI()
        return await api._json_browselibrary(
            "browselibrary", ["items", "0", "10", "mode:search", "search:NoMatch"])

    result = asyncio.run(run())
    assert result.get("count", 0) == 0
    assert not (result.get("loop_loop") or [])


def _lib_db(tmp_path):
    """DB with two artists, each owning a distinct album."""
    db = tmp_path / "lib.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT, year INTEGER, url TEXT, tracknum INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER, artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO albums (id, title) VALUES (10, 'Greatest Hits'), (99, 'Other Album');
        INSERT INTO contributors (id, name) VALUES (1, 'Artist One'), (2, 'Artist Two');
        INSERT INTO tracks (id, title) VALUES (1, 'Song A'), (2, 'Song B');
        INSERT INTO tracks_contributors (track, contributor, role) VALUES (1, 1, 1), (2, 2, 1);
        INSERT INTO tracks_albums (track, album) VALUES (1, 10), (2, 99);
        """
    )
    con.commit()
    con.close()
    return str(db)
