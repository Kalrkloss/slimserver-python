"""Native Cometd stream (port 9080): SqueezePlay cannot open lists.

Live symptom (2026-09-14, SqueezePlay on 9080 — the port the TLV discovery
advertises): handshake, connect and the home menu are fine, but selecting
*Alben* or *Favoriten* makes the app report "kann sich nicht mit dem Server
verbinden" and re-handshake in a loop:

    12:40:29  Body: /slim/subscribe,/slim/subscribe,/slim/request   <- list opens
    12:40:29  Body: /slim/request
    12:40:29  Body: /slim/unsubscribe
    12:40:29  Body: /slim/unsubscribe,/slim/unsubscribe             <- app gives up

Three Perl-parity deviations in the native stream transport caused that; the
ASGI path (web/app.py) already handles all three correctly:

1. A POST pipelined onto the OPEN streaming socket whose body is not a
   Bayeux array (``POST /jsonrpc.js``, HTTP/1.0 — 181 of them in the live
   log) was dropped without ANY response. Measured against the unpatched
   server: 0 bytes back, while the same POST on a plain request socket was
   answered in 73 ms. The caller blocked until its own network deadline.
   Perl supports the pipelining explicitly (HTTP.pm:2065-2072 "We also
   support pipelined cometd requets") and the non-streaming loop proxies
   such a body to the web port.

2. The non-connect POST branch drained the client's queued events into ITS
   OWN response even while the client had a live streaming connection. Jive
   keeps the /meta/connect on a second socket ("2 pools, 1 for chunked
   responses and 1 for requests", Comet.lua:184) and reads its results off
   the connect channel, so the result was STOLEN out of the open stream.
   Perl delivers a finished request into the client's registered connection
   (Cometd.pm:584-589 -> Manager::deliver_events, Manager.pm:247-263) and
   only appends ``get_pending_events`` for a client without a connection
   (Cometd.pm:645). web/app.py:447-450 guards with ``has_live_connection``.

3. A closed streaming socket REMOVED the client (``remove_if_owner``),
   throwing away its subscriptions and everything queued for it. Perl's
   webCloseHandler only unregisters the connection and arms
   ``disconnectClient`` for RETRY_DELAY * 2 (Cometd.pm:1010-1014), which the
   next /meta/(re)connect cancels (:283); web/app.py:282-292 does exactly
   that with ``release_connection``.

These tests drive the real asyncio server on ``127.0.0.1`` with an ephemeral
port, a stub slim.request handler and a stub web app — no database, and the
running dev server on :9000/:9080 is untouched.
"""

import asyncio
import json

from lyrion.networking.cometd_stream import start_cometd_server
from lyrion.web.cometd import DISCONNECT_GRACE, CometdManager

PLAYER = "1c:87:2c:47:fc:36"
RPC_RESULT = {"jsonrpc": "2.0", "id": 1,
              "result": {"version": "9.2.0", "player count": 1}}


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
            "result": {"command": cmd[0], "player_id": player,
                       "count": 9270, "item_loop": [{"text": "album"}]},
        }).encode()


class _Clock:
    """Injectable wall clock (CometdManager(clock=...))."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _run(coro):
    return asyncio.run(coro)


def _post_bytes(payload) -> bytes:
    body = json.dumps(payload).encode()
    return (b"POST /cometd HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


def _jsonrpc_bytes(payload) -> bytes:
    """A POST /jsonrpc.js as SqueezePlay sends it: HTTP/1.0, no cometd path."""
    body = json.dumps(payload).encode()
    return (b"POST /jsonrpc.js HTTP/1.0\r\nHost: x\r\n"
            b"User-Agent: SqueezePlay\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


async def _read_headers(reader) -> bytes:
    head = b""
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        head += line
        if line in (b"\r\n", b"\n", b""):
            return head


async def _read_response(reader) -> tuple[bytes, bytes]:
    """One complete HTTP response (headers, body) off a plain socket."""
    head = await _read_headers(reader)
    ctype = [ln for ln in head.split(b"\r\n")
             if ln.lower().startswith(b"content-length")]
    length = int(ctype[0].split(b":")[1]) if ctype else 0
    body = await asyncio.wait_for(reader.readexactly(length), timeout=5) \
        if length else b""
    return head, body


async def _read_chunk(reader) -> bytes:
    size = int((await asyncio.wait_for(reader.readline(), timeout=5)).strip(), 16)
    if size == 0:
        return b""
    data = await asyncio.wait_for(reader.readexactly(size), timeout=5)
    await asyncio.wait_for(reader.readexactly(2), timeout=5)  # trailing CRLF
    return data


async def _read_until(reader, marker: bytes, timeout: float = 5.0) -> bytes:
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


async def _handshake(reader, writer) -> str:
    writer.write(_post_bytes([{"channel": "/meta/handshake", "id": 1}]))
    await writer.drain()
    head, body = await _read_response(reader)
    return json.loads(body)[0]["clientId"]


async def _open_stream(reader, writer, cid: str) -> None:
    writer.write(_post_bytes([{
        "channel": "/meta/connect", "clientId": cid, "id": 2,
        "connectionType": "streaming"}]))
    await writer.drain()
    head = await _read_headers(reader)
    assert b"chunked" in head.lower(), head
    await _read_chunk(reader)          # the connect ack


def _start_web_app(seen: list | None = None):
    """Stand-in for the web app (:9000): answers /jsonrpc.js and /music/…"""

    async def handler(reader, writer):
        req = await reader.readuntil(b"\r\n\r\n")
        if seen is not None:
            seen.append(req.split(b"\r\n")[0])
        payload = json.dumps(RPC_RESULT).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                     + payload)
        await writer.drain()
        writer.close()

    return handler


def test_pipelined_jsonrpc_post_on_the_streaming_socket_is_answered():
    """Live bug #1: the app's JSON-RPC POST on the streaming socket got 0 bytes.

    Measured before the fix (port 9080, 2026-09-14 12:5x): the pipelined
    ``POST /jsonrpc.js`` produced no answer at all (0 bytes added on the
    socket within 2 s), while the same POST on a plain socket was answered in
    73 ms. SqueezePlay sends 181 such POSTs; the caller blocked until its own
    deadline and the app reported "kann sich nicht mit dem Server verbinden".
    """
    seen: list[bytes] = []

    async def run():
        web = await asyncio.start_server(_start_web_app(seen), "127.0.0.1", 0)
        web_port = web.sockets[0].getsockname()[1]
        mgr = CometdManager(_StubRPC())
        server = await start_cometd_server(mgr, "127.0.0.1", 0, web_port)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                cid = await _handshake(reader, writer)
                await _open_stream(reader, writer, cid)

                # pipelined onto the socket that carries the OPEN stream
                rpc = {"id": 1, "method": "slim.request",
                       "params": ["", ["serverstatus", "0", "1"]]}
                writer.write(_jsonrpc_bytes(rpc))
                await writer.drain()
                got = await _read_until(reader, b"9.2.0", timeout=5)
                assert b"9.2.0" in got, \
                    f"pipelined jsonrpc POST not answered, got {got[:200]!r}"
                assert b"HTTP/1.1 200 OK" in got, got[:200]
                assert seen and seen[0].startswith(b"POST /jsonrpc.js"), seen

                # the stream survives: a cometd POST is still framed as a chunk
                writer.write(_post_bytes([
                    {"channel": "/meta/ping", "clientId": cid, "id": 9}]))
                await writer.drain()
                ack = json.loads(await _read_chunk(reader))
                assert ack[0]["channel"] == "/meta/ping" and ack[0]["successful"]
                assert mgr.get(cid) is not None
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            web.close()
            await web.wait_closed()

    _run(run())


def test_request_on_a_second_socket_does_not_steal_the_stream_result():
    """Live bug #2: Jive's "2 pools" — the result must reach the stream socket.

    Socket A carries the streaming /meta/connect, socket B the /slim/request.
    Perl delivers the finished result into the client's registered connection
    (Cometd.pm:584-589 -> Manager::deliver_events), so it MUST arrive as a
    chunk on A; B's own response carries only the /slim/request ack.
    """
    async def run():
        web = await asyncio.start_server(_start_web_app(), "127.0.0.1", 0)
        web_port = web.sockets[0].getsockname()[1]
        mgr = CometdManager(_StubRPC())
        server = await start_cometd_server(mgr, "127.0.0.1", 0, web_port)
        try:
            port = server.sockets[0].getsockname()[1]
            a_r, a_w = await asyncio.open_connection("127.0.0.1", port)
            b_r, b_w = await asyncio.open_connection("127.0.0.1", port)
            try:
                cid = await _handshake(a_r, a_w)
                await _open_stream(a_r, a_w, cid)

                # the list request goes over socket B
                b_w.write(_post_bytes([{
                    "channel": "/slim/request", "clientId": cid, "id": 5,
                    "data": {"request": [PLAYER,
                                         ["browselibrary", "items", "0",
                                          "512", "menu:1", "mode:albums"]],
                             "response": f"/{cid}/slim/request"}}]))
                await b_w.drain()
                head, body = await _read_response(b_r)
                msgs = json.loads(body)
                chans = [m.get("channel") for m in msgs]
                assert "/slim/request" in chans, msgs       # the ack rides here
                assert f"/{cid}/slim/request" not in chans, (
                    "the result was drained into the POST response of the "
                    f"second socket instead of the open stream: {chans}")

                # ... and the result arrives as a chunk on the STREAM socket
                try:
                    raw = await _read_chunk(a_r)
                except asyncio.TimeoutError:      # nothing pushed: stolen
                    raise AssertionError(
                        "the result never reached the streaming socket — it was "
                        "drained into the second socket's POST response")
                assert b"item_loop" in raw, raw[:300]
                events = json.loads(raw)
                assert events[0]["channel"] == f"/{cid}/slim/request", events
                assert events[0]["id"] == 5, events
                assert events[0]["ext"] == {"priority": ""}, events
                assert "data" in events[0], events
                # Perl's handleRequest form has no 'successful' on the result
                assert "successful" not in events[0], events
            finally:
                a_w.close()
                b_w.close()
        finally:
            server.close()
            await server.wait_closed()
            web.close()
            await web.wait_closed()

    _run(run())


def test_closing_the_stream_keeps_the_client_until_the_grace_expires():
    """Live bug #3: a closed stream socket must not erase the client.

    Perl's webCloseHandler unregisters the connection and arms
    ``disconnectClient`` for RETRY_DELAY * 2 (Cometd.pm:1010-1014); the client
    keeps its subscriptions and its queued events, and only a client that
    never reconnects is reaped when the grace expires.
    """
    clock = _Clock()

    async def run():
        web = await asyncio.start_server(_start_web_app(), "127.0.0.1", 0)
        web_port = web.sockets[0].getsockname()[1]
        mgr = CometdManager(_StubRPC(), clock=clock)
        server = await start_cometd_server(mgr, "127.0.0.1", 0, web_port)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            cid = await _handshake(reader, writer)
            await _open_stream(reader, writer, cid)
            writer.write(_post_bytes([{
                "channel": "/slim/subscribe", "clientId": cid, "id": 4,
                "data": {"request": [PLAYER, ["status", "-", 10]],
                         "response": f"/{cid}/slim/playerstatus/{PLAYER}"}}]))
            await writer.drain()
            await asyncio.sleep(0.3)
            client = mgr.get(cid)
            assert client is not None and client.subscriptions, "no subscription"

            before = clock()
            writer.close()                     # the app re-pools its socket
            await asyncio.sleep(0.4)
            kept = mgr.get(cid)
            assert kept is not None, \
                "closing the stream socket removed the client"
            assert kept.subscriptions, "subscriptions were thrown away"

            # ... a client that does not come back is reaped after the grace
            clock.now = before + DISCONNECT_GRACE + 1
            assert cid in mgr.kill_idle_clients(), \
                "a client that never reconnects was not reaped"
        finally:
            server.close()
            await server.wait_closed()
            web.close()
            await web.wait_closed()

    _run(run())
