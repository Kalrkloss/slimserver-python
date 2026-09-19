"""Frame-Antworten wie Perl: ``Content-Length`` statt ``chunked``.

Gemeldetes Symptom (User, 2026-09-19): „Squeezer zeigt jetzt small UND large
artwork von Radiosendern, **SqueezePlay zeigt gar nichts**."

Beleg (rohe Client-Zeilen, SqueezePlay 9.0.0 r1583 mit eigenem Debug-Log,
2026-09-19, gegen diesen Server):

    SlimServer.lua:1131 fetchArtwork(/imageproxy/http%3A%2F%2Fcdn-profiles\
.tunein.com%2Fs355203%2Fimages%2Flogoq.jpg%3Ft%3D1/image.jpg, 40, nil)
    SlimServer.lua:1227 fetchArtwork(…/image.jpg => …/image_40x40_m.jpg)

Die angefragte URL ist also ``/imageproxy/…/image_40x40_m.jpg`` — **ohne**
Sprachpraefix (kein ``/html/EN/imageproxy/…``), Zeichen fuer Zeichen die Form,
die ``proxiedImage`` (``Slim/Web/ImageProxy.pm:443-461``) liefert.  Trotzdem
blieben die Logos weg: der Client hat die Anfrage nie gesendet, weil seine
Artwork-Queue verklemmte.  Ursache war unser Framing — für **jede** Antwort
``Transfer-Encoding: chunked`` (uvicorn-Standard ohne ``Content-Length``),
während Perl fuer jede endliche Antwort eine Laenge schickt (live 9.1.1:
``/html/images/radio.png`` → ``Content-Length: 16749``,
``/imageproxy/…/image_40x40_m.png`` → 2355, 404-Seite → 97 B ``text/html``).

``jive/net/SocketHttp.lua:777`` schaltet bei ``Transfer-Encoding: chunked``
jeden Socket in den Server-Push-Modus (``self:socketInactive()``, Kommentar
„don't count the chunked connections as active, these are long term
connections used for server push").  Der Artwork-Pool von SqueezePlay
(``SlimServer.lua:1009-1043``, maximal 4 gleichzeitige Fetches) laeuft danach
voll: gemessen 7 ``send processing``, nur 3 ``_getArtworkThumbSink``-Callbacks,
``artworkFetchCount`` klebt am Limit — kein weiteres Artwork wird je
angefordert.  Squeezer (eigener HTTP-Client) und der Browser zeigten die Logos
weiterhin, weil sie chunked korrekt verarbeiten.
"""

from __future__ import annotations

import asyncio
import base64
import urllib.parse
from pathlib import Path

from lyrion.web import app as wapp
from lyrion.web.app import _finite_headers

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Perl 9.1.1 rendert ``html/errors/404.html`` fuer ``/nonexistent`` auf 97 B.
PERL_404_BODY_LEN = 97
#: 1x1 PNG (deckt die imageproxy-Route ohne Netz ab).
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _get(static_dir: Path, path: str, monkeypatch):
    """Drive the ASGI app once and return (status, headers-dict, body)."""
    monkeypatch.setattr(wapp, "_auth_config", lambda: (False, "", ""))
    asgi = wapp.create_app(static_dir=str(static_dir))
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        sent.append(event)

    asyncio.run(asgi(
        {"type": "http", "method": "GET", "path": path, "headers": [],
         "query_string": b""},
        receive, send,
    ))
    head = sent[0]
    headers = {k.decode().lower(): v.decode() for k, v in head["headers"]}
    body = b"".join(e.get("body", b"") for e in sent[1:])
    return head["status"], headers, body


def _static_root(tmp_path: Path) -> Path:
    root = tmp_path / "html"
    (root / "images").mkdir(parents=True)
    (root / "images" / "radio.png").write_bytes(TINY_PNG)
    (root / "index.html").write_text("ok")
    return root


def test_finite_headers_adds_length_and_keeps_existing():
    assert _finite_headers({}, b"abc")["Content-Length"] == "3"
    # Vorhandene Laenge bleibt unangetastet (z. B. HEAD-Faelle).
    assert _finite_headers({"Content-Length": "7"}, b"abc")["Content-Length"] == "7"


def test_static_image_is_sent_by_length_not_chunked(tmp_path, monkeypatch):
    """Perl: ``/html/images/radio.png`` → ``Content-Length: 16749``."""
    status, headers, body = _get(_static_root(tmp_path), "/html/images/radio.png",
                                 monkeypatch)
    assert status == 200
    assert headers["content-length"] == str(len(body))
    assert "transfer-encoding" not in headers


def test_missing_static_file_uses_perls_404_page(tmp_path, monkeypatch):
    """Perl: ``text/html`` + ``html/errors/404.html`` (97 B fuer ``/nonexistent``)."""
    status, headers, body = _get(_static_root(tmp_path), "/nonexistent",
                                 monkeypatch)
    assert status == 404
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["content-length"] == str(len(body))
    assert b"<TITLE>404 Not Found</TITLE>" in body
    assert b"404 Not Found: nonexistent" in body


def test_missing_static_file_body_matches_live_perl_size(tmp_path, monkeypatch):
    root = _static_root(tmp_path)
    _, headers, body = _get(root, "/nonexistent", monkeypatch)
    # Live Perl 9.1.1 (2026-09-19) antwortet mit genau 97 Bytes fuer /nonexistent.
    assert len(body) == PERL_404_BODY_LEN


def test_imageproxy_answer_is_framed_with_a_length(tmp_path, monkeypatch):
    """Perl: ``/imageproxy/…/image_40x40_m.png`` → ``Content-Length: 2355``."""
    src = tmp_path / "logo.png"
    src.write_bytes(TINY_PNG)
    payload = urllib.parse.quote(src.as_uri(), safe="-_. ")
    status, headers, body = _get(_static_root(tmp_path),
                                 f"/imageproxy/{payload}/image_40x40_m.png",
                                 monkeypatch)
    assert status == 200
    assert headers["content-length"] == str(len(body))
    assert "transfer-encoding" not in headers
