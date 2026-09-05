"""Tests for the remote-stream flag lifecycle (radio vs local track).

Regression: playing a radio stream set ``player.remote = 1`` permanently;
starting a local track afterwards never cleared it, so ``_advance_after_track``
refused to advance (it treats ``remote`` as "live stream, never ends").
"""

import asyncio

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState


class _FakeHandler:
    async def send_strm_to_player(self, mac, track_id):
        return True

    async def send_remote_stream(self, mac, url, codec):
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


def test_play_url_sets_remote_flag():
    pm = _fresh_pm()
    p = _player(pm)
    assert asyncio.run(pm.play_url(p.mac, "http://example.com/stream", "Radio")) is True
    assert p.remote == 1


def test_play_track_clears_remote_flag():
    pm = _fresh_pm()
    p = _player(pm)
    p.remote = 1  # simulate a previously played radio stream
    assert asyncio.run(pm.play_track(p.mac, 1)) is True
    assert p.remote == 0
