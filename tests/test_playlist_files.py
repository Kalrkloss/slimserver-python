"""Tests for local playlist files — Perl ``Slim::Formats::Playlists``.

A ``.m3u`` in the music folder is a child of Perl's ``musicfolder``/``bmf``
listing (``fileFilter`` keeps it, ``Slim/Utils/Misc.pm:900-903``) and tapping
it plays the tracks the FILE names — Perl expands the playlist instead of
treating it as a stream:

* ``Slim/Control/Commands.pm:1309`` ``playlistXitemCommand``
* ``Slim/Control/Commands.pm:3686-3693`` (local playlist URL → ``Playlist``)
* ``Slim/Formats/Playlists/M3U.pm:31-186`` / ``PLS.pm:29-93``
* ``Slim/Utils/Misc.pm:447-540`` ``fixPath`` (relative → playlist's own dir)
* ``Slim/Player/Playlist.pm:188`` ``addTracks``

Hermetic: no server, no live DB, no writes.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from lyrion.media import folders
from lyrion.music import playlists
from lyrion.web import api as api_mod

MAC = "1C:87:2C:47:FC:36"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No live prefs, no live library DB connection."""
    monkeypatch.setattr(folders, "_pref", lambda name, default="": default)
    monkeypatch.setattr(folders, "_ro_connection", lambda db_path=None: None)
    monkeypatch.setattr(folders, "_rw_connection", lambda db_path=None: None)
    folders.reset_caches()
    yield
    folders.reset_caches()


def _album(tmp_path: Path) -> Path:
    """An album folder with a playlist, music files and release decoys."""
    album = tmp_path / "Musik" / "Sava-Metamorphosis-2008"
    album.mkdir(parents=True)
    for name in ("01-caravane.mp3", "02-stances.mp3", "03-uti_var_hage.mp3"):
        (album / name).write_bytes(b"\0")
    (album / "00-release.nfo").write_text("release info\n")
    (album / "00-release.sfv").write_text("checksums\n")
    (album / "00-sava.m3u").write_text(
        "01-caravane.mp3\r\n"
        "#EXTINF:123,Sava - Stances\r\n"
        "02-stances.mp3\r\n"
        "\r\n"
        "missing-file.mp3\r\n"
        "http://example.invalid/stream.mp3\r\n"
        "03-uti_var_hage.mp3\r\n")
    return album


# ---------------------------------------------------------------------------
# is_local_playlist / suffix handling
# ---------------------------------------------------------------------------


def test_local_playlist_detection(tmp_path):
    """A local ``.m3u``/``.m3u8``/``.pls`` is expanded; a remote one is not.

    ``Slim/Control/Commands.pm:3686`` — ``isPlaylist($url) &&
    !isRemoteURL($url)``.  A remote ``.m3u`` is an internet-radio stream and
    must keep going through ``play_url``.
    """
    album = _album(tmp_path)
    m3u = album / "00-sava.m3u"
    assert playlists.is_local_playlist(str(m3u))
    assert playlists.is_local_playlist(folders.file_url_from_path(m3u))
    assert playlists.is_local_playlist("http://radio.example/x.m3u") is False
    assert playlists.is_local_playlist("http://radio.example/x.pls") is False
    assert playlists.is_local_playlist(str(album / "01-caravane.mp3")) is False
    assert playlists.is_local_playlist("") is False


def test_m3u_entries_skip_comments_and_missing_files(tmp_path):
    """``M3U.pm:63-186``: ``#``-lines are metadata, a missing entry is dropped.

    ``playlistEntryIsValid`` (``Base.pm:109-131``) refuses an entry that is not
    on disk; a remote entry stays a URL.
    """
    album = _album(tmp_path)
    entries = playlists.entries(str(album / "00-sava.m3u"))
    assert [e.rsplit("/", 1)[-1] for e in entries] == [
        "01-caravane.mp3", "02-stances.mp3",
        "stream.mp3", "03-uti_var_hage.mp3"]
    # the remote entry stays a URL (``Base.pm:112-115``: remote is valid as-is)
    assert entries[2] == "http://example.invalid/stream.mp3"
    assert entries[0] == str(album / "01-caravane.mp3")


def test_m3u_relative_entries_resolve_against_the_playlist_dir(tmp_path):
    """``fixPath`` (``Slim/Utils/Misc.pm:531-540``): ``catfile($base, $file)``.

    The playlist is in ``tmp_path`` and names a file inside ``Musik/Album`` —
    Perl resolves it against the PLAYLIST's directory, not the cwd.
    """
    album = _album(tmp_path)
    (tmp_path / "outside.m3u").write_text("Musik/Sava-Metamorphosis-2008/01-caravane.mp3\n")
    entries = playlists.entries(str(tmp_path / "outside.m3u"))
    assert entries == [str(album / "01-caravane.mp3")]


def test_m3u_entries_fall_back_to_the_audio_dirs(tmp_path):
    """``M3U.pm:153-166``: an unresolvable relative entry is retried per audio dir."""
    album = _album(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "loose.m3u").write_text("01-caravane.mp3\n")
    assert playlists.entries(str(elsewhere / "loose.m3u")) == []
    assert playlists.entries(str(elsewhere / "loose.m3u"),
                             media_dirs=[str(album)]) == [
        str(album / "01-caravane.mp3")]


def test_pls_entries_are_read_by_index(tmp_path):
    """``PLS.pm:29-93``: ``File<n>=`` from ``1`` up to the last index."""
    album = _album(tmp_path)
    (album / "list.pls").write_text(
        "[playlist]\nNumberOfEntries=3\n"
        "File1=01-caravane.mp3\nTitle1=Caravane\n"
        "File2=02-stances.mp3\n"
        "File3=03-uti_var_hage.mp3\n")
    entries = playlists.entries(str(album / "list.pls"))
    assert [e.rsplit("/", 1)[-1] for e in entries] == [
        "01-caravane.mp3", "02-stances.mp3", "03-uti_var_hage.mp3"]


def test_html_error_page_is_not_a_playlist(tmp_path):
    """``M3U.pm:126-128``: an HTML page downloaded as ``.m3u`` yields nothing."""
    page = tmp_path / "error.m3u"
    page.write_text("<!DOCTYPE html>\n<html><body>404</body></html>\n")
    assert playlists.entries(str(page)) == []


def test_utf8_bom_and_cp1252_entries(tmp_path):
    """``M3U.pm:63-84``: BOM stripped, an unknown encoding still decodes."""
    album = _album(tmp_path)
    (album / "bom.m3u").write_text("\ufeff01-caravane.mp3\n", encoding="utf-8")
    assert playlists.entries(str(album / "bom.m3u")) == [
        str(album / "01-caravane.mp3")]
    (album / "latin.m3u").write_bytes("02-stances.mp3\n".encode("cp1252"))
    assert playlists.entries(str(album / "latin.m3u")) == [
        str(album / "02-stances.mp3")]


# ---------------------------------------------------------------------------
# track_ids — the read-only library lookup
# ---------------------------------------------------------------------------


def _library_db(path: Path, urls: list[str]) -> str:
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE tracks (id INTEGER PRIMARY KEY, url TEXT);")
    con.executemany("INSERT INTO tracks (id, url) VALUES (?, ?)",
                    list(enumerate(urls, start=1)))
    con.commit()
    con.close()
    return str(path)


def test_track_ids_resolve_against_the_library(tmp_path):
    """The playlist's entries map to their ``tracks.id``, in file order."""
    album = _album(tmp_path)
    urls = [folders.file_url_from_path(album / n)
            for n in ("01-caravane.mp3", "02-stances.mp3", "03-uti_var_hage.mp3")]
    db = _library_db(tmp_path / "lyrion.db", urls)
    assert playlists.track_ids(str(album / "00-sava.m3u"), db_path=db) == [1, 2, 3]


def test_track_ids_drop_entries_missing_from_the_library(tmp_path):
    """Perl imports a missing entry (``Base.pm:56-68``); we skip it, never invent it.

    ``Slim/Formats/Playlists/Base.pm:56-68`` ``updateOrCreate`` writes a
    ``tracks`` row for an entry the library does not know.  This port does not
    write while playing, so such an entry is logged and left out.
    """
    album = _album(tmp_path)
    db = _library_db(tmp_path / "lyrion.db", [
        folders.file_url_from_path(album / "02-stances.mp3")])
    assert playlists.track_ids(str(album / "00-sava.m3u"), db_path=db) == [1]


def test_track_ids_of_a_remote_playlist_is_empty(tmp_path):
    """A remote playlist is not a local file — nothing to expand here."""
    db = _library_db(tmp_path / "lyrion.db", [])
    assert playlists.track_ids("http://radio.example/x.m3u", db_path=db) == []


# ---------------------------------------------------------------------------
# api wiring — the tap / playlist play of a local playlist file
# ---------------------------------------------------------------------------


class _Handler:
    def __init__(self):
        self.strm = []

    async def send_strm_to_player(self, mac, track_id):
        self.strm.append(track_id)
        return True

    async def send_remote_stream(self, mac, url, codec="m", **kw):
        self.strm.append(url)
        return True


class _Player:
    def __init__(self):
        self.mac = MAC
        self.power = False
        self.mode = "stop"
        self.playlist = []
        self.playlist_position = 0
        self.playlist_total = 0
        self.playlist_modified = 0
        self.remote = 0
        self.last_activity = 0.0


class _PM:
    def __init__(self):
        self.player = _Player()
        self._protocol_handler = _Handler()
        self.modes = []

    def get_player(self, mac):
        return self.player if mac == MAC else None

    def get_all_players(self):
        return [self.player]

    async def power_on_for_playback(self, player):
        player.power = True

    def set_mode(self, mac, mode):
        self.modes.append(mode)
        self.player.mode = mode


def test_bmf_tap_of_a_playlist_row_plays_the_file_contents(tmp_path, monkeypatch):
    """Tapping the ``.m3u`` row loads the tracks the file names.

    Perl's row carries the ``tracks`` row it created while browsing
    (``Slim/Control/Queries.pm:2255-2268``, live ``track_id 205378``); ours has
    no row, so the row keeps the file URL and the tap expands the file.
    """
    album = _album(tmp_path)
    urls = [folders.file_url_from_path(album / n)
            for n in ("01-caravane.mp3", "02-stances.mp3", "03-uti_var_hage.mp3")]
    db = _library_db(tmp_path / "lyrion.db", urls)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    monkeypatch.setattr(api_mod, "_bmf_musicdir_pref", lambda: str(tmp_path / "Musik"))

    pm = _PM()
    m3u_url = folders.file_url_from_path(album / "00-sava.m3u")
    asyncio.run(api_mod.JSONRPCAPI()._bmf_playlist(
        MAC, "play", [f"url:{m3u_url}", "mode:bmf", "menu:browselibrary",
                      "item_id:0"], pm=pm))

    assert pm.player.playlist == [1, 2, 3]
    assert pm.player.playlist_position == 0
    assert pm.player.mode == "play"
    assert pm._protocol_handler.strm == [1]


def test_bmf_tap_of_a_playlist_row_resolves_the_row_by_index(tmp_path, monkeypatch):
    """Without a ``url`` token the window's ``folder_id`` + index finds the row."""
    album = _album(tmp_path)
    db = _library_db(tmp_path / "lyrion.db", [
        folders.file_url_from_path(album / "01-caravane.mp3"),
        folders.file_url_from_path(album / "02-stances.mp3"),
        folders.file_url_from_path(album / "03-uti_var_hage.mp3")])
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    monkeypatch.setattr(api_mod, "_bmf_musicdir_pref", lambda: str(tmp_path / "Musik"))

    pm = _PM()
    asyncio.run(api_mod.JSONRPCAPI()._bmf_playlist(
        MAC, "play", ["mode:bmf", "menu:browselibrary",
                      f"folder_id:{album}", "item_id:0", "touchToPlay:0"], pm=pm))

    # row 0 of the listing is the ``.m3u`` (Perl's order: files by name)
    assert pm.player.playlist == [1, 2, 3]
    assert pm.player.mode == "play"


def test_playlist_play_of_a_local_playlist_url_expands_it(tmp_path, monkeypatch):
    """``playlist play url:<local .m3u>`` loads the entries, not a stream.

    ``Slim/Control/Commands.pm:1309`` ``playlistXitemCommand``: the URL is
    expanded through the playlist parsers (``:3686-3693``) and the tracks go
    into the queue (``Slim/Player/Playlist.pm:188``).
    """
    album = _album(tmp_path)
    db = _library_db(tmp_path / "lyrion.db", [
        folders.file_url_from_path(album / "01-caravane.mp3"),
        folders.file_url_from_path(album / "02-stances.mp3"),
        folders.file_url_from_path(album / "03-uti_var_hage.mp3")])
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)

    pm = _PM()
    api = api_mod.JSONRPCAPI()
    asyncio.run(api._json_control(
        pm, MAC, "playlist",
        ["play", f"url:{folders.file_url_from_path(album / '00-sava.m3u')}"]))

    assert pm.player.playlist == [1, 2, 3]
    assert pm.player.playlist_position == 0
    assert pm.player.mode == "play"


def test_remote_playlist_url_is_still_a_stream(tmp_path, monkeypatch):
    """A remote ``.m3u``/``.pls`` must keep going through ``play_url``.

    Perl only expands a playlist that is NOT remote
    (``Slim/Control/Commands.pm:3686``); internet radio depends on this.
    """
    pm = _PM()
    seen: list[str] = []

    async def fake_play_url(pid, url, title=""):
        seen.append(url)

    monkeypatch.setattr(pm, "play_url", fake_play_url, raising=False)
    api = api_mod.JSONRPCAPI()
    asyncio.run(api._json_control(pm, MAC, "playlist",
                                  ["play", "url:http://radio.example/x.m3u"]))
    assert seen == ["http://radio.example/x.m3u"]
    assert pm.player.playlist == []
