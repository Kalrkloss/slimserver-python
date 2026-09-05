"""Tests for Cometd subscription semantics.

Regression: /meta/unsubscribe only read ``data.unsubscribe``, so a client
sending the top-level ``subscription`` field got a false "successful" while
the subscription stayed; and status change notifications discarded the
stored request's pagination/menu/tags.
"""

import asyncio

from lyrion.web.api import JSONRPCAPI
from lyrion.web.cometd import CometdManager


def _handshake(mgr):
    replies = asyncio.run(mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}]))
    return replies[0]["clientId"]


def test_unsubscribe_removes_top_level_subscription():
    async def run():
        mgr = CometdManager(JSONRPCAPI())
        replies = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = replies[0]["clientId"]
        await mgr.handle_messages([
            {"channel": "/meta/subscribe", "clientId": cid, "id": 2,
             "subscription": "/slim/playerstatus", "data": {}},
        ])
        assert "/slim/playerstatus" in mgr.get(cid).subscriptions

        await mgr.handle_messages([
            {"channel": "/meta/unsubscribe", "clientId": cid, "id": 3,
             "subscription": "/slim/playerstatus"},  # top-level field
        ])
        assert "/slim/playerstatus" not in mgr.get(cid).subscriptions

    asyncio.run(run())


def test_notify_player_status_uses_stored_request():
    async def run():
        mgr = CometdManager(JSONRPCAPI())
        replies = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
        cid = replies[0]["clientId"]
        stored = ["02:11:22:33:44:55", ["playerstatus", "-", "1", "tags:al"]]
        await mgr.handle_messages([
            {"channel": "/meta/subscribe", "clientId": cid, "id": 2,
             "subscription": "/slim/playerstatus/02:11:22:33:44:55",
             "data": {"request": stored}},
        ])

        captured = []

        async def fake_dispatch(req):
            captured.append(req)
            return {"mode": "stop"}

        mgr._dispatch = fake_dispatch
        await mgr.notify_player_status("02:11:22:33:44:55")
        return captured

    captured = asyncio.run(run())
    assert captured, "notify_player_status must dispatch"
    assert captured[0][1] == ["playerstatus", "-", "1", "tags:al"]
