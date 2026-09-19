"""Cover-Endpunkt: /music/current/... und der generische Platzhalter.

Gemeldetes Symptom (User, 2026-09-12): „Albencover auf der Now Playing Seite
der server web gui werden nicht aktualisiert, bzw nicht auf das generische
Symbol gesetzt, wenn das Lied kein cover hat."

Zwei belegte Ursachen:

1. Material Skin holt das Now-Playing-Cover als
   ``/music/current/cover.jpg?player=<mac>&album_id=<id>``
   (``html/material/html/js/currentcover.js:102``, dort extra der Hinweis,
   dass die URL pro Titel wechseln MUSS). Perl löst das in
   ``Slim/Web/Graphics.pm:155-172`` über die coverid des laufenden Tracks auf.
   Wir hatten für ``current`` nur den Ziffern-Regex → 404 → der Browser behielt
   das vorige Cover.
2. Für Alben ohne Bild liefert Perl NICHT 404, sondern den generischen
   Platzhalter ``html/images/cover.png`` mit 200 (live gemessen 2026-09-19:
   ``/music/2/cover.jpg`` → 200 image/png 13113 B, 512x512;
   ``/music/2/cover_40x40_m`` und ``…/cover_40x40_m.jpg`` → 200 image/png
   1619 B, 40x40 — die bemessene Form ist ein PNG, ``Graphics.pm:286-287``
   biegt dafür die Endung um). Dieselbe Datei ist auch der
   Icon-Fallback in Perl (``XMLBrowser.pm:1603``, ``Commands.pm:2151``).
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from lyrion.web.app import (
    _parse_cover_path,
    _placeholder_cover,
    _remote_cover,
    _set_static_root,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_current_path_is_accepted_in_both_forms():
    # Material: cover.jpg + cover_<size>_<crop>
    assert _parse_cover_path("/music/current/cover.jpg") == ("current", None)
    assert _parse_cover_path("/music/current/cover.png") == ("current", None)
    assert _parse_cover_path("/music/current/cover_40x40_m.jpg") == ("current", (40, 40))
    assert _parse_cover_path("/music/current/cover_40x40_m") == ("current", (40, 40))


def test_numeric_paths_still_work():
    assert _parse_cover_path("/music/2441/cover.jpg") == (2441, None)
    assert _parse_cover_path("/music/2441/cover_40x40_m") == (2441, (40, 40))


def test_invalid_paths_are_rejected():
    assert _parse_cover_path("/music/current/foo.jpg") is None
    assert _parse_cover_path("/music/abc/cover.jpg") is None


def test_negative_remote_ids_are_accepted():
    """A negative id is a remote track (``Schema/RemoteTrack.pm:317``).

    Perl answers ``/music/<negative id>/cover.jpg`` with the skin's
    ``html/images/radio.png`` — live 9.1.1, read-only 2026-09-19:
    ``/music/-94115161401160/cover.jpg`` → 200 image/png 16749 B,
    ``…/cover_40x40_m.jpg`` → 200 image/png 1961 B.  Our regex only knew
    ``\\d+``/``current``, so every remote id 404'd and a logo-less radio
    stream had no default logo.
    """
    assert _parse_cover_path("/music/-94115161401160/cover.jpg") == \
        (-94115161401160, None)
    assert _parse_cover_path("/music/-94115161401160/cover_40x40_m.jpg") == \
        (-94115161401160, (40, 40))
    assert _parse_cover_path("/music/-170263893352417/cover_240x240_m") == \
        (-170263893352417, (240, 240))


#: the skin file, as the running server resolves it (``html/`` IS the static
#: root, ``html/EN/html/images/radio.png`` the skin fallback).
RADIO_PNG = REPO_ROOT / "html" / "EN" / "html" / "images" / "radio.png"


def test_remote_cover_is_perls_radio_png():
    """The remote default is ``html/images/radio.png`` — Perl's exact bytes."""
    _set_static_root(REPO_ROOT / "html")
    assert RADIO_PNG.stat().st_size == 16749        # live Perl: same file
    res = _remote_cover()
    assert res is not None
    data, mime = res
    assert data[:8] == b"\x89PNG\r\n\x1a\n"        # real PNG
    assert mime == "image/png"
    assert data == RADIO_PNG.read_bytes()


def test_sized_remote_cover_is_png_like_perl():
    """``cover_40x40_m.jpg`` on a remote id → a 40x40 **PNG** (Perl: image/png)."""
    _set_static_root(REPO_ROOT / "html")
    res = _remote_cover((40, 40))
    assert res is not None
    data, mime = res
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) < RADIO_PNG.stat().st_size


def test_placeholder_is_perls_cover_png():
    _set_static_root(REPO_ROOT)
    res = _placeholder_cover()
    assert res is not None
    data, mime = res
    assert data[:8] == b"\x89PNG\r\n\x1a\n"        # echtes PNG
    assert mime == "image/png"
    # identisch mit der Datei des Perl-LMS (13113 B, sha256 218750bf…)
    assert data == (REPO_ROOT / "html" / "images" / "cover.png").read_bytes()


def test_sized_placeholder_is_png_like_perl():
    """Die bemessene Form ist ein PNG — Perl biegt die Endung dafür um.

    ``Slim/Web/Graphics.pm:286-287`` (Zweig "kein Cover"): ``$path =
    'html/images/cover_' . $spec`` und ``$spec =~ s/\\.\\w+$/.png/`` — "our
    default artwork are PNG files".  Live 9.1.1, nur lesend 2026-09-19::

        /music/2/cover_40x40_m       -> 200 image/png 1619 B, 40x40
        /music/2/cover_40x40_m.jpg   -> 200 image/png 1619 B, 40x40
        /music/2/cover.jpg           -> 200 image/png 13113 B, 512x512

    (Ein früherer Stand dieses Tests erwartete hier image/jpeg mit 1403 B;
    das lässt sich am laufenden Server nicht mehr reproduzieren.)
    """
    _set_static_root(REPO_ROOT)
    data, mime = _placeholder_cover((40, 40))
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) < 13113
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (40, 40)          # genau die Box
        # cover.png ist quadratisch → keine Polsterung, keine schwarze Ecke
        assert im.convert("RGBA").getpixel((0, 0)) != (0, 0, 0, 255)


def test_placeholder_missing_returns_none(tmp_path):
    (tmp_path / "html" / "images").mkdir(parents=True)
    _set_static_root(tmp_path)
    assert _placeholder_cover() is None
