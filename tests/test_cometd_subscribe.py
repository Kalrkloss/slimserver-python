"""Request-driven cometd subscriptions (``/slim/subscribe`` + data.request).

LIVE-01 regression family: SqueezePlay's Now-Playing subscribes WITH a request
(``~/opt/squeezeplay/share/jive/jive/slim/Player.lua:993``):

    {'status','-',10,'menu:menu','useContextMenu:1','subscribe:600'}

``share/jive/jive/net/Comet.lua:286-296`` turns that into a ``/slim/subscribe``
message carrying ``data.request`` (``[<player>, [cmd,...]]``) plus
``data.response`` (the channel to push on).  Perl
(``Slim/Web/Cometd.pm:390-470``) executes the request AND registers it:
on every change the same request is re-executed and the result delivered to
all subscribers of that channel.

The Python server used to ignore the request and fall back to a bare
``[player, ["playerstatus","-","1"]]``, so pushes carried
``mode``/``power``/``playlist_tracks`` but no ``current_title``/``item_loop``
— the displayed title never refreshed.

These tests drive CometdManager in-process with a recording dispatcher
(no server, no database), so they assert exactly WHICH request each push is
built from and on which channel it is delivered.
"""

import asyncio

from lyrion.web.cometd import CometdManager, _default_request

PLAYER = "1c:87:2c:47:fc:36"

# Exactly what Player.lua:993 passes to comet:subscribe().
JIVE_STATUS_CMD = ["status", "-", 10, "menu:menu", "useContextMenu:1",
                   "subscribe:600"]
# The fallback form (same request without the keep-alive token).
JIVE_DEFAULT_CMD = ["status", "-", 10, "menu:menu", "useContextMenu:1"]


class _Recorder:
    """Fake dispatcher: records requests, answers like the real one.

    ``status`` yields a Now-Playing payload (current_title/item_loop), the
    Android ``playerstatus`` form yields the bare status frame — the exact
    difference that made the title stay stale.
    """

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def dispatch(self, request: list) -> dict:
        self.calls.append(request)
        command = ""
        if len(request) > 1 and isinstance(request[1], list) and request[1]:
            command = request[1][0]
        if command == "status":
            return {
                "mode": "play", "power": 1, "playlist_tracks": 1,
                "current_title": "Titel 51994",
                "item_loop": [{"title": "Titel 51994"}],
                "playlist_loop": [{"title": "Titel 51994"}],
            }
        return {"mode": "play", "power": 1, "playlist_tracks": 1}


def _run(coro):
    return asyncio.run(coro)


def _manager():
    rec = _Recorder()
    mgr = CometdManager(rec)
    mgr._dispatch = rec.dispatch
    return mgr, rec


async def _handshake(mgr) -> str:
    replies = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
    return replies[0]["clientId"]


def test_slim_subscribe_with_request_is_stored_and_answered_immediately():
    """The request is persisted with the subscription and run right away."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/playerstatus/{PLAYER}"
        ack = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": response},
        }]))[0]
        client = mgr.get(cid)
        assert client is not None
        stored = client.subscriptions.get(response)
        events = await mgr.wait_for_events(cid, timeout=0)
        return ack, stored, rec.calls, events

    ack, stored, calls, events = _run(run())
    assert ack["successful"] is True
    # subscription persisted WITH its request (Perl request + subscribe)
    assert stored is not None
    assert stored["request"] == [PLAYER, JIVE_STATUS_CMD]
    # the request was executed once, immediately
    assert calls == [[PLAYER, JIVE_STATUS_CMD]]
    # ... and the result was pushed on the response channel
    assert len(events) == 1
    assert events[0]["channel"] == stored["response"]
    assert events[0]["data"]["current_title"] == "Titel 51994"


def test_slim_subscribe_ack_echoes_response_channel():
    async def run():
        mgr, _rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/playerstatus/{PLAYER}"
        return (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": response},
        }]))[0], response

    ack, response = _run(run())
    assert ack["channel"] == "/slim/subscribe"
    assert ack["successful"] is True
    # libcometd (SqueezeClient) requires the subscription field in the ack
    assert ack["subscription"] == response


def test_notify_player_status_reexecutes_client_request():
    """A STAT change re-runs the stored request -> payload carries the title."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": response},
        }])
        await mgr.wait_for_events(cid, timeout=0)  # drain the seed push
        rec.calls.clear()

        await mgr.notify_player_status(PLAYER)
        events = await mgr.wait_for_events(cid, timeout=0)
        return response, rec.calls, events

    response, calls, events = _run(run())
    # the CLIENT's request is re-executed (not playerstatus - 1)
    assert calls == [[PLAYER, JIVE_STATUS_CMD]]
    assert len(events) == 1, f"expected exactly one push, got {events}"
    assert events[0]["channel"] == response
    data = events[0]["data"]
    assert data.get("current_title") == "Titel 51994"
    assert data.get("item_loop") or data.get("playlist_loop")


def test_notify_matches_subscription_by_request_not_channel_name():
    """Perl registers the auto-execute on the COMMAND, not the channel name."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        # a response channel that does not mention "playerstatus"
        response = f"/{cid}/slim/mystatus"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": response},
        }])
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(PLAYER)
        return response, rec.calls, await mgr.wait_for_events(cid, timeout=0)

    response, calls, events = _run(run())
    assert calls == [[PLAYER, JIVE_STATUS_CMD]]
    assert len(events) == 1
    assert events[0]["channel"] == response


def test_notify_player_status_default_jive_request_without_client_request():
    """The pure ``/<cid>/**`` catch-all gets the jive status request."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }])
        # a pure pattern registration dispatches nothing
        assert await mgr.wait_for_events(cid, timeout=0) == []
        assert rec.calls == []
        await mgr.notify_player_status(PLAYER)
        return cid, rec.calls, await mgr.wait_for_events(cid, timeout=0)

    cid, calls, events = _run(run())
    assert calls == [[PLAYER, JIVE_DEFAULT_CMD]], calls
    assert len(events) == 1
    assert events[0]["channel"] == f"/{cid}/slim/playerstatus/{PLAYER}"
    assert events[0]["data"]["current_title"] == "Titel 51994"


def test_default_request_helper_forms():
    """Fallbacks are the jive forms; a pattern never gets a query."""
    cid = "14ff96e66d084eb6"
    assert _default_request(f"/{cid}/**") == []
    assert _default_request("/**") == []
    assert _default_request(f"/{cid}/slim/playerstatus/{PLAYER}") == [
        PLAYER, JIVE_DEFAULT_CMD]
    assert _default_request(f"/{cid}/slim/serverstatus") == [
        "", ["serverstatus", "0", "50", "subscribe:60"]]


def test_channel_only_playerstatus_subscription_seeds_jive_status():
    """A /meta/subscribe channel without a request seeds the jive request."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": channel,
        }])
        return channel, rec.calls, await mgr.wait_for_events(cid, timeout=0)

    channel, calls, events = _run(run())
    assert calls == [[PLAYER, JIVE_DEFAULT_CMD]]
    assert len(events) == 1
    assert events[0]["channel"] == channel


def test_glob_subscription_receives_exactly_one_playerstatus_push():
    """glob + targeted subscription -> one push, on the concrete channel."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }])
        response = f"/{cid}/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 3,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": response},
        }])
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(PLAYER)
        return cid, await mgr.wait_for_events(cid, timeout=0)

    cid, events = _run(run())
    assert len(events) == 1, f"expected exactly one push, got {events}"
    assert events[0]["channel"] == f"/{cid}/slim/playerstatus/{PLAYER}"
    assert events[0]["data"]["current_title"] == "Titel 51994"


def test_other_player_subscription_is_not_notified():
    """A status subscription for another player is left alone."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = "/" + cid + "/slim/playerstatus/aa:bb:cc:dd:ee:ff"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": ["aa:bb:cc:dd:ee:ff", JIVE_STATUS_CMD],
                     "response": response},
        }])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(PLAYER)
        return await mgr.wait_for_events(cid, timeout=0)

    assert _run(run()) == []


def test_non_status_subscription_is_not_pushed_on_stat_change():
    """e.g. a displaystatus subscription must not be re-run by a STAT event."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, ["displaystatus", "subscribe:showbriefly"]],
                     "response": f"/{cid}/slim/displaystatus/{PLAYER}"},
        }])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(PLAYER)
        return await mgr.wait_for_events(cid, timeout=0)

    assert _run(run()) == []


def test_serverstatus_subscription_is_request_driven_and_defaults_to_jive():
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        # 1) request + subscribe (SqueezeCtrl / jive SlimServer.lua:540)
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": ["", ["serverstatus", "0", "50", "subscribe:60"]],
                     "response": f"/{cid}/slim/serverstatus"},
        }])
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_server_status()
        request_driven = rec.calls[:]
        events = await mgr.wait_for_events(cid, timeout=0)

        # 2) channel-only registration -> jive default request
        mgr2, rec2 = _manager()
        cid2 = await _handshake(mgr2)
        await mgr2.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid2, "id": 2,
            "subscription": f"/{cid2}/slim/serverstatus",
        }])
        await mgr2.wait_for_events(cid2, timeout=0)
        rec2.calls.clear()
        await mgr2.notify_server_status()
        default_calls = rec2.calls[:]
        return request_driven, events, default_calls

    request_driven, events, default_calls = _run(run())
    assert request_driven == [["", ["serverstatus", "0", "50", "subscribe:60"]]]
    assert len(events) == 1
    assert default_calls == [["", ["serverstatus", "0", "50", "subscribe:60"]]]
