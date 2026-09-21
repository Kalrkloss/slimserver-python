"""Auto-advance at the end of a local track (STAT STMo/STMu, not STMd).

Measured on the real client (squeezelite v1.9.9-1449, `-d all=debug`,
2026-09-21) while it played a local album track through our
``/stream.mp3``:

    [19:41:32.236467] process_strm:280 strm command q
    [19:41:32.236558] process_strm:280 strm command s
    [19:41:32.236698] sendSTAT:195 STAT: STMc
    [19:41:32.404779] sendSTAT:195 STAT: STMs        ← track started
    [19:41:33..39]    sendSTAT:195 STAT: STMt        ← ~1/s heartbeat
    [19:41:40.070829] sendSTAT:195 STAT: STMo        ← output buffer drained
    (then only STMt forever — NO STMd, NO STMu)

``STMd`` only appeared once the stream socket was closed by a server
restart, ~70 s after playback had ended::

    [19:42:50.627262] slimproto_run:579 error reading from socket: closed
    [19:42:50.633089] stream_thread:413 end of stream (169681 bytes)
    [19:42:50.663082] decode_thread:100 decode complete
    [19:42:55.727793] sendSTAT:195 STAT: STMd
    [19:42:55.727807] sendSTAT:195 STAT: STMu

So the end-of-track report of this client class is the OUTPUT UNDERRUN
(``STMo`` for an HTTP stream, ``STMu`` for a slimproto BODY stream —
squeezelite ``slimproto.c``:716-729), which is also exactly what Perl
handles as the end of the stream:

* ``STMu`` → ``playerStopped`` (``Squeezebox2.pm:161-163``),
* ``STMo`` → ``playerOutputUnderrun`` (``Squeezebox2.pm:172-173``),

and the advance itself is Perl's ``_RetryOrNext``/``_NextIfMore``
(``StreamingController.pm:912-939``, ``:1003-1014``) →
``_getNextTrack(..., $ifMoreTracks=1)`` (``:629-713``) with the index from
``nextsong()`` (``:847-899``). For clients that cannot report the end Perl
fires the very same ``playerReadyToStream`` from its own stream pump when
the source is exhausted (``Squeezebox1.pm:99-110``, ``SLIMP3.pm:104-118``,
``HTTP.pm:87-95`` — "Bug 10400 - need to tell the controller to get next
track ready").
"""
import asyncio
import struct
import time

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager, nextsong
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"

STAT_JIFFIES_OFF = 25
STAT_OUT_FULLNESS_OFF = 33
STAT_ELAPSED_SEC_OFF = 37
STAT_ELAPSED_MS_OFF = 43

BUFFERED = 3525496          # a filled output buffer (measured live)


def _stat_frame(event: str, out_fullness: int = 0, jiffies: int = 0,
                elapsed_ms: int = 0) -> bytes:
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


def _stream_frames(writer: FakeWriter):
    """The ``strm 's'`` frames (a new track being streamed)."""
    return [f for f in writer.frames if f[2:7] == b"strms"]


def _new_player(playlist=(111, 222, 333), position: int = 0) -> PlayerState:
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.playlist = list(playlist)
    player.playlist_position = position
    player.mode = "play"
    return player


def _make_client(player: PlayerState, monkeypatch=None):
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    if monkeypatch is not None:
        try:
            import lyrion.web.cometd as cometd_mod
            monkeypatch.setattr(cometd_mod, "get_manager", lambda: None)
        except Exception:  # pragma: no cover
            pass
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    client._player_connections = {MAC_CLEAN: 1}
    client.web_port = 9000
    client.server_ip = "127.0.0.1"
    return client, writer, pm


def _drive(client, *frames):
    """Feed STAT frames and let the scheduled advance task run."""
    async def _run():
        for frame in frames:
            client._handle_stat_frame(MAC, frame)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────
# nextsong — Perl StreamingController.pm:847-899
# ──────────────────────────────────────────────────────────────────────

def test_nextsong_advances_one_index():
    player = _new_player()
    assert nextsong(player) == 1                      # :881-882


def test_nextsong_repeat_off_ends_the_playlist():
    """At the end of the playlist with repeat off Perl returns undef
    (:897) — ``_getNextTrack(...,1)`` then returns without streaming
    (:662-664) and the status ends in ``stop``."""
    player = _new_player(playlist=(111, 222), position=1)
    assert nextsong(player) is None


def test_nextsong_repeat_all_wraps():
    """``repeat == 2`` (repeat-all) starts the playlist over (:883-893)."""
    player = _new_player(playlist=(111, 222), position=1)
    player.repeat = 2
    assert nextsong(player) == 0


def test_nextsong_repeat_song_replays_the_same_index():
    """``repeat == 1`` (repeat-song) → the CURRENT index (:871-873)."""
    player = _new_player(playlist=(111, 222), position=1)
    player.repeat = 1
    assert nextsong(player) == 1


def test_nextsong_empty_playlist_is_none():
    player = _new_player(playlist=(), position=0)
    assert nextsong(player) is None                   # :858


# ──────────────────────────────────────────────────────────────────────
# The STAT path: STMo/STMu with a drained output buffer IS the track end
# ──────────────────────────────────────────────────────────────────────

def test_stmo_after_start_advances_the_playlist(monkeypatch):
    """The measured live sequence: STMs (track started) → STMo with an
    EMPTY output buffer → the next playlist entry is streamed."""
    player = _new_player()
    client, writer, _pm = _make_client(player, monkeypatch)

    calls = []

    async def _jump(self, mac, index):
        calls.append(index)
        player.playlist_position = index
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    # Our own strm frame went out for track 111 (this arms
    # ``strm_sent_track`` and clears ``_track_started_at``), the player then
    # started it and drained the output buffer at the end of the file:
    assert asyncio.run(client.send_strm_to_player(MAC, 111)) is True
    writer.frames.clear()
    _drive(client,
           _stat_frame("STMs", elapsed_ms=0),
           _stat_frame("STMt", out_fullness=BUFFERED, elapsed_ms=3400),
           _stat_frame("STMo", out_fullness=0, elapsed_ms=6923))

    assert calls == [1], "the drained output buffer must advance the playlist"
    assert player.strm_sent_track is None, "the strm guard is dropped first"


def test_stmu_after_start_advances_the_playlist(monkeypatch):
    """``STMu`` (slimproto BODY streams) is the same end of track:
    Perl ``playerStopped`` (Squeezebox2.pm:161-163)."""
    player = _new_player()
    player.strm_sent_track = 111
    player.playing_track_id = 111
    client, _writer, _pm = _make_client(player, monkeypatch)

    calls = []

    async def _jump(self, mac, index):
        calls.append(index)
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    _drive(client,
           _stat_frame("STMs"),
           _stat_frame("STMu", out_fullness=0, elapsed_ms=6923))

    assert calls == [1]


def test_start_underrun_does_not_advance(monkeypatch):
    """The STMf → STMc → STMo handshake of a START emits an underrun with
    an empty output buffer BEFORE the track ever started; it must not skip
    the track (live 2026-09-12: mode=play, frozen elapsed)."""
    player = _new_player()
    player.strm_sent_track = 111
    client, _writer, _pm = _make_client(player, monkeypatch)

    calls = []

    async def _jump(self, mac, index):
        calls.append(index)
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    _drive(client,
           _stat_frame("STMf", out_fullness=BUFFERED),
           _stat_frame("STMc"),
           _stat_frame("STMo", out_fullness=0))

    assert calls == [], "the start handshake is not a track end"


def test_underrun_with_buffered_output_does_not_advance(monkeypatch):
    """An underrun while the output buffer still holds audio (a hiccup,
    Perl's ``_Rebuffer``, StreamingController.pm:1659-1671) is not a
    track end."""
    player = _new_player()
    player.strm_sent_track = 111
    client, _writer, _pm = _make_client(player, monkeypatch)

    calls = []

    async def _jump(self, mac, index):
        calls.append(index)
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    _drive(client,
           _stat_frame("STMs"),
           _stat_frame("STMo", out_fullness=BUFFERED, elapsed_ms=500))

    assert calls == []


def test_paused_player_underrun_does_not_advance(monkeypatch):
    """Perl's ``OutputUnderrun`` row is ``_NoOp`` while PAUSED
    (StreamingController.pm:236) — a paused player never advances."""
    player = _new_player()
    player.strm_sent_track = 111
    client, _writer, _pm = _make_client(player, monkeypatch)

    calls = []

    async def _jump(self, mac, index):
        calls.append(index)
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    _drive(client, _stat_frame("STMs"))
    player.mode = "pause"                      # the user paused the player
    _drive(client, _stat_frame("STMo", out_fullness=0, elapsed_ms=6923))

    assert calls == []


def test_remote_stream_underrun_does_not_advance(monkeypatch):
    """A radio stream never ends: Perl re-streams it instead
    (``_RetryOrNext`` :918-931). Our radio path must stay untouched."""
    player = _new_player()
    player.remote = 1
    player.strm_sent_track = None          # a direct stream has no track id
    client, _writer, _pm = _make_client(player, monkeypatch)

    calls = []

    async def _jump(self, mac, index):
        calls.append(index)
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    _drive(client,
           _stat_frame("STMs"),
           _stat_frame("STMo", out_fullness=0, elapsed_ms=120))

    assert calls == []


def test_track_end_is_handled_once(monkeypatch):
    """The same end arrives as STMo and, once the socket closes, as STMu
    plus a late STMd (measured). Only ONE advance may happen."""
    player = _new_player()
    player.strm_sent_track = 111
    player.playing_track_id = 111
    client, _writer, _pm = _make_client(player, monkeypatch)

    calls = []

    async def _jump(self, mac, index):
        calls.append(index)
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    _drive(client,
           _stat_frame("STMs"),
           _stat_frame("STMo", out_fullness=0, elapsed_ms=6923))
    # the late reports of the SAME stream (the connection was closed)
    _drive(client, _stat_frame("STMd", out_fullness=0))
    _drive(client, _stat_frame("STMu", out_fullness=0, elapsed_ms=6923))

    assert calls == [1], "one track end must advance exactly once"


def test_last_track_stops_and_next_track_rearms(monkeypatch):
    """End of the playlist with repeat off: Perl's ``nextsong`` returns
    undef (:897) → no stream (:662-664) → the player ends in ``stop``
    (``Stopped`` :224-230)."""
    player = _new_player(playlist=(111, 222), position=1)
    player.strm_sent_track = 222
    player.playing_track_id = 222
    client, _writer, _pm = _make_client(player, monkeypatch)

    jumps, stops = [], []

    async def _jump(self, mac, index):
        jumps.append(index)
        return True

    async def _stop(self, mac):
        stops.append(mac)
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    monkeypatch.setattr(PlayerManager, "stop_player", _stop)
    _drive(client,
           _stat_frame("STMs"),
           _stat_frame("STMo", out_fullness=0, elapsed_ms=5000))

    assert jumps == []
    assert stops == [MAC], "the playlist ends cleanly with a stop"

    # …and the next track's ``Started`` re-arms the one-shot guard, so a
    # repeat-song can end (and advance) again.
    player._track_end_done_for = 222
    client._handle_stat_frame(MAC, _stat_frame("STMs"))
    assert player._track_end_done_for is None


def test_repeat_all_wraps_at_the_end_of_the_playlist(monkeypatch):
    """``repeat == 2``: the end of the playlist starts over (:883-893)."""
    player = _new_player(playlist=(111, 222), position=1)
    player.repeat = 2
    player.strm_sent_track = 222
    player.playing_track_id = 222
    client, _writer, _pm = _make_client(player, monkeypatch)

    calls = []

    async def _jump(self, mac, index):
        calls.append(index)
        return True

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    _drive(client,
           _stat_frame("STMs"),
           _stat_frame("STMo", out_fullness=0, elapsed_ms=5000))

    assert calls == [0], "repeat-all wraps to the first entry"


def test_advance_streams_the_next_track(monkeypatch):
    """End to end over the real send path: the track end produces a strm
    frame for the NEXT playlist entry (the player starts it)."""
    player = _new_player()
    player.strm_sent_track = 111
    player.playing_track_id = 111
    client, writer, _pm = _make_client(player, monkeypatch)

    async def _jump(self, mac, index):
        # what playlist_play does: play the item at ``index``
        player.playlist_position = index
        return await client.send_strm_to_player(mac, player.playlist[index])

    monkeypatch.setattr(PlayerManager, "playlist_jump", _jump)
    writer.frames.clear()
    _drive(client,
           _stat_frame("STMs"),
           _stat_frame("STMo", out_fullness=0, elapsed_ms=6923))

    streams = _stream_frames(writer)
    assert len(streams) == 1, "exactly one new strm frame for the next track"
    assert player.playlist_position == 1
    assert player.strm_sent_track == 222
    assert time.time() - player.strm_sent_at < 5
