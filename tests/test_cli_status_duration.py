"""CLI ``status``: Perl publishes ``duration`` for the CURRENT song only.

``Slim/Control/Queries.pm``::

    :4099  if (my $dur = $song->duration()) {
    :4100      $dur += 0;
    :4101      $request->addResult('duration', $dur);
    :4102  }

``$song->duration()`` is the length of the song playing NOW
(``$self->_duration() || Slim::Music::Info::getDuration($song->currentTrack()->url)``,
``Slim/Player/Song.pm:809-816``): the ``LENGTH`` of the local entry that plays,
the parsed length of a remote stream — and 0/undef for a radio stream, which
has no length of its own.  Perl then publishes NO ``duration`` token at all.

Live Perl 9.1.1 (read-only 2026-09-14, player ``00:04:20:2b:88:c8``, stream
"Dj Fada 2 - Life Breath (oct12)", ``status - 1 tags:cdltoK``): the answer
carries no ``duration`` key, while ``remoteMeta.duration`` and
``playlist_loop[0].duration`` report the string ``"0"``.

This port read ``PlayerState.duration`` — a player-level cache that a stream
start does NOT reset — so a stream entry inherited the length of the LOCAL
number played before it.  Everything here runs in-process against the real
``cmd_status``; no server start/stop, no DB write, no live command.
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.control import cli_commands
from lyrion.control.cli import CLIContext
from lyrion.control.queries import escape
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1c:87:2c:47:fc:36"
MAC_CLEAN = "1C872C47FC36"
TRACK_ID = 7
TRACK_DURATION = 213
STREAM_URL = "http://hirschmilch.de:7000/chillout.mp3"


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


@pytest.fixture()
def local_db(monkeypatch):
    """Only the ``tracks`` row of ``TRACK_ID`` — everything else answers empty."""

    async def fake_query_db(sql: str, params: tuple = ()) -> list[dict]:
        if "FROM tracks WHERE id" in sql and params and int(params[0]) == TRACK_ID:
            return [{"id": TRACK_ID, "title": "Sonnentanz",
                     "url": "file:///m/1.mp3", "duration": TRACK_DURATION,
                     "genre": "Electronic", "year": 2013, "tracknum": 1,
                     "modtime": 1711053369, "disc": 1, "remote": 0}]
        return []

    monkeypatch.setattr(cli_commands, "_query_db", fake_query_db)


def _install(player: PlayerState) -> PlayerState:
    PlayerManager().players[MAC_CLEAN] = player
    return player


def _status(player: PlayerState, args: list[str] | None = None) -> str:
    _install(player)
    ctx = CLIContext(client_id="test", player_id=MAC.upper())
    return asyncio.run(cli_commands.cmd_status(None, ctx, args or []))[0]


def _stream_player(stale_duration: float = 0.0) -> PlayerState:
    """A playing radio stream — ``player.duration`` may still hold the local
    length of the number played before it (a stream start does not reset it)."""
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856, connected=True, power=True)
    player.mode = "play"
    player.playlist = [STREAM_URL]
    player.playlist_position = 0
    player.playlist_total = 1
    player.remote = 1
    player.current_url = STREAM_URL
    player.current_title = "Hirschmilch Chillout"
    player.duration = stale_duration
    return player


# ── the stream entry: NO duration token (Perl Queries.pm:4099) ────────────

def test_stream_entry_publishes_no_duration(local_db):
    """A radio stream has no length → the token is absent (live Perl)."""
    line = _status(_stream_player())
    assert escape("duration:") not in line, line
    assert escape("time:") in line and escape("rate:1") in line, line


def test_stream_entry_does_not_inherit_the_previous_local_length(local_db):
    """The regression: ``duration`` of the local number before the stream.

    ``player.duration`` is the only place this port tracks a length and it is
    NOT reset when a stream takes over (``manager.play_url`` /
    ``_play_playlist_item`` set ``remote``/``current_track_id`` but leave
    ``duration``); the old code published that stale value as the stream's.
    """
    line = _status(_stream_player(stale_duration=float(TRACK_DURATION)))
    assert escape("duration:") not in line, line


def test_stream_entry_does_not_read_the_db_row_of_a_previous_track(local_db):
    """A leftover ``current_track_id`` must not resurrect a length either."""
    player = _stream_player()
    player.current_track_id = TRACK_ID          # e.g. not cleared on a tap
    line = _status(player)
    assert escape("duration:") not in line, line


def test_stopped_player_publishes_no_duration(local_db):
    """No playing entry (empty queue) → Perl has no ``playingSong()``."""
    player = _stream_player(stale_duration=float(TRACK_DURATION))
    player.mode = "stop"
    player.playlist = []
    player.playlist_position = 0
    player.playlist_total = 0
    line = _status(player)
    assert escape("duration:") not in line, line


# ── the local entry: its own DB length (Perl's LENGTH tag) ────────────────

def test_local_entry_publishes_its_db_length(local_db):
    """``$song->duration()`` of the local entry that plays."""
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856, connected=True, power=True)
    player.mode = "play"
    player.playlist = [TRACK_ID]
    player.playlist_position = 0
    player.playlist_total = 1
    player.current_track_id = TRACK_ID
    player.duration = 0.0
    line = _status(player)
    assert escape(f"duration:{TRACK_DURATION}") in line, line


def test_local_entry_falls_back_to_the_cached_length_without_a_db_row(monkeypatch):
    """Perl's ``$self->_duration() || Info::getDuration(url)`` chain."""
    async def empty_db(sql: str, params: tuple = ()) -> list[dict]:
        return []

    monkeypatch.setattr(cli_commands, "_query_db", empty_db)
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856, connected=True, power=True)
    player.mode = "play"
    player.playlist = [TRACK_ID]
    player.playlist_position = 0
    player.playlist_total = 1
    player.current_track_id = TRACK_ID
    player.duration = 99.0
    line = _status(player)
    assert escape("duration:99") in line, line
