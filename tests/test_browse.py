"""Tests for browselibrary navigation fixes.

Regression: ``browselibrary items mode:genres`` crashed with a JSON -32603
('id') because the genres query selected only ``genre`` while the renderer
read ``r["id"]``.
"""

import asyncio
import json
import sqlite3

from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI


def _db(tmp_path):
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT, year INTEGER, url TEXT,
                             audio INTEGER DEFAULT 1, content_type TEXT);
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


def _search_menu(tmp_path, monkeypatch, args):
    monkeypatch.setattr(api_mod, "_library_db_path",
                        lambda: str(tmp_path / "lyrion.db"))

    async def run():
        return await JSONRPCAPI()._json_browselibrary("browselibrary", args)

    return asyncio.run(run())


def test_browselibrary_search_answers_perls_search_menu(tmp_path, monkeypatch):
    """``mode:search`` is the search MENU, not a title query.

    Perl ``_search``/``searchItems`` (``Slim/Menu/BrowseLibrary.pm:978-1070``)
    answers five ``type: search`` rows (Artists/Albums/Works/Songs/Playlists)
    with ``icon => 'html/images/search.png'``; live Perl 9.1.1 (read-only
    2026-09-14) returns exactly those five with ``title`` "Suchen" and
    ``window.windowStyle`` ``home_menu``.  The port used to run a title LIKE
    query here, which returned 0 rows without a ``search:`` token.
    """
    res = _search_menu(tmp_path, monkeypatch,
                       ["items", "0", "10", "mode:search"])
    loop = res.get("loop_loop") or []
    assert [i["name"] for i in loop] == ["Artists", "Albums", "Works", "Songs",
                                         "Playlists"], loop
    assert all(i["type"] == "search" and i["hasitems"] == 1 for i in loop)
    assert [i["image"] for i in loop] == ["html/images/search.png"] * 5
    assert res.get("title")

    menu = _search_menu(tmp_path, monkeypatch,
                        ["items", "0", "10", "menu:1", "mode:search"])
    rows = menu["item_loop"]
    assert [r["type"] for r in rows] == ["search"] * 5
    assert menu["window"] == {"windowStyle": "home_menu"}
    # the tap's request params (Perl ``XMLBrowser.pm:1192-1205``)
    assert rows[3]["actions"]["go"]["params"]["search"] == "__TAGGEDINPUT__"
    assert rows[3]["actions"]["go"]["params"]["cachesearch"] == "SONGS"
    assert rows[3]["input"]["len"] == 1
    assert set(rows[3]["input"]) == {"len", "processingPopup", "softbutton1",
                                     "softbutton2", "title", "help"}


def test_browselibrary_search_drills_into_the_target_feed(tmp_path, monkeypatch):
    """A search sends ``item_id:<row>`` + ``search:<text>`` (``:1036-1064``).

    Perl's search rows point at the *feeds*
    (``url => $browseLibraryModeMap{'tracks'}`` etc.); XMLBrowser walks that
    feed with the query, so ``item_id:3`` (Songs) + ``search:Sunset`` lists
    matching tracks.
    """
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             audio INTEGER DEFAULT 1, content_type TEXT);
        INSERT INTO tracks (id, title) VALUES (1, 'Sunset Orion'), (2, 'Other Song');
        """
    )
    con.commit()
    con.close()
    res = _search_menu(tmp_path, monkeypatch,
                       ["items", "0", "10", "mode:search", "item_id:3",
                        "search:Sunset"])
    loop = res.get("loop_loop") or []
    assert len(loop) == 1, f"search must return only matches, got {loop}"
    assert loop[0]["name"] == "Sunset Orion"


def test_browselibrary_playlists_feed_is_not_the_album_list(tmp_path, monkeypatch):
    """``mode:playlists`` is the saved-playlists feed (``:2154-2222``).

    With no saved playlist the live Perl answer is XMLBrowser's single EMPTY
    placeholder row (``:841-846``: ``{"id": "<sid>.0", "type": "text",
    "title": "Leer", "isaudio": 0, "hasitems": 0}``, ``count`` 1) — before
    this fix the port fell through to the ALBUM list.
    """
    con = sqlite3.connect(tmp_path / "lyrion.db")
    con.executescript("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT);")
    con.commit()
    con.close()
    res = _search_menu(tmp_path, monkeypatch,
                       ["items", "0", "10", "mode:playlists"])
    loop = res.get("loop_loop") or []
    assert len(loop) == 1 and loop[0]["type"] == "text", loop
    assert loop[0]["isaudio"] == 0 and loop[0]["hasitems"] == 0
    assert "album_id" not in json.dumps(res)

    menu = _search_menu(tmp_path, monkeypatch,
                        ["items", "0", "10", "menu:1", "mode:playlists"])
    assert menu["window"] == {"windowStyle": "text_list"}
    assert menu["item_loop"][0]["type"] == "text"
    assert menu["item_loop"][0]["style"] == "itemNoAction"


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
    """A search that matches nothing must NOT fall back to listing all albums.

    The search runs in the row's target feed (``item_id:3`` = Songs,
    ``BrowseLibrary.pm:1057``); a query that matches no track answers 0 rows.
    """
    db_path = _lib_db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db_path)

    async def run():
        api = JSONRPCAPI()
        return await api._json_browselibrary(
            "browselibrary", ["items", "0", "10", "mode:search", "item_id:3",
                              "search:NoMatch"])

    result = asyncio.run(run())
    assert result.get("count", 0) == 0
    assert not (result.get("loop_loop") or [])


def _lib_db(tmp_path):
    """DB with two artists, each owning a distinct album."""
    db = tmp_path / "lib.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT, year INTEGER, url TEXT, tracknum INTEGER,
                             audio INTEGER DEFAULT 1, content_type TEXT);
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


def test_album_items_carry_hex_icon_id_for_artwork(tmp_path, monkeypatch):
    """Jive only asks for artwork when `icon-id` is a hex id and `icon` is
    the RELATIVE Perl form — it builds '/music/' .. iconId .. '/cover' ..
    size itself (share/jive/jive/slim/SlimServer.lua:1188-1190; a URL or a
    leading slash falls into the remote-URL branch and no cover is ever
    fetched, leaving every album row on the generic disc icon)."""
    db = tmp_path / "lib.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, url TEXT);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO albums (id, title, artwork) VALUES (5237, 'Some Album', '/covers/x.jpg');
        INSERT INTO tracks (id, title) VALUES (1, 'Song A');
        INSERT INTO tracks_albums (track, album) VALUES (1, 5237);
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(db))

    api = JSONRPCAPI()
    r = asyncio.run(api._json_browselibrary(
        False, ["items", "0", "5", "menu:1", "mode:albums", "useContextMenu:1"]))
    items = r.get("item_loop") or []
    assert items, "album list must not be empty"
    item = items[0]
    icon_id = item.get("icon-id")
    assert icon_id, "album items need icon-id or SqueezePlay shows placeholders"
    assert icon_id.isalnum() and all(c in "0123456789abcdefABCDEF-" for c in icon_id), (
        f"icon-id must be hex-shaped for Jive's artwork URL, got {icon_id!r}"
    )
    assert item.get("icon") == f"music/{icon_id}/cover"
