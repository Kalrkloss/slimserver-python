"""ASGI /cometd path (`web/app.py` + `web/cometd.py`) — Perl-parity, in-process.

The native stream transport (port 9080, `networking/cometd_stream.py`) was
fixed first; the ASGI path that uvicorn serves on the web port (and that
non-Jive Android controllers such as SqueezeClient reach over plain HTTP)
kept three of the old bugs:

* /meta/connect advice carried ``timeout: 60`` — Bayeux advice values are
  MILLISECONDS (Perl ``LONG_POLLING_TIMEOUT => 60000``, Cometd.pm:48/251), so
  the client read a 60 ms server timeout;
* the connect advice used ``interval: 0`` for a *streaming* connect (Perl:
  ``interval => $streaming ? RETRY_DELAY : 0``, Cometd.pm:45/278);
* an unknown clientId got the re-handshake advice AND a fabricated
  ``successful: true`` /meta/connect (Perl ``last``s out of the loop before
  the connect branch, Cometd.pm:228-244).

Additionally the connect ack lacked the RFC1123 ``timestamp`` (Cometd.pm:276)
and the long-poll hold ignored the client's ``advice.timeout`` override
(Cometd.pm:302-306).

Perl references (read-only ``/tmp/lms-ref``):

* ``Slim/Web/Cometd.pm:45-49``     — RETRY_DELAY / INTERVAL / TIMEOUT / AUTOKILL
* ``Slim/Web/Cometd.pm:200-212``   — message without clientId is dropped
* ``Slim/Web/Cometd.pm:228-244``   — unknown clientId -> advice + ``last``
* ``Slim/Web/Cometd.pm:246-262``   — /meta/handshake ack + advice
* ``Slim/Web/Cometd.pm:271-280``   — /meta/(re)connect ``first_event`` ack
* ``Slim/Web/Cometd.pm:286``       — register_connection on (re)connect
* ``Slim/Web/Cometd.pm:302-306``   — client may override the poll timeout
* ``Slim/Web/Cometd.pm:357/363``   — /meta/subscribe + clientId ack
* ``Slim/Web/Cometd.pm:427/456``   — /slim/subscribe ack + clientId
* ``Slim/Web/Cometd.pm:577``       — /slim/request ack + clientId

These tests drive the real ASGI handler with a fake ``receive``/``send``
(the convention of tests/test_stream_flow.py): no server is started or
stopped, no database is touched, and the live dev server on :9000 is left
alone.
"""

import asyncio
import json
import re
import time

from lyrion.web.app import _handle_cometd
from lyrion.web.cometd import (
    LONG_POLL_TIMEOUT_MS,
    LONG_POLLING_INTERVAL,
    RETRY_DELAY_MS,
    CometdManager,
    connect_timeout,
)

PLAYER = "1c:87:2c:47:fc:36"
JIVE_STATUS_CMD = ["status", "-", 10, "menu:menu", "useContextMenu:1",
                   "subscribe:600"]


class _Recorder:
    """Fake dispatcher: records requests, answers per command."""

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
        if command == "serverstatus":
            return {"players": []}
        return {"mode": "play", "power": 1, "current_title": "Titel 51994",
                "playlist_tracks": 1,
                "item_loop": [{"title": "Titel 51994"}]}


def _manager():
    rec = _Recorder()
    mgr = CometdManager(rec)
    mgr._dispatch = rec.dispatch
    return mgr, rec


class _Transport:
    """Minimal ASGI scope-less harness for one POST /cometd batch.

    ``receive`` hands the encoded batch over once; ``send`` records every
    ASGI event so the tests can assert on the framing (one complete
    response vs. a held-open chunked stream).
    """

    def __init__(self, payload) -> None:
        self.payload = payload
        self.sent: list[dict] = []
        self.chunks: list[bytes] = []
        self.first_chunk = asyncio.Event()
        self._delivered = False

    async def receive(self) -> dict:
        if self._delivered:
            return {"type": "http.disconnect"}
        self._delivered = True
        return {"type": "http.request",
                "body": json.dumps(self.payload).encode(),
                "more_body": False}

    async def send(self, event: dict) -> None:
        self.sent.append(event)
        if event.get("type") == "http.response.body" and event.get("body"):
            self.chunks.append(event["body"])
            self.first_chunk.set()

    # -- assertions helpers ------------------------------------------------
    @property
    def raw_body(self) -> bytes:
        return b"".join(self.chunks)

    @property
    def headers(self) -> dict:
        for event in self.sent:
            if event.get("type") == "http.response.start":
                return {k.decode().lower(): v.decode()
                        for k, v in event.get("headers", [])}
        return {}

    def json(self):
        """The parsed reply — only for a complete, non-streaming response."""
        assert not self.streaming, "the response is a held-open stream"
        return json.loads(self.raw_body)

    @property
    def streaming(self) -> bool:
        """True while the response has not been terminated (more_body)."""
        for event in self.sent:
            if event.get("type") == "http.response.body" and \
                    not event.get("more_body", False):
                return False
        return True


def _post(mgr, payload) -> _Transport:
    """Run one /cometd POST to completion (non-streaming replies only)."""
    tr = _Transport(payload)

    async def run():
        await asyncio.wait_for(_handle_cometd(mgr, "/cometd", tr.receive,
                                              tr.send), timeout=10)

    asyncio.run(run())
    return tr


async def _handshake_async(mgr) -> tuple[str, dict]:
    """Handshake through the real ASGI handler (usable inside a loop)."""
    tr = _Transport([{"channel": "/meta/handshake", "id": 1,
                      "version": "1.0",
                      "supportedConnectionTypes": ["long-polling", "streaming"]}])
    await _handle_cometd(mgr, "/cometd", tr.receive, tr.send)
    ack = tr.json()[0]
    return ack["clientId"], ack


def _asgi_handshake(mgr) -> tuple[str, dict]:
    return asyncio.run(_handshake_async(mgr))


# ---------------------------------------------------------------------------
# Handshake (Perl Cometd.pm:246-262)
# ---------------------------------------------------------------------------

def test_asgi_handshake_advice_timeout_is_60000_ms():
    """``timeout => LONG_POLLING_TIMEOUT`` = 60000 ms, interval 0 (:248-252)."""
    mgr, _rec = _manager()
    _cid, ack = _asgi_handshake(mgr)
    assert ack["successful"] is True
    assert ack["version"] == "1.0"
    assert ack["supportedConnectionTypes"] == ["long-polling", "streaming"]
    assert ack["advice"] == {"reconnect": "retry",
                             "interval": LONG_POLLING_INTERVAL,
                             "timeout": LONG_POLL_TIMEOUT_MS}
    assert ack["advice"]["timeout"] == 60000
    assert re.match(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} "
                    r"\d{2}:\d{2}:\d{2} GMT$", ack["timestamp"]), ack["timestamp"]


# ---------------------------------------------------------------------------
# /meta/connect advice + ack (Perl Cometd.pm:271-280, 302-306)
# ---------------------------------------------------------------------------

def test_long_polling_connect_advice_interval_zero_and_timeout_60000_ms():
    """Perl :277-279: interval 0 for long-polling; timeout in ms, not 60."""
    mgr, _rec = _manager()
    cid, _hs = _asgi_handshake(mgr)
    tr = _post(mgr, [{"channel": "/meta/connect", "clientId": cid, "id": 2,
                      "connectionType": "long-polling",
                      "advice": {"timeout": 0}}])
    acks = [m for m in tr.json() if m.get("channel") == "/meta/connect"]
    assert len(acks) == 1, tr.json()
    ack = acks[0]
    assert ack["successful"] is True
    assert ack["clientId"] == cid
    assert ack["id"] == 2
    assert ack["advice"]["interval"] == LONG_POLLING_INTERVAL == 0
    assert ack["advice"]["timeout"] == LONG_POLL_TIMEOUT_MS == 60000
    assert re.match(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} "
                    r"\d{2}:\d{2}:\d{2} GMT$", ack["timestamp"]), ack["timestamp"]


def test_client_advice_timeout_zero_answers_the_poll_immediately():
    """Perl Cometd.pm:302-306 — the client picks the hold time.

    Before this, the ASGI long-poll always held for the full 60 s: a client
    that asks for an immediate reply (Bayeux ``advice: {timeout: 0}``) blew
    its own network timeout and reported "connection failed".
    """
    mgr, _rec = _manager()
    cid, _hs = _asgi_handshake(mgr)
    started = time.monotonic()
    tr = _post(mgr, [{"channel": "/meta/connect", "clientId": cid, "id": 2,
                      "connectionType": "long-polling",
                      "advice": {"timeout": 0}}])
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"hold ignored the client timeout ({elapsed:.1f}s)"
    assert any(m.get("channel") == "/meta/connect" for m in tr.json())
    # The parsed value is milliseconds -> seconds, and a client may scale it.
    assert connect_timeout({"advice": {"timeout": 250}}) == 0.25
    assert connect_timeout({"advice": {"timeout": 60000}}) == 60.0
    assert connect_timeout({}) == 60.0          # Perl's LONG_POLLING_TIMEOUT


def test_streaming_connect_uses_retry_delay_interval():
    """Perl :45/:278: streaming advice interval = RETRY_DELAY (5000 ms).

    The native stream already did this; the ASGI path advertised 0. Also
    asserts the one and only connect ack (``handle_messages`` never answers
    /meta/connect) and the chunked framing.
    """
    async def run():
        mgr, _rec = _manager()
        cid, _hs = await _handshake_async(mgr)
        tr = _Transport([{"channel": "/meta/connect", "clientId": cid,
                          "id": 2, "connectionType": "streaming"}])
        task = asyncio.create_task(
            _handle_cometd(mgr, "/cometd", tr.receive, tr.send))
        try:
            await asyncio.wait_for(tr.first_chunk.wait(), timeout=5)
            reply = json.loads(tr.chunks[0])
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass
        return cid, tr, reply

    cid, tr, reply = asyncio.run(run())
    acks = [m for m in reply if m.get("channel") == "/meta/connect"]
    assert len(acks) == 1, reply
    ack = acks[0]
    assert ack["clientId"] == cid
    assert ack["advice"]["interval"] == RETRY_DELAY_MS == 5000
    assert ack["advice"]["timeout"] == LONG_POLL_TIMEOUT_MS
    assert "timestamp" in ack
    # uvicorn derives ``Transfer-Encoding: chunked`` from the ASGI framing; the
    # handler's contract is the held-open body (more_body=True), which the live
    # probe against a real uvicorn confirmed as chunked on the wire. The task was
    # cancelled by the test, so only the FIRST body event is inspected.
    bodies = [e for e in tr.sent if e.get("type") == "http.response.body"]
    assert bodies[0]["more_body"] is True


# ---------------------------------------------------------------------------
# Unknown clientId (Perl Cometd.pm:228-244)
# ---------------------------------------------------------------------------

def test_unknown_clientid_long_poll_connect_gets_no_success_ack():
    """Advice alone: ``successful: false`` + reconnect handshake, no ack.

    The ASGI path used to append a fabricated ``successful: true``
    /meta/connect with ``clientId: 'deadbeef'`` and ``timeout: 60`` next to
    the advice.
    """
    mgr, _rec = _manager()
    tr = _post(mgr, [{"channel": "/meta/connect", "clientId": "deadbeef",
                      "id": 7, "connectionType": "long-polling"}])
    payload = tr.json()
    assert not any(m.get("channel") == "/meta/connect" and m.get("successful")
                   for m in payload), payload
    advices = [m for m in payload if m.get("advice")]
    assert len(advices) == 1, payload
    assert advices[0]["channel"] == "/meta/connect"
    assert advices[0]["successful"] is False
    assert advices[0]["clientId"] is None
    assert advices[0]["error"] == "invalid clientId"
    assert advices[0]["advice"] == {"reconnect": "handshake",
                                    "interval": LONG_POLLING_INTERVAL}
    assert mgr.get("deadbeef") is None, "an unknown cid must not be minted"


def test_unknown_clientid_streaming_connect_opens_no_stream():
    """Perl :228-244 ``last`` before :292 — no connect ack, no chunked body."""
    async def run():
        mgr, _rec = _manager()
        tr = _Transport([{"channel": "/meta/connect", "clientId": "deadbeef",
                          "id": 7, "connectionType": "streaming"}])
        await asyncio.wait_for(
            _handle_cometd(mgr, "/cometd", tr.receive, tr.send), timeout=10)
        return tr

    tr = asyncio.run(run())
    assert not tr.streaming, "an unknown cid must not open a stream"
    assert "chunked" not in tr.headers.get("transfer-encoding", "")
    payload = tr.json()
    assert not any(m.get("channel") == "/meta/connect" and m.get("successful")
                   for m in payload), payload
    assert payload[0]["advice"]["reconnect"] == "handshake"


def test_unknown_clientid_slim_subscribe_is_not_acked_as_success():
    """Every message from an unknown cid gets the advice, never an ack."""
    mgr, _rec = _manager()
    tr = _post(mgr, [{"channel": "/slim/subscribe", "clientId": "deadbeef",
                      "id": 3,
                      "data": {"request": [PLAYER, ["status", "-", "1"]],
                               "response": "/deadbeef/slim/playerstatus"}}])
    payload = tr.json()
    assert payload[0]["error"] == "invalid clientId"
    assert payload[0]["successful"] is False
    assert not any(m.get("successful") is True for m in payload), payload
    assert mgr.get("deadbeef") is None


# ---------------------------------------------------------------------------
# Acks carry the clientId (Perl :363/:456/:577)
# ---------------------------------------------------------------------------

def test_acks_carry_client_id_on_the_asgi_path():
    """anchor: /meta/subscribe, /slim/subscribe and connect all echo clientId."""
    mgr, _rec = _manager()
    cid, _hs = _asgi_handshake(mgr)

    meta = _post(mgr, [{"channel": "/meta/subscribe", "clientId": cid, "id": 2,
                        "subscription": f"/{cid}/slim/menustatus/{PLAYER}"}])
    meta_acks = [m for m in meta.json() if m.get("channel") == "/meta/subscribe"]
    assert meta_acks and all(a["clientId"] == cid for a in meta_acks), meta.json()
    assert meta_acks[0]["subscription"] == f"/{cid}/slim/menustatus/{PLAYER}"

    slim = _post(mgr, [{"channel": "/slim/subscribe", "clientId": cid, "id": 3,
                        "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                                 "response": f"/{cid}/slim/playerstatus/{PLAYER}"}}])
    slim_acks = [m for m in slim.json() if m.get("channel") == "/slim/subscribe"]
    assert len(slim_acks) == 1, slim.json()
    assert slim_acks[0]["clientId"] == cid
    assert slim_acks[0]["subscription"] == \
        f"/{cid}/slim/playerstatus/{PLAYER}"

    req = _post(mgr, [{"channel": "/slim/request", "clientId": cid, "id": 4,
                       "data": {"request": [PLAYER, ["status", "-", "1"]],
                                "response": f"/{cid}/slim/request"}}])
    req_acks = [m for m in req.json() if m.get("channel") == "/slim/request"]
    assert req_acks and req_acks[0]["clientId"] == cid, req.json()

    conn = _post(mgr, [{"channel": "/meta/connect", "clientId": cid, "id": 5,
                        "connectionType": "long-polling",
                        "advice": {"timeout": 0}}])
    conn_acks = [m for m in conn.json() if m.get("channel") == "/meta/connect"]
    assert conn_acks and conn_acks[0]["clientId"] == cid, conn.json()


# ---------------------------------------------------------------------------
# First state after subscribing (Perl :454-475, request+subscribe)
# ---------------------------------------------------------------------------

def test_playerstatus_subscribe_seeds_state_on_the_subscribed_channel():
    """The reply to /slim/subscribe already carries the first state."""
    mgr, rec = _manager()
    cid, _hs = _asgi_handshake(mgr)
    channel = f"/{cid}/slim/playerstatus/{PLAYER}"
    tr = _post(mgr, [{"channel": "/slim/subscribe", "clientId": cid, "id": 2,
                      "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                               "response": channel}}])
    payload = tr.json()
    seeds = [m for m in payload if m.get("channel") == channel]
    assert seeds, payload
    assert seeds[0]["data"]["current_title"] == "Titel 51994"
    assert seeds[0]["id"] == 2, "SqueezeClient/libcometd needs the request id"
    assert rec.calls[-1] == [PLAYER, JIVE_STATUS_CMD]


def test_menustatus_subscribe_seeds_state_on_the_subscribed_channel():
    """A channel-only /meta/subscribe for menustatus gets the home menu."""
    mgr, _rec = _manager()
    cid, _hs = _asgi_handshake(mgr)
    channel = f"/{cid}/slim/menustatus/{PLAYER}"
    tr = _post(mgr, [{"channel": "/meta/subscribe", "clientId": cid, "id": 2,
                      "subscription": channel}])
    payload = tr.json()
    seeds = [m for m in payload if m.get("channel") == channel]
    assert seeds, payload
    assert seeds[0]["data"][1][0]["id"] == "myMusic"


def test_resubscribe_keeps_the_stored_request():
    """Perl :357 only adds the channel; the request from :456 survives.

    The second reply must still be driven by the stored ``status …`` request
    (and its ``subscribe:600``), not by the channel-only jive default.
    """
    mgr, rec = _manager()
    cid, _hs = _asgi_handshake(mgr)
    channel = f"/{cid}/slim/playerstatus/{PLAYER}"
    _post(mgr, [{"channel": "/slim/subscribe", "clientId": cid, "id": 2,
                 "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                          "response": channel}}])
    stored = mgr.get(cid).subscriptions[channel]
    assert stored["request"] == [PLAYER, JIVE_STATUS_CMD]

    tr = _post(mgr, [{"channel": "/meta/subscribe", "clientId": cid, "id": 3,
                      "subscription": channel}])
    assert mgr.get(cid).subscriptions[channel]["request"] == \
        [PLAYER, JIVE_STATUS_CMD], "a channel-only resubscribe wiped the request"
    seeds = [m for m in tr.json() if m.get("channel") == channel]
    assert seeds and seeds[0]["data"]["current_title"] == "Titel 51994"
    assert rec.calls[-1] == [PLAYER, JIVE_STATUS_CMD]
    assert len(mgr.get(cid).subscriptions) == 1


# ---------------------------------------------------------------------------
# /meta/connect without a clientId (Perl Cometd.pm:200-212)
# ---------------------------------------------------------------------------

def test_connect_without_clientid_gets_no_fabricated_ack():
    """Perl discards a message without a clientId: no ack, no stream."""
    mgr, _rec = _manager()
    tr = _post(mgr, [{"channel": "/meta/connect", "id": 3,
                      "connectionType": "long-polling"}])
    payload = tr.json()
    assert not any(m.get("channel") == "/meta/connect" for m in payload), payload
