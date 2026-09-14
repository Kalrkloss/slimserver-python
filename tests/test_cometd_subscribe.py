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
import time

from lyrion.web.cometd import (
    CometdManager,
    _default_request,
    keepalive_due,
)

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


# ---------------------------------------------------------------------------
# R0.5 P3 fixes — delivery per matched channel, no phantom client from a
# cid-less response path, and Perl's error acks for malformed subscriptions.
# ---------------------------------------------------------------------------


def test_two_exact_status_subs_of_same_player_both_delivered():
    """Perl delivers one event per matched channel (Manager::deliver_events),
    so two distinct EXACT status subscriptions of the same player — two
    response channels, two stored requests — must yield TWO events. Only a
    glob that resolves to an already-matched channel is collapsed
    (review P3-1: 708 collapsed them to one)."""
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        first = f"/{cid}/slim/playerstatus/{PLAYER}"
        second = f"/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "clientId": cid, "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": first},
        }])
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "clientId": cid, "id": 3,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": second},
        }])
        await mgr.wait_for_events(cid, timeout=0)  # drain both seed pushes
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(PLAYER)
        return first, second, rec.calls, await mgr.wait_for_events(cid, timeout=0)

    first, second, calls, events = _run(run())
    assert sorted(e["channel"] for e in events) == sorted([first, second]), events
    assert len(calls) == 2, calls


def test_two_exact_status_subs_same_channel_delivered_once():
    """Two subscriptions that resolve to the SAME concrete channel are one
    delivery (dedupe per channel), not two."""
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        for msgid in (2, 3):
            await mgr.handle_messages([{
                "channel": "/slim/subscribe", "clientId": cid, "id": msgid,
                "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                         "response": channel},
            }])
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(PLAYER)
        return channel, await mgr.wait_for_events(cid, timeout=0)

    channel, events = _run(run())
    assert len(events) == 1, events
    assert events[0]["channel"] == channel


def test_cidless_response_channel_creates_no_phantom_client():
    """A /slim/subscribe whose response channel has no clientId segment
    (``/slim/serverstatus``) must not mint a phantom client named after the
    namespace root (review P3-2: a client ``slim`` swallowed the events)."""
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        ack = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD],
                     "response": "/slim/serverstatus"},
        }]))[0]
        return cid, ack, sorted(mgr._clients), rec.calls

    cid, ack, clients, calls = _run(run())
    assert clients == [cid], clients
    assert ack["successful"] is False
    assert ack["error"]
    assert calls == []


def test_slim_subscribe_without_response_is_an_error_ack():
    """Perl answers a /slim/subscribe without data.response with
    successful:false + 'response data key not found' (Cometd.pm:486-492);
    the old code acked success while registering nothing (review P3-3)."""
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        ack = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "clientId": cid, "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD]},
        }]))[0]
        return ack, mgr.get(cid).subscriptions, rec.calls

    ack, subs, calls = _run(run())
    assert ack["successful"] is False
    assert ack["error"] == "response data key not found"
    assert subs == {}
    assert calls == []


def test_slim_subscribe_without_request_is_an_error_ack():
    """Perl: /slim/subscribe without data.request -> 'request data key not
    found' (Cometd.pm:479-485), successful:false."""
    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        ack = (await mgr.handle_messages([{
            "channel": "/slim/subscribe", "clientId": cid, "id": 2,
            "data": {"response": f"/{cid}/slim/playerstatus/{PLAYER}"},
        }]))[0]
        return ack, mgr.get(cid).subscriptions, rec.calls

    ack, subs, calls = _run(run())
    assert ack["successful"] is False
    assert ack["error"] == "request data key not found"
    assert subs == {}
    assert calls == []


# ---------------------------------------------------------------------------
# R0.6 P0 — publish on the channel spelling the CLIENT registered.
#
# SqueezePlay subscribes with the lower-case colon form of the MAC
# (``/14ff96e66d084eb6/slim/playerstatus/1c:87:2c:47:fc:36``, live log
# 19:17:50) while the STAT handler calls ``notify_player_status(player.mac)``
# with the upper-case ``1C:87:2C:47:FC:36``. Two defects followed:
#
#   * the subscription was skipped entirely (case-sensitive player compare),
#   * a glob pushed on a REBUILT ``/…/playerstatus/<UPPER>`` channel, which
#     jive discards as "not subscribed" (Comet.lua compares the channel name
#     verbatim) — Now-Playing froze and the playlist read "Nichts".
#
# Rules pinned here: the event goes out on the client's own channel name,
# player ids compare case-insensitively (colon/format-insensitive), and a
# glob concretises to the channel the client registered for that player.
# ---------------------------------------------------------------------------

UPPER_PLAYER = PLAYER.upper()  # 1C:87:2C:47:FC:36 — what PlayerState.mac holds


def test_lowercase_subscription_gets_uppercase_player_event_on_its_own_channel():
    """(a) request-driven lower-case sub + notify(UPPER) -> lower-case channel."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/playerstatus/{PLAYER}"  # lower-case, as jive sent it
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": response},
        }])
        await mgr.wait_for_events(cid, timeout=0)  # drain seed push
        rec.calls.clear()
        await mgr.notify_player_status(UPPER_PLAYER)
        return response, rec.calls, await mgr.wait_for_events(cid, timeout=0)

    response, calls, events = _run(run())
    assert calls == [[PLAYER, JIVE_STATUS_CMD]], calls
    assert len(events) == 1, f"expected exactly one push, got {events}"
    assert events[0]["channel"] == response, events
    assert events[0]["data"]["current_title"] == "Titel 51994"


def test_channel_only_lowercase_subscription_matches_uppercase_player_id():
    """(a') channel-only sub: the player comes from the subscription path."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": channel,
        }])
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(UPPER_PLAYER)
        return channel, await mgr.wait_for_events(cid, timeout=0)

    channel, events = _run(run())
    assert len(events) == 1, f"expected exactly one push, got {events}"
    assert events[0]["channel"] == channel, events


def test_uppercase_subscription_gets_lowercase_player_event_on_its_own_channel():
    """(b) request-driven upper-case sub + notify(lower) -> upper-case channel."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/playerstatus/{UPPER_PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [UPPER_PLAYER, JIVE_STATUS_CMD], "response": response},
        }])
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(PLAYER)  # lower-case STAT mac
        return response, await mgr.wait_for_events(cid, timeout=0)

    response, events = _run(run())
    assert len(events) == 1, f"expected exactly one push, got {events}"
    assert events[0]["channel"] == response, events


def test_glob_plus_targeted_sub_pushes_once_on_targeted_channel_spelling():
    """(c) glob + targeted sub -> ONE event, on the targeted channel name."""

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
        await mgr.notify_player_status(UPPER_PLAYER)
        return response, rec.calls, await mgr.wait_for_events(cid, timeout=0)

    response, calls, events = _run(run())
    assert len(events) == 1, f"exactly one event per client, got {events}"
    assert events[0]["channel"] == response, events
    # the targeted subscription's stored request is the one re-executed
    assert calls == [[PLAYER, JIVE_STATUS_CMD]], calls


def test_glob_registered_after_targeted_sub_still_pushes_once_on_target():
    """(c') same as (c) with the registration order reversed."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": response},
        }])
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 3,
            "subscription": f"/{cid}/**",
        }])
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(UPPER_PLAYER)
        return response, rec.calls, await mgr.wait_for_events(cid, timeout=0)

    response, calls, events = _run(run())
    assert len(events) == 1, f"exactly one event per client, got {events}"
    assert events[0]["channel"] == response, events
    assert calls == [[PLAYER, JIVE_STATUS_CMD]], calls


def test_glob_only_client_uses_registered_spelling_for_concrete_channel():
    """Glob-only client: no exact channel of its own -> the standard
    playerstatus path, still built from the spelling the client used in its
    player-bearing subscriptions (here only a menustatus channel)."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }])
        # a player-bearing channel in the client's lower-case spelling
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 3,
            "subscription": f"/{cid}/slim/menustatus/{PLAYER}",
        }])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(UPPER_PLAYER)
        return await mgr.wait_for_events(cid, timeout=0)

    events = _run(run())
    assert len(events) == 1, f"exactly one event, got {events}"
    assert events[0]["channel"].endswith(f"/slim/playerstatus/{PLAYER}"), events
    assert UPPER_PLAYER not in events[0]["channel"], events


def test_serverstatus_push_is_unaffected_by_player_normalisation():
    """(d) serverstatus keeps its own channel spelling and stays request-driven."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        server_channel = f"/{cid}/slim/serverstatus"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": ["", ["serverstatus", "0", "50", "subscribe:60"]],
                     "response": server_channel},
        }])
        # an upper-case playerstatus sub of the SAME client must not leak in
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 3,
            "data": {"request": [UPPER_PLAYER, JIVE_STATUS_CMD],
                     "response": f"/{cid}/slim/playerstatus/{UPPER_PLAYER}"},
        }])
        await mgr.wait_for_events(cid, timeout=0)
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_server_status()
        server_events = await mgr.wait_for_events(cid, timeout=0)
        await mgr.notify_player_status(PLAYER)
        player_events = await mgr.wait_for_events(cid, timeout=0)
        return server_channel, server_events, player_events, cid

    server_channel, server_events, player_events, cid = _run(run())
    assert len(server_events) == 1, server_events
    assert server_events[0]["channel"] == server_channel
    # the playerstatus sub is matched case-insensitively and pushed on ITS
    # own (upper-case) channel
    assert len(player_events) == 1, player_events
    assert player_events[0]["channel"] == f"/{cid}/slim/playerstatus/{UPPER_PLAYER}"


def test_two_players_only_the_matching_client_is_notified():
    """(e) two clients, two players: only the changed player's client gets it."""
    other = "aa:bb:cc:dd:ee:ff"

    async def run():
        mgr, rec = _manager()
        cid_a = await _handshake(mgr)
        cid_b = await _handshake(mgr)
        a_channel = f"/{cid_a}/slim/playerstatus/{PLAYER}"
        b_channel = f"/{cid_b}/slim/playerstatus/{other}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": a_channel},
        }])
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "clientId": cid_b, "id": 3,
            "data": {"request": [other, JIVE_STATUS_CMD], "response": b_channel},
        }])
        await mgr.wait_for_events(cid_a, timeout=0)
        await mgr.wait_for_events(cid_b, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(UPPER_PLAYER)
        a_events = await mgr.wait_for_events(cid_a, timeout=0)
        b_events = await mgr.wait_for_events(cid_b, timeout=0)
        return a_channel, b_channel, a_events, b_events

    a_channel, b_channel, a_events, b_events = _run(run())
    assert [e["channel"] for e in a_events] == [a_channel], a_events
    assert b_events == [], b_events


def test_live_squeezeplay_subscription_set_updates_now_playing():
    """Live repro (log 19:17:50, cid 14ff96e66d084eb6): the full SqueezePlay
    registration set + a STAT change with the upper-case PlayerState.mac must
    produce exactly ONE event, on the lower-case channel jive holds, carrying
    the client's stored request (current_title/item_loop → Now-Playing and the
    playlist refresh instead of "Nichts")."""

    async def run():
        mgr, rec = _manager()
        hs = await mgr.handle_messages([{
            "channel": "/meta/handshake", "id": 1,
            "ext": {"uuid": "4ff96e66d084eb6"},
        }])
        cid = hs[0]["clientId"]

        def sub(msgid, request, response):
            return {"channel": "/slim/subscribe", "id": msgid,
                    "data": {"request": request, "response": response}}

        await mgr.handle_messages([{  # Comet.lua:702 catch-all
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }])
        for msgid, request, response in (
            (3, ["", ["serverstatus", 0, 50, "subscribe:60"]],
             f"/{cid}/slim/serverstatus"),
            (4, [PLAYER, ["menustatus"]], f"/{cid}/slim/menustatus/{PLAYER}"),
            (5, [PLAYER, JIVE_STATUS_CMD], f"/{cid}/slim/playerstatus/{PLAYER}"),
            (6, [PLAYER, ["displaystatus", "subscribe:showbriefly"]],
             f"/{cid}/slim/displaystatus/{PLAYER}"),
        ):
            await mgr.handle_messages([sub(msgid, request, response)])
        for _ in range(6):
            await mgr.wait_for_events(cid, timeout=0)  # drain the seed pushes
        rec.calls.clear()

        # the STAT handler passes PlayerState.mac — UPPER case
        await mgr.notify_player_status(UPPER_PLAYER)
        return cid, rec.calls, await mgr.wait_for_events(cid, timeout=0)

    cid, calls, events = _run(run())
    assert cid == "14ff96e66d084eb6"  # the live client id
    assert [e["channel"] for e in events] == [f"/{cid}/slim/playerstatus/{PLAYER}"], \
        events
    assert calls == [[PLAYER, JIVE_STATUS_CMD]], calls
    assert events[0]["data"]["current_title"] == "Titel 51994"
    assert events[0]["data"]["playlist_tracks"] == 1


def test_player_key_normalises_case_and_colons():
    """MAC comparison helper: case-insensitive, colon-insensitive, '' never
    matches (so a missing player cannot equal a real one)."""
    from lyrion.web.cometd import _player_key, _same_player

    assert _player_key("1C:87:2C:47:FC:36") == _player_key("1c872c47fc36")
    assert _same_player("1C:87:2C:47:FC:36", "1c:87:2c:47:fc:36")
    assert _same_player("1c872c47fc36", "1C:87:2C:47:FC:36")
    assert not _same_player("", "1c:87:2c:47:fc:36")
    assert not _same_player("", "")
    assert not _same_player("aa:bb:cc:dd:ee:ff", "1c:87:2c:47:fc:36")


def test_glob_only_client_uses_the_client_visible_channel_form():
    """A glob-only client has no channel string of its own.

    Perl never rebuilds a channel: the event goes out on the string the client
    registered (``Cometd.pm:852`` ``$request->source("$response|…")``,
    ``:942-944`` ``requestCallback``, ``Manager.pm:230-243``), and a pure
    ``/<cid>/**`` registration produces no event at all (``Cometd.pm:782-819``
    — a request-less subscription is only a channel registration).  The
    Python-side concretisation for such a client must therefore not stamp the
    SERVER-internal ``PlayerState.mac`` (upper case: Perl builds its own id
    from the HELO frame with ``unpack("…H2H2H2H2H2H2…")`` +
    ``join(':')``, ``Slimproto.pm:964``/``:990``) — every controller names its
    channels with the lower-case form (live log 2026-09-14 16:05:08:
    ``/14ff96e66d084eb6/slim/playerstatus/1c:87:2c:47:fc:36``).
    """

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": f"/{cid}/**",
        }])
        rec.calls.clear()
        await mgr.notify_player_status(UPPER_PLAYER)
        return await mgr.wait_for_events(cid, timeout=0)

    events = _run(run())
    assert len(events) == 1, events
    assert events[0]["channel"].endswith(f"/slim/playerstatus/{PLAYER}"), events
    assert "1C:87:2C:47:FC:36" not in events[0]["channel"], events
    assert events[0]["channel"].count(":") == 5, events


def test_a_seeded_status_keeps_the_upper_case_id_of_a_client_that_used_it():
    """The client's OWN spelling always wins — never normalised away."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        channel = f"/{cid}/slim/playerstatus/{UPPER_PLAYER}"
        await mgr.handle_messages([{
            "channel": "/meta/subscribe", "clientId": cid, "id": 2,
            "subscription": channel,
        }])
        await mgr.wait_for_events(cid, timeout=0)
        rec.calls.clear()
        await mgr.notify_player_status(PLAYER)
        return channel, await mgr.wait_for_events(cid, timeout=0)

    channel, events = _run(run())
    assert [e["channel"] for e in events] == [channel], events


def test_pushed_events_carry_perls_frame_shape():
    """Perl's subscription event is ``{channel, id, data, ext:{priority}}``.

    ``requestCallback`` (``Cometd.pm:962-970``) builds exactly those four keys
    — ``data`` = the re-executed request's result (:955), ``id`` = the request
    id (:769 ``$params->{id} || 0``) and ``ext.priority`` = the client's
    ``data.priority`` or ``''`` (:772).  Our pushes were missing ``ext``.
    """

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        response = f"/{cid}/slim/playerstatus/{PLAYER}"
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, JIVE_STATUS_CMD], "response": response},
        }])
        events = await mgr.wait_for_events(cid, timeout=0)
        return events

    events = _run(run())
    assert len(events) == 1, events
    assert set(events[0]) == {"channel", "id", "data", "ext"}, events[0]
    assert events[0]["ext"] == {"priority": ""}, events[0]
    assert isinstance(events[0]["id"], int), events[0]


# ---------------------------------------------------------------------------
# subscribe:N keep-alive timing (Perl Request.pm:2176-2181)
# ---------------------------------------------------------------------------


def test_keepalive_is_armed_not_fired_immediately():
    r"""Perl arms ``setTimer(now + $timeout, \&__autoexecute)`` at registration.

    The first re-execution is N seconds away.  Firing at once (``last``
    defaulting to 0 made every interval look overdue) pushed one extra status
    per subscription right after the client subscribed — Perl's subscribe
    reply already carried that result (Cometd.pm:454-475).
    """
    assert keepalive_due(None, 600, 1_000_000.0) is False   # just armed
    assert keepalive_due(1_000_000.0, 600, 1_000_599.0) is False
    assert keepalive_due(1_000_000.0, 600, 1_000_600.0) is True
    assert keepalive_due(1_000_000.0, 1, 1_000_002.0) is True


def test_keepalive_loop_waits_one_interval_before_the_first_push():
    """End to end: ``subscribe:1`` -> no push inside the first second."""

    async def run():
        mgr, rec = _manager()
        cid = await _handshake(mgr)
        await mgr.handle_messages([{
            "channel": "/slim/subscribe", "id": 2,
            "data": {"request": [PLAYER, ["status", "-", "1", "subscribe:1"]],
                     "response": f"/{cid}/slim/playerstatus/{PLAYER}"},
        }])
        await mgr.wait_for_events(cid, timeout=0)  # the seed of the subscribe
        task = asyncio.create_task(mgr.keepalive_loop())
        started = time.monotonic()
        try:
            # The loop ticks once per second; "just armed" is not "overdue",
            # so the first push may only come one interval AFTER the arm.
            events = await mgr.wait_for_events(cid, timeout=5.0)
            delay = time.monotonic() - started
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass
        return events, delay

    events, delay = _run(run())
    assert len(events) == 1, events
    assert events[0]["channel"].endswith(f"/slim/playerstatus/{PLAYER}")
    # Firing on the loop's first pass (delay ~1.0 s: the loop's own tick) was
    # the bug; Perl's timer needs the arm PLUS the interval (>= ~2.0 s).
    assert delay >= 1.5, f"first auto-execute after {delay:.2f}s — Perl arms it"
