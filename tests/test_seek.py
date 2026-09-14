"""Tests for seek/skip and pause-resume position handling.

Regression: the skip-ahead frame was a 13-byte stub (instead of the
Perl 24-byte strm body with the skip interval in milliseconds in the
replay-gain field), and resume restarted a track from position zero.

The pause path itself is Perl ``strm 'p'`` / resume ``strm 'u'``
(Squeezebox.pm:197-204, Squeezebox2.pm:1104-1110) — the stream is not
re-fetched and the position is kept in the state, see
``tests/test_pause_resume.py``.
"""

import asyncio
import struct

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState


class _FakeWriter:
    def __init__(self):
        self.written = b""

    def is_closing(self):
        return False

    def write(self, data):
        self.written += data

    async def drain(self):
        pass


class _FakeHandler:
    def __init__(self):
        self.started = []   # track ids sent via send_strm_to_player
        self.skips = []     # seconds sent via send_skip_to_player
        self.stopped = []
        self.paused = []
        self.unpaused = []

    async def send_strm_to_player(self, mac, track_id):
        self.started.append(track_id)
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        return True

    async def send_stop_to_player(self, mac):
        self.stopped.append(mac)
        return True

    async def send_pause_to_player(self, mac, pause_ms=0):
        self.paused.append(mac)
        return True

    async def send_unpause_to_player(self, mac):
        self.unpaused.append(mac)
        return True

    async def send_skip_to_player(self, mac, seconds):
        self.skips.append(seconds)
        return True


def _fresh_pm():
    pm = object.__new__(PlayerManager)
    pm.players = {}
    pm._protocol_handler = _FakeHandler()
    return pm


def _player(pm):
    p = PlayerState(mac="02:11:22:33:44:55", name="Test", ip="127.0.0.1", port=0)
    pm.players[p.mac] = p
    return p


# ── protocol-level frame format ──────────────────────────────────────

def test_send_skip_builds_24_byte_strm_a_frame_with_ms_interval():
    client = SlimProtoClient()
    writer = _FakeWriter()
    client._player_writers = {"021122334455": writer}

    assert asyncio.run(client.send_skip_to_player("02:11:22:33:44:55", 30)) is True

    data = writer.written
    length = struct.unpack(">H", data[:2])[0]
    assert length == 4 + 24  # 'strm' opcode + 24-byte body
    assert data[2:6] == b"strm"

    body = data[6:]
    assert len(body) == 24
    assert body[0:1] == b"a"  # command = skip-ahead
    # replay-gain field carries the interval in MILLISECONDS (offset 14..18)
    assert struct.unpack(">I", body[14:18])[0] == 30000


# ── manager-level behaviour ───────────────────────────────────────────

def test_seek_to_forwards_to_skip_ahead():
    pm = _fresh_pm()
    p = _player(pm)
    p.playlist = [1]
    p.playlist_position = 0
    p.elapsed = 10.0

    assert asyncio.run(pm.seek_to(p.mac, 25)) is True
    # absolute seek to 25 with elapsed 10 → skip forward 15s
    assert pm._protocol_handler.skips == [15]


def test_resume_continues_at_pause_position_without_restart():
    """Perl resume() = ``strm 'u'`` (Squeezebox2.pm:1104-1110): the output
    continues in place and the displayed position resumes at ``resumeTime``
    (StreamingController.pm:1605-1614). The file must NOT be re-streamed
    (no second /stream.mp3 GET) and no forward seek is issued."""
    pm = _fresh_pm()
    p = _player(pm)
    p.playlist = [1]
    p.playlist_position = 0
    p.current_track_id = 1
    p.mode = "play"
    p.elapsed = 60.0
    h = pm._protocol_handler

    async def run():
        assert await pm.pause_player(p.mac, True) is True
        assert p.mode == "pause"
        assert p.pause_time == 60.0
        assert await pm.pause_player(p.mac, False) is True

    asyncio.run(run())
    assert p.mode == "play"
    assert p.elapsed == 60.0          # continues at the pause point
    assert h.paused == [p.mac]
    assert h.unpaused == [p.mac]
    assert h.started == []            # no restart → no second stream GET
    assert h.skips == []              # no seek needed
    assert h.stopped == []            # pause is not a stop
