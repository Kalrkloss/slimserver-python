"""Cometd endpoint for Jive-based controllers (SqueezeControl, iPeng,
Orange Squeeze, jivelite).

The Jive Comet client (share/jive/jive/net/Comet.lua) speaks a
Bayeux-flavoured protocol over POST /cometd:

  handshake : /meta/handshake          -> server assigns a clientId
  subscribe : /slim/subscribe          -> { data: { request: [player,
             {channel,subscription}], subscription: '/slim/...' } }
  request   : /slim/request            -> { data: { request: [player,
             [cmd,...]], response: '/<clientId>/slim/request' } }
  connect   : /meta/connect            -> long-poll: held open, server
             pushes queued events, replies after timeout otherwise

Events are pushed on the subscribed channel (e.g. /slim/serverstatus)
or the per-client response channel. The request payloads are exactly
slim.request params (player + command array) and are dispatched through
the JSON-RPC handler.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

LONG_POLL_TIMEOUT = 25  # seconds a /meta/connect request is held open


@dataclass
class CometdClient:
    """One connected Jive controller."""
    client_id: str
    subscriptions: dict[str, dict] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    notify: asyncio.Event = field(default_factory=asyncio.Event)


# Module-level manager singleton — lets the slimproto layer wake
# /slim/serverstatus subscribers when players connect/disconnect.
_manager: Optional["CometdManager"] = None


def _set_manager(mgr: "CometdManager") -> None:
    global _manager
    _manager = mgr


def get_manager() -> Optional["CometdManager"]:
    """Return the active CometdManager (created at startup), or None."""
    return _manager


class CometdManager:
    """Server-side Bayeux/Cometd endpoint for LMS-style controllers.

    Clients are Jive-family apps (Orange Squeeze, SqueezeCtrl, Squeezer,
    SqueezeClient, Jivelite, Material) that subscribe to /slim/... and
    /meta/... channels over /cometd (long-polling or streaming).
    """

    def __init__(self, jsonrpc) -> None:
        self._jsonrpc = jsonrpc
        self._clients: dict[str, CometdClient] = {}
        self._counter = itertools.count(1)
        _set_manager(self)

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def handshake(self, ext: dict | None = None) -> CometdClient:
        """Create a client. The id is derived from the client UUID when
        available (stable across reconnects — Orange Squeeze caches the
        id from its first handshake), otherwise a counter."""
        uuid = ""
        if isinstance(ext, dict):
            uuid = str(ext.get("uuid", "") or "")
        if uuid:
            client_id = "1" + uuid.replace("-", "")[:15]
        else:
            client_id = f"lyrion-{next(self._counter)}"
        client = CometdClient(client_id=client_id)
        self._clients[client_id] = client
        return client

    def get_or_create(self, client_id: str) -> CometdClient:
        """Return the client, creating it on the fly if unknown.

        Orange Squeeze caches its clientId from the first handshake and
        reuses it for subscriptions after a reconnect/restart — without
        this the server would answer 'successful: false' and the app
        could not connect.
        """
        client = self._clients.get(client_id)
        if client is None:
            client = CometdClient(client_id=client_id)
            self._clients[client_id] = client
        return client

    def get(self, client_id: str) -> CometdClient | None:
        return self._clients.get(client_id)

    def remove(self, client_id: str) -> None:
        self._clients.pop(client_id, None)

    def push(self, client_id: str, event: dict) -> None:
        client = self._clients.get(client_id)
        if client is None:
            return
        client.events.append(event)
        client.notify.set()

    async def notify_server_status(self) -> None:
        """Push a fresh serverstatus to all /slim/serverstatus subscribers.

        Called when players connect/disconnect so subscribed controllers
        (Jive/SqueezeCtrl/ioBroker) see the player list change.
        """
        for client in list(self._clients.values()):
            for sub, data in list(client.subscriptions.items()):
                if "serverstatus" not in sub:
                    continue
                try:
                    request = (data.get("request") if isinstance(data, dict)
                               else None) or ["", ["serverstatus", "0", "100"]]
                    result = await self._dispatch(request)
                    self.push(client.client_id, {
                        "channel": sub,
                        "data": result,
                        "id": 0,
                    })
                except Exception:  # noqa: BLE001
                    pass

    async def notify_player_status(self, player_id: str) -> None:
        """Push a fresh player status to status/playerstatus subscribers.

        Called by the slimproto layer on every STAT change so the
        Android controllers (SqueezeCtrl, Orange Squeeze, Squeezer) get
        the new state immediately instead of on their next poll.
        """
        for client in list(self._clients.values()):
            for sub in list(client.subscriptions.keys()):
                if "playerstatus" not in sub and "status/" not in sub:
                    continue
                parts = sub.split("/")
                sub_player = parts[-1] if len(parts) >= 2 else ""
                # Match the changed player. The /null/... and
                # 00:00:00:00:00:00 forms (SqueezeCtrl) are app-chosen and
                # player-agnostic — deliver to them for ANY player change.
                if sub_player and sub_player not in (
                        player_id, "null", "00:00:00:00:00:00", ""):
                    continue
                try:
                    result = await self._dispatch([player_id, ["playerstatus", "-", "1"]])
                    self.push(client.client_id, {
                        "channel": sub,
                        "data": result,
                        "id": 0,
                    })
                except Exception:  # noqa: BLE001
                    pass

    async def notify_favorites_changed(self) -> None:
        """Push a 'favorites changed' event to all favorites subscribers.

        SqueezeCtrl subscribes to /<cid>/slim/favorites/* with
        ['favorites', ['changed']] and reloads the list on the event.
        """
        for client in list(self._clients.values()):
            for sub in list(client.subscriptions.keys()):
                if "favorites" not in sub:
                    continue
                self.push(client.client_id, {
                    "channel": sub,
                    "data": ["favorites", ["changed"]],
                    "id": 0,
                })

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_messages(self, messages: list[dict]) -> list[dict]:
        """Handle one batch of Bayeux messages, return immediate replies.

        /meta/connect messages are NOT answered here — they long-poll
        and get their reply through wait_for_events().
        """
        replies: list[dict] = []
        for msg in messages:
            channel = msg.get("channel", "")
            cid = msg.get("clientId", "")
            reply: dict = {"channel": channel, "id": msg.get("id", "")}

            if channel == "/meta/handshake":
                client = self.handshake(msg.get("ext") if isinstance(msg.get("ext"), dict) else None)
                reply.update({
                    "successful": True,
                    "version": "1.0",
                    "clientId": client.client_id,
                    "supportedConnectionTypes": ["long-polling", "streaming"],
                    "advice": {"reconnect": "retry", "interval": 0},
                })
                logger.info("Cometd handshake -> client %s", client.client_id)

            elif channel in ("/meta/subscribe", "/slim/subscribe"):
                data = msg.get("data", {})
                # Material sends data.response, Jive/SqueezeClient send
                # data.subscription, SqueezeCtrl sends 'subscription' as
                # a TOP-LEVEL field of /meta/subscribe — accept all.
                subscription = (data.get("subscription") or data.get("response")
                                or msg.get("subscription") or "")
                # Orange Squeeze's /slim/subscribe carries NO clientId —
                # derive it from the subscription path (/<clientId>/...).
                if not cid and subscription.startswith("/"):
                    cid = subscription.split("/")[1]
                client = self.get_or_create(cid)
                if client is not None and subscription:
                    client.subscriptions[subscription] = data
                    logger.info("Cometd %s subscribed %s", cid, subscription)
                    # Push the initial result of the subscription request.
                    # SqueezeCtrl subscribes to /slim/serverstatus WITHOUT
                    # a request — deliver the player list anyway.
                    request = data.get("request") or []
                    if not request and "serverstatus" in subscription:
                        request = ["", ["serverstatus", "0", "100", "subscribe:60"]]
                    elif not request and "menustatus" in subscription:
                        # Squeezer subscribes to /<cid>/slim/menustatus/*
                        # without a request — deliver the home menu array
                        request = ["", ["menustatus"]]
                    elif not request and "favorites" in subscription:
                        # SqueezeCtrl subscribes to /<cid>/slim/favorites/* —
                        # deliver the favorites list (DB ids; the apps parse
                        # them as numbers).
                        request = ["", ["favorites", "items"]]
                    elif not request and "playerstatus" in subscription:
                        # SqueezeCtrl subscribes to /<cid>/slim/playerstatus/<player>
                        parts = subscription.split("/")
                        sub_player = parts[-1] if len(parts) >= 2 else ""
                        request = [sub_player, ["playerstatus", "-", "1"]]
                    result = await self._dispatch(request)
                    self.push(cid, {
                        "channel": subscription,
                        "data": result,
                        # SqueezeClient's Message class requires id: Int
                        # — a missing id breaks the whole array parse.
                        "id": msg.get("id", ""),
                    })
                reply.update({"successful": client is not None, "error": None})
                # libcometd (SqueezeClient) requires the subscription
                # field in the ack — otherwise 'Subscription response
                # missing'.
                if subscription:
                    reply["subscription"] = subscription

            elif channel in ("/meta/unsubscribe", "/slim/unsubscribe"):
                client = self.get(cid)
                data = msg.get("data", {})
                subscription = data.get("unsubscribe", "")
                if client is not None and subscription:
                    client.subscriptions.pop(subscription, None)
                reply.update({"successful": client is not None})

            elif channel == "/slim/request":
                data = msg.get("data", {})
                response_channel = data.get("response", "")
                # Orange Squeeze's slim/request carries NO clientId —
                # derive it from the response channel (/<clientId>/...).
                if not cid and response_channel.startswith("/"):
                    cid = response_channel.split("/")[1]
                client = self.get_or_create(cid)
                request = data.get("request") or []
                result = await self._dispatch(request)
                if client is not None:
                    self.push(cid, {
                        "channel": response_channel,
                        "data": result,
                        "id": msg.get("id", ""),
                    })
                    reply.update({"successful": True})
                else:
                    reply.update({"successful": False})

            elif channel == "/meta/disconnect":
                # Remove the client so its subscriptions/events are freed.
                self.remove(cid)
                reply.update({
                    "successful": True,
                    "advice": {"reconnect": "none", "interval": 0},
                })
                logger.info("Cometd disconnect -> client %s", cid)

            elif channel == "/meta/ping":
                reply.update({"successful": True})

            else:
                reply.update({"successful": True})

            replies.append(reply)
        return replies

    async def wait_for_events(
        self, client_id: str, timeout: float | None = LONG_POLL_TIMEOUT
    ) -> list[dict]:
        """Block until the client has queued events (or the timeout
        expires; None = wait forever). Returns the events (and clears
        the queue)."""
        client = self.get(client_id)
        if client is None:
            return []
        if not client.events:
            try:
                if timeout is None:
                    await client.notify.wait()
                else:
                    await asyncio.wait_for(client.notify.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        client.notify.clear()
        events = client.events
        client.events = []
        return events

    async def peek_events(self, client_id: str) -> list[dict]:
        """Return queued events WITHOUT clearing them. Jive clients
        expect request results in the POST reply, while SqueezeClient
        receives them via the open stream — so the native server sends
        them in the reply AND leaves them queued for the push_task."""
        client = self.get(client_id)
        if client is None:
            return []
        return list(client.events)

    async def _dispatch(self, request: list) -> dict:
        """Dispatch a slim.request payload and return the result dict.

        Accepts both [player, [cmd,...]] (Jive/SqueezeClient/Material)
        and [[cmd,...]] (no player — Material subscriptions).
        """
        try:
            if not isinstance(request, list) or not request:
                return {}
            if len(request) == 1 and isinstance(request[0], list):
                player_id, command = "", request[0]
            elif len(request) >= 2 and isinstance(request[1], list):
                player_id, command = request[0], request[1]
            else:
                return {}
            body = json.dumps({
                "id": 1,
                "method": "slim.request",
                "params": [player_id, command],
            }).encode("utf-8")
            raw = await self._jsonrpc.handle_request(body)
            try:
                parsed = json.loads(raw)
            except Exception:
                return {}
            if isinstance(parsed, dict) and "result" in parsed:
                return parsed["result"]
            if isinstance(parsed, dict) and "error" in parsed:
                return {"error": parsed["error"]}
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cometd dispatch failed: %s", exc)
            return {"error": str(exc)}
