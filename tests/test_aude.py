"""'aude' — Audio-Ausgänge schalten (Perl ``audio_outputs_enable``).

Perl:
* ``Slim/Player/Squeezebox2.pm:900-906``::

    my $data = pack('CC', $enabled, $enabled);   # spdif + dac
    $client->sendFrame('aude', \\$data);

* Aufrufer: ``Player.pm:253`` (Power off → ``audio_outputs_enable(0)``),
  ``Player.pm:268`` (Power on → ``(1)``) und ``Slimproto.pm:1265``
  (bei jedem HELO: ``audio_outputs_enable($client->power())``).
* Basisklasse ``Player.pm:358`` ist ein No-op.

Rahmen: Server→Player = 2-Byte-BE-Länge (inkl. 4 Opcode-Bytes) + 4-ASCII-Opcode
+ Payload, hier also ``00 06 'aude' 01 01``.
"""

from __future__ import annotations

import asyncio
import struct

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"


class _FakeWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closing = False

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closing


class _RecordingHandler:
    """Minimal protocol handler that records send_aude calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def send_aude(self, mac: str, enabled: bool) -> bool:
        self.calls.append((mac, enabled))
        return True


def _client_with_writer() -> tuple[SlimProtoClient, _FakeWriter]:
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    return client, writer


def _manager_with_player(handler=None) -> tuple[PlayerManager, PlayerState]:
    pm = PlayerManager()
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=57510,
                         model="squeezeplay", power=False)
    pm.players = {MAC: player}
    pm._protocol_handler = handler
    return pm, player


# ── Frame-Layout ───────────────────────────────────────────────────────────


def test_aude_frame_enabled_is_two_ones():
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_aude(MAC, True) is True
        assert writer.frames[0] == b"\x00\x06aude\x01\x01"

    asyncio.run(run())


def test_aude_frame_disabled_is_two_zeros():
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_aude(MAC, False) is True
        assert writer.frames[0] == b"\x00\x06aude\x00\x00"
        # Länge im Header zählt die 4 Opcode-Bytes mit (Perl sendFrame)
        (length,) = struct.unpack(">H", writer.frames[0][:2])
        assert length == 4 + 2

    asyncio.run(run())


def test_aude_without_writer_returns_false():
    async def run():
        client = SlimProtoClient.__new__(SlimProtoClient)
        client._player_writers = {}
        assert await client.send_aude(MAC, True) is False

    asyncio.run(run())


# ── Aufrufer: Power-Schalter ────────────────────────────────────────────────


def test_set_power_sends_aude_for_both_directions():
    async def run():
        handler = _RecordingHandler()
        pm, _ = _manager_with_player(handler)
        pm.set_power(MAC, True)
        await asyncio.sleep(0)     # die eingeplante Task ausführen
        pm.set_power(MAC, False)
        await asyncio.sleep(0)
        assert handler.calls == [(MAC, True), (MAC, False)]

    asyncio.run(run())


def test_power_on_for_playback_only_on_transition():
    async def run():
        handler = _RecordingHandler()
        pm, player = _manager_with_player(handler)
        # Player ist aus → Power on + aude(1)
        await pm.power_on_for_playback(player)
        assert player.power is True
        assert handler.calls == [(MAC, True)]
        # schon an → kein zweiter Frame
        await pm.power_on_for_playback(player)
        assert handler.calls == [(MAC, True)]

    asyncio.run(run())


# ── Aufrufer: HELO (Slimproto.pm:1265) ─────────────────────────────────────


def test_helo_sends_aude_with_current_power_state():
    async def run():
        client, writer = _client_with_writer()
        pm, player = _manager_with_player()
        player.power = False
        await client._send_aude_state(MAC)
        assert writer.frames[0] == b"\x00\x06aude\x00\x00"
        player.power = True
        await client._send_aude_state(MAC)
        assert writer.frames[1] == b"\x00\x06aude\x01\x01"

    asyncio.run(run())


def test_manager_send_audio_outputs_without_handler_is_false():
    async def run():
        pm, _ = _manager_with_player(None)
        assert await pm.send_audio_outputs(MAC, True) is False

    asyncio.run(run())
