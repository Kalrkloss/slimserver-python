"""Native Cometd stream (port 9080): a request pipelined onto the OPEN
streaming socket must be served — it used to kill the controller's session.

Live symptom (2026-09-14, SqueezePlay): the home menu came up, but pressing
*Alben* or *Favoriten* made the app report "kann sich nicht mit dem Server
verbinden". The native-port log shows why:

    09:45:44  Body: /slim/subscribe,/slim/subscribe,/slim/subscribe,/slim/subscribe
    09:45:44  Body: /slim/request          (artworkspec add 40x40_m squeezeplayskin)
    ...
    09:46:05  NativeCometd: redirecting /html/images/artists_40x40_m.png to the web port
    09:46:05  lyrion.web.cometd: Cometd connection close -> removing client 14ff96e66d084eb6

Opening those screens makes Jive fetch the menu icons/artwork, and Jive
builds EVERY server URL from the advertised (Cometd) port — the GET goes
onto the very socket that carries the open streaming /meta/connect. Perl
supports exactly that (``Slim/Web/HTTP.pm:2065-2072``):

    # Check for additional pipelined GET or HEAD requests we need to process
    # We also support pipelined cometd requets, even though this is against the HTTP RFC
    if ( ${*$httpClient}{httpd_rbuf} =~ m{^(?:GET|HEAD|POST /cometd)} ) {
        main::INFOLOG && $log->is_info && $log->info("Pipelined request found, processing");
        processHTTP($httpClient);

The native stream server instead read ``request["body"]`` unconditionally in
its streaming loop. ``_read_http_request`` returns a GET/HEAD request as
``{"method", "target", "headers"}`` — no ``"body"`` key — so the KeyError
escaped the handler's ``except`` tuple, the handler died together with its
socket and the ``finally`` removed the client
(``CometdManager.remove_if_owner`` -> "connection close -> removing client").
Measured against the unpatched server: the GET got NO answer at all (the
socket was closed; 0 bytes) and the next POST on it raised
``ConnectionResetError`` in the client.

These tests drive the real asyncio server on ``127.0.0.1`` with an ephemeral
port and a stub slim.request handler — no database, and the running dev
server on :9000/:9080 is untouched.
"""

import asyncio
import json

from lyrion.networking.cometd_stream import start_cometd_server
from lyrion.web.cometd import CometdManager

ICON = b"\x89PNG\r\n\x1a\nARTISTS_40x40_ICON"  # distinguishable artwork bytes


class _StubRPC:
    """Minimal slim.request handler — records requests, echoes a result."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def handle_request(self, body: bytes) -> bytes:
        payload = json.loads(body)
        player, cmd = payload["params"]
        self.calls.append([player, cmd])
        return json.dumps({
            "id": 1, "method": "slim.request",
            "result": {"command": cmd[0], "player_id": player, "count": 1},
        }).encode()


def _run(coro):
    return asyncio.run(coro)


def _post_bytes(payload) -> bytes:
    body = json.dumps(payload).encode()
    return (b"POST /cometd HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


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
        return b""
    data = await asyncio.wait_for(reader.readexactly(size), timeout=5)
    await asyncio.wait_for(reader.readexactly(2), timeout=5)  # trailing CRLF
    return data


async def _read_until(reader, marker: bytes, timeout: float = 5.0) -> bytes:
    """Read until ``marker`` was seen (or EOF/timeout) and return the bytes."""
    buf = b""
    deadline = asyncio.get_running_loop().time() + timeout
    while marker not in buf:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=left)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


async def _open_stream(server, cid_out: list):
    """handshake + streaming /meta/connect; returns (reader, writer, cid)."""
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_post_bytes([{"channel": "/meta/handshake", "id": 1}]))
    await writer.drain()
    head = await _read_headers(reader)
    length = int([ln for ln in head.split(b"\r\n")
                  if ln.lower().startswith(b"content-length")][0].split(b":")[1])
    cid = json.loads(await asyncio.wait_for(
        reader.readexactly(length), timeout=5))[0]["clientId"]
    cid_out.append(cid)
    writer.write(_post_bytes([{
        "channel": "/meta/connect", "clientId": cid, "id": 2,
        "connectionType": "streaming"}]))
    await writer.drain()
    head = await _read_headers(reader)
    assert b"chunked" in head.lower(), head
    await _read_chunk(reader)          # the connect ack
    return reader, writer, cid


def _start_web_app(payload: bytes = ICON, seen: list | None = None):
    """A stand-in for the web app on :9000 — answers every request."""

    async def handler(reader, writer):
        line = await reader.readuntil(b"\r\n\r\n")
        if seen is not None:
            seen.append(line.split(b"\r\n")[0])
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                     + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                     + payload)
        await writer.drain()
        writer.close()

    return handler


def test_pipelined_get_on_the_streaming_socket_is_served_and_the_stream_lives():
    """The exact live failure: artwork GET on the socket with the open stream.

    Before the fix the GET was answered with nothing (the handler died with a
    KeyError) and ``remove_if_owner`` dropped the client.
    """
    seen: list[bytes] = []

    async def run():
        web = await asyncio.start_server(_start_web_app(seen=seen), "127.0.0.1", 0)
        web_port = web.sockets[0].getsockname()[1]
        rpc = _StubRPC()
        mgr = CometdManager(rpc)
        server = await start_cometd_server(mgr, "127.0.0.1", 0, web_port)
        cids: list[str] = []
        try:
            reader, writer, cid = await _open_stream(server, cids)
            try:
                # 1) the artwork GET, pipelined onto the OPEN streaming socket
                writer.write(b"GET /html/images/artists_40x40_m.png HTTP/1.1\r\n"
                             b"Host: 127.0.0.1\r\nAccept: */*\r\n\r\n")
                await writer.drain()
                got = await _read_until(reader, ICON, timeout=5)
                assert ICON in got, f"GET not answered, got {got[:200]!r}"
                assert b"HTTP/1.1 200 OK" in got, got[:200]
                assert seen, "the request never reached the web app"
                assert seen[0].startswith(b"GET /html/images/artists_40x40_m.png"), seen

                # 2) ... and the cometd stream is still usable afterwards:
                #    a follow-up POST on the same socket is answered as a chunk
                #    ("/meta/ping" -> successful, no client removal).
                writer.write(_post_bytes([
                    {"channel": "/meta/ping", "clientId": cid, "id": 9}]))
                await writer.drain()
                ack = json.loads(await _read_chunk(reader))
                assert ack[0]["channel"] == "/meta/ping", ack
                assert ack[0]["successful"] is True, ack

                # 3) the client was NOT dropped by the GET
                assert mgr.get(cid) is not None, "the pipelined GET removed the client"
                assert mgr.has_live_connection(cid)
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            web.close()
            await web.wait_closed()

    _run(run())


def test_squeezeplay_batch_pipeline_on_one_socket_is_answered():
    """SqueezePlay's pipelining: several POSTs on ONE socket, no read between.

    Live shape (2026-09-14 09:45:44): handshake, then
    ``/meta/connect,/meta/subscribe`` and a batch of
    ``subscribe, subscribe, subscribe, subscribe`` plus
    ``/slim/request`` — all pipelined while the connect response is open.
    Every message must be acknowledged, the request's result must arrive on
    its response channel and the chunk framing must stay valid.
    """

    async def run():
        web = await asyncio.start_server(_start_web_app(), "127.0.0.1", 0)
        web_port = web.sockets[0].getsockname()[1]
        rpc = _StubRPC()
        mgr = CometdManager(rpc)
        server = await start_cometd_server(mgr, "127.0.0.1", 0, web_port)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                # handshake, read, then pipeline everything else (jive style)
                writer.write(_post_bytes([{"channel": "/meta/handshake",
                                           "id": 1}]))
                await writer.drain()
                head = await _read_headers(reader)
                length = int([ln for ln in head.split(b"\r\n")
                              if ln.lower().startswith(b"content-length")][0]
                             .split(b":")[1])
                cid = json.loads(await asyncio.wait_for(
                    reader.readexactly(length), timeout=5))[0]["clientId"]
                player = "1c:87:2c:47:fc:36"

                writer.write(_post_bytes([
                    {"channel": "/meta/connect", "clientId": cid, "id": 2,
                     "connectionType": "streaming"},
                    {"channel": "/meta/subscribe", "clientId": cid, "id": 3,
                     "subscription": f"/{cid}/**"},
                ]))
                writer.write(_post_bytes([
                    {"channel": "/slim/subscribe", "clientId": cid, "id": 4,
                     "data": {"request": [player, ["status", "-", 10]],
                              "response": f"/{cid}/slim/playerstatus/{player}"}},
                    {"channel": "/slim/subscribe", "clientId": cid, "id": 5,
                     "data": {"request": [player, ["menustatus"]],
                              "response": f"/{cid}/slim/menustatus/{player}"}},
                    {"channel": "/slim/request", "clientId": cid, "id": 6,
                     "data": {"request": [player, ["status", 0, 200]],
                              "response": f"/{cid}/slim/request/1"}},
                ]))
                await writer.drain()

                head = await _read_headers(reader)
                assert b"chunked" in head.lower(), head
                # frame 1: subscribe ack + the single connect ack
                first = json.loads(await _read_chunk(reader))
                chans = [m["channel"] for m in first]
                assert "/meta/connect" in chans and "/meta/subscribe" in chans, first
                assert chans.count("/meta/connect") == 1, first

                # frames 2..n: the batch acks and the request result; the
                # framing must stay parseable (a wrong chunk size corrupts it)
                seen = []
                for _ in range(3):
                    try:
                        data = await _read_chunk(reader)
                    except asyncio.TimeoutError:
                        break
                    if not data:
                        break
                    seen += [m["channel"] for m in json.loads(data)]
                assert f"/{cid}/slim/request/1" in seen, seen
                assert f"/{cid}/slim/playerstatus/{player}" in seen, seen
                assert mgr.get(cid) is not None
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            web.close()
            await web.wait_closed()

    _run(run())
