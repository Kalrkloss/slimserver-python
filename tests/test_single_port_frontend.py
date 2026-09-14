"""Single public port (Perl parity): the native frontend + the ASGI relay.

Perl LMS serves ONE HTTP port itself and pipelines requests on it
(``Slim/Web/HTTP.pm:2065-2072``). The Python stack cannot (uvicorn/h11
buffers until the response ends, h11_impl.py:191-197), so the consolidation
puts the native, pipelining-capable server in FRONT (``public_http_port``,
default 9000) and uvicorn behind it (``internal_http_port``, default 9001,
loopback only).

What these tests lock down:

* the relay is a STREAMING pipe — the first body bytes reach the client while
  the internal app still holds its response open. A proxy that reads the
  whole body first would stall ``/stream.mp3`` (an album file, up to ~900 MB)
  and kill playback;
* framing per Perl: chunked only for HTTP/1.1 clients (HTTP.pm:2838-2845),
  Content-Length for Jive's HTTP/1.0 JSON-RPC so its pipelined POSTs keep
  working on the same socket (HTTP.pm:1191 disables keep-alive for
  stream.mp3);
* every announcement names the PUBLIC port (TLV discovery, the JSON presence
  beacon, ``serverstatus.httpport``) and the layout is configurable —
  ``public_http_port == 0`` (or == internal) rolls the consolidation back.

Everything runs in-process against ephemeral ports and a stub internal app —
the live dev server on :9000/:9001 is never touched.
"""

import asyncio
import json
import socket

from lyrion.networking.cometd_stream import start_cometd_server
from lyrion.web.api import JSONRPCAPI
from lyrion.web.cometd import CometdManager

import lyrion.config as config_mod


# ── helpers ────────────────────────────────────────────────────────────────

class _StubRPC:
    """Minimal slim.request handler for the Bayeux side."""

    async def handle_request(self, body: bytes) -> bytes:
        payload = json.loads(body)
        player, cmd = payload["params"]
        return json.dumps({
            "id": 1, "method": "slim.request",
            "result": {"command": cmd[0], "player_id": player, "count": 1},
        }).encode()


def _run(coro):
    return asyncio.run(coro)


async def _read_headers(reader) -> bytes:
    head = b""
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        head += line
        if line in (b"\r\n", b"\n", b""):
            return head


async def _read_chunk(reader) -> bytes:
    size = int((await asyncio.wait_for(reader.readline(), timeout=5)).strip(), 16)
    if size == 0:
        # the terminating chunk is "0\r\n\r\n" (plus trailers): consume it so
        # the buffer stays aligned for the next pipelined response
        while (await asyncio.wait_for(reader.readline(), timeout=5)) not in (b"\r\n", b"\n", b""):
            pass
        return b""
    data = await asyncio.wait_for(reader.readexactly(size), timeout=5)
    await asyncio.wait_for(reader.readexactly(2), timeout=5)
    return data


def _head_value(head: bytes, name: bytes) -> bytes:
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == name.lower():
            return v.strip()
    return b""


def _start_internal(handler):
    """Start a fake internal app; ``handler(reader, writer, head)`` answers."""

    async def serve(reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            await handler(reader, writer, head)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    return serve


async def _start_frontend(internal_port: int):
    mgr = CometdManager(_StubRPC())
    server = await start_cometd_server(mgr, "127.0.0.1", 0, internal_port)
    return server, server.sockets[0].getsockname()[1]


# ── 1) announcements: config defaults, TLV, beacon, serverstatus ───────────


def test_http_ports_defaults_to_one_public_port():
    public, internal = config_mod.http_ports()
    assert public == 9000
    assert internal == 9001
    assert config_mod.frontend_enabled() is True


def test_rollback_configuration_puts_uvicorn_back_on_the_public_port(monkeypatch):
    """``internal=public`` (or public 0) => no frontend, uvicorn serves it.

    Rollback is configuration only: no code revert is needed, uvicorn binds
    the (now public) internal port again on 0.0.0.0.
    """
    cfg = config_mod.get_config()
    monkeypatch.setitem(cfg._prefs._cache, "internal_http_port", "9000")
    monkeypatch.setitem(cfg._prefs._cache, "public_http_port", "9000")
    assert config_mod.frontend_enabled() is False
    assert config_mod.asgi_bind() == ("0.0.0.0", 9000)

    # ... same with the frontend explicitly switched off
    monkeypatch.setitem(cfg._prefs._cache, "public_http_port", "0")
    assert config_mod.frontend_enabled() is False
    assert config_mod.http_ports() == (0, 9000)
    assert config_mod.asgi_bind() == ("0.0.0.0", 9000)

    # and the consolidated default keeps uvicorn on loopback
    monkeypatch.setitem(cfg._prefs._cache, "public_http_port", "9000")
    monkeypatch.setitem(cfg._prefs._cache, "internal_http_port", "9001")
    assert config_mod.frontend_enabled() is True
    assert config_mod.asgi_bind() == ("127.0.0.1", 9001)


def test_httpport_cli_flag_still_names_the_public_port(monkeypatch):
    cfg = config_mod.get_config()
    monkeypatch.setattr(cfg, "_cli_args", type("A", (), {"httpport": 9300})(),
                        raising=False)
    assert config_mod.public_http_port() == 9300


def test_serverstatus_reports_the_public_port(monkeypatch):
    res = asyncio.run(JSONRPCAPI()._slim_request("", ["serverstatus", "0", "5"]))
    assert res["httpport"] == 9000

    # ... and follows the configured port (it is not a hardcoded 9000)
    cfg = config_mod.get_config()
    monkeypatch.setitem(cfg._prefs._cache, "public_http_port", "9300")
    res = asyncio.run(JSONRPCAPI()._slim_request("", ["serverstatus", "0", "5"]))
    assert res["httpport"] == 9300


def test_tlv_discovery_announces_the_public_port():
    """SqueezeCtrl/SqueezePlay read the JSON field and connect there."""
    from lyrion.networking.discovery import DiscoveryService

    sent: list[tuple] = []

    class _Sock:
        def sendto(self, data, addr):
            sent.append((data, addr))

    ds = DiscoveryService()
    ds._beacon_socket = _Sock()
    asyncio.run(ds._reply_tlv(("127.0.0.1", 4000), b"e" + b"JSON" + bytes([0])))
    assert sent, "no TLV reply"
    data = sent[0][0]
    assert data[:1] == b"E"
    fields = {}
    body, pos = data[1:], 0
    while pos + 5 <= len(body):
        tag = body[pos:pos + 4]
        ln = body[pos + 4]
        fields[tag] = body[pos + 5:pos + 5 + ln]
        pos += 5 + ln
    assert fields[b"JSON"] == b"9000", fields
    assert b"IPAD" in fields


def test_json_presence_beacon_names_the_public_port():
    """The UDP beacon must name the same port as the TLV — two different
    ports made SqueezePlay keep two sessions in parallel."""
    from lyrion.__main__ import _broadcast_server_presence

    sent: list[tuple] = []

    class _Sock:
        def __init__(self, *a, **k):
            self.closed = False

        def settimeout(self, *a):
            pass

        def connect(self, addr):
            raise OSError("unreachable")     # force the 127.0.0.1 fallback

        def getsockname(self):
            return ("127.0.0.1", 12345)

        def setsockopt(self, *a):
            pass

        def sendto(self, data, addr):
            sent.append((data, addr))

        def close(self):
            self.closed = True

    class _Log:
        def debug(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

    real_socket = socket.socket

    async def go():
        socket.socket = lambda *a, **k: _Sock()
        task = asyncio.create_task(
            _broadcast_server_presence(_Log(), 3483, 9000))
        try:
            for _ in range(50):
                if sent:
                    break
                await asyncio.sleep(0.05)
        finally:
            socket.socket = real_socket
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return sent

    sent = asyncio.run(go())
    assert sent, "no presence beacon"
    msg = json.loads(sent[0][0])
    assert msg["port"] == 9000
    assert msg["jsonrpc"].endswith(":9000/jsonrpc.js")
    assert sent[0][1] == ("255.255.255.255", 3483)


# ── 2) the relay: streaming, pipelining, framing ───────────────────────────


def test_relay_streams_the_response_instead_of_buffering_it():
    """The internal app holds its body OPEN after the first slice.

    A proxy that reads the body to its end first (the mistake that kills
    /stream.mp3) would deliver NOTHING here — the first slice must arrive
    while the internal app is still waiting.
    """
    async def run():
        release = asyncio.Event()

        async def origin(reader, writer, head):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/flac\r\n"
                         b"Content-Length: 20\r\n\r\nFIRSTCHUNK")
            await writer.drain()
            await release.wait()                # ... response stays open
            writer.write(b"SECONDHALF")
            await writer.drain()
            writer.close()

        internal = await asyncio.start_server(_start_internal(origin),
                                              "127.0.0.1", 0)
        internal_port = internal.sockets[0].getsockname()[1]
        server, port = await _start_frontend(internal_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"GET /stream.mp3?id=1 HTTP/1.1\r\n"
                             b"Host: 127.0.0.1\r\n\r\n")
                await writer.drain()
                head = await _read_headers(reader)
                assert b"200 OK" in head, head
                assert b"Content-Length: 20" in head, head
                first = await asyncio.wait_for(reader.readexactly(10), timeout=3)
                assert first == b"FIRSTCHUNK", first
                assert not release.is_set(), \
                    "the relay buffered the body until it was complete"
                release.set()
                rest = await asyncio.wait_for(reader.readexactly(10), timeout=3)
                assert rest == b"SECONDHALF", rest
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            internal.close()
            await internal.wait_closed()

    _run(run())


def test_relay_copies_a_large_body_byte_for_byte():
    """/stream.mp3 shape: several MB with a Content-Length, no truncation."""
    payload = bytes(range(256)) * 12000            # ~3 MB, incompressible order

    async def run():
        async def origin(reader, writer, head):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                         + f"Content-Length: {len(payload)}\r\n\r\n".encode())
            for off in range(0, len(payload), 64 * 1024):
                writer.write(payload[off:off + 64 * 1024])
                await writer.drain()
            writer.close()

        internal = await asyncio.start_server(_start_internal(origin),
                                              "127.0.0.1", 0)
        internal_port = internal.sockets[0].getsockname()[1]
        server, port = await _start_frontend(internal_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"GET /stream.mp3?id=1 HTTP/1.1\r\n"
                             b"Host: 127.0.0.1\r\n\r\n")
                await writer.drain()
                head = await _read_headers(reader)
                assert b"Content-Length: %d" % len(payload) in head, head
                got = await asyncio.wait_for(reader.readexactly(len(payload)),
                                             timeout=15)
                assert got == payload
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            internal.close()
            await internal.wait_closed()

    _run(run())


def test_pipelined_jsonrpc_posts_on_the_public_socket_are_both_answered():
    """Jive sends its /jsonrpc.js calls as HTTP/1.0 and pipelines them onto the
    same socket. Both must be answered, each self-delimiting (Content-Length,
    like the uvicorn chunked answer re-framed), and the socket must stay
    usable — the Perl frontend keeps it open for the next pipelined call."""
    seen: list[bytes] = []

    async def run():
        async def origin(reader, writer, head):
            seen.append(head.split(b"\r\n")[0])
            body = json.dumps({"result": {"httpport": 9000}}).encode()
            # uvicorn answers chunked; the frontend must re-frame it.
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n"
                         + f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            await writer.drain()
            writer.close()

        internal = await asyncio.start_server(_start_internal(origin),
                                              "127.0.0.1", 0)
        internal_port = internal.sockets[0].getsockname()[1]
        server, port = await _start_frontend(internal_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                body = json.dumps({"id": 1, "method": "slim.request",
                                   "params": ["", ["serverstatus", 0, 10]]}
                                  ).encode()
                req = (b"POST /jsonrpc.js HTTP/1.0\r\n"
                       b"Content-Type: application/json\r\n"
                       + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                writer.write(req + req)          # pipelined, no read in between
                await writer.drain()
                for i in range(2):
                    head = await _read_headers(reader)
                    assert b"200 OK" in head, (i, head)
                    length = int(_head_value(head, b"content-length") or 0)
                    assert length > 0, f"answer {i} is not self-delimiting: {head}"
                    assert b"transfer-encoding" not in head.lower(), head
                    payload = await asyncio.wait_for(
                        reader.readexactly(length), timeout=5)
                    assert json.loads(payload)["result"]["httpport"] == 9000
                assert len(seen) == 2, seen
                assert all(line.startswith(b"POST /jsonrpc.js") for line in seen)
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            internal.close()
            await internal.wait_closed()

    _run(run())


def test_http11_client_keeps_the_chunked_framing_verbatim():
    """An HTTP/1.1 client frames the body itself (Perl sets chunked for
    HTTP/1.1 only), so the frontend must pass the framing through — and the
    socket stays reusable afterwards."""
    async def run():
        async def origin(reader, writer, head):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n"
                         b"5\r\nCOVER\r\n0\r\n\r\n")
            await writer.drain()
            writer.close()

        internal = await asyncio.start_server(_start_internal(origin),
                                              "127.0.0.1", 0)
        internal_port = internal.sockets[0].getsockname()[1]
        server, port = await _start_frontend(internal_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"GET /music/1/cover_40x40_m.jpg HTTP/1.1\r\n"
                             b"Host: x\r\n\r\n")
                await writer.drain()
                head = await _read_headers(reader)
                assert b"chunked" in head.lower(), head
                assert b"connection:" not in head.lower(), head
                assert await _read_chunk(reader) == b"COVER"
                assert await _read_chunk(reader) == b""
                # framing is aligned: the next request on this socket works
                writer.write(b"GET /music/1/cover_40x40_m.jpg HTTP/1.1\r\n"
                             b"Host: x\r\n\r\n")
                await writer.drain()
                head = await _read_headers(reader)
                assert b"200 OK" in head, head
                assert await _read_chunk(reader) == b"COVER"
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            internal.close()
            await internal.wait_closed()

    _run(run())


def test_http10_client_gets_a_dechunked_close_delimited_body():
    """Perl switches on ``$response->request->protocol eq 'HTTP/1.1'``
    (HTTP.pm:2838-2845) and disables keep-alive for a body of unknown length
    (HTTP.pm:1191): an older client must NOT receive chunked framing."""
    async def run():
        async def origin(reader, writer, head):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n"
                         b"5\r\nFIRST\r\n4\r\nLAST\r\n0\r\n\r\n")
            await writer.drain()
            writer.close()

        internal = await asyncio.start_server(_start_internal(origin),
                                              "127.0.0.1", 0)
        internal_port = internal.sockets[0].getsockname()[1]
        server, port = await _start_frontend(internal_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"GET /stream.mp3?id=1 HTTP/1.0\r\n"
                             b"Host: x\r\n\r\n")
                await writer.drain()
                head = await _read_headers(reader)
                assert b"chunked" not in head.lower(), head
                assert b"Connection: close" in head, head
                body = await asyncio.wait_for(reader.read(), timeout=5)
                assert body == b"FIRSTLAST", body
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            internal.close()
            await internal.wait_closed()

    _run(run())


def test_head_request_is_relayed_without_a_body():
    async def run():
        async def origin(reader, writer, head):
            assert head.startswith(b"HEAD "), head
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                         b"Content-Length: 1234\r\n\r\n")
            await writer.drain()
            writer.close()

        internal = await asyncio.start_server(_start_internal(origin),
                                              "127.0.0.1", 0)
        internal_port = internal.sockets[0].getsockname()[1]
        server, port = await _start_frontend(internal_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"HEAD /html/images/icon.png HTTP/1.1\r\n"
                             b"Host: x\r\n\r\n")
                await writer.drain()
                head = await _read_headers(reader)
                assert b"200 OK" in head, head
                assert b"Content-Length: 1234" in head, head
                # the connection is reusable (no body was expected)
                writer.write(b"HEAD /html/images/icon.png HTTP/1.1\r\n"
                             b"Host: x\r\n\r\n")
                await writer.drain()
                head = await _read_headers(reader)
                assert b"200 OK" in head, head
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            internal.close()
            await internal.wait_closed()

    _run(run())


def test_large_request_body_is_streamed_to_the_internal_app():
    """The request direction is a streaming copy too: a body far bigger than
    one TCP round (1 MB, e.g. a settings form / a big CLI request) must arrive
    upstream complete and in order, without the frontend buffering it whole."""
    body = (b"x" * 1000 + b"\n") * 1049        # ~1 MB with a visible pattern
    got: list[bytes] = []

    async def run():
        async def origin(reader, writer, head):
            length = int(_head_value(head, b"content-length") or 0)
            got.append(await reader.readexactly(length))
            payload = b'{"ok":true}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                         + payload)
            await writer.drain()
            writer.close()

        internal = await asyncio.start_server(_start_internal(origin),
                                              "127.0.0.1", 0)
        internal_port = internal.sockets[0].getsockname()[1]
        server, port = await _start_frontend(internal_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"POST /jsonrpc.js HTTP/1.1\r\nHost: x\r\n"
                             b"Content-Type: application/json\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode())
                await writer.drain()
                writer.write(body)
                await writer.drain()
                head = await _read_headers(reader)
                assert b"200 OK" in head, head
                assert got == [body], (len(got[0]) if got else None, len(body))
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            internal.close()
            await internal.wait_closed()

    _run(run())


def test_expect_100_continue_is_answered_before_the_body():
    """curl and browsers wait for the interim response; without it the body
    never arrives and both sides block until a timeout."""
    got_body: list[bytes] = []

    async def run():
        async def origin(reader, writer, head):
            length = int(_head_value(head, b"content-length") or 0)
            got_body.append(await reader.readexactly(length))
            payload = b'{"ok":true}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                         + payload)
            await writer.drain()
            writer.close()

        internal = await asyncio.start_server(_start_internal(origin),
                                              "127.0.0.1", 0)
        internal_port = internal.sockets[0].getsockname()[1]
        server, port = await _start_frontend(internal_port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                body = b'{"id":1}'
                writer.write(b"POST /jsonrpc.js HTTP/1.1\r\nHost: x\r\n"
                             b"Expect: 100-continue\r\n"
                             b"Content-Type: application/json\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode())
                await writer.drain()
                interim = await _read_headers(reader)
                assert b"100 Continue" in interim, interim
                writer.write(body)
                await writer.drain()
                head = await _read_headers(reader)
                assert b"200 OK" in head, head
                assert got_body == [body], got_body
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            internal.close()
            await internal.wait_closed()

    _run(run())
