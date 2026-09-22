"""``mode:playlistTracks`` — Perl's ``_playlistTracks`` feed (saved playlist).

Perl's feed for a SAVED playlist (``Slim/Menu/BrowseLibrary.pm:2224-2307``)
carries the same ``playall``/``addall`` base actions as a track drill
(:2295-2306), only with the playlist id as the fixed param and **without** the
``sort`` a drill adds::

    playall => { command => ['playlistcontrol'],
                 fixedParams => {cmd => 'load',
                                 %{&_tagsToParams(\\@searchTags)}},   # playlist_id
                 variables   => [play_index => 'play_index'] }
    addall  => { command => ['playlistcontrol'], variables => [],
                 fixedParams => {cmd => 'add', %{&_tagsToParams(\\@searchTags)}} }

``_tagsToParams`` (:1069-1077) turns the feed's ``passthrough``
``[@searchTags, 'playlist_id:' . $row->{id}]`` (:2219) into
``playlist_id => <id>``; ``XMLBrowser.pm:954``/``:959`` pick ``playall``/
``addall`` for the window's ``play``/``add`` and ``:1682-1686`` names the
parameter map after the ACTION (``itemsParams 'playallParams'`` /
``'addallParams'``).  Every row carries the absolute ``play_index``
(``$_->{'play_index'} = $offset++``, :2263).

Live Perl 9.1.1 (read-only 2026-09-22,
``browselibrary items 0 10 menu:1 mode:playlistTracks playlist_id:1`` — its
playlist store is empty, so the row is the ``Empty`` placeholder, but the base
actions are data independent)::

    play  = {player:0, cmd:["playlistcontrol"], itemsParams:"playallParams",
             nextWindow:"nowPlaying",
             params:{cmd:"load", playlist_id:"1", menu:1}}     # no sort!
    add   = {player:0, cmd:["playlistcontrol"], itemsParams:"addallParams",
             params:{cmd:"add", playlist_id:"1", menu:1}}
    add-hold = {player:0, cmd:["playlistcontrol"], itemsParams:"commonParams",
                params:{cmd:"insert", menu:1}}
    go    = {player:0, cmd:["trackinfo","items"], itemsParams:"commonParams",
             params:{menu:1}}
    more  = {player:0, cmd:["trackinfo","items"], itemsParams:"commonParams",
             window:{isContextMenu:1}, params:{menu:1}}
    playControl = {player:0, cmd:["browselibrary","items"],
                   itemsParams:"playControlParams", window:{isContextMenu:1},
                   params:{_index:"0", _quantity:"10", menu:"1",
                           mode:"playlistTracks", playlist_id:"1"}}

This port answered the SINGLE-TRACK form (``itemsParams 'commonParams'``,
``params {cmd:load, menu:1}`` — no playlist id, no ``playallParams``): a tap
loaded one track and ignored the playlist entirely.  Everything here runs
in-process against the real ``JSONRPCAPI`` with a temp library — no server
start/stop, no DB write, no live command.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

MAC = "1c:87:2c:47:fc:36"
MAC_CLEAN = "1C872C47FC36"
PLAYLIST_ID = 45
TRACK_IDS = (10, 11, 12)


def _build_db(path) -> str:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT,
                             year INTEGER, url TEXT, tracknum INTEGER,
                             duration REAL, audio INTEGER DEFAULT 1,
                             content_type TEXT);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        CREATE TABLE playlists (id INTEGER PRIMARY KEY, playlist TEXT,
                                name TEXT, changed TEXT, pl_type INTEGER,
                                remote INTEGER DEFAULT 0,
                                disabled INTEGER DEFAULT 0);
        CREATE TABLE playlist_items (id INTEGER PRIMARY KEY, playlist INTEGER,
                                     track INTEGER, position INTEGER, url TEXT);
        INSERT INTO tracks (id, title, genre, year, url, tracknum, duration)
        VALUES
            (10, 'Track One', 'Rock', 2000, 'file:///m/A/one.mp3', 1, 200.0),
            (11, 'Track Two', 'Rock', 2000, 'file:///m/A/two.mp3', 2, 210.0),
            (12, 'Other Hey', 'Jazz', 2001, 'file:///m/B/other.mp3', 1, 99.0);
        INSERT INTO playlists (id, playlist, name, changed, pl_type)
        VALUES (45, 'Meine Liste', 'Meine Liste', '2026-09-22', 0);
        INSERT INTO playlist_items (id, playlist, track, position) VALUES
            (1, 45, 12, 0), (2, 45, 10, 1), (3, 45, 11, 2);
        """
    )
    con.commit()
    con.close()
    return str(path)


@pytest.fixture()
def lib_db(tmp_path, monkeypatch):
    path = _build_db(tmp_path / "lib.db")
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def _isolated_players():
    prev = getattr(PlayerManager, "_instance", None)
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    yield
    PlayerManager._instance = prev


def _install(mode: str = "stop") -> PlayerState:
    """A connected, idle client — Perl's touch-to-play branch (:1259-1267)."""
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856, connected=True, power=True)
    player.mode = mode
    player.playlist = []
    player.playlist_position = 0
    player.playlist_total = 0
    pm = PlayerManager()
    pm.players[MAC_CLEAN] = player
    return player


def browse(args: list, pid: str | None = MAC) -> dict:
    async def run():
        return await JSONRPCAPI()._json_browselibrary(
            "browselibrary", [str(a) for a in args], pid)
    return asyncio.run(run())


ARGS = ["items", "0", "10", "menu:1", "mode:playlistTracks",
        f"playlist_id:{PLAYLIST_ID}"]


def test_play_is_perls_playall_form_with_the_playlist_id(lib_db):
    """``BrowseLibrary.pm:2295-2301`` → ``playall``, no ``sort``."""
    _install()
    actions = browse(ARGS)["base"]["actions"]
    assert actions["play"] == {
        "player": 0, "cmd": ["playlistcontrol"],
        "itemsParams": "playallParams", "nextWindow": "nowPlaying",
        "params": {"cmd": "load", "playlist_id": str(PLAYLIST_ID), "menu": 1},
    }, actions["play"]
    assert "sort" not in actions["play"]["params"]


def test_add_is_perls_addall_form_with_the_playlist_id(lib_db):
    """``BrowseLibrary.pm:2302-2306`` → ``addall`` (itemsParams addallParams)."""
    _install()
    actions = browse(ARGS)["base"]["actions"]
    assert actions["add"] == {
        "player": 0, "cmd": ["playlistcontrol"],
        "itemsParams": "addallParams",
        "params": {"cmd": "add", "playlist_id": str(PLAYLIST_ID), "menu": 1},
    }, actions["add"]


def test_rows_carry_the_absolute_play_index(lib_db):
    """``BrowseLibrary.pm:2263`` ``$_->{'play_index'} = $offset++``.

    Live Perl answers the same key on its placeholder row
    (``playallParams {play_index: null}``); the drill's window already had it
    (``test_play_control_item_paths``), the playlist window did not.
    """
    _install()
    res = browse(ARGS)
    rows = res["item_loop"]
    assert [r["playallParams"] for r in rows] == [
        {"play_index": 0}, {"play_index": 1}, {"play_index": 2}], rows
    assert res["count"] == 3


def test_go_is_the_feeds_info_action_and_the_tap_plays(lib_db):
    """``$actions{'items'} = $actions{'info'}`` (:2308) + ``:1429-1430``.

    The feed's ``info`` action is ``trackinfo items`` with
    ``itemsParams 'commonParams'`` and ``{menu:1}`` (live), and an all-audio
    window's ``go`` becomes its ``play`` (all rows take the touch-to-play
    branch, ``:1259-1267``).
    """
    _install()
    actions = browse(ARGS)["base"]["actions"]
    assert actions["go"] == actions["play"], actions["go"]
    assert actions["playControl"]["params"] == {
        "_index": "0", "_quantity": "10", "menu": "1",
        "mode": "playlistTracks", "playlist_id": str(PLAYLIST_ID)}, \
        actions["playControl"]["params"]
    assert actions["add-hold"]["itemsParams"] == "commonParams"
    assert actions["add-hold"]["params"] == {"cmd": "insert", "menu": 1}
    assert actions["more"]["params"] == {"menu": 1}


def test_more_is_the_info_action_without_the_playlist_id(lib_db):
    """Live Perl ``more`` = ``trackinfo items`` + ``{menu:1}`` (no fixedParams)."""
    _install()
    more = browse(ARGS)["base"]["actions"]["more"]
    assert more == {
        "player": 0, "cmd": ["trackinfo", "items"],
        "itemsParams": "commonParams", "window": {"isContextMenu": 1},
        "params": {"menu": 1},
    }, more


def test_a_track_drill_keeps_its_sort_and_single_track_add_hold(lib_db):
    """Gegenprobe: the album drill is untouched (``sort:albumtrack``)."""
    _install()
    drill = browse(["items", "0", "10", "menu:1", "mode:tracks",
                    "album_id:45"])["base"]["actions"]
    assert drill["play"]["params"] == {"cmd": "load", "album_id": "45",
                                       "sort": "albumtrack", "menu": 1}
    assert drill["add"]["params"] == {"cmd": "add", "album_id": "45",
                                      "sort": "albumtrack", "menu": 1}
    assert drill["add"]["itemsParams"] == "addallParams"


def test_the_search_list_keeps_the_single_track_form(lib_db):
    """Gegenprobe: ``mode:tracks search:`` has no playall (BrowseLibrary:1964)."""
    _install()
    actions = browse(["items", "0", "10", "menu:1", "mode:tracks",
                      "search:hey"])["base"]["actions"]
    assert actions["play"]["itemsParams"] == "commonParams"
    assert "playlist_id" not in actions["play"]["params"]
