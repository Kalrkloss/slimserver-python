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

Sources: ``Slim/Web/ImageProxy.pm:111-236/238-261/263-295/297-360/362-371/
373-397/474-493``, ``Slim/Web/Graphics.pm:128-133/534-539``,
``Slim/Web/HTTP.pm:1011/1208-1212`` and
``Slim/Plugin/InternetRadio/TuneIn/Metadata.pm:44-62/507-559``.
"""

from __future__ import annotations

import asyncio
import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

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

REPO_HTML = Path(__file__).resolve().parents[1] / "html"


def _run(coro):
    return asyncio.run(coro)


def _png(size=(600, 600)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (12, 140, 210)).save(buf, format="PNG")
    return buf.getvalue()


class _Upstream(BaseHTTPRequestHandler):
    """Tiny HTTP source: ``/logo.png`` (200 PNG), ``/missing.png`` (404)."""

    png = b""

    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.startswith("/logo.png"):
            body = self.png
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"nope")


@pytest.fixture(scope="module")
def upstream():
    _Upstream.png = _png()
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
    yield
    _set_static_root(None)
    app_mod._imageproxy_cache.clear()


def _escaped(url: str) -> str:
    from urllib.parse import quote

    return quote(url, safe="-_.:")


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


def test_redirect_rule_for_plain_extension(upstream):
    """``ImageProxy.pm:145-155`` — no resize asked → 301 to the original URI.

    Live Perl 9.1.1 (2026-09-14): ``301 Moved Permanently``, ``Location:
    http://192.168.1.90:9000/music/2/cover.jpg``, ``Content-Length: 0``,
    ``Content-Type: application/octet-stream`` (HTTP.pm:1011).
    """
    target = f"{upstream}/logo.png"
    status, headers, body = _run(_serve_imageproxy(
        f"/imageproxy/{_escaped(target)}/image.png"))
    assert status == 301
    assert headers["Location"] == target
    assert headers["Content-Type"] == "application/octet-stream"
    assert body == b""


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
