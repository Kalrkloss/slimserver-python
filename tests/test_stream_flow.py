"""Flow control + stream-switch cancellation for /stream.mp3 (LIVE-08).

Without pacing our handler flushed a whole 11.7 MB track into the socket in
0.40 s; the player then sat on a full buffer and never opened a new
``GET /stream.mp3`` when the user tapped another track (elapsed frozen at
62.202 s, out_fullness pinned at 3525496/3528000).

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
  wired as "send more".

These tests are in-process: no server, no real waiting. The pacer's clock
(``_now``) and sleep (``_sleep``) hooks are monkeypatched so pacing is
measured deterministically.
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
                fake_clock=None, on_sleep=None):
    """Run stream_track against a temp file with a fake ``send``.

    Returns (sent_events, sleeps, clock) — ``sleeps`` is only recorded when
    a clock is injected (the fake ``_sleep`` advances it instantly).
    """
    f = tmp_path / "audio.mp3"
    f.write_bytes(content)
    sent: list[dict] = []
    sleeps: list[float] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        sent.append(event)

    headers = [(b"range", range_value)] if range_value else []
    scope = {
        "type": "http", "method": "GET",
        "query_string": query.encode(),
        "headers": headers,
    }

    async def run():
        original_load = stream_mod._load_track
        original_bitrate = stream_mod._stream_bitrate_bps
        original_now = stream_mod._now
        original_sleep = stream_mod._sleep

        async def _fake_load(track_id):
            return f, "audio/mpeg"

        async def _fake_sleep(delay):
            sleeps.append(delay)
            if fake_clock is not None:
                fake_clock["t"] += delay
            if on_sleep is not None:
                result = on_sleep(len(sleeps))
                if asyncio.iscoroutine(result):
                    await result

        stream_mod._load_track = _fake_load
        stream_mod._stream_bitrate_bps = _bitrate_stub(bitrate)
        if fake_clock is not None:
            stream_mod._now = lambda: fake_clock["t"]
            stream_mod._sleep = _fake_sleep
        try:
            await stream_mod.stream_track(scope, receive, send)
        finally:
            stream_mod._load_track = original_load
            stream_mod._stream_bitrate_bps = original_bitrate
            stream_mod._now = original_now
            stream_mod._sleep = original_sleep

    asyncio.run(run())
    return sent, sleeps


def _body(sent) -> bytes:
    return b"".join(e["body"] for e in sent
                    if e["type"] == "http.response.body" and e["body"])


# ── (a) pacer: burst then ~1x realtime ────────────────────────────────────

def test_pacer_bursts_then_throttles_to_realtime(tmp_path):
    content = bytes(range(256)) * 1563          # ~400 KB
    clock = {"t": 0.0}
    sent, sleeps = _run_stream(
        tmp_path, content=content,
        query=f"id=7&player={MAC}", bitrate=128000, fake_clock=clock,
    )

    # every byte still arrives, in order
    assert _body(sent) == content
    assert sent[-1]["more_body"] is False

    burst = stream_mod.BURST_SECONDS * (128000 // 8)      # 3 s @128 kbit/s
    expected = (len(content) - burst) / (128000 / 8)
    assert sleeps, "a 400 KB track must not be written unthrottled"
    assert abs(sum(sleeps) - expected) < 1.0, (
        f"pacing {sum(sleeps):.2f}s for {len(content)} bytes, "
        f"expected ~{expected:.2f}s"
    )
    # the fast start survives: the first pace delay is ≤ one poll slice
    assert max(sleeps) <= stream_mod.CANCEL_POLL_SECONDS + 1e-6


def test_pacer_default_bitrate_is_conservative(tmp_path):
    # no bitrate in the DB → conservative 128 kbit/s default
    pacer = stream_mod.StreamPacer.from_bitrate(None)
    assert pacer.bytes_per_sec == stream_mod.DEFAULT_BITRATE_BPS // 8
    assert pacer.burst_bytes == int(pacer.bytes_per_sec * stream_mod.BURST_SECONDS)


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


def test_pacer_schedule_is_zero_inside_the_burst():
    clock = {"t": 0.0}
    original_now = stream_mod._now
    stream_mod._now = lambda: clock["t"]
    try:
        pacer = stream_mod.StreamPacer.from_bitrate(128000, burst_seconds=2.0)
        pacer.start()
        # 2 s @128k = 32000 B burst
        assert pacer.schedule(16000) == 0.0
        assert pacer.schedule(16000) == 0.0
        assert pacer.schedule(16000) > 0.0        # past the burst
        clock["t"] += 10.0                        # plenty of time elapsed
        assert pacer.schedule(16000) == 0.0       # already ahead of realtime
    finally:
        stream_mod._now = original_now


# ── (b) cancel stops a running response and cleans the registry ───────────

def test_cancel_active_stream_stops_the_running_response(tmp_path):
    content = b"z" * (256 * 1024)
    clock = {"t": 0.0}
    asleep: list[float] = []

    def _on_sleep(count):
        if count == 2:
            assert stream_mod.cancel_active_stream(MAC) is True

    sent, sleeps = _run_stream(
        tmp_path, content=content,
        query=f"id=7&player={MAC}", bitrate=128000,
        fake_clock=clock, on_sleep=_on_sleep,
    )
    asleep.extend(sleeps)

    assert len(asleep) >= 2, "cancel never reached the running handler"
    assert 0 < len(_body(sent)) < len(content), (
        "a cancelled stream must stop early, not deliver the whole file"
    )
    assert MAC_CLEAN not in stream_mod._active_streams, (
        "registry entry leaked after the handler finished"
    )


def test_handler_registers_itself_and_unregisters_on_completion(tmp_path):
    seen = {}

    def _on_sleep(_count):
        seen.setdefault("macs", list(stream_mod._active_streams))

    content = bytes(range(256)) * 313   # ~80 KB → several paced chunks
    clock = {"t": 0.0}
    _run_stream(tmp_path, content=content, query=f"id=7&player={MAC}",
                bitrate=64000, fake_clock=clock, on_sleep=_on_sleep)
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


# ── (d) range requests stay byte-exact and unpaced ────────────────────────

def test_range_request_is_not_paced_and_not_cancelled(tmp_path):
    content = bytes(range(256)) * 64          # 16 KB
    clock = {"t": 0.0}
    sent, sleeps = _run_stream(
        tmp_path, content=content, query=f"id=7&player={MAC}",
        range_value=b"bytes=100-199", fake_clock=clock,
    )
    start = sent[0]
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 206
    assert headers["content-range"] == f"bytes 100-199/{len(content)}"
    assert _body(sent) == content[100:200]
    assert sleeps == [], "range/seek responses must keep the old behaviour"
    assert stream_mod._active_streams == {}, "range requests must not register"


def test_unthrottled_request_without_player_is_unchanged(tmp_path):
    content = b"q" * 4096
    _sent, sleeps = _run_stream(
        tmp_path, content=content, query="id=7",
        fake_clock={"t": 0.0},
    )
    assert sleeps == []


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
