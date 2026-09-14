"""The STAT heartbeat must not push a player status (Perl parity).

Perl dispatches every STAT code that has no branch of its own (STMf, STMp,
STMr, STMt, STMz …) as ``$client->controller->playerStatusHeartbeat($client)``
(``Slim/Player/Squeezebox2.pm:174-176``); the streaming controller's state
table maps ``StatusHeartbeat`` to ``_NoOp`` (STOPPED), ``_CheckSync``
(PLAYING — a no-op unless more than one player shares a sync group,
``StreamingController.pm:485-490``) or ``_CheckPaused`` (:424-452).  None of
them notifies, so the ``status`` auto-execute subscription is not re-run
(``Queries.pm:4588-4597`` + ``Request.pm:2065-2102``).

Measured before the fix against a stopped-idle player: 15 status pushes in
12 s (one per ~1/s STAT tick) where Perl sends one per change.
"""
import asyncio
import struct

import pytest

import lyrion.player.manager as manager_mod
import lyrion.web.cometd as cometd_mod
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager, status_signature
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"


def _tick(event: str = "STMt", elapsed_ms: int = 0, jiffies: int = 0,
          output_fullness: int = 0, signal: int = 0) -> bytes:
    """A 51-byte STAT frame (no error_code, like SqueezePlay's)."""
    frame = bytearray(51)
    frame[0:4] = event.encode("ascii")
    struct.pack_into(">I", frame, 25, jiffies)
    struct.pack_into(">I", frame, 33, output_fullness)
    struct.pack_into(">H", frame, 23, signal)
    struct.pack_into(">I", frame, 43, elapsed_ms)
    return bytes(frame)


class _PushCounter:
    """Records the STAT-driven pushes the protocol handler schedules."""

    def __init__(self) -> None:
        self.pushes: list[str] = []

    async def notify_player_status(self, mac: str) -> None:
        self.pushes.append(mac)


class _FakeManager:
    """A PlayerManager stand-in that runs the REAL status gate."""

    def __init__(self, player: PlayerState) -> None:
        self._player = player
        self._status_signatures: dict[str, tuple] = {}

    def get_player(self, mac: str):
        return self._player

    def status_changed(self, player: PlayerState) -> bool:
        return PlayerManager.status_changed(self, player)


def _wire(monkeypatch, player: PlayerState):
    pm = _FakeManager(player)
    pushes = _PushCounter()
    monkeypatch.setattr(manager_mod, "PlayerManager", lambda: pm)
    monkeypatch.setattr(cometd_mod, "get_manager", lambda: pushes)
    client = object.__new__(SlimProtoClient)
    return client, pushes


@pytest.mark.asyncio
async def test_repeated_heartbeat_ticks_push_only_once(monkeypatch):
    """Five ~1/s STAT heartbeats with an unchanged status -> ONE push.

    The first tick announces the status (Perl sends the seed when the
    subscription is registered — here the client saw none yet), every later
    tick changes only the clock, which Perl's heartbeat never turns into a
    notification.
    """
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=0)
    client, pushes = _wire(monkeypatch, player)

    for i in range(5):
        client._handle_stat_frame(
            player.mac, _tick(elapsed_ms=1000 + 250 * i, jiffies=100 + i,
                              output_fullness=3_500_000, signal=64))
        await asyncio.sleep(0)

    assert pushes.pushes == [player.mac], pushes.pushes


@pytest.mark.asyncio
async def test_a_real_state_change_pushes_again(monkeypatch):
    """A STAT is a signal again once the status it reports changed."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=0)
    client, pushes = _wire(monkeypatch, player)

    client._handle_stat_frame(player.mac, _tick(elapsed_ms=1000))
    await asyncio.sleep(0)
    assert len(pushes.pushes) == 1

    # e.g. the user turns the volume up from the web UI: the change becomes
    # visible on the NEXT tick (the only server-side clock we have) and
    # Perl's `mixer` notification would have pushed it.
    player.volume = 55
    client._handle_stat_frame(player.mac, _tick(elapsed_ms=1250))
    await asyncio.sleep(0)
    assert len(pushes.pushes) == 2

    # ... and only that one tick: the following heartbeat is silent again.
    client._handle_stat_frame(player.mac, _tick(elapsed_ms=1500))
    await asyncio.sleep(0)
    assert len(pushes.pushes) == 2


@pytest.mark.asyncio
async def test_a_track_change_pushes_again(monkeypatch):
    """STMs (a real event, Perl ``playerTrackStarted``) still pushes."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=0)
    client, pushes = _wire(monkeypatch, player)

    client._handle_stat_frame(player.mac, _tick(elapsed_ms=1000))
    await asyncio.sleep(0)
    player.current_track_id = 51994
    client._handle_stat_frame(player.mac, _tick("STMs", elapsed_ms=0))
    await asyncio.sleep(0)

    assert len(pushes.pushes) == 2


def test_status_signature_ignores_the_stat_clock():
    """``elapsed``/``signal``/jiffies/bytes move every tick — not part of it."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=0)
    before = status_signature(player)
    player.elapsed = 42.5
    player.signal_strength = 77
    player._stat = {"jiffies": 999, "bytes_received": 1}
    player._last_elapsed_seen = 42.5
    player.strm_sent_track = 51994
    player.playing_track_id = 51994
    player.last_activity = 1234567890.0
    assert status_signature(player) == before

    # ... but the status-bearing fields do.
    for field, value in (("mode", "play"), ("volume", 11), ("power", True),
                         ("current_track_id", 5), ("playlist", [1, 2]),
                         ("mute", True)):
        mutated = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=0)
        assert status_signature(mutated) == before
        setattr(mutated, field, value)
        assert status_signature(mutated) != before, field
