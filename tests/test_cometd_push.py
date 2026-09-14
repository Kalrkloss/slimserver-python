"""Cometd push semantics — in-process tests (no running server).

Covers the SqueezePlay / jive "pause icon" regression family:

* jive registers a catch-all channel ``/<clientId>/**`` via
  ``/meta/subscribe`` AND targeted channels via ``/slim/subscribe``
  (``data.response = '/<clientId>/slim/<query>'``).
* a /meta/subscribe glob is a pure channel registration — it must NOT
  dispatch a query or emit a junk event (Perl registers channels only).
* every event is delivered EXACTLY ONCE (Perl manager::get_pending_events
  clears the queue); a second poll sees nothing.
* events queued before ``/meta/connect`` are flushed on connect, once,
  in order.
* a targeted subscription only receives its own channel's events.

These run against CometdManager directly, so they exercise the same
delivery semantics the native stream server (cometd_stream.py) and the
ASGI handler (web/app.py) rely on.
"""

import asyncio
import json

from lyrion.networking.cometd_stream import start_cometd_server
from lyrion.web.cometd import (
    LONG_POLLING_AUTOKILL,
    CometdManager,
    _channel_matches,
)


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
            "result": {"command": cmd[0], "player_id": player, "mode": "play"},
        }).encode()


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    """Injectable wall clock — ``CometdManager(clock=...)``.

    Lets a test drive Perl's disconnect grace (RETRY_DELAY * 2, Cometd.pm:1010
    -1014) without sleeping ten real seconds.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_channel_glob_matches_perl_semantics():
    """``/foo/**`` and ``/foo/*`` mirror Perl Manager::add_channels.

    Perl rewrites ``/foo/**`` to ``^/foo/`` and ``/foo/*`` to
    ``^/foo/[^/]+`` when registering a channel; anything without that
    glob suffix is an exact channel key.  The Perl regex for ``/foo/*``
    is not end-anchored, so it also matches ``/foo/bar/boo`` (Perl's own
    comment claims the opposite, the regex and this code agree; verified
    against Perl 5.38).
    """
    assert _channel_matches("/1abc/**", "/1abc/slim/serverstatus")
    assert _channel_matches("/1abc/**", "/1abc/slim/playerstatus/aa:bb")
    assert not _channel_matches("/1abc/**", "/other/slim/serverstatus")
    assert not _channel_matches("/1abc/**", "/1abc")  # needs the trailing /
    assert _channel_matches("/slim/**", "/slim/serverstatus")
    assert _channel_matches("/slim/*", "/slim/serverstatus")
    assert _channel_matches("/slim/*", "/slim/serverstatus/extra")  # prefix
    assert not _channel_matches("/slim/serverstatus", "/slim/playerstatus/x")
    assert _channel_matches("/slim/serverstatus", "/slim/serverstatus")


def test_bare_double_star_is_a_python_extension_not_perl():
    """A bare ``**``/``/**`` is tolerated here, but is NOT Perl semantics.

    Perl only rewrites the ``/<something>/**`` and ``/<something>/*``
    forms; a bare ``**`` is passed to Tie::RegexpHash unchanged and does
    not compile as a regex (``perl -e '"/x/y" =~ "**"'`` dies with
    "Quantifier follows nothing in regex").  No real client sends it —
    jive registers ``/<clientId>/**`` (Comet.lua:702).  The bare forms
    are matched anyway as a Python-side courtesy, not as Perl parity.
    """
    assert _channel_matches("**", "/anything/at/all")
    assert _channel_matches("/**", "/anything/at/all")


def test_handshake_and_subscribe_glob_and_targeted():
    async def run():
        rpc = _StubRPC()
        mgr = CometdManager(rpc)
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]

        # jive: catch-all glob via /meta/subscribe (top-level subscription)
        meta_ack = (await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }]))[0]
        assert meta_ack["successful"] is True
        assert meta_ack["subscription"] == f"/{cid}/**"
        assert f"/{cid}/**" in mgr.get(cid).subscriptions

        # jive: targeted query via /slim/subscribe (data.response channel)
        response = f"/{cid}/slim/serverstatus"
        sub_ack = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 3, "data": {
                "request": ["", ["serverstatus", "0", "100", "subscribe:60"]],
                "response": response,
            },
        }]))[0]
        assert sub_ack["successful"] is True
        assert response in mgr.get(cid).subscriptions
        return cid

    cid = _run(run())
    assert cid


def test_meta_subscribe_glob_emits_no_junk_event():
    """A pure ``/<cid>/**`` registration must not dispatch/emit data."""
    async def run():
        rpc = _StubRPC()
        mgr = CometdManager(rpc)
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }])
        events = await mgr.wait_for_events(cid, timeout=0)
        return rpc.calls, events

    calls, events = _run(run())
    assert calls == [], "glob /meta/subscribe must not dispatch a query"
    assert events == [], "glob /meta/subscribe must not emit a seed event"


def test_glob_subscription_receives_serverstatus_once():
    """jive with both glob + targeted sub gets exactly ONE push."""
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }])
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 3, "data": {
                "request": ["", ["serverstatus", "0", "100", "subscribe:60"]],
                "response": f"/{cid}/slim/serverstatus",
            },
        }])
        await mgr.wait_for_events(cid, timeout=0)  # drain seed event

        await mgr.notify_server_status()
        return cid, await mgr.wait_for_events(cid, timeout=0)

    cid, events = _run(run())
    assert len(events) == 1, f"expected exactly one push, got {events}"
    assert events[0]["channel"] == f"/{cid}/slim/serverstatus"


def test_pure_glob_subscriber_receives_serverstatus():
    """A ``/<cid>/**`` pattern matches the serverstatus event channel."""
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }])
        await mgr.notify_server_status()
        return cid, await mgr.wait_for_events(cid, timeout=0)

    cid, events = _run(run())
    assert len(events) == 1
    assert events[0]["channel"] == f"/{cid}/slim/serverstatus"


def test_event_delivered_exactly_once_across_polls():
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        mgr.push(cid, {"channel": f"/{cid}/slim/request", "data": {"n": 1}, "id": 7})
        first = await mgr.wait_for_events(cid, timeout=0)
        second = await mgr.wait_for_events(cid, timeout=0)
        third = await mgr.wait_for_events(cid, timeout=0)
        return first, second, third

    first, second, third = _run(run())
    assert [e["id"] for e in first] == [7]
    assert second == [], "second poll must not re-deliver"
    assert third == [], "third poll must not re-deliver"


def test_buffered_events_flushed_once_at_connect_in_order():
    """Events queued before /meta/connect are flushed once, in order.

    Mirrors cometd_stream.py: connect replies with acks + a single
    wait_for_events(timeout=0), the push task then sees an empty queue.
    """
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        mgr.push(cid, {"channel": f"/{cid}/slim/request", "data": {"seq": 1}, "id": 1})
        mgr.push(cid, {"channel": f"/{cid}/slim/request", "data": {"seq": 2}, "id": 2})

        # connect flush
        flushed = await mgr.wait_for_events(cid, timeout=0)
        # push task's first wait
        after = await mgr.wait_for_events(cid, timeout=0)
        return flushed, after

    flushed, after = _run(run())
    assert [e["data"]["seq"] for e in flushed] == [1, 2], "order must be preserved"
    assert after == [], "connect flush must not leave duplicates for the push task"


def test_targeted_subscriptions_only_receive_their_channel():
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        player = "02:11:22:33:44:55"

        # serverstatus-only subscriber
        a = mgr.get_or_create(client_id := cid)
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2, "data": {
                "request": ["", ["serverstatus", "0", "100"]],
                "response": f"/{client_id}/slim/serverstatus",
            },
        }])
        # playerstatus-only subscriber
        b = mgr.get_or_create(cid2 := cid + "b")
        b.subscriptions[f"/{cid2}/slim/playerstatus/{player}"] = {
            "request": [player, ["playerstatus", "-", "1"]]}

        await mgr.wait_for_events(a.client_id, timeout=0)

        await mgr.notify_server_status()
        await mgr.notify_player_status(player)

        a_events = await mgr.wait_for_events(a.client_id, timeout=0)
        b_events = await mgr.wait_for_events(b.client_id, timeout=0)
        return a_events, b_events

    a_events, b_events = _run(run())
    assert all("serverstatus" in e["channel"] for e in a_events)
    assert len(a_events) == 1, f"serverstatus client got {a_events}"
    assert all("playerstatus" in e["channel"] for e in b_events)
    assert len(b_events) == 1, f"playerstatus client got {b_events}"


# ---------------------------------------------------------------------------
# End-to-end over the native streaming server (ephemeral port, in-process —
# never touches the running LMS on :9000/:9080).
# ---------------------------------------------------------------------------

def test_native_stream_single_delivery_and_chunk_framing():
    async def read_headers(reader):
        head = b""
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            head += line
            if line in (b"\r\n", b"\n", b""):
                return head

    async def read_chunk(reader):
        size = int((await asyncio.wait_for(reader.readline(), timeout=5)).strip(), 16)
        if size == 0:
            return b""
        data = await asyncio.wait_for(reader.readexactly(size), timeout=5)
        await asyncio.wait_for(reader.readexactly(2), timeout=5)  # trailing CRLF
        return data

    async def run():
        from lyrion.networking.cometd_stream import start_cometd_server

        rpc = _StubRPC()
        mgr = CometdManager(rpc)
        server = await start_cometd_server(mgr, "127.0.0.1", 0)

        def post(path, payload):
            body = json.dumps(payload).encode()
            return (f"POST {path} HTTP/1.1\r\nHost: x\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n").encode() + body

        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                # 1) handshake (own POST, like jive) -> Content-Length reply
                writer.write(post("/cometd", [{"channel": "/meta/handshake",
                                               "id": 1}]))
                await writer.drain()
                head = await read_headers(reader)
                clen = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        clen = int(line.split(b":", 1)[1].strip())
                hs = json.loads(await asyncio.wait_for(
                    reader.readexactly(clen), timeout=5))
                cid = hs[0]["clientId"]

                # 2) streaming /meta/connect -> chunked headers + first chunk
                writer.write(post("/cometd", [{
                    "channel": "/meta/connect", "clientId": cid, "id": 2,
                    "connectionType": "streaming"}]))
                await writer.drain()
                head = await read_headers(reader)
                assert b"chunked" in head.lower(), head
                first = json.loads(await read_chunk(reader))
                assert sum(1 for m in first if m["channel"] == "/meta/connect") == 1, first
                assert first[-1]["channel"] == "/meta/connect"
                assert first[-1]["successful"] is True

                # 3) glob subscribe + another connect on the open stream:
                #    the reply must be a well-formed chunk carrying the ack.
                writer.write(post("/cometd", [
                    {"channel": "/meta/subscribe", "clientId": cid, "id": 3,
                     "subscription": f"/{cid}/**"},
                    {"channel": "/meta/connect", "clientId": cid, "id": 4},
                ]))
                await writer.drain()
                ack = json.loads(await read_chunk(reader))
                channels = [m["channel"] for m in ack]
                assert "/meta/subscribe" in channels, ack
                assert "/meta/connect" in channels, ack
                # ... exactly ONE connect ack (the follow-up connect is
                # answered by the stream handler, not by handle_messages)
                assert sum(1 for c in channels if c == "/meta/connect") == 1, ack
                # glob registration produced no junk event
                assert not any(m.get("data") for m in ack), ack

                # 4) a push is delivered ONCE on the concrete channel
                await mgr.notify_server_status()
                ev = json.loads(await read_chunk(reader))
                assert len(ev) == 1, ev
                assert ev[0]["channel"] == f"/{cid}/slim/serverstatus"
                # nothing queued afterwards
                assert await mgr.wait_for_events(cid, timeout=0) == []
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    _run(run())


# ---------------------------------------------------------------------------
# Review fixes (R0-B): one connect ack, exactly-once per CLIENT, resource
# cleanup on abrupt close / truncated body, framed ack for non-connect POSTs.
# ---------------------------------------------------------------------------


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


async def _open_stream(server):
    """Handshake + streaming /meta/connect; returns (reader, writer, cid)."""
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_post_bytes([{"channel": "/meta/handshake", "id": 1}]))
    await writer.drain()
    head = await _read_headers(reader)
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            clen = int(line.split(b":", 1)[1].strip())
    hs = json.loads(await asyncio.wait_for(reader.readexactly(clen), timeout=5))
    cid = hs[0]["clientId"]
    writer.write(_post_bytes([{"channel": "/meta/connect", "clientId": cid,
                               "id": 2, "connectionType": "streaming"}]))
    await writer.drain()
    head = await _read_headers(reader)
    assert b"chunked" in head.lower(), head
    return reader, writer, cid


def test_handle_messages_never_answers_meta_connect():
    """The transport writes the one and only /meta/connect ack."""
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        return await mgr.handle_messages([
            {"channel": "/meta/subscribe", "clientId": cid, "id": 2,
             "subscription": f"/{cid}/**"},
            {"channel": "/meta/connect", "clientId": cid, "id": 3},
        ])

    replies = _run(run())
    assert [r["channel"] for r in replies] == ["/meta/subscribe"], replies


def test_connect_chunk_contains_exactly_one_connect_ack():
    async def run():
        mgr = CometdManager(_StubRPC())
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            reader, writer, cid = await _open_stream(server)
            try:
                first = json.loads(await _read_chunk(reader))
                writer.write(_post_bytes([
                    {"channel": "/meta/subscribe", "clientId": cid, "id": 3,
                     "subscription": f"/{cid}/**"},
                    {"channel": "/meta/connect", "clientId": cid, "id": 4},
                ]))
                await writer.drain()
                follow = json.loads(await _read_chunk(reader))
                return first, follow
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    first, follow = _run(run())
    for chunk in (first, follow):
        assert sum(1 for m in chunk if m["channel"] == "/meta/connect") == 1, chunk


def test_cidless_exact_plus_glob_delivers_once_per_client():
    """A cid-less exact channel + a glob must not double-deliver."""
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        await mgr.handle_messages([{"channel": "/meta/subscribe", "clientId": cid,
                                    "id": 2, "subscription": f"/{cid}/**"}])
        await mgr.handle_messages([{"channel": "/meta/subscribe", "clientId": cid,
                                    "id": 3, "subscription": "/slim/serverstatus"}])
        await mgr.wait_for_events(cid, timeout=0)  # seed of the exact sub
        await mgr.notify_server_status()
        return await mgr.wait_for_events(cid, timeout=0)

    events = _run(run())
    assert len(events) == 1, f"exactly once per client, got {events}"
    # the channel the client explicitly subscribed to wins over the glob
    assert events[0]["channel"] == "/slim/serverstatus"


def test_cidless_exact_plus_glob_playerstatus_delivers_once():
    player = "02:11:22:33:44:55"

    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        await mgr.handle_messages([{"channel": "/meta/subscribe", "clientId": cid,
                                    "id": 2, "subscription": f"/{cid}/**"}])
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 3,
            "subscription": f"/slim/playerstatus/{player}"}])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(player)
        return await mgr.wait_for_events(cid, timeout=0)

    events = _run(run())
    assert len(events) == 1, f"exactly once per client, got {events}"
    assert events[0]["channel"] == f"/slim/playerstatus/{player}"


def test_queue_has_no_cap_like_perl_and_dead_clients_are_reaped_instead():
    """Perl's ``queue_events`` (Manager.pm:181-189) never drops an event.

    Perl keeps every event for the client and drops the *client* when it stops
    polling (LONG_POLLING_AUTOKILL -> disconnectClient, Cometd.pm:49/:693).
    There used to be a Python-side cap of 256 events that silently threw the
    oldest ones away — a client got less than Perl would have delivered.
    """
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        n = 512
        for i in range(n):
            mgr.push(cid, {"channel": "/x", "data": {"n": i}, "id": i})
        queued = await mgr.wait_for_events(cid, timeout=0)
        # a client that never comes back is not kept forever either: Perl's
        # autokill drops it (and with it its queue) after 180 s.
        reaped = mgr.kill_idle_clients(timeout=0.0)
        return queued, reaped, mgr.get(cid)

    queued, reaped, client = _run(run())
    assert len(queued) == 512, f"events were dropped: {len(queued)}"
    assert [e["data"]["n"] for e in queued] == list(range(512))
    assert reaped and client is None, "an idle client was not reaped"


def test_abrupt_close_keeps_the_client_until_the_grace_expires():
    """No /meta/disconnect (app killed, network gone): Perl's grace decides.

    webCloseHandler (Cometd.pm:1010-1014) unregisters the connection and arms
    ``disconnectClient`` for RETRY_DELAY * 2 = 10 s, so a client that only
    re-pooled its streaming socket keeps its subscriptions and its queued
    events and does NOT have to re-handshake. The expired grace is what drops
    the client and its queue — that is the bound this test used to pin by
    removing the client on the spot.
    """
    clock = _Clock()

    async def run():
        mgr = CometdManager(_StubRPC(), clock=clock)
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            reader, _writer, cid = await _open_stream(server)
            await _read_chunk(reader)
            assert mgr.get(cid) is not None
            _writer.close()  # abrupt: no /meta/disconnect is ever sent
            try:
                await _writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            for _ in range(60):
                c = mgr.get(cid)
                if c is not None and c.disconnect_at is not None:
                    break
                await asyncio.sleep(0.05)
            client = mgr.get(cid)
            disconnect_at = client.disconnect_at if client is not None else None
            if disconnect_at is not None:
                clock.now = disconnect_at + 0.1
            reaped = mgr.kill_idle_clients()
            return cid, client, disconnect_at, reaped
        finally:
            server.close()
            await server.wait_closed()

    cid, client, disconnect_at, reaped = _run(run())
    assert client is not None, "a closed stream must not erase the client"
    assert disconnect_at is not None, "Perl's disconnectClient timer is missing"
    assert reaped == [cid], "the expired grace must reap the client"


def test_truncated_body_closes_cleanly():
    """A body shorter than Content-Length must not leak the socket or
    surface an unhandled asyncio.IncompleteReadError. The client itself is
    kept for Perl's disconnect grace (Cometd.pm:1010-1014), not erased."""
    clock = _Clock()

    async def run():
        mgr = CometdManager(_StubRPC(), clock=clock)
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        loop = asyncio.get_running_loop()
        captured: list = []
        prev = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: captured.append(ctx))
        try:
            reader, writer, cid = await _open_stream(server)
            await _read_chunk(reader)
            # Content-Length claims 100 bytes, only 5 arrive, then EOF
            writer.write(b"POST /cometd HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Length: 100\r\n\r\n[1,2,")
            await writer.drain()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            for _ in range(60):
                c = mgr.get(cid)
                if c is not None and c.disconnect_at is not None:
                    break
                await asyncio.sleep(0.05)
            client = mgr.get(cid)
            disconnect_at = client.disconnect_at if client is not None else None
            if disconnect_at is not None:
                clock.now = disconnect_at + 0.1
            reaped = mgr.kill_idle_clients()
            return client, disconnect_at, reaped, captured
        finally:
            loop.set_exception_handler(prev)
            server.close()
            await server.wait_closed()

    client, disconnect_at, reaped, captured = _run(run())
    names = [type(ctx.get("exception")).__name__
             for ctx in captured if ctx.get("exception")]
    assert "IncompleteReadError" not in names, captured
    assert "EOFError" not in names, captured
    assert client is not None, "a truncated body must not erase the client"
    assert reaped == [client.client_id], "the grace must reap it"


def test_non_connect_post_on_stream_is_answered_as_a_chunk():
    """A POST while the stream is open must be answered as a CHUNK.

    A complete HTTP response (``HTTP/1.1 200 OK`` + Content-Length)
    written into the already-open ``Transfer-Encoding: chunked`` body
    corrupts the framing — the same argument the connect path fixed.
    """
    async def run():
        mgr = CometdManager(_StubRPC())
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            reader, writer, cid = await _open_stream(server)
            try:
                await _read_chunk(reader)
                writer.write(_post_bytes([{
                    "channel": "/slim/request", "clientId": cid, "id": 5,
                    "data": {"request": ["", ["serverstatus", "0", "1"]],
                             "response": f"/{cid}/slim/request"},
                }]))
                await writer.drain()
                # ValueError here means a bare HTTP status line was written
                return await _read_chunk(reader)
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    raw = _run(run())
    assert b"HTTP/1.1" not in raw, raw
    payload = json.loads(raw)
    assert any(m.get("channel") == "/slim/request" for m in payload), payload


# ---------------------------------------------------------------------------
# R0.5 fixes — client cleanup on ALL paths (handshake-only, non-connect, ASGI,
# disconnect) + Perl's LONG_POLLING_AUTOKILL idle reaper (review P2-1, repros
# H/I) and the documented connect-ack position deviation (review P3-4).
# ---------------------------------------------------------------------------


def _fake_clock(start: float = 1000.0):
    """Returns (now_list, clock) — a mutable clock for idle-timeout tests."""
    now = [start]
    return now, (lambda: now[0])


async def _read_clen_reply(reader) -> bytes:
    """Read one Content-Length framed /cometd reply."""
    head = await _read_headers(reader)
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            clen = int(line.split(b":", 1)[1].strip())
    return await asyncio.wait_for(reader.readexactly(clen), timeout=5)


def test_handshake_only_client_is_reaped_after_idle_timeout():
    """A client that handshakes but never /meta/connect is dropped once it
    exceeds Perl's LONG_POLLING_AUTOKILL window (review P2-1, repro H)."""
    now, clock = _fake_clock()

    async def run():
        mgr = CometdManager(_StubRPC(), clock=clock)
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        assert mgr.get(cid) is not None
        # one second short of the window: still alive
        now[0] += LONG_POLLING_AUTOKILL - 1
        assert mgr.kill_idle_clients() == []
        assert mgr.get(cid) is not None
        now[0] += 2
        return cid, mgr.kill_idle_clients(), mgr.get(cid)

    cid, reaped, client = _run(run())
    assert reaped == [cid], "handshake-only client must be reaped"
    assert client is None


def test_idle_reaper_spares_client_with_open_streaming_connection():
    """A silent but open streaming connection must never be reaped."""
    now, clock = _fake_clock()

    async def run():
        mgr = CometdManager(_StubRPC(), clock=clock)
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        mgr.connection_open(cid)
        now[0] += 10 * LONG_POLLING_AUTOKILL
        spared = mgr.kill_idle_clients()
        mgr.connection_closed(cid)
        now[0] += LONG_POLLING_AUTOKILL + 1
        reaped = mgr.kill_idle_clients()
        return cid, spared, reaped, mgr.get(cid)

    cid, spared, reaped, client = _run(run())
    assert spared == []
    assert reaped == [cid]
    assert client is None


def test_native_handshake_only_post_close_keeps_client(caplog):
    """P0 case (a): a /meta/handshake POST ending must NOT remove the client.

    A Bayeux client spreads its POSTs over several sockets — HTTP
    long-polling keeps a response pool (the /meta/connect) and a request
    pool (handshake/subscribe/request; Comet.lua:184 "2 pools, 1 for
    chunked responses and 1 for requests"). The handshake POST socket
    therefore ends as soon as its reply is written, which is normal, not
    a disconnect. Perl Cometd.pm:286 registers the connection with the
    manager ONLY in the /meta/(re)connect branch, and webCloseHandler
    (Cometd.pm:1003) removes a client only when the lost connection is
    that registered connection.
    """
    caplog.set_level("INFO")

    async def run():
        mgr = CometdManager(_StubRPC())
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(_post_bytes([{"channel": "/meta/handshake", "id": 1}]))
            await writer.drain()
            hs = json.loads(await _read_clen_reply(reader))
            cid = hs[0]["clientId"]
            assert mgr.get(cid) is not None
            writer.close()  # handshake POST socket ends (no connect here)
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            # Give the handler's ``finally`` ample time to (wrongly) remove
            # the client — the connection loops try exactly once.
            await asyncio.sleep(0.3)
            return cid, mgr.get(cid)
        finally:
            server.close()
            await server.wait_closed()

    cid, client = _run(run())
    assert f"removing client {cid}" not in caplog.text, \
        f"handshake-POST close removed the client; log: {caplog.text}"
    assert client is not None, (
        "handshake-POST close must not drop the client "
        f"(log: {caplog.text})")


def test_native_subscribe_post_close_keeps_client_and_subscriptions(caplog):
    """P0 case (b): a /meta/subscribe POST ending must NOT remove the client
    or its subscriptions.

    Same rule as case (a): the subscribe POST is a request-pool socket; the
    client's /meta/connect lives on another socket and keeps ownership.
    """
    caplog.set_level("INFO")
    player = "02:11:22:33:44:55"

    async def run():
        mgr = CometdManager(_StubRPC())
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(_post_bytes([{"channel": "/meta/handshake", "id": 1}]))
            await writer.drain()
            cid = json.loads(await _read_clen_reply(reader))[0]["clientId"]

            # non-connect POST that stores a subscription
            writer.write(_post_bytes([{
                "channel": "/meta/subscribe", "clientId": cid, "id": 2,
                "subscription": f"/{cid}/slim/playerstatus/{player}",
            }]))
            await writer.drain()
            await _read_clen_reply(reader)
            assert f"/{cid}/slim/playerstatus/{player}" in mgr.get(cid).subscriptions

            writer.close()  # subscribe POST socket ends, no /meta/disconnect
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.3)
            client = mgr.get(cid)
            subs = dict(client.subscriptions) if client else {}
            if client is not None:
                await mgr.notify_player_status(player)
                events = await mgr.wait_for_events(cid, timeout=0)
            else:
                events = []
            return cid, client, subs, events
        finally:
            server.close()
            await server.wait_closed()

    cid, client, subs, events = _run(run())
    assert f"removing client {cid}" not in caplog.text, \
        f"subscribe-POST close removed the client; log: {caplog.text}"
    assert client is not None, (
        "subscribe-POST close must not drop the client "
        f"(log: {caplog.text})")
    assert f"/{cid}/slim/playerstatus/{player}" in subs, subs
    assert any("playerstatus" in e["channel"] for e in events), events


def test_asgi_streaming_connect_removes_client_on_abort():
    """ASGI streaming /meta/connect: a failed send (client gone) must drop
    the client — the ASGI path never sees /meta/disconnect (review P2-1d)."""
    async def run():
        from lyrion.web.app import _handle_cometd

        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        body = json.dumps([{"channel": "/meta/connect", "clientId": cid,
                            "id": 2, "connectionType": "streaming"}]).encode()
        sent = []

        async def receive():
            if not sent:
                sent.append(1)
                return {"type": "http.request", "body": body,
                        "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):  # client vanished before the response
            raise RuntimeError("client gone")

        await _handle_cometd(mgr, "/cometd", receive, send)
        return cid, mgr.get(cid)

    _cid, client = _run(run())
    assert client is None, "aborted ASGI stream must drop the client"


def test_asgi_long_poll_removes_client_on_aborted_response():
    """ASGI long-poll /meta/connect: a failed response send drops the
    client (silent-success leak, review P2-1d)."""
    async def run():
        from lyrion.web.app import _handle_cometd

        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        mgr.push(cid, {"channel": f"/{cid}/slim/x", "data": {}, "id": 0})
        body = json.dumps([{"channel": "/meta/connect", "clientId": cid,
                            "id": 2}]).encode()
        sent = []

        async def receive():
            if not sent:
                sent.append(1)
                return {"type": "http.request", "body": body,
                        "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            raise RuntimeError("client gone")

        await _handle_cometd(mgr, "/cometd", receive, send)
        return cid, mgr.get(cid)

    _cid, client = _run(run())
    assert client is None, "aborted long-poll must drop the client"


def test_connect_ack_position_is_documented_perl_deviation():
    """Perl forces the /meta/(re)connect reply to be the FIRST event of the
    response (Cometd.pm:279-292, ``first_event``). Python keeps it after the
    batch acks. That is a deliberate, documented deviation: the
    Android/libcometd clients could not be exercised in this test suite, so
    the shipped order is pinned here instead of being changed silently
    (review P3-4). Flip this test and both transports together when a device
    can verify the reordering."""
    async def run():
        mgr = CometdManager(_StubRPC())
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            reader, writer, cid = await _open_stream(server)
            try:
                await _read_chunk(reader)  # first chunk: connect ack only
                writer.write(_post_bytes([
                    {"channel": "/meta/subscribe", "clientId": cid, "id": 3,
                     "subscription": f"/{cid}/**"},
                    {"channel": "/meta/connect", "clientId": cid, "id": 4},
                ]))
                await writer.drain()
                return json.loads(await _read_chunk(reader))
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    follow = _run(run())
    assert [m["channel"] for m in follow] == ["/meta/subscribe", "/meta/connect"]
    assert follow[0]["channel"] != "/meta/connect"


# ---------------------------------------------------------------------------
# P0 regression (commit 0023b9930): connection cleanup may only remove the
# client whose /meta/connect that connection handled.
#
# The regression: cometd_stream.py made EVERY cid a POST touched owned by that
# socket and removed them all in its ``finally``. A client that spreads its
# POSTs over sockets (HTTP long-polling: Comet.lua:184 "2 pools, 1 for chunked
# responses and 1 for requests") lost clientId and subscriptions as soon as a
# handshake/subscribe/request POST ended, so notify_player_status found zero
# clients (protocol.py:2550 "STAT … → notify_player_status (0 cometd
# clients)") and pushed nothing.  Perl registers the connection with the
# manager ONLY in the /meta/(re)connect branch (Cometd.pm:286) and removes a
# client only when that connection closes (webCloseHandler, Cometd.pm:1003).
# ---------------------------------------------------------------------------

_PLAYER = "02:11:22:33:44:55"


async def _stream_connect_cid(server, cid, request_id=9):
    """New socket + streaming /meta/connect for an EXISTING cid.

    Returns (reader, writer) with the connect ack chunk already consumed.
    """
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_post_bytes([{
        "channel": "/meta/connect", "clientId": cid, "id": request_id,
        "connectionType": "streaming"}]))
    await writer.drain()
    head = await _read_headers(reader)
    assert b"chunked" in head.lower(), head
    first = json.loads(await _read_chunk(reader))
    assert any(m.get("channel") == "/meta/connect" for m in first), first
    return reader, writer


def test_p0_connect_socket_close_unregisters_the_client():
    """P0 case (c): the connection that processed /meta/connect owns the
    client; its close unregisters that connection (Perl webCloseHandler,
    Cometd.pm:1003) while the client's other POST socket is still open — and
    the client itself survives for the disconnect grace (:1010-1014) instead
    of being erased, so its subscriptions are not lost."""
    player = _PLAYER
    clock = _Clock()

    async def run():
        mgr = CometdManager(_StubRPC(), clock=clock)
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            # socket 1: handshake + streaming connect (the "response" pool)
            _r1, w1, cid = await _open_stream(server)
            # socket 2: subscribe POST (the "request" pool), left open
            reader2, w2 = await asyncio.open_connection("127.0.0.1", port)
            w2.write(_post_bytes([{
                "channel": "/meta/subscribe", "clientId": cid, "id": 3,
                "subscription": f"/{cid}/slim/playerstatus/{player}"}]))
            await w2.drain()
            await _read_clen_reply(reader2)

            w1.close()  # the CONNECT socket goes away
            try:
                await w1.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            for _ in range(60):
                c = mgr.get(cid)
                if c is not None and c.disconnect_at is not None:
                    break
                await asyncio.sleep(0.05)
            client = mgr.get(cid)
            disconnect_at = client.disconnect_at if client is not None else None
            if disconnect_at is not None:
                clock.now = disconnect_at + 0.1
            reaped = mgr.kill_idle_clients()
            kept = bool(client and client.subscriptions)
            w2.close()
            try:
                await w2.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return cid, client, disconnect_at, kept, reaped
        finally:
            server.close()
            await server.wait_closed()

    cid, client, disconnect_at, kept, reaped = _run(run())
    assert client is not None, "closing the /meta/connect socket erased the client"
    assert disconnect_at is not None, "Perl's disconnectClient timer is missing"
    assert kept, "the client's subscriptions were thrown away"
    assert reaped == [cid], "the expired grace must reap the client"


def test_p0_meta_disconnect_removes_client():
    """P0 case (d): an explicit /meta/disconnect removes the client."""
    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/slim/playerstatus/{_PLAYER}"}])
        replies = await mgr.handle_messages([{
            "channel": "/meta/disconnect", "clientId": cid, "id": 3}])
        return cid, replies, mgr.get(cid)

    _cid, replies, client = _run(run())
    assert replies[0]["successful"] is True
    assert client is None, "/meta/disconnect must remove the client"


def test_p0_idle_timeout_reaps_handshake_only_client():
    """P0 case (e): LONG_POLLING_AUTOKILL (Perl Cometd.pm:49) is the path that
    removes a handshake-only client — the POST socket ending is not.

    A frozen clock stands in for the 180 s window; the client must survive the
    socket close and then be reaped once the window expires.
    """
    now, clock = _fake_clock()

    async def run():
        mgr = CometdManager(_StubRPC(), clock=clock)
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(_post_bytes([{"channel": "/meta/handshake", "id": 1}]))
            await writer.drain()
            cid = json.loads(await _read_clen_reply(reader))[0]["clientId"]
            writer.write(_post_bytes([{
                "channel": "/meta/subscribe", "clientId": cid, "id": 2,
                "subscription": f"/{cid}/slim/playerstatus/{_PLAYER}"}]))
            await writer.drain()
            await _read_clen_reply(reader)
            writer.close()  # never connects, never disconnects
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.3)
            survived = mgr.get(cid) is not None
            now[0] += LONG_POLLING_AUTOKILL + 1
            reaped = mgr.kill_idle_clients()
            return cid, survived, reaped, mgr.get(cid)
        finally:
            server.close()
            await server.wait_closed()

    cid, survived, reaped, client = _run(run())
    assert survived, "handshake-only client must survive the POST socket close"
    assert reaped == [cid], "the idle autokill must reap the handshake-only client"
    assert client is None


def test_p0_reconnect_new_socket_keeps_subscriptions():
    """P0 case (f): a new /meta/connect on a new socket takes over ownership
    (Perl register_connection overwrites the stored connection); the stale
    socket's close must neither remove the client nor lose its subscriptions,
    and the new socket keeps receiving the pushes."""
    player = _PLAYER

    async def run():
        mgr = CometdManager(_StubRPC())
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            # socket 1: handshake + connect + subscribe (owns the client)
            reader1, writer1, cid = await _open_stream(server)
            await _read_chunk(reader1)  # connect ack
            writer1.write(_post_bytes([{
                "channel": "/meta/subscribe", "clientId": cid, "id": 3,
                "subscription": f"/{cid}/slim/playerstatus/{player}"}]))
            await writer1.drain()
            await _read_chunk(reader1)  # subscribe ack + seed event

            # socket 2: the client reconnects — the new connect owns it now
            reader2, writer2 = await _stream_connect_cid(server, cid, request_id=4)

            # the stale socket ends: it must not take the client with it
            writer1.close()
            try:
                await writer1.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.3)
            client = mgr.get(cid)
            alive = client is not None
            subs = dict(client.subscriptions) if client else {}
            # the new socket still serves the subscriptions
            await mgr.notify_player_status(player)
            try:
                pushed = json.loads(await _read_chunk(reader2))
            except Exception:  # noqa: BLE001
                pushed = []
            writer2.close()
            try:
                await writer2.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return cid, alive, subs, pushed
        finally:
            server.close()
            await server.wait_closed()

    cid, alive, subs, pushed = _run(run())
    assert alive, "a stale connection close must not remove a reconnected client"
    assert f"/{cid}/slim/playerstatus/{player}" in subs, subs
    assert any("playerstatus" in m.get("channel", "") for m in pushed), pushed


def test_p0_asgi_non_connect_post_abort_keeps_client(caplog):
    """P0: on the ASGI path a failed handshake/subscribe/request response
    (client gone from that POST) must not erase a client whose /meta/connect
    is served elsewhere."""
    caplog.set_level("INFO")

    async def run():
        from lyrion.web.app import _handle_cometd

        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = hs[0]["clientId"]
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**"}])
        body = json.dumps([{
            "channel": "/slim/request", "clientId": cid, "id": 3,
            "data": {"request": ["", ["serverstatus", "0", "1"]],
                     "response": f"/{cid}/slim/request"}}]).encode()
        sent = []

        async def receive():
            if not sent:
                sent.append(1)
                return {"type": "http.request", "body": body,
                        "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            raise RuntimeError("client gone")

        await _handle_cometd(mgr, "/cometd", receive, send)
        return cid, mgr.get(cid)

    _cid, client = _run(run())
    assert client is not None, (
        "a non-connect POST abort must not drop the client "
        f"(log: {caplog.text})")


# ---------------------------------------------------------------------------
# R1 controller hardening — advice units and the invalid-clientId connect.
#
# Perl Cometd.pm:277-279 gives a /meta/(re)connect the advice
# ``{ interval => $streaming ? RETRY_DELAY : 0 }`` (RETRY_DELAY = 5000,
# Cometd.pm:45) and carries no ``timeout``/``reconnect`` there. Python keeps
# ``reconnect`` and the 60 s hold time as a documented superset; the timeout
# is emitted in MILLISECONDS (60000), matching the handshake advice
# (Cometd.pm:251) — it used to go out as ``60``, i.e. 60 ms.
# ---------------------------------------------------------------------------


def test_connect_advice_is_a_documented_superset_of_perl():
    from lyrion.web.cometd import (
        LONG_POLLING_INTERVAL,
        LONG_POLL_TIMEOUT_MS,
        RETRY_DELAY_MS,
        connect_advice,
    )

    streaming = connect_advice("streaming")
    assert streaming["interval"] == RETRY_DELAY_MS == 5000
    assert connect_advice("long-polling")["interval"] == LONG_POLLING_INTERVAL
    assert streaming["timeout"] == LONG_POLL_TIMEOUT_MS == 60000
    # superset fields (Perl's connect advice has interval only)
    assert streaming["reconnect"] == "retry"


def test_connect_with_unknown_clientid_is_not_acked_as_success():
    """Perl Cometd.pm:228-244 answers the re-handshake advice alone.

    handle_messages() reports the invalid clientId; the transport must not
    append a fabricated ``successful: true`` /meta/connect next to it (the
    client would believe it is still connected and never re-register its
    subscriptions).
    """
    from lyrion.web.cometd import has_invalid_client_advice

    async def run():
        mgr = CometdManager(_StubRPC())
        replies = await mgr.handle_messages([{
            "channel": "/meta/connect", "clientId": "deadbeef", "id": 4,
            "connectionType": "streaming"}])
        return replies

    replies = _run(run())
    assert has_invalid_client_advice(replies), replies
    assert not any(r.get("channel") == "/meta/connect"
                   and r.get("successful") for r in replies), replies

