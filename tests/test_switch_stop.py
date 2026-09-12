"""Switching songs must STOP the player first (Perl's switch sequence).

Perl's controller stops the player on every song change:
``_StopGetNext`` → ``_Stop`` → ``_stopClient``
(StreamingController.pm:599-601, :622-627) → ``$client->stop`` =
``stream('q')`` (Squeezebox.pm:206-216) → then ``@{$client->chunks} = ()``
and ``closeStream()`` (Squeezebox.pm:143-147).

Why the stop matters: jive's ``strm 'q'`` handler runs
``_stopPauseAndStopTimers()`` + ``stopInternal()`` (Playback.lua:966-970),
which tears down the DECODED audio pipeline. A new ``strm 's'`` alone only
flushes the network buffer (``_streamDisconnect(nil, true)`` →
``Stream:flush()``, Playback.lua:896 and :777-779) — the audio that is
already decoded keeps playing. Live measurement before the fix: after
selecting another stream the player kept sounding the old one for ~4.5 s
(elapsed 11.3 s → 15.4 s) before the new stream's STMs arrived.
"""

import asyncio
import struct

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1c:87:2c:47:fc:36"
MAC_CLEAN = "1C872C47FC36"
TRACK = 31406


class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass

    def is_closing(self):
        return False


def _client(mode="play", strm_sent_track=TRACK):
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    player.mode = mode
    player.strm_sent_track = strm_sent_track
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm

    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    return client, writer, player


def test_switching_stops_the_player_before_the_new_stream():
    client, writer, _ = _client(mode="play")

    asyncio.run(client._cancel_if_switching(MAC))

    assert writer.frames, "a switch must send the stop frame"
    stop = writer.frames[0]
    assert stop[2:6] == b"strm"
    assert struct.unpack(">H", stop[:2])[0] == 4 + 24
    assert stop[6:7] == b"q", "Perl stops with stream('q')"


def test_paused_player_is_stopped_too():
    client, writer, _ = _client(mode="pause")
    asyncio.run(client._cancel_if_switching(MAC))
    assert writer.frames and writer.frames[0][6:7] == b"q"


def test_first_play_does_not_stop_anything():
    """Nothing streaming (no guard, mode stop) → Perl's _Stop has no song to
    stop, so no 'q' frame may be sent."""
    client, writer, _ = _client(mode="stop", strm_sent_track=None)
    asyncio.run(client._cancel_if_switching(MAC))
    assert writer.frames == []


def test_switch_stop_frame_has_the_perl_stream_q_layout():
    """Perl Squeezebox.pm:1096-1114 for command 'q':
    q 30 6d 3f 3f 3f 3f 00 00 00 30 00 00 00 ... (byte-verified with perl)."""
    client, writer, _ = _client(mode="play")
    asyncio.run(client._cancel_if_switching(MAC))
    body = writer.frames[0][6:]
    assert body.hex() == "71306d3f3f3f3f0000003000000000000000000000000000"
