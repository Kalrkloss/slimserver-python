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

def test_rehandshake_keeps_the_existing_client_object():
    """A second /meta/handshake with the same clientId must NOT replace the
    client: jive re-handshakes after a server restart, and the replacement
    dropped its subscriptions and its open-transport count, so the idle
    reaper (Perl LONG_POLLING_AUTOKILL = 180 s, Cometd.pm:49) killed a live
    client whose stream was still open — Now-Playing stopped updating while
    the audio kept playing (live 2026-09-12)."""
    async def run():
        mgr = CometdManager(JSONRPCAPI())
        first = await mgr.handle_messages([
            {"channel": "/meta/handshake", "id": 1,
             "ext": {"uuid": "14ff96e6-6d08-4eb6-9a11-223344556677"}},
        ])
        cid = first[0]["clientId"]
        client = mgr.get(cid)
        await mgr.handle_messages([
            {"channel": "/meta/subscribe", "clientId": cid, "id": 2,
             "subscription": "/slim/playerstatus", "data": {}},
        ])
        mgr.connection_open(cid)                      # its stream is open

        again = await mgr.handle_messages([
            {"channel": "/meta/handshake", "id": 3,
             "ext": {"uuid": "14ff96e6-6d08-4eb6-9a11-223344556677"}},
        ])
        assert again[0]["clientId"] == cid
        assert mgr.get(cid) is client, "re-handshake replaced the client"
        assert "/slim/playerstatus" in mgr.get(cid).subscriptions
        assert mgr.get(cid).connections == 1, (
            "the open transport count was reset — the reaper would drop a "
            "streaming client"
        )
        mgr.remove(cid)

    asyncio.run(run())


def test_long_poll_timeout_is_perls_sixty_seconds():
    # Perl Slim::Web::Cometd Cometd.pm:48 LONG_POLLING_TIMEOUT => 60000
    from lyrion.web.cometd import LONG_POLL_TIMEOUT
    assert LONG_POLL_TIMEOUT == 60
