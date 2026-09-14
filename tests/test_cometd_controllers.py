"""Cometd / Bayeux side as the *controllers* use it (Perl-parity, in-process).

Every assertion names the Perl line it mirrors.  The controllers this covers
are SqueezePlay/jive, Squeezer, SqueezeCtrl and Orange Squeeze — all of them
subscribe to ``/slim/playerstatus/<mac>`` (resp. ``/slim/menustatus/<mac>``)
and many of them WAIT for the first state that must arrive right after the
subscribe.

Perl references (read-only ``/tmp/lms-ref``):

* ``Slim/Web/Cometd.pm:246-262``  — /meta/handshake ack + advice
* ``Slim/Web/Cometd.pm:264-329``  — /meta/(re)connect, streaming intervals
* ``Slim/Web/Cometd.pm:225-244``  — unknown clientId -> re-handshake advice
* ``Slim/Web/Cometd.pm:348-368``  — /meta/subscribe (channel(s) only)
* ``Slim/Web/Cometd.pm:390-493``  — /slim/subscribe (request + subscribe)
* ``Slim/Web/Cometd.pm:494-526``  — /slim/unsubscribe
* ``Slim/Web/Cometd.pm:527-608``  — /slim/request
* ``Slim/Web/Cometd.pm:609-620``  — any other channel -> successful ack
* ``Slim/Web/Cometd/Manager.pm:102-179`` — channel globs -> regexes
* ``Slim/Control/Jive.pm:150-155`` — the ``menustatus`` query/dispatch

These run against ``CometdManager`` and the native stream server on an
ephemeral port: no server is started or stopped, no database is touched.
"""

import asyncio
import json
import re

import pytest

from lyrion.networking.cometd_stream import start_cometd_server
from lyrion.web.cometd import (
    DISCONNECT_GRACE,
    LONG_POLLING_INTERVAL,
    LONG_POLL_TIMEOUT_MS,
    RETRY_DELAY_MS,
    CometdManager,
    _channel_matches,
    _default_request,
    connect_advice,
    has_invalid_client_advice,
)

PLAYER = "1c:87:2c:47:fc:36"          # jive registers the LOWER-case spelling
PLAYER_UPPER = "1C:87:2C:47:FC:36"    # PlayerState.mac is upper case
OTHER_PLAYER = "aa:bb:cc:dd:ee:ff"

# Exactly what Player.lua:993 passes to comet:subscribe() (SqueezePlay).
JIVE_STATUS_CMD = ["status", "-", 10, "menu:menu", "useContextMenu:1",
                   "subscribe:600"]


class _Recorder:
    """Fake dispatcher: records the request and answers per command."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def dispatch(self, request: list) -> dict:
        self.calls.append(request)
        command = ""
        if len(request) > 1 and isinstance(request[1], list) and request[1]:
            command = request[1][0]
        elif request and isinstance(request[0], list) and request[0]:
            command = request[0][0]
        if command == "menustatus":
            return [None, [{"id": "myMusic", "text": "My Music"}], "add", ""]
        return {"mode": "play", "power": 1, "playlist_tracks": 1,
                "current_title": "Titel 51994",
                "item_loop": [{"title": "Titel 51994"}]}


def _manager():
    rec = _Recorder()
    mgr = CometdManager(rec)
    mgr._dispatch = rec.dispatch
    return mgr, rec


def _run(coro):
    return asyncio.run(coro)


async def _handshake(mgr) -> str:
    replies = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
    return replies[0]["clientId"]


# ---------------------------------------------------------------------------
# Handshake (Perl Cometd.pm:246-262)
# ---------------------------------------------------------------------------

def test_handshake_ack_matches_perl_shape():
    """version/supportedConnectionTypes/clientId/advice — and nothing else.

    Perl Cometd.pm:254-262 spreads EXACTLY ``id``, ``channel``, ``version``,
    ``supportedConnectionTypes => ['long-polling','streaming']``, the new
    ``clientId``, ``successful => true`` and ``advice => {reconnect =>
    'retry', interval => LONG_POLLING_INTERVAL, timeout =>
    LONG_POLLING_TIMEOUT}`` (60000 ms).  There is NO ``timestamp`` in a
    handshake answer — ``time2str`` appears only in /meta/(re)connect (:276)
    and /meta/disconnect (:338).  Live Perl 9.1.1 192.168.1.90:
    ``[{"successful":true,"supportedConnectionTypes":["long-polling",
    "streaming"],"id":1,"channel":"/meta/handshake","clientId":"a1632b04",
    "version":"1.0","advice":{"timeout":60000,"reconnect":"retry",
    "interval":0}}]``.
    """
    async def run():
        mgr, _rec = _manager()
        return (await mgr.handle_messages(
            [{"channel": "/meta/handshake", "id": 7}])), mgr

    replies, mgr = _run(run())
    ack = replies[0]
    assert ack["channel"] == "/meta/handshake"
    assert ack["id"] == 7
    assert ack["successful"] is True
    assert ack["version"] == "1.0"
    assert ack["supportedConnectionTypes"] == ["long-polling", "streaming"]
    assert ack["clientId"]
    assert mgr.get(ack["clientId"]) is not None
    assert set(ack) == {"channel", "id", "successful", "version",
                        "clientId", "supportedConnectionTypes", "advice"}
    advice = ack["advice"]
    assert advice["reconnect"] == "retry"
    assert advice["interval"] == LONG_POLLING_INTERVAL == 0
    # milliseconds, exactly like Perl's handshake advice (Cometd.pm:251)
    assert advice["timeout"] == LONG_POLL_TIMEOUT_MS == 60000


def test_rehandshake_with_known_uuid_keeps_subscriptions():
    """A jive re-handshake after a server restart reuses the clientId.

    The id is derived from the client UUID so the SAME client object (and its
    subscriptions) survives; a fresh object would have ``connections == 0``
    while its stream is still open (live 2026-09-12).
    """
    async def run():
        mgr, _rec = _manager()
        first = (await mgr.handle_messages([{
            "channel": "/meta/handshake", "id": 1,
            "ext": {"uuid": "12345678-90ab-cdef-1234-567890abcdef"}}]))[0]
        cid = first["clientId"]
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/slim/playerstatus/{PLAYER}"}])
        second = (await mgr.handle_messages([{
            "channel": "/meta/handshake", "id": 3,
            "ext": {"uuid": "12345678-90ab-cdef-1234-567890abcdef"}}]))[0]
        client = mgr.get(second["clientId"])
        return cid, second["clientId"], sorted(client.subscriptions)

    cid, cid2, subs = _run(run())
    assert cid == cid2
    assert subs == [f"/{cid}/slim/playerstatus/{PLAYER}"]


# ---------------------------------------------------------------------------
# First state right after the subscribe
# ---------------------------------------------------------------------------

def test_playerstatus_subscribe_seeds_state_on_the_subscribed_channel():
    """``/slim/subscribe`` + ``data.request`` -> state pushed at once.

    Perl Cometd.pm:411-476 executes the request AND registers it; the result
    is delivered to the response channel (Cometd.pm:465-475), or queued for
    the next poll when there is no connection yet (Cometd.pm:631-638).
    SqueezePlay's Now-Playing waits for exactly this seed (Player.lua:993).
    """
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        ack = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                     "response": channel}}]))[0]
        events = await mgr.wait_for_events(cid, timeout=0)
        return ack, channel, rec.calls, events

    ack, channel, calls, events = _run(run())
    assert ack["successful"] is True
    assert calls == [[PLAYER, JIVE_STATUS_CMD]]
    assert len(events) == 1, events
    assert events[0]["channel"] == channel
    assert events[0]["data"]["current_title"] == "Titel 51994"


def test_menustatus_subscribe_seeds_state_on_the_subscribed_channel():
    """``/<cid>/slim/menustatus/<mac>`` gets its first state right away.

    ``menustatus`` is a real Perl dispatch (``Slim/Control/Jive.pm:150-155``)
    and Squeezer subscribes to ``/<cid>/slim/menustatus/*``; the seed carries
    Squeezer's ``[?, items, directive, player]`` shape.
    """
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/menustatus/{PLAYER}"
        ack = (await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": channel}]))[0]
        events = await mgr.wait_for_events(cid, timeout=0)
        return ack, channel, rec.calls, events

    ack, channel, calls, events = _run(run())
    assert ack["successful"] is True
    assert calls == [["", ["menustatus"]]], calls
    assert len(events) == 1, events
    assert events[0]["channel"] == channel
    assert events[0]["data"][2] == "add"          # Squeezer's ADD directive


def test_pipelined_subscribe_and_connect_deliver_seed_in_connect_reply():
    """Subscribe pipelined with /meta/connect: seed comes with the connect ack.

    jive pipelines POSTs; the transport flushes the queued seed together with
    the connect reply (cometd_stream.py ``wait_for_events(cid, timeout=0)``).
    """
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        replies = await mgr.handle_messages([
            {"channel": "/slim/subscribe", "id": 2,
             "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                      "response": channel}},
            {"channel": "/meta/connect", "clientId": cid, "id": 3},
        ])
        events = await mgr.wait_for_events(cid, timeout=0)
        return replies, channel, events

    replies, channel, events = _run(run())
    # handle_messages never answers /meta/connect itself
    assert [r["channel"] for r in replies] == ["/slim/subscribe"]
    assert len(events) == 1 and events[0]["channel"] == channel


def test_notify_publishes_on_the_spelling_the_client_registered():
    """Lower-case subscription vs. upper-case ``PlayerState.mac``.

    jive compares the channel name verbatim, so the event must go out on the
    channel the client itself registered (LIVE-10).
    """
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                     "response": channel}}])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(PLAYER_UPPER)   # PlayerState.mac
        return channel, await mgr.wait_for_events(cid, timeout=0)

    channel, events = _run(run())
    assert len(events) == 1, events
    assert events[0]["channel"] == channel


# ---------------------------------------------------------------------------
# Subscribe forms: data.request, globs, duplicates, resubscribe
# ---------------------------------------------------------------------------

def test_subscribe_ack_carries_clientid_and_subscription():
    """Perl puts ``clientId`` + ``subscription`` into every subscribe ack.

    Cometd.pm:359-367 (/meta/subscribe) and Cometd.pm:454-459
    (/slim/subscribe). libcometd (SqueezeClient) additionally requires the
    ``subscription`` field — otherwise "Subscription response missing".
    """
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        meta = (await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": channel}]))[0]
        slim = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 3,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                     "response": channel}}]))[0]
        return cid, channel, meta, slim

    cid, channel, meta, slim = _run(run())
    for ack in (meta, slim):
        assert ack["successful"] is True
        assert ack["clientId"] == cid
        assert ack["subscription"] == channel


def test_subscribe_accepts_a_channel_array():
    """Perl accepts "an array of channel names and channel patterns".

    Cometd.pm:350-355 wraps a scalar in an array and Cometd.pm:359-367 emits
    one ack per subscription.  A list used to be indexed into the
    subscriptions dict, which raised ``unhashable type: 'list'`` and killed
    the whole batch.
    """
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        a, b = f"/{cid}/slim/playerstatus/{PLAYER}", f"/{cid}/slim/serverstatus"
        replies = await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "data": {"subscription": [a, b]}}])
        return cid, a, b, replies, sorted(mgr.get(cid).subscriptions)

    cid, a, b, replies, subs = _run(run())
    assert [r["subscription"] for r in replies] == [a, b]
    assert all(r["successful"] for r in replies)
    assert subs == sorted([a, b])


def test_glob_forms_match_like_perl_manager():
    """``/foo/**`` -> ``^/foo/``, ``/foo/*`` -> ``^/foo/[^/]+`` (not anchored).

    Perl Slim/Web/Cometd/Manager.pm:111-119.
    """
    assert _channel_matches(f"/{PLAYER}/**", f"/{PLAYER}/slim/playerstatus/x")
    assert not _channel_matches(f"/{PLAYER}/**", f"/{PLAYER}")
    assert _channel_matches("/slim/**", "/slim/playerstatus/1c:87")
    assert _channel_matches("/slim/playerstatus/*", "/slim/playerstatus/1c:87")
    assert not _channel_matches("/slim/playerstatus/*", "/slim/playerstatus")
    assert _channel_matches("/slim/playerstatus/1c:87:2c:47:fc:36",
                            "/slim/playerstatus/1c:87:2c:47:fc:36")


def test_glob_only_client_receives_concretised_playerstatus_push():
    """A ``/<cid>/**`` catch-all is concretised to the playerstatus channel."""
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**"}])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(PLAYER)
        return cid, await mgr.wait_for_events(cid, timeout=0)

    cid, events = _run(run())
    assert len(events) == 1, events
    assert events[0]["channel"] == f"/{cid}/slim/playerstatus/{PLAYER}"
    assert events[0]["data"]["current_title"] == "Titel 51994"


def test_duplicate_subscribe_of_same_channel_pushes_once():
    """Subscribing twice is idempotent for delivery.

    Perl's add_channels is ``||=``-idempotent per channel/clid
    (Manager.pm:121-122) and delivers one event per matched channel
    (Manager.pm:230-244).
    """
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        for msgid in (2, 3):
            await mgr.handle_messages([{
                "channel": "/meta/subscribe", "clientId": cid, "id": msgid,
                "subscription": channel}])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(PLAYER)
        return channel, await mgr.wait_for_events(cid, timeout=0)

    channel, events = _run(run())
    assert len(events) == 1, events
    assert events[0]["channel"] == channel


def test_unsubscribe_then_resubscribe_for_player_switch():
    """Player switch: unsubscribe A, subscribe B, only B is pushed.

    Perl Cometd.pm:494-526 removes the subscription; a following
    /slim/subscribe re-registers the new request.
    """
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        a = f"/{cid}/slim/playerstatus/{PLAYER}"
        b = f"/{cid}/slim/playerstatus/{OTHER_PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": a}}])
        await mgr.wait_for_events(cid, timeout=0)
        unsub = (await mgr.handle_messages([{
            "channel": "/slim/unsubscribe", "clientId": cid, "id": 3,
            "data": {"unsubscribe": a}}]))[0]
        after_unsub = sorted(mgr.get(cid).subscriptions)
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 4,
            "data": {"request": [OTHER_PLAYER, JIVE_STATUS_CMD],
                     "response": b}}])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(PLAYER)          # the OLD player
        old_events = await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(OTHER_PLAYER)    # the NEW player
        new_events = await mgr.wait_for_events(cid, timeout=0)
        return unsub, after_unsub, b, old_events, new_events

    unsub, after_unsub, b, old_events, new_events = _run(run())
    assert unsub["successful"] is True
    assert unsub["data"] == {"unsubscribe": unsub["data"]["unsubscribe"]}
    assert after_unsub == []
    assert old_events == []
    assert len(new_events) == 1 and new_events[0]["channel"] == b


def test_resubscribe_does_not_wipe_a_stored_request():
    """A channel-only /meta/subscribe must keep the client's request.

    Perl's /meta/subscribe only calls ``Manager::add_channels``
    (Cometd.pm:357) — it never touches the request registered through
    /slim/subscribe. Replacing the stored payload would silently downgrade
    every later push to the jive default and lose ``subscribe:N``.
    """
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                     "response": channel}}])
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 3,
            "subscription": channel}])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(PLAYER)
        return rec.calls, await mgr.wait_for_events(cid, timeout=0)

    calls, events = _run(run())
    assert calls == [[PLAYER, JIVE_STATUS_CMD]], calls
    assert len(events) == 1, events


def test_slim_unsubscribe_ack_echoes_data_and_clientid():
    """Perl Cometd.pm:519-525: clientId + the echoed ``data``."""
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/serverstatus"
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": channel}])
        return cid, (await mgr.handle_messages([{
            "channel": "/slim/unsubscribe", "clientId": cid, "id": 3,
            "data": {"unsubscribe": channel}}]))[0], channel

    cid, ack, channel = _run(run())
    assert ack["successful"] is True
    assert ack["clientId"] == cid
    assert ack["data"] == {"unsubscribe": channel}


# ---------------------------------------------------------------------------
# Handshake variants / advice / ack frames / error cases
# ---------------------------------------------------------------------------

def test_connect_advice_uses_retry_delay_for_streaming():
    """Perl Cometd.pm:267,277-279: streaming -> RETRY_DELAY, else 0.

    ``RETRY_DELAY`` is 5000 ms (Cometd.pm:45); Bayeux advice values are
    milliseconds.  The 60 s ``timeout`` belongs to the HANDSHAKE advice
    (:248-253), not to the connect advice (Parity-Audit D1).
    """
    assert RETRY_DELAY_MS == 5000
    assert connect_advice("streaming") == {"interval": RETRY_DELAY_MS}
    assert connect_advice("long-polling")["interval"] == LONG_POLLING_INTERVAL
    assert connect_advice("")["interval"] == LONG_POLLING_INTERVAL
    assert "timeout" not in connect_advice("streaming")


def test_unknown_clientid_is_answered_with_rehandshake_advice():
    """Perl Cometd.pm:225-244 — no processing, advice.reconnect=handshake."""
    async def run():
        mgr, rec = _manager()
        replies = await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": "deadbeef", "id": 1,
            "subscription": f"/deadbeef/slim/playerstatus/{PLAYER}"}])
        return replies, rec.calls, sorted(mgr._clients)

    replies, calls, clients = _run(run())
    assert len(replies) == 1
    reply = replies[0]
    assert reply["successful"] is False
    assert reply["error"] == "invalid clientId"
    assert reply["clientId"] is None
    assert reply["advice"]["reconnect"] == "handshake"
    assert calls == []
    # never mint a client out of an unknown clientId
    assert clients == []
    assert has_invalid_client_advice(replies)


def test_unknown_channel_is_acked_successfully():
    """Perl Cometd.pm:609-620: any other channel -> ``successful => true``."""
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        return (await mgr.handle_messages([{
            "channel": "/some/custom/channel", "clientId": cid,
            "id": 2, "data": {"hello": 1}}]))[0]

    ack = _run(run())
    assert ack["successful"] is True
    assert ack["channel"] == "/some/custom/channel"
    assert ack["id"] == 2


def test_slim_subscribe_error_acks():
    """Perl Cometd.pm:479-492 — request/response are both mandatory."""
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        no_request = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "clientId": cid, "id": 2,
            "data": {"response": f"/{cid}/slim/playerstatus/{PLAYER}"}}]))[0]
        no_response = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "clientId": cid, "id": 3,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD]}}]))[0]
        return no_request, no_response, sorted(mgr.get(cid).subscriptions)

    no_request, no_response, subs = _run(run())
    assert no_request["successful"] is False
    assert no_request["error"] == "request data key not found"
    assert no_response["successful"] is False
    assert no_response["error"] == "response data key not found"
    assert subs == []


def test_default_request_for_status_channels_is_the_jive_form():
    """The seed fallback carries title/playlist (LIVE-01).

    A pattern is a matcher only — Perl's add_channels never dispatches a
    query for it, so ``/x/**`` must yield no request.
    """
    assert _default_request(f"/{PLAYER}/**") == []
    assert _default_request("/**") == []
    assert _default_request(f"/{PLAYER}/slim/playerstatus/{PLAYER}") == [
        PLAYER, JIVE_STATUS_CMD[:-1]]
    assert _default_request(f"/{PLAYER}/slim/menustatus/{PLAYER}") == [
        "", ["menustatus"]]
    assert _default_request(f"/{PLAYER}/slim/serverstatus") == [
        "", ["serverstatus", "0", "50", "subscribe:60"]]


# ---------------------------------------------------------------------------
# Native stream transport: connect acks, invalid clientId, connection loss
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


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":", 1)[1].strip())
    return 0


async def _read_chunk(reader) -> bytes:
    size = int((await asyncio.wait_for(reader.readline(), timeout=5)).strip(), 16)
    if size == 0:
        return b""
    data = await asyncio.wait_for(reader.readexactly(size), timeout=5)
    await asyncio.wait_for(reader.readexactly(2), timeout=5)  # trailing CRLF
    return data


async def _open_stream(server):
    """Handshake + streaming /meta/connect, connect chunk consumed."""
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_post_bytes([{"channel": "/meta/handshake", "id": 1}]))
    await writer.drain()
    head = await _read_headers(reader)
    hs = json.loads(await asyncio.wait_for(
        reader.readexactly(_content_length(head)), timeout=5))
    cid = hs[0]["clientId"]
    writer.write(_post_bytes([{
        "channel": "/meta/connect", "clientId": cid, "id": 2,
        "connectionType": "streaming"}]))
    await writer.drain()
    head = await _read_headers(reader)
    assert b"chunked" in head.lower(), head
    return reader, writer, cid


def test_streaming_connect_ack_carries_advice_and_timestamp():
    """The one connect ack: clientId, id, RFC1123 timestamp, RETRY_DELAY."""
    async def run():
        mgr, _rec = _manager()
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            reader, writer, cid = await _open_stream(server)
            try:
                chunk = json.loads(await _read_chunk(reader))
            finally:
                writer.close()
            return cid, chunk
        finally:
            server.close()
            await server.wait_closed()

    cid, chunk = _run(run())
    acks = [m for m in chunk if m.get("channel") == "/meta/connect"]
    assert len(acks) == 1, chunk
    ack = acks[0]
    assert ack["successful"] is True
    assert ack["clientId"] == cid
    assert ack["id"] == 2
    assert ack["advice"] == {"interval": RETRY_DELAY_MS}
    assert "timeout" not in ack["advice"]
    assert re.match(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} "
                    r"\d{2}:\d{2}:\d{2} GMT$", ack["timestamp"]), ack["timestamp"]


def test_connect_with_unknown_clientid_gets_no_successful_connect_ack():
    """Perl Cometd.pm:228-244: advice only, no connect ack, no stream.

    The old code appended a fabricated ``successful: true`` /meta/connect next
    to the re-handshake advice, so jive believed it was still connected and
    never re-registered its subscriptions.
    """
    async def run():
        mgr, _rec = _manager()
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(_post_bytes([{
                    "channel": "/meta/connect", "clientId": "deadbeef",
                    "id": 5, "connectionType": "streaming"}]))
                await writer.drain()
                head = await _read_headers(reader)
                assert b"chunked" not in head.lower(), head
                payload = json.loads(await asyncio.wait_for(
                    reader.readexactly(_content_length(head)), timeout=5))
                return payload
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    payload = _run(run())
    assert not any(m.get("channel") == "/meta/connect" and m.get("successful")
                   for m in payload), payload
    advices = [m for m in payload if m.get("advice")]
    assert advices, payload
    assert advices[0]["advice"]["reconnect"] == "handshake"
    assert advices[0]["error"] == "invalid clientId"


def test_connection_lost_mid_push_keeps_the_client_for_the_grace():
    """A client whose socket vanishes is unregistered, then reaped by the grace.

    Perl's webCloseHandler -> disconnectClient (Cometd.pm:1003-1020) does NOT
    delete the client: it unregisters the connection and arms the timer for
    RETRY_DELAY * 2, so a jive app that only re-pooled its streaming socket
    keeps its subscriptions and the events queued for it (the ASGI path fixed
    this first, web/app.py:282-292 "the app then re-handshook and lost the
    pending request"). The expired grace is what finally drops the client and
    its queue — no stray asyncio exception either way.
    """
    now = [1000.0]

    async def run():
        rec = _Recorder()
        mgr = CometdManager(rec, clock=lambda: now[0])
        mgr._dispatch = rec.dispatch
        server = await start_cometd_server(mgr, "127.0.0.1", 0)
        loop = asyncio.get_running_loop()
        captured: list = []
        prev = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: captured.append(ctx))
        try:
            reader, writer, cid = await _open_stream(server)
            await _read_chunk(reader)
            writer.write(_post_bytes([{
                "channel": "/slim/subscribe", "id": 3,
                "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                         "response": f"/{cid}/slim/playerstatus/{PLAYER}"}}]))
            await writer.drain()
            await _read_chunk(reader)          # ack + seed
            # a push is queued while the client dies: close without reading
            await mgr.notify_player_status(PLAYER)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            for _ in range(80):
                client = mgr.get(cid)
                if client is not None and client.disconnect_at is not None:
                    break
                await asyncio.sleep(0.05)
            client = mgr.get(cid)
            kept_subscriptions = bool(client and client.subscriptions)
            disconnect_at = client.disconnect_at if client is not None else None
            if disconnect_at is not None:
                now[0] = disconnect_at + 0.1
            reaped = mgr.kill_idle_clients()
            return cid, client, kept_subscriptions, reaped, captured
        finally:
            loop.set_exception_handler(prev)
            server.close()
            await server.wait_closed()

    cid, client, kept_subscriptions, reaped, captured = _run(run())
    assert client is not None, "a lost connection must not erase the client"
    assert kept_subscriptions, "the client's subscriptions were thrown away"
    assert reaped == [cid], "the expired grace must reap the client"
    names = [type(ctx.get("exception")).__name__
             for ctx in captured if ctx.get("exception")]
    assert names == [], f"unexpected asyncio errors: {captured}"


# ---------------------------------------------------------------------------
# Perl's disconnect grace (webCloseHandler -> disconnectClient, RETRY_DELAY*2)
# ---------------------------------------------------------------------------

def test_release_connection_arms_the_grace_and_a_reconnect_kills_it():
    """Cometd.pm:1010-1014 / :283 — a lost connection keeps the client 10 s.

    Perl's webCloseHandler unregisters the *connection* of a client whose
    socket died and arms ``disconnectClient`` for RETRY_DELAY * 2 = 10 s; the
    next /meta/(re)connect kills that timer (``killTimers($clid,
    \\&disconnectClient)``). Python used to remove the client immediately, so a
    client that merely reconnected lost its subscriptions and every event
    queued for it — the app then re-handshook and its pending request was gone.
    """
    now = [1000.0]
    rec = _Recorder()
    mgr = CometdManager(rec, clock=lambda: now[0])
    cid = mgr.handshake().client_id
    client = mgr.get(cid)
    client.subscriptions["/slim/serverstatus"] = {"request": ["", ["serverstatus"]]}

    owner = object()
    mgr.register_connection(cid, owner)
    mgr.connection_open(cid)
    # the app's teardown order: close the transport, then release the client
    mgr.connection_closed(cid)
    assert mgr.release_connection(cid, owner) is True

    assert client.owner is None and client.connections == 0
    assert client.transport == ""            # untouched by the release
    assert client.disconnect_at == 1000.0 + DISCONNECT_GRACE
    assert DISCONNECT_GRACE == (RETRY_DELAY_MS / 1000.0) * 2 == 10.0
    # A stale connection cannot release a client it no longer owns.
    assert mgr.release_connection(cid, object()) is False
    # While the grace runs the client is kept, subscriptions included.
    assert mgr.kill_idle_clients() == []
    assert mgr.get(cid) is client
    assert client.subscriptions

    # The reconnect kills Perl's timer (Cometd.pm:283) ...
    other = object()
    mgr.register_connection(cid, other)
    assert mgr.get(cid).disconnect_at is None
    assert mgr.get(cid).subscriptions      # ... and the session survived


def test_grace_expiry_reaps_a_client_that_never_reconnected():
    """Cometd.pm:1049-1074 — disconnectClient removes the client.

    The sweep is Python's form of Perl's timer: the client disappears only
    once the grace has really expired, never before.
    """
    now = [2000.0]
    rec = _Recorder()
    mgr = CometdManager(rec, clock=lambda: now[0])
    cid = mgr.handshake().client_id
    owner = object()
    mgr.register_connection(cid, owner)
    mgr.connection_open(cid)
    mgr.connection_closed(cid)
    mgr.release_connection(cid, owner)

    now[0] += DISCONNECT_GRACE - 0.1
    assert mgr.kill_idle_clients() == [], "reaped before the grace expired"
    now[0] += 0.2
    assert mgr.kill_idle_clients() == [cid]
    assert mgr.get(cid) is None


def test_streaming_connect_advice_and_grace_come_from_perl_retry_delay():
    """Both numbers are Perl's RETRY_DELAY (Cometd.pm:45), never invented.

    The streaming ack advertises ``interval => RETRY_DELAY`` (Cometd.pm:278)
    so a client whose stream ends (only the network can end it — Perl arms no
    hold timer for the streaming branch, Cometd.pm:288-297) is told when to
    come back. The disconnect grace is Perl's ``RETRY_DELAY * 2``
    (Cometd.pm:1010-1014). There is NO Python-side streaming hold window any
    more: it used to close the ASGI response after exactly this interval,
    which Perl never does.
    """
    import lyrion.web.cometd as cometd_mod

    assert not hasattr(cometd_mod, "STREAMING_HOLD_WINDOW")
    assert DISCONNECT_GRACE == (RETRY_DELAY_MS / 1000.0) * 2 == 10.0
    assert connect_advice("streaming")["interval"] == RETRY_DELAY_MS
    assert connect_advice("long-polling")["interval"] == LONG_POLLING_INTERVAL
