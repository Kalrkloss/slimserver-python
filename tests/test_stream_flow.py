"""Flow control + stream-switch cancellation for /stream.mp3 (LIVE-08).

A player stream is written UNTHROTTLED: no artificial rate limit, flow
control comes from the socket/ASGI backpressure. An explicit 1x-realtime
pacer (with an up-front burst) was removed on 2026-09-12: jive buffers
1 MB before it starts decoding lossless/PCM formats (Playback.lua:914-925),
so a realtime-paced FLAC stayed silent for ~30 s (live: track 470,
735 kbit/s) — Perl has no such delay.

Perl parcel (citations: /tmp/lms-ref, public/9.2 @ d1d0a683d):

* ``Slim/Web/HTTP.pm:2152-2439`` ``sendStreamingResponse`` — the server
  writes at most ``MAXCHUNKSIZE`` (32768, line 61) bytes per select()
  callback, only when the socket is writable (``addWrite``, line 2143);
  a partial write / EWOULDBLOCK requeues the unsent remainder (2387-2405).
* ``Slim/Web/HTTP.pm:2136`` — a freshly opened streaming socket becomes
  ``$client->streamingsocket``; ``2185-2199`` closes any streaming socket
  that is no longer the client's current one → the old stream is aborted
  on switch.
* ``Slim/Player/Squeezebox.pm:206-216`` ``stop`` sets
  ``streamingsocket(undef)`` with the comment "HTTP.pm will close the
  socket on the next select".
* ``cont`` exists ONLY as a server→player frame
  (``Slim/Player/Squeezebox2.pm:724``); there is no client→server ``cont``
  handler anywhere in the Perl tree, so an incoming ``cont`` must NOT be
  wired as "send more" (that flow control is the socket's).

These tests are in-process: no server, no real waiting. ``asyncio.sleep``
is spied on to prove nothing paces the response.
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import stream as stream_mod

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"
MAC_OTHER = "AA:BB:CC:DD:EE:02"


@pytest.fixture(autouse=True)
def _clean_registry():
    stream_mod._active_streams.clear()
    yield
    stream_mod._active_streams.clear()


# ── harness ───────────────────────────────────────────────────────────────

def _bitrate_stub(bps: int | None):
    async def _fake(_track_id, _file_bytes=None):
        return bps
    return _fake


def _run_stream(tmp_path, *, content: bytes, query: str,
                range_value: bytes | None = None, bitrate: int | None = 128000,
                on_chunk=None):
    """Run stream_track against a temp file with a fake ``send``.

    Returns (sent_events, body_chunk_sizes). ``on_chunk(n)`` (optional, sync
    or async) fires after the n-th body chunk was handed to ``send``.
    """
    f = tmp_path / "audio.mp3"
    f.write_bytes(content)
    sent: list[dict] = []
    chunks: list[int] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        sent.append(event)
        if event["type"] == "http.response.body" and event.get("body"):
            chunks.append(len(event["body"]))
            if on_chunk is not None:
                result = on_chunk(len(chunks))
                if asyncio.iscoroutine(result):
                    await result

    headers = [(b"range", range_value)] if range_value else []
    scope = {
        "type": "http", "method": "GET",
        "query_string": query.encode(),
        "headers": headers,
    }

    async def run():
        original_load = stream_mod._load_track
        original_bitrate = stream_mod._stream_bitrate_bps

        async def _fake_load(track_id):
            return f, "audio/mpeg"

        stream_mod._load_track = _fake_load
        stream_mod._stream_bitrate_bps = _bitrate_stub(bitrate)
        try:
            await stream_mod.stream_track(scope, receive, send)
        finally:
            stream_mod._load_track = original_load
            stream_mod._stream_bitrate_bps = original_bitrate

    asyncio.run(run())
    return sent, chunks


def _body(sent) -> bytes:
    return b"".join(e["body"] for e in sent
                    if e["type"] == "http.response.body" and e["body"])


# ── (a) no artificial throttle: chunks go out as fast as the socket ───────

def _sleep_spy(monkeypatch) -> list[float]:
    """Record every ``asyncio.sleep`` the handler performs."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _spy(delay, *args, **kwargs):
        sleeps.append(delay)
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _spy)
    return sleeps


def test_stream_is_not_throttled(tmp_path, monkeypatch):
    """Perl never rate-limits a player stream (HTTP.pm:61, :2143,
    :2152-2439) — flow control is the socket, and in our ASGI stack that is
    ``await send(...)``, which blocks while uvicorn's transport is
    write-paused (uvicorn/protocols/http/h11_impl.py ``flow.drain``)."""
    sleeps = _sleep_spy(monkeypatch)
    content = bytes(range(256)) * 1563          # ~400 KB
    sent, chunks = _run_stream(
        tmp_path, content=content, query=f"id=7&player={MAC}", bitrate=128000,
    )

    assert _body(sent) == content               # every byte arrives, in order
    assert sent[-1]["more_body"] is False
    assert chunks and all(n == stream_mod.PERL_MAXCHUNKSIZE for n in chunks[:-1])
    assert sleeps == [], (
        "the stream path must not pace: jive waits for 1 MB before it starts "
        "a lossless track (Playback.lua:914-925), so throttling to realtime "
        "delays playback by tens of seconds"
    )


def test_chunk_size_is_perls_maxchunksize():
    # Slim/Web/HTTP.pm:61 MAXCHUNKSIZE = 32768
    assert stream_mod.PERL_MAXCHUNKSIZE == 32768


# ── bitrate source: tracks.bitrate is bits/s, not kbit/s ──────────────────
#
# Verified against the live library DB (~/.lyrion/Lyrion/Prefs/lyrion.db):
# a 320 kbit/s MP3 stores 320000, raw PCM stores 1411200. Multiplying by
# 1000 would pace 1000x too slowly and starve the player.

def _fake_track_db(monkeypatch, bitrate, duration):
    import lyrion.database.sqlite_helper as helper

    class _Result:
        def one_or_none(self):
            return (bitrate, duration)

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(helper, "db_session", lambda: _Session())


def test_db_bitrate_column_is_taken_as_bits_per_second(monkeypatch):
    _fake_track_db(monkeypatch, bitrate=320000, duration=0.0)
    assert asyncio.run(stream_mod._stream_bitrate_bps(4, 7_040_000)) == 320000


def test_bitrate_is_derived_from_duration_when_available(monkeypatch):
    # 4 MB over 100 s = 320 kbit/s — beats a stale/rounded column value
    _fake_track_db(monkeypatch, bitrate=999999, duration=100.0)
    assert asyncio.run(stream_mod._stream_bitrate_bps(4, 4_000_000)) == 320000


def test_unknown_bitrate_yields_none(monkeypatch):
    _fake_track_db(monkeypatch, bitrate=None, duration=0.0)
    assert asyncio.run(stream_mod._stream_bitrate_bps(4, 1234)) is None


def test_absurd_duration_estimate_falls_back(monkeypatch):
    # duration far too long for the bytes → implausible rate → use column
    _fake_track_db(monkeypatch, bitrate=320000, duration=100_000.0)
    assert asyncio.run(stream_mod._stream_bitrate_bps(4, 4_000_000)) == 320000


# ── (b) cancel stops a running response and cleans the registry ───────────

def test_cancel_active_stream_stops_the_running_response(tmp_path):
    content = b"z" * (256 * 1024)
    triggered = {"n": 0}

    def _on_chunk(count):
        if count == 3:
            triggered["n"] = count
            assert stream_mod.cancel_active_stream(MAC) is True

    sent, _chunks = _run_stream(
        tmp_path, content=content,
        query=f"id=7&player={MAC}", bitrate=128000, on_chunk=_on_chunk,
    )

    assert triggered["n"] == 3, "cancel never reached the running handler"
    assert 0 < len(_body(sent)) < len(content), (
        "a cancelled stream must stop early, not deliver the whole file"
    )
    assert MAC_CLEAN not in stream_mod._active_streams, (
        "registry entry leaked after the handler finished"
    )


def test_handler_registers_itself_and_unregisters_on_completion(tmp_path):
    seen = {}

    def _on_chunk(_count):
        seen.setdefault("macs", list(stream_mod._active_streams))

    content = bytes(range(256)) * 313   # ~80 KB → several chunks
    _run_stream(tmp_path, content=content, query=f"id=7&player={MAC}",
                bitrate=64000, on_chunk=_on_chunk)
    assert seen.get("macs") == [MAC_CLEAN]
    assert stream_mod._active_streams == {}


# ── (c) a new strm frame cancels the previous stream ──────────────────────

class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _make_client(player: PlayerState):
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    client.web_port = 9000
    client.server_ip = "127.0.0.1"
    return client, writer


def test_new_strm_frame_cancels_the_previous_stream():
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.mode = "stop"
    client, writer = _make_client(player)

    async def run():
        old = stream_mod.register_active_stream(MAC)
        assert old.cancel_event.is_set() is False
        assert await client.send_strm_to_player(MAC, 4242) is True
        assert writer.frames, "no strm frame was sent"
        assert old.cancel_event.is_set(), (
            "a new strm frame must abort the player's previous /stream.mp3"
        )

    asyncio.run(run())


def test_new_remote_stream_cancels_the_previous_stream():
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.mode = "stop"
    client, _writer = _make_client(player)

    async def run():
        old = stream_mod.register_active_stream(MAC)
        # https on a player without CanHTTPS → proxy path, but the cancel
        # must happen before any frame goes out, on every remote branch.
        await client.send_remote_stream(MAC, "https://example.invalid/x.mp3")
        assert old.cancel_event.is_set(), (
            "send_remote_stream must abort the previous /stream.mp3 response"
        )

    asyncio.run(run())


# ── (d) range requests stay byte-exact and unthrottled ────────────────────

def test_range_request_is_not_paced_and_not_cancelled(tmp_path, monkeypatch):
    sleeps = _sleep_spy(monkeypatch)
    content = bytes(range(256)) * 64          # 16 KB
    sent, _chunks = _run_stream(
        tmp_path, content=content, query=f"id=7&player={MAC}",
        range_value=b"bytes=100-199",
    )
    start = sent[0]
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 206
    assert headers["content-range"] == f"bytes 100-199/{len(content)}"
    assert _body(sent) == content[100:200]
    assert sleeps == []
    assert stream_mod._active_streams == {}, "range requests must not register"


def test_stream_without_player_is_never_registered(tmp_path, monkeypatch):
    sleeps = _sleep_spy(monkeypatch)
    _sent, _chunks = _run_stream(tmp_path, content=b"q" * 4096, query="id=7")
    assert sleeps == []
    assert stream_mod._active_streams == {}


# ── (e) cancelling one player must not touch another ──────────────────────

def test_cancel_only_affects_the_named_player():
    async def run():
        a = stream_mod.register_active_stream(MAC)
        b = stream_mod.register_active_stream(MAC_OTHER)
        assert stream_mod.cancel_active_stream(MAC_OTHER) is True
        assert b.cancel_event.is_set()
        assert not a.cancel_event.is_set()
        assert stream_mod.cancel_active_stream(MAC) is True
        assert stream_mod.cancel_active_stream("00:00:00:00:00:00") is False

    asyncio.run(run())


def test_registering_a_new_stream_replaces_and_cancels_the_old_one():
    async def run():
        old = stream_mod.register_active_stream(MAC)
        new = stream_mod.register_active_stream(MAC)   # player reconnected
        assert old.cancel_event.is_set()
        assert not new.cancel_event.is_set()
        assert stream_mod._active_streams[MAC_CLEAN] is new
        stream_mod.unregister_active_stream(old)       # stale handler exits
        assert stream_mod._active_streams[MAC_CLEAN] is new, (
            "a stale handler must not evict the live registry entry"
        )

    asyncio.run(run())


def test_no_rate_limiter_is_reintroduced():
    """Regression guard: the stream handler must not grow an artificial
    pacer again. Perl writes MAXCHUNKSIZE chunks as fast as the socket
    accepts them (HTTP.pm:61, :2143, :2152-2439); flow control is the
    socket. An invented 1x-realtime limit (with a "burst") cost ~30 s of
    silence at the start of every lossless track (jive waits for 1 MB,
    Playback.lua:914-925) — live 2026-09-12, FLAC track 470."""
    for gone in ("StreamPacer", "BURST_SECONDS", "BURST_MIN_BYTES",
                 "PACED_CHUNK_SIZE", "_sleep_or_cancel"):
        assert not hasattr(stream_mod, gone), (
            f"{gone} is back — flow control belongs to the socket, not to an "
            "invented rate limit"
        )
    assert stream_mod.PERL_MAXCHUNKSIZE == 32768       # HTTP.pm:61
