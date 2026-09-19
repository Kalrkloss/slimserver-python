"""Bildpolsterung: nicht-quadratisches Artwork wird auf die Box gepolstert.

Perl liefert für ``cover_40x40_m`` / ``/imageproxy/…/100x100_m`` **genau die
angeforderte Box**, nicht das eingepasste Bild: ``Slim/Utils/GDResizer.pm:166-171``
ruft ``$im->resize({width, height, bgcolor => hex($bgcolor), keep_aspect => 1})``,
und ``Image::Scale`` passt das Bild ein, zentriert es und füllt den Rest mit der
Hintergrundfarbe.  ``bgcolor`` wird von ``Slim/Web/Graphics.pm:227-234`` gar
nicht übergeben, bleibt also undef — ``hex(undef)`` ist ``0``, d. h. **opak
schwarz**.

Alle Sollwerte hier sind am laufenden Perl 9.1.1 abgelesen (nur lesend,
2026-09-19, eigener HTTP-Server mit Testbildern, ``/imageproxy/<160x85>/<spec>``)::

    Quelle   spec        Perl-Antwort
    -------  ----------  -----------------------------------------------
    160x85   40x40_m     40x40 PNG, Inhalt 40x21, oben 9 / unten 10 schwarz
    160x85   100x100_m   100x100 PNG, Inhalt 100x53, oben 23 / unten 24
    160x85   200x200_m   85x85 (kein Hochskalieren, :152-162)
    160x85   300x300_m   85x85 (dieselbe Klammer)
    160x85   40x40_o     40x21 JPEG — Modus 'o' polstert nicht (:196-211)
    160x85   40x40_f     40x21 JPEG (auch 'f'/'c' fallen in den else-Zweig)
    160x85   40x40_F     40x40 JPEG — polstert, aber bleibt JPEG (:174-194)
    300x300  200x200_m   200x200 JPEG (quadratisch → keine Polsterung)
    600x600  40x40_m     40x40 JPEG
    40x40    200x200_m   40x40 (Klammer greift)
    1x1      200x200_m   1x1   (Klammer greift)
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from lyrion.media.artwork import (
    PAD_RGB,
    ArtworkHandler,
    fit_and_pad,
    gd_effective_box,
)
from lyrion.web.app import (
    _gdresize,
    _imageproxy_resize,
    _parse_cover_spec,
    _resize_cover,
)


def _png(width: int, height: int) -> bytes:
    """Testbild wie am Perl-Server: vier Quadranten, damit die Lage im
    gepolsterten Rahmen prüfbar ist."""
    im = Image.new("RGB", (width, height), (0, 0, 200))
    px = im.load()
    for y in range(height):
        for x in range(width):
            if x < width // 2 and y < height // 2:
                px[x, y] = (255, 0, 0)
            elif x >= width // 2 and y >= height // 2:
                px[x, y] = (0, 180, 0)
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def _jpeg(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(out, format="JPEG")
    return out.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _black_borders(data: bytes) -> tuple[tuple[int, int], tuple[int, int]]:
    """(Spalten links/rechts, Zeilen oben/unten) der schwarzen Polsterung."""
    im = _open(data)
    w, h = im.size
    px = im.load()

    def black(point) -> bool:
        return px[point] == (0, 0, 0, 255)

    top = 0
    while top < h and all(black((x, top)) for x in range(w)):
        top += 1
    bottom = 0
    while bottom < h and all(black((x, h - 1 - bottom)) for x in range(w)):
        bottom += 1
    left = 0
    while left < w and all(black((left, y)) for y in range(top, h - bottom)):
        left += 1
    right = 0
    while right < w and all(black((w - 1 - right, y)) for y in range(top, h - bottom)):
        right += 1
    return (left, right), (top, bottom)


# ---------------------------------------------------------------------------
# Die Rechenregeln selbst (Perl-Zitat je Funktion in artwork.py)
# ---------------------------------------------------------------------------

def test_box_is_pulled_back_to_the_original_long_edge():
    """``GDResizer.pm:152-162`` — nicht hochskalieren, Box auf die lange Kante."""
    assert gd_effective_box(160, 85, 40, 40) == (40, 40)
    assert gd_effective_box(160, 85, 100, 100) == (100, 100)
    # beide Wunschmaße größer: Querformat → Breite = req_w * (in_h / req_h),
    # Höhe = in_h … Perl liefert live 85x85
    assert gd_effective_box(160, 85, 200, 200) == (85, 85)
    assert gd_effective_box(160, 85, 300, 300) == (85, 85)
    # Hochformat → Höhe = req_h * (in_w / req_w), Breite = in_w
    assert gd_effective_box(85, 160, 200, 200) == (85, 85)
    assert gd_effective_box(40, 40, 200, 200) == (40, 40)
    assert gd_effective_box(1, 1, 200, 200) == (1, 1)
    # nur ein Maß zu groß → keine Klammer (Perl prüft mit &&)
    assert gd_effective_box(160, 85, 200, 40) == (200, 40)


def test_fit_and_pad_fills_the_box_and_centres_floorwise():
    im = Image.new("RGB", (160, 85), (255, 0, 0))
    out = fit_and_pad(im, 40, 40)
    assert out.size == (40, 40)
    assert out.getpixel((0, 0)) == PAD_RGB == (0, 0, 0)
    (left, right), (top, bottom) = _black_borders(_to_png(out))
    assert (left, right) == (0, 0)
    assert (top, bottom) == (9, 10)      # Perl live: oben 9, unten 10


def _to_png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _gdresize = Perl GDResizer->resize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("box", [(40, 40), (100, 100)])
def test_nonsquare_is_padded_to_the_exact_box(box):
    """160x85 → exakt die Box, Inhalt zentriert, Rand opak schwarz, PNG."""
    out, fmt = _gdresize(_png(160, 85), box[0], box[1], mode="m")
    im = _open(out)
    assert im.size == box
    assert fmt == "png"                       # Bug 17140, GDResizer.pm:141-149
    assert im.getpixel((0, 0)) == (0, 0, 0, 255)
    assert im.getpixel((box[0] - 1, box[1] - 1)) == (0, 0, 0, 255)
    # Inhalt = eingepasste Größe, zentriert (floor), wie Perl live
    (left, right), (top, bottom) = _black_borders(out)
    assert (left, right) == (0, 0)
    assert top + bottom == box[1] - round(85 * (box[0] / 160))
    assert abs(top - bottom) <= 1


def test_200x200_of_a_160x85_logo_is_85x85_like_perl():
    """Perl live: ``200x200_m`` → 85x85 (und ``300x300_m`` genauso)."""
    out, fmt = _gdresize(_png(160, 85), 200, 200, mode="m")
    assert _open(out).size == (85, 85)
    assert fmt == "png"
    out300, _ = _gdresize(_png(160, 85), 300, 300, mode="m")
    assert _open(out300).size == (85, 85)


def test_portrait_logo_is_padded_the_same_way():
    out, fmt = _gdresize(_png(85, 160), 40, 40, mode="m")
    assert _open(out).size == (40, 40)
    assert fmt == "png"
    assert _open(out).getpixel((0, 0)) == (0, 0, 0, 255)
    assert _open(out).getpixel((39, 0)) == (0, 0, 0, 255)
    (left, right), (top, bottom) = _black_borders(out)
    assert top + bottom == 0
    assert abs(left - right) <= 1


def test_square_sources_stay_unpadded():
    """Quadratische Quelle → keine Polsterung, also auch kein PNG-Zwang.

    Live 9.1.1: ``/imageproxy/<300x300>/40x40_m.jpg`` → 200 **image/jpeg**
    (die angeforderte Endung gewinnt, wenn nicht gepolstert wird).
    """
    for src, box in (((300, 300), (200, 200)), ((600, 600), (40, 40))):
        out, fmt = _gdresize(_png(*src), box[0], box[1], mode="m", fmt="jpg")
        assert _open(out).size == box
        assert fmt == "jpg"
        (left, right), (top, bottom) = _black_borders(out)
        assert (left, right) == (top, bottom) == (0, 0)
    # ohne Angabe: PNG-Quelle bleibt PNG (``:124-131`` — nur jpg bleibt jpg)
    _out, fmt_no_ext = _gdresize(_png(300, 300), 200, 200, mode="m")
    assert fmt_no_ext == "png"


def test_small_square_source_is_not_upscaled():
    """Perl live: 40x40-Quelle @ 200x200_m → 40x40 (Klammer ``:152-162``).

    Die Bytes bleiben dabei die der Quelle (dieses Ports Zusicherung: was
    schon die Box *ist*, wird nicht neu kodiert) — Perl würde neu kodieren.
    """
    data = _png(40, 40)
    out, fmt = _gdresize(data, 200, 200, mode="m", fmt="jpg")
    assert _open(out).size == (40, 40)
    assert out == data
    assert fmt == "png"                     # Label passt zu den Bytes


def test_one_by_one_source_collapses():
    out, _ = _gdresize(_png(1, 1), 200, 200, mode="m")
    assert _open(out).size == (1, 1)            # Perl live: 1x1


@pytest.mark.parametrize("mode", ["m", "p", "", None])
def test_modes_that_pad(mode):
    """``m``/``p`` und der Default (``GDResizer.pm:108-111``) polstern."""
    out, fmt = _gdresize(_png(160, 85), 40, 40, mode=mode or "")
    assert _open(out).size == (40, 40)
    assert fmt == "png"


@pytest.mark.parametrize("mode", ["o", "f", "c"])
def test_modes_that_do_not_pad(mode):
    """Alles außer m/p/F läuft in Perls else-Zweig: nur die Breite
    (``GDResizer.pm:196-211``) — live: 40x40_f/c/o → 40x21 JPEG."""
    out, fmt = _gdresize(_png(160, 85), 40, 40, mode=mode, fmt="jpg")
    assert _open(out).size == (40, 21)
    assert fmt == "jpg"


def test_mode_F_pads_but_keeps_jpeg():
    """``GDResizer.pm:174-194`` — live: 40x40_F → 40x40 JPEG, nicht PNG."""
    out, fmt = _gdresize(_png(160, 85), 40, 40, mode="F", fmt="jpg")
    assert _open(out).size == (40, 40)
    assert fmt == "jpg"


def test_explicit_extension_does_not_win_against_padding():
    """Live 9.1.1 liefert auch bei ``40x40_m.jpg`` ein PNG, sobald gepolstert
    wird (160x85-Quelle), bei quadratischer Quelle dagegen JPEG."""
    padded, padded_fmt = _gdresize(_png(160, 85), 40, 40, mode="m", fmt="jpg")
    assert padded_fmt == "png"
    square, square_fmt = _gdresize(_png(600, 600), 40, 40, mode="m", fmt="jpg")
    assert square_fmt == "jpg"


def test_garbage_input_is_not_an_image():
    assert _gdresize(b"not an image", 40, 40, mode="m") is None


# ---------------------------------------------------------------------------
# _resize_cover / _imageproxy_resize — die Wege, die der Server geht
# ---------------------------------------------------------------------------

def test_resize_cover_pads_a_nonsquare_cover():
    data = _resize_cover(_png(160, 85), (40, 40))
    im = Image.open(io.BytesIO(data))
    assert im.size == (40, 40)                  # vorher: 40x21
    assert im.format == "PNG"


def test_resize_cover_still_hands_back_untouched_bytes():
    original = _jpeg(40, 40)
    assert _resize_cover(original, (40, 40)) == original
    assert _resize_cover(b"not an image", (40, 40)) == b"not an image"


def test_imageproxy_resize_pads_like_perl():
    body, fmt = _imageproxy_resize(_png(160, 85), "40x40_m.jpg", "image/png")
    assert fmt == "png"
    assert Image.open(io.BytesIO(body)).size == (40, 40)
    # Modus 'o' bleibt ungepolstert
    body_o, fmt_o = _imageproxy_resize(_png(160, 85), "40x40_o.jpg", "image/png")
    assert fmt_o == "jpg"
    assert Image.open(io.BytesIO(body_o)).size == (40, 21)


def test_cover_spec_exposes_mode_and_extension():
    assert _parse_cover_spec("/music/7/cover_40x40_m.jpg") == (7, (40, 40), "m", "jpg")
    assert _parse_cover_spec("/music/7/cover_40x40_o") == (7, (40, 40), "o", "")
    assert _parse_cover_spec("/music/7/cover_40x40") == (7, (40, 40), "", "")
    assert _parse_cover_spec("/music/7/cover.jpg") == (7, None, "", "")
    assert _parse_cover_spec("/music/7/cover_40x40_m.gif") is None


# ---------------------------------------------------------------------------
# Die gecachten Varianten in media/artwork.py
# ---------------------------------------------------------------------------

def test_cached_sizes_are_padded_to_the_box(tmp_path):
    """Die gecachten Größen sind die *Box* — mit Perls Klammer (``:152-162``).

    Für 160x85 liefert ''200x200_m'' live 85x85, also ist der 200er-Eintrag
    eine 85x85-Datei (der Schlüssel bleibt die angeforderte Box, genau wie
    Perls Artwork-Cache den Spec ``200x200_m`` ablegt).
    """
    handler = ArtworkHandler(cache_dir=tmp_path / "cache", db_path=tmp_path / "db.sqlite")
    src = tmp_path / "logo.png"
    src.write_bytes(_png(160, 85))
    sizes = asyncio.run(handler.ensure_sizes(src, [40, 100, 200]))
    assert sorted(sizes) == [(40, 40), (100, 100), (200, 200)]
    expected = {(40, 40): (40, 40), (100, 100): (100, 100), (200, 200): (85, 85)}
    for box, path in sizes.items():
        with Image.open(path) as im:
            assert im.size == expected[box], (box, im.size)
            assert im.convert("RGB").getpixel((0, 0)) == PAD_RGB


def test_cached_square_cover_is_unpadded(tmp_path):
    handler = ArtworkHandler(cache_dir=tmp_path / "cache2", db_path=tmp_path / "db2.sqlite")
    src = tmp_path / "cover.png"
    src.write_bytes(_png(600, 600))
    sizes = asyncio.run(handler.ensure_sizes(src, [40, 200]))
    for box, path in sizes.items():
        with Image.open(path) as im:
            assert im.size == box
            assert im.convert("RGB").getpixel((0, 0)) != PAD_RGB
