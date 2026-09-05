"""Tests for power on/off semantics.

Regression: power-off only flipped a state flag and never stopped playback,
so a "powered off" player kept streaming.
"""

import asyncio

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState


class _FakeHandler:
    def __init__(self):
        self.stopped = []

    async def send_stop_to_player(self, mac):
        self.stopped.append(mac)
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


def test_power_off_clears_playing_state():
    pm = _fresh_pm()
    p = _player(pm)
    p.power = True
    p.mode = "play"
    pm.set_power(p.mac, False)
    assert p.power is False
    assert p.mode == "stop"


def test_power_on_sets_flag_without_stopping():
    pm = _fresh_pm()
    p = _player(pm)
    pm.set_power(p.mac, True)
    assert p.power is True


def test_power_off_sends_stop_frame():
    async def run():
        pm = _fresh_pm()
        p = _player(pm)
        p.power = True
        p.mode = "play"
        pm.set_power(p.mac, False)
        await asyncio.sleep(0.02)  # let the scheduled stop task run
        return pm._protocol_handler.stopped

    stopped = asyncio.run(run())
    assert stopped == ["02:11:22:33:44:55"]
