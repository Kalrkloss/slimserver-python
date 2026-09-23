"""Perl's notification core — the ``Slim::Control::Request`` notification half.

Perl sends *notifications* (not answers) by building a request from an array
and pushing it onto a queue that the idle loop drains::

    Slim/Control/Request.pm:838-862   notifyFromArray() / checkNotifications()
    Slim/Control/Request.pm:967-1108  Request->new(): walk the dispatch tree,
                                      declared parameters, every surplus token
                                      becomes the positional ``_p<i>`` (:1049-1061)
    Slim/Control/Request.pm:1063      requeststr = join(',', @request)
    Slim/Control/Request.pm:1754-1764 isCommand() / isQuery()
    Slim/Control/Request.pm:2381-2395 __matchingRequest()
    Slim/Control/Request.pm:2005-2102 notify(): ``%listeners`` first, then
                                      ``%subscribers`` (autoExecuteFilter)
    Slim/Control/Request.pm:513-553   subscribe()/unsubscribe() — the listeners
    Slim/Control/Request.pm:2226-2296 renderAsArray() (``_key`` → bare value)
    Slim/Control/Request.pm:641-663   the NOTIFICATION entries of addDispatch
    Slim/Control/Stdio.pm:123-138     array_to_string(): clientid in front,
                                      every element escaped, joined with ' '
    Slim/Plugin/CLI/Plugin.pm:970-1017 cli_subscribe_notification() — a listener
    Slim/Plugin/CLI/Plugin.pm:934-966  cli_subscribe_manage() — subscribe the
                                      listener only while a client listens
    Slim/Control/Queries.pm:3925-3990 statusQuery_filter() — a subscriber filter

Rendering reuses :mod:`lyrion.control.queries` (``render_line``) so a
notification line and an answer line are built by exactly one code path.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from lyrion.control.queries import render_line

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dispatch table (notifications only)
# ---------------------------------------------------------------------------
#
# ``Slim/Control/Request.pm:641-663`` — the NOTIFICATION block of addDispatch.
# Key = the verb tuple Perl matches by walking its dispatch tree
# (:1010-1014), value = the declared parameter names in order (:1026-1028).
# ``newsong`` declares none, so ``['playlist','newsong',title,index]`` leaves
# ``_p2`` = title and ``_p3`` = index (Request.pm:1049-1061 — this is exactly
# what AudioScrobbler checks to tell the two newsong sources apart,
# ``Slim/Plugin/AudioScrobbler/Plugin.pm:322-327``).
NOTIFICATION_DISPATCH: dict[tuple[str, ...], tuple[str, ...]] = {
    ("client", "disconnect"): (),                       # :641
    ("client", "new"): (),                              # :642
    ("client", "reconnect"): (),                        # :643
    ("client", "upgrade_firmware"): (),                 # :644
    ("playlist", "load_done"): (),                      # :646
    ("playlist", "newsong"): (),                        # :645
    ("playlist", "open"): ("_path",),                   # :647
    ("playlist", "sync"): (),                           # :648
    ("playlist", "cant_open"): ("_url", "_error"),      # :649
    ("playlist", "pause"): ("_newvalue",),              # :650
    ("playlist", "stop"): (),                           # :651
    ("rescan", "done"): (),                             # :652
    ("library", "changed"): ("_newvalue",),             # :653
    ("unknownir",): ("_ircode", "_time"),               # :654
    ("prefset",): ("_namespace", "_prefname", "_newvalue"),   # :655
    ("displaynotify",): ("_type", "_parts", "_duration"),     # :656
    ("alarm", "sound"): ("_id",),                       # :657
    ("alarm", "end"): ("_id",),                         # :658
    ("alarm", "snooze"): ("_id",),                      # :659
    ("alarm", "snooze_end"): ("_id",),                  # :660
    ("fwdownloaded",): ("_machine",),                   # :661
    ("newmetadata",): (),                               # :662
}


# ---------------------------------------------------------------------------
# The notification itself
# ---------------------------------------------------------------------------


@dataclass
class Notification:
    """One Perl ``Slim::Control::Request`` built by ``notifyFromArray``.

    ``verbs`` are the matched request verbs (:1010-1014), ``params`` the
    parameters in Perl's insertion order (``tie %params, 'Tie::IxHash'``,
    :849/:980-983) so ``renderAsArray`` walks them in that order.
    """

    client_id: Optional[str] = None
    verbs: list[str] = field(default_factory=list)
    params: list[tuple[str, Any]] = field(default_factory=list)
    # ``Request.pm`` sources: the CLI sets 'CLI' so its own connection is not
    # echoed twice (Plugin/CLI/Plugin.pm:994-995).
    source: Optional[str] = None
    connection_id: Optional[str] = None

    # -- Perl Request->new ($clientid, $requestLineRef) :967-1108 ----------
    @classmethod
    def from_array(
        cls,
        client_id: Optional[str],
        terms: list[Any],
        *,
        source: Optional[str] = None,
        connection_id: Optional[str] = None,
    ) -> "Notification":
        """``Slim::Control::Request->new($clientid, @request, 1)`` (:967-1108).

        The dispatch tree is walked for the longest verb prefix that is a
        leaf (:1010-1014); its declared parameters take the tokens at their
        position (:1026-1028) and every surplus token becomes the positional
        parameter ``_p<i>`` with ``i`` = the index in the *whole* request
        array (:1049-1061) — that is why ``_p2``/``_p3`` are fixed.
        """
        terms = list(terms)
        declared: tuple[str, ...] = ()
        verbs: list[str] = []
        for length in (2, 1):
            candidate = tuple(terms[:length])
            if candidate in NOTIFICATION_DISPATCH:
                verbs = list(candidate)
                declared = NOTIFICATION_DISPATCH[candidate]
                break
        if not verbs:
            # No dispatch leaf matched.  Perl still keeps the request: the
            # object is built with ``_request => \@request`` (:987) and the
            # tree walk only *parses* what it finds (:1008-1014) — an
            # unregistered verb (``notifyFromArray(['firmware_upgrade'])``,
            # Squeezebox2.pm:372) therefore survives with an empty parameter
            # list and is delivered to subscribers as-is.  Dropping it here
            # would silently swallow every notification Perl sends.
            verbs = list(terms)

        params: list[tuple[str, Any]] = []
        index = len(verbs)
        for name in declared:
            params.append((name, terms[index] if index < len(terms) else None))
            index += 1
        for position in range(index, len(terms)):
            params.append((f"_p{position}", terms[position]))

        return cls(client_id=client_id, verbs=verbs, params=params,
                   source=source, connection_id=connection_id)

    # -- Perl accessors ---------------------------------------------------
    @property
    def request_string(self) -> str:
        """``$request->getRequestString`` — ``join(' ', @{$self->{_request}})``."""
        return " ".join(self.verbs)

    @property
    def request_str(self) -> str:
        """``$self->{'_requeststr'}`` — ``join(',', @request)`` (:1063)."""
        return ",".join(self.verbs)

    def get_param(self, name: str) -> Any:
        """``$request->getParam($name)``."""
        for key, value in self.params:
            if key == name:
                return value
        return None

    def is_command(self, spec: Any) -> bool:
        """``$request->isCommand([['playlist'],['newsong','stop']])`` (:1758).

        ``spec`` is a sequence of groups; the n-th request verb must be in the
        n-th group (:2381-2395). Notifications are never queries, so this is
        the plain :meth:`matching_request`.
        """
        return self.matching_request(spec)

    def matching_request(self, spec: Any) -> bool:
        """``__matchingRequest`` (``Request.pm:2381-2395``)."""
        for position, names in enumerate(spec):
            if isinstance(names, str):
                names = (names,)
            if position >= len(self.verbs):
                return False
            if self.verbs[position] not in names:
                return False
        return True

    # -- Perl rendering ---------------------------------------------------
    def render_as_array(self) -> list[str]:
        """``renderAsArray`` (``Request.pm:2226-2296``) minus the client id."""
        elements: list[str] = list(self.verbs)
        for key, value in self.params:
            elements.extend(_param_tokens(key, value))
        return elements

    def render_line(self) -> str:
        """``cli_request_write`` (``Plugin/CLI/Plugin.pm:692-698``) — one line.

        ``Slim::Control::Stdio::array_to_string($request->clientid(),
        \\@elements)`` (:123-138): the client id in front when defined, every
        element ``uri_escape_utf8``-escaped, joined with a single space.
        """
        return render_line(clientid=self.client_id, terms=self.verbs,
                           params=self.params)


def _param_tokens(key: str, value: Any) -> list[str]:
    """``renderAsArray``'s parameter conventions (``Request.pm:2245-2255``)."""
    from lyrion.control.queries import result_tokens

    return result_tokens(key, value)


# ---------------------------------------------------------------------------
# The notification queue (Request.pm:852-862, :2005-2102)
# ---------------------------------------------------------------------------

_queue: deque[Notification] = deque()
_pump: Optional[asyncio.Task] = None
# ``%listeners`` (Request.pm:513-553): func -> (filter, func, client_id)
_listeners: dict[int, tuple[Any, Callable[[Notification], None], Optional[str]]] = {}
# ``%connections{...}{'subscribe'}`` of the CLI plugin (Plugin.pm:892-931)
_cli_connections: dict[str, "CliConnection"] = {}
_cli_listener_subscribed = False


@dataclass
class CliConnection:
    """A CLI connection that may ``listen`` (``Plugin/CLI/Plugin.pm:979-1017``)."""

    connection_id: str
    write: Callable[[str], Any]
    # ``$connections{$sock}{'subscribe'}{'listen'}``: '*' for everything, an
    # arrayref-of-arrayrefs for specific terms (Plugin.pm:892-897).
    listen: Any = None


# -- listener registry (Perl subscribe/unsubscribe, :788-834) ---------------

def subscribe(
    func: Callable[[Notification], None],
    requests: Any = None,
    client_id: Optional[str] = None,
) -> None:
    """``Slim::Control::Request::subscribe`` — register a notification listener."""
    _listeners[id(func)] = (requests, func, client_id)


def unsubscribe(
    func: Callable[[Notification], None],
    client_id: Optional[str] = None,
) -> None:
    """``Slim::Control::Request::unsubscribe``."""
    _listeners.pop(id(func), None)


def _call_listeners(notification: Notification) -> None:
    """The ``%listeners`` half of ``notify`` (``Request.pm:2013-2053``)."""
    for _filter, func, client_id in list(_listeners.values()):
        if client_id:
            # A client-specific listener only hears about its own client;
            # ``playlist newmetadata`` reaches the whole sync group
            # (Bug 10064, :2038-2045). ``$self->client() || next`` skips the
            # listener entirely when the notification's client is unknown.
            if not notification.client_id:
                continue
            if notification.is_command([["playlist", "newmetadata"]]):
                if _get_player(notification.client_id) is None:
                    continue
                if not _in_sync_group(notification.client_id, client_id):
                    continue
            elif notification.client_id != client_id:
                continue
        try:
            func(notification)
        except Exception as exc:  # noqa: BLE001 — Perl wraps this in eval {}
            logger.warning("Failed notify: %s", exc)


def _get_player(mac: str) -> Any:
    """``$request->client`` — the client object, or None when unknown."""
    try:
        from lyrion.player.manager import PlayerManager

        return PlayerManager().get_player(mac)
    except Exception:  # noqa: BLE001
        return None


def _in_sync_group(mac: str, client_id: str) -> bool:
    """Perl ``grep($_->id eq $clientid, $client->syncGroupActiveMembers())``.

    ``Client.pm:1373`` — ``syncGroupActiveMembers`` is the controller's
    ``activePlayers()``, which always contains the client itself (a single
    player is a group of one), plus the active members of its sync group.
    """
    if not mac or not client_id:
        return False
    if mac == client_id:
        return True
    try:
        from lyrion.player.manager import PlayerManager

        members = PlayerManager().get_sync_group(mac)
        return any(m.mac == client_id for m in members)
    except Exception:  # noqa: BLE001
        return False


# -- the CLI block of cli_subscribe_notification (Plugin.pm:969-1017) -------

def _cli_subscribe_notification(notification: Notification) -> None:
    """``Slim/Plugin/CLI/Plugin.pm:970-1017`` — write to every listening CLI."""
    for connection in list(_cli_connections.values()):
        if connection.listen is None:
            continue                                   # :982 unsubscribed
        # :994-995 — never echo a command twice to the connection that sent it
        if (notification.source == "CLI"
                and notification.connection_id == connection.connection_id):
            continue
        sent = True                                    # :998 everything
        if isinstance(connection.listen, list):         # :1001-1006
            sent = notification.is_command(connection.listen)
        if not sent:
            continue
        try:
            connection.write(notification.render_line())
        except Exception as exc:  # noqa: BLE001
            logger.debug("CLI notification write failed for %s: %s",
                         connection.connection_id, exc)


def _cli_subscribe_manage() -> None:
    """``cli_subscribe_manage`` (``Plugin/CLI/Plugin.pm:934-966``).

    The plugin subscribes its listener only while at least one connection has
    a ``listen`` term, and unsubscribes when the last one goes away.
    """
    global _cli_listener_subscribed
    needed = any(c.listen is not None for c in _cli_connections.values())
    if needed and not _cli_listener_subscribed:
        subscribe(_cli_subscribe_notification)
        _cli_listener_subscribed = True
    elif not needed and _cli_listener_subscribed:
        unsubscribe(_cli_subscribe_notification)
        _cli_listener_subscribed = False


# -- CLI connection registry ------------------------------------------------

def register_cli_connection(connection_id: str, write: Callable[[str], Any]) -> None:
    """Register a CLI connection (Perl: ``$connections{$client_socket}``)."""
    _cli_connections[connection_id] = CliConnection(connection_id=connection_id,
                                                    write=write)


def unregister_cli_connection(connection_id: str) -> None:
    """Forget a CLI connection and re-run ``cli_subscribe_manage``."""
    _cli_connections.pop(connection_id, None)
    _cli_subscribe_manage()


def cli_set_listen(connection_id: str, value: Any) -> None:
    """``cli_subscribe_terms_none``/``_all``/``_terms`` (Plugin.pm:899-930)."""
    connection = _cli_connections.get(connection_id)
    if connection is None:
        return
    connection.listen = value
    _cli_subscribe_manage()


def cli_get_listen(connection_id: str) -> Any:
    """``defined($connections{$sock}{'subscribe'}{'listen'}) || 0`` (:854)."""
    connection = _cli_connections.get(connection_id)
    if connection is None or connection.listen is None:
        return 0
    return 1


# -- subscriber re-execution (Request.pm:2055-2102, Queries.pm:3925-3990) ----

def status_query_filter(notification: Notification, subscriber_clientid: Optional[str]) -> float:
    """``statusQuery_filter`` (``Slim/Control/Queries.pm:3925-3990``).

    Perl's filter takes the *subscriber's* request as ``$self`` and the
    notification as ``$request``; our callers know the subscriber, so its
    ``clientid`` is passed in. The return value is Perl's: ``0`` = the
    subscription is not relevant, otherwise the delay in seconds (the timer in
    ``notify`` fires after ``relevant - 1``).
    """
    clientid = notification.client_id
    if not clientid:
        return 0
    myclientid = subscriber_clientid
    if not myclientid:
        return 0

    # Bug 10064: playlist notifications get sent to everyone in the sync-group.
    # ``isCommand([['playlist', 'newmetadata']])`` is a SINGLE verb group
    # (``Request.pm:2381-2395``: the first verb must be one of the names), so
    # every ``playlist …`` notification — including ``playlist newsong`` —
    # takes this branch, and the second condition is ``$request->client``: a
    # notification whose client is unknown to the server falls through to the
    # plain clientid comparison below.
    if notification.is_command([["playlist", "newmetadata"]]):
        client = _get_player(clientid)
        if client is None:
            if clientid != myclientid:
                return 0
        elif not _in_sync_group(clientid, myclientid):
            return 0
    elif notification.is_command([["sync"]]):
        pass                       # sync commands reach every client (:3941-3944)
    else:
        if clientid != myclientid:
            return 0

    # ignore most prefset commands, but e.g. alarmSnoozeSeconds needs a status
    if notification.is_command([["prefset", "playerpref"]]):
        prefname = notification.get_param("_prefname")
        if prefname not in ("alarmSnoozeSeconds", "digitalVolumeControl", "libraryId"):
            return 0

    # commands we ignore
    if notification.is_command([["ir", "button", "debug", "pref", "display"]]):
        return 0

    # the client is gone
    if notification.is_command([["client"], ["forget"]]):
        return 1

    if notification.is_command([["mixer"], ["muting"]]):
        return 1.4                 # bug 5255 — room for the fade to finish
    if notification.is_command([["playlist"], ["stop"]]):
        return 2.0
    if notification.is_command([["playlist"], ["open", "jump"]]):
        # quite likely about to be followed by a 'playlist newsong'
        return 2.5
    return 1.3                     # burst coalescing for every other notif


def _autoexecute(notification: Notification) -> None:
    """The ``%subscribers`` half of ``notify`` (:2055-2102) + ``__autoexecute``.

    Perl re-executes the subscriber's own request ``relevant - 1`` seconds
    later when its ``autoExecuteFilter`` says the notification matters. The
    port's status subscribers are the CLI ``status subscribe:<n>`` sessions
    (``CLIHandler.notify_subscribers``) and the Cometd/Jive controllers
    (``CometdManager.notify_player_status``) — both are woken for the client
    the notification is about.
    """
    clientid = notification.client_id
    if not clientid:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return                     # Perl's timers only exist with an idle loop
    relevant = status_query_filter(notification, clientid)
    if not relevant:
        return
    loop.call_later(max(0.0, float(relevant) - 1.0), _reexecute_status, clientid)


def _reexecute_status(clientid: str) -> None:
    try:
        from lyrion.control.cli import CLIHandler

        CLIHandler.notify_subscribers(clientid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("status re-execute (CLI) failed for %s: %s", clientid, exc)
    try:
        from lyrion.web.cometd import get_manager

        manager = get_manager()
        if manager is not None:
            asyncio.get_running_loop().create_task(
                manager.notify_player_status(clientid))
    except Exception as exc:  # noqa: BLE001
        logger.debug("status re-execute (cometd) failed for %s: %s", clientid, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def notify(notification: Notification) -> None:
    """``$request->notify()`` (``Slim/Control/Request.pm:2005-2102``)."""
    if not notification.verbs:
        return
    logger.debug("Notify: %s", notification.request_string)
    _call_listeners(notification)
    _autoexecute(notification)


def check_notifications() -> int:
    """``checkNotifications`` (:856-862) — send ONE queued notification.

    Returns 1 when a notification was sent, 0 when the queue was empty (Perl's
    caller uses that to decide whether to keep the idle loop busy).
    """
    if not _queue:
        return 0
    notify(_queue.popleft())
    return 1


async def _pump_queue() -> None:
    while _queue:
        check_notifications()
        await asyncio.sleep(0)


def notify_from_array(
    client_id: Optional[str],
    terms: list[Any],
    *,
    source: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> Notification:
    """``Slim::Control::Request::notifyFromArray($client, \\@request)`` (:838-853).

    The request is queued and sent by the idle loop; the port drains the queue
    as a task on the running loop, or synchronously when there is none (tests
    and tooling without an event loop).
    """
    notification = Notification.from_array(client_id, terms, source=source,
                                           connection_id=connection_id)
    logger.debug("notifyFromArray(%s)", notification.request_string)
    _queue.append(notification)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        check_notifications()
        return notification
    global _pump
    if _pump is None or _pump.done():
        _pump = loop.create_task(_pump_queue())
    return notification


def reset() -> None:
    """Drop queued notifications, listeners and CLI connections (tests)."""
    global _pump, _cli_listener_subscribed
    _queue.clear()
    _listeners.clear()
    _cli_connections.clear()
    _cli_listener_subscribed = False
    if _pump is not None and not _pump.done():
        _pump.cancel()
    _pump = None
