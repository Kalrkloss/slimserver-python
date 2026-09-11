"""Regression (LIVE-02 / R0.5-P1): the strm idempotency guard must never
swallow a play the player cannot answer with audio.

The caller (_play_playlist_item / play_track) sets ``mode='play'`` and
``current_track_id`` optimistically BEFORE calling ``send_strm_to_player``,
so the guard must key off the track we ACTUALLY streamed
(``strm_sent_track``) — and it must be cleared on every path where the
player no longer holds that stream:

* player flush/close ``STMf`` (Perl: flush() stream('f') + readyToStream(1),
  Squeezebox2.pm:387-396; stop() stream('q') + streamingsocket(undef) +
  readyToStream(1), Squeezebox.pm:206-216),
* decode error ``STMn`` (Perl: readyToStream(1) + playerStreamingFailed,
  Squeezebox2.pm:153-157),
* natural track end (STMt/STMo after the decoder ran dry and the output
  buffer drained),
* a stop that could not be delivered (writer gone),
* disconnect/reconnect (a fresh SlimProto connection has no stream).

A stale guard means: the play request reports success, ``mode`` stays
"play", and the player is silent — and a replay of the same track cannot
repair it. Each test below was red against the code that shipped in
``86a1c52a7``.
"""
import asyncio
import struct
import time

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"

# STAT offsets, see SlimProtoClient._STAT_FIELDS.
STAT_JIFFIES_OFF = 25
STAT_OUT_FULLNESS_OFF = 33


def _stat_frame(event: str, out_fullness: int = 0, jiffies: int = 0) -> bytes:
    """A 53-byte STAT frame (Perl unpack layout) for ``event``."""
    buf = bytearray(53)
    buf[0:4] = event.encode("ascii")
    buf[STAT_JIFFIES_OFF:STAT_JIFFIES_OFF + 4] = struct.pack(">I", jiffies)
    buf[
        STAT_OUT_FULLNESS_OFF:STAT_OUT_FULLNESS_OFF + 4
    ] = struct.pack(">I", out_fullness)
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
    """Only the real ``strm 's'`` stream frames (not flush/stop frames)."""
    return [f for f in writer.frames if f[2:7] == b"strms"]


def _make_client(player: PlayerState, monkeypatch=None):
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    if monkeypatch is not None:
        # No Cometd manager → the notify branch is skipped instead of
        # building an un-awaited coroutine.
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


def _new_player() -> PlayerState:
    return PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)


def _replay_sends(client, writer, player, track_id: int = 51997) -> bool:
    """Caller-side optimistic state + replay of the same track.

    Returns True when a real strm frame actually went out.
    """
    player.mode = "play"              # set optimistically by the caller
    player.current_track_id = track_id
    writer.frames.clear()
    ok = asyncio.run(client.send_strm_to_player(MAC, track_id))
    assert ok is True, "send_strm_to_player must report success"
    return bool(_stream_frames(writer))


def test_first_play_sends_strm_despite_optimistic_state():
    """mode='play' + current_track_id set by the caller must NOT count as
    'already playing' — the first play has to emit a strm frame."""
    player = _new_player()
    player.mode = "play"            # set optimistically by the caller
    player.current_track_id = 51997  # ditto
    player.strm_sent_track = None    # nothing streamed yet
    client, writer, _pm = _make_client(player)

    ok = asyncio.run(client.send_strm_to_player(MAC, 51997))
    assert ok is True
    assert _stream_frames(writer), "no strm frame was sent for a fresh play"
    assert player.strm_sent_track == 51997


def test_repeat_play_of_same_track_is_skipped():
    """A redundant cmd:load for the track already streaming must not
    re-send (the flush/restart loop that tore the buffers down)."""
    player = _new_player()
    player.mode = "play"
    player.current_track_id = 51997
    player.strm_sent_track = 51997   # we really did stream this one
    client, writer, _pm = _make_client(player)

    ok = asyncio.run(client.send_strm_to_player(MAC, 51997))
    assert ok is True
    assert writer.frames == [], "repeat play must not re-send the stream"


def test_flush_resets_the_guard_so_a_replay_streams_again():
    """A flush drops the player's buffers — a following play of the same
    track MUST send a fresh frame."""
    player = _new_player()
    player.mode = "play"
    player.current_track_id = 51997
    player.strm_sent_track = 51997
    client, writer, _pm = _make_client(player)

    assert asyncio.run(client.send_flush_to_player(MAC)) is True
    assert player.strm_sent_track is None, "flush must clear the guard"
    writer.frames.clear()

    assert asyncio.run(client.send_strm_to_player(MAC, 51997)) is True
    assert _stream_frames(writer), "after a flush the same track must re-stream"


def test_stop_resets_the_guard():
    player = _new_player()
    player.mode = "play"
    player.strm_sent_track = 51997
    _client, _writer, pm = _make_client(player)

    class _Handler:
        async def send_stop_to_player(self, mac):
            return True

    pm.set_protocol_handler(_Handler())
    assert asyncio.run(pm.stop_player(MAC)) is True
    assert player.strm_sent_track is None


# ──────────────────────────────────────────────────────────────────────
# R0.5-P1: guard lifecycle — every path that loses the stream must reset
# ──────────────────────────────────────────────────────────────────────

def test_stmf_from_player_resets_guard_so_replay_streams(monkeypatch):
    """(g1) Player-initiated flush/stop (`STMf`): the player flushed its
    buffers, Perl closes the streaming socket and sets readyToStream(1)
    (Squeezebox2.pm:387-396, Squeezebox.pm:206-216) — the next play of the
    SAME track must stream again."""
    player = _new_player()
    player.mode = "play"
    player.strm_sent_track = 51997
    client, writer, _pm = _make_client(player, monkeypatch)

    client._handle_stat_frame(MAC, _stat_frame("STMf"))
    assert player.strm_sent_track is None, (
        "STMf: the player flushed — the guard must not stay armed"
    )
    assert _replay_sends(client, writer, player)


def test_stmn_decode_error_resets_guard_so_replay_streams(monkeypatch):
    """(g2) Decode error (`STMn`): Perl logs the error, sets
    readyToStream(1) and calls playerStreamingFailed
    (Squeezebox2.pm:153-157) — the track is gone."""
    player = _new_player()
    player.mode = "play"
    player.strm_sent_track = 51997
    client, writer, _pm = _make_client(player, monkeypatch)

    client._handle_stat_frame(MAC, _stat_frame("STMn"))
    assert player.strm_sent_track is None, (
        "STMn: the decoder failed — the guard must not stay armed"
    )
    assert _replay_sends(client, writer, player)


def test_natural_track_end_st_mt_resets_guard_so_replay_streams(monkeypatch):
    """(g3) Undetected natural end: an `STMt` tick with the decoder dry
    (STMd seen) and the output buffer fully drained IS the track end. The
    old code had two `elif event == "STMt"` branches — the second one (the
    end-of-track one) was unreachable, so `mode` stayed "play" and the
    guard stayed armed."""
    player = _new_player()
    player.mode = "play"
    player.strm_sent_track = 51997
    player.playlist = []                 # nothing to advance to
    player._last_stmd = time.time()      # the decoder ran dry
    client, writer, _pm = _make_client(player, monkeypatch)

    async def _drive():
        client._handle_stat_frame(
            MAC, _stat_frame("STMt", out_fullness=0),
        )
        await asyncio.sleep(0)  # let the scheduled advance task run
    asyncio.run(_drive())

    assert player.strm_sent_track is None, (
        "natural track end: the player drained this track — guard must reset"
    )
    assert _replay_sends(client, writer, player)


def test_st_mt_tick_while_output_is_buffered_keeps_the_guard(monkeypatch):
    """A periodic `STMt` heartbeat during playback (output buffer NOT
    drained) is not a track end: no advance, guard stays — otherwise we
    would re-stream a track that is happily playing."""
    player = _new_player()
    player.mode = "play"
    player.strm_sent_track = 51997
    player.playlist = [51997, 51998]
    player._last_stmd = time.time()
    client, _writer, _pm = _make_client(player, monkeypatch)

    client._handle_stat_frame(
        MAC, _stat_frame("STMt", out_fullness=3525496),
    )
    assert player.strm_sent_track == 51997
    assert player.playlist_position == 0


def test_failed_stop_still_resets_guard_so_replay_streams(monkeypatch):
    """(g4) `stop_player` used to reset the guard only on success. When the
    writer is gone the stop cannot be delivered — the local guard is not
    authoritative, and after a reconnect the replay must stream."""
    player = _new_player()
    player.mode = "play"
    player.strm_sent_track = 51997
    client, writer, pm = _make_client(player, monkeypatch)

    class _Handler:
        async def send_stop_to_player(self, mac):
            return False

    pm.set_protocol_handler(_Handler())
    assert asyncio.run(pm.stop_player(MAC)) is False
    assert player.strm_sent_track is None, (
        "a failed stop must not leave the guard armed"
    )
    assert _replay_sends(client, writer, player)


def test_reconnect_resets_guard_so_replay_streams(monkeypatch):
    """(f) Reconnect: the TCP connection drops (writer popped) and the
    player connects again. A fresh SlimProto connection has no stream in
    progress, so the guard must be cleared when the writer is registered."""
    player = _new_player()
    player.mode = "play"
    player.current_track_id = 51997
    player.strm_sent_track = 51997
    client, _writer, _pm = _make_client(player, monkeypatch)

    client._player_writers.pop(MAC_CLEAN, None)   # disconnect
    new_writer = FakeWriter()
    client._register_player_writer(MAC_CLEAN, new_writer)  # HELO again

    assert player.strm_sent_track is None, "reconnect must clear the guard"
    assert _replay_sends(client, new_writer, player)


# ──────────────────────────────────────────────────────────────────────
# R0.5-P3: check-and-set must not be separated by an await
# ──────────────────────────────────────────────────────────────────────

def test_concurrent_sends_for_same_track_stream_once(monkeypatch):
    """Two parallel `playlistcontrol cmd:load` requests for the same track
    (the LIVE-02 pattern, observed live as two `Sent strm ... track=51997`
    in the same second) must produce exactly ONE flush+strm pair: the
    in-flight claim is taken synchronously, before the first await."""
    player = _new_player()
    player.mode = "play"
    player.current_track_id = 51997
    client, _writer, _pm = _make_client(player, monkeypatch)

    class _BlockingWriter(FakeWriter):
        """drain() suspends until released — keeps the first send in flight."""

        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()

        async def drain(self) -> None:
            await self.release.wait()

    writer = _BlockingWriter()
    client._player_writers[MAC_CLEAN] = writer

    async def _race():
        first = asyncio.create_task(client.send_strm_to_player(MAC, 51997))
        # Yield (no real waiting) until the first call owns the track — the
        # claim happens before its first await.
        for _ in range(50):
            await asyncio.sleep(0)
            if player.stream_in_flight == 51997:
                break
        assert player.stream_in_flight == 51997, (
            "the in-flight claim must be taken before the first await"
        )
        assert _stream_frames(writer) == []
        # Second, concurrent request for the SAME track:
        ok2 = await client.send_strm_to_player(MAC, 51997)
        assert ok2 is True
        assert _stream_frames(writer) == [], "duplicate must not stream"
        writer.release.set()
        assert await first is True
        return player

    player = asyncio.run(_race())
    assert len(_stream_frames(writer)) == 1, (
        "exactly one strm frame for two concurrent same-track calls"
    )
    assert player.stream_in_flight is None, "in-flight flag must be released"
    assert player.strm_sent_track == 51997


def test_in_flight_claim_released_after_failure(monkeypatch):
    """The claim must be released on every exit path, else that track can
    never be streamed again (the silence this guard exists to prevent)."""
    player = _new_player()
    player.mode = "play"
    client, writer, _pm = _make_client(player, monkeypatch)

    class _BoomWriter(FakeWriter):
        def write(self, data: bytes) -> None:
            raise OSError("boom")

    client._player_writers[MAC_CLEAN] = _BoomWriter()
    assert asyncio.run(client.send_strm_to_player(MAC, 51997)) is False
    assert player.stream_in_flight is None, "claim leaked on failure"

    class _HardBoomWriter(FakeWriter):
        def write(self, data: bytes) -> None:
            raise ValueError("boom")   # NOT caught by the send itself

    client._player_writers[MAC_CLEAN] = _HardBoomWriter()
    with pytest.raises(ValueError):
        asyncio.run(client.send_strm_to_player(MAC, 51997))
    assert player.stream_in_flight is None, "claim leaked on a hard failure"

    client._player_writers[MAC_CLEAN] = writer
    assert asyncio.run(client.send_strm_to_player(MAC, 51997)) is True
    assert _stream_frames(writer)
