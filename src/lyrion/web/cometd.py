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

Client lifecycle: a client is removed on /meta/disconnect, when the
transport connection that handled its /meta/connect closes
(cometd_stream.py, and the ASGI streaming handler in web/app.py), or when
it goes idle past LONG_POLLING_AUTOKILL — Perl's disconnect timer, which
catches the handshake-only and non-connecting clients no close handler can
see. A connection that merely carried a handshake/subscribe/request POST
never removes the client: those POSTs normally live on another socket than
the connect (HTTP long-polling, Comet.lua:184), and Perl registers a
connection with the manager only in the /meta/(re)connect branch
(Slim/Web/Cometd.pm:286).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Optional

logger = logging.getLogger(__name__)

LONG_POLL_TIMEOUT = 60  # Perl Cometd.pm:48 LONG_POLLING_TIMEOUT => 60000 ms
# ("server will wait up to 60s for events to send", then answers the
# /meta/connect so the client polls again). Was an invented 25 s.

# The same value in milliseconds. Bayeux ``advice`` interval/timeout are
# milliseconds; Perl hands its own ms constant straight through
# (Cometd.pm:251 ``timeout => LONG_POLLING_TIMEOUT`` = 60000).
LONG_POLL_TIMEOUT_MS = LONG_POLL_TIMEOUT * 1000

# Perl Cometd.pm:47 ``use constant LONG_POLLING_INTERVAL => 0;`` — a
# long-polling client may re-poll immediately.
LONG_POLLING_INTERVAL = 0

# Perl Cometd.pm:45 ``use constant RETRY_DELAY => 5000;`` (ms). Used as the
# advice interval of a *streaming* /meta/connect (Cometd.pm:277-279
# ``interval => $streaming ? RETRY_DELAY : 0``).
RETRY_DELAY_MS = 5000

# Perl Slim::Web::Cometd LONG_POLLING_AUTOKILL (Cometd.pm:49, 693): after a
# long-polling response is sent, a timer is armed for this many seconds and
# fires disconnectClient if no new poll arrives. It covers exactly the clients
# Perl's webCloseHandler cannot see — a handshake-only client, a long-polling
# client with no active connection, or one that dies without ever sending
# /meta/disconnect. Python implements it as an idle sweep instead of a timer.
LONG_POLLING_AUTOKILL = 180.0

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
    # Wall-clock of the client's last message / connection activity. Used by
    # the idle reaper (Perl LONG_POLLING_AUTOKILL -> disconnectClient).
    last_seen: float = 0.0
    # Number of OPEN transports (streaming connections). A streaming
    # connection is legitimately silent between events, so the idle reaper
    # must never touch a client while this is > 0.
    connections: int = 0
    # The transport connection that currently owns this client. Only the
    # newest connection may remove the client (Perl webCloseHandler checks
    # ``$conn->[HTTP_CLIENT] == $httpClient`` before disconnectClient), so a
    # stale connection closing cannot kill a reconnected client.
    owner: object | None = None


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


def _player_key(value) -> str:
    """Comparison key for a player id: lower-case, no colons.

    SqueezePlay subscribes with the lower-case colon form of the MAC
    (``/14ff96e66d084eb6/slim/playerstatus/1c:87:2c:47:fc:36``, live log) while
    ``PlayerState.mac`` — the argument of every notify_* call — is upper-case
    (``1C:87:2C:47:FC:36``).  ``PlayerManager.get_player`` already treats both
    as the same player, so cometd routing has to compare them the same way or
    the client never receives its own player's events.
    """
    if not isinstance(value, str):
        return ""
    return value.replace(":", "").lower()


def _same_player(first, second) -> bool:
    """True when two player ids name the same player; '' matches nothing."""
    key = _player_key(first)
    return bool(key) and key == _player_key(second)


def _channel_player(channel: str) -> str:
    """The player named by a channel's last path segment ('' when none)."""
    if not isinstance(channel, str):
        return ""
    parts = channel.split("/")
    return parts[-1] if len(parts) >= 2 else ""


def _is_player_status_subscription(channel: str) -> bool:
    """True when a channel-only subscription carries *player status*.

    ``/x/slim/playerstatus/<mac>`` and ``/x/slim/status/<mac>`` qualify.
    ``/x/slim/displaystatus/<mac>`` and ``/x/slim/menustatus/<mac>`` merely
    *contain* ``status/`` and must NOT be fed a playerstatus payload — their
    own command drives them (a request-driven one is filtered by its command
    above).  A trailing empty segment keeps the old player-agnostic
    tolerance: only the player comparison then decides.
    """
    if not isinstance(channel, str):
        return False
    parts = channel.split("/")
    if len(parts) < 2:
        return False
    if parts[-1] == "":
        return "playerstatus" in channel or "/status/" in channel
    return parts[-2] in ("playerstatus", "status")


def _status_channel_player(channel: str) -> str:
    """Player named by a *player-status* channel, '' when it is not one.

    ``/x/slim/playerstatus/<mac>`` and ``/x/slim/status/<mac>`` qualify;
    ``/x/slim/displaystatus/<mac>`` and ``/x/slim/menustatus/<mac>`` must
    NOT — they name the same player but a different sink, so a glob must
    never be concretised onto them.
    """
    if not isinstance(channel, str):
        return ""
    parts = channel.split("/")
    if len(parts) >= 2 and parts[-2] in ("playerstatus", "status"):
        return parts[-1]
    return ""


def _set_target(targets: dict[str, tuple[dict, bool]], channel: str,
                stored: dict, exact: bool) -> None:
    """Record one client's delivery on a concrete channel.

    The channel key is the dedupe: Perl delivers once per matched channel
    (Manager::deliver_events).  When a glob and an exact subscription
    resolve to the same channel the exact one wins, so the request the
    client explicitly registered (pagination/subscribe:N/tags) is the one
    re-executed.
    """
    current = targets.get(channel)
    if current is None or exact or not current[1]:
        targets[channel] = (stored, exact)


# Channel roots that are namespaces, never clientIds. A /slim/subscribe
# without a clientId derives the id from its response channel's first segment
# (Orange Squeeze sends none); without this guard a response channel like
# ``/slim/serverstatus`` minted a phantom client called ``slim`` that then
# received every serverstatus event (review P3-2).
_RESERVED_CHANNEL_ROOTS = frozenset({"slim", "meta", "cometd"})


def _client_id_from_channel(channel) -> str:
    """ClientId embedded as the first segment of a response channel.

    Perl expects ``$response =~ m{/([0-9a-f]{8})/}`` (Cometd.pm:427), but
    Python hands out non-hex ids too (``lyrion-N``, ``1<uuid>``) and a real
    jive/Android client reuses whatever the handshake returned, so any
    non-empty, non-glob, non-namespace first segment is accepted. Returns
    "" when there is no usable segment.
    """
    if not isinstance(channel, str) or not channel.startswith("/"):
        return ""
    parts = channel.split("/")
    if len(parts) < 2:
        return ""
    segment = parts[1]
    if not segment or _is_glob(segment) or segment in _RESERVED_CHANNEL_ROOTS:
        return ""
    return segment


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


def _http_timestamp() -> str:
    """Perl's ``timestamp => time2str(time())`` (HTTP::Date, RFC 1123, GMT).

    Perl stamps /meta/handshake (Cometd.pm:260), /meta/(re)connect
    (Cometd.pm:276), /meta/disconnect (Cometd.pm:338) and the
    invalid-clientId advice (Cometd.pm:235) with it; the Bayeux clients echo
    it into their logs. ``email.utils.formatdate(usegmt=True)`` produces the
    byte-identical string (verified against Perl 5.38 / HTTP::Date).
    """
    return formatdate(time.time(), usegmt=True)


def connect_advice(connection_type: str = "") -> dict:
    """Advice for a /meta/(re)connect reply (Perl Cometd.pm:277-279).

    Perl: ``advice => { interval => $streaming ? RETRY_DELAY : 0 }`` where
    ``RETRY_DELAY`` is 5000 (Cometd.pm:45). Bayeux advice values are
    milliseconds, so a streaming connect advertises the 5000 ms retry delay
    and a long-polling connect advertises 0 (re-poll immediately,
    Cometd.pm:47). ``timeout`` is the 60 s hold time in milliseconds — the
    same number Perl puts into the handshake advice (Cometd.pm:251); it used
    to go out as ``60``, i.e. 60 ms.

    ``reconnect => 'retry'`` is kept from the previous Python form: Perl's
    connect advice carries only ``interval``, this is a documented superset.
    """
    streaming = connection_type == "streaming"
    return {
        "reconnect": "retry",
        "interval": RETRY_DELAY_MS if streaming else LONG_POLLING_INTERVAL,
        "timeout": LONG_POLL_TIMEOUT_MS,
    }


def connect_ack(msg: dict, client_id: str) -> dict:
    """The single /meta/(re)connect ack (Perl Cometd.pm:271-280).

    Perl stores it in ``first_event`` and every field is fixed there:
    ``id``, ``channel``, ``clientId``, ``successful``, ``timestamp``
    (``time2str(time())``, :276) and ``advice => { interval => $streaming ?
    RETRY_DELAY : 0 }`` (:278). Both transports must emit exactly this
    frame — it is the one and only answer to a /meta/connect
    (``handle_messages`` deliberately never answers that channel).

    ``advice.timeout`` is the documented Python superset of Perl's connect
    advice (see ``connect_advice``); it must be the 60 s *milliseconds*
    value, not 60, or a client reads it as a 60 ms server timeout.
    """
    return {
        "channel": "/meta/connect",
        "successful": True,
        "clientId": client_id,
        "id": msg.get("id", ""),
        "timestamp": _http_timestamp(),
        "advice": connect_advice(msg.get("connectionType", "")),
    }


def connect_timeout(msg: dict, default: float = LONG_POLL_TIMEOUT) -> float:
    """Hold time in SECONDS for one long-polling /meta/connect.

    Perl Cometd.pm:302-306: ``my $timeout = LONG_POLLING_TIMEOUT;`` and
    "Client can override timeout" — a client that sends
    ``advice.timeout`` (milliseconds, 0 = answer now) decides how long the
    poll may be held.  A Bayeux client does exactly that when it wants its
    pending requests flushed immediately (libcometd/jive send
    ``advice: {timeout: 0}``); ignoring it held the reply for the full 60 s
    while the client's own network timeout had long expired, so the app
    reported "connection failed" although the server was listening.
    """
    advice = msg.get("advice") if isinstance(msg, dict) else None
    if isinstance(advice, dict) and "timeout" in advice:
        try:
            return max(0.0, float(advice["timeout"]) / 1000.0)
        except (TypeError, ValueError):
            return default
    return default


def has_invalid_client_advice(replies: list) -> bool:
    """True when a reply is Perl's invalid-clientId / re-handshake advice.

    Perl answers a message whose clientId the manager does not know with
    ``advice => { reconnect => 'handshake', interval => 0 }`` and ``last``s
    out of the message loop (Cometd.pm:228-244), so the /meta/connect branch
    is never reached and the response carries NO connect ack. Both transports
    use this to recognise that case instead of fabricating a successful
    connect ack next to it.
    """
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        advice = reply.get("advice")
        if isinstance(advice, dict) and advice.get("reconnect") == "handshake":
            return True
    return False


def _subscriptions(message: dict, data: dict) -> list[str]:
    """The channel(s) a subscribe message registers.

    Perl (Cometd.pm:350-355): "a channel name or a channel pattern or an
    array of channel names and channel patterns", wrapping a scalar into an
    array. Material sends ``data.response``, Jive/SqueezeClient send
    ``data.subscription``, SqueezeCtrl sends ``subscription`` as a TOP-LEVEL
    field of /meta/subscribe — all three are still accepted. A list value is
    why this returns a list: indexing ``subscriptions[sub]`` with one crashed
    the whole batch with ``unhashable type: 'list'``.
    """
    raw = (data.get("subscription") or data.get("response")
           or message.get("subscription") or "")
    if isinstance(raw, str):
        raw = [raw] if raw else []
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, str) and s]


def _merged_subscription(existing, data) -> dict:
    """Store ``data`` for a channel without dropping an earlier request.

    Perl's /meta/subscribe is a pure channel registration
    (``Manager::add_channels``, Cometd.pm:357) and never touches the request
    a client registered through /slim/subscribe — the two live in different
    places (channel buckets vs. the Request subsystem). Blindly replacing the
    stored payload with a request-less one would silently downgrade every
    later push to the jive default and lose the client's
    pagination/``subscribe:N``.
    """
    if not isinstance(existing, dict) or not isinstance(data, dict):
        return data if isinstance(data, dict) else {}
    if _stored_request(existing) and not _stored_request(data):
        merged = dict(data)
        merged["request"] = existing["request"]
        if existing.get("response") and not merged.get("response"):
            merged["response"] = existing["response"]
        return merged
    return data


class CometdManager:
    """Server-side Bayeux/Cometd endpoint for LMS-style controllers.

    Clients are Jive-family apps (Orange Squeeze, SqueezeCtrl, Squeezer,
    SqueezeClient, Jivelite, Material) that subscribe to /slim/... and
    /meta/... channels over /cometd (long-polling or streaming).
    """

    def __init__(self, jsonrpc, clock=None) -> None:
        self._jsonrpc = jsonrpc
        self._clients: dict[str, CometdClient] = {}
        self._counter = itertools.count(1)
        # Injectable wall clock so the idle reaper (LONG_POLLING_AUTOKILL) can
        # be tested without sleeping; production uses time.time.
        self._clock = clock or time.time
        self._autokill_task: asyncio.Task | None = None
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
        existing = self._clients.get(client_id)
        if existing is not None:
            # Re-handshake of a KNOWN clientId (jive re-handshakes after a
            # server restart): keep the SAME object. Replacing it threw away
            # its subscriptions AND its open-transport count, so the fresh
            # object had connections == 0 while its stream was still open —
            # the idle reaper then killed a live client after
            # LONG_POLLING_AUTOKILL and Now-Playing stopped updating (live
            # 2026-09-12: "Player spielt, Fenster aktualisiert nicht").
            existing.last_seen = self._clock()
            return existing
        client = CometdClient(client_id=client_id)
        client.last_seen = self._clock()
        self._clients[client_id] = client
        self.ensure_autokill()
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
            client.last_seen = self._clock()
            self._clients[client_id] = client
            self.ensure_autokill()
        else:
            client.last_seen = self._clock()
        return client

    def get(self, client_id: str) -> CometdClient | None:
        return self._clients.get(client_id)

    def remove(self, client_id: str) -> None:
        self._clients.pop(client_id, None)

    def touch(self, client_id: str) -> None:
        """Mark client activity (Perl kills the autokill timer on connect)."""
        client = self._clients.get(client_id)
        if client is not None:
            client.last_seen = self._clock()

    def register_connection(self, client_id: str, owner: object) -> None:
        """Make ``owner`` the client's current (newest) transport connection.

        Called ONLY by a connection that processed the client's /meta/connect
        — mirrors Perl Manager::register_connection (Cometd.pm:286, reached
        only from the /meta/(re)connect branch). Combined with
        remove_if_owner this keeps a stale connection's close from removing a
        client that already reconnected, and stops a handshake/subscribe/
        request POST (which never calls this) from ever owning the client.
        """
        client = self._clients.get(client_id)
        if client is None:
            return
        client.owner = owner
        client.last_seen = self._clock()

    def remove_if_owner(self, client_id: str, owner: object) -> bool:
        """Remove the client only while ``owner`` is its current connection.

        Mirrors Perl webCloseHandler (Cometd.pm:1003): only the connection
        that handled the client's /meta/connect may remove it. A connection
        that merely carried a handshake/subscribe/request POST finds
        ``client.owner`` pointing at another connection (or None) and must do
        nothing — those POSTs often live on a different socket than the
        connect (HTTP long-polling, Comet.lua:184).
        """
        client = self._clients.get(client_id)
        if client is None or client.owner is not owner:
            return False
        logger.info("Cometd connection close -> removing client %s", client_id)
        self.remove(client_id)
        return True

    def connection_open(self, client_id: str) -> None:
        """Record an open transport so the idle reaper spares the client.

        A streaming connection stays open indefinitely and is silent between
        events, so it must not count as idleness.
        """
        client = self._clients.get(client_id)
        if client is None:
            return
        client.connections += 1
        client.last_seen = self._clock()

    def connection_closed(self, client_id: str) -> None:
        client = self._clients.get(client_id)
        if client is None:
            return
        client.connections = max(0, client.connections - 1)
        client.last_seen = self._clock()

    def kill_idle_clients(self, timeout: float | None = None) -> list[str]:
        """Drop clients idle longer than ``timeout`` and return their ids.

        This is Perl's LONG_POLLING_AUTOKILL -> disconnectClient: a client
        that stops polling, never connects after a handshake, or dies without
        /meta/disconnect is removed so notify_* / keepalive_loop stop doing
        work for it. Clients with an open transport are never reaped.
        """
        limit = LONG_POLLING_AUTOKILL if timeout is None else timeout
        now = self._clock()
        reaped: list[str] = []
        for client_id, client in list(self._clients.items()):
            if client.connections:
                continue
            if now - client.last_seen >= limit:
                self.remove(client_id)
                reaped.append(client_id)
                logger.info("Cometd idle autokill -> client %s", client_id)
        return reaped

    async def autokill_loop(self, interval: float = 5.0,
                            timeout: float | None = None) -> None:
        """Periodically reap idle clients (Perl's autokill timer)."""
        while True:
            await asyncio.sleep(interval)
            self.kill_idle_clients(timeout)

    def ensure_autokill(self) -> None:
        """Start the idle reaper once, lazily, when the first client appears.

        The manager is shared between uvicorn and the native stream server, so
        starting the task here keeps it single while both transports benefit.
        No-op outside a running event loop.
        """
        if self._autokill_task is not None and not self._autokill_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._autokill_task = loop.create_task(self.autokill_loop())

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
        exact channel or from a glob; ``_pick_targets`` uses it to drop a glob
        that only duplicates an exact subscription for the same event.
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
    def _pick_targets(targets: dict[str, tuple[dict, bool]],
                      base_channels: list[str]) -> list[tuple[str, dict]]:
        """Reduce one client's channel matches to its list of deliveries.

        Perl delivers an event once per *matched channel*
        (Manager::deliver_events iterates the channel buckets), so a client
        holding two distinct EXACT status subscriptions — two response
        channels, two stored requests — gets one event per channel
        (Cometd.pm:840-870). ``targets`` is already keyed by the concrete
        channel, so distinct channels are distinct deliveries.

        A Bayeux *glob* is only a catch-all: when the client also holds an
        exact channel for the same event it adds nothing and is dropped (the
        channel the client explicitly named wins and keeps its stored
        request). A glob-only client keeps its single, most specific delivery.
        That keeps jive's ``/<cid>/**`` from duplicating a targeted
        ``/<cid>/slim/...`` push without collapsing two genuine exact
        subscriptions into one (review P3-1).
        """
        exact: list[str] = []
        globs: list[str] = []
        for channel in list(base_channels) + [c for c in targets
                                              if c not in base_channels]:
            entry = targets.get(channel)
            if entry is None:
                continue
            (exact if entry[1] else globs).append(channel)
        # Exact subscriptions deliver on every channel they named; a glob is a
        # catch-all with no request of its own, so it stays a single delivery.
        chosen = exact or globs[:1]
        return [(c, targets[c][0]) for c in chosen]

    def _registered_player_spelling(self, client: CometdClient,
                                    player_id: str) -> str:
        """The spelling of ``player_id`` the client itself registered.

        Events must go out on the exact channel a client subscribed to: jive
        (share/jive/jive/net/Comet.lua) compares the channel name verbatim, so
        an event pushed on a channel we rebuilt with a different mac spelling
        is dropped as "not subscribed" and the Now-Playing screen freezes.
        SqueezePlay registers ``/<cid>/slim/playerstatus/1c:87:...`` (lower
        case) while ``PlayerState.mac`` is upper case.  Returns ``player_id``
        unchanged when the client registered no concrete channel naming that
        player (a glob-only client cannot express a spelling of its own).
        """
        for sub in list(client.subscriptions):
            if _is_glob(sub):
                continue
            candidate = _channel_player(sub)
            if candidate and _same_player(candidate, player_id):
                return candidate
        return player_id

    @staticmethod
    def _glob_concrete_channel(client: CometdClient, pattern: str,
                               player_id: str, base: list[str]) -> str:
        """Concrete channel a glob subscription is delivered on.

        Preference: a *player-status* channel the client itself registered for
        this player that the pattern matches (that is the channel jive holds,
        so it accepts the event and calls its sink); otherwise the standard
        playerstatus path from ``base``, already built with the client's own
        spelling.  Menu/display status channels are skipped — they name the
        same player but a different sink.
        """
        for sub in list(client.subscriptions):
            if _is_glob(sub):
                continue
            sub_player = _status_channel_player(sub)
            if sub_player and _same_player(sub_player, player_id) \
                    and _channel_matches(pattern, sub):
                return sub
        return next((c for c in base if _channel_matches(pattern, c)), "")

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

        Exactly once per matched channel: a client holding a targeted channel
        *and* a redundant ``/<cid>/**`` glob gets a single event on the
        targeted channel (see _pick_targets); two distinct exact
        subscriptions each get their own event.
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
            for channel, data in self._pick_targets(targets, base):
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

        Published on the CLIENT's channel spelling: every event goes out on
        the channel name exactly as that client registered it (a glob is
        concretised to the channel the client holds for this player).  jive
        compares the channel name verbatim, so a rebuilt
        ``/…/playerstatus/<MAC>`` with a different case is discarded as
        "not subscribed" and Now-Playing freezes.  Player ids are compared
        case- and colon-insensitively (``_same_player``) because
        ``PlayerState.mac`` is upper-case while clients register lower-case.

        Exactly once per matched channel: a redundant cid-less exact channel
        and ``/<cid>/**`` collapse to the exact channel, while two distinct
        exact subscriptions each get their own event (see _pick_targets).
        """
        for client in list(self._clients.values()):
            targets: dict[str, tuple[dict, bool]] = {}
            # Candidate concrete channels, built with the spelling THIS client
            # registered for the player (fallback: the id we were given).
            spelling = self._registered_player_spelling(client, player_id)
            base = [f"/{client.client_id}/slim/playerstatus/{spelling}",
                    f"/slim/playerstatus/{spelling}"]
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
                            and not _same_player(req_player, player_id):
                        continue
                    channel = sub
                    if _is_glob(sub):
                        channel = self._glob_concrete_channel(
                            client, sub, player_id, base)
                    if channel:
                        _set_target(targets, channel, stored, sub == channel)
                    continue
                if _is_glob(sub):
                    # e.g. /<cid>/** or /<cid>/slim/playerstatus/* — deliver
                    # on the concrete channel the client holds for this player.
                    channel = self._glob_concrete_channel(
                        client, sub, player_id, base)
                    if channel:
                        _set_target(targets, channel, stored, False)
                    continue
                if not _is_player_status_subscription(sub):
                    continue
                sub_player = _channel_player(sub)
                # The /null/... and 00:00:00:00:00:00 forms (SqueezeCtrl)
                # are app-chosen and player-agnostic — deliver to them for
                # ANY player change.
                if sub_player and sub_player not in _ANY_PLAYER \
                        and not _same_player(sub_player, player_id):
                    continue
                _set_target(targets, sub, stored, True)
            for channel, data in self._pick_targets(targets, base):
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
            # Drop bookkeeping for clients the idle reaper removed — a
            # per-(client, channel) key would otherwise grow forever.
            live = {c.client_id for c in self._clients.values()}
            for key in [k for k in last if k[0] not in live]:
                del last[key]

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

            # Perl Cometd.pm:225-244: a message from a clientId the manager
            # does not know (streaming connection lost, server restarted) is
            # answered "invalid clientId" with advice.reconnect = 'handshake',
            # and the message is NOT processed. The client then performs a
            # fresh handshake AND re-sends all its subscriptions.
            #
            # Answering success and creating the client on the fly (the old
            # get_or_create behaviour) left jive believing its lost
            # /slim/playerstatus/<mac> subscription was still registered, so
            # it never subscribed again: the Now-Playing title, artist and
            # cover stayed on the OLD track while the audio and the time
            # (client-local) were correct (live 2026-09-12).
            if channel != "/meta/handshake" and cid and cid not in self._clients:
                reply.update({
                    "successful": False,
                    "clientId": None,
                    "timestamp": _http_timestamp(),
                    "error": "invalid clientId",
                    "advice": {"reconnect": "handshake",
                               "interval": LONG_POLLING_INTERVAL},
                })
                replies.append(reply)
                continue

            if channel == "/meta/handshake":
                client = self.handshake(msg.get("ext") if isinstance(msg.get("ext"), dict) else None)
                reply.update({
                    "successful": True,
                    "version": "1.0",
                    "clientId": client.client_id,
                    "supportedConnectionTypes": ["long-polling", "streaming"],
                    "timestamp": _http_timestamp(),
                    # Perl Cometd.pm:248-252: the handshake advice carries
                    # reconnect/interval AND the 60 s hold time in ms. The
                    # timeout was missing here.
                    "advice": {"reconnect": "retry",
                               "interval": LONG_POLLING_INTERVAL,
                               "timeout": LONG_POLL_TIMEOUT_MS},
                })
                logger.info("Cometd handshake -> client %s", client.client_id)

            elif channel in ("/meta/subscribe", "/slim/subscribe"):
                data = msg.get("data", {})
                if not isinstance(data, dict):
                    data = {}
                # Material sends data.response, Jive/SqueezeClient send
                # data.subscription, SqueezeCtrl sends 'subscription' as
                # a TOP-LEVEL field of /meta/subscribe — accept all, and an
                # ARRAY of channels/patterns too (Perl Cometd.pm:350-355).
                subscriptions = _subscriptions(msg, data)
                if channel == "/slim/subscribe":
                    # Perl Slim/Web/Cometd.pm:479-492: a /slim/subscribe is a
                    # request+subscribe and needs BOTH data.request and
                    # data.response. Missing either is answered with an error
                    # (successful:false) — never a silent success the client
                    # trusts while nothing was registered (review P3-3).
                    request = data.get("request")
                    if not (isinstance(request, list) and request):
                        reply.update({"successful": False,
                                      "error": "request data key not found"})
                        replies.append(reply)
                        continue
                    if not data.get("response"):
                        reply.update({"successful": False,
                                      "error": "response data key not found"})
                        replies.append(reply)
                        continue
                # Orange Squeeze's /slim/subscribe carries NO clientId — derive
                # it from the response channel (/<clientId>/...). A namespace
                # root like /slim/... is never a clientId (review P3-2).
                if not cid:
                    cid = _client_id_from_channel(
                        subscriptions[0] if subscriptions else "")
                client = self.get_or_create(cid) if cid else None
                if client is None:
                    # No clientId and no derivable id in the response channel:
                    # never mint a client (review P3-2) — ack the failure.
                    reply.update({"successful": False,
                                  "error": "clientId not found"})
                    replies.append(reply)
                    continue
                # Perl Cometd.pm:359-367 pushes one /meta/subscribe ack per
                # subscription; /slim/subscribe answers once (Cometd.pm:454).
                acks = list(subscriptions) if channel == "/meta/subscribe" \
                    else (subscriptions[:1] or [""])
                for sub in acks:
                    if sub:
                        stored = _merged_subscription(
                            client.subscriptions.get(sub), data)
                        client.subscriptions[sub] = stored
                        logger.info("Cometd %s subscribed %s", cid, sub)
                        # Push the initial result of the subscription request.
                        # Without a client request fall back to the jive form
                        # so the seed payload already carries title/playlist
                        # (a bare `playerstatus - 1` does not — LIVE-01).
                        seed = _stored_request(stored)
                        if seed is None:
                            seed = _default_request(sub)
                        if seed:
                            result = await self._dispatch(seed)
                            self.push(cid, {
                                "channel": sub,
                                "data": result,
                                # SqueezeClient's Message class requires id:
                                # Int — a missing id breaks the array parse.
                                "id": msg.get("id", ""),
                            })
                    ack = dict(reply)
                    ack["successful"] = True
                    ack["error"] = None
                    # Perl puts clientId into every subscribe ack
                    # (Cometd.pm:363 / :456); libcometd (SqueezeClient)
                    # requires the subscription field — else 'Subscription
                    # response missing'.
                    ack["clientId"] = cid
                    if sub:
                        ack["subscription"] = sub
                    replies.append(ack)
                if not subscriptions:
                    reply.update({"successful": True, "error": None,
                                  "clientId": cid})
                    replies.append(reply)
                continue

            elif channel in ("/meta/unsubscribe", "/slim/unsubscribe"):
                client = self.get(cid)
                self.touch(cid)
                data = msg.get("data", {})
                if not isinstance(data, dict):
                    data = {}
                # Accept data.unsubscribe (Perl's field, Cometd.pm:504 /
                # :371), data.subscription, or the TOP-LEVEL 'subscription'
                # field (libcometd/Android send it top-level, like
                # /meta/subscribe). A channel ARRAY is accepted too
                # (Perl Cometd.pm:371-376).
                raw_unsub = data.get("unsubscribe")
                if isinstance(raw_unsub, str) and raw_unsub:
                    subscriptions = [raw_unsub]
                elif isinstance(raw_unsub, list):
                    subscriptions = [s for s in raw_unsub
                                     if isinstance(s, str) and s]
                else:
                    subscriptions = _subscriptions(msg, data)
                if client is not None:
                    for sub in subscriptions:
                        client.subscriptions.pop(sub, None)
                # Perl Cometd.pm:382-387 / :519-525: every unsubscribe ack
                # carries clientId and the subscription; /slim/unsubscribe
                # echoes data back.
                reply.update({"successful": client is not None,
                              "clientId": cid or None})
                if subscriptions:
                    reply["subscription"] = subscriptions[0]
                if channel == "/slim/unsubscribe":
                    reply["data"] = data

            elif channel == "/slim/request":
                data = msg.get("data", {})
                response_channel = data.get("response", "")
                # Orange Squeeze's slim/request carries NO clientId — derive it
                # from the response channel (/<clientId>/...); a namespace root
                # like "slim" is never a clientId (review P3-2).
                if not cid:
                    cid = _client_id_from_channel(response_channel)
                client = self.get_or_create(cid) if cid else None
                request = data.get("request") or []
                result = await self._dispatch(request)
                if client is not None:
                    self.push(cid, {
                        "channel": response_channel,
                        "data": result,
                        "id": msg.get("id", ""),
                    })
                    # Perl Cometd.pm:575-580: the /slim/request ack carries
                    # clientId (a missing one breaks libcometd's correlation).
                    reply.update({"successful": True, "clientId": cid})
                else:
                    reply.update({"successful": False, "clientId": None})

            elif channel == "/meta/disconnect":
                # Remove the client so its subscriptions/events are freed.
                self.remove(cid)
                # Perl Cometd.pm:333-339: the ack is stamped with a timestamp
                # and Perl sets ``Connection: close``. The transports close
                # the socket themselves.
                reply.update({
                    "successful": True,
                    "clientId": cid or None,
                    "timestamp": _http_timestamp(),
                    "advice": {"reconnect": "none",
                               "interval": LONG_POLLING_INTERVAL},
                })
                logger.info("Cometd disconnect -> client %s", cid)

            elif channel == "/meta/ping":
                reply.update({"successful": True})

            elif channel == "/meta/connect":
                # A (re)connect is activity: Perl cancels the autokill timer
                # here (Cometd.pm:289 killTimers) so a client that keeps
                # polling is never reaped while it is live.
                self.touch(cid)
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
