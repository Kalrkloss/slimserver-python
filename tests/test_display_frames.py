"""Display-Frames (PROT-18): ``visu``, ``vfdc``, ``grfb`` und der Framebuffer-``grfe``.

Alle erwarteten Byte-Folgen sind mit Perl ``pack`` erzeugt und hier als Hex
hinterlegt (Referenz: ``/tmp/lms-ref``):

* ``visu``  — ``Slim/Display/Squeezebox2.pm:259-291``
  ``pack "CC", $which, $count`` + je ``pack "N", $param``
* ``vfdc``  — ``Slim/Player/Squeezebox.pm:495-502`` (Payload = roher
  TextVFD-Stream aus ``Slim/Display/Lib/TextVFD.pm:312-357``)
* ``grfb``  — ``Slim/Display/Graphics.pm:496-509`` ``pack('n', $brightnessMap[$brightness])``
* ``grfe``  — ``Slim/Display/Squeezebox2.pm:230-250`` ``pack('n', $offset) .
  $transition . pack('c', $param) . $bits``

Rahmen (Server→Player): 2-Byte-BE-Länge INKL. der 4 Opcode-Bytes +
4 ASCII-Opcode-Bytes + Payload — ``Slim/Player/Squeezebox.pm:1159``
``my $frame = pack('n', $len + 4) . $type . $$dataRef;``.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from lyrion.networking.protocol import (
    BRIGHTNESS_MAP_SQUEEZEBOX2,
    BRIGHTNESS_MAP_SQUEEZEBOXG,
    DISPLAY_FRAMEBUF_BYTES_SB2,
    DISPLAY_TRANSITION_DEFAULT,
    VFD_MAX_BYTES,
    VISU_NONE,
    VISU_VUMETER,
    SlimProtoClient,
)

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"


class _FakeWriter:
    def __init__(self, closing: bool = False) -> None:
        self.frames: list[bytes] = []
        self.closing = closing

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closing


def _client_with_writer(closing: bool = False) -> tuple[SlimProtoClient, _FakeWriter]:
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter(closing=closing)
    client._player_writers = {MAC_CLEAN: writer}
    return client, writer


def _payload(frame: bytes) -> bytes:
    """Prüft das Längenfeld und liefert den Payload (Opcode + Daten)."""
    (length,) = struct.unpack(">H", frame[:2])
    payload = frame[2:]
    assert length == len(payload)
    return payload


# ── visu — Squeezebox2.pm:259-291 ─────────────────────────────────────────


def test_visu_mode3_exact_perl_bytes():
    """Mode 3 (VUMETER_SMALL) = params [1, 0, 0, 280, 18, 302, 18] (Squeezebox2.pm:86-88)."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_visu(MAC, [1, 0, 0, 280, 18, 302, 18]) is True
        frame = writer.frames[0]
        # Perl: pack("CC",1,6) . pack("N",0) . pack("N",0) . pack("N",280)
        #       . pack("N",18) . pack("N",302) . pack("N",18)
        expected = (
            b"\x00\x1e"          # Länge 30 = 4 Opcode + 2 Byte + 6*4
            b"visu"
            b"\x01\x06"
            b"\x00\x00\x00\x00"  # param 0
            b"\x00\x00\x00\x00"  # param 0
            b"\x00\x00\x01\x18"  # param 280
            b"\x00\x00\x00\x12"  # param 18
            b"\x00\x00\x01\x2e"  # param 302
            b"\x00\x00\x00\x12"  # param 18
        )
        assert frame == expected
        assert frame.hex() == (
            "001e" "76697375" "0106"
            "00000000" "00000000" "00000118" "00000012" "0000012e" "00000012"
        )
        assert VISU_VUMETER == 1

    asyncio.run(run())


def test_visu_hidden_is_one_byte_which_and_zero_count():
    """Versteckter Visualizer = ``[0]`` → ``pack("CC",0,0)`` (Squeezebox2.pm:266-268)."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_visu(MAC, [0]) is True
        assert writer.frames[0] == b"\x00\x06visu\x00\x00"
        assert VISU_NONE == 0

    asyncio.run(run())


def test_visu_mode9_count_byte_and_two_full_channels():
    """Mode 9 (spectrum analyser, 2 Kanäle) = 20 Werte (Squeezebox2.pm:110-112).

    Erwartung mit Perl erzeugt: ``pack("CC",2,19)`` + 19× ``pack("N",…)``
    für ``[0,0,0x10000,0,160,0,4,1,1,1,1,160,160,1,4,1,1,1,1]``.
    """
    params = [2, 0, 0, 0x10000, 0, 160, 0, 4, 1, 1, 1, 1,
              160, 160, 1, 4, 1, 1, 1, 1]
    perl_hex = (
        "0213"
        "00000000" "00000000" "00010000" "00000000" "000000a0"
        "00000000" "00000004" "00000001" "00000001" "00000001"
        "00000001" "000000a0" "000000a0" "00000001" "00000004"
        "00000001" "00000001" "00000001" "00000001"
    )

    async def run():
        client, writer = _client_with_writer()
        assert await client.send_visu(MAC, params) is True
        payload = _payload(writer.frames[0])
        assert payload[:4] == b"visu"
        assert payload[4] == 2      # which: spectrum analyser (Squeezebox2.pm:69)
        assert payload[5] == 0x13   # count = 19 Restwerte (Perl: scalar(@params))
        assert len(payload) == 4 + 2 + 19 * 4 == 82
        assert payload[4:].hex() == perl_hex
        assert payload[6:10] == b"\x00\x00\x00\x00"       # param 0
        assert payload[14:18] == b"\x00\x01\x00\x00"      # param 0x10000 (u32 BE)
        assert payload[22:26] == b"\x00\x00\x00\xa0"      # param 160

    asyncio.run(run())


def test_visu_truncates_like_perl_pack_cc_and_n():
    """Perl prüft keine Werte: ``pack("CC",300,-1)`` = ``2c ff``,
    ``pack("N",0x123456789)`` = ``23456789`` (mit perl -e verifiziert)."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_visu(MAC, [300, -1, 0x123456789]) is True
        payload = _payload(writer.frames[0])
        assert payload == b"visu" + bytes.fromhex("2c02") + bytes.fromhex("ffffffff") \
            + bytes.fromhex("23456789")

    asyncio.run(run())


def test_visu_without_writer_returns_false():
    async def run():
        client = SlimProtoClient.__new__(SlimProtoClient)
        client._player_writers = {}
        assert await client.send_visu(MAC, [0]) is False
        client, writer = _client_with_writer(closing=True)
        assert await client.send_visu(MAC, [0]) is False
        assert writer.frames == []

    asyncio.run(run())


# ── vfdc — Squeezebox.pm:495-502 ──────────────────────────────────────────

# TextVFD-Stream für displaywidth=2 und die Zeile "ABCD", byte-genau aus
# TextVFD.pm aufgebaut ($noritakeBrightPrelude :51-55 + $vfdBright[4] +
# $vfdReset :56 + escaping :337 + "HOME2"-Splice :340).
VFD_SAMPLE = bytes.fromhex(
    "0233 0200 0230 03"   # $noritakeBrightPrelude (TextVFD.pm:51-55)
    "00"                  # $vfdBright[4] = 100 % (:47)
    "020c 0202"           # $vfdReset (:56)
    "0341 0342"           # "AB", escaped mit $vfdCodeChar (:337)
    "02c0"                # $vfdCodeCmd . HOME2 (:340)
    "0343 0344"           # "CD", escaped
)
VFD_SAMPLE_HEX = "0233020002300300020c02020341034202c003430344"


def test_vfdc_frame_is_perl_passthrough_of_the_vfd_stream():
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_vfdc(MAC, VFD_SAMPLE) is True
        frame = writer.frames[0]
        # TextVFD.pm:359-362: Länge gerade und <= 500
        assert len(VFD_SAMPLE) % 2 == 0
        assert len(VFD_SAMPLE) == len(VFD_SAMPLE_HEX) // 2 == 22
        assert len(VFD_SAMPLE) <= VFD_MAX_BYTES
        assert frame == b"\x00\x1avfdc" + VFD_SAMPLE   # 26 = 4 + 22
        assert frame.hex() == "001a76666463" + VFD_SAMPLE_HEX
        assert _payload(frame)[4:] == VFD_SAMPLE

    asyncio.run(run())


def test_vfdc_sends_bytes_unchanged_even_when_perl_would_logdie():
    """Ungerader/zu langer Stream: wir warnen nur, Perl logdie't (TextVFD.pm:361-362)."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_vfdc(MAC, b"\x02") is True
        assert writer.frames[0] == b"\x00\x05vfdc\x02"
        over = bytes(VFD_MAX_BYTES + 2)
        assert await client.send_vfdc(MAC, over) is True
        assert _payload(writer.frames[1])[4:] == over

    asyncio.run(run())


def test_vfdc_without_writer_returns_false():
    async def run():
        client = SlimProtoClient.__new__(SlimProtoClient)
        client._player_writers = {}
        assert await client.send_vfdc(MAC, VFD_SAMPLE) is False
        client, writer = _client_with_writer(closing=True)
        assert await client.send_vfdc(MAC, VFD_SAMPLE) is False
        assert writer.frames == []

    asyncio.run(run())


# ── grfb — Graphics.pm:496-509 ────────────────────────────────────────────


def test_grfb_squeezebox2_brightness_map_bytes():
    """Squeezebox2.pm:209-211 ``(65535, 0, 1, 3, 4)`` → ``pack('n', …)``."""
    expected = {
        0: "ffff",   # 100 %
        1: "0000",   # aus
        2: "0001",
        3: "0003",
        4: "0004",
    }
    assert list(BRIGHTNESS_MAP_SQUEEZEBOX2) == [65535, 0, 1, 3, 4]
    assert sorted(expected) == list(range(len(BRIGHTNESS_MAP_SQUEEZEBOX2)))

    async def run():
        client, writer = _client_with_writer()
        for index, code in enumerate(BRIGHTNESS_MAP_SQUEEZEBOX2):
            assert await client.send_grfb(MAC, code) is True
            assert writer.frames[index] == b"\x00\x06grfb" + bytes.fromhex(expected[index])

    asyncio.run(run())


def test_grfb_squeezeboxg_brightness_map_bytes():
    """SqueezeboxG.pm:135-137 ``(0, 1, 4, 16, 30)`` — bitmapped SB1."""
    assert list(BRIGHTNESS_MAP_SQUEEZEBOXG) == [0, 1, 4, 16, 30]
    expected = ["0000", "0001", "0004", "0010", "001e"]

    async def run():
        client, writer = _client_with_writer()
        for index, code in enumerate(BRIGHTNESS_MAP_SQUEEZEBOXG):
            assert await client.send_grfb(MAC, code) is True
            assert _payload(writer.frames[index]) == b"grfb" + bytes.fromhex(expected[index])

    asyncio.run(run())


def test_grfb_truncates_mod_65536_like_pack_n():
    """``pack("n", 70000)`` = ``1170``, ``pack("n", -1)`` = ``ffff``."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_grfb(MAC, 70000) is True
        assert writer.frames[0] == b"\x00\x06grfb\x11\x70"
        assert await client.send_grfb(MAC, -1) is True
        assert writer.frames[1] == b"\x00\x06grfb\xff\xff"

    asyncio.run(run())


def test_grfb_without_writer_returns_false():
    async def run():
        client = SlimProtoClient.__new__(SlimProtoClient)
        client._player_writers = {}
        assert await client.send_grfb(MAC, 65535) is False
        client, writer = _client_with_writer(closing=True)
        assert await client.send_grfb(MAC, 65535) is False
        assert writer.frames == []

    asyncio.run(run())


# ── grfe-Framebuffer — Squeezebox2.pm:230-250 ──────────────────────────────

BITS = bytes.fromhex("aabbccdd")


def test_grfe_framebuffer_header_and_bits():
    """Kopf = offset(2 BE) + transition(1) + param(1 c), dann Bitmap (:243-248)."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_display_framebuffer(MAC, BITS) is True
        frame = writer.frames[0]
        assert DISPLAY_TRANSITION_DEFAULT == "c"
        assert frame == b"\x00\x0cgrfe" + bytes.fromhex("00006300") + BITS
        assert _payload(frame)[4:8] == bytes.fromhex("00006300")

    asyncio.run(run())


def test_scrollheader_is_exactly_the_default_4_byte_header():
    """Squeezebox2.pm:338-341 ``pack('n',0) . 'c' . pack('c',0)`` — der Beweis
    für das Header-Layout, den Graphics.pm:542 beim Scrollen voranstellt."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_display_framebuffer(MAC, b"") is True
        assert _payload(writer.frames[0]) == b"grfe" + bytes.fromhex("00006300")

    asyncio.run(run())


def test_grfe_transporter_screen2_offset_640():
    """Transporter.pm:209/:281 ``drawFrameBuf($bits, 640)`` → ``pack('n',640)`` = 0280."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_display_framebuffer(MAC, BITS, offset=640) is True
        assert _payload(writer.frames[0]) == b"grfe" + bytes.fromhex("02806300") + BITS

    asyncio.run(run())


def test_grfe_animation_transition_and_param():
    """pushBumpAnimate (Squeezebox2.pm:401-411) übergibt 'r' und den Screen-Extent."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_display_framebuffer(
            MAC, BITS, transition="r", param=64) is True
        assert _payload(writer.frames[0]) == b"grfe" + bytes.fromhex("00007240") + BITS

    asyncio.run(run())


def test_grfe_param_is_pack_c_signed_byte():
    """``pack("c",300)`` = ``2c``, ``pack("c",-1)`` = ``ff``."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_display_framebuffer(MAC, b"", param=300) is True
        assert _payload(writer.frames[0]) == b"grfe" + bytes.fromhex("0000632c")
        assert await client.send_display_framebuffer(MAC, b"", param=-1) is True
        assert _payload(writer.frames[1]) == b"grfe" + bytes.fromhex("000063ff")

    asyncio.run(run())


def test_grfe_offset_is_pack_n_u16():
    """``pack("n",65536)`` = ``0000``, ``pack("n",-1)`` = ``ffff``."""
    async def run():
        client, writer = _client_with_writer()
        assert await client.send_display_framebuffer(MAC, b"", offset=65536) is True
        assert _payload(writer.frames[0]) == b"grfe" + bytes.fromhex("00006300")
        assert await client.send_display_framebuffer(MAC, b"", offset=-1) is True
        assert _payload(writer.frames[1]) == b"grfe" + bytes.fromhex("ffff6300")

    asyncio.run(run())


def test_grfe_full_squeezebox2_framebuffer_length():
    """320x32-Panel = 4 Byte/Spalte * 320 Spalten = 1280 Byte (Squeezebox2.pm:180-190)."""
    assert DISPLAY_FRAMEBUF_BYTES_SB2 == 1280
    async def run():
        client, writer = _client_with_writer()
        bits = bytes(DISPLAY_FRAMEBUF_BYTES_SB2)
        assert await client.send_display_framebuffer(MAC, bits) is True
        frame = writer.frames[0]
        (length,) = struct.unpack(">H", frame[:2])
        assert length == 4 + 4 + DISPLAY_FRAMEBUF_BYTES_SB2 == 1288
        assert len(frame) == 2 + length

    asyncio.run(run())


def test_grfe_transition_must_be_one_byte():
    """Perl konkateniert den String ungeprüft (:244) — hier brechen wir ab."""
    async def run():
        client, _ = _client_with_writer()
        with pytest.raises(ValueError):
            await client.send_display_framebuffer(MAC, BITS, transition="cc")
        with pytest.raises(ValueError):
            await client.send_display_framebuffer(MAC, BITS, transition="")

    asyncio.run(run())


def test_grfe_without_writer_returns_false():
    async def run():
        client = SlimProtoClient.__new__(SlimProtoClient)
        client._player_writers = {}
        assert await client.send_display_framebuffer(MAC, BITS) is False
        client, writer = _client_with_writer(closing=True)
        assert await client.send_display_framebuffer(MAC, BITS) is False
        assert writer.frames == []

    asyncio.run(run())
