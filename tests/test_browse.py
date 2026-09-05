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
