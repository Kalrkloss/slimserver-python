"""Tests for the JSON-RPC control dispatch (playlist album/artist play,
shuffle/repeat state).

Regression: JSON ``playlist play album_id:X`` fell through to replaying the
current item (the server's own album-play action was broken), and JSON
``playlist shuffle``/``repeat`` were swallowed into a no-op.
"""

import asyncio
import sqlite3

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

MAC = "02:11:22:33:44:55"


class _FakeHandler:
    async def send_strm_to_player(self, mac, track_id):
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        return True


def _fresh_pm():
    pm = object.__new__(PlayerManager)
    pm.players = {}
    pm._protocol_handler = _FakeHandler()
    p = PlayerState(mac=MAC, name="Test", ip="127.0.0.1", port=0)
    pm.players[p.mac] = p
    return pm, p


def _album_db(tmp_path):
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, tracknum INTEGER, url TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO tracks (id, title, tracknum) VALUES (1, 'A', 1), (2, 'B', 2), (3, 'C', 1);
        INSERT INTO tracks_albums (track, album) VALUES (1, 10), (2, 10), (3, 99);
        """
    )
    con.commit()
    con.close()
    return str(db)


def test_playlist_play_album_id_expands_tracks(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _album_db(tmp_path))
    pm, p = _fresh_pm()

    async def run():
        api = JSONRPCAPI()
        await api._json_control(pm, MAC, "playlist", ["play", "album_id:10"])

    asyncio.run(run())
    assert p.playlist == [1, 2]          # album 10 tracks, not album 99
    assert p.playlist_position == 0


# ── play_index: the album drill starts at the TAPPED row ───────────────────
#
# Perl's album drill window carries ``itemsParams "playallParams"`` and every
# row a ``playallParams {play_index: <absolute row>}``
# (Slim/Menu/BrowseLibrary.pm:1871, Slim/Control/XMLBrowser.pm:1328-1345), so
# the tap sends ``playlistcontrol cmd:load album_id:X sort:albumtrack
# play_index:N``.  The command reads it as ``$jumpIndex``
# (Slim/Control/Commands.pm:1878), hands it to the track loader (:2161
# ``['playlist','loadtracks','listRef',$tracks,undef,$jumpIndex]``) which
# reads it as the ``_index`` p5 (:1681) and jumps there (:1796
# ``['playlist','jump',$jumpToIndex]`` → playlistJumpCommand wraps an absolute
# index into the loaded list, :1005/:1010-1013).  Live raw proof against our
# server (album 4642, 12 tracks, tapped row 3): ``status`` answered
# ``playlist_tracks 12`` + ``playlist_cur_index 2`` + title ``Unter Eis``.

def _patch_start_on(monkeypatch, api, started: list) -> None:
    async def fake_start(pm_arg, player, index):
        started.append(index)
        return True

    monkeypatch.setattr(api, "_play_playlist_item", fake_start)


def test_load_with_play_index_starts_at_the_tapped_row(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _album_db(tmp_path))
    pm, p = _fresh_pm()
    started: list = []
    api = JSONRPCAPI()
    _patch_start_on(monkeypatch, api, started)

    async def run():
        await api._json_control(
            pm, MAC, "playlistcontrol",
            ["cmd:load", "album_id:10", "sort:albumtrack", "menu:1",
             "play_index:1"])

    asyncio.run(run())
    assert p.playlist == [1, 2], "the whole album is loaded"
    assert p.playlist_position == 1
    assert started == [1], "row 2 of the album starts, not its first track"


def test_load_without_play_index_still_starts_the_first_row(tmp_path,
                                                           monkeypatch):
    """Perl's default: an undefined ``play_index`` is index 0
    (``Commands.pm:1524``/``:1005``)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _album_db(tmp_path))
    pm, p = _fresh_pm()
    started: list = []
    api = JSONRPCAPI()
    _patch_start_on(monkeypatch, api, started)

    async def run():
        await api._json_control(
            pm, MAC, "playlistcontrol",
            ["cmd:load", "album_id:10", "sort:albumtrack", "menu:1"])

    asyncio.run(run())
    assert p.playlist == [1, 2]
    assert started == [0]


def test_load_play_index_beyond_the_end_wraps(tmp_path, monkeypatch):
    """An absolute index is wrapped into the playlist
    (``playlistJumpCommand``, ``Commands.pm:1005``/``:1010-1013``)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _album_db(tmp_path))
    pm, p = _fresh_pm()
    started: list = []
    api = JSONRPCAPI()
    _patch_start_on(monkeypatch, api, started)

    async def run():
        await api._json_control(
            pm, MAC, "playlistcontrol",
            ["cmd:load", "album_id:10", "sort:albumtrack", "menu:1",
             "play_index:3"])

    asyncio.run(run())
    assert p.playlist == [1, 2]
    assert started == [1], "3 % 2 = 1 — Perl's modulo wrap"


def test_playlist_shuffle_sets_state():
    pm, p = _fresh_pm()

    async def run():
        api = JSONRPCAPI()
        await api._json_control(pm, MAC, "playlist", ["shuffle", "1"])

    asyncio.run(run())
    assert p.shuffle == 1


def test_playlist_repeat_sets_state():
    pm, p = _fresh_pm()

    async def run():
        api = JSONRPCAPI()
        await api._json_control(pm, MAC, "playlist", ["repeat", "2"])

    asyncio.run(run())
    assert p.repeat == 2


def test_mixer_bass_does_not_change_volume():
    pm, p = _fresh_pm()
    p.volume = 50

    async def run():
        api = JSONRPCAPI()
        await api._json_control(pm, MAC, "mixer", ["bass", "25"])

    asyncio.run(run())
    assert p.volume == 50  # bass must not be routed to volume
