"""Regression (LIVE-02): the strm idempotency guard must not swallow a
fresh play.

The caller (_play_playlist_item) sets ``mode='play'`` and
``current_track_id`` optimistically BEFORE calling ``send_strm_to_player``.
The original guard compared those two values, so every fresh play was
treated as a repeat: no strm frame went out at all and the player stayed
silent while status reported mode=play. The guard must compare against the
track we actually streamed (``strm_sent_track``), and a flush/stop must
reset it.
"""
import asyncio

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"


class FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _make_client(player: PlayerState):
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    client.web_port = 9000
    client.server_ip = "127.0.0.1"
    return client, writer, pm


def test_first_play_sends_strm_despite_optimistic_state():
    """mode='play' + current_track_id set by the caller must NOT count as
    'already playing' — the first play has to emit a strm frame."""
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.mode = "play"            # set optimistically by the caller
    player.current_track_id = 51997  # ditto
    player.strm_sent_track = None    # nothing streamed yet
    client, writer, _pm = _make_client(player)

    ok = asyncio.run(client.send_strm_to_player(MAC, 51997))
    assert ok is True
    assert writer.frames, "no strm frame was sent for a fresh play"
    assert player.strm_sent_track == 51997


def test_repeat_play_of_same_track_is_skipped():
    """A redundant cmd:load for the track already streaming must not
    re-send (the flush/restart loop that tore the buffers down)."""
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
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
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.mode = "play"
    player.current_track_id = 51997
    player.strm_sent_track = 51997
    client, writer, _pm = _make_client(player)

    assert asyncio.run(client.send_flush_to_player(MAC)) is True
    assert player.strm_sent_track is None, "flush must clear the guard"
    writer.frames.clear()

    assert asyncio.run(client.send_strm_to_player(MAC, 51997)) is True
    assert writer.frames, "after a flush the same track must be re-streamed"


def test_stop_resets_the_guard():
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.mode = "play"
    player.strm_sent_track = 51997
    _client, _writer, pm = _make_client(player)

    class _Handler:
        async def send_stop_to_player(self, mac):
            return True

    pm.set_protocol_handler(_Handler())
    assert asyncio.run(pm.stop_player(MAC)) is True
    assert player.strm_sent_track is None
