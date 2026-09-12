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
    def __init__(self, headers):
        self.headers = headers


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """httpx-Ersatz: liefert die Header eines echten Senders (read-only)."""

    def __init__(self, *, headers=None, head_fails=False, **kw):
        self._headers = headers or {}
        self._head_fails = head_fails

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def head(self, url):
        if self._head_fails:
            raise RuntimeError("HEAD not allowed")
        return _FakeResponse(self._headers)

    def stream(self, method, url):
        return _FakeStreamCtx(_FakeResponse(self._headers))


def _patch_httpx(monkeypatch, **kwargs):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(**kwargs))


def test_probe_reads_content_type(monkeypatch):
    _patch_httpx(monkeypatch, headers={"content-type": "audio/aac"})
    assert asyncio.run(probe_remote_type(AAC_URL)) == "aac"
    # zweiter Aufruf kommt aus dem Cache (Perl: setContentType)
    assert stream_probe._TYPE_CACHE[AAC_URL] == "aac"


def test_probe_falls_back_to_get_when_head_is_refused(monkeypatch):
    _patch_httpx(monkeypatch, headers={"content-type": "audio/aac"}, head_fails=True)
    assert asyncio.run(probe_remote_type(AAC_URL)) == "aac"


def test_probe_uses_icy_name_when_no_content_type(monkeypatch):
    _patch_httpx(monkeypatch, headers={"icy-name": "Some Radio"})
    assert asyncio.run(probe_remote_type("http://x/stream")) == "mp3"


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
