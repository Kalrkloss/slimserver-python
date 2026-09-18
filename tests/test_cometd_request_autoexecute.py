"""``subscribe:<n>`` of a ``/slim/request`` — Perl's repeating re-execution.

Regression (after b0e2b70b6 "kein stiller Stream-Abbruch"): Squeezer and
Squeeze Client showed no player status at all — no play symbol, no times, no
metadata.  The apps kept their cometd session for only ~60 s and then
re-handshaked under a NEW clientId (logcat ``Disconnected from event stream``
/ ``Connected … with client ID lyrion-4|lyrion-7|lyrion-8|lyrion-12``), so the
UI lost the binding to its subscriptions.

Cause: the apps ask for their periodic serverstatus with a ``subscribe:<n>``
token inside a ``/slim/request``:

    {'request': ['', ['serverstatus','0','2147483647','subscribe:60',…]],
     'response': '/<cid>/slim/serverstatus'}

Perl registers that for re-execution in the query itself —
``Slim/Control/Queries.pm:4593-4597`` (status) and ``:3869-3875``
(serverstatus):

    if (defined(my $timeout = $request->getParam('subscribe'))) {
        # register ourselves to be automatically re-executed on timeout
        $request->registerAutoExecute($timeout, \\&serverstatusQuery_filter);
    }

``Slim/Control/Request.pm:2167`` (``$timeout ne '-'``) stores it,
``:2176-2181`` arms ``setTimer($request, time() + $timeout, \\&__autoexecute)``
and ``:2467-2495`` re-executes the request and calls the cometd
``requestCallback`` (Cometd.pm:855-858/939-970) — a fresh push every n
seconds, on the original response channel, with the original message id.
The /slim/request branch of Cometd.pm (:545-558) goes through the SAME
``handleRequest``, and the modern WebSocket transport spells the rule out:
"lets Slim::Control::Request re-invoke us for subscribe:<n> style requests"
(``Slim/Plugin/WebSocket/Plugin.pm:282-283``).

Our code armed the timer only for a STORED ``/slim/subscribe`` — a
``/slim/request … subscribe:60`` stayed a one-shot.  The client's streaming
connect then went silent for longer than its own connect timeout
(``advice.timeout`` = 60 s, Cometd.pm:251; measured live: the app gave up
66 s after its connect), and Squeeze Client abandoned the session and
re-handshaked.  The invented empty ``[]`` batch of the old code had been
papering over exactly this gap.

These tests drive CometdManager in-process (no server, no database).
"""

import asyncio
import time

from lyrion.web.cometd import (
    CometdManager,
    subscribe_timeout,
)

PLAYER = "1c:87:2c:47:fc:36"
# Squeeze Client's serverstatus request (live log 2026-09-18 20:00:50).
SERVERSTATUS_CMD = ["serverstatus", "0", "2147483647", "subscribe:60",
                    "prefs:mediadirs"]
# SqueezePlay's Now-Playing subscription (Player.lua:993).
STATUS_CMD = ["status", "-", 10, "menu:menu", "useContextMenu:1",
              "subscribe:600"]


class _Recorder:
    """Fake dispatcher: records every request it is asked to run."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def dispatch(self, request: list) -> dict:
        self.calls.append(request)
        command = ""
        if len(request) > 1 and isinstance(request[1], list) and request[1]:
            command = request[1][0]
        if command == "status":
            return {"mode": "play", "current_title": "Titel 51994",
                    "item_loop": [{"title": "Titel 51994"}]}
        return {"player count": 1, "players_loop": [{"name": "Taverne"}]}


def _run(coro):
    return asyncio.run(coro)


def _manager():
    rec = _Recorder()
    mgr = CometdManager(rec)
    mgr._dispatch = rec.dispatch
    return mgr, rec


async def _handshake(mgr) -> str:
    replies = await mgr.handle_messages([{"channel": "/meta/handshake",
                                          "id": 1}])
    return replies[0]["clientId"]


async def _slim_request(mgr, cid, command, response, msg_id=5):
    return await mgr.handle_messages([{
        "channel": "/slim/request", "id": msg_id, "clientId": cid,
        "data": {"request": ["", command], "response": response},
    }])


async def _run_loop_for(mgr, seconds: float):
    """Run the real keepalive loop for ``seconds`` and collect the pushes."""
    task = asyncio.create_task(mgr.keepalive_loop())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
            pass


# ---------------------------------------------------------------------------
# Perl's getParam('subscribe') (Queries.pm:4593 / :3869)
# ---------------------------------------------------------------------------

def test_subscribe_timeout_parses_perls_token():
    """None = no token, -1 = 'subscribe:-', N = seconds (0 = store only)."""
    assert subscribe_timeout([]) is None
    assert subscribe_timeout(["status", "-", "1"]) is None
    assert subscribe_timeout(["status", "-", "1", "subscribe:600"]) == 600
    assert subscribe_timeout([PLAYER, ["status", "-", 1, "subscribe:0"]]) == 0
    assert subscribe_timeout([PLAYER, ["status", "-", 1, "subscribe:-"]]) == -1
    assert subscribe_timeout([["serverstatus", "0", "50",
                               "subscribe:60"]]) == 60
    # nested command array, exactly as the apps send it
    assert subscribe_timeout(["", ["serverstatus", "0", "2147483647",
                                   "subscribe:60", "prefs:mediadirs"]]) == 60


# ---------------------------------------------------------------------------
# A /slim/request that carries subscribe:N is NOT a one-shot (Perl parity)
# ---------------------------------------------------------------------------

def test_slim_request_subscribe_arms_a_repeating_request():
    """subscribe:60 arms Perl's timer — 60 s away, armed at registration."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/serverstatus"
        replies = await _slim_request(mgr, cid, SERVERSTATUS_CMD, response)
        client = mgr.get(cid)
        entries = dict(client.autoexecute)
        # the client's OWN reply carried the initial status ("Anlauf-Status")
        events = await mgr.wait_for_events(cid, timeout=0)
        return rec.calls, entries, replies, events, response, cid

    calls, entries, replies, events, response, cid = _run(run())
    # the request ran immediately and its result was pushed right away
    assert calls == [["", SERVERSTATUS_CMD]], calls
    assert len(events) == 1 and events[0]["channel"] == response, events
    # ... and it is registered for re-execution, keyed by channel + command
    assert list(entries) == [(response, "serverstatus")], entries
    entry = entries[(response, "serverstatus")]
    assert entry["interval"] == 60.0
    assert entry["request"] == ["", SERVERSTATUS_CMD]
    assert entry["id"] == 5
    # the immediate answer is the /slim/request ack (Perl Cometd.pm:575-589)
    assert any(r.get("channel") == "/slim/request" for r in replies), replies


def test_slim_request_without_the_token_stays_a_one_shot():
    """No ``subscribe:`` -> no timer (Perl: nothing is registered)."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/serverstatus"
        await _slim_request(mgr, cid, ["serverstatus", "0", "50"], response)
        client = mgr.get(cid)
        await mgr.wait_for_events(cid, timeout=0)
        await _run_loop_for(mgr, 2.2)
        return client.autoexecute, await mgr.wait_for_events(cid, timeout=0)

    entries, events = _run(run())
    assert entries == {}
    assert events == []


def test_subscribe_dash_and_zero_arm_no_timer():
    """Perl Request.pm:2167 stores nothing for '-', :2176 arms none for 0."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        dash = f"/{cid}/slim/serverstatus"
        zero = f"/{cid}/slim/playerstatus/{PLAYER}"
        await _slim_request(mgr, cid, ["serverstatus", "0", "50",
                                       "subscribe:-"], dash, msg_id=5)
        await _slim_request(mgr, cid, ["status", "-", "1", "subscribe:0"],
                            zero, msg_id=6)
        client = mgr.get(cid)
        armed = {k: v["interval"] for k, v in client.autoexecute.items()}
        await mgr.wait_for_events(cid, timeout=0)          # the two seeds
        await _run_loop_for(mgr, 2.2)
        return armed, await mgr.wait_for_events(cid, timeout=0)

    armed, events = _run(run())
    # '-' is not stored at all; 0 is remembered without a timer
    assert armed == {(f"/lyrion-1/slim/playerstatus/{PLAYER}", "status"): 0.0} \
        or list(armed.values()) == [0.0], armed
    assert events == []


def test_repeating_request_pushes_the_fresh_result_on_its_own_channel():
    """End to end: subscribe:1 -> one extra push per second, original id."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/serverstatus"
        await _slim_request(mgr, cid, ["serverstatus", "0", "50", "subscribe:1"],
                            response, msg_id=7)
        await mgr.wait_for_events(cid, timeout=0)          # the initial status
        rec.calls.clear()
        task = asyncio.create_task(mgr.keepalive_loop())
        started = time.monotonic()
        try:
            first = await mgr.wait_for_events(cid, timeout=5.0)
            first_delay = time.monotonic() - started
            second = await mgr.wait_for_events(cid, timeout=5.0)
            second_delay = time.monotonic() - started
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass
        return first, first_delay, second, second_delay, rec.calls

    first, first_delay, second, second_delay, calls = _run(run())
    # the timer is armed for now + N (Perl Request.pm:2179-2181), never fired
    # at registration time
    assert first_delay >= 1.0, f"first repeat after {first_delay:.2f}s"
    assert first and first[0]["channel"].endswith("/slim/serverstatus")
    assert first[0]["id"] == 7, first
    assert first[0]["data"]["player count"] == 1
    assert second and second_delay - first_delay >= 1.0
    assert calls and calls[0] == ["", ["serverstatus", "0", "50",
                                       "subscribe:1"]]


def test_unsubscribe_removes_the_repeating_request():
    """Perl's unregisterAutoExecute: no more pushes after /slim/unsubscribe."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/serverstatus"
        await _slim_request(mgr, cid, ["serverstatus", "0", "50", "subscribe:1"],
                            response, msg_id=9)
        client = mgr.get(cid)
        armed_before = dict(client.autoexecute)
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.handle_messages([{
            "channel": "/slim/unsubscribe", "clientId": cid, "id": 10,
            "data": {"unsubscribe": response},
        }])
        return armed_before, dict(client.autoexecute)

    before, after = _run(run())
    assert before, "the request was never armed"
    assert after == {}


def test_rearming_the_same_request_replaces_the_old_timer():
    """Perl Request.pm:2143-2161 kills the previous subscription first."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/serverstatus"
        await _slim_request(mgr, cid, ["serverstatus", "0", "50",
                                       "subscribe:60"], response, msg_id=5)
        await _slim_request(mgr, cid, ["serverstatus", "0", "50",
                                       "subscribe:30"], response, msg_id=6)
        client = mgr.get(cid)
        return dict(client.autoexecute)

    entries = _run(run())
    assert len(entries) == 1, entries
    entry = next(iter(entries.values()))
    assert entry["interval"] == 30.0 and entry["id"] == 6, entry


def test_disconnected_client_leaves_no_autoexecute_bookkeeping():
    """A removed client takes its repeating requests with it."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        await _slim_request(mgr, cid, ["serverstatus", "0", "50",
                                       "subscribe:1"],
                            f"/{cid}/slim/serverstatus", msg_id=5)
        await mgr.handle_messages([{"channel": "/meta/disconnect", "id": 6,
                                    "clientId": cid}])
        await _run_loop_for(mgr, 2.2)
        return mgr.get(cid)

    assert _run(run()) is None
