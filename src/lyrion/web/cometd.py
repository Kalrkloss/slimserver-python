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

logger = logging.getLogger(__name__)

LONG_POLL_TIMEOUT = 25  # seconds a /meta/connect request is held open


@dataclass
class CometdClient:
    """One connected Jive controller."""
    client_id: str
    subscriptions: dict[str, dict] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    notify: asyncio.Event = field(default_factory=asyncio.Event)


class CometdManager:
    """Holds Cometd clients and dispatches /slim/request payloads."""

    def __init__(self, jsonrpc) -> None:
        self._jsonrpc = jsonrpc
        self._clients: dict[str, CometdClient] = {}
        self._counter = itertools.count(1)

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def handshake(self) -> CometdClient:
        client = CometdClient(client_id=f"lyrion-{next(self._counter)}")
        self._clients[client.client_id] = client
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
                client = self.handshake()
                reply.update({
                    "successful": True,
                    "version": "1.0",
                    "clientId": client.client_id,
                    "supportedConnectionTypes": ["long-polling", "streaming"],
                    "advice": {"reconnect": "retry", "interval": 0},
                })
                logger.info("Cometd handshake -> client %s", client.client_id)

            elif channel in ("/meta/subscribe", "/slim/subscribe"):
                client = self.get(cid)
                data = msg.get("data", {})
                # Material sends data.response, Jive/SqueezeClient send
                # data.subscription — accept both.
                subscription = data.get("subscription") or data.get("response") or ""
                if client is not None and subscription:
                    client.subscriptions[subscription] = data
                    logger.info("Cometd %s subscribed %s", cid, subscription)
                    # Push the initial result of the subscription request.
                    request = data.get("request") or []
                    result = await self._dispatch(request)
                    self.push(cid, {
                        "channel": subscription,
                        "data": result,
                    })
                reply.update({"successful": client is not None, "error": None})

            elif channel in ("/meta/unsubscribe", "/slim/unsubscribe"):
                client = self.get(cid)
                data = msg.get("data", {})
                subscription = data.get("unsubscribe", "")
                if client is not None and subscription:
                    client.subscriptions.pop(subscription, None)
                reply.update({"successful": client is not None})

            elif channel == "/slim/request":
                client = self.get(cid)
                data = msg.get("data", {})
                response_channel = data.get("response", f"/{cid}/slim/request")
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

            else:
                reply.update({"successful": True})

            replies.append(reply)
        return replies

    async def wait_for_events(
        self, client_id: str, timeout: float = LONG_POLL_TIMEOUT
    ) -> list[dict]:
        """Block until the client has queued events or the poll timeout
        expires. Returns the events (and clears the queue)."""
        client = self.get(client_id)
        if client is None:
            return []
        if not client.events:
            try:
                await asyncio.wait_for(client.notify.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        client.notify.clear()
        events = client.events
        client.events = []
        return events

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
