"""The /meta/(re)connect wire format a Squeeze Client sees — Perl, byte for byte.

Squeeze Client (``maniac103/squeezeclient``, GPL-3; read-only clone
``/tmp/squeezeclient-src``) is NOT a Bayeux library client — ``CometdClient.kt``
speaks the protocol by hand and parses every frame into

    @Serializable
    data class Message(
        @SerialName("channel") val channelId: ChannelId,   // REQUIRED
        val clientId: String? = null,
        val id: Int,                                       // REQUIRED
        val successful: Boolean = true,
        val error: String? = null,
        val data: JsonElement? = null)          # CometdClient.kt:296-303
    val json = Json { coerceInputValues = true; ignoreUnknownKeys = true }

with ``startListening`` (CometdClient.kt:163-210) requiring BOTH acks of its
batch to be ``successful`` within 5 s, else it raises
``CometdException("Subscription response unsuccessful"/"…missing")``.

The frames below are the ones it actually sends and receives.  Handshake has no
``ext`` and no ``connectionType`` (CometdClient.kt:144-150), the connect batch
carries ``connectionType: "streaming"`` plus ``/meta/subscribe /<cid>/**``
(:167-179), and the UA is the hardcoded ``Squeezer-squeezer/1.0`` (:284):

    HANDSHAKE      [{"channel":"/meta/handshake","id":1,
                     "supportedConnectionTypes":"streaming","version":"1.0"}]
    CONNECT BATCH  [{"channel":"/meta/connect","id":2,
                     "connectionType":"streaming","clientId":"<cid>"},
                    {"channel":"/meta/subscribe","subscription":"/<cid>/**",
                     "clientId":"<cid>","id":3}]
    SUBSCRIBE      POST /slim/subscribe
                   {"request":["",["serverstatus",0,100,"subscribe:30",
                                   "prefs:mediadirs"]],
                    "response":"/<cid>/slim/serverstatus"}

RAW REFERENCE, live Perl 9.1.1 192.168.1.90 (measured 2026-09-14, same UA):

  handshake ->
    [{"successful":true,"supportedConnectionTypes":["long-polling","streaming"],
      "id":1,"channel":"/meta/handshake","clientId":"a1632b04",
      "version":"1.0","advice":{"timeout":60000,"reconnect":"retry",
      "interval":0}}]
    (Cometd.pm:246-262 — seven keys, NO ``timestamp``)

  connect batch -> ONE chunked array, the connect ack FIRST:
    [{"advice":{"interval":5000},"successful":true,
      "timestamp":"Mon, 14 Sep 2026 13:38:52 GMT","channel":"/meta/connect",
      "clientId":"a1632b04","id":2},
     {"id":3,"clientId":"a1632b04","channel":"/meta/subscribe",
      "subscription":"/a1632b04/**","successful":true}]
    (Cometd.pm:269-280 ``first_event``; :277-279 advice carries ONLY
     ``interval``; :356-367 the subscribe ack)

  /slim/subscribe -> ack only, no ``error`` key:
    [{"successful":true,"id":4,"clientId":"a1632b04",
      "channel":"/slim/subscribe"}]
    (Cometd.pm:454-456)

The three deviations this file pins shut (all measured against :9000 before the
fix and re-measured after): the handshake carried an invented ``timestamp``,
the connect ack went out AFTER the batch acks (Perl: ``first_event``), and the
subscribe acks carried an invented ``error: null``.
"""

import asyncio
import json
import re

from lyrion.networking.cometd_stream import start_cometd_server
from lyrion.web.cometd import (
    LONG_POLLING_INTERVAL,
    LONG_POLL_TIMEOUT_MS,
    RETRY_DELAY_MS,
    CometdManager,
)

PLAYER = "1c:87:2c:47:fc:36"
UA = "Squeezer-squeezer/1.0"          # CometdClient.kt:284


class _StubRPC:
    """Minimal slim.request handler — records the request, echoes a result."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def handle_request(self, body: bytes) -> bytes:
        payload = json.loads(body)
        player, cmd = payload["params"]
        self.calls.append([player, cmd])
        return json.dumps({
            "id": 1, "method": "slim.request",
            "result": {"command": cmd[0], "player_id": player,
                       "version": "9.2.0", "mediadirs": [],
                       "players_loop": [{"playerid": PLAYER}]},
        }).encode()


def _run(coro):
    return asyncio.run(coro)


def _post_bytes(payload) -> bytes:
    body = json.dumps(payload).encode()
    return (b"POST /cometd HTTP/1.1\r\nHost: x\r\n"
            + f"User-Agent: {UA}\r\n".encode()
            + b"Content-Type: application/json\r\n"
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
    size_line = await asyncio.wait_for(reader.readline(), timeout=5)
    size = int(size_line.strip(), 16)
    if size == 0:
        return b""
    data = await asyncio.wait_for(reader.readexactly(size), timeout=5)
    await asyncio.wait_for(reader.readexactly(2), timeout=5)   # trailing CRLF
    return data


async def _handshake_raw(reader, writer) -> bytes:
    """SqueezeClient handshake -> the RAW response body (CometdClient.kt:144)."""
    writer.write(_post_bytes([
        {"channel": "/meta/handshake", "id": 1,
         "supportedConnectionTypes": "streaming", "version": "1.0"}]))
    await writer.drain()
    _head, body = await _read_response(reader)
    return body


def _assert_message_contract(msgs: list, where: str) -> None:
    """Every frame must satisfy SqueezeClient's ``Message`` (CometdClient.kt:296).

    ``channel`` and ``id`` are non-nullable without defaults, so a missing
    ``id`` makes ``parseToMessageArrayOrThrow`` throw; ``readFromEventStream``
    then treats it as "JSON was incomplete" and NEVER clears its buffer, so the
    stream dies (5 s window -> ``CometdException``).
    """
    for m in msgs:
        assert isinstance(m.get("channel"), str), (where, m)
        assert "id" in m, f"{where}: frame without id -> {m}"
        assert isinstance(m["id"], (int, str)), (where, m)


# ---------------------------------------------------------------------------
# Handshake (Perl Cometd.pm:246-262)
# ---------------------------------------------------------------------------

def test_handshake_frame_is_perl_exact_on_the_native_port():
    """Native :9000 answers SqueezeClient's ext-less handshake like Perl."""
    async def run():
        server = await start_cometd_server(CometdManager(_StubRPC()),
                                          "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection(
                *server.sockets[0].getsockname()[:2])
            try:
                return json.loads(await _handshake_raw(reader, writer))
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    msgs = _run(run())
    _assert_message_contract(msgs, "handshake")
    ack = msgs[0]
    # Perl Cometd.pm:254-262 — seven keys, exactly.
    assert set(ack) == {"channel", "id", "version", "supportedConnectionTypes",
                        "clientId", "successful", "advice"}, ack
    assert ack["channel"] == "/meta/handshake"
    assert ack["id"] == 1
    assert ack["version"] == "1.0"
    assert ack["successful"] is True
    assert ack["supportedConnectionTypes"] == ["long-polling", "streaming"]
    assert ack["advice"] == {"reconnect": "retry",
                             "interval": LONG_POLLING_INTERVAL,
                             "timeout": LONG_POLL_TIMEOUT_MS}
    assert ack["advice"]["timeout"] == 60000     # ms, NOT 60 (Cometd.pm:251)


# ---------------------------------------------------------------------------
# Connect batch: order + advice (Perl Cometd.pm:264-280)
# ---------------------------------------------------------------------------

def test_connect_batch_is_perl_exact_on_the_native_port():
    """connect ack FIRST, subscribe ack second, advice = {interval: 5000}."""
    async def run():
        server = await start_cometd_server(CometdManager(_StubRPC()),
                                          "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection(
                *server.sockets[0].getsockname()[:2])
            try:
                cid = json.loads(await _handshake_raw(reader, writer))[0]["clientId"]
                writer.write(_post_bytes([
                    {"channel": "/meta/connect", "id": 2,
                     "connectionType": "streaming", "clientId": cid},
                    {"channel": "/meta/subscribe",
                     "subscription": f"/{cid}/**", "clientId": cid, "id": 3},
                ]))
                await writer.drain()
                head = await _read_headers(reader)
                assert b"chunked" in head.lower(), head
                return cid, json.loads(await _read_chunk(reader))
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    cid, frames = _run(run())
    _assert_message_contract(frames, "connect batch")

    # Perl: first_event slot -> the connect answer leads (Cometd.pm:269-271).
    assert frames[0]["channel"] == "/meta/connect", frames
    assert frames[1]["channel"] == "/meta/subscribe", frames

    conn = frames[0]
    assert conn["successful"] is True
    assert conn["clientId"] == cid
    assert conn["id"] == 2
    # Cometd.pm:277-279: advice carries ONLY ``interval`` — RETRY_DELAY (5000)
    # for streaming, LONG_POLLING_INTERVAL (0) otherwise.
    assert conn["advice"] == {"interval": RETRY_DELAY_MS} == {"interval": 5000}
    assert set(conn) == {"channel", "successful", "clientId", "id",
                         "timestamp", "advice"}, conn
    # Cometd.pm:276 ``timestamp => time2str(time())``.
    assert re.match(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} "
                    r"\d{2}:\d{2}:\d{2} GMT$", conn["timestamp"]), conn["timestamp"]

    sub = frames[1]
    assert sub["successful"] is True
    assert sub["clientId"] == cid
    assert sub["subscription"] == f"/{cid}/**"
    # Perl's success ack has no ``error`` key (only the failure paths carry
    # one: Cometd.pm:200-212).
    assert "error" not in sub, sub


def test_connect_advice_is_interval_only_for_both_transports():
    """Streaming advertises RETRY_DELAY, long-polling 0 (Cometd.pm:45/47/278).

    The advice of a connect answer is a Bayeux control frame: Perl puts exactly
    one key in it.  ``reconnect``/``timeout`` live in the HANDSHAKE advice —
    a connect answer that invents them is a superset no Perl client ever saw.
    """
    async def run():
        mgr = CometdManager(_StubRPC())
        cid = (await mgr.handle_messages(
            [{"channel": "/meta/handshake", "id": 1}]))[0]["clientId"]
        from lyrion.web.cometd import connect_ack
        streaming = connect_ack(
            {"id": 2, "clientId": cid, "connectionType": "streaming"}, cid)
        longpoll = connect_ack(
            {"id": 3, "clientId": cid, "connectionType": "long-polling"}, cid)
        return streaming["advice"], longpoll["advice"]

    streaming, longpoll = _run(run())
    assert streaming == {"interval": RETRY_DELAY_MS}
    assert longpoll == {"interval": LONG_POLLING_INTERVAL}
    for advice in (streaming, longpoll):
        assert "reconnect" not in advice
        assert "timeout" not in advice


# ---------------------------------------------------------------------------
# Full client sequence: handshake -> connect+subscribe -> /slim/subscribe
# ---------------------------------------------------------------------------

def test_squeezeclient_sequence_gets_every_answer_it_waits_for():
    """The exact SqueezeClient startup: nothing missing, nothing invented.

    ``ConnectionHelper.connect`` (ConnectionHelper.kt:131-196) needs, in order:
    a handshake clientId, a successful /meta/connect, a successful
    /meta/subscribe ``/<cid>/**``, then the serverstatus record on its stream
    (``client.subscribe(serverStatus(cid))`` reads ``eventFlow``, i.e. the
    chunked /meta/connect body — NOT the POST answer; without the record it
    raises ``"Initial server status timeout"`` after CONNECTION_TIMEOUT).
    """
    async def run():
        rpc = _StubRPC()
        server = await start_cometd_server(CometdManager(rpc), "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(
            *server.sockets[0].getsockname()[:2])
        try:
            cid = json.loads(await _handshake_raw(reader, writer))[0]["clientId"]
            # startListening: connect + /meta/subscribe on the streaming socket.
            writer.write(_post_bytes([
                {"channel": "/meta/connect", "id": 2,
                 "connectionType": "streaming", "clientId": cid},
                {"channel": "/meta/subscribe",
                 "subscription": f"/{cid}/**", "clientId": cid, "id": 3},
            ]))
            await writer.drain()
            await _read_headers(reader)
            batch = json.loads(await _read_chunk(reader))

            # publishRequest(ServerStatusRequest(30)) on /slim/subscribe — a
            # second socket, exactly like the app (OkHttp keeps the stream and
            # the request pool apart).
            reader_b, writer_b = await asyncio.open_connection(
                *server.sockets[0].getsockname()[:2])
            try:
                writer_b.write(_post_bytes([
                    {"channel": "/slim/subscribe", "clientId": cid, "id": 4,
                     "data": {
                         "request": ["", ["serverstatus", 0, 100,
                                          "subscribe:30", "prefs:mediadirs"]],
                         "response": f"/{cid}/slim/serverstatus"}}]))
                await writer_b.drain()
                _head, post_answers = await _read_response(reader_b)
                post_answers = json.loads(post_answers)
            finally:
                writer_b.close()

            # The record itself is pushed on the open stream.
            streamed = []
            for _ in range(4):
                try:
                    streamed.extend(json.loads(await _read_chunk(reader)))
                except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                        ValueError):
                    break
                if any(m["channel"] == f"/{cid}/slim/serverstatus"
                       for m in streamed):
                    break
            return cid, batch, post_answers, streamed
        finally:
            writer.close()
            server.close()
            await server.wait_closed()

    cid, batch, post_answers, streamed = _run(run())
    _assert_message_contract(batch, "connect batch")
    _assert_message_contract(post_answers, "/slim/subscribe")

    # startListening: the first two messages must be the two acks, successful.
    ack_pairs = [m for m in batch
                 if m["channel"] in ("/meta/connect", "/meta/subscribe")]
    assert len(ack_pairs) == 2, batch
    assert all(m["successful"] for m in ack_pairs), batch
    assert {m["channel"] for m in ack_pairs} == {"/meta/connect",
                                                 "/meta/subscribe"}

    # publish(): the ack of the /slim/subscribe POST decides success — and it
    # must not carry an invented ``error`` key.
    ack = post_answers[0]
    assert ack["channel"] == "/slim/subscribe"
    assert ack["id"] == 4
    assert ack["successful"] is True
    assert "error" not in ack, ack

    # parseServerStatus decodes ServerStatusResponse(version, players_loop,
    # mediadirs) from the record on the stream (ConnectionHelper.kt:450-480).
    records = [m for m in streamed
               if m["channel"] == f"/{cid}/slim/serverstatus"]
    assert records, streamed
    data = records[0]["data"]
    for key in ("version", "players_loop", "mediadirs"):
        assert key in data, (key, data)
