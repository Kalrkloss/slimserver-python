"""Now-Playing status fixes (R0.6-A) — SqueezePlay renders the current track
from ``item_loop[1]`` (Lua is 1-based → our ``item_loop[0]``) and reads
``params.track_id`` / ``icon-id`` from it; ``time``/``duration`` drive the
progress bar.

Client ground truth::

    jive/slim/Player.lua:1193   self.trackTime     = tonumber(event.data.time)
    jive/slim/Player.lua:1194   self.trackDuration = tonumber(event.data.duration)
    jive/slim/Player.lua:1197   self.playlistCurrentIndex =
                                    tonumber(event.data.playlist_cur_index) + 1
    jive/slim/Player.lua:269-282 _whatsPlaying(obj): obj.item_loop[1].params.track_id
                                    (local) else .text; artwork from
                                    obj.item_loop[1]["icon-id"] or .icon
    applets/NowPlaying/NowPlayingApplet.lua:117  item['params']['track_id']

Perl reference (read-only /tmp/lms-ref, public/9.2):
  * ``Queries.pm:5561-5645 _addJiveSong``: every item carries
    ``params = {track_id => id+0, playlist_index => $index}``, ``style =>
    'itemplay'`` and ``icon`` (from artwork_url) or ``icon-id`` (from
    coverid/artwork_track_id, radio.png for a cover-less remote track).
  * ``Queries.pm:4093-4101``: ``time`` = ``Slim::Player::Source::songTime``,
    ``duration`` = ``$song->duration()`` (the real track length).
  * ``Queries.pm:4186-4190``: ``$repeat += 0; playlist repeat`` and
    ``$shuffle += 0; playlist shuffle`` → JSON ints.
  * ``Queries.pm:4425-4431``: with ``menu:menu`` (menuMode) and index ``-``
    the item_loop window starts at ``playlist_cur_index``
    (``normalize($playlist_cur_index, $quantity, $songCount)``) — the current
    track is therefore the FIRST item.
  * ``docs/status-parity-analysis.md`` (live Python-vs-Perl diff) and
    ``.hermes/gap-analysis/02-control-cli-queries.md`` CTRL-03/04 (live Perl
    probe): ``playlist repeat``/``playlist shuffle`` are ints,
    ``playlist_cur_index`` is the STRING ``"0"``.
"""

import asyncio
import sqlite3

from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

MAC = "1c:87:2c:47:fc:36"
STREAM_URL = "http://stream.example.org:8000/live.mp3"

# Current-track durations come from the DB (like Perl's LENGTH tag).
DUR = {11: 180.0, 12: 245.5, 13: 300.25}


def _db(tmp_path):
    """Library DB with the columns ``_load_tracks`` selects (incl. the
    role/album join tables and album artwork)."""
    path = tmp_path / "lyrion.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY, title TEXT, url TEXT, duration REAL,
            year INTEGER, tracknum INTEGER, bitrate INTEGER, samplerate INTEGER,
            bitspersample INTEGER, genre TEXT, cover TEXT, remote INTEGER,
            disc INTEGER, filesize INTEGER, comment TEXT, lyrics TEXT,
            content_type TEXT
        );
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO tracks (id, title, url, duration, genre, tracknum, cover, remote, content_type)
        VALUES
            (11, 'First Song', 'file:///music/first.mp3', 180.0, 'Rock', 1, NULL, 0, 'mp3'),
            (12, 'Second Song', 'file:///music/second.mp3', 245.5, 'Rock', 2, NULL, 0, 'mp3'),
            (13, 'Third Song', 'file:///music/third.mp3', 300.25, 'Rock', 3, NULL, 0, 'mp3');
        INSERT INTO contributors (id, name) VALUES (7, 'The Artist');
        INSERT INTO tracks_contributors (track, contributor, role)
        VALUES (11, 7, 1), (12, 7, 1), (13, 7, 1);
        INSERT INTO albums (id, title, artwork) VALUES
            (45, 'The Album', '/music/art/cover.jpg');
        INSERT INTO tracks_albums (track, album)
        VALUES (11, 45), (12, 45), (13, 45);
        """
    )
    con.commit()
    con.close()
    return str(path)


class _PM:
    """Minimal PlayerManager stand-in (only what the status builder uses)."""

    def __init__(self, player):
        self._player = player

    def get_all_players(self):
        return [self._player]

    def get_player(self, mac):
        return self._player if mac in (self._player.mac, None) else None


def _player(playlist, position, **kw):
    p = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=58044)
    p.power = True
    p.playlist = list(playlist)
    p.playlist_position = position
    p.playlist_total = len(p.playlist)
    for key, value in kw.items():
        setattr(p, key, value)
    return p


def _status(player, args=None):
    api = JSONRPCAPI()
    args = args or ["-", "10", "menu:menu", "useContextMenu:1"]
    return asyncio.run(api._json_player_status(_PM(player), MAC, args))


# ----------------------------------------------------------------------
# (a) local track: real DB duration + running `time` + params.track_id
# ----------------------------------------------------------------------
def test_local_track_duration_is_db_length_and_time_is_elapsed(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    # current index 1 -> the window must START there (Perl menuMode)
    player = _player([11, 12, 13], 1, mode="play", elapsed=42.5)

    r = _status(player)

    # real track duration from the DB — neither 0 nor the elapsed position
    assert r["duration"] == DUR[12], f"duration must be the DB length, got {r['duration']!r}"
    assert r["duration"] != player.elapsed
    # running progress (STAT seconds) for the progress bar
    assert r["time"] == 42.5, f"time must be the live progress, got {r['time']!r}"

    item = r["item_loop"][0]
    assert item["params"]["track_id"] == 12
    assert item["params"]["playlist_index"] == 1
    assert item["duration"] == DUR[12]
    # local tracks also expose the album/artist ids (Perl item params)
    assert item["params"]["album_id"] == 45
    assert item["params"]["artist_id"] == 7


def test_item_loop_window_starts_at_current_track(tmp_path, monkeypatch):
    """SqueezePlay's _whatsPlaying reads item_loop[1] and Material's
    server.js:606 / SqueezeJS Base.js:389 read playlist_loop[0] as the
    current song — if the window starts at playlist index 0 the Now-Playing
    screen never follows the song. Perl: normalize($playlist_cur_index,
    $quantity, $songCount) (Queries.pm:4425)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([11, 12, 13], 2, mode="play", elapsed=7.0)

    r = _status(player, ["-", "10", "menu:menu"])

    assert r["item_loop"][0]["params"]["track_id"] == 13, (
        f"item_loop must start at the playing track, got {[i['params']['track_id'] for i in r['item_loop']]}"
    )
    assert r["playlist_loop"][0]["params"]["track_id"] == 13, (
        "playlist_loop[0] is the current song for Material/SqueezeJS"
    )
    assert r["count"] == len(r["item_loop"]) == 1
    assert r["offset"] == "2"
    # the absolute playlist index is preserved so a client can map the
    # window back onto the queue
    assert r["item_loop"][0]["playlist index"] == 2


def test_explicit_index_windows_the_queue(tmp_path, monkeypatch):
    """Material's queue page fetches `status <start> <count>` (lmsList) —
    the window must honour the start index (Perl normalize)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([11, 12, 13], 1, mode="play", elapsed=7.0)

    r = _status(player, ["0", "10", "menu:menu"])

    assert [i["playlist index"] for i in r["item_loop"]] == [0, 1, 2]
    assert [i["params"]["track_id"] for i in r["item_loop"]] == [11, 12, 13]
    assert r["offset"] == "0"


def test_stopped_player_keeps_duration_and_zero_time(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([12], 0, mode="stop", elapsed=99.0)

    r = _status(player)

    assert r["duration"] == DUR[12]
    assert r["time"] == 0


def test_paused_local_track_keeps_position(tmp_path, monkeypatch):
    """Perl's songTime() returns resumeTime while paused — the bar must not
    jump to zero on pause."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([12], 0, mode="pause", elapsed=55.0)

    r = _status(player)

    assert r["time"] == 55.0
    assert r["duration"] == DUR[12]


# ----------------------------------------------------------------------
# (b) stream: non-zero duration (existing rule) and a `time`
# ----------------------------------------------------------------------
def test_stream_has_nonzero_duration_and_time(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([STREAM_URL], 0, mode="play", elapsed=17.0,
                     current_title="Live Radio", current_url=STREAM_URL)

    r = _status(player)

    assert r["duration"] is not None and r["duration"] > 0
    assert "time" in r and r["time"] == 17.0
    # streams get the analogous item params (Perl: id+0 == 0 for a URL)
    params = r["item_loop"][0]["params"]
    assert params["track_id"] == 0
    assert params["playlist_index"] == 0


# ----------------------------------------------------------------------
# (c) Perl types: shuffle/repeat int, playlist_cur_index str
# ----------------------------------------------------------------------
def test_perl_types_for_shuffle_repeat_and_cur_index(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([11, 12, 13], 1, mode="play", elapsed=3.0, shuffle=1, repeat=2)

    r = _status(player)

    assert isinstance(r["playlist shuffle"], int) and not isinstance(r["playlist shuffle"], bool)
    assert isinstance(r["playlist repeat"], int) and not isinstance(r["playlist repeat"], bool)
    assert r["playlist shuffle"] == 1
    assert r["playlist repeat"] == 2
    assert isinstance(r["playlist_cur_index"], str)
    assert r["playlist_cur_index"] == "1"


# ----------------------------------------------------------------------
# (d) artwork at the current item
# ----------------------------------------------------------------------
def test_current_item_carries_artwork(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([11, 12, 13], 1, mode="play", elapsed=1.0)

    item = _status(player)["item_loop"][0]

    icon = item.get("icon-id") or item.get("icon")
    assert icon, f"current item needs icon-id/icon for artwork: {sorted(item.keys())}"
    assert "45" in icon, f"artwork must point at the album cover, got {icon!r}"


def test_stream_current_item_carries_artwork(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([STREAM_URL], 0, mode="play", elapsed=5.0, current_url=STREAM_URL)

    item = _status(player)["item_loop"][0]

    assert item.get("icon-id") or item.get("icon"), (
        f"stream item needs icon-id/icon: {sorted(item.keys())}"
    )


# ----------------------------------------------------------------------
# playlist entry that is a digit-only STRING: still a local track
# ----------------------------------------------------------------------
def test_numeric_string_playlist_entry_is_a_local_track(tmp_path, monkeypatch):
    """CLI/plugin paths can hand us "12" instead of 12. A bare number is
    never a stream URL — it must resolve to the DB track (real duration),
    not be rendered as a radio station named "12"."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player(["12"], 0, mode="play", elapsed=4.0)

    r = _status(player)

    assert r["duration"] == DUR[12]
    assert r["remote"] == 0
    item = r["item_loop"][0]
    assert item["trackType"] == "local"
    assert item["params"]["track_id"] == 12
    assert item["title"] == "Second Song"


# ----------------------------------------------------------------------
# (i) an EMPTY item_loop must be OMITTED (Jive Lua crash guard)
# ----------------------------------------------------------------------
def test_empty_playlist_omits_item_loop(tmp_path, monkeypatch):
    """Jive's `_whatsPlaying` indexes `item_loop[1].params` unguarded
    (share/jive/jive/slim/Player.lua:272-273). An EMPTY array makes
    `item_loop[1]` nil and Lua raises "attempt to index field '?' (a nil
    value)" — the artwork/now-playing sink dies (live: missing covers,
    `RequestHttp.lua:71 Response sink` error). Perl omits the key."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([], -1, mode="stop", elapsed=0.0)

    r = _status(player)

    assert "item_loop" not in r, "an empty item_loop crashes Jive's artwork sink"
    assert r.get("playlist_tracks") == 0
