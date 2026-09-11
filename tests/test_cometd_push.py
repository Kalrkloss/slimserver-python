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

from lyrion.web.cometd import CometdManager, _channel_matches


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


def test_channel_glob_matches_perl_semantics():
    # Perl Manager::add_channels: '/foo/**' -> ^/foo/, '/foo/*' -> ^/foo/[^/]+
    assert _channel_matches("/1abc/**", "/1abc/slim/serverstatus")
    assert _channel_matches("/1abc/**", "/1abc/slim/playerstatus/aa:bb")
    assert not _channel_matches("/1abc/**", "/other/slim/serverstatus")
    assert not _channel_matches("/1abc/**", "/1abc")  # needs the trailing /
    assert _channel_matches("/slim/**", "/slim/serverstatus")
    assert _channel_matches("/slim/*", "/slim/serverstatus")
    assert _channel_matches("**", "/anything/at/all")
    assert not _channel_matches("/slim/serverstatus", "/slim/playerstatus/x")
    assert _channel_matches("/slim/serverstatus", "/slim/serverstatus")


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
