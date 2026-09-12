"""Display-Frames (grfe) + der Default der Anzeigedauer.

Perl: ``Slim/Display/Display.pm:258``
``$duration = $args->{'duration'} || 1;  # duration - default to 1 second``
und ``Slim/Utils/Prefs.pm:169`` ``'displaytexttimeout' => 1``.

Wir hatten 3 s erfunden (Audit-Konstanten D49) — bei einem "showBriefly"
bleibt so jeder Hinweis dreimal so lange stehen wie im Perl-LMS.
"""

from __future__ import annotations

import asyncio
import struct

from lyrion.networking.protocol import (
    DISPLAY_DURATION_DEFAULT,
    SlimProtoClient,
)

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"


class _FakeWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _client_with_writer():
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    return client, writer


def test_display_duration_default_is_perls_one_second():
    # Display.pm:258 / Utils/Prefs.pm:169
    assert DISPLAY_DURATION_DEFAULT == 1


def test_grfe_frame_layout_and_default_duration():
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_display_to_player(MAC, "Zeile 1", "Zeile 2") is True
        frame = writer.frames[0]
        (length,) = struct.unpack(">H", frame[:2])
        payload = frame[2:]
        assert length == len(payload)
        assert payload[:4] == b"grfe"
        assert payload[4] == 0x01                    # format: two text lines
        assert struct.unpack(">H", payload[5:7])[0] == 1   # Perl's 1 s default
        rest = payload[7:]
        line1, _, tail = rest.partition(b"\x00")
        line2, _, tail = tail.partition(b"\x00")
        assert line1 == "Zeile 1".encode()
        assert line2 == "Zeile 2".encode()
        assert tail == b""

    asyncio.run(run())


def test_explicit_duration_is_sent_verbatim():
    async def run():
        client, writer = _client_with_writer()
        await client.send_display_to_player(MAC, "a", "b", duration=7)
        payload = writer.frames[0][2:]
        assert struct.unpack(">H", payload[5:7])[0] == 7
        # clamping stays inside the 2-byte field
        await client.send_display_to_player(MAC, "a", "b", duration=99999)
        clamped = writer.frames[1][2:]
        assert struct.unpack(">H", clamped[5:7])[0] == 65535

    asyncio.run(run())


def test_display_without_connected_player_returns_false():
    async def run():
        client = SlimProtoClient.__new__(SlimProtoClient)
        client._player_writers = {}
        assert await client.send_display_to_player(MAC, "a", "b") is False

    asyncio.run(run())
