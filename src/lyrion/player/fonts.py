"""Bitmap-Schriften + Text -> Bits (PROT-18 Nutzlasten für ``grfe``/``grfd``/``vfdc``).

Herkunft der Schriftdaten (alles aus dem Perl-Referenzbaum, ``/tmp/lms-ref``)
============================================================================
Die Schriftdaten sind **Datendateien**, kein PHP/Perl-Blob:
``Slim/Display/Lib/Fonts.pm:578`` sucht ``/<name>.font.bmp`` im
Graphics-Verzeichnis (``graphicsDirs`` :540-547 = ``Graphics``-Ordner des
Servers und der Plugins); ``fontfiles`` :561-608 sammelt sie, ``loadFonts``
:610-723 parst jede Datei mit ``parseBMP`` und ``parseFont``. Ein Zeichensatz
ist also eine 1bpp-, unkomprimierte Windows-BMP mit ``height+1`` Zeilen, deren
Spalten in Zeichengruppen zerfallen. Im Port liegen genau diese Dateien unter
``graphics/`` (dieselben Namen, Stand LMS), z. B.
``graphics/standard.2.font.bmp`` (3164x33x1) und ``graphics/medium.1.font.bmp``.

Geparster Aufbau (:726-762): Die unterste Bildzeile ist die Grundlinie und
dient nur der Trennung (``$bottomRow``, :733); alle Spalten, in denen dort ein
Pixel gesetzt ist, trennen Zeichen. Ein Zeichenblock ist eine Folge von
Pixelspalten von je ``height`` Bits (``$bottomIndex`` Zeilen, :747-753) →
``height/8`` Bytes pro Pixelspalte. Der Block mit Index 0 ist das
Zwischenraumzeichen ``$font->[0]``; hat er Pixel, gibt es keinen Zwischenraum
(``$fonttable->[0] = ''``, :756-759).

``string`` (:292-522) verkettet die Blöcke und schiebt nach jedem Zeichen den
Zwischenraum ein („inter character space“, :513-516). ``render_text`` gibt
diese Bits zurück — die Screengröße (``screenBytes``, ``Graphics.pm:99-103`` =
``bytesPerColumn * displayWidth``) gehört in ``Graphics.pm:398-414``: Zeile
plus Null-Padding bis zur Screengröße bzw. Abschneiden, wenn sie nicht passt.
Das macht ``render_text`` mit dem optionalen ``width``-Argument.

Nicht portiert
==============
Der TrueType-Pfad (``Fonts.pm:318-355``, ``Font::FreeType``) und die
hebräische BiDi-Umkehr (:349-354). Perl benutzt TTF nur für Codepoints > 255,
wenn ein Unicode-TTF im Graphics-Verzeichnis liegt (:207-232, :326); ohne ihn
fällt die Bitmap-Schleife wie in Perl auf ``'?'`` (``:489-491``) zurück.
``Slim::Utils::Unicode::utf8toLatin1Transliterate`` (:360) ist ebenfalls nicht
portiert; dieser Zweig wird deshalb nur für Zeichen < 256 durchlaufen, wo er
nichts ändert.

Abgrenzung
==========
Das ist NICHT der erfundene 5x7-Font aus ``lyrion/display/renderer.py`` — hier
werden die echten LMS-Schriftdateien geparst.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence, Union

logger = logging.getLogger(__name__)

# Fonts.pm:578 — ``$file =~ /[\..](.+?)\.font\.bmp\z/``: der Name der Schrift
# ist der Dateistamm, die Dateiendung ``.font.bmp`` ist fest.
FONT_SUFFIX = ".font.bmp"

# Fonts.pm:566-591 (graphicsDirs :540-547): der Graphics-Ordner des Servers.
# Im Port ist das ``<repo>/graphics`` (dort liegen die .font.bmp aus LMS).
_DEFAULT_GRAPHICS_DIR = Path(__file__).resolve().parents[3] / "graphics"


def graphics_dir() -> Path:
    """Graphics-Verzeichnis mit den ``*.font.bmp`` (Fonts.pm:540-547).

    ``LYRION_GRAPHICS_DIR`` überstimmt den Repo-Pfad (Tests/Betrieb).
    """
    override = os.environ.get("LYRION_GRAPHICS_DIR")
    if override:
        return Path(override)
    return _DEFAULT_GRAPHICS_DIR


@dataclass(frozen=True)
class Font:
    """Eine geparste Schrift (Perl ``$fonts->{$fontname}``, Fonts.pm:709).

    ``table[ord]`` ist der Bitblock des Zeichens: ``height/8`` Bytes je
    Pixelspalte, Zeilen von oben nach unten (``pack("B*", …)`` :754).
    ``table[0]`` ist das Zwischenraumzeichen (:310).
    """

    name: str
    table: tuple[bytes, ...]
    height: int          # Fonts.pm:696 ``$fonts->{height}` = biHeight - 1
    extent: int = 0      # Fonts.pm:274-290 ``loadExtent``

    @property
    def bytes_per_column(self) -> int:
        """Bytes je Pixelspalte = ``height/8`` (displayHeight/8 in Perl)."""
        return max(1, self.height // 8)

    @property
    def interspace(self) -> bytes:
        """``$defaultFont->[0]`` (Fonts.pm:310, :369)."""
        return self.table[0] if self.table else b""

    def __len__(self) -> int:
        return len(self.table)


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


def parse_bmp(data: bytes) -> tuple[list[list[int]], int]:
    """Port von ``Slim::Display::Lib::Fonts::parseBMP`` (Fonts.pm:767-843).

    Liest eine 1bpp-, unkomprimierte BMP in ein Zeilenraster aus 0/1
    (``$line[$j]`` = gesetztes Pixel), **von oben nach unten** (:839 kehrt die
    BMP-Zeilenreihenfolge um). Gibt ``(grid, biHeight)`` zurück; ``biHeight``
    ist die Perl-``$height`` (Fonts.pm:842), die Höhe der Schrift ist
    ``biHeight - 1`` (:696).
    """
    # unpack("a2 V xx xx V V V V v v V V xxxx xxxx xxxx xxxx V", $fontstring)
    # a2 + V = 6 Bytes, xx xx = 2, dann offset/biSize/biWidth/biHeight an
    # 10/14/18/22, planes/bitcount als u16 an 26/28, compression/sizeImage an
    # 30/34, 16 Bytes Palette (2 Einträge) und der erste Paletteneintrag an 54.
    if data[:2] != b"BM":
        raise ValueError("kein BMP-Kopf (Fonts.pm:780-784)")
    bi_width = _u32(data, 18)
    bi_height = _u32(data, 22)
    bi_planes = _u16(data, 26)
    bi_bit_count = _u16(data, 28)
    bi_compression = _u32(data, 30)
    offset = _u32(data, 10)
    first_palette_entry = _u32(data, 54)

    # Perl prüft fsize, planes, bitcount und compression (Fonts.pm:786-808)
    # und lehnt alles außer 1bpp/uncompressed ab.
    if _u32(data, 2) != len(data):
        raise ValueError("falsche Dateigröße (Fonts.pm:786-790)")
    if bi_planes != 1:
        raise ValueError(f"biPlanes={bi_planes} (Fonts.pm:792-796)")
    if bi_bit_count != 1:
        raise ValueError(f"biBitCount={bi_bit_count} (Fonts.pm:798-802)")
    if bi_compression != 0:
        raise ValueError(f"biCompression={bi_compression} (Fonts.pm:804-808)")

    # Fonts.pm:811-814 — Zeilen sind auf 32 Pixel aufgerundet.
    bits_per_line = bi_width - (bi_width % 32)
    if bi_width % 32:
        bits_per_line += 32
    bytes_per_line = bits_per_line // 8
    body = data[offset:]

    grid: list[list[int]] = [[] for _ in range(bi_height)]
    for i in range(bi_height):
        line_bytes = body[i * bytes_per_line:(i + 1) * bytes_per_line]
        if len(line_bytes) < bytes_per_line:
            line_bytes = line_bytes + b"\x00" * (bytes_per_line - len(line_bytes))
        bitstring = "".join(f"{b:08b}" for b in line_bytes)[:bi_width]
        # Fonts.pm:824-837 — normal palette ('1' = gesetzt) vs. reversed.
        # Perl: ``substr($bitstring,$j,1) ? 0 : 1`` — '0' ist in Perl falsch.
        if first_palette_entry == 0xFFFFFF:
            line = [1 if ch == "1" else 0 for ch in bitstring]
        else:
            line = [0 if ch == "1" else 1 for ch in bitstring]
        # :839 — $font[$biHeight-$i-1] = \@line (BMP ist von unten nach oben).
        grid[bi_height - i - 1] = line
    return grid, bi_height


def parse_font(grid: list[list[int]]) -> list[bytes]:
    """Port von ``Slim::Display::Lib::Fonts::parseFont`` (Fonts.pm:726-762).

    Zerlegt das Pixelraster in Zeichenblöcke. Die unterste Rasterzeile ist die
    Grundlinie (:731-735); jede Spalte mit gesetztem Pixel dort trennt Zeichen.
    """
    bottom_index = len(grid) - 1
    bottom_row = grid[bottom_index]
    width = len(bottom_row)

    table: list[bytes] = []
    char_index = -1
    i = 0
    while i < width:
        # Fonts.pm:741 ``next if ($bottomRow->[$i])`` — gesetzte Pixel in der
        # Grundlinie sind Trennspalten und werden übersprungen.
        if bottom_row[i]:
            i += 1
            continue
        char_index += 1
        column: list[int] = []
        # Perl: while (!$bottomRow->[$i]) { push rows 0..$bottomIndex-1; $i++ }
        while i < width and not bottom_row[i]:
            for j in range(bottom_index):
                row = grid[j]
                column.append(row[i] if i < len(row) else 0)
            i += 1
        table.append(_pack_bits(column))
        i += 1  # Perl: die for-Schleife erhöht $i nach dem Segment
        if char_index == 0 and _popcount(table[0]):
            # :756-759 — Pixel im Zwischenraumzeichen heißen: kein Zwischenraum.
            table[0] = b""
    return table


def _pack_bits(bits: list[int]) -> bytes:
    """``pack("B*", join('', @column))`` (Fonts.pm:754)."""
    value = 0
    for bit in bits:
        value = (value << 1) | (1 if bit else 0)
    pad = (-len(bits)) % 8
    return (value << pad).to_bytes((len(bits) + pad) // 8, "big")


def _popcount(data: bytes) -> int:
    """``unpack('%32b*', $bits)`` (Fonts.pm:756, :282)."""
    return sum(bin(byte).count("1") for byte in data)


def load_extent(font: "Font") -> int:
    """Port von ``loadExtent``/``extent`` (Fonts.pm:263-290).

    Zählt die gesetzten Pixel in ``string($fontname, chr(0x1f))`` (das Zeichen
    0x1f ist eine Bitmaske der gültigen Zeilen); Schriften der oberen Zeile
    (``.1``) werden negativ (:285).
    """
    extent = 0
    if len(font.table) >= 0x1F:
        extent = _popcount(font.table[0x1F])
    if ".1" in font.name:
        extent = -extent
    return extent


@lru_cache(maxsize=64)
def load_font(name: str, directory: Optional[str] = None) -> Font:
    """Lädt ``<name>.font.bmp`` und parst sie (Fonts.pm:566-723).

    ``name`` ist der Perl-Schriftname (``standard.2``, ``medium.1`` …), die
    Datei dazu ``<graphics>/<name>.font.bmp``.
    """
    base = Path(directory) if directory else graphics_dir()
    path = base / f"{name}{FONT_SUFFIX}"
    data = path.read_bytes()
    grid, bi_height = parse_bmp(data)
    table = tuple(parse_font(grid))
    font = Font(name=name, table=table, height=bi_height - 1)
    extent = load_extent(font)
    return Font(name=name, table=table, height=bi_height - 1, extent=extent)


def font_height(font_name: str) -> int:
    """``Slim::Display::Lib::Fonts::fontheight`` (Fonts.pm:248-252)."""
    try:
        return load_font(font_name).height
    except (OSError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Text -> Bits (Perl ``Slim::Display::Lib::Fonts::string``, Fonts.pm:292-522)
# ---------------------------------------------------------------------------


def _is_fast_path(text: str) -> bool:
    """``$string !~ /[^\\x00-\\x09|\\x0b-\\x1a\\|\\x1e-\\xff]/`` (Fonts.pm:307).

    Die Zeichenklasse endet bei ``\\xff``: alles ab 0x100 fällt durch (Perl
    zählt hier Codepoints), also geht z. B. ``€`` (:326-341) in den TTF-Zweig.
    """
    for ch in text:
        code = ord(ch)
        if 0x00 <= code <= 0x09 or 0x0B <= code <= 0x1A or 0x1E <= code <= 0xFF:
            continue
        return False
    return True


def _block(font: Font, code: int) -> bytes:
    """``$font->[$ord]`` — außerhalb der Tabelle leer (Perl: undef → '')."""
    if 0 <= code < len(font.table):
        return font.table[code]
    return b""


def _or(lhs: bytes, rhs: bytes) -> bytes:
    """Perl ``|=`` auf Strings (Fonts.pm:479, :503).

    Perl verodert byteweise bis zur Länge des längeren Operanden; fehlende
    Bytes zählen als 0.
    """
    size = max(len(lhs), len(rhs))
    left = lhs.ljust(size, b"\x00")
    right = rhs.ljust(size, b"\x00")
    return bytes(a | b for a, b in zip(left, right))


def render_text(
    text: str,
    font: Union[str, Font],
    width: Optional[int] = None,
) -> bytes:
    """Rendert Text in Display-Bits — Perl ``Fonts::string`` (Fonts.pm:292-522).

    Args:
        text: Der Text. Bytes 0x00-0xff werden als Zeichencodes benutzt
            (Perl-Schriften sind cp1252/latin1); die Steuerzeichen aus
            ``Fonts.pm:372-376`` wirken: ``\\x1b`` = Schriftwechsel
            (``\\x1b<name>\\x1b``), ``\\x1d`` = „tight“ (kein Zwischenraum),
            ``\\x1c`` = Zwischenraum wieder an, ``\\x0a`` = Cursorposition.
        font: Schriftname (``standard.2``) oder :class:`Font`.
        width: Optional die Breite **in Pixelspalten**. Damit wird das
            Ergebnis wie in ``Graphics.pm:398-414`` auf
            ``width * bytes_per_column`` Bytes gepolstert (mit Nullbytes) bzw.
            darauf abgeschnitten — die Screengröße aus ``Graphics.pm:99-103``.
            Ohne ``width`` kommen die rohen ``string``-Bits zurück.

    Returns:
        Die Bitmap-Bytes: je Pixelspalte ``height/8`` Bytes, Zeilen von oben
        nach unten.
    """
    if isinstance(font, str):
        font = load_font(font)
    if text is None:
        return b""

    interspace = font.interspace
    ords = [ord(ch) for ch in text]

    if _is_fast_path(text):
        # Fonts.pm:307-316
        bits = bytearray()
        remaining = len(ords)
        for code in ords:
            bits += _block(font, code)
            remaining -= 1
            if remaining:
                bits += interspace
        return _fit(bytes(bits), font, width)

    # Fonts.pm:364-519 — Sonderzeichen-Schleife.
    bits = bytearray()
    current = font
    inter = current.interspace
    tight = False
    font_change = False
    new_font_name = ""
    cursorpos = False
    remaining = len(ords)

    for code in ords:
        remaining -= 1
        if font_change:
            if code == 27:
                # :386-392 — Ende der Schriftdefinition.
                try:
                    current = load_font(new_font_name)
                except (OSError, ValueError):
                    current = font
                inter = b"" if tight else current.interspace
                font_change = False
                new_font_name = ""
            else:
                new_font_name += chr(code)
        elif code == 27:
            font_change = True                                     # :400-402
        elif code == 29:
            inter = b""                                            # :404-407
            tight = True
        elif code == 28:
            inter = current.interspace                             # :409-412
            tight = False
        elif code == 10:
            cursorpos = True                                       # :414-416
        else:
            if code > 255:
                code = 63                                          # :489-491 '?'
            if cursorpos:
                char_bits = _block(current, code)
                # :497-500 — schmale Zeichen verbreitern, damit der Cursor sichtbar ist.
                if len(char_bits) < 3 * len(inter):
                    char_bits = inter + char_bits + inter
                size = len(char_bits)
                # :503 — $char_bits |= substr($defaultFont->[10] x $len, 0, $len)
                char_bits = _or(char_bits, (font.table[10] if len(font.table) > 10 else b"") * size)[:size]
                bits += char_bits
                cursorpos = False
            else:
                bits += _block(current, code)                      # :510
            if remaining:
                bits += inter                                      # :513-516

    return _fit(bytes(bits), font, width)


def _fit(bits: bytes, font: Font, width: Optional[int]) -> bytes:
    """Screengröße anwenden — ``Graphics.pm:398-414`` + ``:99-103``.

    ``screenBytes = bytesPerColumn * displayWidth`` (:99-103). Passt die Zeile,
    wird mit Nullbytes auf ``screenBytes`` aufgefüllt (:400); passt sie nicht
    und es gibt keinen Overlay/Scroller, wird sie auf ``screenBytes``
    abgeschnitten (:409-414).
    """
    if width is None:
        return bits
    screensize = width * font.bytes_per_column
    if len(bits) >= screensize:
        return bits[:screensize]
    return bits + b"\x00" * (screensize - len(bits))


# ---------------------------------------------------------------------------
# Welche Schrift/welche Breite hat welche Display-Klasse?
# ---------------------------------------------------------------------------

# Default-Schriftfamilie je Display-Klasse: ``activeFont``/``idleFont`` mit
# ``activeFont_curr`` = Index in die Liste (Graphics.pm:792-800 nimmt
# ``$font = $prefs->get($prefname)->[$size]`` und schlägt sie in ``gfonthash``
# nach; die Familienliste ist das ``$fonts->{hash}`` aus dem Dateinamen,
# Fonts.pm:703-707 — ``fonthash->{standard}->{line}[0] = 'standard.1'``,
# ``->{line}[1] = 'standard.2'``).
#
#   Squeezebox2.pm:136-141  [light standard full], curr 1 -> standard
#   Boom.pm:126-131         [light_n standard_n full_n], curr 1 -> standard_n
#   Transporter.pm          erbt Squeezebox2 -> standard
#   SqueezeboxG.pm:46-51    [small medium large huge], curr 1 -> medium
# Die Zeilennummer im Namen (``.1``/``.2``/``.3``) ist die Displayzeile:
# Graphics.pm:284-285 rendert ``line[$l]`` mit ``$sfonts->{line}[$l]`` und
# setzt die Bits per ``|=`` zusammen (:400).
DEFAULT_FONT_FAMILY: dict[str, str] = {
    "Squeezebox2": "standard",
    "Boom": "standard_n",
    "Transporter": "standard",
    "SqueezeboxG": "medium",
}

# ``bytesPerColumn``: Squeezebox2.pm:180-182 = 4 (SB2/Boom/Transporter erben),
# SqueezeboxG.pm:119-121 = 2.
BYTES_PER_COLUMN: dict[str, int] = {
    "Squeezebox2": 4,
    "Boom": 4,
    "Transporter": 4,
    "SqueezeboxG": 2,
}

# ``displayHeight``: Squeezebox2.pm:184-186 = 32, SqueezeboxG.pm:123-125 = 16.
DISPLAY_HEIGHT: dict[str, int] = {
    "Squeezebox2": 32,
    "Boom": 32,
    "Transporter": 32,
    "SqueezeboxG": 16,
}

# Breiten je Visualizer-Modus — das ist ``displayWidth``:
#   Squeezebox2.pm:188-203 liefert ``modes->[$mode]{width}`` aus der Tabelle
#   :72-129; Boom.pm:26-104 (Breiten 160/145); SqueezeboxG.pm:127-129 = 280;
#   Transporter.pm:192-193 = ``widthOverride || 320``.
MODE_WIDTHS: dict[str, tuple[int, ...]] = {
    "Squeezebox2": (320, 320, 320, 278, 278, 278, 278, 278, 278, 320, 320, 320, 320, 320),
    "Boom": (160, 160, 160, 160, 145, 145, 160, 160, 160, 160, 160, 160),
    "Transporter": (320,) * 8,
    "SqueezeboxG": (280,) * 7,
}


def screen_width(display_class: str, mode: int = 0) -> int:
    """``displayWidth`` in Pixelspalten für einen Modus.

    ``mode`` ist die Modusnummer aus ``playingDisplayModes`` (Squeezebox2.pm:199);
    unbekannte Klassen/Modi fallen auf den ersten Eintrag zurück, wie Perl es
    mit ``modes->[$mode || 0]`` (:202) tut.
    """
    widths = MODE_WIDTHS.get(display_class)
    if not widths:
        return 0
    if not 0 <= mode < len(widths):
        return widths[0]
    return widths[mode]


def render_display_text(
    display_class: str,
    lines: Union[str, Sequence[str]],
    *,
    mode: int = 0,
    family: Optional[str] = None,
) -> bytes:
    """Fertige Screen-Bits für einen ``grfe``/``grfd``-Frame.

    Verbindet ``Fonts::string`` (:292-522) mit der Screengröße: je Zeile die
    Schrift ``<familie>.<l+1>`` (fonthash ``line[$l]``, Fonts.pm:703-707),
    Breite aus ``displayWidth`` (Modustabelle), Polsterung/Abschnitt aus
    ``Graphics.pm:398-414`` — die Zeilen werden per ``|=`` übereinandergelegt
    (:400), genau wie Perl die Screens zusammensetzt.

    Args:
        lines: Die Zeilen des Screens in Perl-Reihenfolge (``line[0]`` = obere
            Zeile, Schrift ``.1``). Ein einzelner String ist ``line[0]``. Die
            Auswahl „welche Zeile welchen Text bekommt“ macht in Perl das
            jeweilige Button-Modul (``Buttons/Playlist.pm:398-480``); das ist
            nicht Teil dieses Moduls.
        mode: Modusnummer für ``displayWidth`` (Squeezebox2.pm:199).
        family: Schriftfamilie überstimmen (Default: ``DEFAULT_FONT_FAMILY``).
    """
    family_name = family or DEFAULT_FONT_FAMILY.get(display_class)
    if not family_name:
        raise ValueError(f"keine Schriftfamilie für Display-Klasse {display_class!r}")
    if isinstance(lines, str):
        lines = [lines]
    screensize = screen_width(display_class, mode) * BYTES_PER_COLUMN.get(display_class, 0)
    bits = bytearray(screensize)
    for line_no, text in enumerate(lines):
        if text is None:
            continue
        font_name = f"{family_name}.{line_no + 1}"
        rendered = render_text(text, load_font(font_name), width=screen_width(display_class, mode))
        for i, byte in enumerate(rendered[:screensize]):
            bits[i] |= byte
    return bytes(bits)


# ===========================================================================
# Text-VFD-Encoder — die Nutzlast des ``vfdc``-Frames
# ===========================================================================
#
# Port von ``Slim::Display::Lib::TextVFD::vfdUpdate`` (:147-363). Der Frame
# selbst (``vfdc``, ``Player/Squeezebox.pm:495-502``) ist roher Byte-Strom:
# je Datenbyte ein Codebyte davor — ``$vfdCodeCmd`` = 0x02 für Kommandos,
# ``$vfdCodeChar`` = 0x03 für Zeichen (:29-37). Aufbau (:312-357):
# Brightness-Prelude (:317-321), ``$vfdReset`` = INCSC + HOME (:67), CFF
# (:334), Custom-Char-Definitionen (:323-330), der Zeichensatz-gewandelte Text
# (:337), der Zeilenumbruch auf die zweite Displayzeile über HOME2 (:340) und
# optional die Cursorposition (:344-355).
#
# Nicht portiert: die Custom-Charakter-Verwaltung (:103-110, :244-294,
# :323-330) samt der Bitmaps am Dateiende (:365-1141). Diese Blöcke braucht nur
# Text mit ``\x1F<name>\x1F``-Tokens, den ein reiner Titel nicht enthält;
# ein solcher Token löst :class:`UnsupportedVFDToken` aus, statt eine
# Zuordnung zu erfinden.

# TextVFD.pm:36-37 — die zwei Codebytes, die jedem Datenbyte vorangehen.
VFD_CODE_CMD = 0x02
VFD_CODE_CHAR = 0x03

# TextVFD.pm:41-47 — Kommandos (Nibble-Muster als Bytes).
VFD_COMMAND: dict[str, int] = {
    "CFF": 0b00001100,      # :41
    "CUR": 0b00001110,      # :42
    "HOME": 0b00000010,     # :44
    "HOME2": 0b11000000,    # :45
    "INCSC": 0b00000110,    # :47
}

# TextVFD.pm:49-53 (Noritake) und :55-59 (Futaba) — Helligkeit 0..4.
VFD_BRIGHT = (0b00000011, 0b00000011, 0b00000010, 0b00000001, 0b00000000)
VFD_BRIGHT_FUTABA = (0b00111011, 0b00111011, 0b00111010, 0b00111001, 0b00111000)

# TextVFD.pm:61-65 — ``$noritakeBrightPrelude`` (endet mit einem Zeichencode,
# damit der Helligkeitswert als Zeichenbyte folgt).
VFD_NORITAKE_PRELUDE = bytes([
    VFD_CODE_CMD, 0b00110011,
    VFD_CODE_CMD, 0b00000000,
    VFD_CODE_CMD, 0b00110000,
    VFD_CODE_CHAR,
])

# TextVFD.pm:67 — ``$vfdReset`` = INCSC + HOME.
VFD_RESET = bytes([VFD_CODE_CMD, VFD_COMMAND["INCSC"], VFD_CODE_CMD, VFD_COMMAND["HOME"]])

# TextVFD.pm:70-96 — ``%symbolmap`` je Sprache.
VFD_SYMBOLMAP: dict[str, dict[str, int]] = {
    "katakana": {"notesymbol": 0x0E, "rightarrow": 0x0F, "leftvbar": 0x10,
                 "rightvbar": 0x18, "hardspace": 0x20, "solidblock": 0x1F},
    "latin1": {"rightarrow": 0x1A, "hardspace": 0x20, "solidblock": 0x1F},
    "european": {"rightarrow": 0x7E, "hardspace": 0x20, "solidblock": 0x1F},
    "squeezeslave": {"rightarrow": 0x10, "hardspace": 0x20, "solidblock": 0x0B,
                     "notesymbol": 0x91, "bell": 0x98},
}

# TextVFD.pm:295-308 — die Zeichensatz-Transliterationen (``tr{}{}``). Die
# Tabellen sind eins zu eins aus dem Perl-Quelltext übernommen.
VFD_TRANSLATE: dict[str, tuple[bytes, bytes]] = {
    "european": (
        bytes([31, 146, 161, 162, 163, 164, 165, 166, 168, 169, 171, 173, 175,
               187, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202,
               203, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215,
               216, 217, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228,
               229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239, 240, 241,
               242, 243, 244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 254,
               255]),
        bytes([255, 39, 33, 99, 76, 111, 89, 124, 34, 99, 34, 45, 45, 34, 235,
               180, 179, 211, 178, 241, 243, 206, 201, 184, 183, 214, 247, 240,
               176, 208, 177, 203, 222, 175, 191, 223, 207, 239, 120, 48, 182,
               181, 244, 212, 89, 251, 226, 164, 163, 195, 162, 225, 195, 190,
               201, 168, 167, 198, 231, 224, 160, 192, 161, 171, 238, 175, 191,
               223, 207, 239, 47, 189, 166, 165, 228, 245, 172, 251, 204]),
    ),
    "katakana": (
        bytes([31, 146, 14, 15, 92, 112, 126, 127, 160, 161, 162, 163, 164, 165,
               166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178,
               179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 191,
               192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204,
               205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215, 216, 217,
               218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228, 229, 230,
               231, 232, 233, 234, 235, 236, 237, 238, 239, 240, 241, 242, 243,
               244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 254, 255]),
        bytes([255, 39, 25, 126, 140, 240, 142, 143, 32, 152, 236, 146, 235, 92,
               152, 143, 222, 99, 97, 60, 163, 45, 114, 176, 223, 183, 50, 51,
               96, 228, 241, 148, 44, 49, 223, 62, 37, 37, 37, 63, 129, 129,
               130, 130, 128, 129, 144, 153, 69, 69, 69, 69, 73, 73, 73, 73, 68,
               238, 79, 79, 79, 79, 134, 120, 48, 85, 85, 85, 138, 89, 112,
               226, 132, 131, 132, 132, 225, 132, 145, 153, 101, 101, 101, 101,
               105, 105, 105, 105, 149, 238, 111, 111, 111, 111, 239, 253, 136,
               117, 117, 117, 245, 121, 240, 121]),
    ),
    # TextVFD.pm:303-308 — latin1 und squeezeslave teilen sich diese Tabelle.
    "latin1": (bytes([0x92]), bytes([0x26])),
}
VFD_TRANSLATE["squeezeslave"] = VFD_TRANSLATE["latin1"]

# TextVFD.pm:182 — ``$Slim::Display::Text::commandmap{'cursorpos'}``
# (Text.pm:867-877) = "\x1ecursorpos\x1e".
VFD_CURSORPOS_TOKEN = b"\x1ecursorpos\x1e"

# Text.pm:36 — ``$defaultWidth = 40`` (``displayWidth``, Text.pm:80-83).
TEXT_DISPLAY_WIDTH = 40
# TextVFD.pm:30 — ``our $MAXBRIGHTNESS = 4``.
VFD_MAX_BRIGHTNESS = 4

# TextVFD.pm:359-362 — Perl wirft, wenn der Strom ungerade oder > 500 Bytes ist.
VFD_MAX_BYTES = 500


class UnsupportedVFDToken(Exception):
    """Ein Custom-Char-Token (``\\x1F<name>\\x1F``), den der Port nicht kennt.

    TextVFD.pm:218-234 bzw. :244-294 legen dafür ein Custom-Zeichen an; die
    Bitmaps dazu stehen in ``TextVFD.pm:365-1141`` und sind nicht portiert.
    """


def vfd_model_language(model: str) -> str:
    """``$lang`` aus ``$client->vfdmodel`` (TextVFD.pm:161-166).

    ``$lang =~ s/[^-]*-(.*)/$1/`` — alles nach dem ersten Bindestrich;
    ohne Bindestrich bleibt der Name stehen (``squeezeslave``). Leer ->
    ``katakana`` (:162-163).
    """
    if not model:
        return "katakana"
    return model.split("-", 1)[1] if "-" in model else model


def _translate_vfd(line: bytes, lang: str) -> bytes:
    """``$line =~ tr{}{}`` je Sprache (TextVFD.pm:295-308)."""
    table = VFD_TRANSLATE.get(lang)
    if not table:
        return line
    return line.translate(bytes.maketrans(table[0], table[1]))


def _vfd_escape(data: bytes) -> bytes:
    """``$line =~ s/(.)/$vfdCodeChar$1/gos`` (TextVFD.pm:337)."""
    out = bytearray()
    for byte in data:
        out.append(VFD_CODE_CHAR)
        out.append(byte)
    return bytes(out)


def vfd_update(
    line1: Optional[str],
    line2: Optional[str] = None,
    *,
    model: str = "noritake-latin1",
    width: int = TEXT_DISPLAY_WIDTH,
    brightness: Optional[int] = None,
) -> bytes:
    """Baut den ``vfdc``-Byte-Strom — Port von ``TextVFD::vfdUpdate`` (:147-363).

    Args:
        line1/line2: die zwei Displayzeilen. ``None`` -> eine Zeile voller
            Leerzeichen (:172-173); ``brightness == 0`` blendet beide aus
            (:175-178).
        model: ``$client->vfdmodel`` — bestimmt Sprache und Helligkeitskommando
            (``Text.pm:85-108``: ``noritake-*``/``futaba-*``/``squeezeslave``).
        width: ``$display->displayWidth`` — die Zeichenbreite (:157, :340).
        brightness: ``$client->brightness()`` (0..4, ``$MAXBRIGHTNESS`` :30).

    Returns:
        Der rohe VFD-Strom (``$client->vfd($vfddata)``, :357).
    """
    lang = vfd_model_language(model)
    spaces = " " * width
    line1 = spaces if line1 is None else line1
    line2 = spaces if line2 is None else line2
    if brightness == 0:
        line1 = spaces
        line2 = spaces

    line = bytearray()
    cur = -1
    index = 0
    for curline in (line1, line2):
        raw = curline.encode("latin-1", "replace")
        # :198-201 — nicht-latin1 wird transliteriert (Perl
        # utf8toLatin1Transliterate); im Port nur die Latin1-taugliche Variante.
        linepos = 0
        while linepos < len(raw):
            scan = raw[linepos:]
            if scan.startswith(VFD_CURSORPOS_TOKEN):
                cur = index                              # :213-216
                linepos += len(VFD_CURSORPOS_TOKEN)
                continue
            if scan[:1] == b"\x1f":
                end = scan.find(b"\x1f", 1)
                if end != -1:
                    name = scan[1:end].decode("latin-1")
                    code = VFD_SYMBOLMAP.get(lang, {}).get(name)
                    if code is None:
                        # :224-233/:244-294 — Custom-Zeichen, nicht portiert.
                        raise UnsupportedVFDToken(name)
                    line.append(code)                    # :222
                    linepos += end + 1
                    index += 1
                    continue
            line.append(raw[linepos])                    # :238
            linepos += 1
            index += 1

    vfddata = bytearray()
    if model.startswith("futaba"):                       # :317-318
        vfddata += bytes([VFD_CODE_CMD, VFD_BRIGHT_FUTABA[_bright_index(brightness)]])
    elif model != "squeezeslave":                        # :319-321
        vfddata += VFD_NORITAKE_PRELUDE
        vfddata += bytes([VFD_BRIGHT[_bright_index(brightness)]])

    vfddata += VFD_RESET                                 # :333
    vfddata += bytes([VFD_CODE_CMD, VFD_COMMAND["CFF"]])  # :334

    escaped = _vfd_escape(_translate_vfd(bytes(line), lang))  # :337
    # :340 — nach 2*displaywidth Zeichen auf die zweite Zeile umschalten.
    split_at = 2 * width
    escaped = (escaped[:split_at] + bytes([VFD_CODE_CMD, VFD_COMMAND["HOME2"]])
               + escaped[split_at:])
    vfddata += escaped                                   # :342

    if cur >= 0:                                         # :345-355
        if cur < width:
            pos = 0b10000000 + cur
        else:
            pos = 0b11000000 + cur - width
        vfddata += bytes([VFD_CODE_CMD, pos])
        vfddata += bytes([VFD_CODE_CMD, VFD_COMMAND["CUR"]])

    data = bytes(vfddata)
    if len(data) % 2:                                    # :359-361
        raise ValueError(f"ungerader vfd-Strom ({len(data)} Bytes)")
    if len(data) > VFD_MAX_BYTES:                        # :362
        raise ValueError(f"vfd-Strom zu lang ({len(data)} Bytes)")
    return data


def _bright_index(brightness: Optional[int]) -> int:
    """Perl ``$vfdBright[$brightness]`` — undef liest Index 0, sonst clamp."""
    if brightness is None:
        return 0
    return max(0, min(len(VFD_BRIGHT) - 1, int(brightness)))


__all__ = [
    "BYTES_PER_COLUMN",
    "DEFAULT_FONT_FAMILY",
    "DISPLAY_HEIGHT",
    "FONT_SUFFIX",
    "MODE_WIDTHS",
    "TEXT_DISPLAY_WIDTH",
    "UnsupportedVFDToken",
    "VFD_MAX_BYTES",
    "VFD_SYMBOLMAP",
    "Font",
    "font_height",
    "graphics_dir",
    "load_extent",
    "load_font",
    "parse_bmp",
    "parse_font",
    "render_display_text",
    "render_text",
    "screen_width",
    "vfd_model_language",
    "vfd_update",
]
