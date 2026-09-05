"""Tests for codec classification and legacy HELO parsing."""

import asyncio
import struct

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager


@pytest.mark.parametrize("mime,want", [
    ("audio/mpeg", "m"), ("audio/mp3", "m"),
    ("audio/flac", "f"), ("audio/x-flac", "f"),
    ("audio/aac", "a"), ("audio/mp4", "a"), ("audio/x-m4a", "a"),
    ("audio/ogg", "o"), ("audio/vorbis", "o"),
    ("audio/opus", "u"),
    ("audio/wav", "p"), ("audio/aiff", "p"), ("audio/x-aiff", "p"),
    ("audio/x-ms-wma", "w"), ("audio/wma", "w"),
    ("audio/alac", "l"), ("audio/x-alac", "l"),
    # substring traps: wavpack contains "wav" but is NOT raw PCM
    ("audio/x-wavpack", "m"), ("audio/ape", "m"), (None, "m"),
])
def test_codec_char_exact_mapping(mime, want):
    assert SlimProtoClient._codec_char(mime) == want


def test_legacy_20_byte_helo_registers_player():
    """Older firmware sends a 20-byte HELO body (no uuid); the server must
    accept it instead of blocking on a 43-byte read."""
    async def run():
        client = SlimProtoClient()
        server = await asyncio.start_server(client._handle_player, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        # 20-byte body: deviceid(1)+revision(1)+mac(6)+wlan(2)+brH(4)+brL(4)+lang(2)
        body = (
            bytes([4, 0])                          # deviceid, revision
            + bytes.fromhex("021122334455")        # mac
            + struct.pack(">H", 0)                 # wlan
            + struct.pack(">I", 0)                 # brH
            + struct.pack(">I", 0)                 # brL
            + b"en"                                # lang
        )
        frame = b"HELO" + struct.pack(">I", 20) + body

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(frame)
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

        return PlayerManager().get_player("02:11:22:33:44:55")

    try:
        p = asyncio.run(run())
        assert p is not None, "20-byte HELO must register the player"
    finally:
        PlayerManager().players.clear()
