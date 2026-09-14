"""Regression (P1): a pause is NOT a stop+restart — the position survives.

Live symptom this suite locks down: after the UI pause (`c`) the server
answered ``mode=pause, time=0.0``, SqueezePlay showed 0:00 / -2:11 and the
progress slider jumped back to the start. Cause: pause was implemented as
``strm 'q'`` (stop) + restart on resume, so the position was lost.

Perl parity (pinned clone /tmp/lms-ref = public/9.2, read-only):

* pause  = ``strm 'p'``
  ``sub pause { $client->stream('p'); $client->playPoint(undef);
  $client->SUPER::pause(); }``  — Slim/Player/Squeezebox.pm:197-204
* resume = ``strm 'u'``
  ``sub resume { $client->stream('u', ...); $client->SUPER::resume(); }``
  — Slim/Player/Squeezebox2.pm:1104-1110
* stop   = ``strm 'q'`` — Slim/Player/Squeezebox.pm:206-216 (DIFFERENT cmd)
* replay-gain field: ``'p'`` carries ``int(interval*1000)`` (ms), ``'u'``
  the interval directly — Squeezebox.pm:1080-1094.
* the paused position is kept in the state machine:
  - ``resumeTime => undef, # elapsed time when paused``
    — Slim/Player/StreamingController.pm:64
  - ``$self->{'resumeTime'} = playingSongElapsed($self);`` on pause
    — StreamingController.pm:1552-1556 (_Pause)
  - ``sub playingSongElapsed { ... if ($self->isPaused()) { return
    $self->{'resumeTime'}; } ... }`` — StreamingController.pm:1719-1724
    → the status ``time`` (``Slim/Player/Source.pm:51-53 songTime`` →
    ``playingSongElapsed``) reports the FROZEN position while paused.
  - resume jumps back to it: ``_JumpOrResume ... {newtime =>
    $self->{'resumeTime'}, restartIfNoSeek => 1}``
    — StreamingController.pm:1605-1614
  - mode stays 'pause', not 'stop' — Slim/Player/Source.pm:55-64
    (``_returnPlayMode``: ``isPaused ? 'pause' : 'play'``).
"""

import asyncio
import struct

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "02:11:22:33:44:55"
MAC_CLEAN = "021122334455"
TRACK = 51997


class _RecorderHandler:
    """Records which strm control command the manager emitted."""

    def __init__(self):
        self.paused = []
        self.unpaused = []
        self.stopped = []
        self.started = []      # send_strm_to_player (would re-fetch /stream.mp3)
        self.flushed = []

    async def send_pause_to_player(self, mac, pause_ms=0):
        self.paused.append(mac)
        return True

    async def send_unpause_to_player(self, mac):
        self.unpaused.append(mac)
        return True

    async def send_stop_to_player(self, mac):
        self.stopped.append(mac)
        return True

    async def send_strm_to_player(self, mac, track_id):
        self.started.append(track_id)
        return True

    async def send_flush_to_player(self, mac):
        self.flushed.append(mac)
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        return True


def _fresh_pm():
    pm = object.__new__(PlayerManager)
    pm.players = {}
    pm._protocol_handler = _RecorderHandler()
    return pm


def _player(pm, *, mode="play", elapsed=42.0, track_id=TRACK):
    p = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=0)
    p.mode = mode
    p.elapsed = elapsed
    p.current_track_id = track_id
    p.playlist = [track_id]
    p.playlist_position = 0
    p.strm_sent_track = track_id   # we really did stream this track
    pm.players[p.mac] = p
    return p


# ── manager: pause emits strm 'p' (never a stop) ──────────────────────

def test_pause_sends_strm_p_and_no_stop():
    """Perl pause() = stream('p'). A stop frame ('q') loses the position."""
    async def run():
        pm = _fresh_pm()
        p = _player(pm)
        ok = await pm.pause_player(p.mac, True)
        return ok, p, pm._protocol_handler

    ok, p, h = asyncio.run(run())
    assert ok
    assert h.paused == [MAC], "pause must send the strm 'p' command"
    assert h.stopped == [], "pause must NOT stop the stream (position loss)"
    assert h.flushed == []
    assert p.mode == "pause"


def test_pause_freezes_position_and_keeps_strm_guard():
    """The frozen position lives in the state (Perl resumeTime) and the
    player still holds the stream — so the guard MUST stay armed and no
    new /stream.mp3 GET / flush may be triggered."""
    async def run():
        pm = _fresh_pm()
        p = _player(pm, elapsed=73.5)
        await pm.pause_player(p.mac, True)
        return p, pm._protocol_handler

    p, h = asyncio.run(run())
    assert p.pause_time == pytest.approx(73.5), "pause must save the position"
    assert p.elapsed == pytest.approx(73.5)
    assert p.strm_sent_track == TRACK, "pause must not drop the strm guard"
    assert h.started == [], "pause must not re-stream the file"
    assert h.flushed == []


def test_resume_sends_strm_u_and_restores_position_without_restream():
    """Perl resume() = stream('u'); resumeTime is restored, the file is NOT
    re-fetched (no second /stream.mp3 GET)."""
    async def run():
        pm = _fresh_pm()
        p = _player(pm, elapsed=73.5)
        await pm.pause_player(p.mac, True)
        p.elapsed = 0.0            # a stray zero STAT while paused
        ok = await pm.pause_player(p.mac, False)
        return ok, p, pm._protocol_handler

    ok, p, h = asyncio.run(run())
    assert ok
    assert h.unpaused == [MAC], "resume must send the strm 'u' command"
    assert h.started == [], "resume must not restart/re-fetch the track"
    assert h.stopped == []
    assert p.mode == "play"
    assert p.pause_requested is False
    assert p.elapsed == pytest.approx(73.5), "resume continues at the pause point"


def test_resume_after_pause_does_not_restream_same_track():
    """End-to-end at unit level: pause → resume → a redundant play of the
    same track still does not re-send (guard armed) — i.e. no second GET."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    player.mode = "play"
    player.current_track_id = TRACK
    player.strm_sent_track = TRACK   # this track is really streaming now

    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm

    class _FakeWriter:
        def __init__(self):
            self.frames = []

        def write(self, data):
            self.frames.append(data)

        async def drain(self):
            pass

        def is_closing(self):
            return False

    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    client._player_connections = {MAC_CLEAN: 1}
    client.web_port = 9000
    client.server_ip = "127.0.0.1"
    pm.set_protocol_handler(client)

    def _frames(cmd):
        return [f for f in writer.frames if f[2:7] == b"strm" + cmd]

    async def run():
        assert await pm.pause_player(MAC, True) is True
        pause_frames = _frames(b"p")
        assert await pm.pause_player(MAC, False) is True
        resume_frames = _frames(b"u")
        stop_frames = _frames(b"q")
        # the caller sets these optimistically on every play request
        player.mode = "play"
        player.current_track_id = TRACK
        writer.frames.clear()
        assert await client.send_strm_to_player(MAC, TRACK) is True
        return pause_frames, resume_frames, stop_frames, player

    pause_frames, resume_frames, stop_frames, player = asyncio.run(run())
    assert pause_frames, "pause frame (strm 'p') missing"
    assert resume_frames, "resume frame (strm 'u') missing"
    assert stop_frames == [], "pause/resume must not stop"
    assert [f for f in writer.frames if f[2:7] == b"strms"] == [], (
        "no strm stream frame after resume — the file must not be re-fetched"
    )
    assert player.strm_sent_track == TRACK


# ── manager: stop still cleans up ─────────────────────────────────────

def test_stop_after_pause_resets_guard_and_clears_position():
    """Stop is the command that really ends the stream: guard reset + a
    following play of the same track must stream again."""
    async def run():
        pm = _fresh_pm()
        p = _player(pm, elapsed=20.0)
        await pm.pause_player(p.mac, True)
        ok = await pm.stop_player(p.mac)
        return ok, p, pm._protocol_handler

    ok, p, h = asyncio.run(run())
    assert ok
    assert h.stopped == [MAC], "stop must still send strm 'q'"
    assert p.strm_sent_track is None, "stop resets the guard (stream is gone)"
    assert p.mode == "stop"
    assert p.pause_requested is False


# ── protocol: frames carry the right strm command ─────────────────────

class _FakeWriter:
    def __init__(self):
        self.written = b""

    def is_closing(self):
        return False

    def write(self, data):
        self.written += data

    async def drain(self):
        pass


def _frame_body(written: bytes) -> bytes:
    length = struct.unpack(">H", written[:2])[0]
    assert length == 4 + 24          # 'strm' opcode + Perl 24-byte body
    assert written[2:6] == b"strm"
    return written[6:]


def test_send_pause_builds_strm_p_frame_with_ms_interval():
    client = SlimProtoClient()
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}

    assert asyncio.run(client.send_pause_to_player(MAC, 0)) is True
    body = _frame_body(writer.written)
    assert body[0:1] == b"p"
    # 'p' carries int(interval*1000) in the replay-gain field (offset 14)
    assert struct.unpack(">I", body[14:18])[0] == 0


def test_send_pause_interval_is_converted_to_milliseconds():
    client = SlimProtoClient()
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}

    assert asyncio.run(client.send_pause_to_player(MAC, 1500)) is True
    body = _frame_body(writer.written)
    assert body[0:1] == b"p"
    assert struct.unpack(">I", body[14:18])[0] == 1500


def test_send_unpause_builds_strm_u_frame():
    client = SlimProtoClient()
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}

    assert asyncio.run(client.send_unpause_to_player(MAC)) is True
    body = _frame_body(writer.written)
    assert body[0:1] == b"u"
    assert struct.unpack(">I", body[14:18])[0] == 0


# ── protocol: STAT must not zero the paused position ──────────────────

def _stat_client(monkeypatch, player):
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    try:
        import lyrion.web.cometd as cometd_mod
        monkeypatch.setattr(cometd_mod, "get_manager", lambda: None)
    except Exception:  # pragma: no cover
        pass
    client = SlimProtoClient.__new__(SlimProtoClient)
    client._player_writers = {MAC_CLEAN: _FakeWriter()}
    return client


def _stat_frame(event: str, elapsed_seconds: int = 0) -> bytes:
    """53-byte STAT frame; elapsed_seconds at offset 37 (Perl layout)."""
    buf = bytearray(53)
    buf[0:4] = event.encode("ascii")
    buf[37:41] = struct.pack(">I", elapsed_seconds)
    return bytes(buf)


def test_zero_elapsed_stat_while_paused_keeps_position(monkeypatch):
    """Perl: ``playingSongElapsed`` returns ``resumeTime`` while paused
    (StreamingController.pm:1719-1724), it does NOT fall back to the live
    playpoint — a STAT reporting 0 must not reset the displayed time."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    player.mode = "pause"
    player.elapsed = 61.0
    player.pause_time = 61.0
    player.strm_sent_track = TRACK
    client = _stat_client(monkeypatch, player)

    client._handle_stat_frame(MAC, _stat_frame("STMu", 0))

    assert player.elapsed == pytest.approx(61.0), "paused position was zeroed"
    assert player.mode == "pause"


def test_pause_ack_keeps_the_guard_and_position(monkeypatch):
    """An ``STMp``/pause ack is not a stream loss: the player merely holds
    its output. Reset the guard only on real losses (stop/track end)."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    player.mode = "play"
    player.elapsed = 30.0
    player.strm_sent_track = TRACK
    client = _stat_client(monkeypatch, player)

    client._handle_stat_frame(MAC, _stat_frame("STMp"))

    assert player.mode == "pause"
    assert player.strm_sent_track == TRACK, (
        "a pause ack must not drop the guard — resume would re-stream"
    )
    assert player.elapsed == pytest.approx(30.0)


def test_stop_event_still_resets_guard(monkeypatch):
    """A real stop (player event 'stop') ends the stream: guard reset."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    player.mode = "play"
    player.strm_sent_track = TRACK
    client = _stat_client(monkeypatch, player)

    client._handle_stat_frame(MAC, _stat_frame("stop"))

    assert player.mode == "stop"
    assert player.strm_sent_track is None
