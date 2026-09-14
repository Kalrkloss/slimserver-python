"""Remote-Stream-Format über HTTP-Header (Perl Remote.pm:333-390).

Der Bug, den diese Tests festnageln (live 2026-09-12, „AAC radio stream geht
nicht"): AAC-Sender ohne ``.aac`` in der URL wurden als MP3 angesagt, weil nur
die URL-Endung ausgewertet wurde. Echte read-only-Proben der Sender:

    https://stream02.pcradio.app/Garik_Sukachov-hi   Content-Type: audio/aac
    https://radiorecord.hostingradio.ru/russiangold96.aacp  audio/aac
    http://strm112.1.fm/chilloutlounge_mobile_mp3    audio/mpeg
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.formats import stream_probe
from lyrion.formats.stream_probe import (
    clear_stream_type_cache,
    codec_for_stream_url,
    perl_type_from_response,
    probe_remote_type,
    scan_stream_url,
)

AAC_URL = "https://stream02.pcradio.app/Garik_Sukachov-hi"
AACP_URL = "https://radiorecord.hostingradio.ru/russiangold96.aacp"
MP3_URL = "http://strm112.1.fm/chilloutlounge_mobile_mp3"


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_stream_type_cache()
    yield
    clear_stream_type_cache()


# ── Perl-Regeln (Remote.pm:333-390) ────────────────────────────────────────


def test_real_aac_stations_without_suffix_are_recognised():
    # Genau der gemeldete Fall: kein ".aac" in der URL, content-type audio/aac
    assert perl_type_from_response(AAC_URL, "audio/aac", {}) == "aac"
    assert perl_type_from_response(AACP_URL, "audio/aac", {}) == "aac"
    assert perl_type_from_response(MP3_URL, "audio/mpeg", {}) == "mp3"


def test_content_type_with_charset_is_parsed():
    # audio/x-mpegurl; charset=ISO-8859-1 (Remote.pm:342-347)
    assert perl_type_from_response("http://x/y", "audio/mpegurl; charset=utf-8", {}) == "m3u"


def test_bug_3396_aac_url_served_as_mpeg():
    assert perl_type_from_response("http://x/stream.aac", "audio/mpeg", {}) == "aac"


def test_m4a_url_served_as_mpeg_becomes_mp4():
    assert perl_type_from_response("http://x/song.m4a", "audio/mpeg", {}) == "mp4"
    assert perl_type_from_response("http://x/song.mp4", "audio/mpeg", {}) == "mp4"


def test_playlist_extension_wins_over_html_content_type():
    # Remote.pm:357-360 — Sender liefern Playlists als text/html
    assert perl_type_from_response("http://x/list.pls", "text/html", {}) == "pls"
    assert perl_type_from_response("http://x/list.m3u", "text/html", {}) == "m3u"


def test_wma_with_m3u_url():
    assert perl_type_from_response("http://x/list.m3u", "audio/x-ms-wma", {}) == "m3u"


def test_plain_html_falls_back_to_m3u():
    assert perl_type_from_response("http://x/whatever", "text/html", {}) == "m3u"


def test_octet_stream_uses_a_valid_url_extension():
    # Remote.pm:375-380: nur mit gültiger Endung wird der Typ ersetzt …
    assert perl_type_from_response("http://x/foo.mp3", "application/octet-stream", {}) == "mp3"
    # … sonst bleibt der ROHE content-type stehen (Perl: $type ist truthy,
    # der icy-name-Fallback greift deshalb nicht). Für uns unkritisch: daraus
    # entsteht kein Format-Byte, der Aufrufer nimmt die URL-Endung.
    assert perl_type_from_response("http://x/foo", "application/octet-stream", {}) \
        == "application/octet-stream"
    assert perl_type_from_response("http://x/foo", "application/octet-stream",
                                   {"icy-name": "X"}) == "application/octet-stream"


def test_shoutcast_without_content_type_but_icy_name_is_mp3():
    # Remote.pm:381-383
    assert perl_type_from_response("http://x/stream", "", {"icy-name": "Radio"}) == "mp3"
    assert perl_type_from_response("http://x/stream", None, {}) is None


# ── Probe + Codec-Byte ─────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, headers, status_code=200, url="http://x/stream",
                 reason="OK", body=b""):
        self.headers = headers
        self.status_code = status_code
        self.reason_phrase = reason
        self.url = url
        self._body = body

    async def aiter_bytes(self):
        if self._body:
            yield self._body


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """httpx-Ersatz: liefert die Header eines echten Senders (read-only)."""

    def __init__(self, *, headers=None, head_fails=False, status_code=200,
                 url="http://x/stream", body=b"", **kw):
        self._headers = headers or {}
        self._head_fails = head_fails
        self._status = status_code
        self._url = url
        self._body = body
        self.methods: list[str] = []
        _FakeClient.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def head(self, url):
        self.methods.append("HEAD")
        if self._head_fails:
            raise RuntimeError("HEAD not allowed")
        return _FakeResponse(self._headers)

    def stream(self, method, url):
        self.methods.append(method)
        return _FakeStreamCtx(_FakeResponse(
            self._headers, status_code=self._status, url=self._url,
            reason="Service Unavailable" if self._status >= 400 else "OK",
            body=self._body))

    async def get(self, url, **kw):
        self.methods.append("GET")
        return _FakeResponse(self._headers, status_code=self._status,
                             url=self._url, body=self._body)


def _patch_httpx(monkeypatch, **kwargs):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(**kwargs))


def test_probe_reads_content_type(monkeypatch):
    _patch_httpx(monkeypatch, headers={"content-type": "audio/aac"})
    assert asyncio.run(probe_remote_type(AAC_URL)) == "aac"
    # zweiter Aufruf kommt aus dem Cache (Perl: setContentType)
    assert stream_probe._TYPE_CACHE[AAC_URL] == "aac"


def test_probe_scans_with_get_like_perl_scanurl(monkeypatch):
    """Perl öffnet die URL mit GET (Scanner/Remote.pm:205) — nie HEAD."""
    _patch_httpx(monkeypatch, headers={"content-type": "audio/aac"})
    assert asyncio.run(probe_remote_type(AAC_URL)) == "aac"
    assert _FakeClient.last.methods == ["GET"]


def test_probe_uses_icy_name_when_no_content_type(monkeypatch):
    _patch_httpx(monkeypatch, headers={"icy-name": "Some Radio"})
    assert asyncio.run(probe_remote_type("http://x/stream")) == "mp3"


def test_probe_falls_back_to_get_when_head_is_refused(monkeypatch):
    # Der historische Fall (Server lehnt HEAD ab) ist mit dem GET-Scan
    # strukturell erledigt: HEAD wird gar nicht mehr benutzt.
    _patch_httpx(monkeypatch, headers={"content-type": "audio/aac"}, head_fails=True)
    assert asyncio.run(probe_remote_type(AAC_URL)) == "aac"


def test_http_503_is_a_scan_failure(monkeypatch):
    """1.FM live 2026-09-14: die Probe bekam 503 und wurde folgenlos verworfen.

    Perl wertet einen Status außerhalb 2xx/3xx als Fehler
    (Slim/Networking/Async/HTTP.pm:434-435 → Scanner/Remote.pm:228-238) und
    bricht den Play ab (Song.pm:302-312) — kein Format, kein strm.
    """
    _patch_httpx(monkeypatch, headers={}, status_code=503,
                 url="http://strm112.1.fm/ambientpsy_mobile_mp3")
    scan = asyncio.run(scan_stream_url("http://strm112.1.fm/ambientpsy_mobile_mp3"))
    assert scan.failed
    assert scan.error == "503 Service Unavailable"
    assert scan.type is None
    # Der Aufrufer bekommt KEIN Format und keinen Codec aus der Endung.
    assert asyncio.run(probe_remote_type("http://strm112.1.fm/ambientpsy_mobile_mp3")) is None


def test_2xx_without_type_or_icy_name_is_playlist_no_items(monkeypatch):
    # Remote.pm:416 → isSong() false → Playlist-Zweig → :1172-1179
    _patch_httpx(monkeypatch, headers={}, status_code=200)
    scan = asyncio.run(scan_stream_url("http://x/stream"))
    assert scan.failed and scan.error == "PLAYLIST_NO_ITEMS_FOUND"


def test_scan_reports_the_icy_bitrate_of_the_same_response(monkeypatch):
    # Remote.pm:530-545 liest icy-br aus DERSELBEN Scan-Antwort.
    _patch_httpx(monkeypatch, headers={"content-type": "audio/mpeg", "icy-br": "256"})
    scan = asyncio.run(scan_stream_url("http://x/mp3"))
    assert not scan.failed and scan.type == "mp3"
    assert scan.bitrate == 256000.0


def test_playlist_body_is_followed_to_its_entry(monkeypatch):
    # Remote.pm:1195-1214 — Einträge werden erneut gescannt; der erste
    # brauchbare gewinnt.
    calls: list[str] = []

    class _Client(_FakeClient):
        def stream(self, method, url):
            calls.append(url)
            if url.endswith(".m3u"):
                return _FakeStreamCtx(_FakeResponse(
                    {"content-type": "audio/x-mpegurl"}, url=url,
                    body=b"http://dead.example/x\nhttp://live.example/y.mp3\n"))
            if "dead" in url:
                return _FakeStreamCtx(_FakeResponse({}, status_code=503, url=url))
            return _FakeStreamCtx(_FakeResponse(
                {"content-type": "audio/mpeg"}, url=url))

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))
    scan = asyncio.run(scan_stream_url("http://x/list.m3u"))
    assert calls[0] == "http://x/list.m3u"
    assert scan.url == "http://live.example/y.mp3"
    assert not scan.failed and scan.type == "mp3"


def test_codec_byte_for_aac_station_is_a(monkeypatch):
    _patch_httpx(monkeypatch, headers={"content-type": "audio/aac"})
    # strm-Format-Byte: aac -> 'a' (nicht 'm'!)
    assert asyncio.run(codec_for_stream_url(AAC_URL, fallback="m")) == "a"


def test_codec_falls_back_to_the_url_guess_on_failure(monkeypatch):
    import httpx

    def _boom(**kw):
        raise OSError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    assert asyncio.run(codec_for_stream_url("http://x/stream.mp3", fallback="m")) == "m"

