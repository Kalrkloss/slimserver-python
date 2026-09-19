"""``search:`` must filter the browse-library feeds (artists/genres/albums).

Symptom: My Music → Künstler/Stilrichtungen with a typed search ignores the
term.  Live comparison against the Perl LMS 9.1.1 (read-only, 2026-09-14) of
``browselibrary items 0 5 mode:<m> menu:1 [search:<term>]``::

    mode      Perl plain -> search   port before this fix
    artists   11171    -> 22        10604 -> 10604   search ignored
    genres    762      -> 91        713   -> 713     search ignored
    albums    7189     -> 107       9270  -> 118     ok
    tracks    1        -> 311       80441 -> 309     ok

Perl's browse-library feed passes ``$search`` into the row queries
(``Slim/Menu/BrowseLibrary.pm:1091-1137`` for artists; the genres feed filters
its name list the same way) — the port built the ``WHERE`` clause for albums
and tracks only.
"""

from __future__ import annotations

import asyncio
import sqlite3

from lyrion.web import api as api_mod
from lyrion.web import menus as menus_mod
from lyrion.web.api import JSONRPCAPI


def _db(tmp_path):
    db = tmp_path / "lyrion.db"
    if db.exists():                     # one library per test, created once
        return str(db)
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT,
                             year INTEGER, url TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             artwork INTEGER);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO tracks (id, title, genre, year) VALUES
            (1, 'Radio Song', 'Rock', 2000),
            (2, 'Silence', 'Jazz', 1999),
            (3, 'Radio Gaga', 'Rock', 2001);
        INSERT INTO contributors (id, name) VALUES
            (1, 'Radiohead'), (2, 'Quiet Band');
        INSERT INTO tracks_contributors (track, contributor, role) VALUES
            (1, 1, 1), (2, 2, 1), (3, 1, 1);
        INSERT INTO albums (id, title, year) VALUES
            (1, 'Radio Live', 2000), (2, 'Plain Album', 2001);
        INSERT INTO tracks_albums (track, album) VALUES (1, 1), (2, 2), (3, 1);
        """
    )
    con.commit()
    con.close()
    return str(db)


def _rows(tmp_path, monkeypatch, mode, *extra):
    _db(tmp_path)                       # the tiny library is created per test
    monkeypatch.setattr(api_mod, "_library_db_path",
                        lambda: str(tmp_path / "lyrion.db"))
    args = ["items", "0", "50", f"mode:{mode}", "menu:1", *extra]

    async def run():
        return await JSONRPCAPI()._json_browselibrary("browselibrary", args)

    return asyncio.run(run())


def _names(res):
    return [i.get("text") or i.get("name") for i in res["item_loop"]]


def test_artists_feed_filters_by_search(tmp_path, monkeypatch):
    plain = _rows(tmp_path, monkeypatch, "artists")
    assert plain["count"] == 2, plain
    filtered = _rows(tmp_path, monkeypatch, "artists", "search:radio")
    assert filtered["count"] == 1, filtered
    assert _names(filtered) == ["Radiohead"]
    # the item keeps Perl's browse-library shape (menu-mode row: text/type,
    # commonParams for the drill, presetParams for "add to favourites")
    item = filtered["item_loop"][0]
    assert item["type"] == "playlist"
    assert item["commonParams"] == {"artist_id": "1"}
    assert item["presetParams"]["favorites_url"] == \
        "db:contributor.name=Radiohead"


def test_genres_feed_filters_by_search(tmp_path, monkeypatch):
    plain = _rows(tmp_path, monkeypatch, "genres")
    assert plain["count"] == 2, plain                      # Jazz + Rock
    assert _names(plain) == ["Jazz", "Rock"]
    filtered = _rows(tmp_path, monkeypatch, "genres", "search:rock")
    assert filtered["count"] == 1, filtered
    assert _names(filtered) == ["Rock"]
    # without a term the full list stays (the bmf branch re-uses the same
    # variable for its folder path — no accidental filtering there)
    assert _rows(tmp_path, monkeypatch, "genres")["count"] == 2


def test_albums_feed_keeps_filtering(tmp_path, monkeypatch):
    """The already-working mode must not regress."""
    plain = _rows(tmp_path, monkeypatch, "albums")
    assert plain["count"] == 2
    filtered = _rows(tmp_path, monkeypatch, "albums", "search:radio")
    assert filtered["count"] == 1, filtered
    assert _names(filtered) == ["Radio Live"]


def test_search_filter_is_case_insensitive(tmp_path, monkeypatch):
    lower = _rows(tmp_path, monkeypatch, "artists", "search:radio")
    upper = _rows(tmp_path, monkeypatch, "artists", "search:RADIO")
    assert lower["count"] == upper["count"] == 1, (lower, upper)


def test_no_match_answers_an_empty_loop_not_an_error(tmp_path, monkeypatch):
    """Kein Treffer → Perls ``Empty``-Zeile, kein Fehler.

    Live Perl 9.1.1 (read-only) ``browselibrary items 0 4 menu:1 mode:genres
    search:zzzqq`` → ``{count: 1, offset: 0, item_loop: [{'text': 'Leer',
    'style': 'itemNoAction', 'action': 'none', 'type': 'text', …}]}``
    (Bug 7024, ``Slim/Control/XMLBrowser.pm:841-846``) — auch bei
    ``menu:browselibrary`` (der Form, die die App sendet).
    """
    empty = _rows(tmp_path, monkeypatch, "genres", "search:zzz")
    assert empty["count"] == 1
    assert [it["text"] for it in empty["item_loop"]] == [menus_mod.menu_title("EMPTY")]


def test_genres_falls_back_to_the_track_text_table(tmp_path, monkeypatch):
    """A legacy DB without the ``genres`` table still filters."""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT,
                             year INTEGER, url TEXT);
        INSERT INTO tracks (id, title, genre, year) VALUES
            (1, 'A', 'Rock', 2000), (2, 'B', 'Jazz', 1999);
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(db))

    async def run(*extra):
        return await JSONRPCAPI()._json_browselibrary(
            "browselibrary",
            ["items", "0", "50", "mode:genres", "menu:1", *extra])

    plain = asyncio.run(run())
    assert plain["count"] == 2, plain
    filtered = asyncio.run(run("search:rock"))
    assert filtered["count"] == 1, filtered
    assert _names(filtered) == ["Rock"]
