"""``/imageproxy/…`` — Perl's Slim::Web::ImageProxy (parity deviation D5).

Every expectation here is a Perl rule; the live Perl 9.1.1 baselines were read
read-only on 2026-09-14 (``curl`` against 192.168.1.90:9000):

=============================================  ======  ========================
request                                        Perl    header/body
=============================================  ======  ========================
``/imageproxy/test.jpg``                        200     ``image/png`` 16749 B,
                                                       ``Cache-Control: no-cache``
``/imageproxy/<tunein s111987q.png>/image.png`` 200     ``image/png`` 10497 B,
                                                       ``max-age=31536000``
``/imageproxy/<cdn url>/image.png``             301     ``Location: <url>``,
                                                       ``Content-Length: 0``
``/imageproxy/<cdn url>/40x40_m.png``           200     resized, ``max-age=…``
``/imageproxy/<cdn url>/40x40_m.jpg``           200     ``image/jpeg``
``/imageproxy/ftp://…/image.png``               200     placeholder,
                                                       ``no-cache``
=============================================  ======  ========================

**Bewusste Abweichung (Variante B, ``imageProxyFollowRedirects``, Vorbelegung
AN)**: der 301 der dritten Zeile geht nicht mehr an den Client, sondern der
Server holt das Bild selbst, verfolgt Weiterleitungen und antwortet 200.  Grund:
SqueezePlay folgt dem 301, hat aber kein TLS — der http→https-Sprung der
imgur-Logos (1.FM u. a.) scheitert, das große Logo fehlte.  Mit AUS
(``follow_switch("0")``) kommt Perls 301 exakt zurück; das prüft
:func:`test_switch_off_answers_perls_301`.

Sources: ``Slim/Web/ImageProxy.pm:111-236/238-261/263-295/297-360/362-371/
373-397/474-493``, ``Slim/Web/Graphics.pm:128-133/534-539``,
``Slim/Web/HTTP.pm:1011/1208-1212`` and
``Slim/Plugin/InternetRadio/TuneIn/Metadata.pm:44-62/507-559``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from lyrion.config import get_prefs
from lyrion.web import app as app_mod
from lyrion.web.api import _perl_proxied_image
from lyrion.web.app import (
    _imageproxy_get_right_size,
    _imageproxy_parse_spec,
    _imageproxy_spec,
    _imageproxy_tunein_artwork,
    _serve_imageproxy,
    _set_static_root,
)
from lyrion.web.settings import IMAGEPROXY_FOLLOW_REDIRECTS_PREF

REPO_HTML = Path(__file__).resolve().parents[1] / "html"


def _run(coro):
    return asyncio.run(coro)


def _png(size=(600, 600)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (12, 140, 210)).save(buf, format="PNG")
    return buf.getvalue()


def _content_size(data: bytes) -> tuple[int, int]:
    """Masse des *nicht* schwarzen Inhalts (Perls Polsterung ist opak schwarz)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        rgb = im.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    cols = [x for x in range(w) if any(px[x, y] != (0, 0, 0) for y in range(h))]
    rows = [y for y in range(h) if any(px[x, y] != (0, 0, 0) for x in range(w))]
    if not cols or not rows:
        return (0, 0)
    return (cols[-1] - cols[0] + 1, rows[-1] - rows[0] + 1)


class _Upstream(BaseHTTPRequestHandler):
    """Tiny HTTP source.

    ``/logo.png`` (200 PNG, 600x600), ``/logo160x85.png`` (200 PNG, nicht
    quadratisch — für die Polsterregel), ``/missing.png`` (404) und die
    Weiterleitungs-Pfade der Variante-B-Tests.  Jeder Treffer wird gezählt
    (``hits``), damit die Tests belegen können, dass *nicht* mehrfach geholt
    wird.
    """

    png = b""
    wide = b""
    hits: dict[str, int] = {}
    lock = threading.Lock()

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/plain",
              location: str | None = None) -> None:
        self.send_response(code)
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server API
        with type(self).lock:
            type(self).hits[self.path] = type(self).hits.get(self.path, 0) + 1
        port = self.server.server_address[1]
        if self.path.startswith("/logo.png"):
            self._send(200, self.png, "image/png")
        elif self.path.startswith("/logo160x85.png"):
            self._send(200, self.wide, "image/png")
        elif self.path.startswith("/redirect.png"):
            self._send(301, b"", location="/logo.png")
        elif self.path.startswith("/redirect-absolute.png"):
            self._send(302, b"", location=f"http://127.0.0.1:{port}/logo.png")
        elif self.path.startswith("/redirect-loopback.png"):
            self._send(301, b"", location=f"http://127.0.0.1:{port}/logo.png")
        elif self.path.startswith("/redirect-private.png"):
            self._send(301, b"", location="http://192.168.1.90/logo.png")
        elif self.path.startswith("/redirect-ftp.png"):
            self._send(301, b"", location="ftp://example.invalid/logo.png")
        elif self.path.startswith("/redirect-loop.png"):
            self._send(307, b"", location="/redirect-loop.png")
        elif self.path.startswith("/redirect-to-wide.png"):
            self._send(301, b"", location="/logo160x85.png")
        else:
            self._send(404, b"nope")


@pytest.fixture(scope="module")
def upstream():
    _Upstream.png = _png()
    _Upstream.wide = _png((160, 85))
    srv = HTTPServer(("127.0.0.1", 0), _Upstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _static_and_cache():
    _set_static_root(REPO_HTML)
    app_mod._imageproxy_cache.clear()
    app_mod._imageproxy_locks.clear()
    app_mod._imageproxy_negative.clear()
    _Upstream.hits.clear()
    yield
    _set_static_root(None)
    app_mod._imageproxy_cache.clear()
    app_mod._imageproxy_negative.clear()


@pytest.fixture
def follow_switch(monkeypatch):
    """Schalter ``imageProxyFollowRedirects`` setzen — ohne DB-Schreiben.

    Genau der Weg, den die Settings-Seite schreibt (``prefhash`` = ``_cache``
    des Stores, ``Slim/Utils/Prefs.pm`` ``set``); ``monkeypatch`` stellt den
    vorherigen Stand danach wieder her.  ``None`` = Pref fehlt (Default AN).
    """
    def _set(value: str | None) -> None:
        store = get_prefs()
        cache = dict(store._cache)
        if value is None:
            cache.pop(IMAGEPROXY_FOLLOW_REDIRECTS_PREF, None)
        else:
            cache[IMAGEPROXY_FOLLOW_REDIRECTS_PREF] = value
        monkeypatch.setattr(store, "_cache", cache, raising=False)

    return _set


@pytest.fixture
def local_targets_allowed(monkeypatch):
    """Netz-Schutz für die *Mechanik*-Tests neutralisieren.

    Ein Testziel kann nur auf ``127.0.0.1`` liegen, und
    ``_imageproxy_target_allowed`` lehnt lokale Ziele bewusst ab.  Die Regel
    selbst prüfen :func:`test_redirect_to_loopback_target_is_refused`,
    :func:`test_redirect_to_private_target_is_refused` und
    :func:`test_redirect_to_non_http_target_is_refused` mit der echten
    Funktion.
    """
    monkeypatch.setattr("lyrion.utils.network.is_private_addr",
                        lambda addr: False)


def _escaped(url: str) -> str:
    from urllib.parse import quote

    return quote(url, safe="-_.:")


def _proxied(url: str, spec: str) -> str:
    return f"/imageproxy/{_escaped(url)}/{spec}"


def test_spec_and_parse_spec():
    """``Graphics.pm:128-133`` + ``:534-539``."""
    assert _imageproxy_spec("/imageproxy/http%3A%2F%2Fh/x.png/image.png") == ".png"
    assert _imageproxy_spec("/imageproxy/http%3A%2F%2Fh/x.png/40x40_m.png") == \
        "40x40_m.png"
    assert _imageproxy_spec("/imageproxy/http%3A%2F%2Fh/x.png/100x80_4a5b6c.jpg") \
        == "100x80_4a5b6c.jpg"
    # Perl's spec regex only knows a lowercase "x" between the numbers, so the
    # "_bgcolor" part matches first for an uppercase spelling (same regex,
    # ``Graphics.pm:131``).
    assert _imageproxy_spec("/imageproxy/http%3A%2F%2Fh/x.png/100X80_4a5b6c.jpg") \
        == "_4a5b6c.jpg"
    assert _imageproxy_parse_spec("40x40_m.jpg") == ("40", "40", "m", "", "jpg")
    assert _imageproxy_parse_spec(".png") == ("", "", "", "", "png")
    assert _imageproxy_parse_spec("100x80_c_ffffff") == ("100", "80", "c",
                                                        "ffffff", "")


def test_get_right_size_picks_smallest_fitting():
    """``ImageProxy.pm:474-493`` with TuneIn's size map (``Metadata.pm:512-517``)."""
    sizes = {75: "t", 145: "q", 300: "d", 600: "g"}
    assert _imageproxy_get_right_size("45x45_m.png", sizes) == "t"
    assert _imageproxy_get_right_size("100x100_m.png", sizes) == "q"
    assert _imageproxy_get_right_size("300x300.png", sizes) == "d"
    assert _imageproxy_get_right_size("600x600.png", sizes) == "g"
    assert _imageproxy_get_right_size(".png", sizes) is None


def test_tunein_artwork_url_handler():
    """``TuneIn/Metadata.pm:521-559`` — the registered image handler."""
    # (:526-531) station logo shortcut => the 600x600 "giant" file
    assert _imageproxy_tunein_artwork(
        "http://cdn-radiotime-logos.tunein.com/s111987q.png", ".png"
    ) == "http://cdn-radiotime-logos.tunein.com/s111987g.png"
    # (:533-554) generic CDN name: smallest file that fits the spec
    assert _imageproxy_tunein_artwork(
        "http://xxx.cloudfront.net/293541660g.jpg", "40x40_m.jpg"
    ) == "http://xxx.cloudfront.net/293541660t.jpg"
    assert _imageproxy_tunein_artwork(
        "http://xxx.cloudfront.net/293541660g.jpg", "300x300_m.jpg"
    ) == "http://xxx.cloudfront.net/293541660d.jpg"
    # no spec => keep the largest ("we use either the min required, or the
    # maximum as defined above", :544-552)
    assert _imageproxy_tunein_artwork(
        "http://cdn-profiles.tunein.com/s20291/images/logoq.png", ".png"
    ) == "http://cdn-profiles.tunein.com/s20291/images/logog.png"


def test_proxied_image_roundtrip():
    """``proxiedImage`` (``ImageProxy.pm:407-425``) + the route's unescaping."""
    url = "http://cdn-radiotime-logos.tunein.com/s111987q.png"
    proxied = _perl_proxied_image(url)
    assert proxied == (
        "/imageproxy/http%3A%2F%2Fcdn-radiotime-logos.tunein.com%2F"
        "s111987q.png/image.png")
    # what the route extracts from that path is exactly the source URL again
    # (the route unescapes the path first, ``Slim/Utils/Misc.pm:345-353``)
    import re
    from urllib.parse import unquote

    decoded = unquote(proxied, encoding="utf-8", errors="replace")
    assert re.search(r"imageproxy/(.*)/[^/]*", decoded).group(1) == url


def test_switch_off_answers_perls_301(upstream, follow_switch):
    """``ImageProxy.pm:145-155`` — Schalter AUS → exakt Perls 301.

    Live Perl 9.1.1 (2026-09-14): ``301 Moved Permanently``, ``Location:
    http://192.168.1.90:9000/music/2/cover.jpg``, ``Content-Length: 0``,
    ``Content-Type: application/octet-stream`` (HTTP.pm:1011).  Mit
    ``imageProxyFollowRedirects=0`` (die bewusste Abweichung ist abschaltbar)
    muss genau das zurückkommen — inklusive: kein Netzaufruf.
    """
    follow_switch("0")
    target = f"{upstream}/logo.png"
    status, headers, body = _run(_serve_imageproxy(_proxied(target, "image.png")))
    assert status == 301
    assert headers["Location"] == target
    assert headers["Content-Type"] == "application/octet-stream"
    assert body == b""
    assert _Upstream.hits == {}          # der Server holt nichts


def test_switch_default_is_on_and_follows_the_redirect(upstream, follow_switch,
                                                       local_targets_allowed):
    """Bewusste Abweichung: ohne gesetzte Pref (Default AN) → 200 statt 301.

    Der Server holt das Bild selbst und folgt der Weiterleitung; die Bytes sind
    die der Quelle (``_resizeFromFile``: kein Resize ohne Größe im Spec).
    """
    follow_switch(None)                  # Pref fehlt = frischer Server
    target = f"{upstream}/redirect.png"
    status, headers, body = _run(_serve_imageproxy(_proxied(target, "image.png")))
    assert status == 200, headers
    assert headers["Content-Type"] == "image/png"
    assert headers["Cache-Control"] == f"max-age={app_mod._IMAGEPROXY_ONE_YEAR}"
    assert body == _Upstream.png
    assert _Upstream.hits == {"/redirect.png": 1, "/logo.png": 1}


def test_relative_and_absolute_redirect_targets_are_followed(
        upstream, follow_switch, local_targets_allowed):
    """Beide Schreibweisen der ``Location``-Kopfzeile (relativ und absolut)."""
    follow_switch("1")
    for path in ("/redirect.png", "/redirect-absolute.png"):
        target = f"{upstream}{path}"
        status, headers, body = _run(_serve_imageproxy(_proxied(target, "image.png")))
        assert status == 200, (path, headers)
        assert body == _Upstream.png


def test_followed_image_is_cached_and_not_fetched_twice(upstream, follow_switch,
                                                        local_targets_allowed):
    """Zweiter Abruf kommt aus dem Cache — kein weiterer Netzaufruf (``:126-134``)."""
    follow_switch("1")
    target = f"{upstream}/redirect.png"
    path = _proxied(target, "image.png")
    first = _run(_serve_imageproxy(path))
    second = _run(_serve_imageproxy(path))
    assert first[0] == second[0] == 200
    assert first[2] == second[2]
    assert second[1]["Cache-Control"] == f"max-age={app_mod._IMAGEPROXY_ONE_YEAR}"
    assert _Upstream.hits == {"/redirect.png": 1, "/logo.png": 1}


def test_size_variants_keep_the_padding_rule_through_a_redirect(
        upstream, follow_switch, local_targets_allowed):
    """``_40x40_m`` / ``_100x100_m`` unverändert: Perls Polsterregel gilt weiter.

    Live 9.1.1: ein 160x85-Logo wird für ``40x40_m`` auf **40x40 PNG** mit
    Inhalt 40x21 auf opakem Schwarz gepolstert (Bug 17140, ``GDResizer.pm:141-149``)
    — hier über eine Quelle, die erst per 301 erreichbar ist.
    """
    follow_switch("1")
    target = f"{upstream}/redirect-to-wide.png"
    status, headers, body = _run(_serve_imageproxy(_proxied(target, "image_40x40_m.png")))
    assert status == 200, headers
    assert headers["Content-Type"] == "image/png"
    from PIL import Image

    with Image.open(io.BytesIO(body)) as im:
        assert (im.width, im.height) == (40, 40)
    assert _content_size(body) == (40, 21)          # gepolstert, nicht verzerrt

    status, headers, body = _run(_serve_imageproxy(_proxied(target, "image_100x100_m.png")))
    assert status == 200
    with Image.open(io.BytesIO(body)) as im:
        assert (im.width, im.height) == (100, 100)
    assert _content_size(body) == (100, 53)         # 160x85 auf 100x53 skaliert


def test_dead_source_stays_placeholder_and_is_negatively_cached(
        upstream, follow_switch, caplog):
    """Tote Quelle: Platzhalter, ein einziger Netzaufruf, kurzer Negativ-Cache."""
    follow_switch("1")
    target = f"{upstream}/missing.png"
    path = _proxied(target, "image.png")
    with caplog.at_level(logging.INFO, logger="lyrion.web.app"):
        first = _run(_serve_imageproxy(path))
        second = _run(_serve_imageproxy(path))
        third = _run(_serve_imageproxy(path))
    assert first[0] == second[0] == third[0] == 200
    assert first[1]["Cache-Control"] == "no-cache"      # _artworkError
    assert first[2] == second[2] == third[2]
    assert _Upstream.hits == {"/missing.png": 1}        # kein Endlos-Retry
    assert app_mod._imageproxy_negative_get(target) is True
    assert any("Negativ-Cache" in r.getMessage() for r in caplog.records)


def test_negative_cache_covers_the_size_variants_too(upstream, follow_switch):
    """Auch die Größen-Varianten fragen eine tote Quelle nur einmal an."""
    follow_switch("1")
    target = f"{upstream}/missing.png"
    for _ in range(2):
        status, headers, _body = _run(
            _serve_imageproxy(_proxied(target, "40x40_m.png")))
        assert status == 200
        assert headers["Cache-Control"] == "no-cache"
    assert _Upstream.hits == {"/missing.png": 1}


def test_redirect_loop_stops_at_the_hop_limit(upstream, follow_switch,
                                              local_targets_allowed, caplog):
    """Endlos-Weiterleitung endet nach ``_IMAGEPROXY_FOLLOW_MAX_HOPS`` Sprüngen."""
    follow_switch("1")
    target = f"{upstream}/redirect-loop.png"
    with caplog.at_level(logging.WARNING, logger="lyrion.web.app"):
        status, headers, _body = _run(
            _serve_imageproxy(_proxied(target, "image.png")))
    assert status == 200
    assert headers["Cache-Control"] == "no-cache"       # Platzhalter
    assert _Upstream.hits == {
        "/redirect-loop.png": app_mod._IMAGEPROXY_FOLLOW_MAX_HOPS + 1}
    assert any("Sprungtiefe" in r.getMessage() for r in caplog.records)


def test_redirect_to_loopback_target_is_refused(upstream, follow_switch, caplog):
    """SSRF-Schutz: ein Ziel im Loopback wird nicht geholt (echte Regel)."""
    follow_switch("1")
    target = f"{upstream}/redirect-loopback.png"
    with caplog.at_level(logging.WARNING, logger="lyrion.web.app"):
        status, headers, body = _run(
            _serve_imageproxy(_proxied(target, "image.png")))
    assert status == 200
    assert headers["Cache-Control"] == "no-cache"
    assert body != _Upstream.png
    # nur die Start-URL wurde geholt, das Loopback-Ziel nicht
    assert _Upstream.hits == {"/redirect-loopback.png": 1}
    assert any("Weiterleitungsziel abgelehnt" in r.getMessage()
               for r in caplog.records)


def test_redirect_to_private_target_is_refused(upstream, follow_switch):
    """SSRF-Schutz: kein Ziel im lokalen Netz (192.168.1.90 = Live-Perl-LMS)."""
    follow_switch("1")
    target = f"{upstream}/redirect-private.png"
    status, headers, _body = _run(
        _serve_imageproxy(_proxied(target, "image.png")))
    assert status == 200
    assert headers["Cache-Control"] == "no-cache"
    assert _Upstream.hits == {"/redirect-private.png": 1}


def test_redirect_to_non_http_target_is_refused(upstream, follow_switch):
    """SSRF-Schutz: nur http/https (``ftp://…`` wird nicht verfolgt)."""
    follow_switch("1")
    target = f"{upstream}/redirect-ftp.png"
    status, headers, _body = _run(
        _serve_imageproxy(_proxied(target, "image.png")))
    assert status == 200
    assert headers["Cache-Control"] == "no-cache"
    assert _Upstream.hits == {"/redirect-ftp.png": 1}


def test_svg_source_keeps_perls_301(upstream, follow_switch):
    """``ImageProxy.pm:145`` — ein SVG bleibt beim 301 (auch mit Schalter AN).

    Diese Kette kann ein SVG nicht in ein Rasterbild wandeln; Perl schickt es
    dem Client, und genau das bleibt hier so.
    """
    follow_switch("1")
    target = f"{upstream}/logo.svg"
    status, headers, _body = _run(_serve_imageproxy(_proxied(target, "image.png")))
    assert status == 301
    assert headers["Location"] == target
    assert _Upstream.hits == {}


def test_fetches_and_resizes_like_perl(upstream):
    """``ImageProxy.pm:297-371`` — fetch, resize to the spec, one-year cache."""
    target = f"{upstream}/logo.png"
    status, headers, body = _run(_serve_imageproxy(
        f"/imageproxy/{_escaped(target)}/40x40_m.png"))
    assert status == 200, headers
    assert headers["Content-Type"] == "image/png"
    assert headers["Cache-Control"] == f"max-age={app_mod._IMAGEPROXY_ONE_YEAR}"
    assert "Expires" in headers
    from PIL import Image

    with Image.open(io.BytesIO(body)) as im:
        assert (im.width, im.height) == (40, 40)

    # the output format follows the spec extension (live Perl: 40x40.jpg for a
    # PNG source → 200 image/jpeg)
    status, headers, body = _run(_serve_imageproxy(
        f"/imageproxy/{_escaped(target)}/40x40_m.jpg"))
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    with Image.open(io.BytesIO(body)) as im:
        assert (im.width, im.height) == (40, 40)


def test_cache_replays_the_stored_bytes(upstream):
    """``ImageProxy.pm:126-134`` — a cache hit answers with ``_setHeaders``."""
    target = f"{upstream}/logo.png"
    path = f"/imageproxy/{_escaped(target)}/80x80_m.png"
    first = _run(_serve_imageproxy(path))
    second = _run(_serve_imageproxy(path))
    assert first[0] == second[0] == 200
    assert first[2] == second[2]
    assert second[1]["Cache-Control"] == \
        f"max-age={app_mod._IMAGEPROXY_ONE_YEAR}"


def test_placeholder_matches_perl_radio_png():
    """``ImageProxy.pm:373-397`` — the skin's ``html/images/radio.png``, 200.

    Live Perl answers ``/imageproxy/test.jpg`` with 200 (the error code is never
    set, ``# $response->code($code)`` :391), ``Cache-Control: no-cache``,
    ``Expires`` in the past and the 16749 B radio image.
    """
    status, headers, body = _run(_serve_imageproxy("/imageproxy/test.jpg"))
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert headers["Cache-Control"] == "no-cache"
    assert headers["Expires"]
    radio = (REPO_HTML / "EN" / "html" / "images" / "radio.png").read_bytes()
    assert body == radio


def test_placeholder_resized_to_the_spec():
    """``_artworkError`` resizes the placeholder via GDResizer (``:380-387``)."""
    from PIL import Image

    status, headers, body = _run(
        _serve_imageproxy("/imageproxy/http%3A%2F%2Fnope.invalid%2Fx.png/64x64.png"))
    assert status == 200
    assert headers["Cache-Control"] == "no-cache"
    with Image.open(io.BytesIO(body)) as im:
        assert (im.width, im.height) == (64, 64)


def test_scheme_guard_only_http_and_file():
    """``ImageProxy.pm:160-165`` — ``$url !~ /^(?:file|https?):/i`` → no fetch."""
    status, headers, _body = _run(_serve_imageproxy(
        "/imageproxy/ftp%3A%2F%2Ffoo%2Fa.png/image.png"))
    assert status == 200
    assert headers["Cache-Control"] == "no-cache"


def test_failed_fetch_answers_the_placeholder(upstream):
    """``ImageProxy.pm:263-295`` (``_gotArtworkError``) → ``_artworkError``."""
    target = f"{upstream}/missing.png"
    status, headers, body = _run(_serve_imageproxy(
        f"/imageproxy/{_escaped(target)}/40x40_m.png"))
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert headers["Cache-Control"] == "no-cache"
    from PIL import Image

    with Image.open(io.BytesIO(body)) as im:
        assert (im.width, im.height) == (40, 40)   # resized placeholder


def test_route_is_wired_into_the_asgi_app():
    """``Slim/Web/HTTP.pm:1208-1212`` routes ``imageproxy`` to the artwork code."""
    from lyrion.web.app import create_app

    app = create_app(static_dir=str(REPO_HTML))
    scope = {"type": "http", "method": "GET", "path": "/imageproxy/test.jpg",
             "headers": [], "query_string": b""}
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    _run(app(scope, receive, send))
    start = sent[0]
    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    assert (b"Content-Type", b"image/png") in start["headers"]
    assert sent[1]["body"]


def test_file_url_is_served_with_the_spec():
    """``ImageProxy.pm:208-211`` — ``file:`` URLs are read from disk, no fetch."""
    from PIL import Image

    src = REPO_HTML / "EN" / "html" / "images" / "radio.png"
    url = f"file://{src}"
    status, headers, body = _run(_serve_imageproxy(
        f"/imageproxy/{_escaped(url)}/48x48_m.png"))
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    with Image.open(io.BytesIO(body)) as im:
        assert (im.width, im.height) == (48, 48)
