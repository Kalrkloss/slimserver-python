"""Native Cometd server: non-Cometd requests are proxied to the web app.

Live regression this locks down (log 2026-09-12): the discovery beacon
advertises the native Cometd port, and Jive resolves EVERY server URL
against it — artwork (``/music/3079/cover_40x40_m.jpg``), ``/html/...``,
``/stream.mp3``. The Cometd server only understood ``POST /cometd`` and
logged ``unerwartete Zeile`` for the rest, so every cover stayed blank and
the album list spun forever.

A 302 redirect is NOT enough: SqueezePlay never followed it (no follow-up
request in the server log), so the bytes must be proxied through.
"""

import asyncio

from lyrion.networking.cometd_stream import _read_http_request, _proxy_get


def _run(data: bytes):
    """Feed ``data`` to a fresh StreamReader and parse one request.

    The reader must be created inside the running loop (uvloop refuses a
    StreamReader built outside one).
    """

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await _read_http_request(reader)

    return asyncio.run(go())


def test_get_request_is_reported_for_proxying():
    raw = (
        b"GET /music/3079/cover_40x40_m.jpg HTTP/1.1\r\n"
        b"Host: 192.168.1.130:9080\r\n"
        b"User-Agent: squeezeplay\r\n\r\n"
    )
    req = _run(raw)
    assert req is not None
    assert req["method"] == "GET"
    assert req["target"] == b"/music/3079/cover_40x40_m.jpg"
    assert req["headers"][b"host"] == b"192.168.1.130:9080"


def test_head_request_is_reported_too():
    raw = b"HEAD /html/images/icon.png HTTP/1.1\r\nHost: h:9080\r\n\r\n"
    req = _run(raw)
    assert req is not None
    assert req["method"] == "HEAD"
    assert req["target"] == b"/html/images/icon.png"


def test_post_cometd_still_reads_the_body():
    body = b'[{"channel":"/meta/handshake","id":"1"}]'
    raw = (
        b"POST /cometd HTTP/1.1\r\n"
        b"Host: 192.168.1.130:9080\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    req = _run(raw)
    assert req is not None
    assert req["body"] == body
    assert "method" not in req


def test_other_verbs_are_still_ignored():
    raw = b"DELETE /whatever HTTP/1.1\r\nHost: h:9080\r\n\r\n"
    assert _run(raw) is None


# ── proxy ────────────────────────────────────────────────────────────────

class _FakeWriter:
    def __init__(self):
        self.data = b""

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        return None


def _start_origin(payload: bytes = b"JPEGDATA"):
    """A minimal HTTP server that answers every request with ``payload``."""

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
            + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()

    return handler


def test_proxy_get_returns_the_upstream_bytes():
    async def go():
        server = await asyncio.start_server(_start_origin(b"COVERBYTES"), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        writer = _FakeWriter()
        try:
            await _proxy_get(
                "GET", b"/music/1/cover_40x40_m.jpg",
                {b"host": b"192.168.1.130:9080"}, writer, port,
            )
        finally:
            server.close()
            await server.wait_closed()
        return writer.data

    data = asyncio.run(go())
    assert data.startswith(b"HTTP/1.1 200 OK")
    assert b"image/jpeg" in data
    assert data.endswith(b"COVERBYTES")


def test_proxy_get_reports_a_dead_web_app():
    async def go():
        # Port 1 is never an HTTP server → connection refused.
        writer = _FakeWriter()
        await _proxy_get("GET", b"/music/1/cover.jpg", {}, writer, 1)
        return writer.data

    assert b"502" in asyncio.run(go())


def test_proxy_get_keeps_the_client_connection_reusable():
    """Jive POOLS the thumbnail socket and drops every queued request when
    we close it (live client log: '_getArtworkThumbSink(...) error:
    keep-alive timeout', only placeholder icons). The proxied response must
    therefore NOT announce a close and must not forward the upstream's
    connection headers — the client frames the body itself (Content-Length
    or chunked)."""

    async def origin(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        payload = b"COVER"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
            b"Connection: keep-alive\r\nKeep-Alive: timeout=5\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()

    async def go():
        server = await asyncio.start_server(origin, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        writer = _FakeWriter()
        try:
            await _proxy_get("GET", b"/music/1/cover.jpg", {}, writer, port)
        finally:
            server.close()
            await server.wait_closed()
        return writer.data

    data = asyncio.run(go())
    head = data.split(b"\r\n\r\n", 1)[0].lower()
    assert b"connection:" not in head, "must not close the pooled connection"
    assert b"keep-alive" not in head
    assert b"content-length: 5" in head, "client must be able to frame the body"
    assert data.endswith(b"COVER")
