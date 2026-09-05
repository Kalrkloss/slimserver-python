"""Tests for disconnect/reconnect state retention (Perl forget_disconnected_client).

Regression: a plain TCP close without DSCO unregistered the player
immediately, deleting its playlist/volume/position. Perl marks the player
disconnected and forgets it only after a grace period (300s) unless it
reconnects first.
"""

import asyncio

import lyrion.networking.protocol as proto
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "02:11:22:33:44:55"
KEY = MAC.replace(":", "").upper()


def _player(connected: bool) -> PlayerState:
    p = PlayerState(mac=MAC, name="Test", ip="127.0.0.1", port=0)
    p.connected = connected
    return p


def test_schedule_forget_keeps_player_then_unregisters(monkeypatch):
    monkeypatch.setattr(proto, "FORGET_DISCONNECTED_TIME", 0.05)

    async def run():
        pm = PlayerManager()
        pm.players[MAC] = _player(connected=False)
        client = SlimProtoClient()
        client._schedule_forget(MAC)

        assert KEY in client._forget_tasks
        # player still registered (just offline) during the grace period
        assert pm.players.get(MAC) is not None

        await asyncio.sleep(0.15)
        assert pm.players.get(MAC) is None  # forgotten after grace
        pm.players.clear()

    asyncio.run(run())


def test_cancel_forget_keeps_player_registered(monkeypatch):
    monkeypatch.setattr(proto, "FORGET_DISCONNECTED_TIME", 0.05)

    async def run():
        pm = PlayerManager()
        pm.players[MAC] = _player(connected=False)
        client = SlimProtoClient()
        client._schedule_forget(MAC)
        client._cancel_forget(MAC)  # player reconnected

        assert KEY not in client._forget_tasks
        await asyncio.sleep(0.15)
        assert pm.players.get(MAC) is not None
        pm.players.clear()

    asyncio.run(run())


def test_forget_skips_reconnected_player(monkeypatch):
    monkeypatch.setattr(proto, "FORGET_DISCONNECTED_TIME", 0.05)

    async def run():
        pm = PlayerManager()
        pm.players[MAC] = _player(connected=True)  # reconnected in time
        client = SlimProtoClient()
        client._schedule_forget(MAC)

        await asyncio.sleep(0.15)
        assert pm.players.get(MAC) is not None  # not forgotten
        pm.players.clear()

    asyncio.run(run())
