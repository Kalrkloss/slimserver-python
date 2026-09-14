"""The ``id`` of a PUSHED cometd event is the ``/slim/subscribe`` id.

Perl keeps the id of the subscribing message on the request and stamps every
later push for that subscription with it:

* ``Slim/Web/Cometd.pm:805`` — a plain subscription (no ``subscribe:N``)
  stores ``$request->source("$response|$id|$priority|$clid|$ua")`` in its
  callback, ``:852`` does the same for a request+subscribe, which then arms
  ``autoExecuteCallback(\\&requestCallback)`` (:857).
* ``:942`` — ``requestCallback`` splits the source back into
  ``($channel, $id, $priority, $clid)`` and ``:963-970`` builds the event
  ``{channel => …, id => $id, data => …, ext => {priority => …}}``.
* ``:769`` — a message without an id defaults to ``0`` (``$params->{id} || 0``).

Live Perl 9.1.1 (192.168.1.90, read-only 2026-09-14, no writes on the server):
a handshake, then ``/slim/subscribe`` with ``id: 4242`` for
``["", ["serverstatus", 0, 50, "subscribe:3"]]`` on
``/<cid>/slim/serverstatus``, then a long-poll ``/meta/connect`` delivered a
push with ``id: 4242`` and exactly the keys ``channel``/``id``/``data``/``ext``.

This port pushed ``id: 0`` for every event before the fix.
"""

import asyncio
import json

from lyrion.web.cometd import CometdManager

CID_SUBSCRIBE_ID = 4242


class _StubRPC:
    """Minimal slim.request handler — echoes a playerstatus-shaped result."""

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


async def _subscribe(mgr: CometdManager, cid: str, msg_id, request: list,
                     channel: str, channel_name: str = "/slim/subscribe") -> None:
    """Register one subscription in the given client, id ``msg_id``."""
    await mgr.handle_messages([{
        "channel": channel_name, "id": msg_id, "clientId": cid,
        "data": {"response": channel, "request": request},
        "subscription": channel,
    }])


def test_pushed_event_carries_the_subscribe_message_id():
    """``Cometd.pm:942-970`` — a push is stamped with the subscribe id."""

    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake",
                                         "id": 1}])
        cid = hs[0]["clientId"]
        channel = f"/{cid}/slim/serverstatus"
        await _subscribe(mgr, cid, CID_SUBSCRIBE_ID,
                         ["", ["serverstatus", 0, 50, "subscribe:60"]],
                         channel)
        await mgr.wait_for_events(cid, timeout=0)        # drain the seed event

        await mgr.notify_server_status()
        return cid, channel, await mgr.wait_for_events(cid, timeout=0)

    cid, channel, events = _run(run())
    assert len(events) == 1, events
    assert events[0]["channel"] == channel
    assert events[0]["id"] == CID_SUBSCRIBE_ID, events[0]
    # the event key set is Perl's requestCallback record
    assert set(events[0]) == {"channel", "id", "data", "ext"}


def test_each_subscription_keeps_its_own_id():
    """Two subscriptions of one client carry their own subscribe ids."""

    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake",
                                         "id": 1}])
        cid = hs[0]["clientId"]
        ch_status = f"/{cid}/slim/serverstatus"
        ch_player = f"/{cid}/slim/playerstatus/00:04:20:2b:88:c8"
        await _subscribe(mgr, cid, 7,
                         ["", ["serverstatus", 0, 50, "subscribe:60"]],
                         ch_status)
        await _subscribe(mgr, cid, 9,
                         ["00:04:20:2b:88:c8",
                          ["status", "-", 1, "subscribe:60"]], ch_player)
        await mgr.wait_for_events(cid, timeout=0)

        await mgr.notify_server_status()
        await mgr.notify_player_status("00:04:20:2b:88:c8")
        return await mgr.wait_for_events(cid, timeout=0)

    events = {e["channel"]: e["id"] for e in _run(run())}
    status_id = next(v for k, v in events.items() if "serverstatus" in k)
    player_id = next(v for k, v in events.items() if "playerstatus" in k)
    assert (status_id, player_id) == (7, 9), events


def test_push_id_falls_back_to_the_channel_registrations_message():
    """A channel-only registration keeps the id of the message that made it.

    Perl's ``/meta/subscribe`` is a pure ``add_channels`` registration
    (``Cometd.pm:357``) and never produces a push at all; the pushes this port
    sends for such a channel (its jive-default fallback) therefore have no
    Perl id to copy and use the registering message's id, which keeps the
    value a client can correlate — never ``null``.
    """

    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake",
                                         "id": 1}])
        cid = hs[0]["clientId"]
        await _subscribe(mgr, cid, 2, ["", ["serverstatus", 0, 50]],
                         f"/{cid}/slim/serverstatus",
                         channel_name="/meta/subscribe")
        await mgr.wait_for_events(cid, timeout=0)        # drain the seed event
        await mgr.notify_server_status()
        return await mgr.wait_for_events(cid, timeout=0)

    events = _run(run())
    assert len(events) == 1, events
    assert events[0]["id"] == 2


def test_push_without_any_id_is_zero_like_perl():
    """``Cometd.pm:769`` — a message without an id is ``0``, never null."""

    async def run():
        mgr = CometdManager(_StubRPC())
        hs = await mgr.handle_messages([{"channel": "/meta/handshake",
                                         "id": 1}])
        cid = hs[0]["clientId"]
        await _subscribe(mgr, cid, None,
                         ["", ["serverstatus", 0, 50, "subscribe:60"]],
                         f"/{cid}/slim/serverstatus")
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_server_status()
        return await mgr.wait_for_events(cid, timeout=0)

    events = _run(run())
    assert len(events) == 1, events
    assert events[0]["id"] == 0
