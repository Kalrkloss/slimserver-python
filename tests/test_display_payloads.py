"""Display-Nutzlasten (PROT-18): Font-Rendering + TextVFD — Perl-Goldens.

Jede Byte-Folge in dieser Datei ist mit der **echten Perl-Implementierung**
erzeugt (``/tmp/lms-ref``) und hier als Hex eingetragen:

* ``Slim/Display/Lib/Fonts.pm`` — ``parseBMP`` (:767-843), ``parseFont``
  (:726-762), ``string`` (:292-522), ``loadExtent`` (:274-290). Quellen sind
  die Schriftdateien im Repo (``graphics/*.font.bmp``; Fonts.pm:566-608 sammelt
  im Graphics-Verzeichnis ``<name>.font.bmp``), gegengeprüft gegen den
  mitgelieferten Perl-Font-Cache ``graphics/corefonts.bin`` (Perl ``retrieve``),
  der dieselben Tabellen liefert.
* ``Slim/Display/Lib/TextVFD.pm`` — ``vfdUpdate`` (:147-363) mit einem
  Perl-Testclient (``displayWidth``/``vfdmodel``/``brightness``/``vfd``) und der
  Kommandotabelle aus ``Slim/Display/Text.pm:867-877``.
* ``Slim/Display/Graphics.pm:99-103`` + ``:398-414`` — ``screenBytes`` sowie
  Null-Polsterung/Abschnitt der Zeilen.

Erzeugt hat die Goldens ``/tmp/perlharness/gen_tests.py`` (mit ``oracle.pl``
und ``oracle_vfd.pl``); die Bytes selbst sind hier eingefroren, der Test läuft
ohne Perl.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player import fonts
from lyrion.player.display import (
    BOOM,
    NODISPLAY,
    SQUEEZEBOX2,
    SQUEEZEBOXG,
    TEXT,
    TRANSPORTER,
    DisplayWiring,
)
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"

# ── Perl-Goldens: Font-Tabellen (parseBMP/parseFont) ──────────────────────
# sha256 über alle Zeichenblöcke in Index-Reihenfolge.
PERL_TABLE_DIGESTS: dict[str, tuple[int, str]] = {
    "standard.2": (256, "00e7fac1d2078e92b276acd82d560bcecb1f6979806aea4657839a88f4c42057"),
    "standard.1": (256, "40023a72c0c845d5c389ee143fceed9fbfc93a17185cf10f6e676857876266a0"),
    "standard_n.2": (256, "419f915ea53e27153e85139c6c22e58705f3f526dee25b9cf49c079c3b7e0a9b"),
    "medium.1": (256, "17e0cc7046f0846ffbd6dbe682013622d0638f72cec0fd7b09cc621b203ee8ad"),
    "full.2": (256, "4c8ad2781f152f512c2b9a3f8185f9155b31a9cbcd5bb49232f5089a6c8750cd"),
}

# ── Perl-Goldens: ``Fonts::string`` ───────────────────────────────────────
# perl Fonts::string("standard.2", b'Now Playing') — SB2 line[0] (fonthash->{standard}->{line}[0] = standard.1)
STRING_STANDARD_2_4E6F7720506C6179696E67 = (
    "0007fff00007fff0000380000001c000000070000000380000000e0000000700000001c0000000e00007fff00007fff0"
    "000000000000000000000f8000003fe000003060000060300000603000006030000060300000306000003fe000000f80"
    "00000000000000000000600000007c0000000f00000003e000000070000007f000003f000000700000003f00000007f0"
    "00000070000003e000000f0000007c00000060000000000000000000000000000000000000000000000000000007fff0"
    "0007fff000060c0000060c0000060c0000060c0000060c0000060c0000071c000003f8000001f0000000000000000000"
    "0007fff00007fff00000000000000000000031e0000033f0000063300000633000006630000066300000666000007fe0"
    "00003ff00000001000000000000000000000600300007c0300001f03000007c7000000fe000001f800001fc000007e00"
    "00006000000000000000000000067ff000067ff0000000000000000000007ff000007ff0000030000000600000006000"
    "000060000000700000003ff000001ff0000000000000000000000f8600003fe600007073000060330000603300006033"
    "0000306700007ffe00007ffc"
)

# perl Fonts::string("standard.1", b'Test') — SB2 obere Zeile
STRING_STANDARD_1_54657374 = (
    "4000000040000000400000007f00000040000000400000004000000000000000000000007f0000004900000049000000"
    "490000004100000000000000000000003200000049000000490000004900000026000000000000000000000040000000"
    "40000000400000007f000000400000004000000040000000"
)

# perl Fonts::string("standard_n.2", b'Now Playing') — Boom line[1] (Boom.pm:126-131)
STRING_STANDARD_N_2_4E6F7720506C6179696E67 = (
    "0007fff00007fff0000380000001c000000070000000380000000e0000000700000001c0000000e00007fff00007fff0"
    "0000000000000f8000003fe000003060000060300000603000006030000060300000306000003fe000000f8000000000"
    "0000600000007c0000000f00000003e000000070000007f000003f000000700000003f00000007f000000070000003e0"
    "00000f0000007c0000006000000000000000000000000000000000000007fff00007fff000060c0000060c0000060c00"
    "00060c0000060c0000060c0000071c000003f8000001f000000000000007fff00007fff000000000000031e0000033f0"
    "000063300000633000006630000066300000666000007fe000003ff000000010000000000000600300007c0300001f03"
    "000007c7000000fe000001f800001fc000007e00000060000000000000067ff000067ff00000000000007ff000007ff0"
    "000030000000600000006000000060000000700000003ff000001ff00000000000000f8600003fe60000707300006033"
    "00006033000060330000306700007ffe00007ffc"
)

# perl Fonts::string("medium.1", b'ABC') — SqueezeboxG line[0] (SqueezeboxG.pm:46-51)
STRING_MEDIUM_1_414243 = (
    "7800a000a000a000780000000000f800a800a800a80050000000000070008800880088005000"
)

# perl Fonts::string("full.2", b'Hi') — SB2 full.2
STRING_FULL_2_4869 = (
    "7fffff807fffff807fffff80001c0000001c0000001c0000001c0000001c0000001c0000001c0000001c0000001c0000"
    "001c0000001c0000001c00007fffff807fffff807fffff8000000000000000000000000071ffff8071ffff8071ffff80"
)

# perl Fonts::string("standard.2", b'caf\xe9') — cp1252/latin1-Zeichen
STRING_STANDARD_2_636166E9 = (
    "00000f8000003fe00000307000006030000060300000603000007070000038e0000018c00000000000000000000031e0"
    "000033f0000063300000633000006630000066300000666000007fe000003ff000000010000000000000000000006000"
    "000060000003fff00007fff0000660000006600000060000000000000000000000000f8000003fe00000366000006630"
    "0003663000066630000466300000367000003e6000000e40"
)

# perl Fonts::string("medium.1", b'\nA') — cursorpos-Kommando (Fonts.pm:414-416, :493-507)
STRING_MEDIUM_1_0A41 = (
    "040004007c00a400a400a4007c0004000400"
)

# perl Fonts::string("medium.1", b'A\x1dB') — tight (Fonts.pm:404-407)
STRING_MEDIUM_1_411D42 = (
    "7800a000a000a000780000000000f800a800a800a8005000"
)

# perl Fonts::string("medium.1", b'A\x1cB') — /tight (Fonts.pm:409-412)
STRING_MEDIUM_1_411C42 = (
    "7800a000a000a000780000000000f800a800a800a8005000"
)

# perl Fonts::string("medium.1", b'\x1bmedium\x1bX') — Schriftwechsel (Fonts.pm:386-402)
STRING_MEDIUM_1_1B6D656469756D1B58 = (
    "88005000200050008800"
)

# perl TextVFD::vfdUpdate('noritake-latin1', 'Now Playing', 'Radiohead - Creep', brightness=4, width=40) — Text.pm:105-106 (SB1)
VFD_NORITAKE_LATIN1_4_NOW_PLAYING = (
    "023302000230030002060202020c034e036f037703200350036c036103790369036e03670352036103640369036f0368"
    "0365036103640320032d03200343037203650365037002c0"
)

# perl TextVFD::vfdUpdate('squeezeslave', 'Slim', 'Slave', brightness=undef, width=40) — Text.pm:100-101
VFD_SQUEEZESLAVE_undef_SLIM = (
    "02060202020c0353036c0369036d0353036c03610376036502c0"
)

# perl TextVFD::vfdUpdate('noritake-latin1', 'A', None, brightness=2, width=40) — undef-Zeile -> Leerzeile (TextVFD.pm:172-173)
VFD_NORITAKE_LATIN1_2_A = (
    "023302000230030202060202020c03410320032003200320032003200320032003200320032003200320032003200320"
    "0320032003200320032003200320032003200320032003200320032003200320032003200320032003200320032002c0"
    "0320"
)

# perl TextVFD::vfdUpdate('noritake-latin1', 'X', 'Y', brightness=0, width=4) — Helligkeit 0 blendet aus (TextVFD.pm:175-178)
VFD_NORITAKE_LATIN1_0_X = (
    "023302000230030302060202020c032003200320032002c00320032003200320"
)

# perl TextVFD::vfdUpdate('noritake-latin1', 'A\x1ecursorpos\x1eB', None, brightness=4, width=40) — Cursor (TextVFD.pm:213-216, :345-355)
VFD_NORITAKE_LATIN1_4_A_CURSORPOS_B = (
    "023302000230030002060202020c03410342032003200320032003200320032003200320032003200320032003200320"
    "0320032003200320032003200320032003200320032003200320032003200320032003200320032003200320032002c0"
    "032003200281020e"
)

# perl TextVFD::vfdUpdate('noritake-latin1', '\x1fhardspace\x1fX', None, brightness=4, width=40) — symbolmap latin1 (TextVFD.pm:79-83)
VFD_NORITAKE_LATIN1_4_HARDSPACE_X = (
    "023302000230030002060202020c03200358032003200320032003200320032003200320032003200320032003200320"
    "0320032003200320032003200320032003200320032003200320032003200320032003200320032003200320032002c0"
    "03200320"
)

# perl TextVFD::vfdUpdate('noritake-katakana', '\x1frightarrow\x1f', None, brightness=4, width=40) — symbolmap katakana (:71-78)
VFD_NORITAKE_KATAKANA_4_RIGHTARROW = (
    "023302000230030002060202020c037e0320032003200320032003200320032003200320032003200320032003200320"
    "0320032003200320032003200320032003200320032003200320032003200320032003200320032003200320032002c0"
    "0320"
)

# perl TextVFD::vfdUpdate('noritake-european', '¡ä', None, brightness=4, width=40) — tr{} european (:295-298)
VFD_NORITAKE_EUROPEAN_4_ = (
    "023302000230030002060202020c032103e1032003200320032003200320032003200320032003200320032003200320"
    "0320032003200320032003200320032003200320032003200320032003200320032003200320032003200320032002c0"
    "03200320"
)

# perl TextVFD::vfdUpdate('noritake-latin1', '\x92q\x92', None, brightness=4, width=40) — tr{} latin1 (:303-308)
VFD_NORITAKE_LATIN1_4_Q = (
    "023302000230030002060202020c03260371032603200320032003200320032003200320032003200320032003200320"
    "0320032003200320032003200320032003200320032003200320032003200320032003200320032003200320032002c0"
    "032003200320"
)

# perl TextVFD::vfdUpdate('futaba-latin1', 'AB', 'CD', brightness=2, width=40) — Futaba-Helligkeit (:317-318)
VFD_FUTABA_LATIN1_2_AB = (
    "023a02060202020c034103420343034402c0"
)


# ── Perl-Goldens: ``TextVFD::vfdUpdate`` ──────────────────────────────────

# (font, text, erwartete Bits)
STRING_GOLDENS = (
    ("standard.2", 'Now Playing', STRING_STANDARD_2_4E6F7720506C6179696E67),
    ("standard.1", 'Test', STRING_STANDARD_1_54657374),
    ("standard_n.2", 'Now Playing', STRING_STANDARD_N_2_4E6F7720506C6179696E67),
    ("medium.1", 'ABC', STRING_MEDIUM_1_414243),
    ("full.2", 'Hi', STRING_FULL_2_4869),
    ("standard.2", 'café', STRING_STANDARD_2_636166E9),
    ("medium.1", '\nA', STRING_MEDIUM_1_0A41),
    ("medium.1", 'A\x1dB', STRING_MEDIUM_1_411D42),
    ("medium.1", 'A\x1cB', STRING_MEDIUM_1_411C42),
    ("medium.1", '\x1bmedium\x1bX', STRING_MEDIUM_1_1B6D656469756D1B58),
)

# (model, brightness, width, line1, line2, erwarteter vfd-Strom)
VFD_GOLDENS = (
    ("noritake-latin1", "4", 40, 'Now Playing', 'Radiohead - Creep', VFD_NORITAKE_LATIN1_4_NOW_PLAYING),
    ("squeezeslave", "undef", 40, 'Slim', 'Slave', VFD_SQUEEZESLAVE_undef_SLIM),
    ("noritake-latin1", "2", 40, 'A', None, VFD_NORITAKE_LATIN1_2_A),
    ("noritake-latin1", "0", 4, 'X', 'Y', VFD_NORITAKE_LATIN1_0_X),
    ("noritake-latin1", "4", 40, 'A\x1ecursorpos\x1eB', None, VFD_NORITAKE_LATIN1_4_A_CURSORPOS_B),
    ("noritake-latin1", "4", 40, '\x1fhardspace\x1fX', None, VFD_NORITAKE_LATIN1_4_HARDSPACE_X),
    ("noritake-katakana", "4", 40, '\x1frightarrow\x1f', None, VFD_NORITAKE_KATAKANA_4_RIGHTARROW),
    ("noritake-european", "4", 40, '¡ä', None, VFD_NORITAKE_EUROPEAN_4_),
    ("noritake-latin1", "4", 40, '\x92q\x92', None, VFD_NORITAKE_LATIN1_4_Q),
    ("futaba-latin1", "2", 40, 'AB', 'CD', VFD_FUTABA_LATIN1_2_AB),
)


# ── Hilfen ────────────────────────────────────────────────────────────────


class _FakeWriter:
    """Socket-Doppel wie in ``tests/test_display_wiring.py``."""

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


def _player(model: str = "squeezebox2", **kwargs) -> PlayerState:
    return PlayerState(
        mac=MAC, name="Taverne", ip="127.0.0.1", port=3483, model=model, **kwargs
    )


def _frame_data(frame: bytes) -> bytes:
    """Rahmen ohne Längenfeld **und** ohne die 4 Opcode-Bytes.

    ``Slim/Player/Squeezebox.pm:1159`` ``pack('n', $len + 4) . $type . $$dataRef``
    — die Nutzlast beginnt nach ``pack('n', len+4) . 'grfe'``.
    """
    (length,) = struct.unpack(">H", frame[:2])
    payload = frame[2:]
    assert length == len(payload), "Rahmenlänge inkl. 4 Opcode-Bytes (Squeezebox.pm:1159)"
    return payload[4:]


def _opcodes(writer: _FakeWriter) -> list[str]:
    return [frame[2:6].decode("ascii") for frame in writer.frames]


# ── Font-Parser (Fonts.pm:726-843) ───────────────────────────────────────


@pytest.mark.parametrize("name", sorted(PERL_TABLE_DIGESTS))
def test_font_tables_match_perls_parse_of_the_bmp(name):
    """Zeichenblöcke Byte für Byte wie Perl ``parseBMP``+``parseFont``.

    Die Schriften sind die Dateien aus ``graphics/`` (Fonts.pm:566-608 liest
    ``*.font.bmp`` aus dem Graphics-Verzeichnis); ``table[0]`` ist der
    Zwischenraum (:310), die Höhe ist ``biHeight-1`` (:696/:842).
    """
    count, digest = PERL_TABLE_DIGESTS[name]
    font = fonts.load_font(name)
    assert len(font.table) == count
    assert hashlib.sha256(b"".join(font.table)).hexdigest() == digest


def test_font_metadata_matches_perls_fontcache():
    """Höhen und Extents wie im Perl-Cache ``graphics/corefonts.bin``.

    ``$fonts->{height}`` = ``biHeight-1`` (Fonts.pm:696/:842); ``extent`` =
    gesetzte Bits von ``string($name, chr(0x1f))`` (:263-290), negativ für
    ``.1``-Schriften (:285) — genau die Werte, die der Perl-Cache trägt.
    """
    assert fonts.font_height("standard.2") == 32
    assert fonts.font_height("medium.1") == 16
    assert fonts.font_height("full.2") == 32
    assert fonts.font_height("gibtsnicht.2") == 0
    assert fonts.load_font("standard.2").extent == 19
    assert fonts.load_font("standard.1").extent == -11
    assert fonts.load_font("medium.1").extent == 0
    assert fonts.load_font("light.1").extent == -15
    # Zwischenraumzeichen = Block 0 (:310), zwei Pixelspalten breit und leer
    for name in ("standard.2", "medium.1"):
        font = fonts.load_font(name)
        assert font.interspace == b"\x00" * (2 * font.bytes_per_column)
        assert font.bytes_per_column == font.height // 8


# ── Text -> Bits (Fonts.pm:292-522) ──────────────────────────────────────


@pytest.mark.parametrize("font_name,text,expected", STRING_GOLDENS)
def test_render_text_matches_perl_string(font_name, text, expected):
    """``Fonts::string($font, $text)`` — Zeichenblöcke + Zwischenraum
    (:307-316) bzw. die Sonderzeichen-Schleife (:380-519)."""
    assert fonts.render_text(text, font_name).hex() == "".join(expected.split())


def test_render_text_ignores_characters_above_latin1_like_perl_without_ttf():
    """Ohne TTF-Zweig (:318-355) wird alles > 255 zu ``'?'`` (:489-491)."""
    assert fonts.render_text("\u20ac", "standard.2") == fonts.render_text("?", "standard.2")


def test_missing_font_file_is_reported_not_invented():
    """Perl: unbekannte Schrift -> ``logBacktrace`` + ``(0, '')``
    (Fonts.pm:300-304); ``fontheight`` liefert 0 (:248-252)."""
    with pytest.raises(OSError):
        fonts.load_font("gibtsnicht.2")
    assert fonts.font_height("gibtsnicht.2") == 0


def test_render_text_width_pads_and_truncates_like_graphics_pm():
    """``Graphics.pm:99-103`` + ``:398-414`` — Null-Polsterung bzw. Abschnitt."""
    raw = fonts.render_text("Test", "standard.2")
    assert len(raw) == 168
    padded = fonts.render_text("Test", "standard.2", width=320)
    assert len(padded) == 320 * 4                  # screenBytes (Graphics.pm:99-103)
    assert padded[:len(raw)] == raw
    assert padded[len(raw):] == b"\x00" * (320 * 4 - len(raw))
    long_raw = fonts.render_text("A" * 60, "standard.2")
    truncated = fonts.render_text("A" * 60, "standard.2", width=20)
    assert truncated == long_raw[:20 * 4]


def test_screen_width_follows_the_perl_mode_tables():
    """``displayWidth`` = ``modes->[$mode]{width}`` (Squeezebox2.pm:188-203).

    Breiten aus Squeezebox2.pm:72-129 (320 bzw. 278 klein), Boom.pm:26-104
    (160/145), SqueezeboxG.pm:127-129 (280), Transporter.pm:192-193 (320).
    """
    assert fonts.screen_width(SQUEEZEBOX2, 0) == 320
    assert fonts.screen_width(SQUEEZEBOX2, 5) == 278    # Default playingDisplayMode
    assert fonts.screen_width(SQUEEZEBOX2, 9) == 320
    assert fonts.screen_width(BOOM, 4) == 145
    assert fonts.screen_width(BOOM, 1) == 160
    assert fonts.screen_width(SQUEEZEBOXG, 3) == 280
    assert fonts.screen_width(TRANSPORTER, 0) == 320
    assert fonts.screen_width(SQUEEZEBOX2, 99) == 320   # wie ``modes->[$mode || 0]`` (:202)


def test_render_display_text_builds_the_screen_of_a_bitmapped_sb1():
    """SqueezeboxG: 2 Byte/Spalte · 280 Spalten = 560 Bytes (SqueezeboxG.pm:119-129).

    Die Bits sind ``string('medium.1', 'ABC')`` (Perl-Golden), mit Nullen auf
    ``screenBytes()`` aufgefüllt (Graphics.pm:398-400).
    """
    golden = bytes.fromhex("".join(
        "".join(expected.split()) for font, text, expected in STRING_GOLDENS
        if (font, text) == ("medium.1", "ABC")))
    bits = fonts.render_display_text(SQUEEZEBOXG, "ABC")
    assert len(bits) == 560
    assert bits[:len(golden)] == golden
    assert bits[len(golden):] == b"\x00" * (560 - len(golden))


def test_render_display_text_stacks_lines_like_graphics_render():
    """``Graphics.pm:284-285`` rendert ``line[$l]`` mit Schrift ``.<l+1>`` und
    verodert die Zeilen (:400)."""
    line0 = fonts.render_text("A", "standard.1", width=320)
    line1 = fonts.render_text("B", "standard.2", width=320)
    both = fonts.render_display_text(SQUEEZEBOX2, ["A", "B"])
    assert len(both) == 1280
    assert both == bytes(a | b for a, b in zip(line0, line1))


# ── TextVFD (TextVFD.pm:147-363) ─────────────────────────────────────────


@pytest.mark.parametrize("model,brightness,width,line1,line2,expected", VFD_GOLDENS)
def test_vfd_update_matches_perl(model, brightness, width, line1, line2, expected):
    """``vfdUpdate`` Byte für Byte: Brightness-Prelude (:317-321), ``$vfdReset``
    (:67), CFF (:334), Zeichen (:337-342), HOME2 (:340), Cursor (:345-355)."""
    actual = fonts.vfd_update(
        line1, line2, model=model, width=width,
        brightness=None if brightness == "undef" else int(brightness))
    assert actual.hex() == "".join(expected.split())


def test_vfd_update_rejects_unknown_custom_char_tokens():
    """Custom-Zeichen brauchen die Bitmaps aus ``TextVFD.pm:365-1141``
    (nicht portiert) — kein geratener Ersatz."""
    with pytest.raises(fonts.UnsupportedVFDToken):
        fonts.vfd_update("\x1funknownchar\x1f", None, width=40, brightness=4)


def test_vfd_update_model_language_derivation():
    """``$lang =~ s/[^-]*-(.*)/$1/`` (TextVFD.pm:161-166)."""
    assert fonts.vfd_model_language("noritake-latin1") == "latin1"
    assert fonts.vfd_model_language("noritake-european") == "european"
    assert fonts.vfd_model_language("futaba-latin1") == "latin1"
    assert fonts.vfd_model_language("squeezeslave") == "squeezeslave"
    assert fonts.vfd_model_language("") == "katakana"


# ── Verdrahtung: echte Nutzlasten in den Frames ──────────────────────────


def test_squeezebox2_gets_a_real_grfe_bitmap_for_the_given_text():
    """``Squeezebox2::drawFrameBuf`` (:230-250) mit gerenderten Bits.

    Default-Prefs ``playingDisplayMode 5`` -> ``modes[5]{width}`` = 278
    (:131-134, :72-129), ``screenBytes`` = 4·278 = 1112 (Graphics.pm:99-103).
    """
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play")

        sent = await wiring.update(player, text="A")
        assert sent == ["visu", "grfe"]
        data = _frame_data(writer.frames[1])
        assert data[:4] == bytes.fromhex("00006300")   # offset/transition/param (:243-245)
        bits = data[4:]
        assert len(bits) == 278 * 4
        assert bits[:8] == fonts.render_text("A", "standard.1")[:8]

    asyncio.run(run())


def test_squeezebox2_stopped_renders_the_full_screen_width():
    """Ohne Visualizer ``$mode = 0`` -> 320 Spalten (Squeezebox2.pm:195-203);
    der erste ``visu``-Frame ist Perls ``[0]`` „hide all“ (:301-305)."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="stop")
        assert await wiring.update(player, text="Test") == ["visu", "grfe"]
        data = _frame_data(writer.frames[1])
        assert data[:4] == bytes.fromhex("00006300")
        bits = data[4:]
        assert len(bits) == 320 * 4
        rendered = fonts.render_text("Test", "standard.1")
        assert bits[:len(rendered)] == rendered
        assert bits[len(rendered):] == b"\x00" * (len(bits) - len(rendered))

    asyncio.run(run())


def test_boom_uses_its_narrow_font_and_width():
    """Boom: Familie ``standard_n`` (Boom.pm:126-131), ``modes[1]{width}`` = 160."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("boom", mode="play")
        assert await wiring.update(player, text="A") == ["visu", "grfe"]
        bits = _frame_data(writer.frames[1])[4:]
        assert len(bits) == 160 * 4
        assert bits[:8] == fonts.render_text("A", "standard_n.1")[:8]

    asyncio.run(run())


def test_bitmapped_sb1_gets_a_real_grfd_bitmap():
    """SqueezeboxG: ``grfd`` (SqueezeboxG.pm:149-167), Header ``pack('n',560)``."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox", bitmapped=True, mode="play")
        assert await wiring.update(player, text="ABC") == ["grfd"]
        data = _frame_data(writer.frames[0])
        assert data[:2] == struct.pack(">H", 560)          # :34/:158
        assert data[2:] == fonts.render_display_text(SQUEEZEBOXG, "ABC")
        assert len(data[2:]) == 560

    asyncio.run(run())


def test_text_display_gets_a_real_vfdc_stream():
    """``Text.pm:437-441`` -> ``TextVFD::vfdUpdate`` -> ``vfdc``.

    Modell ``squeezeslave`` -> ``vfdmodel = 'squeezeslave'`` (Text.pm:100-101),
    Breite 40 (Text.pm:36) — der Strom ist der Perl-Golden.
    """
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezeslave", mode="play")
        assert await wiring.update(player, text=["Slim", "Slave"]) == ["vfdc"]
        data = _frame_data(writer.frames[0])
        assert data == fonts.vfd_update("Slim", "Slave", model="squeezeslave",
                                        width=40, brightness=2)
        golden = [e for m, b, w, l1, l2, e in VFD_GOLDENS
                  if (m, l1, l2) == ("squeezeslave", "Slim", "Slave")][0]
        assert data.hex() == "".join(golden.split())

    asyncio.run(run())


def test_vfd_uses_the_brightness_that_was_set():
    """``$client->brightness()`` (TextVFD.pm:170) — ``set_brightness`` merkt sie
    (Display.pm:378-398); Text.pm:575-580 schickt dabei keinen ``grfb``."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezeslave", mode="play")
        await wiring.set_brightness(player, 0)
        await wiring.update(player, text="A")
        assert _frame_data(writer.frames[0]) == fonts.vfd_update(
            "A", None, model="squeezeslave", width=40, brightness=0)
        assert "grfb" not in _opcodes(writer)

    asyncio.run(run())


def test_explicit_bits_and_vfd_still_win_over_text():
    """``bits=``/``vfd=`` bleiben die fertige Nutzlast."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="stop")
        assert await wiring.update(player, bits=b"\xaa\xbb", text="A") == ["visu", "grfe"]
        assert _frame_data(writer.frames[-1])[4:] == b"\xaa\xbb"

    asyncio.run(run())


def test_models_without_display_get_no_payload_even_with_text():
    """``NoDisplay.pm:32`` — auch mit Text kein Frame."""
    async def run():
        assert NODISPLAY == "NoDisplay"
        for model in ("receiver", "squeezeplay", "controller", "squeezelite"):
            client, writer = _client_with_writer()
            wiring = DisplayWiring(client)
            assert await wiring.update(_player(model, mode="play"), text="A") == []
            assert writer.frames == []
            assert TEXT not in _opcodes(writer)

    asyncio.run(run())


def test_unrenderable_text_sends_no_invented_payload():
    """Ein Custom-Char-Token ist im Port nicht renderbar -> kein ``vfdc``
    statt eines erfundenen Stroms."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezeslave", mode="play")
        assert await wiring.update(player, text="\x1funknownchar\x1f") == []
        assert writer.frames == []

    asyncio.run(run())


def test_power_and_on_connect_pass_the_text_through():
    """``Player::power`` (:255-290) und Connect (:114-124) rendern mit.

    Frame-Reihenfolge wie in Perl: Helligkeit (``grfb``) vor dem Screen;
    ``sent`` zählt nur die ``update``-Frames (``set_brightness`` liefert den
    Code, nicht den Frame).
    """
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play", power=True)
        assert await wiring.power(player, True, text="A") == ["visu", "grfe"]
        assert _opcodes(writer) == ["grfb", "visu", "grfe"]

        client2, writer2 = _client_with_writer()
        wiring2 = DisplayWiring(client2)
        assert await wiring2.on_connect(player, text="A") == ["visu", "grfe"]
        assert _opcodes(writer2) == ["grfb", "visu", "grfe"]

    asyncio.run(run())
