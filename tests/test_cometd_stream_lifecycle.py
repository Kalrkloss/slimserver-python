"""Cometd connection lifecycle: no silent loss, Perl's keepalive deadlines.

Measured symptom: a client "verliert still den Kontakt" — the app keeps
showing the last title/„spielt"/the counting time although another stream is
playing.  Three Perl facts fix that, each pinned here:

1. ``/meta/reconnect`` is handled EXACTLY like ``/meta/connect``
   (Perl Cometd.pm:264 ``qr{^/meta/(?:re)?connect$}``): first_event ack with
   ``advice.interval = RETRY_DELAY`` (:269-280), kills the pending
   disconnectClient timer (:283), registers the connection (:286) and keeps the
   response CHUNKED for a streaming client (:288-297).  Live 9.1.1 answers
   ``[{"channel":"/meta/reconnect",…,"advice":{"interval":5000}}, …]`` with
   ``Transfer-Encoding: chunked``.  Our native frontend used to answer the
   unknown channel with a plain ``{"successful": true}`` Content-Length reply:
   the client believed it had a stream, the server had no connection for it —
   no events, no error, no close, stale UI (live 2026-09-18 18:52:34
   ``/meta/reconnect,/meta/subscribe`` followed by ``Cometd disconnect (grace
   expired)`` 8 s later).

2. Perl sends NOTHING on a quiet streaming connect (Cometd.pm:288-297 arms no
   timer; live 9.1.1: ack chunk at once, then no byte) and closes the socket
   after ``KEEPALIVETIMEOUT => 75`` (HTTP.pm:70, re-armed per send
   :2091-2099) — measured EOF at 74.94 s.  The client then re-polls after its
   advice interval (5000 ms) and discovers a reaped registration instead of
   holding a silently dead stream.  We used to write an empty ``[]`` batch every
   60 s and never end the socket, so a client whose registration had been reaped
   kept a silent, open stream forever.

3. A long-polling connect answers with Content-Length after the client's
   timeout (Perl Cometd.pm:298-328, chunked is streaming only, :288-292) and
   unregisters the connection (``sendHTTPResponse``, :682-696) — never a
   chunked response that is held open forever.

The tests drive the real asyncio frontend on 127.0.0.1 with an ephemeral port
and stub handlers; the dev server on :9000 is untouched.
"""

import asyncio
import json
import logging

import pytest

from lyrion.networking import cometd_stream
from lyrion.networking.cometd_stream import start_cometd_server
from lyrion.web.cometd import DISCONNECT_GRACE, CometdManager

PLAYER = "1c:87:2c:47:fc:36"


class _StubRPC:
    async def handle_request(self, body: bytes) -> bytes:
        payload = json.loads(body)
        player, cmd = payload["params"]
        return json.dumps({
            "id": 1, "method": "slim.request",
            "result": {"command": cmd[0], "player_id": player, "count": 1},
        }).encode()


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


async def _read_response(reader) -> tuple[bytes, bytes]:
    head = await _read_headers(reader)
    clen = [ln for ln in head.split(b"\r\n")
            if ln.lower().startswith(b"content-length")]
    length = int(clen[0].split(b":")[1]) if clen else 0
    body = await asyncio.wait_for(reader.readexactly(length), timeout=5) \
        if length else b""
    return head, body


async def _read_chunk(reader) -> bytes:
    size = int((await asyncio.wait_for(reader.readline(), timeout=5)).strip(), 16)
    if size == 0:
        return b""
    data = await asyncio.wait_for(reader.readexactly(size), timeout=5)
    await asyncio.wait_for(reader.readexactly(2), timeout=5)
    return data


async def _handshake(reader, writer) -> str:
    writer.write(_post_bytes([{"channel": "/meta/handshake", "id": 1}]))
    await writer.drain()
    _head, body = await _read_response(reader)
    return json.loads(body)[0]["clientId"]


def _web_stub():
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        payload = b"{}"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                     + payload)
        await writer.drain()
        writer.close()

    return handler


class _Server:
    """The native frontend on an ephemeral port + the ASGI relay stand-in."""

    def __init__(self, manager):
        self.manager = manager
        self._web = None
        self._server = None
        self.port = 0

    async def __aenter__(self) -> "_Server":
        self._web = await asyncio.start_server(_web_stub(), "127.0.0.1", 0)
        web_port = self._web.sockets[0].getsockname()[1]
        self._server = await start_cometd_server(self.manager, "127.0.0.1", 0,
                                                 web_port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()
        self._web.close()
        await self._web.wait_closed()

    async def connect(self):
        return await asyncio.open_connection("127.0.0.1", self.port)


def test_streaming_reconnect_opens_the_stream_and_registers_like_perl(caplog):
    """Perl Cometd.pm:264/:283/:286 — /meta/reconnect IS a (re)connect."""

    async def run():
        mgr = CometdManager(_StubRPC())
        async with _Server(mgr) as srv:
            # the client's FIRST stream, then the app re-pools its socket
            r1, w1 = await srv.connect()
            cid = await _handshake(r1, w1)
            w1.write(_post_bytes([{
                "channel": "/meta/connect", "clientId": cid, "id": 2,
                "connectionType": "streaming"}]))
            await w1.drain()
            assert b"chunked" in (await _read_headers(r1)).lower()
            await _read_chunk(r1)                      # the connect ack
            w1.close()                                 # arms the 10 s grace
            await asyncio.sleep(0.2)
            assert mgr.get(cid) is not None
            assert mgr.get(cid).disconnect_at is not None

            # the app re-polls with /meta/reconnect on a NEW socket
            r2, w2 = await srv.connect()
            w2.write(_post_bytes([
                {"channel": "/meta/reconnect", "clientId": cid, "id": 3,
                 "connectionType": "streaming"},
                {"channel": "/meta/subscribe", "clientId": cid, "id": 4,
                 "subscription": f"/slim/playerstatus/{PLAYER}"}]))
            await w2.drain()
            head = await _read_headers(r2)
            assert b"chunked" in head.lower(), (
                "/meta/reconnect must open a streaming response like "
                f"/meta/connect (Perl Cometd.pm:264/:292); got {head!r}")
            ack = json.loads(await _read_chunk(r2))
            assert ack[0]["channel"] == "/meta/reconnect", ack
            assert ack[0]["advice"] == {"interval": 5000}, ack
            assert ack[0]["successful"] is True, ack

            # Perl :283 kills the pending disconnectClient timer and :286
            # registers this connection — the client keeps its subscriptions.
            client = mgr.get(cid)
            assert client is not None and client.disconnect_at is None, client
            assert mgr.has_live_connection(cid), \
                "the reconnected client must own a live connection"
            assert client.subscriptions, "the subscriptions were lost"

            # ... and a pushed event reaches the reconnected stream
            mgr.push(cid, {"channel": "/slim/test", "data": {"x": 1}})
            pushed = json.loads(await _read_chunk(r2))
            assert pushed[0]["channel"] == "/slim/test", pushed
            w2.close()

    asyncio.run(run())


def test_quiet_stream_is_closed_after_perls_keepalive_timeout(monkeypatch,
                                                              caplog):
    """Perl HTTP.pm:70 KEEPALIVETIMEOUT=75 — silence, then close + a log line.

    Also pins that we no longer write an EMPTY ``[]`` batch: Perl sends nothing
    at all on a quiet streaming connect (Cometd.pm:288-297).
    """
    monkeypatch.setattr(cometd_stream, "KEEPALIVE_TIMEOUT", 0.4)

    async def run():
        mgr = CometdManager(_StubRPC())
        async with _Server(mgr) as srv:
            reader, writer = await srv.connect()
            cid = await _handshake(reader, writer)
            writer.write(_post_bytes([{
                "channel": "/meta/connect", "clientId": cid, "id": 2,
                "connectionType": "streaming"}]))
            await writer.drain()
            assert b"chunked" in (await _read_headers(reader)).lower()
            await _read_chunk(reader)                  # the connect ack
            with caplog.at_level(logging.INFO):
                # Everything after the ack until EOF must be NOTHING ...
                tail = b""
                try:
                    while True:
                        part = await asyncio.wait_for(reader.read(4096),
                                                      timeout=3.0)
                        if not part:
                            break
                        tail += part
                except asyncio.TimeoutError:
                    raise AssertionError(
                        "the idle stream was never closed (Perl closes it "
                        "after KEEPALIVETIMEOUT); bytes so far: %r" % tail)
            assert tail == b"", f"the server wrote into a quiet stream: {tail!r}"
            assert mgr.get(cid) is not None, \
                "an ended stream must only arm the grace (Perl :1010-1014)"
            assert any("KEEPALIVETIMEOUT" in r.getMessage() and cid in
                       r.getMessage() for r in caplog.records), \
                "the closed stream was not logged with its client id"
            writer.close()

    asyncio.run(run())


def test_idle_closed_stream_cleans_up_after_the_perl_grace_only():
    """Perl Cometd.pm:1002-1015 — the grace, then disconnectClient."""

    async def run():
        mgr = CometdManager(_StubRPC())
        async with _Server(mgr) as srv:
            reader, writer = await srv.connect()
            cid = await _handshake(reader, writer)
            writer.write(_post_bytes([{
                "channel": "/meta/connect", "clientId": cid, "id": 2,
                "connectionType": "streaming"}]))
            await writer.drain()
            await _read_headers(reader)
            await _read_chunk(reader)
            writer.close()                            # the client goes away
            await asyncio.sleep(0.3)
            client = mgr.get(cid)
            assert client is not None, "the client was dropped immediately"
            assert client.disconnect_at is not None, \
                "no disconnectClient timer was armed (Perl :1010-1014)"
            assert abs(client.disconnect_at - client.last_seen
                       - DISCONNECT_GRACE) < 0.5, client.disconnect_at
            # only after the grace does the reaper drop it ([[clock]]-driven)
            assert cid not in mgr.kill_idle_clients(), \
                "reaped before the grace expired"
            mgr.touch(cid)
            for _c in mgr._clients.values():
                _c.disconnect_at = 0.0
            assert cid in mgr.kill_idle_clients(), \
                "a client that never reconnected was not reaped"

    asyncio.run(run())


def test_long_polling_connect_is_a_content_length_poll_not_a_stream():
    """Perl Cometd.pm:298-328 — chunked is the STREAMING branch only."""

    async def run():
        mgr = CometdManager(_StubRPC())
        async with _Server(mgr) as srv:
            reader, writer = await srv.connect()
            cid = await _handshake(reader, writer)
            writer.write(_post_bytes([{
                "channel": "/meta/connect", "clientId": cid, "id": 2,
                "connectionType": "long-polling",
                "advice": {"timeout": 0}}]))
            await writer.drain()
            head, body = await _read_response(reader)
            assert b"chunked" not in head.lower(), head
            msgs = json.loads(body)
            assert msgs[0]["channel"] == "/meta/connect", msgs
            assert msgs[0]["advice"] == {"interval": 0}, msgs
            # Perl :685 unregisters the connection after the poll answer; the
            # socket stays usable for the next poll (pipelined on it here).
            assert not mgr.has_live_connection(cid) or \
                mgr.get(cid).owner is None, "the poll connection stayed owner"
            writer.write(_post_bytes([{"channel": "/meta/ping",
                                       "clientId": cid, "id": 9}]))
            await writer.drain()
            _head, body = await _read_response(reader)
            ping = json.loads(body)
            assert ping[0]["channel"] == "/meta/ping", ping
            assert ping[0]["successful"] is True, ping
            writer.close()

    asyncio.run(run())


def test_ping_without_client_id_is_perls_no_clientid_found():
    """Perl Cometd.pm:200-211 (live: error 'No clientId found')."""

    async def run():
        mgr = CometdManager(_StubRPC())
        async with _Server(mgr) as srv:
            reader, writer = await srv.connect()
            writer.write(_post_bytes([{"channel": "/meta/ping", "id": 10}]))
            await writer.drain()
            _head, body = await _read_response(reader)
            reply = json.loads(body)[0]
            assert reply["successful"] is False, reply
            assert reply["error"] == "No clientId found", reply
            writer.close()

    asyncio.run(run())
