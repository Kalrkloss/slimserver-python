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
        self.paused = []
        self.unpaused = []

    async def send_stop_to_player(self, mac):
        self.stopped.append(mac)
        return True

    async def send_pause_to_player(self, mac, pause_ms=0):
        self.paused.append(mac)
        return True

    async def send_unpause_to_player(self, mac):
        self.unpaused.append(mac)
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        return True

    async def send_strm_to_player(self, mac, track_id, start_seconds=0.0):
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


def test_pause_sets_intent_that_stop_ack_must_not_overwrite():
    """Pause = strm 'p' (Squeezebox.pm:197-204); a stray STAT stop-ack must
    keep mode 'pause' (protocol.py consumes pause_requested)."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm)
        p.mode = "play"
        p.current_url = "http://radio.example/x"
        p.remote = 1
        ok = await pm.pause_player(p.mac, True)
        return ok, p.mode, p.pause_requested

    ok, mode, flagged = asyncio.run(run())
    assert ok
    assert mode == "pause"
    assert flagged is True, "STAT stop-ack needs the intent to keep 'pause'"


def test_resume_clears_pause_intent():
    async def run():
        pm = _fresh_pm()
        p = _player(pm)
        p.mode = "pause"
        p.pause_requested = True
        p.current_url = "http://radio.example/x"
        p.current_track_id = None
        p.remote = 1
        ok = await pm.pause_player(p.mac, False)
        return ok, p.pause_requested

    ok, flagged = asyncio.run(run())
    assert ok
    assert flagged is False


def test_play_track_clears_stale_stream_metadata():
    """A local track must not inherit the radio's StreamTitle/meta."""
    async def run():
        pm = _fresh_pm()
        p = _player(pm)
        p.remote = 1
        p.current_title = "Alte Station - irgendein Titel"
        p.remote_meta = {"title": "irgendein Titel", "artist": "Alte Station"}
        ok = await pm.play_track(p.mac, 42)
        return ok, p

    ok, p = asyncio.run(run())
    assert ok
    assert p.remote == 0
    assert p.current_title == "", "radio StreamTitle must be cleared"
    assert p.remote_meta == {}, "radio meta must be cleared"
