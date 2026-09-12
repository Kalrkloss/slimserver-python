"""Album browse: the artist line comes from the ALBUM's contributors.

Regression (live 2026-09-12): the album list built the artist line by joining
`tracks_contributors` for every track of every album on the page. SQLite
planned that as `SEARCH tc USING INDEX idx_tc_role (role=?)` — a scan over all
60k role rows — and one 200-album page took 2281 ms (profiled: the only hot
spot). SqueezePlay pages the album list and only opens the stream afterwards,
so every album pick was silent for ~20 s.

`albums_contributors` is the album→artist relation (the same one Perl's
BrowseLibrary album items use) and is indexed on album: 1 ms for the same
page. These tests pin the semantics, not the timing: an album whose TRACKS
credit other artists must still show the ALBUM artist.
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
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork INTEGER);
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, url TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER, position INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE albums_contributors (album INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);

        INSERT INTO contributors (id, name) VALUES
            (1, 'Album Artist'), (2, 'Track Artist'), (3, 'Other Artist');
        INSERT INTO albums (id, title, artwork) VALUES
            (10, 'Solo Album', 1), (11, 'Sampler', 1);
        INSERT INTO tracks (id, title, url) VALUES
            (100, 'Song A', 'file:///m/a.mp3'),
            (101, 'Song B', 'file:///m/b.mp3'),
            (102, 'Song C', 'file:///m/c.mp3');
        INSERT INTO tracks_albums (track, album, position) VALUES
            (100, 10, 1), (101, 11, 1), (102, 11, 2);
        -- every track credits a DIFFERENT artist than its album
        INSERT INTO tracks_contributors (track, contributor, role) VALUES
            (100, 2, 1), (101, 2, 1), (102, 2, 1);
        -- album 10 has one artist, album 11 is a compilation
        INSERT INTO albums_contributors (album, contributor, role) VALUES
            (10, 1, 1), (11, 1, 1), (11, 3, 1);
        """
    )
    con.commit()
    con.close()
    return str(db)


def _albums(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    api = JSONRPCAPI()
    res = asyncio.run(api._json_browselibrary(
        "items", ["0", "200", "menu:1", "mode:albums", "useContextMenu:1"]))
    return {i["text"].split("\n")[0]: i["text"] for i in res["item_loop"]}


def test_album_artist_line_uses_album_contributors(tmp_path, monkeypatch):
    albums = _albums(tmp_path, monkeypatch)
    assert albums["Solo Album"] == "Solo Album\nAlbum Artist", (
        "the album artist line must come from albums_contributors, not from "
        "the tracks' contributors"
    )


def test_compilation_album_shows_diverse_interpreted(tmp_path, monkeypatch):
    albums = _albums(tmp_path, monkeypatch)
    assert albums["Sampler"] == "Sampler\nDiverse Interpreten"
