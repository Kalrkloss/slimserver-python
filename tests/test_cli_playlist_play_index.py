"""``playlist play <drill> play_index:<n>`` — Perl's start index (CLI path).

``playlistXitemCommand`` reads ``my $jumpToIndex =
$request->getParam('play_index')`` (``Slim/Control/Commands.pm:1336``),
defaults it to 0 for ``play``/``load`` (:1524) and jumps to it after the list
is loaded (:3241-3243 ``$client->execute(['playlist','jump',$index,$fadeIn])``).
Live Perl's album drill sends it with every tap: the window's ``playall``
action carries ``itemsParams 'playallParams'`` and each row
``playallParams {play_index: <absolute row>}``
(``Slim/Menu/BrowseLibrary.pm:1871``, ``Slim/Control/XMLBrowser.pm:1328-1345``).
"""
import asyncio
import sqlite3

import lyrion.player as player_pkg
from lyrion.control import cli_commands
from lyrion.control.cli import CLIHandler, CLIContext

MAC = "02:11:22:33:44:55"


def _album_db(tmp_path) -> str:
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, tracknum INTEGER, url TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO tracks (id, title, tracknum) VALUES (1, 'A', 1), (2, 'B', 2), (3, 'C', 3), (4, 'D', 4);
        INSERT INTO tracks_albums (track, album) VALUES (1, 10), (2, 10), (3, 10), (4, 10);
        """
    )
    con.commit()
    con.close()
    return str(db)


class _FakePlayer:
    def __init__(self):
        self.mac = MAC
        self.mode = "stop"
        self.playlist: list = []
        self.playlist_position = 0
        self.playlist_total = 0


class _FakePM:
    def __init__(self):
        self.player = _FakePlayer()
        self.played: list = []

    def get_player(self, mac):
        return self.player if mac == MAC else None

    def playlist_clear(self, mac) -> bool:
        self.player.playlist.clear()
        self.player.playlist_position = 0
        self.player.playlist_total = 0
        return True

    def playlist_add(self, mac, track_id) -> bool:
        if track_id not in self.player.playlist:
            self.player.playlist.append(track_id)
        self.player.playlist_total = len(self.player.playlist)
        return True

    async def play_track(self, mac, track_id) -> bool:
        self.played.append(track_id)
        self.player.mode = "play"
        return True

    async def playlist_play(self, mac, index) -> bool:   # pragma: no cover
        self.played.append(("index", index))
        return True


def _run(tmp_path, monkeypatch, args: list) -> _FakePM:
    monkeypatch.setattr(cli_commands, "_library_db_path",
                        lambda: _album_db(tmp_path))
    pm = _FakePM()
    monkeypatch.setattr(player_pkg, "PlayerManager", lambda: pm)
    asyncio.run(cli_commands.cmd_playlist_play(
        CLIHandler(), CLIContext(player_id=MAC), args))
    return pm


def test_album_play_index_starts_at_the_tapped_row(tmp_path, monkeypatch):
    pm = _run(tmp_path, monkeypatch, ["album_id:10", "play_index:2"])
    assert pm.player.playlist == [1, 2, 3, 4], "the whole album is loaded"
    assert pm.played == [3], "row 3 of the album starts (Perl :1336/:3241)"


def test_album_without_play_index_starts_the_first_row(tmp_path, monkeypatch):
    pm = _run(tmp_path, monkeypatch, ["album_id:10"])
    assert pm.player.playlist == [1, 2, 3, 4]
    assert pm.played == [1], "Perl's default index 0 (:1524)"


def test_album_play_index_wraps_past_the_end(tmp_path, monkeypatch):
    """``playlistJumpCommand`` wraps an absolute index (``Commands.pm:1005``/
    ``:1010-1013``): 6 % 4 = 2."""
    pm = _run(tmp_path, monkeypatch, ["album_id:10", "play_index:6"])
    assert pm.played == [3]
