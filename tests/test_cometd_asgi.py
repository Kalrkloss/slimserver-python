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

A second round (live symptom: Squeezer showed albums / track info only after a
whole poll cycle — "es dauert immer sehr lange, bis sie erscheinen") closed two
further ASGI-only gaps:

* a finished ``/slim/request`` or ``/slim/subscribe`` reply was swallowed by the
  request POST's OWN response.  Perl routes a non-async request result by the
  transport of the connection that carried it (Cometd.pm:584-589): unless that
  is a long-polling connection it calls ``$manager->deliver_events``, which
  writes into the client's registered connection *immediately*
  (Manager.pm:247-263) — and the native stream does the same (its in-stream
  POST branch sends only the acks, cometd_stream.py:471-485).  The app reads its
  results off the connect channel, so it waited for the next poll: measured
  0.001 s now, versus never (capped at 3 s) before;
* the streaming ``/meta/connect`` loop never read ``receive()`` again, and
  uvicorn reports a vanished peer as ``http.disconnect`` instead of cancelling
  the task — so ``CometdManager`` kept every dead client (open push loop,
  ``connections > 0`` so the idle reaper spared it) forever.  The event wait now
  races that signal and the client is reaped like Perl's ``webCloseHandler`` ->
  ``disconnectClient`` (Cometd.pm:1003).

Perl references (read-only ``/tmp/lms-ref``):

* ``Slim/Web/Cometd.pm:45-49``     — RETRY_DELAY / INTERVAL / TIMEOUT / AUTOKILL
* ``Slim/Web/Cometd.pm:200-212``   — message without clientId is dropped
* ``Slim/Web/Cometd.pm:228-244``   — unknown clientId -> advice + ``last``
* ``Slim/Web/Cometd.pm:246-262``   — /meta/handshake ack + advice
* ``Slim/Web/Cometd.pm:267``       — transport of THIS connect (``connectionType``)
* ``Slim/Web/Cometd.pm:271-280``   — /meta/(re)connect ``first_event`` ack
* ``Slim/Web/Cometd.pm:286``       — register_connection on (re)connect
* ``Slim/Web/Cometd.pm:295/300``   — store the transport (streaming/long-polling)
* ``Slim/Web/Cometd.pm:302-306``   — client may override the poll timeout
* ``Slim/Web/Cometd.pm:357/363``   — /meta/subscribe + clientId ack
* ``Slim/Web/Cometd.pm:427/456``   — /slim/subscribe ack + clientId
* ``Slim/Web/Cometd.pm:466-475``   — /slim/subscribe result routed by transport
* ``Slim/Web/Cometd.pm:577``       — /slim/request ack + clientId
* ``Slim/Web/Cometd.pm:584-589``   — /slim/request result routed by transport
* ``Slim/Web/Cometd/Manager.pm:247-263`` — deliver_events: registered conn or queue

A third round (live symptom: "Squeeze Client verbindet jetzt und kann abspielen,
aber nur verzögert") made the *routing* of a finished result exact. Perl sends
the result of a ``/slim/request`` (Cometd.pm:584-589) or ``/slim/subscribe``
(:466-475) into the requesting POST's OWN response when the transport is
``long-polling`` and through ``$manager->deliver_events`` otherwise
(Manager.pm:247-263). The ASGI path routed everything by
``has_live_connection``, which every long-polling client has: its result only
ever appeared on the Connect channel, so the client saw it one poll cycle
late. The transport is now honoured per client:

* ``long-polling``           -> the result rides in the POST's own response
  (measured: 0.00045 s, versus "only on the held connect" before);
* ``streaming``/websocket    -> ``deliver_events`` (unchanged, 0.00088 s on
  the open stream);
* no registered connection   -> the POST response (standalone request, the
  documented deviation).

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
    DISCONNECT_GRACE,
    LONG_POLL_TIMEOUT_MS,
    LONG_POLLING_INTERVAL,
    RETRY_DELAY_MS,
    STREAMING_HOLD_WINDOW,
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


async def _post_async(mgr, payload) -> _Transport:
    """``_post`` for use inside an already running loop."""
    tr = _Transport(payload)
    await asyncio.wait_for(_handle_cometd(mgr, "/cometd", tr.receive,
                                          tr.send), timeout=10)
    return tr


class _HeldTransport(_Transport):
    """A POST whose response stays open (a streaming /meta/connect).

    ``receive`` never reports ``http.disconnect``, so the held stream is not
    torn down behind the test's back — uvicorn signals a vanished peer
    exactly that way (``_watch_disconnect``).
    """

    def __init__(self, payload) -> None:
        super().__init__(payload)
        self._forever = asyncio.Event()

    async def receive(self) -> dict:
        if not self._delivered:
            self._delivered = True
            return {"type": "http.request",
                    "body": json.dumps(self.payload).encode(),
                    "more_body": False}
        await self._forever.wait()
        return {"type": "http.disconnect"}


async def _wait_for_chunk_channel(tr: "_Transport", channel: str,
                                  timeout: float = 3.0):
    """First frame on a held response mentioning ``channel`` (None on timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for chunk in list(tr.chunks):
            for msg in json.loads(chunk):
                if isinstance(msg, dict) and msg.get("channel") == channel:
                    return msg
        await asyncio.sleep(0.02)
    return None


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


# ---------------------------------------------------------------------------
# Transport routing of a finished result (Perl :584-589 / :466-475)
#
# Perl sends the result of a /slim/request or /slim/subscribe into the
# requesting POST's OWN response when the transport is ``long-polling``
# (:585-587, :467-469) and through ``$manager->deliver_events`` otherwise
# (:589, :475 -> Manager.pm:247-263). Routing everything by
# ``has_live_connection`` — which EVERY long-polling client has after its
# connect — put a long-polling client's result on the Connect channel only,
# so it saw it one poll cycle late (the live "nur verzögert" symptom).
# ---------------------------------------------------------------------------

def test_handshake_alone_does_not_fix_a_transport():
    """Perl :246-262: the handshake answers, it does not set a transport.

    Only the /meta/(re)connect branch writes ``$conn->[HTTP_CLIENT]
    ->transport`` (:295 streaming / :300 long-polling); a client that only
    handshakes must not be classified from ``supportedConnectionTypes``.
    """
    mgr, _rec = _manager()
    cid, _ack = _asgi_handshake(mgr)
    client = mgr.get(cid)
    assert client is not None and client.transport == ""


def test_long_polling_request_result_rides_in_the_post_reply():
    """Perl :584-589 — a long-polling request carries the result itself.

    Order is Perl's: the ack (:577) first, the result (:587) after it. The
    result must NOT be queued for the connect channel as well (Perl pushes it
    into ``@events`` *instead of* calling deliver_events).
    """
    mgr, rec = _manager()
    cid, _hs = _asgi_handshake(mgr)
    channel = f"/{cid}/slim/request"
    conn = _post(mgr, [{"channel": "/meta/connect", "clientId": cid, "id": 2,
                        "connectionType": "long-polling",
                        "advice": {"timeout": 0}}])
    assert any(m.get("channel") == "/meta/connect" for m in conn.json())
    client = mgr.get(cid)
    assert client is not None and client.transport == "long-polling"

    started = time.monotonic()
    tr = _post(mgr, [{"channel": "/slim/request", "clientId": cid, "id": 3,
                      "data": {"request": [PLAYER, ["status", "-", "1"]],
                               "response": channel}}])
    elapsed = time.monotonic() - started
    payload = tr.json()
    assert [m["channel"] for m in payload] == ["/slim/request", channel], payload
    assert payload[0]["successful"] is True and payload[0]["clientId"] == cid
    assert payload[1]["data"]["current_title"] == "Titel 51994"
    assert payload[1]["id"] == 3, "libcometd correlates the result by id"
    assert rec.calls[-1] == [PLAYER, ["status", "-", "1"]]
    client = mgr.get(cid)
    assert client is not None and client.events == [], \
        "the result was queued AND sent twice"
    assert elapsed < 0.2, f"the long-polling reply took {elapsed:.3f}s"


def test_long_polling_subscribe_result_rides_in_the_post_reply():
    """Perl :466-475 — same routing for /slim/subscribe (+ :472).

    Perl also clears the pending delayed response there, so the POST answers
    immediately instead of being held like a connect.
    """
    mgr, _rec = _manager()
    cid, _hs = _asgi_handshake(mgr)
    channel = f"/{cid}/slim/playerstatus/{PLAYER}"
    _post(mgr, [{"channel": "/meta/connect", "clientId": cid, "id": 2,
                 "connectionType": "long-polling", "advice": {"timeout": 0}}])

    started = time.monotonic()
    tr = _post(mgr, [{"channel": "/slim/subscribe", "clientId": cid, "id": 3,
                      "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                               "response": channel}}])
    elapsed = time.monotonic() - started
    payload = tr.json()
    assert [m["channel"] for m in payload] == ["/slim/subscribe", channel], payload
    assert payload[1]["data"]["current_title"] == "Titel 51994"
    assert payload[0]["subscription"] == channel
    client = mgr.get(cid)
    assert client is not None and client.events == []
    assert elapsed < 0.2, f"the subscribe reply took {elapsed:.3f}s"


def test_streaming_request_result_goes_to_the_open_connection():
    """Perl :589 — anything but long-polling uses ``deliver_events``.

    The result must reach the held stream (Manager.pm:247-263 writes into the
    registered connection) and must NOT be duplicated into the request POST's
    own response.
    """
    async def run():
        mgr, _rec = _manager()
        cid, _hs = await _handshake_async(mgr)
        channel = f"/{cid}/slim/request"
        stream = _HeldTransport([{"channel": "/meta/connect", "clientId": cid,
                                  "id": 2, "connectionType": "streaming"}])
        task = asyncio.create_task(_handle_cometd(mgr, "/cometd",
                                                  stream.receive, stream.send))
        await asyncio.wait_for(stream.first_chunk.wait(), timeout=5)
        client = mgr.get(cid)
        assert client is not None and client.transport == "streaming"

        tr = await _post_async(mgr, [{
            "channel": "/slim/request", "clientId": cid, "id": 3,
            "data": {"request": [PLAYER, ["status", "-", "1"]],
                     "response": channel}}])
        framed = await _wait_for_chunk_channel(stream, channel)
        task.cancel()
        return tr.json(), framed

    reply, framed = asyncio.run(run())
    assert [m["channel"] for m in reply] == ["/slim/request"], reply
    assert framed is not None, "the result never reached the open stream"
    assert framed["data"]["current_title"] == "Titel 51994"


def test_connect_switch_moves_the_result_to_the_open_stream():
    """A later /meta/(re)connect overwrites the transport (Perl :295/:300).

    A client that reconnects with ``connectionType: streaming`` (its old
    long-poll died) must be routed by the NEW connect, not by the one that
    set ``long-polling`` earlier.
    """
    async def run():
        mgr, _rec = _manager()
        cid, _hs = await _handshake_async(mgr)
        channel = f"/{cid}/slim/request"
        await _post_async(mgr, [{"channel": "/meta/connect", "clientId": cid,
                                 "id": 2, "connectionType": "long-polling",
                                 "advice": {"timeout": 0}}])
        client = mgr.get(cid)
        assert client is not None and client.transport == "long-polling"

        stream = _HeldTransport([{"channel": "/meta/connect", "clientId": cid,
                                  "id": 3, "connectionType": "streaming"}])
        task = asyncio.create_task(_handle_cometd(mgr, "/cometd",
                                                  stream.receive, stream.send))
        await asyncio.wait_for(stream.first_chunk.wait(), timeout=5)
        client = mgr.get(cid)
        assert client is not None and client.transport == "streaming"

        tr = await _post_async(mgr, [{
            "channel": "/slim/request", "clientId": cid, "id": 4,
            "data": {"request": [PLAYER, ["status", "-", "1"]],
                     "response": channel}}])
        framed = await _wait_for_chunk_channel(stream, channel)
        task.cancel()
        return tr.json(), framed

    reply, framed = asyncio.run(run())
    assert [m["channel"] for m in reply] == ["/slim/request"], reply
    assert framed is not None, "the switched client kept the old transport"
    assert framed["data"]["current_title"] == "Titel 51994"


def test_request_with_no_connection_answers_in_the_post_reply():
    """No registered connection -> the POST response (documented deviation).

    Perl queues the event for the next connect when it has no connection to
    deliver to (Manager.pm:262 ``queue_events``); the ASGI path answers the
    standalone request instead, which is what it did before this change.
    """
    mgr, _rec = _manager()
    cid, _hs = _asgi_handshake(mgr)
    channel = f"/{cid}/slim/request"
    client = mgr.get(cid)
    assert client is not None and client.transport == ""
    tr = _post(mgr, [{"channel": "/slim/request", "clientId": cid, "id": 4,
                      "data": {"request": [PLAYER, ["status", "-", "1"]],
                               "response": channel}}])
    payload = tr.json()
    assert [m["channel"] for m in payload] == ["/slim/request", channel], payload
    assert payload[1]["data"]["current_title"] == "Titel 51994"


def test_long_poll_advice_timeout_still_holds_the_connect():
    """Perl :302-306 survives the routing change: the CLIENT picks the hold.

    A long-polling connect with ``advice.timeout`` is held for exactly that
    time and carries no request result (that now rides in the request POST).
    """
    async def run():
        mgr, _rec = _manager()
        cid, _hs = await _handshake_async(mgr)
        tr = _Transport([{"channel": "/meta/connect", "clientId": cid, "id": 2,
                          "connectionType": "long-polling",
                          "advice": {"timeout": 150}}])
        started = time.monotonic()
        await asyncio.wait_for(_handle_cometd(mgr, "/cometd", tr.receive,
                                              tr.send), timeout=10)
        return tr.json(), time.monotonic() - started

    payload, elapsed = asyncio.run(run())
    assert [m["channel"] for m in payload] == ["/meta/connect"], payload
    assert 0.1 <= elapsed < 1.0, f"the hold ignored advice.timeout ({elapsed:.3f}s)"


# ---------------------------------------------------------------------------
# Live ASGI transport on a real uvicorn (ephemeral port, stub RPC).
#
# These are the only tests in this file that put the handler behind a real
# HTTP stack, because the two behaviours they pin are transport-level and
# cannot be observed with the in-process ``_Transport`` fake:
#
# * uvicorn frames a held-open ASGI body as ``Transfer-Encoding: chunked``
#   and — crucially — reports a vanished client by handing the app
#   ``http.disconnect`` on ``receive()`` (it does NOT cancel the task, which
#   is why the old streaming loop leaked every client whose socket died);
# * the latency the user sees in Squeezer is the round trip from a POST to
#   the *frame on the open connection*, not the POST's own response.
#
# No live server is touched: ``uvicorn.Server.serve()`` binds port 0 in this
# process. ``create_app`` is deliberately not used — its auth gate reads the
# config singleton on every request, which would pollute the timings; the
# /cometd ASGI handler is exactly what is under test here.
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402

import uvicorn  # noqa: E402


class _LiveRPC:
    """Stub slim.request dispatcher: no DB, no live server."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def handle_request(self, body: bytes) -> bytes:
        payload = json.loads(body)
        _player, cmd = payload.get("params", ["", []])
        self.calls.append(cmd)
        return json.dumps({
            "id": 1, "method": "slim.request",
            "result": {"mode": "play", "power": 1,
                       "current_title": "Titel 51994",
                       "playlist_tracks": 1,
                       "item_loop": [{"title": "Titel 51994"}]},
        }).encode()


def _cometd_app(mgr):
    """ASGI wrapper around the real /cometd handler (no auth, no DB)."""

    async def app(scope, receive, send):
        if scope.get("type") != "http":
            return
        await _handle_cometd(mgr, scope.get("path", "/cometd"), receive, send)

    return app


@asynccontextmanager
async def _live_server(mgr):
    """One uvicorn on an ephemeral port, serving the /cometd ASGI handler."""
    config = uvicorn.Config(_cometd_app(mgr), host="127.0.0.1", port=0,
                            log_level="warning", lifespan="off",
                            loop="asyncio", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):  # noqa: PERF203
            task.cancel()


class _Client:
    """Minimal chunked-aware HTTP/1.1 client over asyncio streams."""

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def open(cls, port: int) -> "_Client":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return cls(reader, writer)

    async def post(self, payload, path: str = "/cometd") -> None:
        body = json.dumps(payload).encode()
        self.writer.write(
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await self.writer.drain()

    async def read_headers(self, timeout: float = 5.0):
        head = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), timeout)
        lines = head.decode("latin1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()
        return status, headers

    async def read_chunk(self, timeout: float = 5.0) -> bytes:
        """One chunk of a ``Transfer-Encoding: chunked`` body."""
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        size = int(line.strip().split(b";")[0], 16)
        if size == 0:
            return b""
        data = await asyncio.wait_for(self.reader.readexactly(size), timeout)
        await self.reader.readexactly(2)
        return data

    async def read_full(self, timeout: float = 5.0) -> bytes:
        """One complete (non-streaming) response body."""
        _status, headers = await self.read_headers(timeout)
        if headers.get("transfer-encoding", "").lower() == "chunked":
            parts = []
            while True:
                chunk = await self.read_chunk(timeout)
                if not chunk:
                    break
                parts.append(chunk)
            return b"".join(parts)
        length = int(headers.get("content-length", "0") or 0)
        return await asyncio.wait_for(self.reader.readexactly(length), timeout)

    async def request(self, payload, path: str = "/cometd", timeout: float = 5.0):
        await self.post(payload, path)
        return await self.read_full(timeout)

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _live_handshake(port: int) -> str:
    client = await _Client.open(port)
    try:
        raw = await client.request([{
            "channel": "/meta/handshake", "id": 1, "version": "1.0",
            "ext": {"uuid": "aabbccdd-1122-3344-5566-778899aabbcc"},
        }])
        return json.loads(raw)[0]["clientId"]
    finally:
        client.close()


async def _wait_for_channel(client: _Client, channel: str, deadline: float):
    """First frame on ``client`` mentioning ``channel`` (None on timeout)."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            chunk = await client.read_chunk(timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if not chunk:
            return None
        for msg in json.loads(chunk):
            if isinstance(msg, dict) and msg.get("channel") == channel:
                return msg


def test_live_streaming_connect_delivers_command_reply_on_the_open_stream():
    """A finished /slim/request reply reaches the OPEN stream at once.

    Perl routes a non-async request result by the *transport* of the
    connection that carried it (Cometd.pm:584-589): on a streaming
    connection it calls ``$manager->deliver_events($result)``, and
    ``Manager::deliver_events`` (Manager.pm:247-263) writes into the
    client's registered connection immediately.  The ASGI path instead let
    the request POST drain the queue into ITS OWN response, so the app —
    which reads the result off the connect channel — saw nothing until the
    next cycle.  This test measures POST -> frame on the open stream.
    """
    async def run():
        rpc = _LiveRPC()
        mgr = CometdManager(rpc)
        async with _live_server(mgr) as port:
            cid = await _live_handshake(port)
            response_channel = f"/{cid}/slim/request"

            stream = await _Client.open(port)
            await stream.post([{"channel": "/meta/connect", "clientId": cid,
                                "id": 2, "connectionType": "streaming"}])
            _status, headers = await stream.read_headers()
            assert headers.get("transfer-encoding", "").lower() == "chunked", headers
            first = json.loads(await stream.read_chunk())
            assert any(m.get("channel") == "/meta/connect" for m in first), first

            started = time.monotonic()
            command = await _Client.open(port)
            await command.post([{
                "channel": "/slim/request", "clientId": cid, "id": 3,
                "data": {"request": [PLAYER, ["status", "-", "1"]],
                         "response": response_channel},
            }])
            reply = json.loads(await command.read_full())
            hit = await _wait_for_channel(stream, response_channel,
                                          time.monotonic() + 3.0)
            latency = time.monotonic() - started
            command.close()
            stream.close()
            return hit, latency, reply

    hit, latency, reply = asyncio.run(run())
    print(f"\n[latency] command reply on the open stream: {latency:.3f}s")
    assert hit is not None, (
        "the reply never arrived on the open stream — the request POST "
        f"swallowed it (its own response was {json.dumps(reply)[:200]})")
    assert latency < 1.0, f"reply on the open stream took {latency:.3f}s"


def test_live_streaming_connect_delivers_server_push_immediately():
    """A server-side push (STAT -> notify_player_status) wakes the stream.

    ``CometdManager.push`` (web/cometd.py) is the Python form of
    ``Manager::deliver_events`` -> ``sendResponse`` (Manager.pm:262): it must
    wake the waiting connection, not wait for a timeout.  Measured from the
    notify call to the frame on the wire.
    """
    async def run():
        rpc = _LiveRPC()
        mgr = CometdManager(rpc)
        async with _live_server(mgr) as port:
            cid = await _live_handshake(port)
            channel = f"/{cid}/slim/playerstatus/{PLAYER}"

            subscribe = await _Client.open(port)
            await subscribe.post([{
                "channel": "/slim/subscribe", "clientId": cid, "id": 2,
                "data": {"request": [PLAYER, ["status", "-", "1", "subscribe:600"]],
                         "response": channel},
            }])
            await subscribe.read_full()
            subscribe.close()

            stream = await _Client.open(port)
            await stream.post([{"channel": "/meta/connect", "clientId": cid,
                                "id": 3, "connectionType": "streaming"}])
            await stream.read_headers()
            await stream.read_chunk()          # connect ack

            started = time.monotonic()
            await mgr.notify_player_status(PLAYER)
            pull = await _wait_for_channel(stream, channel,
                                           time.monotonic() + 3.0)
            latency = time.monotonic() - started
            stream.close()
            return pull, latency

    pull, latency = asyncio.run(run())
    print(f"\n[latency] server push on the open stream: {latency:.3f}s")
    assert pull is not None, "the push never arrived on the open stream"
    assert pull["data"]["current_title"] == "Titel 51994"
    assert latency < 1.0, f"push on the open stream took {latency:.3f}s"


def test_live_long_polling_connect_wakes_on_a_push():
    """A held long-poll answers as soon as events are queued (Perl :309-312).

    ``Manager::deliver_events`` calls ``sendResponse`` on the registered
    connection (Manager.pm:247-263) and ``sendResponse`` kills the hold timer
    (Cometd.pm:656-659) — the client does not wait for the 60 s to elapse.
    """
    async def run():
        rpc = _LiveRPC()
        mgr = CometdManager(rpc)
        async with _live_server(mgr) as port:
            cid = await _live_handshake(port)
            channel = f"/{cid}/slim/playerstatus/{PLAYER}"
            sub = await _Client.open(port)
            await sub.post([{
                "channel": "/slim/subscribe", "clientId": cid, "id": 2,
                "data": {"request": [PLAYER, ["status", "-", "1", "subscribe:600"]],
                         "response": channel},
            }])
            await sub.read_full()
            sub.close()

            poll = await _Client.open(port)
            await poll.post([{"channel": "/meta/connect", "clientId": cid,
                              "id": 3, "connectionType": "long-polling"}])
            # Nothing queued: the poll is held. Push after a short delay.
            started = time.monotonic()

            async def push_later():
                await asyncio.sleep(0.2)
                await mgr.notify_player_status(PLAYER)

            pusher = asyncio.create_task(push_later())
            body = json.loads(await poll.read_full(timeout=5.0))
            latency = time.monotonic() - started - 0.2
            await pusher
            poll.close()
            return body, latency

    body, latency = asyncio.run(run())
    print(f"\n[latency] push delivered to a held long-poll: {latency:.3f}s")
    assert any(m.get("channel") == f"/{PLAYER}/slim/playerstatus/{PLAYER}"
               or str(m.get("channel", "")).endswith(f"playerstatus/{PLAYER}")
               for m in body), json.dumps(body)[:300]
    assert latency < 1.0, f"held long-poll answered after {latency:.3f}s"


def test_live_long_polling_honours_the_client_advice_timeout():
    """Perl Cometd.pm:302-306 — the CLIENT picks the long-poll hold time.

    A Bayeux client asking for 300 ms must get its (empty) reply after ~0.3 s,
    not after the server default. The default for a client that sends no
    advice is Perl's ``LONG_POLLING_TIMEOUT => 60000`` ms (Cometd.pm:48),
    which is also the value handed out in the handshake advice
    (Cometd.pm:251) and the one libcometd clients are built against. A
    *streaming* connect must ignore ``advice.timeout`` — Perl reads it only in
    the long-polling branch (:298-307).
    """
    async def run():
        rpc = _LiveRPC()
        mgr = CometdManager(rpc)
        async with _live_server(mgr) as port:
            cid = await _live_handshake(port)
            poll = await _Client.open(port)
            started = time.monotonic()
            await poll.post([{"channel": "/meta/connect", "clientId": cid,
                              "id": 2, "connectionType": "long-polling",
                              "advice": {"timeout": 300}}])
            body = json.loads(await poll.read_full(timeout=5.0))
            elapsed = time.monotonic() - started
            poll.close()
            return body, elapsed

    body, elapsed = asyncio.run(run())
    print(f"\n[latency] long-poll held {elapsed:.3f}s (client asked 0.300s)")
    assert any(m.get("channel") == "/meta/connect" for m in body), body
    assert 0.2 <= elapsed < 1.0, f"hold ignored advice.timeout ({elapsed:.3f}s)"
    # Perl's own numbers: LONG_POLLING_TIMEOUT = 60000 ms (Cometd.pm:48).
    assert connect_timeout({}) == 60.0
    assert connect_timeout({"advice": {"timeout": 300}}) == 0.3
    assert LONG_POLL_TIMEOUT_MS == 60000


def test_live_dead_streaming_client_runs_perls_disconnect_grace():
    """A streaming socket that dies releases the client after Perl's grace.

    uvicorn does NOT cancel the ASGI task when the peer vanishes; it hands the
    app ``http.disconnect`` on ``receive()``.  The old streaming loop never
    called ``receive()`` again, so ``CometdManager.get(cid)`` stayed set
    forever and the client leaked (Perl: Cometd.pm:1003 -> disconnectClient).

    Perl does not drop the client at the moment the connection is lost: its
    webCloseHandler (Cometd.pm:1002-1015) unregisters the *connection* and arms
    ``disconnectClient`` for RETRY_DELAY * 2 = 10 s, a timer the next
    /meta/(re)connect kills (:283) — that is what lets a client that reconnects
    right away keep its subscriptions and its queued events. So the client must
    survive the loss and be reaped once the grace has expired, not before.
    """
    async def run():
        rpc = _LiveRPC()
        mgr = CometdManager(rpc)
        async with _live_server(mgr) as port:
            cid = await _live_handshake(port)
            stream = await _Client.open(port)
            await stream.post([{"channel": "/meta/connect", "clientId": cid,
                                "id": 2, "connectionType": "streaming"}])
            await stream.read_headers()
            await stream.read_chunk()
            assert mgr.get(cid) is not None
            stream.close()                      # the peer disappears
            for _ in range(100):
                client = mgr.get(cid)
                if client is not None and not client.connections \
                        and client.disconnect_at is not None:
                    break
                await asyncio.sleep(0.05)
            released = mgr.get(cid)
            # Not dropped while the grace runs (Perl's timer is still pending)
            still_there = mgr.kill_idle_clients()
            # ... and gone once it has expired (idle sweep = disconnectClient)
            mgr._clock = lambda: time.time() + DISCONNECT_GRACE + 1
            reaped = mgr.kill_idle_clients()
            return cid, released, still_there, reaped, mgr.get(cid)

    cid, released, still_there, reaped, after = asyncio.run(run())
    assert released is not None, "a lost connection dropped the client at once"
    assert released.connections == 0
    assert released.disconnect_at is not None, "Perl's disconnect timer unset"
    assert cid not in still_there and after is None, cid
    assert cid in reaped, f"grace expiry did not reap {cid}"


def test_live_push_right_after_a_command_reaches_the_open_stream():
    """The user's two Squeezer legs, measured back to back.

    (a) the command POST -> its reply frame on the OPEN stream, then
    (b) the server event the command triggers (STAT -> notify_player_status,
    protocol.py:3939) -> its push frame on the same stream. Both must be
    immediate: the app reads the result off the connect channel, so a reply
    that only lands in the command POST's own response is invisible to it.
    """
    async def run():
        rpc = _LiveRPC()
        mgr = CometdManager(rpc)
        async with _live_server(mgr) as port:
            cid = await _live_handshake(port)
            cmd_channel = f"/{cid}/slim/request"
            status_channel = f"/{cid}/slim/playerstatus/{PLAYER}"

            subscribe = await _Client.open(port)
            await subscribe.post([{
                "channel": "/slim/subscribe", "clientId": cid, "id": 2,
                "data": {"request": [PLAYER, ["status", "-", "1", "subscribe:600"]],
                         "response": status_channel},
            }])
            await subscribe.read_full()
            subscribe.close()

            stream = await _Client.open(port)
            await stream.post([{"channel": "/meta/connect", "clientId": cid,
                                "id": 3, "connectionType": "streaming"}])
            await stream.read_headers()
            await stream.read_chunk()

            t_command = time.monotonic()
            command = await _Client.open(port)
            await command.post([{
                "channel": "/slim/request", "clientId": cid, "id": 4,
                "data": {"request": [PLAYER, ["playlistcontrol", "cmd:load",
                                              "track_id:51994"]],
                         "response": cmd_channel},
            }])
            await command.read_full()
            cmd_reply = await _wait_for_channel(stream, cmd_channel,
                                                time.monotonic() + 3.0)
            cmd_latency = time.monotonic() - t_command
            command.close()

            t_push = time.monotonic()
            await mgr.notify_player_status(PLAYER)
            push = await _wait_for_channel(stream, status_channel,
                                           time.monotonic() + 3.0)
            push_latency = time.monotonic() - t_push
            stream.close()
            return cmd_reply, cmd_latency, push, push_latency

    cmd_reply, cmd_latency, push, push_latency = asyncio.run(run())
    print(f"\n[latency] (a) command reply on the open stream: {cmd_latency:.3f}s")
    print(f"[latency] (b) follow-up push on the open stream: {push_latency:.3f}s")
    assert cmd_reply is not None, "the command reply never reached the open stream"
    assert push is not None, "the follow-up push never reached the open stream"
    assert cmd_latency < 1.0, f"(a) took {cmd_latency:.3f}s"
    assert push_latency < 1.0, f"(b) took {push_latency:.3f}s"


def test_live_long_polling_request_result_rides_in_the_post_response():
    """The Squeeze Client leg: its own POST carries the result, at once.

    A long-polling client (SqueezeClient: handshake with
    ``supportedConnectionTypes: ["long-polling"]``, connect
    ``connectionType: "long-polling"``, one subscription on
    ``/<cid>/slim/request``) keeps a poll open on one connection and sends its
    requests on another — the pattern the live dev log shows
    (``lyrion-8``: handshake -> subscribe -> connect long-polling -> request).

    Perl answers that request POST with the result itself (Cometd.pm:584-589)
    so the client never has to wait for a poll cycle. Measured here: the reply
    must contain the result and must arrive in well under the 60 s hold.
    """
    async def run():
        rpc = _LiveRPC()
        mgr = CometdManager(rpc)
        async with _live_server(mgr) as port:
            cid = await _live_handshake(port)
            channel = f"/{cid}/slim/request"

            # The client's poll: held on its own connection (30 s advice).
            poll = await _Client.open(port)
            await poll.post([{"channel": "/meta/connect", "clientId": cid,
                              "id": 2, "connectionType": "long-polling",
                              "advice": {"timeout": 30000}}])
            await asyncio.sleep(0.1)          # let the poll register

            command = await _Client.open(port)
            t0 = time.monotonic()
            await command.post([{
                "channel": "/slim/request", "clientId": cid, "id": 3,
                "data": {"request": [PLAYER, ["status", "-", "1"]],
                         "response": channel}}])
            body = json.loads(await command.read_full(timeout=5.0))
            latency = time.monotonic() - t0
            command.close()

            # The held poll must NOT also carry it (no double delivery): if the
            # result had gone to the connect, the poll would answer right now
            # instead of staying held for its 30 s advice.
            poll_answered = True
            try:
                await asyncio.wait_for(poll.reader.readuntil(b"\r\n"), 0.5)
            except asyncio.TimeoutError:
                poll_answered = False
            poll.close()
            return body, latency, poll_answered

    body, latency, poll_answered = asyncio.run(run())
    print(f"\n[latency] long-polling request reply with the result: {latency:.3f}s")
    channels = [m.get("channel") for m in body]
    assert channels == ["/slim/request", f"/{body[0]['clientId']}/slim/request"], body
    assert body[0]["successful"] is True
    assert body[1]["data"]["current_title"] == "Titel 51994"
    assert latency < 0.2, (
        f"the long-polling client waited {latency:.3f}s for its own reply")
    assert not poll_answered, (
        "the result was delivered twice: the held connect answered as well")


# ---------------------------------------------------------------------------
# Streaming hold window + Perl's disconnect grace (the 10 s app teardown)
# ---------------------------------------------------------------------------

def test_streaming_hold_window_closes_the_response_and_arms_the_grace():
    """After STREAMING_HOLD_WINDOW of silence the stream ends — client kept.

    Perl holds a streaming response for as long as the socket lives
    (Cometd.pm:288-297 arms no timer for that branch; live probe: 20 s of
    silence on a bare connect). uvicorn cannot: it queues HTTP pipelined
    requests until the response completes (h11_impl.py:191-197, :278), so a
    POST the client sent behind the stream was never served — the app's own
    deadline for a non-connect message is libcometd's ``maxNetworkDelay``
    = 10000 ms (libcometd.js:1268, :380-389 adds advice.timeout only for
    ``metaConnect``), which is the 10 s after which the app re-handshaked.

    The window's value is Perl's RETRY_DELAY (Cometd.pm:45), the interval the
    connect ack advertises for re-connecting (Cometd.pm:277-279). The client
    itself is NOT dropped when the window closes: Perl's webCloseHandler arms
    ``disconnectClient`` for RETRY_DELAY * 2 (Cometd.pm:1010-1014) instead.
    """
    async def run():
        import lyrion.web.app as app_mod

        mgr, _rec = _manager()
        cid, _hs = await _handshake_async(mgr)
        old = app_mod.STREAMING_HOLD_WINDOW
        app_mod.STREAMING_HOLD_WINDOW = 0.1        # keep the test short
        try:
            tr = _HeldTransport([{"channel": "/meta/connect", "clientId": cid,
                                  "id": 2, "connectionType": "streaming"}])
            task = asyncio.create_task(
                _handle_cometd(mgr, "/cometd", tr.receive, tr.send))
            for _ in range(200):
                if not tr.streaming:
                    break
                await asyncio.sleep(0.02)
            client = mgr.get(cid)
            await asyncio.wait_for(task, timeout=5)
        finally:
            app_mod.STREAMING_HOLD_WINDOW = old
        return tr, client

    tr, client = asyncio.run(run())
    bodies = [e for e in tr.sent if e.get("type") == "http.response.body"]
    assert bodies[0]["more_body"] is True               # ack first, held open
    assert bodies[-1]["more_body"] is False, "the window did not close it"
    assert json.loads(bodies[-1]["body"]) == []         # Perl's empty batch
    assert not tr.streaming
    assert client is not None, "the client was dropped while its grace ran"
    assert client.connections == 0
    assert client.disconnect_at is not None             # Perl's 10 s timer
    assert STREAMING_HOLD_WINDOW == RETRY_DELAY_MS / 1000.0 == 5.0
    assert DISCONNECT_GRACE == 10.0


def test_streaming_hold_window_is_restarted_by_every_pushed_event():
    """A stream that keeps delivering events is not closed by the window."""
    async def run():
        import lyrion.web.app as app_mod

        mgr, _rec = _manager()
        cid, _hs = await _handshake_async(mgr)
        old = app_mod.STREAMING_HOLD_WINDOW
        app_mod.STREAMING_HOLD_WINDOW = 0.3
        try:
            tr = _HeldTransport([{"channel": "/meta/connect", "clientId": cid,
                                  "id": 2, "connectionType": "streaming"}])
            task = asyncio.create_task(
                _handle_cometd(mgr, "/cometd", tr.receive, tr.send))

            async def feed():
                for n in range(6):
                    await asyncio.sleep(0.15)
                    mgr.push(cid, {"channel": f"/{cid}/slim/x", "id": 0,
                                   "data": {"n": n}})

            pusher = asyncio.create_task(feed())
            await asyncio.sleep(0.8)                   # > 1 event, > 2 windows
            open_after_events = tr.streaming
            await pusher
            for _ in range(200):
                if not tr.streaming:
                    break
                await asyncio.sleep(0.02)
            await asyncio.wait_for(task, timeout=5)
        finally:
            app_mod.STREAMING_HOLD_WINDOW = old
        return tr, open_after_events

    tr, open_after_events = asyncio.run(run())
    assert open_after_events, (
        "an event-carrying stream was closed after one idle window")
    pushed = [m for chunk in tr.chunks for m in json.loads(chunk)
              if isinstance(m, dict) and str(m.get("channel", "")).startswith("/")]
    assert any(str(m.get("channel", "")).endswith("/slim/x") for m in pushed)


def test_live_post_behind_an_open_stream_is_served():
    """The app's real pattern: connect AND its next POST on one socket.

    Perl answers the queued POST while the stream is still open (measured
    live against LMS 9.1.1: 0.8 s — its HTTP layer keeps reading the socket,
    Slim/Web/HTTP.pm:277, and writes each response on its own,
    addHTTPResponse HTTP.pm:1895-1960). uvicorn defers pipelined requests
    until the response completes, so without the hold window this POST stayed
    unanswered until the client gave up — the reported
    "favourites never open" symptom.
    """
    async def run():
        import lyrion.web.app as app_mod

        rpc = _LiveRPC()
        mgr = CometdManager(rpc)
        old = app_mod.STREAMING_HOLD_WINDOW
        app_mod.STREAMING_HOLD_WINDOW = 0.3
        try:
            async with _live_server(mgr) as port:
                cid = await _live_handshake(port)
                client = await _Client.open(port)
                # both POSTs back to back on ONE socket, like the Android apps
                await client.post([{"channel": "/meta/connect", "clientId": cid,
                                    "id": 2, "connectionType": "streaming"}])
                await client.post([{
                    "channel": "/slim/request", "clientId": cid, "id": 3,
                    "data": {"request": [PLAYER, ["favorites", "items", "0", "100"]],
                             "response": f"/{cid}/slim/request/7"}}])
                started = time.monotonic()
                await client.read_headers()
                ack = json.loads(await client.read_chunk())
                while await client.read_chunk(timeout=5.0):
                    pass
                await client.reader.readexactly(2)     # chunked-body terminator
                awaited = time.monotonic() - started
                _status, headers = await client.read_headers(timeout=5.0)
                if headers.get("transfer-encoding", "").lower() == "chunked":
                    parts = []
                    while True:
                        part = await client.read_chunk(timeout=5.0)
                        if not part:
                            break
                        parts.append(part)
                    payload = b"".join(parts)
                else:
                    payload = await client.reader.readexactly(
                        int(headers["content-length"]))
                client.close()
                return cid, ack, json.loads(payload), awaited, mgr.get(cid), headers
        finally:
            app_mod.STREAMING_HOLD_WINDOW = old

    cid, ack, reply, awaited, client, headers = asyncio.run(run())
    print(f"\n[len] queued POST behind the open stream served after "
          f"{awaited:.2f}s, headers={headers}")
    assert ack[0]["channel"] == "/meta/connect" and ack[0]["successful"] is True
    channels = [m.get("channel") for m in reply]
    assert f"/{cid}/slim/request/7" in channels, reply
    # inside the client's own deadline for a non-connect message (10 s)
    assert awaited < 10.0, f"the queued POST waited {awaited:.1f}s"
    # ... and the client survived the end of that stream (Perl's grace)
    assert client is not None
