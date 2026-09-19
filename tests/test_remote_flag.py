"""Tests for the remote-stream flag lifecycle (radio vs local track).

Regression: playing a radio stream set ``player.remote = 1`` permanently;
starting a local track afterwards never cleared it, so ``_advance_after_track``
refused to advance (it treats ``remote`` as "live stream, never ends").
"""

import asyncio

import pytest

from lyrion.formats import stream_probe
from lyrion.formats.stream_probe import StreamScan
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState


class _FakeHandler:
    def __init__(self):
        self.remote_calls: list[tuple] = []

    async def send_strm_to_player(self, mac, track_id, start_seconds=0.0):
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        self.remote_calls.append((url, codec, kw.get("resolved")))
        return True


def _fresh_pm():
    pm = object.__new__(PlayerManager)
    pm.players = {}
    pm._protocol_handler = _FakeHandler()
    return pm


def _player(pm):
    p = PlayerState(mac="02:11:22:33:44:55", name="Test", ip="127.0.0.1", port=0)
    pm.players[p.mac] = p
    return p


@pytest.fixture
def ok_scan(monkeypatch):
    """Der Stream-Scan (Perl scanURL) ohne Netz: MP3, 128 kbit/s."""
    async def fake(url, timeout=15.0, _depth=0):
        return StreamScan(url=url, status=200, type="mp3", bitrate=128000.0,
                          headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr(stream_probe, "scan_stream_url", fake)
    return fake


def test_play_url_sets_remote_flag(ok_scan):
    pm = _fresh_pm()
    p = _player(pm)
    assert asyncio.run(pm.play_url(p.mac, "http://example.com/stream", "Radio")) is True
    assert p.remote == 1
    # Der gescannte Codec (nicht die URL-Endung) geht in das strm-Frame,
    # die finale URL kommt fertig aufgelöst an (kein zweiter Resolve-Schritt).
    assert pm._protocol_handler.remote_calls == [("http://example.com/stream", "m", True)]


def test_play_url_aborts_when_the_scan_fails(monkeypatch):
    """1.FM live 2026-09-14: Scan-Antwort 503.

    Perl bricht dann ab (Async/HTTP.pm:434-435 → Scanner/Remote.pm:228-238 →
    Song.pm:302-312 `playlist cant_open`) — der Player bekommt KEIN strm und
    der Server fällt NICHT auf einen Proxy/Endungs-Guess zurück.
    """
    async def failing(url, timeout=15.0, _depth=0):
        return StreamScan(url=url, status=503,
                          error="503 Service Unavailable")

    monkeypatch.setattr(stream_probe, "scan_stream_url", failing)
    pm = _fresh_pm()
    p = _player(pm)
    p.playlist = ["http://old.example/track.mp3"]
    p.playlist_position = 0
    assert asyncio.run(pm.play_url(p.mac, "http://strm112.1.fm/ambientpsy_mobile_mp3")) is False
    assert pm._protocol_handler.remote_calls == []      # kein strm
    assert p.playlist == ["http://old.example/track.mp3"]  # Ausgangszustand
    assert p.remote == 0


def test_play_track_clears_remote_flag():
    pm = _fresh_pm()
    p = _player(pm)
    p.remote = 1  # simulate a previously played radio stream
    assert asyncio.run(pm.play_track(p.mac, 1)) is True
    assert p.remote == 0
