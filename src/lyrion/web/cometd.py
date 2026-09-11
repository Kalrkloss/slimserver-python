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
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

LONG_POLL_TIMEOUT = 25  # seconds a /meta/connect request is held open

# Upper bound for one client's pending-event queue. Perl has no explicit cap
# (it relies on LONG_POLLING_AUTOKILL / webCloseHandler to drop dead clients);
# this is a Python-side safety net so a stalled or half-dead client cannot grow
# the queue forever. Oldest events are dropped first.
MAX_QUEUED_EVENTS = 256


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


def _is_glob(pattern: str) -> bool:
    """True when ``pattern`` is a Bayeux channel pattern, not a channel.

    SqueezePlay/jive registers the catch-all ``/<clientId>/**`` via
    /meta/subscribe (Comet.lua:702) in addition to its targeted
    /slim/subscribe response channels. A pattern is only a channel
    *matcher* — it must never be dispatched as a query.

    ``/foo/**`` and ``/foo/*`` are Perl's own forms: Perl's
    Manager::add_channels turns them into the regexes ``^/foo/`` resp.
    ``^/foo/[^/]+``. The bare ``**`` / ``/**`` forms are tolerated
    Python-side only — Perl has no such form (it rewrites only
    ``/<something>/**`` and ``/<something>/*``, and a bare ``**`` is not
    even a valid regex: "Quantifier follows nothing"). No real client
    sends them; keeping them costs nothing for third-party catch-alls.
    """
    return pattern in ("**", "/**") or pattern.endswith("/**") \
        or pattern.endswith("/*")


def _channel_matches(pattern: str, channel: str) -> bool:
    """Bayeux channel matching, mirroring Perl
    Slim::Web::Cometd::Manager::add_channels:

    - ``/foo/**`` -> ``^/foo/``  (matches /foo/bar and /foo/bar/boo,
      not /foo; the trailing slash is mandatory)
    - ``/foo/*``  -> ``^/foo/[^/]+`` (Perl's regex is NOT end-anchored,
      so it is a prefix match: /foo/bar *and* /foo/bar/boo)
    - anything else matches the channel exactly.

    The bare ``**`` / ``/**`` forms match every channel, but that is a
    Python-side courtesy and **not** Perl semantics: Perl rewrites only
    the ``/<something>/**`` and ``/<something>/*`` patterns and hands
    everything else to Tie::RegexpHash unchanged, where a bare ``**``
    does not compile as a regex ("Quantifier follows nothing in
    regex", verified with Perl 5.38). No real client sends it — jive
    registers ``/<clientId>/**`` (Comet.lua:702).
    """
    if pattern == channel:
        return True
    if pattern in ("**", "/**"):
        return True
    if pattern.endswith("/**"):
        return channel.startswith(pattern[:-len("/**")] + "/")
    if pattern.endswith("/*"):
        head = pattern[:-1]  # '/foo/'
        return channel.startswith(head) and len(channel) > len(head)
    return False


# ---------------------------------------------------------------------------
# Request-driven subscriptions (Perl Slim::Web::Cometd:390-470)
# ---------------------------------------------------------------------------
# A Jive controller subscribes WITH a request and the Perl server stores it:
# on every change the same request is executed again and its result pushed to
# the response channel.  SqueezePlay's Now-Playing does exactly this
# (share/jive/jive/slim/Player.lua:993):
#
#   comet:subscribe('/slim/playerstatus/<id>',
#       _getSink(self, {'status','-',10,'menu:menu','useContextMenu:1',
#                       'subscribe:600'}), self.id, cmd)
#
# which Comet.lua:286-296 turns into
#   {channel:'/slim/subscribe', data:{request:[<player>,[...]], response:...}}
#
# Subscriptions that arrive WITHOUT a request (the bare /<cid>/** catch-all)
# still need a renderable payload: a plain `playerstatus - 1` answer carries
# no current_title/item_loop, which is why SqueezePlay's displayed title
# never updated (LIVE-01).  The fallbacks below are the jive forms.
JIVE_STATUS_REQUEST = ["status", "-", 10, "menu:menu", "useContextMenu:1"]
JIVE_SERVERSTATUS_REQUEST = ["serverstatus", "0", "50", "subscribe:60"]

# Commands that carry player status. Perl's Request::subscribe registers the
# auto-execute callback on the *dispatched command*, so a subscription for
# the jive `status` command and one for the Android `playerstatus` command
# must both be re-executed on a STAT change.
_PLAYER_STATUS_COMMANDS = ("status", "playerstatus")

# Players a subscription may be registered for without meaning a concrete
# player (SqueezeCtrl uses /null/ and 00:00:00:00:00:00).
_ANY_PLAYER = ("null", "00:00:00:00:00:00")


def _stored_request(data) -> list | None:
    """Return the request a subscription stored, or None.

    ``/slim/subscribe`` + ``/meta/subscribe`` with ``data.request`` register
    "request + subscribe" (Perl handleRequest type=subscribe); a pure channel
    registration stores no request and must not be dispatched.
    """
    if isinstance(data, dict):
        request = data.get("request")
        if isinstance(request, list) and request:
            return request
    return None


def _request_command(request: list) -> str:
    """First command token of a slim.request payload.

    Handles ``[player, [cmd, ...]]`` (jive/Android) and ``[[cmd, ...]]``
    (Material, no player).
    """
    for item in request:
        if isinstance(item, list):
            if item and isinstance(item[0], str):
                return item[0]
            return ""
    if request and isinstance(request[0], str):
        return request[0]
    return ""


def _request_player(request: list) -> str:
    """Player mac of a slim.request payload ('' when there is none)."""
    if request and isinstance(request[0], str):
        return request[0]
    return ""


def _default_request(subscription: str) -> list:
    """Jive-default request for a subscription that carries none.

    Mirrors the requests jive itself sends (Player.lua:993,
    SlimServer.lua:540).  A Bayeux *pattern* (``/<cid>/**``) registers the
    channel only — Perl's add_channels() never dispatches a query for a
    pattern, and dispatching one emitted a junk event on the pattern channel
    which Jive logged as "event we aren't subscribed to".
    """
    if not subscription or _is_glob(subscription):
        return []
    parts = subscription.split("/")
    sub_player = parts[-1] if len(parts) >= 2 else ""
    if "serverstatus" in subscription:
        return ["", list(JIVE_SERVERSTATUS_REQUEST)]
    if "playerstatus" in subscription or "/status" in subscription:
        return [sub_player, list(JIVE_STATUS_REQUEST)]
    if "menustatus" in subscription:
        # Squeezer subscribes to /<cid>/slim/menustatus/* — deliver the
        # home menu array.
        return ["", ["menustatus"]]
    if "favorites" in subscription:
        # SqueezeCtrl subscribes to /<cid>/slim/favorites/* — deliver the
        # favorites list (DB ids; the apps parse them as numbers).
        return ["", ["favorites", "items"]]
    return []


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

    def _matched_targets(self, client: CometdClient,
                         base_channels: list[str],
                         ) -> dict[str, tuple[dict, bool]]:
        """Return {concrete_event_channel: (subscription_data, exact)}.

        ``base_channels`` are the real channels an event can be sent on,
        most-specific first (e.g. ``/<cid>/slim/serverstatus`` then
        ``/slim/serverstatus``). A client is included when any of its
        subscriptions matches one of them — exact channels and Bayeux
        globs alike; per subscription the most specific matching base
        channel wins. An exact subscription wins over a glob for the same
        channel so the stored request (pagination/subscribe:N/tags) is
        preserved.

        The second element tells the caller whether the entry came from an
        exact channel or from a glob; ``_pick_target`` uses if to collapse
        several matches for ONE client into a single delivery (a client
        holding both a cid-less exact channel and ``/<cid>/**`` would
        otherwise get the same event twice).
        """
        matched: dict[str, tuple[dict, bool]] = {}
        for sub, data in list(client.subscriptions.items()):
            stored = data if isinstance(data, dict) else {}
            for channel in base_channels:
                if sub == channel or _channel_matches(sub, channel):
                    exact = sub == channel
                    if channel not in matched or exact:
                        matched[channel] = (stored, exact)
                    break
        return matched

    @staticmethod
    def _pick_target(targets: dict[str, tuple[dict, bool]],
                     base_channels: list[str]) -> tuple[str, dict] | None:
        """Reduce one client's channel matches to a SINGLE delivery.

        Perl delivers an event once per *matched channel*; the Python
        server synthesises the concrete channel, so a client that holds a
        cid-less exact channel *and* a catch-all glob matches two
        channels for one event. Jive treats the second copy as "event we
        aren't subscribed to", so exactly-once is enforced per client:
        an exact subscription wins over a glob (it is the channel the
        client explicitly named and it keeps the stored request), ties
        keep the most specific base channel.
        """
        for exact in (True, False):
            for channel in list(base_channels) + [c for c in targets
                                                   if c not in base_channels]:
                entry = targets.get(channel)
                if entry is not None and entry[1] is exact:
                    return channel, entry[0]
        return None

    def push(self, client_id: str, event: dict) -> None:
        client = self._clients.get(client_id)
        if client is None:
            return
        client.events.append(event)
        if len(client.events) > MAX_QUEUED_EVENTS:
            # Drop the oldest — a stalled/dead client must not grow the
            # queue without bound (Perl drops the whole client via
            # LONG_POLLING_AUTOKILL; this cap is the cheap safety net).
            del client.events[:len(client.events) - MAX_QUEUED_EVENTS]
        client.notify.set()

    async def notify_server_status(self) -> None:
        """Push a fresh serverstatus to all serverstatus subscribers.

        Called when players connect/disconnect so subscribed controllers
        (Jive/SqueezeCtrl/ioBroker) see the player list change.

        Routing is glob-aware: a client registered for ``/<cid>/**``
        (SqueezePlay's catch-all) receives the event on the concrete
        ``/<cid>/slim/serverstatus`` channel, matching Perl's
        manager->deliver_events() channel matching.

        Request-driven (Perl parity): the stored request is re-executed and
        its result pushed; only a subscription without a request falls back
        to the jive serverstatus request.

        Exactly once per CLIENT: a client holding both a targeted channel
        and ``/<cid>/**`` gets a single event (see _pick_target).
        """
        for client in list(self._clients.values()):
            base = [f"/{client.client_id}/slim/serverstatus",
                    "/slim/serverstatus"]
            targets = self._matched_targets(client, base)
            for sub, data in list(client.subscriptions.items()):
                request = _stored_request(data)
                if request is None or _request_command(request) != "serverstatus":
                    continue
                channel = sub
                if _is_glob(sub):
                    channel = next((c for c in base if _channel_matches(sub, c)), "")
                if channel:
                    targets.setdefault(channel,
                                       (data if isinstance(data, dict) else {},
                                        sub == channel))
            picked = self._pick_target(targets, base)
            if picked is None:
                continue
            channel, data = picked
            try:
                request = _stored_request(data) or ["", list(JIVE_SERVERSTATUS_REQUEST)]
                result = await self._dispatch(request)
                self.push(client.client_id, {
                    "channel": channel,
                    "data": result,
                    "id": 0,
                })
            except Exception:  # noqa: BLE001
                pass

    async def notify_player_status(self, player_id: str) -> None:
        """Push a fresh player status to status/playerstatus subscribers.

        Called by the slimproto layer on every STAT change so the
        controllers (SqueezePlay/SqueezeCtrl/Orange Squeeze/Squeezer) get
        the new state immediately instead of on their next poll.

        Delivery is request-driven (Perl parity): a subscription that
        stored a request gets THAT request re-executed, so SqueezePlay's
        ``status - 10 menu:menu useContextMenu:1`` subscription delivers
        ``current_title``/``item_loop`` — not a bare playerstatus frame.
        A subscription without a request (the pure ``/<cid>/**``
        catch-all) falls back to the jive default request, which is what
        makes the Now-Playing title update (LIVE-01).

        Exactly once per CLIENT: a client holding both a cid-less exact
        channel and ``/<cid>/**`` gets a single event (see _pick_target).
        """
        for client in list(self._clients.values()):
            targets: dict[str, tuple[dict, bool]] = {}
            base = [f"/{client.client_id}/slim/playerstatus/{player_id}",
                    f"/slim/playerstatus/{player_id}"]
            for sub, data in list(client.subscriptions.items()):
                stored = data if isinstance(data, dict) else {}
                request = _stored_request(data)
                if request is not None:
                    # request + subscribe: only deliver status subscriptions
                    # of the changed player (Perl re-executes the request,
                    # so its payload carries whatever the client asked for).
                    if _request_command(request) not in _PLAYER_STATUS_COMMANDS:
                        continue
                    req_player = _request_player(request)
                    if req_player and req_player not in _ANY_PLAYER \
                            and req_player != player_id:
                        continue
                    channel = sub
                    if _is_glob(sub):
                        channel = next(
                            (c for c in base if _channel_matches(sub, c)), "")
                    if channel:
                        targets.setdefault(channel, (stored, sub == channel))
                    continue
                if _is_glob(sub):
                    # e.g. /<cid>/** or /<cid>/slim/playerstatus/* — deliver
                    # on the concrete channel the pattern matches.
                    for channel in base:
                        if _channel_matches(sub, channel):
                            targets.setdefault(channel, (stored, False))
                    continue
                if "playerstatus" not in sub and "status/" not in sub:
                    continue
                parts = sub.split("/")
                sub_player = parts[-1] if len(parts) >= 2 else ""
                # The /null/... and 00:00:00:00:00:00 forms (SqueezeCtrl)
                # are app-chosen and player-agnostic — deliver to them for
                # ANY player change.
                if sub_player and sub_player not in ("", player_id, *_ANY_PLAYER):
                    continue
                targets.setdefault(sub, (stored, True))
            picked = self._pick_target(targets, base)
            if picked is None:
                continue
            channel, data = picked
            try:
                request = _stored_request(data)
                if request is not None:
                    # A subscription that names no player still gets the
                    # changed player's status (Perl dispatches mac-less
                    # subscriptions for any client).
                    if not _request_player(request):
                        command = next(
                            (item for item in request
                             if isinstance(item, list) and item),
                            list(JIVE_STATUS_REQUEST))
                        request = [player_id, command]
                else:
                    request = [player_id, list(JIVE_STATUS_REQUEST)]
                result = await self._dispatch(request)
                self.push(client.client_id, {
                    "channel": channel,
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
    # subscribe:N keep-alive
    # ------------------------------------------------------------------

    @staticmethod
    def _subscribe_interval(data: dict | None) -> int:
        """Parse the 'subscribe:<seconds>' token from a subscription request.

        The Android controllers subscribe to playerstatus with
        'status - 1 ... subscribe:30' — they expect a fresh status push
        every 30 s even when nothing changed (keep-alive). Returns 0 when
        no interval is requested.
        """
        if not isinstance(data, dict):
            return 0
        request = data.get("request")
        if not isinstance(request, list):
            return 0
        # The subscribe token lives in the nested command array, e.g.
        # ["<player>", ["status", "-", "1", "subscribe:30"]].
        for item in request:
            tokens = item if isinstance(item, list) else [item]
            for token in tokens:
                if isinstance(token, str) and token.startswith("subscribe:"):
                    try:
                        return max(0, int(token[len("subscribe:"):]))
                    except ValueError:
                        return 0
        return 0

    async def keepalive_loop(self) -> None:
        """Push fresh status to subscribe:N subscriptions on schedule.

        Runs for the lifetime of the server (started from the app/CLI
        entrypoints). Without it the controller apps never get status
        updates while a player is idle (mode=stop → no STAT events) and
        treat the silent stream as dead, reconnecting every ~75 s.
        """
        last: dict[tuple[str, str], float] = {}
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for client in list(self._clients.values()):
                for sub, data in list(client.subscriptions.items()):
                    interval = self._subscribe_interval(data)
                    if interval <= 0:
                        continue
                    key = (client.client_id, sub)
                    if now - last.get(key, 0) < interval:
                        continue
                    last[key] = now
                    try:
                        result = await self._dispatch(
                            data.get("request") or ["", ["status", "-", "1"]])
                        self.push(client.client_id, {
                            "channel": sub,
                            "data": result,
                            "id": 0,
                        })
                    except Exception:  # noqa: BLE001
                        pass

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_messages(self, messages: list[dict]) -> list[dict]:
        """Handle one batch of Bayeux messages, return immediate replies.

        /meta/connect messages are NOT answered here — they long-poll and
        get their single reply from the transport (cometd_stream.py or
        web/app.py), which also writes it into the streaming chunk.
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
                if not isinstance(data, dict):
                    data = {}
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
                    # Store the subscription together with its request. On a
                    # /slim/subscribe (jive Comet.lua:286-296) that request is
                    # the query to re-execute on every change; on a pure
                    # /meta/subscribe channel registration there is none.
                    client.subscriptions[subscription] = data
                    logger.info("Cometd %s subscribed %s", cid, subscription)
                    # Push the initial result of the subscription request.
                    # Without a client request fall back to the jive form so
                    # the seed payload already carries title/playlist
                    # (a bare `playerstatus - 1` does not — LIVE-01).
                    request = _stored_request(data)
                    if request is None:
                        request = _default_request(subscription)
                    if request:
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
                # Accept data.unsubscribe, data.subscription, or the TOP-LEVEL
                # 'subscription' field (libcometd/Android send it top-level,
                # like /meta/subscribe).
                subscription = (data.get("unsubscribe") or data.get("subscription")
                                or msg.get("subscription") or "")
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

            elif channel == "/meta/connect":
                # Deliberately NOT replied to here: /meta/connect long-polls,
                # and the transport (cometd_stream.py / web/app.py) writes
                # the one and only connect ack itself. Replying here as well
                # sent two /meta/connect answers per connect, and jive calls
                # _connected() for every one of them (Comet.lua:760-770).
                continue

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
