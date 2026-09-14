"""Playing status ``mode`` follows Perl's controller state, not the player's STATs.

Perl hat kein eigenes ``mode``-Feld: die Status-Query liefert
``Slim::Player::Source::playmode($client)`` (``Slim/Control/Queries.pm:4081``),
und das ist ``_returnPlayMode`` (``Slim/Player/Source.pm:55-64``)::

    return 'stop' if !$_[1]->power();
    my $returnedmode = $controller->isStopped ? 'stop'
                        : $controller->isPaused ? 'pause' : 'play';

``isStopped`` verlangt BOTH ``playingState == STOPPED`` UND
``streamingState == IDLE`` (``StreamingController.pm:1681-1683``). Sobald der
Server das ``strm``-Frame geschickt hat, ist der Controller in
BUFFERING+STREAMING (``_Stream`` :1350-1352) — der Status ist damit ``play``,
*auch während der Player noch puffert* und ohne dass ein ``STMs`` eintreffen
muss.

Der Port war hier zweimal falsch (LIVE 2026-09-14 18:59:41, SqueezePlay
``1C:87:2C:47:FC:36``):

* ``send_remote_stream`` (direkter Radio-Stream) hat den Controller-Tail
  ausgelassen — ``isStopped`` blieb wahr, ``mode:stop`` für die ganze
  Startphase (11 s) bis zum ``STMs``.
* Der eigene ``STMf``-Start-Ack des Players wurde als "Stream verloren"
  gelesen und setzte ``mode=stop``, weil die Handshake-Marke am
  Lied-Id hing — ein Radio-Stream hat keine.

Perl: ``statHandler`` hat gar keinen ``STMf``-Zweig; er läuft in
``playerStatusHeartbeat`` (``Squeezebox2.pm:174-176``), und die Marke
``streamStartTimestamp`` wird von JEDEM ``strm``-Frame gelöscht
(``Squeezebox.pm:567``) und vom ``STMc`` des Players neu gesetzt
(``Squeezebox2.pm:141-142``).
"""

import asyncio
import struct
import time

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player import streaming
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.player.streaming import PlayingState, StreamingState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"
RADIO_URL = "http://example.com/radio.mp3"


@pytest.fixture(autouse=True)
def quiet_cometd(monkeypatch):
    """No cometd manager — the status push is not what these tests check."""
    monkeypatch.setattr("lyrion.web.cometd.get_manager", lambda: None)

STAT_JIFFIES_OFF = 25
STAT_OUT_FULLNESS_OFF = 33
STAT_ELAPSED_SEC_OFF = 37
STAT_ELAPSED_MS_OFF = 43


def _stat_frame(event: str, out_fullness: int = 0, jiffies: int = 0,
                elapsed_ms: int = 0) -> bytes:
    """A 53-byte STAT frame (Perl unpack layout) for ``event``."""
    buf = bytearray(53)
    buf[0:4] = event.encode("ascii")
    buf[STAT_JIFFIES_OFF:STAT_JIFFIES_OFF + 4] = struct.pack(">I", jiffies)
    buf[STAT_OUT_FULLNESS_OFF:STAT_OUT_FULLNESS_OFF + 4] = struct.pack(
        ">I", out_fullness)
    if elapsed_ms:
        buf[STAT_ELAPSED_SEC_OFF:STAT_ELAPSED_SEC_OFF + 4] = struct.pack(
            ">I", elapsed_ms // 1000)
        buf[STAT_ELAPSED_MS_OFF:STAT_ELAPSED_MS_OFF + 4] = struct.pack(
            ">I", elapsed_ms)
    return bytes(buf)


class FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _new_player() -> PlayerState:
    return PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)


def _make_client(player: PlayerState):
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    client._player_connections = {MAC_CLEAN: 1}
    client._resp_waiters = {}
    client.web_port = 9000
    client.server_ip = "127.0.0.1"
    return client, writer, pm


# ---------------------------------------------------------------------------
# The derivation itself (Perl Source.pm:55-64)
# ---------------------------------------------------------------------------

def test_reported_playmode_follows_the_controller():
    """``stop`` only while STOPPED+IDLE, else ``pause``/``play``.

    ``StreamingController.pm:1676-1695`` (isPlaying/isStopped/isPaused) plus
    ``Source.pm:55-64`` — the three values the status may report.
    """
    streaming.reset()
    assert streaming.reported_playmode(MAC) is None  # no controller yet

    streaming.note_strm_sent(MAC, None)
    ctl = streaming.controller_for(MAC)
    assert (ctl.playing_state, ctl.streaming_state) == (
        PlayingState.BUFFERING, StreamingState.STREAMING)   # :1350-1352
    assert streaming.reported_playmode(MAC) == "play"

    # A Pause while BUFFERING is `_NoOp` in Perl's table (:133-139 row
    # BUFFERING) — the played state must be reached first (STMs → `Started`).
    ctl.resolve("Started")
    assert ctl.playing_state == PlayingState.PLAYING
    ctl.resolve("Pause")                       # STMp/Pause → PAUSED (:1552-1586)
    assert streaming.reported_playmode(MAC) == "pause"

    ctl.resolve("Stop")                        # stop → STOPPED+IDLE (:585-620)
    assert streaming.reported_playmode(MAC) == "stop"
    streaming.reset()


# ---------------------------------------------------------------------------
# A) Direct radio stream
# ---------------------------------------------------------------------------

def test_direct_radio_stream_reports_play_immediately():
    """The status is ``play`` with the strm frame, not only after STMs.

    Perl ``_Stream`` (:1350-1352) + ``Source.pm:55-64`` → ``Queries.pm:4081``.
    """
    streaming.reset()
    player = _new_player()
    client, _writer, _pm = _make_client(player)

    assert asyncio.run(client.send_remote_stream(MAC, RADIO_URL)) is True

    ctl = streaming.controller_for(MAC)
    assert ctl is not None, "the strm frame must feed the controller"
    assert ctl.playing_state == PlayingState.BUFFERING
    assert ctl.streaming_state == StreamingState.STREAMING
    assert player.mode == "play", "status mode must not wait for STMs"
    assert player.strm_sent_at > 0, "Squeezebox.pm:567 — handshake per frame"
    streaming.reset()


def test_direct_radio_start_handshake_stmf_keeps_play():
    """The player's own STMf start ack is not a stop.

    ``Squeezebox2.pm:398-403`` ("always use a new stream") → the player closes
    the old stream and acks with STMf; ``statHandler`` has no STMf branch
    (:174-176 — plain ``playerStatusHeartbeat``, and only STMu reaches
    ``playerStopped``, :159-161).
    """
    streaming.reset()
    player = _new_player()
    client, _writer, _pm = _make_client(player)
    assert asyncio.run(client.send_remote_stream(MAC, RADIO_URL)) is True

    client._handle_stat_frame(MAC, _stat_frame("STMf"))

    ctl = streaming.controller_for(MAC)
    assert player.mode == "play"
    assert "Stopped" not in ctl.events_seen
    streaming.reset()


def test_stmf_after_the_handshake_window_drops_the_guard():
    """A late flush/close drops the strm guard without inventing a stop.

    Perl: ``statHandler`` has no STMf branch (``Squeezebox2.pm:174-176``) and
    ``playerStatusHeartbeat`` is ``_NoOp`` in BUFFERING/STREAMING
    (``StreamingController.pm:238-244`` + row BUFFERING :255) — an STMf never
    changes the play state, so the status keeps reporting the controller's
    value (``Source.pm:55-64``). The idempotency guard drop is the port's own
    bookkeeping (a replay of the same track must stream again, R0.5-P1).
    """
    streaming.reset()
    player = _new_player()
    client, _writer, _pm = _make_client(player)
    assert asyncio.run(client.send_remote_stream(MAC, RADIO_URL)) is True
    player.strm_sent_at = time.time() - 10          # long past the start ack

    client._handle_stat_frame(MAC, _stat_frame("STMf"))

    assert player.strm_sent_track is None           # guard dropped
    assert player.playing_track_id is None
    assert player.mode == "play", "the controller still streams"
    streaming.reset()


def test_late_stmf_after_a_server_stop_keeps_stop():
    """A player ack of our stop must not resurrect playback.

    The server stop path leaves the controller STOPPED+IDLE
    (``StreamingController.pm:585-620``), and ``Source.pm:55-64`` reports
    ``stop`` from that state.
    """
    streaming.reset()
    player = _new_player()
    client, _writer, _pm = _make_client(player)
    assert asyncio.run(client.send_remote_stream(MAC, RADIO_URL)) is True
    streaming.apply_event(MAC, "Stop")              # the server's stop
    player.mode = "stop"
    player.strm_sent_at = time.time() - 10

    client._handle_stat_frame(MAC, _stat_frame("STMf"))

    assert player.mode == "stop"
    streaming.reset()


# ---------------------------------------------------------------------------
# Local track (the /stream.mp3 path)
# ---------------------------------------------------------------------------

def test_local_track_reports_play_and_stms_keeps_it():
    """Local file: ``play`` from the frame on, ``STMs`` keeps it."""
    streaming.reset()
    player = _new_player()
    client, _writer, _pm = _make_client(player)

    assert asyncio.run(client.send_strm_to_player(MAC, 222)) is True
    assert player.mode == "play"
    assert player.strm_sent_track == 222        # idempotency guard
    assert player.strm_sent_at > 0

    # STMs → `Started` → PLAYING (StreamingController.pm:2250-2266).
    client._handle_stat_frame(MAC, _stat_frame("STMs", out_fullness=1000))
    ctl = streaming.controller_for(MAC)
    assert player.mode == "play"
    assert ctl.playing_state == PlayingState.PLAYING
    streaming.reset()


# ---------------------------------------------------------------------------
# Pause / resume must not flip anything else
# ---------------------------------------------------------------------------

def test_stmp_and_stmr_ack_the_pause_and_the_resume():
    """STMp → pause, STMr → play (Perl `Pause`/`Resume` names, :133-146)."""
    streaming.reset()
    player = _new_player()
    client, _writer, _pm = _make_client(player)
    assert asyncio.run(client.send_remote_stream(MAC, RADIO_URL)) is True

    client._handle_stat_frame(MAC, _stat_frame("STMp"))
    assert player.mode == "pause"
    assert player.pause_requested is False

    client._handle_stat_frame(MAC, _stat_frame("STMr"))
    assert player.mode == "play"
    streaming.reset()


def test_pause_state_survives_the_stream_tail():
    """A paused player stays ``pause`` when a strm frame goes out.

    Perl ``_Stream`` only sets BUFFERING **from STOPPED** (:1351) and keeps
    PAUSED — ``_returnPlayMode`` then reports ``pause`` (Source.pm:61-62).
    """
    streaming.reset()
    player = _new_player()
    client, _writer, _pm = _make_client(player)
    assert asyncio.run(client.send_remote_stream(MAC, RADIO_URL)) is True
    ctl = streaming.controller_for(MAC)
    ctl.resolve("Started")                      # the player's STMs
    ctl.resolve("Pause")                        # the manager's pause path
    player.mode = "pause"

    player.forget_stream()
    assert asyncio.run(client.send_remote_stream(MAC, RADIO_URL)) is True

    assert ctl.playing_state == PlayingState.PAUSED
    assert player.mode == "pause", "a paused player must not flip to play"
    streaming.reset()
