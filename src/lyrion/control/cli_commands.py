"""
Built-in CLI command implementations for Pyrion Music Server.

These are registered via the @register_command decorator in cli.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from lyrion.control.cli import (
    CLIContext,
    CLIHandler,
    ResponseFormat,
    register_command,
)
from lyrion.control.queries import (
    fulltext_weight,
    query_params,
    render_line,
    search_string_split,
    search_tokens,
)

if TYPE_CHECKING:
    from lyrion.control.request import RequestDispatcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Perl CLI text format (CTRL-02)
# ---------------------------------------------------------------------------
# Every answer is ONE percent-escaped line — request verbs first, then the
# echoed request parameters, then the results:
#
#   Slim/Plugin/CLI/Plugin.pm:692-698   renderAsArray() + array_to_string()
#   Slim/Control/Request.pm:2226-2296   the renderAsArray conventions
#   Slim/Control/Stdio.pm:123-138       client id + uri_escape_utf8 + ' '
#
# Rules used below (all in lyrion.control.queries.render_line):
#   * 'key:value' — no space after the colon (Request.pm:2291)
#   * ':' → '%3A', ' ' → '%20' (Stdio.pm:134)
#   * a '_'-prefixed result prints its value alone (Request.pm:2288-2290)
#   * a '*_loop' result is unrolled inline, one key:value run per item,
#     no loop name and no line break (Request.pm:2264-2281)
#   * a client-bound request (status, mixer, …) starts with the client id
#     (Stdio.pm:131); 'players'/'player count' have none (Request.pm:547/535)
#   * the request terms are echoed as sent, including the query marker '?'
#     when it is a surplus token (Request.pm:1049-1060) — see query_params()
#
# Live reference (Perl LMS 9.1.1, 192.168.1.90:9090, read-only, 2026-09-12):
#   ping                → ping
#   version ?           → version 9.1.1
#   ver 0 ?             → ver 0 %3F            (no 'ver' dispatch in Perl)
#   players 0 1         → players 0 1 count%3A4 playerindex%3A0 playerid%3A… uuid%3A … firmware%3Av1.0-1672-16
#   player count ?      → player count 4
#   mixer volume ?      → 24%3A0a%3Ac4%3A29%3A77%3A90 mixer volume 23
#   artists 0 1         → artists 0 1 id%3A5620 artist%3A%3F favorites_url%3A… count%3A11151


def _server_version() -> str:
    """The value Perl reports as '_version' (Queries.pm:4941-4943: $::VERSION)."""
    try:
        from lyrion import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "9.2.0"


def _player_field(player: Any, name: str, default: Any = None) -> Any:
    """Read one field from a PlayerState or a plain dict (dispatcher players)."""
    if isinstance(player, dict):
        return player.get(name, default)
    return getattr(player, name, default)


def _connected_player_count(players: list[Any]) -> int:
    """Perl's ``player count`` / ``players`` count: ``Client::clientCount()``."""
    return len(players)


def _is_query_echo(args: list[str]) -> bool:
    """True when a trailing '?' makes the request non-dispatchable in Perl.

    Perl only has an entry for a trailing '?' when the request was registered
    with a '?' parameter (``Slim/Control/Request.pm:1021-1039``).  'players'
    (:547), 'status' (:618), 'albums' (:479), 'artists' (:481), 'songs'
    (:617), 'titles' (:628) and 'genres' (:499) are not, so such a request
    matches no entry, gets status 104 and is echoed verbatim (:1093-1100 with
    ``Plugin/CLI/Plugin.pm:657-663``) — live Perl: ``players ?`` →
    ``players %3F``, ``status ?`` → ``status %3F``, ``players 0 ?`` →
    ``players 0 %3F``.  'version' (:630), 'player count' (:535) and
    'mixer <entity> ?' (:523-524) *are* registered with one.
    """
    return bool(args) and args[-1] == "?"


# Perl's request verbs that are dispatchable on their own — i.e. a request made
# of exactly that word matches a leaf in the dispatch tree
# (Slim/Control/Request.pm:474-637).  A word like 'mixer', 'player', 'playlist'
# or 'pref' is only a *prefix* there and stays non-dispatchable on its own:
# live Perl 9.1.1, read-only, 2026-09-12 answers ``can mixer ?`` → ``can mixer
# 0`` even though ``mixer volume ?`` exists.
_CAN_REQUESTS = frozenset({
    "abortscan", "albums", "alarms", "artists", "can", "displaystatus",
    "genres", "libraries", "playlists", "rescan", "rescanprogress", "roles",
    "search", "serverstatus", "songinfo", "songs", "syncgroups", "tags",
    "titles", "tracks", "works", "years",
})

# 'login', 'shutdown' and 'exit' do not go through the dispatch table and are
# therefore always available (Slim/Plugin/CLI/Plugin.pm:780-783).
_CAN_ALWAYS = frozenset({"login", "shutdown", "exit"})


def _echo(cmd: str, args: list[str], clientid: Optional[str] = None) -> list[str]:
    """One line for a request Perl does not dispatch (status 104).

    ``Slim/Plugin/CLI/Plugin.pm:657-663`` ("Request [$cmd] unknown or missing
    client -- will echo as is...") hands the request to ``cli_request_write``
    (:692-698); with no match in the dispatch table ``_request`` stays empty and
    every token becomes the positional parameter ``_p<i>``
    (``Slim/Control/Request.pm:1093-1100``), which ``renderAsArray`` prints
    *bare* (:2245-2253).  The answer is therefore the request verbatim.

    Live Perl 9.1.1 (192.168.1.90:9090, read-only, 2026-09-12): ``playercount``
    → ``playercount``, ``unsubscribe`` → ``unsubscribe``, ``logout`` →
    ``logout``, ``exit`` → ``exit``, ``volume 50`` → ``volume 50``,
    ``volume ?`` → ``volume %3F``, ``prev`` → ``prev``, ``display x y 5`` →
    ``24%3A0a%3Ac4%3A29%3A77%3A90 display x y 5``.
    """
    return [render_line(clientid=clientid, terms=[cmd, *args])]


def _command_line(
    verbs: list[str],
    args: list[str],
    declared: list[str],
    clientid: Optional[str] = None,
    has_tags: bool = False,
    results: Any = (),
) -> list[str]:
    """One line for a Perl command/query that echoes its declared parameters.

    Perl splits a request into the matched verbs and the parameters named by
    the dispatch entry (``Slim/Control/Request.pm:1021-1061``); a parameter
    starting with ``_`` is printed as its bare value (:2245-2253), a missing
    one still occupies its slot and renders as the empty string (:1026-1028).
    Live Perl: ``ir 123`` → ``<clientid> ir 123 `` (trailing space from the
    unset ``_time``, Request.pm:506), ``wipecache`` → ``wipecache `` (unset
    ``_queue``, Request.pm:631).
    """
    return [
        render_line(
            clientid=clientid,
            terms=list(verbs),
            params=query_params(list(args), list(declared), has_tags=has_tags),
            results=results,
        )
    ]


def _command_echo(
    verbs: list[str],
    args: list[str],
    declared: list[str],
    clientid: Optional[str] = None,
    has_tags: bool = True,
) -> list[str]:
    """Like :func:`_command_line` but without results (the echo case)."""
    return _command_line(verbs, args, declared, clientid, has_tags, ())


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@register_command("login")
async def cmd_login(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    login [<password>]
    Authenticate the CLI session. If server has no password set, any
    non-empty password is accepted.
    """
    if not args:
        return ["login: "]

    password = args[0]
    if handler.check_password(password):
        ctx.authenticated = True
        return ["login: 1"]
    else:
        ctx.authenticated = False
        return ["login: 0"]


@register_command("exit", "quit")
async def cmd_exit(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """exit — Perl echoes the request, then closes the connection.

    ``Slim/Plugin/CLI/Plugin.pm:618-619`` sets ``$exit = 1`` for 'exit' (and
    falls through to ``cli_request_write``, :665/:692-698), so the client
    receives ``exit`` before the socket is closed (live Perl 2026-09-12:
    ``exit`` → ``exit``).  'quit' has no Perl dispatch at all and is echoed
    verbatim (:657-663).  The network path breaks its read loop *before*
    dispatching (src/lyrion/control/cli_server.py:56-57), so this answer is
    only the protocol line; the connection is still closed by the caller.
    """
    return _echo(str(getattr(ctx, "command", "exit") or "exit"), args)


@register_command("ping")
async def cmd_ping(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """ping — connection liveness (SqueezePlay/controllers poll it).

    Perl has no 'ping' dispatch and echoes an unknown request back
    (Slim/Plugin/CLI/Plugin.pm:657-663 + Request.pm:1095-1100), so the Perl
    CLI answers exactly ``ping`` (live probe 2026-09-12 on :9090).
    """
    return [render_line(terms=["ping"])]


@register_command("client")
async def cmd_client(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """client <mac> <model> <name> [..] — register a controlling client.

    SqueezePlay logs in through this: it sends its client identity and the
    server acknowledges. Until it is recognised the controller can't reach
    the menus. We just acknowledge (the discovery + websocket paths carry
    the real data)."""
    return ["client: ok"]


@register_command("listen")
async def cmd_listen(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """listen [<on|off>] | listen ? — one escaped line.

    'listen' is registered twice by the CLI plugin itself
    (``Slim/Plugin/CLI/Plugin.pm:103-106``): as a command with ``_newvalue``
    (:0,0,0) and as a query (:0,1,0).  ``listenCommand`` adds no result
    (:799-828) → the answer is the echoed request; ``listenQuery`` adds the
    bare result ``_listen`` (:829-845, ``defined($connections{$sock}
    {'subscribe'}{'listen'}) || 0``).  Live Perl 2026-09-12: ``listen ?`` →
    ``listen 0``.

    The command itself subscribes this connection to the notification stream
    (:809-834):

    * no parameter            → toggle (``!defined $connections{..}{'subscribe'}``)
    * ``$param == 0``         → ``cli_subscribe_terms_none`` (:826-828) — a
      Perl numeric comparison, so ``listen off``/``listen on`` fall in here too
      ('on'/'off' numify to 0) and only a numeric non-zero subscribes
    * ``$param == 1``         → ``cli_subscribe_terms_all`` (:829-831), i.e.
      ``$connections{$sock}{'subscribe'}{'listen'} = '*'`` (:910-918)

    A listening connection then receives every notification line
    (``cli_subscribe_notification``, :970-1017), e.g.
    ``<clientid> playlist newsong <title> <index>``.
    """
    if _is_query_echo(args):
        from lyrion.control import notifications

        return _command_line(["listen"], [], [],
                             results=[("_listen",
                                       notifications.cli_get_listen(ctx.client_id))])

    param: Any = args[0] if args else None
    if param is None:
        # Perl: ``$param = !defined($connections{$client_socket}{'subscribe'})``
        param = 0 if ctx.listen is not None else 1
    # ``$param == 0`` / ``$param == 1`` — Perl's numeric comparison.
    if _perl_num(param) == 0:
        handler.set_listen(False)
    elif _perl_num(param) == 1:
        handler.set_listen(True)
    return _command_echo(["listen"], args, ["_newvalue"])


def _perl_num(value: Any) -> float:
    """Perl's numeric coercion of a string (leading number, else 0)."""
    import re

    match = re.match(r"\s*[-+]?\d*\.?\d+", str(value))
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:  # pragma: no cover — the regex guarantees a number
        return 0.0


# ---------------------------------------------------------------------------
# Server info
# ---------------------------------------------------------------------------


@register_command("version", "ver")
async def cmd_version(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """version [?] — return the server version ('ver' is our alias).

    Perl: ``addDispatch(['version','?'])`` (Request.pm:630), ``versionQuery``
    adds the result ``_version`` (Queries.pm:4941-4943) → ``version 9.1.1``.
    The trailing '?' occupies the declared parameter slot and is not echoed
    (Request.pm:1036), while anything in front of it is dropped and a further
    '?' becomes a surplus positional token → ``version 0 ?`` answers
    ``version %3F 9.1.1`` (live probe 2026-09-12, :9090).

    Perl has no 'ver' dispatch at all — unknown requests are echoed
    (Plugin/CLI/Plugin.pm:657-663 + Request.pm:1095-1100) → ``ver 0 ?``
    answers ``ver 0 %3F`` (live probe).  We keep the alias reachable but
    answer exactly like Perl does.
    """
    invoked = str(getattr(ctx, "command", "") or "version")
    if invoked != "version":
        return [render_line(terms=[invoked, *args])]
    if args and args[-1] == "?":
        return [
            render_line(
                terms=["version"],
                params=query_params(args, ["?"], has_tags=False),
                results=[("_version", _server_version())],
            )
        ]
    # No '?' terminator: Perl's query entry is not selected and the request
    # is echoed (Request.pm:1021-1024, 1095-1100).
    return [render_line(terms=["version", *args])]


@register_command("can", "capabilities")
async def cmd_can(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """can <request…> ? — is that request dispatchable? One escaped line.

    Perl's CLI plugin registers ``['can','_p1'…'_p5','?']`` as a query
    (``Slim/Plugin/CLI/Plugin.pm:101-102``) with ``[0,1,0]`` — no client, no
    tags.  ``canQuery`` (:753-796) drops every ``_p<i>`` that is unset or the
    literal ``'?'`` and adds the bare result ``_can``: ``1`` for
    login/shutdown/exit (they bypass the dispatch table, :780-783) or when the
    remaining tokens name a dispatchable request (:785-787), else ``0``.

    Live Perl 9.1.1, read-only, 2026-09-12: ``can`` → ``can`` (no '?', not a
    query, echoed), ``can ?`` → ``can 0``, ``can exit ?`` → ``can exit 1``,
    ``can mixer ?`` → ``can mixer 0``, ``can foobar ?`` → ``can foobar 0``.
    """
    if not args:
        # Without a trailing '?' the request does not select the query entry;
        # live Perl answers the bare request.
        return _echo("can", args)
    if not _is_query_echo(args):
        return _echo("can", args)

    wanted = [a for a in args if a != "?"]
    if not wanted:
        can = 0
    elif wanted[0] in _CAN_ALWAYS:
        can = 1
    else:
        can = 1 if wanted[0].lower() in _CAN_REQUESTS else 0
    return _command_line(["can"], wanted, [], results=[("_can", can)])


@register_command("serverstatus")
async def cmd_serverstatus(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """serverstatus [<index> <quantity>] — the server status as ONE CLI line.

    ``addDispatch(['serverstatus','_index','_quantity'],[0,1,1,serverstatusQuery])``
    (``Slim/Control/Request.pm:611``) — no client (hence no client id in front,
    Stdio.pm:131) but tags.  ``serverstatusQuery`` (Queries.pm:3705-3830) adds,
    in this order: ``rescan`` + ``progressname``/``progressdone``/
    ``progresstotal`` while a scan runs (…:3714-3720) or ``lastscan``
    (…:3727), ``version`` (:3752/:3755), ``uuid`` (:3772), ``ip`` (:3778),
    ``httpport`` (:3779), the five ``info total <entity>`` keys (:3785-3789),
    ``player count`` (:3820), the ``players_loop`` (:3826 → ``_addPlayersLoop``,
    Queries.pm:2615-2676) and ``other player count`` (:3833).

    Live Perl 9.1.1, read-only, 2026-09-12::

        serverstatus 0 5 rescan%3A1 progressname%3A… progresstotal%3A80529
        version%3A9.1.1 uuid%3A809f80c3-… ip%3A192.168.1.90 httpport%3A9000
        info%20total%20albums%3A7181 … player%20count%3A4 playerindex%3A0 …
    """
    try:
        from lyrion import __version__
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        players = list(pm.get_all_players())
    except Exception:  # noqa: BLE001
        __version__ = "9.2.0"
        pm = None
        players = []

    results: list[tuple[str, Any]] = []

    # Scan progress (Perl: only while scanning, else 'lastscan').
    scanning = False
    scan_progress = 0
    scan_total = 0
    try:
        from lyrion.media.scan_state import SCAN_STATE

        snap = SCAN_STATE.snapshot()
        scanning = bool(snap.get("scanning"))
        scan_progress = int(snap.get("progress", 0) or 0)
        scan_total = int(snap.get("total", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    if scanning:
        results.append(("rescan", 1))
        results.append(("progressname", "Rescanning"))
        results.append(("progressdone", scan_progress))
        results.append(("progresstotal", scan_total))

    results.append(("version", _server_version()))
    uuid = getattr(pm, "server_uuid", "") or "lyrion-local"
    results.append(("uuid", uuid))
    # Server address (Perl: Slim::Utils::Network::serverAddr()).
    import socket

    server_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        server_ip = s.getsockname()[0]
        s.close()
    except Exception:  # noqa: BLE001
        pass
    results.append(("ip", server_ip))
    results.append(("httpport", 9000))

    totals: dict[str, int] = {}
    try:
        rows = await _query_db(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration),0) AS d FROM tracks"
        )
        totals["albums"] = 0
        totals["artists"] = 0
        totals["genres"] = 0
        totals["songs"] = int(rows[0]["n"]) if rows else 0
        totals["duration"] = int(rows[0]["d"]) if rows else 0
        r_alb = await _query_db("SELECT COUNT(*) AS n FROM albums")
        totals["albums"] = int(r_alb[0]["n"]) if r_alb else 0
        r_art = await _query_db(
            "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
        )
        totals["artists"] = int(r_art[0]["n"]) if r_art else 0
        r_gen = await _query_db(
            "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre != ''"
        )
        totals["genres"] = int(r_gen[0]["n"]) if r_gen else 0
        # A scan that ran before adds 'lastscan' instead of the live progress.
        if not scanning:
            # Perl: Import->lastScanTime() aus metainformation
            # 'lastRescanTime' (Import.pm:290-300). Unser Schema hat keine
            # metainformation-Tabelle; die Spalte 'last_rescan' existiert
            # nicht (die Abfrage schlug bisher still fehl und die Zeile
            # fehlte) — jüngster Erfassungs-Zeitstempel der Titel, als
            # Unix-Epoch wie in Perl.
            r_scan = await _query_db(
                "SELECT strftime('%s', MAX(lastscanned)) AS t FROM tracks"
            )
            lastscan = (
                int(r_scan[0]["t"])
                if r_scan and r_scan[0]["t"]
                else 0
            )
            if lastscan:
                results.append(("lastscan", lastscan))
    except Exception:  # noqa: BLE001
        pass

    for entity in ("albums", "artists", "genres", "songs", "duration"):
        results.append((f"info total {entity}", totals.get(entity, 0)))

    results.append(("player count", _connected_player_count(players)))
    results.append(
        (
            "players_loop",
            [_players_loop_entry(i, p) for i, p in enumerate(players)],
        )
    )
    results.append(("other player count", 0))

    return [
        render_line(
            terms=["serverstatus"],
            params=query_params(args, ["_index", "_quantity"]),
            results=results,
        )
    ]


# ---------------------------------------------------------------------------
# Player management
# ---------------------------------------------------------------------------


def _players_loop_entry(index: int, player: Any) -> dict[str, Any]:
    """One ``players_loop`` item in Perl's field order.

    Perl's ``_addPlayersLoop`` (Slim/Control/Queries.pm:2615-2676) adds
    playerindex, playerid, uuid, ip (``ipport()`` = ip:port), name, seq_no
    (only when the client has one), model, modelname, power, isplaying,
    displaytype (omitted for the 'http' model), isplayer, canpoweroff,
    connected and firmware — in exactly that order (live probe 2026-09-12).
    """
    from lyrion.player.manager import display_type_for, model_name_for

    model = _player_field(player, "model", "") or "squeezebox"
    ip = _player_field(player, "ip", "") or ""
    port = _player_field(player, "port", 0) or 0

    entry: dict[str, Any] = {
        "playerindex": index,
        "playerid": _player_field(player, "mac", "") or _player_field(player, "playerid", ""),
        "uuid": _player_field(player, "uuid", "") or "",
        "ip": f"{ip}:{port}" if port else ip,
        "name": _player_field(player, "name", "") or "",
    }
    seq_no = _player_field(player, "seq_no")
    if seq_no is not None:
        entry["seq_no"] = seq_no
    entry["model"] = model
    entry["modelname"] = _player_field(player, "model_name", "") or model_name_for(model)
    entry["power"] = 1 if _player_field(player, "power", False) else 0
    entry["isplaying"] = 1 if _player_field(player, "mode", "stop") == "play" else 0
    displaytype = display_type_for(model)
    if displaytype is not None:
        entry["displaytype"] = displaytype
    entry["isplayer"] = 1 if _player_field(player, "is_player", True) else 0
    entry["canpoweroff"] = 1 if _player_field(player, "can_power_off", True) else 0
    entry["connected"] = 1 if _player_field(player, "connected", False) else 0
    entry["firmware"] = _player_field(player, "firmware", "") or ""
    return entry


@register_command("players")
async def cmd_players(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """players [<index> <quantity>] — list the players as one CLI line.

    Perl (playersQuery, Queries.pm:2581-2613 + _addPlayersLoop :2615-2676)
    echoes the request terms, then ``count`` (added *before* the loop, :2602)
    and unrolls the loop inline:

        players 0 1 count%3A4 playerindex%3A0 playerid%3A24%3A0a%3A… uuid%3A
        ip%3A192.168.1.154%3A52091 name%3ASchlafzimmer seq_no%3A0 … firmware%3A…

    'players' needs no client, so no client id is prefixed (Request.pm:547).
    A trailing '?' is a surplus token — the dispatch declares only _index and
    _quantity (:547) — so the request is not dispatchable and is echoed:
    ``players ?`` → ``players %3F`` (live probe 2026-09-12).
    """
    players: list[Any] = []
    try:
        from lyrion.player import PlayerManager

        players = list(PlayerManager().get_all_players())
    except Exception:  # noqa: BLE001
        players = []
    if not players:
        dispatcher = handler._dispatcher
        if dispatcher:
            players = await dispatcher.list_players()

    if _is_query_echo(args):
        # No '?' entry for 'players' (Request.pm:547) → Perl echoes the request
        # and renders no results at all (live: 'players 0 ?' → 'players 0 %3F').
        return [render_line(terms=["players", *args])]

    return [
        render_line(
            terms=["players"],
            params=query_params(args, ["_index", "_quantity"]),
            results=[
                ("count", _connected_player_count(players)),
                ("players_loop", [_players_loop_entry(i, p) for i, p in enumerate(players)]),
            ],
        )
    ]


@register_command("playercount")
async def cmd_playercount(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playercount — Perl has no such request, so it is echoed verbatim.

    No entry in the dispatch table (``Slim/Control/Request.pm:474-637``)
    → status 104 → the request is echoed (Plugin/CLI/Plugin.pm:657-663,
    Request.pm:1093-1100).  Live Perl 2026-09-12: ``playercount`` →
    ``playercount``.  The count comes from ``player count ?``
    (Queries.pm:2519-2531 → ``player count 4``).
    """
    return _echo("playercount", args)


@register_command("player")
async def cmd_player(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """player count ? | player id|name|model [<id> ?] | player <playerid>

    Perl's ``playerXQuery`` returns the entity value as a bare result
    (``addResult("_count", clientCount())`` Queries.pm:2529; ``"_$entity"``
    :2557-2573), and the '?' is the declared query parameter that is *not*
    echoed (Request.pm:1036) while a value in front of it is a surplus
    positional token that is (Request.pm:1049-1060):

        player count ?              → player count 4
        player id ?                 → player id %3F 24%3A0a%3Ac4%3A29%3A77%3A90
        player name 24:0a:c4:…      → player name 24%3A0a%3Ac4%3A…   (echo)

    (live probes 2026-09-12 on :9090).  A bare ``player <playerid>`` has no
    Perl dispatch either (Request.pm:534-543) — it is echoed here as well,
    but still binds the session player.
    """
    if not args:
        return [render_line(terms=["player"])]

    sub = str(args[0]).lower()

    if sub == "count":
        try:
            from lyrion.player import PlayerManager

            count = PlayerManager().get_player_count()
        except Exception:  # noqa: BLE001
            count = 0
        return [
            render_line(
                terms=["player", "count"],
                params=query_params(args[1:], ["?"], has_tags=False),
                results=[("_count", count)],
            )
        ]

    if sub in ("id", "name", "model"):
        try:
            from lyrion.player import PlayerManager

            pid = args[1] if len(args) > 1 and args[1] != "?" else ctx.player_id
            player = PlayerManager().get_player(pid) if pid else None
            if player is None:
                # Perl signals an unresolvable player with an 'error' result
                # (statusQuery, Queries.pm:4020).
                return [
                    render_line(
                        terms=["player", sub],
                        params=query_params(args[1:], ["_IDorIndex", "?"], has_tags=False),
                        results=[("error", "invalid player")],
                    )
                ]
            value = {"id": player.mac, "name": player.name or "", "model": player.model or ""}[sub]
            return [
                render_line(
                    terms=["player", sub],
                    params=query_params(args[1:], ["_IDorIndex", "?"], has_tags=False),
                    results=[(f"_{sub}", value)],
                )
            ]
        except Exception as exc:  # noqa: BLE001
            return [
                render_line(
                    terms=["player", sub, *args[1:]],
                    results=[("error", str(exc))],
                )
            ]

    ctx.player_id = args[0]
    return [render_line(terms=["player", *args])]


@register_command("name")
async def cmd_name(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """name [<new name>] | name ? — one escaped line, client id in front.

    ``nameQuery`` adds the bare result ``_value`` (``Slim/Control/Queries.pm:
    2496-2512``, ``addDispatch(['name','?'])`` Request.pm:530) while the setter
    ``nameCommand`` adds nothing at all (Commands.pm:668-694), so both forms
    answer ``<clientid> name <value>``; a bare ``0`` means "no rename"
    (Commands.pm:685).  Live Perl 2026-09-12: ``name ?`` →
    ``24%3A0a%3Ac4%3A29%3A77%3A90 name Schlafzimmer``.
    """
    if not ctx.player_id:
        return [render_line(terms=["name", *args])]
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return [render_line(terms=["name", *args])]
        if args and str(args[0]) != "?":
            new_name = " ".join(str(a) for a in args)
            if new_name != "0":
                pm.rename_player(player.mac, new_name)
            return _command_line(["name"], args, ["_newvalue"],
                                 clientid=ctx.player_id)
        return _command_line(["name"], [a for a in args if a != "?"], [],
                             clientid=ctx.player_id,
                             results=[("_value", player.name or "")])
    except Exception as exc:  # noqa: BLE001
        return _echo("name", args, clientid=ctx.player_id)


@register_command("sync")
async def cmd_sync(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """sync <playerid> | sync ? — one escaped line, client id in front.

    ``syncQuery`` adds the bare result ``_sync``: the comma-joined sync buddies
    or ``'-'`` when the player is not synced (``Slim/Control/Queries.pm:
    4682-4706``, ``addDispatch(['sync','?'])`` Request.pm:621).  The command
    entry ``['sync','_indexid-']`` (:622) adds nothing → the request is echoed.
    Live Perl 2026-09-12: ``sync ?`` →
    ``24%3A0a%3Ac4%3A29%3A77%3A90 sync 00%3A04%3A20%3A2b%3A88%3Ac8``.
    """
    if _is_query_echo(args):
        buddies: list[str] = []
        try:
            from lyrion.player import PlayerManager

            player = PlayerManager().get_player(ctx.player_id) if ctx.player_id else None
            if player is not None:
                buddies = [
                    str(m) for m in (getattr(player, "sync_slaves", None) or [])
                ]
                if getattr(player, "sync_master", None):
                    buddies = [str(player.sync_master), *buddies]
        except Exception:  # noqa: BLE001
            buddies = []
        return _command_line(
            ["sync"], [], [], clientid=ctx.player_id,
            results=[("_sync", ",".join(buddies) if buddies else "-")],
        )
    if len(args) >= 1:
        try:
            from lyrion.player import PlayerManager

            pm = PlayerManager()
            # Perl's sync master is the request's client; the parameter names
            # the buddy (Slim/Control/Commands.pm syncCommand:12).
            if ctx.player_id:
                pm.sync_players(ctx.player_id, [args[0]])
            elif len(args) >= 2:
                pm.sync_players(args[0], [args[1]])
        except Exception:  # noqa: BLE001
            pass
    return _command_echo(["sync"], args, ["_indexid-"], clientid=ctx.player_id)


@register_command("unsync")
async def cmd_unsync(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """unsync <playerMac> — Perl has no 'unsync' request; it is echoed.

    There is no entry in the dispatch table (``Slim/Control/Request.pm:
    474-637``) and no CLI plugin registration, so the request is echoed
    (Plugin/CLI/Plugin.pm:657-663, Request.pm:1093-1100).  Live Perl
    2026-09-12: ``unsync`` → ``unsync``.
    """
    target = args[0] if args else ctx.player_id
    if target:
        try:
            from lyrion.player import PlayerManager

            PlayerManager().unsync_player(target)
        except Exception:  # noqa: BLE001
            pass
    return _echo("unsync", args)


@register_command("syncgroups")
async def cmd_syncgroups(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """syncgroups ? — one escaped line, loop unrolled inline.

    ``syncGroupsQuery`` adds the ``syncgroups_loop`` with exactly two keys per
    group: ``sync_members`` (master id + its buddies, comma-joined) and
    ``sync_member_names`` (the same players' names)
    (``Slim/Control/Queries.pm:4708-4746``, ``addDispatch(['syncgroups','?'])``
    Request.pm:623 — no client).  Live Perl 2026-09-12::

        syncgroups sync_members%3A24%3A0a%3Ac4%3A29%3A77%3A90%2C00%3A04%3A20%2A
        sync_member_names%3ASchlafzimmer%2CSqueezebox%20Radio
    """
    if not _is_query_echo(args):
        return _echo("syncgroups", args)
    loop: list[dict[str, Any]] = []
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        players = list(pm.get_all_players())
        groups: dict[str, list[Any]] = {}
        for p in players:
            master = getattr(p, "sync_master", None)
            if master:
                groups.setdefault(str(master), []).append(p)
        by_mac = {str(getattr(p, "mac", "")): p for p in players}
        for master_mac, slaves in groups.items():
            master = by_mac.get(master_mac)
            members = [master_mac, *[str(getattr(s, "mac", "")) for s in slaves]]
            names = [
                str(getattr(master, "name", "") or "") if master else "",
                *[str(getattr(s, "name", "") or "") for s in slaves],
            ]
            loop.append({
                "sync_members": ",".join(members),
                "sync_member_names": ",".join(names),
            })
    except Exception:  # noqa: BLE001
        loop = []
    return _command_line(["syncgroups"], [], [], results=[("syncgroups_loop", loop)])


@register_command("songinfo")
async def cmd_songinfo(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """songinfo <start> <count> track_id:<id> — ONE line, ``songinfo_loop``.

    ``songinfoQuery`` (``Slim/Control/Queries.pm:4603-4680``) requires
    ``track_id`` or ``url`` and *without* either sets status bad-params
    (:4627-4629) — the request is then still echoed (Plugin/CLI/Plugin.pm:665),
    live Perl 2026-09-12: ``songinfo 0 1`` → ``songinfo 0 1``, ``songinfo ?`` →
    ``songinfo %3F``.  With a track the answer is the ``songinfo_loop``
    (:4645-4670) unrolled inline (Request.pm:2264-2281); there is **no** count
    result.  Live Perl: ``songinfo 0 20 track_id:6417`` →
    ``songinfo 0 20 track_id%3A6417 rescan%3A1`` (the scan flag, :4635-4637).
    """
    if _is_query_echo(args):
        return _echo("songinfo", args)
    offset, limit, filters = _parse_query_args(args)
    tid = filters.get("track_id")
    if not tid or not str(tid).isdigit():
        # Perl: missing track_id/url → bad params → the request is echoed.
        return _command_line(["songinfo"], args, ["_index", "_quantity"])
    rows = await _query_db(
        "SELECT t.id, t.title, t.url, t.duration, t.year, t.tracknum, t.genre, "
        "t.filesize, t.samplerate, t.bitspersample AS samplesize, t.channels, "
        "t.content_type AS ctype, t.modtime, t.remote AS lossless "
        "FROM tracks t WHERE t.id = ? LIMIT 1",
        (int(tid),),
    )
    if not rows:
        return _command_line(["songinfo"], args, ["_index", "_quantity"])
    r = rows[0]

    import os as _os

    fields: list[tuple] = [
        ("id", r["id"]),
        ("title", r["title"] or ""),
    ]
    if r["duration"]:
        try:
            fields.append(("duration", round(float(r["duration"]), 3)))
        except Exception:
            pass
    if r["url"]:
        fields.append(("url", r["url"]))
    # artist / artist_id
    rows_a = await _query_db(
        "SELECT c.id, c.name FROM contributors c JOIN tracks_contributors tc "
        "ON tc.contributor = c.id AND tc.role = 1 WHERE tc.track = ? "
        "ORDER BY c.name LIMIT 1",
        (r["id"],),
    )
    if rows_a and rows_a[0]["name"]:
        fields.append(("artist", rows_a[0]["name"]))
        fields.append(("artist_id", str(rows_a[0]["id"])))
    # album / album_id
    rows_al = await _query_db(
        "SELECT al.id, al.title FROM albums al JOIN tracks_albums ta "
        "ON ta.album = al.id WHERE ta.track = ? LIMIT 1",
        (r["id"],),
    )
    if rows_al and rows_al[0]["title"]:
        fields.append(("album", rows_al[0]["title"]))
        fields.append(("album_id", str(rows_al[0]["id"])))
        fields.append(("compilation", "0"))
    if r["genre"]:
        fields.append(("genre", r["genre"]))
        rows_g = await _query_db(
            "SELECT id FROM genres WHERE name = ? LIMIT 1", (r["genre"],))
        if rows_g:
            fields.append(("genre_id", str(rows_g[0]["id"])))
    if r["year"]:
        fields.append(("year", str(r["year"])))
    if r["tracknum"]:
        fields.append(("tracknum", str(r["tracknum"])))
    # File-derived fields (Perl parity: filesize/type/bitrate/samplerate/…)
    fsize = r["filesize"]
    if not fsize and r["url"]:
        try:
            from urllib.parse import unquote, urlparse
            p = urlparse(r["url"]).path
            fsize = _os.path.getsize(unquote(p)) if p.startswith("/") else ""
        except OSError:
            fsize = ""
    if fsize:
        fields.append(("filesize", str(fsize)))
    ctype = r["ctype"] or ""
    type_code = {
        "audio/flac": "flc", "audio/x-flac": "flc",
        "audio/mpeg": "mp3", "audio/mp3": "mp3",
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/aiff": "aif",
        "audio/ogg": "ogg", "audio/aac": "aac", "audio/mp4": "m4a",
    }.get(ctype.lower(), "")
    if type_code:
        fields.append(("type", type_code))
    if r["samplerate"]:
        fields.append(("samplerate", str(int(r["samplerate"]))))
    if r["samplesize"]:
        fields.append(("samplesize", str(int(r["samplesize"]))))
    if r["channels"]:
        fields.append(("channels", str(int(r["channels"]))))
    lossless = r["lossless"]
    if type_code in ("flc", "wav", "aif"):
        lossless = 1
    elif type_code:
        lossless = 0
    if lossless is not None and lossless != "":
        fields.append(("lossless", "1" if lossless else "0"))
    fields.append(("remote", "0"))
    if r["modtime"]:
        fields.append(("modificationTime", r["modtime"]))
        fields.append(("addedTime", r["modtime"]))
        fields.append(("lastUpdated", r["modtime"]))
    fields.append(("work", ""))
    fields.append(("artwork_url", "0"))

    # ONE line: the loop items are unrolled inline, no per-field line
    # (Slim/Control/Request.pm:2264-2281; Plugin/CLI/Plugin.pm:692-698).
    return [
        render_line(
            terms=["songinfo"],
            params=query_params(args, ["_index", "_quantity"]),
            results=[("songinfo_loop", [dict(fields)])],
        )
    ]


@register_command("years")
async def cmd_years(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """years [<index> <quantity>] — ONE line, ``years_loop`` then ``count``.

    ``yearsQuery`` (``Slim/Control/Queries.pm:4949-5060``) iterates the
    distinct years and adds ``year`` + ``favorites_url`` per item
    (``db:year.id=<year>``) into ``years_loop``; the ``count`` result is added
    *after* the loop.  Live Perl 9.1.1, read-only, 2026-09-12: ``years 0 1`` →
    ``years 0 1 year%3A0 favorites_url%3Adb%3Ayear.id%3D0 count%3A65``.
    """
    if _is_query_echo(args):
        return _echo("years", args)
    offset, limit, _ = _parse_query_args(args)
    rows = await _query_db(
        "SELECT DISTINCT year AS y FROM tracks WHERE year > 0 "
        "ORDER BY year DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    total = await _query_db(
        "SELECT COUNT(DISTINCT year) AS n FROM tracks WHERE year > 0"
    )
    total_n = total[0]["n"] if total else 0
    loop = [
        {"year": r["y"], "favorites_url": f"db:year.id={r['y']}"}
        for r in rows
    ]
    return _command_line(
        ["years"], args, ["_index", "_quantity"],
        results=[("years_loop", loop), ("count", total_n)],
    )


@register_command("musicfolder")
async def cmd_musicfolder(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """musicfolder [<index> <quantity>] [folder_id:<path>] — ONE line.

    ``musicfolderQuery`` is a thin wrapper around ``mediafolderQuery``
    (``Slim/Control/Queries.pm:2165-2167`` → :2169-2507) which fills the
    ``folder_loop`` with ``id``, ``filename`` and ``type`` (``'folder'`` for a
    directory, :2472-2487) and adds ``count`` last (:2507).  Live Perl 9.1.1,
    read-only, 2026-09-12: ``musicfolder 0 1`` → ``musicfolder 0 1 id%3A81408
    filename%3A6MzM6F.Fetenhits_Rock_Classics_Best_Of-3CD-2020-NoGroup.nfo
    type%3Afolder count%3A303``.

    Datenquelle ist dieselbe wie im JSON-Pfad
    (``lyrion.media.folders.musicfolder_result``, von ``web/api.py``
    ``_json_musicfolder`` gerufen): media dirs (``Slim/Utils/Misc.pm:727-756``)
    → ``readDirectory`` (Misc.pm:973-1043). Die alte Ableitung aus
    ``tracks.url`` lieferte ``count=1``.
    """
    if _is_query_echo(args):
        return _echo("musicfolder", args)
    offset, limit, filters = _parse_query_args(args)
    from lyrion.media.folders import musicfolder_result

    folder_id = filters.get("folder_id", "") or None
    url = filters.get("url", "") or None
    res = musicfolder_result(offset, limit, folder_id=folder_id, url=url)
    loop = res.get("folder_loop") or []
    count = int(res.get("count", 0))
    return _command_line(
        ["musicfolder"], args, ["_index", "_quantity"],
        results=[("folder_loop", loop), ("count", count)],
    )


@register_command("rescanprogress")
async def cmd_rescanprogress(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """rescanprogress — ONE line: ``rescanprogress rescan:<0|1> …``.

    ``rescanprogressQuery`` is registered as a *query* with ``[0,1,1]`` and no
    parameter (``Slim/Control/Request.pm:608``); it adds the result ``rescan``
    (``Slim/Control/Queries.pm:3231-3360``) — ``1`` plus ``fullname``/per-step
    percentages/``steps``/``totaltime`` while a scan runs, else ``0``
    (:3355-3357).  Live Perl 9.1.1, read-only, 2026-09-12 (idle):
    ``rescanprogress`` → ``rescanprogress rescan%3A0``.

    The shape lives in :mod:`lyrion.control.rescan` (shared with the JSON-RPC
    branch in ``lyrion.web.api``), including Perl's ``lastscanfailed`` for a
    leftover failure row (:3291-3296, ``_scanFailed`` :6247-6258).
    """
    from lyrion.control import rescan as scan

    return _command_line(["rescanprogress"], [], [],
                         results=scan.progress_results())


@register_command("abortscan")
async def cmd_abortscan(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """abortscan — Perl answers the echoed request (no result at all).

    ``addDispatch(['abortscan'],[0,0,0,abortScanCommand])``
    (``Slim/Control/Request.pm:474``); ``abortScanCommand`` only calls
    ``Slim::Music::Import->abortScan`` and ``setStatusDone`` — it adds no
    result, so ``renderAsArray`` prints just the request verb
    (Plugin/CLI/Plugin.pm:692-698).  Live Perl 2026-09-12: ``abortscan`` →
    ``abortscan``.

    The flag has to reach the scan *process* (``Slim/Control/Commands.pm:46-49``
    → ``Slim/Music/Import.pm:257-270``), so it goes through
    :func:`lyrion.control.rescan.request_abort`.
    """
    try:
        from lyrion.control.rescan import request_abort

        request_abort()
    except Exception:  # noqa: BLE001
        pass
    return _echo("abortscan", args)


@register_command("menu")
async def cmd_menu(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """menu [<index> <quantity>] [direct:1] — ONE line, ``item_loop`` inline.

    Jive registers ``['menu','_index','_quantity']`` as a query
    (``Slim/Control/Jive.pm:57``).  ``menuQuery`` (:Jive.pm menuQuery) only
    fills results for a *disconnected* client or with ``direct`` set — then
    ``setRawResults({count, offset, item_loop})``; otherwise it adds nothing
    and the request is echoed.  Live Perl 9.1.1, read-only, 2026-09-12:
    ``menu 0 5`` → ``24%3A0a%3Ac4%3A29%3A77%3A90 menu 0 5`` (no results).

    We always expose the items (our home menu) as ``item_loop`` entries with
    ``text``/``browse``; the loop is unrolled inline like every other loop
    (``Slim/Control/Request.pm:2264-2281``).
    """
    try:
        from lyrion.web.api import JSONRPCAPI

        res = await JSONRPCAPI()._slim_request(ctx.player_id or "", ["menu"] + list(args))
        loop = res.get("item_loop")
        if loop is None:
            home = JSONRPCAPI()._home_menu()
            start = int(args[0]) if args and str(args[0]).isdigit() else 0
            count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else len(home)
            loop = home[start:start + count]
        total = res.get("count")
        if total is None:
            total = len(loop)
        items: list[dict[str, Any]] = []
        for item in loop:
            text = item.get("text") or item.get("name", "")
            browse_id = (
                item.get("browse", {}).get("id", "")
                if isinstance(item.get("browse"), dict)
                else item.get("browse", "")
            )
            items.append({
                "text": text,
                "node": item.get("node") or item.get("id", ""),
                "browse": browse_id,
            })
        return _command_line(
            ["menu"], args, ["_index", "_quantity"],
            clientid=ctx.player_id,
            results=[("count", total), ("offset", 0), ("item_loop", items)],
        )
    except Exception as exc:  # noqa: BLE001
        # Perl echoes a request it cannot dispatch (Plugin/CLI/Plugin.pm:657-663).
        return _echo("menu", args, clientid=ctx.player_id)


@register_command("playerstatus")
async def cmd_playerstatus(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playerstatus [<playerId>] — full player status (same as status)."""
    # The CLI dispatch already set ctx.player_id from the leading token
    # ('<mac> playerstatus ...'). args[0] is usually '-' (the "any/current
    # player" placeholder) — overwriting the id with it makes the lookup
    # fail with 'player not found', which is exactly what SqueezePlay's
    # SlimPlayer sees, so playlistSize never updates and the Now-Playing
    # window never opens. Only take a real id from args.
    if args and args[0] not in ("-", ""):
        ctx.player_id = args[0]
    return await cmd_status(handler, ctx, [])


@register_command("displaystatus")
async def cmd_displaystatus(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """displaystatus [subscribe:<mode>] — ONE line, client id in front.

    ``addDispatch(['displaystatus'],[1,1,1,displaystatusQuery])``
    (``Slim/Control/Request.pm:495``).  ``displaystatusQuery`` (Queries.pm:
    1630-…) only adds results while a display notification is being tracked
    (``$request->privateData``) or when a subscription is set up; for a plain
    request it adds nothing, so the answer is the client id plus the request
    verb.  Live Perl 9.1.1, read-only, 2026-09-12: ``displaystatus`` →
    ``24%3A0a%3Ac4%3A29%3A77%3A90 displaystatus``.
    """
    return _command_line(
        ["displaystatus"], [], [], clientid=ctx.player_id
    )


# ---------------------------------------------------------------------------
# Playback control
# ---------------------------------------------------------------------------


@register_command("play")
async def cmd_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """play [<trackId>] — start playback; the answer is ONE escaped line.

    Perl registers ``['play','_fadein']`` as the *command* entry
    (``Slim/Control/Request.pm:533``) and only a command (no '?' query), so
    ``playcontrolCommand`` adds no result and the request is echoed with the
    client id in front (Plugin/CLI/Plugin.pm:692-698; live Perl 2026-09-12:
    ``play ?`` → ``play %3F`` — command-only entries answer the raw echo).
    """
    if _is_query_echo(args):
        return _echo("play", args)
    if not ctx.player_id:
        return _command_echo(["play"], args, ["_fadein"])
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        if args:
            await pm.play_track(ctx.player_id, int(args[0]))
            return _command_echo(["play"], args, ["_fadein"],
                                 clientid=ctx.player_id)
        # No track id — resume whatever is selected
        player = pm.get_player(ctx.player_id)
        if player is not None:
            # Paused -> resume in place (LMS play button behaviour)
            if player.mode == "pause":
                await pm.pause_player(ctx.player_id, False)
            # Radio stream (current_track_id None, current_url set)
            elif player.current_track_id is None and getattr(player, "current_url", None):
                await pm.play_url(ctx.player_id, str(player.current_url),
                                  getattr(player, "current_title", "") or "")
            elif player.current_track_id is not None:
                await pm.play_track(ctx.player_id, player.current_track_id)
            # Fallback: resume the current playlist entry
            elif player.playlist and 0 <= player.playlist_position < len(player.playlist):
                entry = player.playlist[player.playlist_position]
                if isinstance(entry, str):
                    await pm.play_url(ctx.player_id, entry, "")
                else:
                    await pm.play_track(ctx.player_id, entry)
        return _command_echo(["play"], args, ["_fadein"], clientid=ctx.player_id)
    except Exception:  # noqa: BLE001
        return _command_echo(["play"], args, ["_fadein"], clientid=ctx.player_id)


@register_command("pause")
async def cmd_pause(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """pause [0|1] — toggle/force pause; the answer is ONE escaped line.

    ``['pause','_newvalue','_fadein','_suppressShowBriefly']`` is a command
    entry (``Slim/Control/Request.pm:532``) whose handler adds no result
    (Commands.pm playcontrolCommand), so the request is echoed — with the
    client id because it needs one (:0 needsClient flag is 1).  Live Perl
    2026-09-12: ``pause ?`` → ``pause %3F`` (command-only entry ⇒ raw echo).
    """
    if _is_query_echo(args):
        return _echo("pause", args)
    if not ctx.player_id:
        return _command_echo(["pause"], args,
                             ["_newvalue", "_fadein", "_suppressShowBriefly"])
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        if args and args[0] == "0":
            await pm.pause_player(ctx.player_id, False)
        elif args and args[0] == "1":
            await pm.pause_player(ctx.player_id, True)
        else:
            player = pm.get_player(ctx.player_id)
            currently_paused = player is not None and player.mode == "pause"
            await pm.pause_player(ctx.player_id, not currently_paused)
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["pause"], args,
                         ["_newvalue", "_fadein", "_suppressShowBriefly"],
                         clientid=ctx.player_id)


@register_command("stop")
async def cmd_stop(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """stop — stop playback; the answer is ONE escaped line.

    ``addDispatch(['stop'],[1,0,0,playcontrolCommand])``
    (``Slim/Control/Request.pm:619``) declares no parameter and adds no
    result → ``<clientid> stop``.
    """
    if not ctx.player_id:
        return _echo("stop", args)
    try:
        from lyrion.player import PlayerManager

        await PlayerManager().stop_player(ctx.player_id)
    except Exception:  # noqa: BLE001
        pass
    return _command_line(["stop"], [], [], clientid=ctx.player_id)


@register_command("prev")
async def cmd_prev(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """prev — Perl has no 'prev' request, so it is echoed verbatim.

    No entry in the dispatch table (``Slim/Control/Request.pm:474-637``)
    → status 104 → echo (Plugin/CLI/Plugin.pm:657-663).  Live Perl 2026-09-12:
    ``prev`` → ``prev``.  LMS controllers step the playlist with
    ``playlist index -1`` / ``playlist jump`` instead (Request.pm:561-568).
    """
    if not ctx.player_id:
        return _echo("prev", args)
    try:
        from lyrion.player import PlayerManager

        await PlayerManager().playlist_prev(ctx.player_id)
    except Exception:  # noqa: BLE001
        pass
    return _echo("prev", args)


@register_command("next")
async def cmd_next(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """next — Perl has no 'next' request, so it is echoed verbatim.

    Same as 'prev': no dispatch entry (``Slim/Control/Request.pm:474-637``)
    → echo (Plugin/CLI/Plugin.pm:657-663).  Live Perl 2026-09-12: ``next`` →
    ``next``.
    """
    if not ctx.player_id:
        return _echo("next", args)
    try:
        from lyrion.player import PlayerManager

        await PlayerManager().playlist_next(ctx.player_id)
    except Exception:  # noqa: BLE001
        pass
    return _echo("next", args)


@register_command("power")
async def cmd_power(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """power [0|1] — ONE line: ``<clientid> power 1`` / ``power 0`` / echo.

    ``powerQuery`` adds the bare result ``_power`` (``Slim/Control/Queries.pm:
    2991-3006``; ``addDispatch(['power','?'])`` Request.pm:599), the setter
    ``['power','_newvalue','_noplay']`` (:600) adds nothing → the request is
    echoed.  Live Perl 9.1.1, read-only, 2026-09-12: ``power ?`` →
    ``24%3A0a%3Ac4%3A29%3A77%3A90 power 1``.
    """
    if _is_query_echo(args):
        if not ctx.player_id:
            return _echo("power", args)
        try:
            from lyrion.player import PlayerManager

            player = PlayerManager().get_player(ctx.player_id)
            power = 1 if (player is not None and player.power) else 0
        except Exception:  # noqa: BLE001
            power = 0
        return _command_line(["power"], [], [], clientid=ctx.player_id,
                             results=[("_power", power)])
    if not ctx.player_id:
        return _command_echo(["power"], args, ["_newvalue", "_noplay"], has_tags=True)
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is not None and args and str(args[0]) in ("0", "1"):
            on = str(args[0]) == "1"
            player.power = on
            if not on:
                await pm.stop_player(ctx.player_id)
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["power"], args, ["_newvalue", "_noplay"],
                         clientid=ctx.player_id)


# ---------------------------------------------------------------------------
# Volume / Mixer
# ---------------------------------------------------------------------------


@register_command("volume")
async def cmd_volume(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """volume [<0-100>] — Perl has no 'volume' request; it is echoed.

    The mixer is addressed as ``mixer volume`` (``Slim/Control/Request.pm:
    523-524``); there is no bare 'volume' dispatch entry (:474-637), so the
    request is echoed (Plugin/CLI/Plugin.pm:657-663).  Live Perl 9.1.1,
    read-only, 2026-09-12: ``volume 50`` → ``volume 50``, ``volume ?`` →
    ``volume %3F``.  We still apply the volume so telnet users keep working.
    """
    if ctx.player_id and args and str(args[0]).replace(".", "").isdigit():
        try:
            from lyrion.player import PlayerManager

            await PlayerManager().set_volume(ctx.player_id, int(float(str(args[0]))))
        except Exception:  # noqa: BLE001
            pass
    # Live Perl 2026-09-12: 'volume 50' → 'volume 50', 'volume ?' → 'volume %3F'
    # — no client id, the request is not dispatchable.
    return _echo("volume", args)


@register_command("mixer")
async def cmd_mixer(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """mixer <parameter> [value] — one escaped line, client id in front.

    Perl answers ``<clientid> mixer volume 23`` (live probe 2026-09-12):
    ``mixerQuery`` returns the value as a bare ``"_$entity"`` result
    (Queries.pm:2127-2137), the '?' is the declared query parameter and is not
    echoed (Request.pm:523/:1036), and ``mixer <entity> <value>`` echoes the
    value through the command entry's ``_newvalue`` (Request.pm:524).  The
    client id is the resolved player (Stdio.pm:131).
    """
    if not ctx.player_id:
        return _echo("mixer", args)
    try:
        from lyrion.player.manager import PlayerManager

        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return [
                render_line(
                    clientid=ctx.player_id,
                    terms=["mixer", *args],
                    results=[("error", "invalid player")],
                )
            ]

        entity = str(args[0]).lower() if args else ""

        if entity == "volume":
            if len(args) > 1 and str(args[1]).isdigit():
                ok = await pm.set_volume(ctx.player_id, int(str(args[1])))
                if not ok:
                    return [
                        render_line(
                            clientid=ctx.player_id,
                            terms=["mixer", "volume"],
                            results=[("error", "could not set volume")],
                        )
                    ]
                return [
                    render_line(
                        clientid=ctx.player_id,
                        terms=["mixer", "volume"],
                        params=query_params(args[1:], ["_newvalue"]),
                    )
                ]
            return [
                render_line(
                    clientid=ctx.player_id,
                    terms=["mixer", "volume"],
                    params=query_params(args[1:], ["?"]),
                    results=[("_volume", player.volume)],
                )
            ]

        # bass/treble/pitch/muting: stored on the player state (no SlimProto
        # frame for these; SqueezePlay-only equalizer in the real LMS)
        if entity in ("bass", "treble", "pitch", "muting"):
            attr = f"mixer_{entity}"
            if len(args) > 1 and str(args[1]).isdigit():
                setattr(player, attr, int(str(args[1])))
                return [
                    render_line(
                        clientid=ctx.player_id,
                        terms=["mixer", entity],
                        params=query_params(args[1:], ["_newvalue"]),
                    )
                ]
            return [
                render_line(
                    clientid=ctx.player_id,
                    terms=["mixer", entity],
                    params=query_params(args[1:], ["?"]),
                    results=[(f"_{entity}", getattr(player, attr, 0))],
                )
            ]

        if handler._dispatcher:
            return await handler._dispatcher.player_command(ctx.player_id, "mixer", args)
        # Unknown entity: Perl's dispatch declares only volume/muting/treble/
        # bass/pitch (Request.pm:513-524), so the request is echoed.
        return [render_line(clientid=ctx.player_id, terms=["mixer", *args])]
    except Exception as exc:  # noqa: BLE001
        return [
            render_line(
                clientid=ctx.player_id,
                terms=["mixer", *args],
                results=[("error", str(exc))],
            )
        ]


@register_command("playerpref")
async def cmd_playerpref(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playerpref <key> [<value>|?] — ONE line, client id in front.

    ``playerprefQuery`` adds the bare result ``_p2``
    (``Slim/Control/Queries.pm:3009-3046``, ``addDispatch(['playerpref',
    '_prefname','?'])`` Request.pm:544); the setter ``['playerpref','_prefname',
    '_newvalue']`` (:546) declares no result, so both forms answer
    ``<clientid> playerpref <key> <value>``.  Live Perl 9.1.1, read-only,
    2026-09-12: ``playerpref ?`` →
    ``24%3A0a%3Ac4%3A29%3A77%3A90 playerpref %3F `` (bare ``_prefname`` '?'
    plus the empty ``_p2`` value → one trailing space).
    """
    if not args:
        # Live Perl 2026-09-12: 'playerpref' → '<clientid> playerpref  '.
        return _command_echo(["playerpref"], [], ["_prefname", "_newvalue"],
                             clientid=ctx.player_id, has_tags=True)
    key = str(args[0])
    try:
        from lyrion.player import PlayerManager

        player = PlayerManager().get_player(ctx.player_id) if ctx.player_id else None
        prefs = getattr(player, "playerprefs", None)
        if prefs is None:
            prefs = {}
            if player is not None:
                player.playerprefs = prefs
        if len(args) == 1 or (len(args) == 2 and str(args[1]) == "?"):
            value = prefs.get(key, "")
            return _command_line(
                ["playerpref"], args, ["_prefname", "?"],
                clientid=ctx.player_id, results=[("_p2", value)],
            )
        # Set form: value may be multi-word (join the rest)
        value = " ".join(str(a) for a in args[1:])
        prefs[key] = value
        # Wie Perls setChange-Callbacks (Player.pm:79): die Werte, die unsere
        # Frames lesen (digitalVolumeControl/preampVolumeControl, Mixer-Werte),
        # sofort auf den PlayerState anwenden.
        from lyrion.player.playerprefs import apply_player_pref

        if player is not None:
            apply_player_pref(player, key, value)
        return _command_line(["playerpref"], args, ["_prefname", "_newvalue"],
                             clientid=ctx.player_id, has_tags=True)
    except Exception:  # noqa: BLE001
        return _command_echo(["playerpref"], args, ["_prefname", "_newvalue"],
                             clientid=ctx.player_id)


@register_command("button")
async def cmd_button(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """button <name> — simulate a front-panel button press; ONE line.

    ``addDispatch(['button','_buttoncode','_time','_orFunction'],``
    ``[1,0,0,buttonCommand])`` (``Slim/Control/Request.pm:483``);
    ``buttonCommand`` adds no result (Commands.pm), so the request is echoed —
    with the client id (needsClient=1) and the declared ``_time``/
    ``_orFunction`` slots rendered empty when unset (Request.pm:1026-1028).
    The button name is executed through the shared map/function table
    (``Commands.pm:288`` → ``IR::executeButton(..., _orFunction=1)``), the same
    path the JSON/cometd ``button`` command uses — ``button jump_fwd`` (what
    the controllers post) is the function ``jump`` with argument ``fwd``
    (``Common.pm:1338-1360``, :296-337).
    """
    if not args:
        return _echo("button", args, clientid=ctx.player_id)
    if not ctx.player_id:
        return _command_echo(["button"], args,
                             ["_buttoncode", "_time", "_orFunction"])
    name = str(args[0]).lower()
    try:
        from lyrion.player import PlayerManager
        from lyrion.player.buttons import execute_named_button

        await execute_named_button(name, ctx.player_id, manager=PlayerManager())
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["button"], args,
                         ["_buttoncode", "_time", "_orFunction"],
                         clientid=ctx.player_id)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def _current_song_duration(player: Any) -> Any:
    """Length of the song playing NOW — Perl ``$song->duration()``.

    ``Slim/Player/Song.pm:809-816``::

        sub duration {
            my $self = shift;
            if (scalar @_) { return $self->_duration($_[0]); }
            return $self->_duration()
                || Slim::Music::Info::getDuration($self->currentTrack()->url);
        }

    The length belongs to the *current song*: the ``LENGTH`` of the playlist
    entry that plays, or the parsed length of a remote stream — 0/undef when
    there is none (a radio stream has no length of its own; live Perl
    9.1.1 reports 0 in ``remoteMeta.duration`` and omits the status token).

    This port tracks a length only for local entries (``PlayerState.duration``,
    written when a local track loads), so the length is read from the CURRENT
    playlist entry: a stream entry (``str`` URL) has no length — never the
    length of the local number that played before it — while a local entry
    (``int`` track id) takes the DB ``duration`` of that id, with the cached
    state value as fallback when the row is gone (Perl's
    ``$self->_duration() || …`` chain).  Returns 0 when nothing plays.
    """
    items = list(getattr(player, "playlist", None) or [])
    pos = getattr(player, "playlist_position", None)
    entry = items[pos] if isinstance(pos, int) and 0 <= pos < len(items) else None
    if entry is None:
        # No playing entry: Perl has no ``playingSong()`` and publishes no
        # duration at all.  ``current_track_id`` of a stopped player must not
        # resurrect a length either.
        return 0
    if not isinstance(entry, int):
        return 0                      # stream URL entry: no known length
    rows_d = await _query_db(
        "SELECT duration FROM tracks WHERE id = ?", (entry,))
    if rows_d:
        dur = rows_d[0]["duration"] or 0
        try:
            player.duration = float(dur)
        except Exception:
            pass
        return dur
    return getattr(player, "duration", 0) or 0


@register_command("status")
async def cmd_status(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """status [- <count>] [tags:<code>] [subscribe:<seconds>] — one CLI line.

    Perl renders the status answer as a single escaped line, the client id in
    front, then the request terms, the echoed request parameters and the
    results in insertion order (``statusQuery``, Queries.pm:3996-4601;
    live probe 2026-09-12 on :9090):

        <clientid> status <args…> player_name%3ASchlafzimmer
        player_connected%3A1 player_ip%3A192.168.1.154%3A52091 power%3A1
        signalstrength%3A49 mode%3Astop remote%3A1 current_title%3A… time%3A0
        rate%3A1 sync_master%3A… sync_slaves%3A… mixer%20volume%3A23
        playlist%20repeat%3A0 playlist%20shuffle%3A0 playlist%20mode%3Aoff
        seq_no%3A0 playlist_cur_index%3A0 playlist_timestamp%3A… 
        playlist_tracks%3A1 randomplay%3A0 digital_volume_control%3A1
        use_volume_control%3A1

    The playlist loop is appended inline as ``playlist_loop`` tokens when the
    playlist is exposed (Perl: ``$loop = $menuMode ? 'item_loop' :
    'playlist_loop'``, Queries.pm:4353; unrolled by Request.pm:2264-2281).
    Field semantics: ``duration`` follows Perl's rule now (token only for a
    truthy length of the song playing NOW, Queries.pm:4099-4102 — see
    :func:`_current_song_duration`).  Still different: the subscribe handling
    and the remoteMeta hashref Perl prints as ``HASH(0x…)`` (Queries.pm:4391).
    """
    if _is_query_echo(args):
        # 'status' has no '?' entry (Request.pm:618) → Perl echoes the request
        # without results and without a client id (live: 'status ?' →
        # 'status %3F').
        return [render_line(terms=["status", *args])]

    if not ctx.player_id:
        return _echo("status", args)
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return [
                render_line(
                    clientid=ctx.player_id,
                    terms=["status", *args],
                    results=[("error", "invalid player")],
                )
            ]
        # tags: a=artist l=album d=duration u=url g=genre y=year t=tracknum
        # n=modificationTime i=disc c=coverid K=artwork_url N=remote_title
        # x=remote (Perl's %tagMap, Queries.pm:5660-5700) — `id`/`title` are
        # added unconditionally (:5971-5972), see _status_playlist_loop.
        tags = ""
        subscribe_interval = 0
        for a in args:
            s = str(a)
            if s.startswith("tags:"):
                tags = s[5:]
            if s.startswith("subscribe:"):
                try:
                    subscribe_interval = int(s[10:])
                except ValueError:
                    subscribe_interval = 0
        elapsed = int(getattr(player, "elapsed", 0) or 0)
        # Perl publishes ``duration`` for the song that is playing NOW, and
        # only when its length is truthy — ``Slim/Control/Queries.pm``::
        #
        #     :4099  if (my $dur = $song->duration()) {
        #     :4100      $dur += 0;
        #     :4101      $request->addResult('duration', $dur);
        #     :4102  }
        #
        # ``$song->duration()`` is the length of the *current* song
        # (``$self->_duration() || Slim::Music::Info::getDuration(
        # $song->currentTrack()->url)``, ``Slim/Player/Song.pm:809-816``): the
        # DB length of the local entry that plays, the parsed length of a
        # remote stream — and NOTHING for a radio stream, which has no length
        # of its own.  The former code read ``player.duration``, a
        # player-level cache that a stream start does not reset, so a stream
        # entry inherited the length of the LOCAL number played before it.
        # Live Perl 9.1.1 (read-only 2026-09-14, player ``00:04:20:2b:88:c8``,
        # stream "Dj Fada 2 - Life Breath (oct12)", ``status - 1 tags:cdltoK``)
        # carries **no** ``duration`` token; the same rule is applied by the
        # JSON-RPC path (``api._perl_status_duration``).
        from lyrion.web.api import _perl_status_duration

        duration = _perl_status_duration(await _current_song_duration(player))
        remote = getattr(player, "remote", 0)
        current_title = getattr(player, "current_title", None)
        sync_master = getattr(player, "sync_master", None)
        sync_slaves = getattr(player, "sync_slaves", None) or []

        # Result order copied from Perl's statusQuery (Queries.pm:4052-4230;
        # live probe 2026-09-12).
        results: list[tuple[str, Any]] = [
            ("player_name", player.name),
            ("player_connected", 1 if player.connected else 0),
        ]
        if player.connected:
            results.append(("player_ip", f"{player.ip}:{player.port}"))
        results.extend([
            ("power", 1 if player.power else 0),
            ("signalstrength", getattr(player, "signal_strength", 0) or 0),
            ("mode", player.mode or "stop"),
        ])
        if remote:
            results.append(("remote", 1))
        if current_title:
            results.append(("current_title", current_title))
        results.extend([
            ("time", elapsed),
            ("rate", 1),
        ])
        # Perl adds the token only for a truthy song length (Queries.pm:4099);
        # the key is ABSENT for a radio stream.
        if duration is not None:
            results.append(("duration", duration))
        results.append(("volume", player.volume))   # EXTRA (not in Perl)
        if sync_master:
            results.append(("sync_master", sync_master))
        if sync_slaves:
            results.append(("sync_slaves", ",".join(str(s) for s in sync_slaves)))
        results.extend([
            ("mixer volume", player.volume),
            ("playlist repeat", getattr(player, "repeat", 0)),
            ("playlist shuffle", getattr(player, "shuffle", 0)),
            ("playlist mode", "off"),
            ("seq_no", getattr(player, "seq_no", 0)),
        ])
        if player.current_track_id is not None:
            results.append(("playlist_cur_id", player.current_track_id))
        results.extend([
            ("playlist_cur_index", player.playlist_position),
            ("playlist_timestamp", int(getattr(player, "playlist_timestamp", 0) or 0)),
            ("playlist_tracks", player.playlist_total),
            ("randomplay", getattr(player, "randomplay", 0)),
            ("digital_volume_control", 1 if getattr(player, "digital_volume_control", 1) else 0),
            ("use_volume_control", 1 if getattr(player, "use_volume_control", 1) else 0),
        ])
        if getattr(player, "current_url", None):
            results.append(("current_url", player.current_url))
        # remoteMeta: the now-playing stream metadata (HTTP headers / ICY).
        # Perl adds the hashref as one result and renderAsArray stringifies it
        # to 'HASH(0x…)' (Queries.pm:4391) — not reproducible, so we keep the
        # per-key fields we always had.
        remote_meta = getattr(player, "remote_meta", None)
        if remote_meta:
            for k, v in remote_meta.items():
                results.append((f"remoteMeta.{k}", v))
        # Playlist loop: Perl adds it whenever the playlist is non-empty —
        # the tag letters do NOT gate it (``statusQuery`` picks the loop name
        # from ``$menuMode`` alone and runs ``_addSong`` per entry,
        # ``Slim/Control/Queries.pm:4348-4353`` + ``:4405-4420``).  Live Perl
        # 9.1.1 (read-only 2026-09-20): ``<mac> status 0 3 tags:K`` answers
        # ``playlist index:0 id:… title:… artwork_url:…``.  The former
        # ``'l' in tags`` gate (``l`` is Perl's ALBUM tag) swallowed the whole
        # loop — and with it every artwork field — for a client asking with
        # ``tags:K``/``tags:c`` only, while this port's own JSON-RPC status
        # answers it (same state, same request).
        results.append(("playlist_loop", await _status_playlist_loop(player, tags)))

        return [
            render_line(
                clientid=ctx.player_id,
                terms=["status"],
                params=query_params(args, ["_index", "_quantity"]),
                results=results,
            )
        ]
    except Exception as e:  # noqa: BLE001
        return [
            render_line(
                clientid=ctx.player_id,
                terms=["status", *args],
                results=[("error", str(e))],
            )
        ]


async def _status_playlist_loop(player: Any, tags: str) -> list[dict[str, Any]]:
    """Build the ``playlist_loop`` items of the status answer — Perl ``_songData``.

    Perl builds every entry with ``_addSong`` → ``_songData`` and starts it with
    ``'playlist index'`` (unshifted, Queries.pm:5461-5479 + :4410-4414).  The
    field set is *one* code path for local tracks and URL entries
    (``RemoteTrack``): ``id`` and ``title`` are added BEFORE the tag loop and
    are NOT gated by any tag letter (Queries.pm:5971-5972)::

        $returnHash{'id'}    = $track->id;
        $returnHash{'title'} = $remoteMeta->{title} || $track->title;
        for my $tag (split (//, $tags)) { … $tagMap{$tag} … }

    Everything else follows the tag letters **in the order the client sent
    them** (``split //, $tags``, ``next if $seen{$tag}++``) and is omitted when
    the value is undefined/empty (``:6103-6108``).  Live Perl 9.1.1, read-only
    2026-09-20 — ``<mac> status 0 5 <tags>`` on the playing stream
    (``http://hirschmilch.de:7000/chillout.mp3``)::

        (no tags)   → playlist index:0 id:-94115167819792 title:A-Frame
        tags:a      → … title:A-Frame artist:Unknown Reality
        tags:d      → … title:A-Frame duration:0
        tags:u      → … title:A-Frame url:http://hirschmilch.de:7000/chillout.mp3
        tags:o      → … title:A-Frame type:MP3 Radio
        tags:N      → … title:A-Frame remote_title:Hirschmilch Chillout
        tags:K      → … title:A-Frame artwork_url:/imageproxy/http%3A%2F%2F
                      cdn-radiotime-logos.tunein.com%2Fs111987q.png/image.png
        tags:c      → … title:A-Frame coverid:-94115167819792
        tags:al     → … title:A-Frame artist:Unknown Reality   (no album)

    and, for a LOCAL track (same ``%tagMap``/``_songData`` path, live via
    ``titles 0 1 <tags>`` — ``id``/``title`` again unconditional)::

        tags:t → id:201214 title:… tracknum:7        ('t' => tracknum!)
        tags:n → id:201214 title:… modificationTime:1723554429
        tags:i → disc:1      tags:o → type:mp3       tags:d → duration:1195.075

    ``tags`` is Perl's ``$request->getParam('tags') || ''`` (Queries.pm:4008):
    an EMPTY code adds nothing beyond ``id``/``title`` — there is no
    "everything" mode (the former ``want_all`` here was this port's invention).

    Numeric fields keep Perl's ``defined $value && $value ne ''`` rule
    (:6103), so a stream answers ``duration:0`` and ``year:0`` (RemoteTrack's
    numeric defaults — live), while ``tracknum``/``disc`` stay absent for it
    (undef there, live ``tags:t``/``tags:i`` → nothing).

    The stream logo resolution is the JSON-RPC status' own
    (:func:`lyrion.web.api._stream_artwork_url`) — one resolution, both
    protocols — with Perl's ``html/images/radio.png`` fallback
    (``Protocols/HTTP.pm:1140-1147`` ``cover => $cover || $icon`` → the
    handler's ``cover`` becomes ``$remoteMeta->{K}``, Queries.pm:5928), and
    ``coverid`` the RemoteTrack id (``Slim/Schema/RemoteTrack.pm:317``
    ``id => -int($self)``).

    Not served here (measured live, unchanged): ``o`` (type — the stream's
    ``MP3 Radio``, the ICY content type), ``n`` for a stream (Perl answers a
    formatted date, ``Sonntag, 20. September 2026, 20:49``), ``r``/``f``/
    ``T``/``I``/``H`` (bitrate/filesize/samplerate/…), ``e``/``s``/``b``/``h``/
    ``z``/``A``/``S``, ``B``/``L``/``E``/``V``.
    """
    playlist = getattr(player, "playlist", []) or []
    if not playlist:
        return []
    items: list[dict[str, Any]] = []
    for i, item in enumerate(playlist):
        entry: dict[str, Any] = {"playlist index": i}
        if isinstance(item, int):
            rows = await _query_db(
                "SELECT id, title, url, duration, genre, year, tracknum, "
                "modtime, disc, remote FROM tracks WHERE id = ?",
                (item,),
            )
            if not rows:
                items.append(entry)
                continue
            r = rows[0]
            entry["id"] = int(r["id"] or item)
            entry["title"] = r["title"] or ""
            for code in dict.fromkeys(tags):
                if code == "u":                     # 'u' => url
                    entry["url"] = r["url"] or ""
                elif code == "d":                   # 'd' => secs
                    entry["duration"] = int(r["duration"] or 0)
                elif code == "g":                   # 'g' => genre.name
                    if r["genre"]:
                        entry["genre"] = r["genre"]
                elif code == "y":                   # 'y' => year (0 is emitted:
                    if r["year"] is not None:       # Perl's `$value ne ''`)
                        entry["year"] = r["year"]
                elif code == "t":                   # 't' => tracknum
                    if r["tracknum"] is not None:
                        entry["tracknum"] = r["tracknum"]
                elif code == "i":                   # 'i' => disc
                    if r["disc"] is not None:
                        entry["disc"] = r["disc"]
                elif code == "n":                   # 'n' => modificationTime
                    if r["modtime"] is not None:
                        entry["modificationTime"] = int(r["modtime"])
                elif code == "x":                   # 'x' => remote (only if set)
                    if r["remote"]:
                        entry["remote"] = 1
                elif code == "a":                   # 'a' => artistName
                    rows_a = await _query_db(
                        "SELECT c.name FROM contributors c JOIN tracks_contributors tc "
                        "ON tc.contributor = c.id AND tc.role = 1 "
                        "WHERE tc.track = ? ORDER BY c.name LIMIT 1",
                        (item,),
                    )
                    if rows_a and rows_a[0]["name"]:
                        entry["artist"] = rows_a[0]["name"]
                elif code == "l":                   # 'l' => albumname
                    rows_al = await _query_db(
                        "SELECT al.title FROM albums al JOIN tracks_albums ta "
                        "ON ta.album = al.id WHERE ta.track = ? LIMIT 1",
                        (item,),
                    )
                    if rows_al and rows_al[0]["title"]:
                        entry["album"] = rows_al[0]["title"]
        else:
            # URL item (radio/favourite) — the SAME ``_songData`` over Perl's
            # ``RemoteTrack``: ``$remoteMeta`` comes from the URL's protocol
            # handler (``Protocols/HTTP.pm:1031-1085``), ``$track`` from
            # ``objectForUrl($url)`` (Queries.pm:5888-5901).
            from lyrion.web.api import (
                REMOTE_ART_FALLBACK,
                _perl_proxied_image,
                _remote_track_id,
                _stream_artwork_url,
                _stream_entry_title,
                _stream_registered_name,
            )

            url = str(item)
            stream_title, stream_artist = _stream_entry_title(player, url)
            entry["id"] = _remote_track_id(url)
            entry["title"] = stream_title or url
            for code in dict.fromkeys(tags):
                if code == "u":                     # 'u' => RemoteTrack::url
                    entry["url"] = url
                elif code == "a":                   # $remoteMeta->{a} (ICY artist)
                    if stream_artist:
                        entry["artist"] = stream_artist
                elif code == "d":                   # (duration||secs||0) + 0
                    entry["duration"] = 0
                elif code == "y":                   # RemoteTrack::year is 0 —
                    entry["year"] = 0               # Perl emits `year:0` (live)
                elif code == "x":                   # 'x' => remote (isRemote)
                    entry["remote"] = 1
                elif code == "N":                   # 'N' => remote_title
                    name = _stream_registered_name(player, url)
                    if name:
                        entry["remote_title"] = name
                elif code == "K":                   # 'K' => artwork_url, proxied
                    art = _stream_artwork_url(player, url)
                    entry["artwork_url"] = (
                        _perl_proxied_image(art) if art else REMOTE_ART_FALLBACK)
                elif code == "c":                   # 'c' => RemoteTrack::coverid
                    entry["coverid"] = _remote_track_id(url)
        items.append(entry)
    return items


@register_command("mode")
async def cmd_mode(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """mode ? — ONE line: ``<clientid> mode <play|pause|stop>``.

    ``modeQuery`` adds the bare result ``_mode`` (``Slim/Control/Queries.pm:
    2144-2162``; ``addDispatch(['mode','?'])`` Request.pm:525).  ``mode
    pause|play|stop`` are separate command entries (:664-666) that add nothing
    → the request is echoed.  Live Perl 9.1.1, read-only, 2026-09-12:
    ``mode ?`` → ``24%3A0a%3Ac4%3A29%3A77%3A90 mode stop``.
    """
    if not _is_query_echo(args):
        # ``addDispatch(['mode','pause'|'play'|'stop'])`` (Request.pm:664-666)
        # are *literal* children of the 'mode' node — command entries with
        # requiresClient=1.  Any other shape (bare 'mode', 'mode foo') reaches
        # no leaf → status 104 → bare echo
        # (Request.pm:1093-1100, Plugin/CLI/Plugin.pm:657-663).
        # Live Perl 2026-09-12 (read-only): ``mode`` → ``mode``.
        word = str(args[0]).lower() if args else ""
        if word in ("pause", "play", "stop"):
            return _command_echo(["mode", word], args[1:], [],
                                 clientid=ctx.player_id)
        return _echo("mode", args, clientid=ctx.request_clientid)
    mode = "stop"
    try:
        from lyrion.player import PlayerManager

        player = PlayerManager().get_player(ctx.player_id) if ctx.player_id else None
        if player is not None:
            mode = player.mode or "stop"
    except Exception:  # noqa: BLE001
        pass
    return _command_line(["mode"], [], [], clientid=ctx.player_id,
                         results=[("_mode", mode)])


@register_command("time")
async def cmd_time(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """time [<seconds>|<mm:ss>|?<delta>] — ONE line.

    ``timeQuery`` adds the bare result ``_time`` (elapsed seconds;
    ``Slim/Control/Queries.pm:4786-4804``; ``addDispatch(['time','?'])``
    Request.pm:625).  The seek form ``['time','_newvalue']`` (:626) adds
    nothing → the request is echoed.  Live Perl 9.1.1, read-only, 2026-09-12:
    ``time ?`` → ``24%3A0a%3Ac4%3A29%3A77%3A90 time 0``.
    """
    if not args or (_is_query_echo(args) and len(args) == 1):
        elapsed = 0
        try:
            from lyrion.player import PlayerManager

            player = PlayerManager().get_player(ctx.player_id) if ctx.player_id else None
            if player is not None:
                elapsed = int(getattr(player, "elapsed", 0) or 0)
        except Exception:  # noqa: BLE001
            pass
        return _command_line(["time"], [], [],
                             clientid=ctx.player_id,
                             results=[("_time", elapsed)])
    if not ctx.player_id:
        return _command_echo(["time"], args, ["_newvalue"])
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        val = str(args[0])
        if val.startswith("?"):  # '?-5' / '?+10' relative query
            delta = int(val[2:] if val[1] in "+-" else val[1:])
            cur = int(getattr(player, "elapsed", 0) or 0)
            return _command_line(["time"], [val], [],
                                 clientid=ctx.player_id,
                                 results=[("_time", max(0, cur + delta))])
        # mm:ss form
        if ":" in val:
            parts = val.split(":")
            try:
                seconds = float(parts[0]) * 60 + float(parts[-1]) \
                    if len(parts) == 2 else float(parts[-1])
            except ValueError:
                seconds = 0.0
        else:
            seconds = float(val.rstrip("+-") or 0)
            if val.endswith("-"):
                seconds = max(0.0, (getattr(player, "elapsed", 0) or 0) - seconds)
            elif val.endswith("+"):
                seconds = (getattr(player, "elapsed", 0) or 0) + seconds
        await pm.seek_to(ctx.player_id, int(seconds))
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["time"], args, ["_newvalue"], clientid=ctx.player_id)


@register_command("sleep")
async def cmd_sleep(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """sleep [<seconds>|off|?] — ONE line.

    ``sleepQuery`` adds the bare result ``_sleep`` — the seconds left, clamped
    at 0 (``Slim/Control/Queries.pm:3900-3923``; ``addDispatch(['sleep','?'])``
    Request.pm:614).  The setter ``['sleep','_newvalue']`` (:615) adds nothing
    → the request is echoed.  Live Perl 9.1.1, read-only, 2026-09-12:
    ``sleep ?`` → ``24%3A0a%3Ac4%3A29%3A77%3A90 sleep 0``.
    """
    remaining = 0
    try:
        from lyrion.player import PlayerManager

        player = PlayerManager().get_player(ctx.player_id) if ctx.player_id else None
        if player is not None:
            remaining = int(getattr(player, "sleep_remaining", 0) or 0)
    except Exception:  # noqa: BLE001
        player = None
    if not args or (_is_query_echo(args) and len(args) == 1):
        return _command_line(["sleep"], [], [],
                             clientid=ctx.player_id,
                             results=[("_sleep", remaining)])
    if player is not None:
        t = str(args[0]).lower()
        if t == "off":
            player.sleep_remaining = 0
        else:
            try:
                player.sleep_remaining = int(t)
            except ValueError:
                pass
    return _command_echo(["sleep"], args, ["_newvalue"], clientid=ctx.player_id)


@register_command("signalstrength")
async def cmd_signalstrength(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """signalstrength ? — ONE line: ``<clientid> signalstrength <pct>``.

    ``signalstrengthQuery`` adds the bare result ``_signalstrength``
    (``Slim/Control/Queries.pm:3882-3898``, ``... || 0``;
    ``addDispatch(['signalstrength','?'])`` Request.pm:613).  Live Perl 9.1.1,
    read-only, 2026-09-12: ``signalstrength ?`` →
    ``24%3A0a%3Ac4%3A29%3A77%3A90 signalstrength 44``.
    """
    if not _is_query_echo(args):
        # ``['signalstrength','?']`` (Request.pm:613) is the only entry — a
        # query variant.  The bare word selects the missing command variant →
        # status 104 → bare echo (Request.pm:1093-1100,
        # Plugin/CLI/Plugin.pm:657-663).  Live Perl 2026-09-12:
        # ``signalstrength`` → ``signalstrength``.
        return _echo("signalstrength", args, clientid=ctx.request_clientid)
    sig = 0
    try:
        from lyrion.player import PlayerManager

        player = PlayerManager().get_player(ctx.player_id) if ctx.player_id else None
        if player is not None:
            sig = int(getattr(player, "signal_strength", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return _command_line(["signalstrength"], [], [], clientid=ctx.player_id,
                         results=[("_signalstrength", sig)])


@register_command("randomplay")
async def cmd_randomplay(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """randomplay [<mode>] — dispatched without '?', echoed bare with one.

    ``Slim/Plugin/RandomPlay/Plugin.pm:178`` registers
    ``addDispatch(['randomplay','_mode'], …)``; the core table
    (``Slim/Control/Request.pm:474-637``) has no entry.  A trailing '?' picks
    the query variant that this command-only entry does not have → status 104 →
    bare echo (``Slim/Control/Request.pm:1036`` + :1093-1100).  Live Perl 9.1.1,
    read-only, 2026-09-12: ``randomplay ?`` → ``randomplay %3F``,
    ``randomplay`` → ``<mac> randomplay ``.  We still store the mode on the
    player so telnet users keep working.
    """
    if ctx.player_id and args and str(args[0]) != "?":
        try:
            from lyrion.player import PlayerManager

            player = PlayerManager().get_player(ctx.player_id)
            if player is not None:
                player.randomplay = max(0, min(2, int(str(args[0]))))
        except Exception:  # noqa: BLE001
            pass
    # ``Slim/Plugin/RandomPlay/Plugin.pm:178`` registers
    # ``addDispatch(['randomplay','_mode'], …)`` — a *command* entry: its last
    # token is '_mode', not '?', so the query variant ``::[1]``
    # (``Slim/Control/Request.pm:1036``) stays unset.  A trailing '?' therefore
    # selects a variant that does not exist → status 104 → the request is
    # echoed VERBATIM and without a client id
    # (``Slim/Control/Request.pm:1093-1100`` + ``Slim/Plugin/CLI/Plugin.pm:
    # 657-663``).  Without a '?' the command entry is used (:1026) and the
    # entry's requiresClient=1 flag gives the echo the client id, while the
    # unset '_mode' still occupies its slot (:1026-1028).
    # Live Perl 9.1.1, read-only, 2026-09-12: ``randomplay ?`` →
    # ``randomplay %3F`` (bare), ``randomplay`` → ``<mac> randomplay `` (one
    # trailing space from the unset slot).
    if _is_query_echo(args):
        return _echo("randomplay", args, clientid=ctx.request_clientid)
    return _command_echo(["randomplay"], args, ["_mode"], clientid=ctx.player_id)


@register_command("current_title")
async def cmd_current_title(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """current_title ? — ONE line: ``<clientid> current_title <title>``.

    ``cursonginfoQuery`` adds the bare result ``_current_title``
    (``Slim/Music/Info::getCurrentTitle``, ``Slim/Control/Queries.pm:
    1448-1495``) and adds *nothing* when the URL is undefined (:1464) — the
    key is omitted, not sent empty.  Live Perl 9.1.1, read-only, 2026-09-12:
    ``current_title ?`` → ``24%3A0a%3Ac4%3A29%3A77%3A90 current_title Dense%20…``.
    """
    return await _current_metadata(ctx, "current_title")


@register_command("artist")
async def cmd_artist(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """artist ? — ONE line: ``<clientid> artist <name>`` (key omitted if unset).

    ``cursonginfoQuery`` (``Slim/Control/Queries.pm:1448-1495``) only adds the
    bare result ``_artist`` when ``_songData`` has the field (:1487-1489), so a
    track without an artist answers just the verb.  Live Perl 9.1.1, read-only,
    2026-09-12: ``artist ?`` → ``24%3A0a%3Ac4%3A29%3A77%3A90 artist``.
    """
    return await _current_metadata(ctx, "artist")


@register_command("album")
async def cmd_album(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """album ? — ONE line: ``<clientid> album <title>`` (key omitted if unset)."""
    return await _current_metadata(ctx, "album")


@register_command("genre")
async def cmd_genre(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """genre ? — ONE line: ``<clientid> genre <name>`` (key omitted if unset)."""
    return await _current_metadata(ctx, "genre")


async def _current_metadata(ctx: CLIContext, field: str) -> list[str]:
    """One line for the ``cursonginfoQuery`` entity of the current track.

    The result key is the bare ``_<entity>`` (``Slim/Control/Queries.pm:
    1487-1493``) and is added only when the value is defined; otherwise the
    answer is just ``<clientid> <entity>`` (live Perl 2026-09-12:
    ``album ?`` → ``24%3A… album``).
    """
    value: Any = None
    if ctx.player_id:
        try:
            from lyrion.player import PlayerManager

            player = PlayerManager().get_player(ctx.player_id)
            if player is not None and player.current_track_id is not None:
                cached = getattr(player, f"current_{field}", None)
                if cached:
                    value = str(cached)
                else:
                    if field == "artist":
                        sql = ("SELECT c.name AS v FROM contributors c "
                               "JOIN tracks_contributors tc ON tc.contributor = c.id "
                               "AND tc.role = 1 WHERE tc.track = ? LIMIT 1")
                    elif field == "album":
                        # tracks has no album column; resolve via tracks_albums.
                        sql = ("SELECT al.title AS v FROM albums al "
                               "JOIN tracks_albums ta ON ta.album = al.id "
                               "WHERE ta.track = ? LIMIT 1")
                    elif field == "current_title":
                        sql = "SELECT title AS v FROM tracks WHERE id = ?"
                    else:
                        sql = f"SELECT {field} AS v FROM tracks WHERE id = ?"
                    rows = await _query_db(sql, (player.current_track_id,))
                    if rows and rows[0]["v"]:
                        value = str(rows[0]["v"])
        except Exception:  # noqa: BLE001
            value = None
    results = [(f"_{field}", value)] if value is not None else []
    return _command_line([field], [], [], clientid=ctx.player_id, results=results)


# ---------------------------------------------------------------------------
# Playlist commands
# ---------------------------------------------------------------------------


def _move_playlist_item(pm, player_id: str, frm: int, to: int) -> bool:
    """Move a playlist item from index frm to index to."""
    player = pm.get_player(player_id)
    if player is None:
        return False
    n = len(player.playlist)
    if not (0 <= frm < n) or not (0 <= to < n):
        return False
    item = player.playlist.pop(frm)
    player.playlist.insert(to, item)
    player.last_activity = time.time()
    return True


# The 'playlist <entity> ?' query entities of playlistXQuery
# (Slim/Control/Queries.pm:2708-2774).
_PLAYLIST_QUERY_ENTITIES = frozenset({
    "name", "url", "modified", "tracks", "index", "jump", "genre", "title",
    "duration", "artist", "album", "path", "remote",
})


def _playlist_entity(player: Any, entity: str) -> Any:
    """The bare ``_<entity>`` value of one playlist query entity.

    ``playlistXQuery`` (``Slim/Control/Queries.pm:2708-2774``): repeat/shuffle
    from the playlist state (:2715/:2718), index/jump = the playing song index
    (:2721), name = the current playlist's title (:2724), url (:2729),
    modified (:2732), tracks = the playlist count (:2736), genre/title/
    duration = the ``_songData`` field of the track at ``_index`` (:2752-2758).
    """
    if player is None:
        return ""
    if entity == "repeat":
        return int(getattr(player, "repeat", 0) or 0)
    if entity == "shuffle":
        return int(getattr(player, "shuffle", 0) or 0)
    if entity in ("index", "jump"):
        return int(getattr(player, "playlist_position", 0) or 0)
    if entity == "tracks":
        return int(getattr(player, "playlist_total", 0)
                   or len(getattr(player, "playlist", []) or []))
    if entity == "modified":
        return int(getattr(player, "playlist_modified", 0) or 0)
    if entity == "name":
        return str(getattr(player, "current_playlist_name", "") or "")
    if entity == "url":
        return str(getattr(player, "current_url", "") or "")
    if entity == "path":
        return str(getattr(player, "current_url", "") or "") or "0"
    if entity == "remote":
        return 1 if getattr(player, "remote", 0) else 0
    # duration / artist / album / title / genre — the current song's field
    cached = getattr(player, f"current_{entity}", None)
    if cached:
        return str(cached)
    if entity == "title":
        return str(getattr(player, "current_title", "") or "")
    return ""


@register_command("playlist")
async def cmd_playlist(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist <entity|sub> [args] — every answer is ONE escaped line.

    Perl registers 'playlist' as one of the biggest dispatch groups
    (``Slim/Control/Request.pm:548-591``).  Two shapes matter here:

    * the *queries* ``playlist name|url|modified|tracks|index|shuffle|repeat|
      genre|title|duration|artist|album|path|remote ?`` → the bare result
      ``_<entity>`` (``playlistXQuery``, Queries.pm:2708-2774); live Perl
      2026-09-12: ``playlist name ?`` → ``<clientid> playlist name
      Hirschmilch%20Chillout``, ``playlist tracks ?`` → ``playlist tracks 1``,
      ``playlist genre ?`` → ``playlist genre %3F`` (the index is '?' → no
      result).
    * the *commands* (add/insert/load/play/resume/clear/delete/move/save/
      repeat/shuffle/index/jump) add no result — ``playlistXitemCommand``,
      ``playlistClearCommand``, ``playlistMoveCommand``, ``playlistJumpCommand``
      and friends in Commands.pm — so they are echoed (Plugin/CLI/Plugin.pm:
      692-698).  ``playlist save`` adds only the suppressed ``__playlist_id``
      (Commands.pm playlistSaveCommand:66).

    Sub-commands we keep beyond Perl (``loop``, the ``tracks`` listing, the
    ``next``/``prev``/``url`` shortcuts) answer in the same one-line shape.
    """
    if not args:
        # Live Perl 2026-09-12: 'playlist' → 'playlist'.
        return _echo("playlist", args)
    if not ctx.player_id:
        return _echo("playlist", args)
    sub = args[0].lower()
    rest = args[1:]
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        # playlistXQuery: every 'playlist <entity> ?' answers the bare
        # '_<entity>' result (Slim/Control/Queries.pm:2708-2774).  Live Perl
        # 9.1.1, read-only, 2026-09-12: 'playlist name ?' → '<clientid>
        # playlist name Hirschmilch%20Chillout', 'playlist tracks ?' →
        # 'playlist tracks 1', 'playlist url ?' → 'playlist url ' (empty).
        if sub in _PLAYLIST_QUERY_ENTITIES and _is_query_echo(rest):
            return _command_line(
                ["playlist", sub], [], [], clientid=ctx.player_id,
                results=[(f"_{sub}",
                          _playlist_entity(pm.get_player(ctx.player_id), sub))],
            )
        if sub == "play":
            return await cmd_playlist_play(handler, ctx, rest)
        if sub == "add":
            return await cmd_playlist_add(handler, ctx, rest)
        if sub == "insert":
            if rest:
                return await cmd_playlist_add(handler, ctx, rest)
            return _command_echo(["playlist", "insert"], rest, ["_item", "_title"],
                                 clientid=ctx.player_id)
        if sub == "delete":
            if not rest or not str(rest[0]).isdigit():
                return _command_echo(["playlist", "delete"], rest, ["_index"],
                                     clientid=ctx.player_id)
            pm.playlist_remove(ctx.player_id, int(rest[0]))
            return _command_echo(["playlist", "delete"], rest, ["_index"],
                                 clientid=ctx.player_id)
        if sub == "clear":
            pm.playlist_clear(ctx.player_id)
            return _command_line(["playlist", "clear"], [], [],
                                 clientid=ctx.player_id)
        if sub in ("next", "prev"):
            # No Perl dispatch entry for these — echoed like Perl does with an
            # unknown request (Plugin/CLI/Plugin.pm:657-663).
            if sub == "next":
                await pm.playlist_next(ctx.player_id)
            else:
                await pm.playlist_prev(ctx.player_id)
            # ``playlist next``/``playlist prev`` have no Perl dispatch leaf
            # (Request.pm:548-591 — every playlist entry has a literal
            # sub-verb) → status 104 → the request is echoed token by token and
            # without a client id (Request.pm:1093-1100,
            # Plugin/CLI/Plugin.pm:657-663).  Live Perl 2026-09-12:
            # ``playlist next`` → ``playlist next``.
            return [render_line(clientid=ctx.request_clientid,
                                terms=["playlist", sub, *rest])]
        if sub == "tracks":
            player = pm.get_player(ctx.player_id)
            tracks = player.playlist if player else []
            if _is_query_echo(rest):
                # Perl: playlistXQuery adds the bare _tracks (Queries.pm:2744).
                return _command_line(["playlist", "tracks"], [], [],
                                     clientid=ctx.player_id,
                                     results=[("_tracks", len(tracks))])
            # Titel der lokalen Tracks für die UI-Anzeige (ein Query)
            track_titles: dict[int, str] = {}
            track_ids = [e for e in tracks if not isinstance(e, str)]
            if track_ids:
                try:
                    from sqlalchemy import select

                    from lyrion.database.schema import Track
                    from lyrion.database.sqlite_helper import db_session

                    async with db_session() as session:
                        result = await session.execute(
                            select(Track.id, Track.title).where(Track.id.in_(track_ids))
                        )
                        track_titles = {tid: t for tid, t in result.all()}
                except Exception as exc:  # noqa: BLE001
                    logger.debug("playlist tracks: title lookup failed: %s", exc)
            items: list[dict[str, Any]] = []
            for i, entry in enumerate(tracks):
                if isinstance(entry, str):
                    items.append({"id": i, "url": entry,
                                  "title": "Radio Stream"})
                else:
                    items.append({"id": i, "track_id": entry,
                                  "title": track_titles.get(entry, "")})
            return _command_line(["playlist", "tracks"], rest, [],
                                 clientid=ctx.player_id,
                                 results=[("count", len(tracks)),
                                          ("item_loop", items)])
        if sub == "move":
            if len(rest) >= 2 and str(rest[0]).isdigit() and str(rest[1]).isdigit():
                _move_playlist_item(pm, ctx.player_id, int(rest[0]), int(rest[1]))
            return _command_echo(["playlist", "move"], rest,
                                 ["_fromindex", "_toindex"],
                                 clientid=ctx.player_id)
        if sub == "save":
            if rest:
                await pm.save_playlist(ctx.player_id, rest[0])
            return _command_echo(["playlist", "save"], rest, ["_title"],
                                 clientid=ctx.player_id)
        if sub in ("load", "resume"):
            if rest:
                await pm.load_playlist(ctx.player_id, rest[0])
            return _command_echo(["playlist", sub], rest, ["_item"],
                                 clientid=ctx.player_id)
        if sub == "url":
            # playlist url [<url> [title]] — replace queue with a stream URL
            if not rest:
                player2 = pm.get_player(ctx.player_id)
                return _command_line(["playlist", "url"], [], [],
                                     clientid=ctx.player_id,
                                     results=[("_url",
                                               getattr(player2, "current_url", "") or "")])
            title = " ".join(rest[1:]) if len(rest) > 1 else ""
            await pm.play_url(ctx.player_id, rest[0], title)
            return _command_echo(["playlist", "url"], rest, ["_item"],
                                 clientid=ctx.player_id)
        if sub in ("index", "jump"):
            # playlist index <n> / playlist jump [+n|-n|<n>] — Perl serves both
            # verbs from playlistJumpCommand (Commands.pm:925) and accepts
            # relative offsets ('+1'/'+0'/'-1', :970-1014); the arithmetic
            # lives in lyrion.player.manager.jump_target.
            player3 = pm.get_player(ctx.player_id)
            cur = getattr(player3, "playlist_position", 0) if player3 else 0
            if not rest or str(rest[0]) == "?":
                return _command_line(["playlist", sub], [], [],
                                     clientid=ctx.player_id,
                                     results=[(f"_{sub}", cur)])
            if player3 is None:
                return _command_echo(["playlist", sub], rest, ["_index"],
                                     clientid=ctx.player_id)
            from lyrion.player.manager import jump_target

            target = jump_target(player3, rest[0])
            if target is not None:
                await pm.playlist_play(ctx.player_id, target)
            return _command_echo(["playlist", sub], rest, ["_index"],
                                 clientid=ctx.player_id)
        if sub in ("shuffle", "repeat"):
            player4 = pm.get_player(ctx.player_id)
            if not rest or str(rest[0]) == "?":
                value = int(getattr(player4, sub, 0)) if player4 else 0
                return _command_line(["playlist", sub], [], [],
                                     clientid=ctx.player_id,
                                     results=[(f"_{sub}", value)])
            if player4 is not None:
                setattr(player4, sub, max(0, min(2, int(str(rest[0])))))
            return _command_echo(["playlist", sub], rest, ["_newvalue"],
                                 clientid=ctx.player_id)
        if sub == "loop":
            # Our own convenience verb (Perl has 'repeat'/'shuffle' only).
            player6 = pm.get_player(ctx.player_id)
            if _is_query_echo(rest):
                rep = int(getattr(player6, "repeat", 0)) if player6 else 0
                shu = int(getattr(player6, "shuffle", 0)) if player6 else 0
                return _command_line(
                    ["playlist", "loop"], [], [], clientid=ctx.player_id,
                    results=[("_repeat", rep), ("_shuffle", shu)],
                )
            return [render_line(clientid=ctx.request_clientid,
                                terms=["playlist", "loop", *rest])]
        if sub in ("genres", "genre"):
            # LMS: 'playlist genre ?' → the comma-joined genre list (Queries.pm
            # playlistXQuery 'genre' → bare _genre).
            player7 = pm.get_player(ctx.player_id)
            ids = [e for e in (player7.playlist if player7 else [])
                   if isinstance(e, int)] if player7 else []
            names = ""
            if ids:
                placeholders = ",".join("?" * len(ids))
                g_rows = await _query_db(
                    "SELECT DISTINCT t.genre AS g FROM tracks t "
                    f"WHERE t.id IN ({placeholders}) AND t.genre != '' "
                    "ORDER BY t.genre",
                    tuple(ids),
                )
                names = ",".join(r["g"] for r in g_rows)
            return _command_line(["playlist", "genre"], [], [],
                                 clientid=ctx.player_id,
                                 results=[("_genre", names)])
        # No dispatch leaf for this shape ('playlist ?', 'playlist foo', …):
        # Perl echoes the request verbatim, without a client id — the request
        # never becomes dispatchable (Request.pm:1063-1101,
        # Plugin/CLI/Plugin.pm:657-663).  Live Perl 2026-09-12: ``playlist ?``
        # → ``playlist %3F``.
        return _command_echo(["playlist", sub], rest, [],
                             clientid=ctx.request_clientid)
    except Exception:  # noqa: BLE001
        # Der Fehlerpfad trägt weiter den Sitzungs-Client: der Request war
        # dispatch-förmig (nur die Ausführung schlug lokal fehl), Perl würde ihn
        # mit Client beantworten (needsClient=1, Request.pm:548-591).
        return _echo("playlist", args, clientid=ctx.player_id)


# Sub-command "playlist play" — routed via the base handler, not the registry.
async def cmd_playlist_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist play <trackId|index|tag:value> — ONE escaped line.

    ``addDispatch(['playlist','play','_item','_title','_fadein'])``
    (``Slim/Control/Request.pm:576``); ``playlistXitemCommand`` adds no result
    (Commands.pm), so the request is echoed with the client id in front
    (Plugin/CLI/Plugin.pm:692-698).

    Extended (tagged) parameters from the Jive actions:
      track_id:<n>   play a library track
      item_id:<n>    play a favorite (stream)
      album_id:<n>   play all tracks of an album
      artist_id:<n>  play all tracks of an artist
      index:<n>      jump to a playlist index
      play_index:<n> Perl's start index OF THE LOADED LIST — the tapped row of
                     an album/artist drill.  ``playlistXitemCommand`` reads
                     ``my $jumpToIndex = $request->getParam('play_index')``
                     (Slim/Control/Commands.pm:1336), defaults it to 0 for
                     ``play``/``load`` (:1524) and jumps to it once the list is
                     loaded (:3241-3243 ``$client->execute(['playlist','jump',
                     $index, $fadeIn])``).  Live Perl's album drill sends it
                     with every tap: ``playallParams {play_index: <row>}``
                     (BrowseLibrary.pm:1871, XMLBrowser.pm:1328-1345).
    """
    if not ctx.player_id:
        return _command_echo(["playlist", "play"], args,
                             ["_item", "_title", "_fadein"])
    if not args:
        return _command_echo(["playlist", "play"], args,
                             ["_item", "_title", "_fadein"])
    try:
        from lyrion.player import PlayerManager

        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)

        # Tagged parameters (Jive action format)
        tags: dict[str, str] = {}
        positional: list[str] = []
        for a in args:
            s = str(a)
            if ":" in s:
                k, _, v = s.partition(":")
                if k in ("track_id", "item_id", "album_id", "artist_id",
                         "index", "play_index"):
                    tags[k] = v
                    continue
            positional.append(s)

        if "item_id" in tags:
            # Play a favorite (stream or folder entry)
            from lyrion.control.cli_commands import _fav_resolve_id
            from lyrion.music.favorites import get_favorites_manager

            fm = get_favorites_manager()
            fav_id = await _fav_resolve_id(fm, tags["item_id"])
            if fav_id is not None:
                await fm.play(ctx.player_id, fav_id)
            else:
                # Fallback: radio station id
                from lyrion.music.radio import get_radio_manager

                station = await get_radio_manager().get_station(int(tags["item_id"]))
                if station is not None:
                    await pm.play_url(ctx.player_id, station.url, station.name)
        elif "track_id" in tags:
            tid = int(tags["track_id"])
            pm.playlist_add(ctx.player_id, tid)
            await pm.play_track(ctx.player_id, tid)
        elif "album_id" in tags or "artist_id" in tags:
            # Expand to all tracks of the album/artist (one query)
            import sqlite3

            db = sqlite3.connect(
                f"file:{_library_db_path()}?mode=ro", uri=True)
            try:
                if "album_id" in tags:
                    rows = db.execute(
                        "SELECT t.id FROM tracks t JOIN tracks_albums ta ON ta.track = t.id "
                        "WHERE ta.album = ? ORDER BY t.tracknum, t.title",
                        (int(tags["album_id"]),)).fetchall()
                else:
                    rows = db.execute(
                        "SELECT t.id FROM tracks t JOIN tracks_contributors tc ON tc.track = t.id "
                        "WHERE tc.contributor = ? AND tc.role = 1 ORDER BY t.title",
                        (int(tags["artist_id"]),)).fetchall()
            finally:
                db.close()
            ids = [r[0] for r in rows]
            if ids:
                pm.playlist_clear(ctx.player_id)
                for tid in ids:
                    pm.playlist_add(ctx.player_id, tid)
                # The tapped row starts the album: Perl's ``play_index`` is the
                # jump target of the freshly loaded list (Commands.pm:1336
                # ``my $jumpToIndex = $request->getParam('play_index')`` →
                # :3241-3243 ``['playlist','jump',$index,$fadeIn]``, whose
                # playlistJumpCommand wraps an absolute index into the list,
                # :1005/:1010-1013).  Without it the list starts at 0
                # (:1524 ``$jumpToIndex = 0 if !defined $jumpToIndex``).
                start = 0
                if "play_index" in tags and player is not None:
                    from lyrion.player.manager import jump_target

                    target = jump_target(player, tags["play_index"])
                    if target is not None:
                        start = target
                await pm.play_track(ctx.player_id, ids[start])
        elif "index" in tags:
            await pm.playlist_play(ctx.player_id, int(tags["index"]))
        elif positional and str(positional[0]).isdigit():
            idx = int(str(positional[0]))
            if player and idx < len(player.playlist):
                await pm.playlist_play(ctx.player_id, idx)
            else:
                pm.playlist_add(ctx.player_id, idx)
                await pm.play_track(ctx.player_id, idx)
        elif positional:
            first = str(positional[0])
            # Bare stream URL (SqueezePlay sends playlist play <url> for a
            # favorite whose url it uses directly, no item_id) — play it.
            if "://" in first:
                await pm.play_url(ctx.player_id, first, "")
            else:
                track_id = int(first)
                pm.playlist_add(ctx.player_id, track_id)
                await pm.play_track(ctx.player_id, track_id)
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["playlist", "play"], args,
                         ["_item", "_title", "_fadein"],
                         clientid=ctx.player_id)


# Sub-command "playlist add" — routed via the base handler, not the registry.
async def cmd_playlist_add(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist add <trackId>... — ONE escaped line (Perl echoes the request).

    ``addDispatch(['playlist','add','_item','_title'])``
    (``Slim/Control/Request.pm:548``, ``playlistXitemCommand`` adds no result).
    """
    if ctx.player_id:
        try:
            from lyrion.player import PlayerManager

            pm = PlayerManager()
            for a in args:
                # Accept both plain ids and 'track_id:<n>' tagged form
                s = str(a)
                if ":" in s:
                    k, _, v = s.partition(":")
                    if k in ("track_id", "item_id"):
                        s = v
                try:
                    pm.playlist_add(ctx.player_id, int(s))
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001
            pass
    return _command_echo(["playlist", "add"], args, ["_item", "_title"],
                         clientid=ctx.player_id)


# Sub-command "playlist clear" — routed via the base handler, not the registry.
async def cmd_playlist_clear(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist clear — ONE escaped line (``playlistClearCommand``, no result)."""
    if ctx.player_id:
        try:
            from lyrion.player import PlayerManager

            PlayerManager().playlist_clear(ctx.player_id)
        except Exception:  # noqa: BLE001
            pass
    return _command_line(["playlist", "clear"], [], [], clientid=ctx.player_id)


# Sub-command "playlist save" — routed via the base handler, not the registry.
async def cmd_playlist_save(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist save <name> — ONE escaped line.

    ``playlistSaveCommand`` adds only ``__playlist_id``, which renderAsArray
    suppresses (``Slim/Control/Request.pm:2261``; Commands.pm
    playlistSaveCommand:66) — plus ``writeError`` on failure (:17/:63).
    """
    if not args:
        return _echo("playlist save", args, clientid=ctx.player_id)
    return _command_echo(["playlist", "save"], args, ["_title"],
                         clientid=ctx.player_id)


# Sub-command "playlist load" — routed via the base handler, not the registry.
async def cmd_playlist_load(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist load <name> — ONE escaped line (``playlistXitemCommand``)."""
    if not args:
        return _echo("playlist load", args, clientid=ctx.player_id)
    return _command_echo(["playlist", "load"], args, ["_item"],
                         clientid=ctx.player_id)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def _search_lms(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """LMS grouped search: 'search <start> <count> term:<begriff>' — ONE line.

    ``searchQuery`` (``Slim/Control/Queries.pm:3472-3659``) runs the search for
    each entity and, per entity, adds ``<type>s_count`` (:3595) and the unrolled
    ``<type>s_loop`` with the keys ``<type>_id`` and ``<type>`` (:3622/:3625);
    the final ``count`` is the sum over all entities (:3657).  The entities are
    contributor, album, work, genre and track (:3649-3653) — we have no 'work'
    table, so we cover the other four.

    The term is tokenized (``parseSearchTerm``, Plugin.pm:345-457: AND of the
    tokens, ``-word`` = NOT) and the hits are ranked by Perl's
    ``fulltextweight`` (``_getWeight``, Plugin.pm:486-503;
    ``ORDER BY quickSearch.fulltextweight DESC``, Queries.pm:3597).  Genres
    have no FTS path (Plugin.pm:3516) and keep the ``namesort`` order
    (Queries.pm:1911).

    Everything is appended to the same line by renderAsArray
    (``Slim/Control/Request.pm:2264-2281``); live Perl 9.1.1, read-only,
    2026-09-12: ``search 0 3 term:night`` → ``search 0 3 term%3Anight
    rescan%3A1`` (the scan guard, :3495-3497) with empty result sets while
    scanning.
    """
    nums = [int(a) for a in args if str(a).isdigit()]
    start = nums[0] if nums else 0
    count = nums[1] if len(nums) > 1 else 20
    term = next((str(a)[5:] for a in args if str(a).startswith("term:")), "")
    if not term:
        return _command_line(["search"], args, ["_index", "_quantity"])
    tokens = search_tokens(term)
    if not tokens:
        return _command_line(["search"], args, ["_index", "_quantity"])

    def _where(exprs: list[str]) -> tuple[str, tuple]:
        conds, params = _search_conditions(tokens, exprs)
        return ((" WHERE " + " AND ".join(conds)) if conds else "", tuple(params))

    cw, cp = _where(["c.name"])
    aw, ap = _where(["al.title"])
    gw, gp = _where(["g.namespell"])
    track_pred, track_params = _track_search_predicate(tokens, "tracks")
    # Perl's non-FTS fallback searches the entity's own title column
    # (`me.title` LIKE, Queries.pm:3570/:1814) and the FTS path additionally
    # matches album, artist and genre (Plugin.pm:31-47).  The unaliased
    # `FROM tracks WHERE title LIKE` shape of the fallback is kept as the first
    # term; the widening UNION is OR'ed in.
    tw = " WHERE title LIKE ?"
    tp: tuple = (f"%{term}%",)
    if track_pred:
        tw += f" OR {track_pred}"
        tp = tp + tuple(track_params)

    try:
        contributors = await _query_db(
            "SELECT DISTINCT c.id, c.name FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
            + cw + " ORDER BY c.id LIMIT ? OFFSET ?", cp + (count, start))
        albums = await _query_db(
            "SELECT al.id, al.title, al.year FROM albums al"
            + aw + " ORDER BY al.id LIMIT ? OFFSET ?", ap + (count, start))
        genres = await _query_db(
            "SELECT g.id, g.name, g.sortkey FROM genres g"
            + gw + " ORDER BY g.sortkey COLLATE NOCASE LIMIT ? OFFSET ?",
            gp + (count, start))
        tracks = await _query_db(
            "SELECT DISTINCT tracks.id, tracks.title, tracks.genre"
            + _track_weight_cols("tracks")
            + " FROM tracks" + tw + " ORDER BY tracks.id LIMIT ? OFFSET ?",
            tp + (count, start))
        c_total = await _query_db(
            "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
            + cw, cp)
        al_total = await _query_db(
            "SELECT COUNT(*) AS n FROM albums al" + aw, ap)
        g_total = await _query_db(
            "SELECT COUNT(*) AS n FROM genres g" + gw, gp)
        t_total = await _query_db(
            "SELECT COUNT(DISTINCT tracks.id) AS n FROM tracks" + tw, tp)
    except Exception:  # noqa: BLE001
        return _command_line(["search"], args, ["_index", "_quantity"])

    # Relevance order per entity (Queries.pm:3597); genres keep namesort.
    contributors = _rank_search_rows(contributors, tokens, title_key="name",
                                     w1_keys=())
    albums = _rank_search_rows(albums, tokens, title_key="title", w5_keys=("year",),
                               w3_keys=("artist_name",))
    tracks = _rank_search_rows(tracks, tokens, title_key="title",
                               w5_keys=("album_title", "genre"),
                               w3_keys=("artist_name", "comment"))

    c_n = c_total[0]["n"] if c_total else 0
    al_n = al_total[0]["n"] if al_total else 0
    g_n = g_total[0]["n"] if g_total else 0
    t_n = t_total[0]["n"] if t_total else 0

    results: list[tuple[str, Any]] = [
        ("contributors_count", c_n),
        ("contributors_loop", [{"contributor_id": r.get("id"),
                                "contributor": r.get("name") or ""}
                               for r in contributors]),
        ("albums_count", al_n),
        ("albums_loop", [{"album_id": r.get("id"), "album": r.get("title") or ""}
                         for r in albums]),
        ("genres_count", g_n),
        ("genres_loop", [{"genre_id": r.get("id"), "genre": r.get("name") or ""}
                         for r in genres]),
        ("tracks_count", t_n),
        ("tracks_loop", [{"track_id": r.get("id"), "track": r.get("title") or ""}
                         for r in tracks]),
        ("count", c_n + al_n + g_n + t_n),
    ]
    return _command_line(["search"], args, ["_index", "_quantity"],
                         results=results)


@register_command("search")
async def cmd_search(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """search <start> <count> term:<begriff> | search <type> <query> — ONE line.

    ``addDispatch(['search','_index','_quantity'],[0,1,1,searchQuery])``
    (``Slim/Control/Request.pm:610``).  A trailing '?' matches no entry (the
    query is registered without one) → echoed, live Perl 2026-09-12:
    ``search ?`` → ``search %3F``.

    The ``term:`` form is Perl's grouped search (see :func:`_search_lms`); the
    older ``search <type> <query>`` form is our own and answers in the same
    Perl shape (``<type>s_count``/``<type>s_loop``/``count``).
    """
    if args and str(args[0]).lower() == "items":
        # ``search items …`` belongs to the Radio plugin's dynamic search feed,
        # not to this library query: Perl walks the dispatch tree token by token
        # (``Slim/Control/Request.pm:1010-1014``) and therefore finds the
        # ``['search','items','_index','_quantity']`` leaf the radio search
        # plugin registered (``Slim/Plugin/OPMLBased.pm:117-120``) *before*
        # ``items`` could be consumed as the ``_index`` of the library entry
        # (``Queries.pm:610``).  Live Perl 9.1.1, read-only 2026-09-18:
        # ``search items 0 20`` → ``<mac> search items 0 20 count%3A0`` (with the
        # client prefix of the radio dispatch), ``search 0 3 term:rock`` → the
        # library answer (``contributors_count``/``albums_count``).
        return await _radio_feed_answer("search", ctx, args)
    if _is_query_echo(args):
        return _echo("search", args)
    if not args:
        return _echo("search", args)
    # LMS grouped format: search <start> <count> term:<begriff>
    if any(str(a).startswith("term:") for a in args):
        return await _search_lms(handler, ctx, args)

    search_type = args[0].lower()
    query = str(args[1]) if len(args) > 1 else ""
    offset = int(args[2]) if len(args) > 2 else 0
    limit = int(args[3]) if len(args) > 3 else 100
    # Perl's entity names (Slim/Control/Queries.pm:3577-3581).
    perl_type = {
        "tracks": "track", "songs": "track", "titles": "track",
        "artists": "contributor", "albums": "album", "genres": "genre",
    }.get(search_type)
    if perl_type is None:
        return _echo("search", args)

    like = f"%{query}%"
    try:
        if perl_type == "track":
            rows = await _query_db(
                "SELECT id, title AS name FROM tracks WHERE title LIKE ? "
                "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, limit, offset),
            )
            total = await _query_db(
                "SELECT COUNT(*) AS n FROM tracks WHERE title LIKE ?", (like,))
        elif perl_type == "contributor":
            rows = await _query_db(
                "SELECT id, name FROM contributors WHERE name LIKE ? "
                "ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, limit, offset),
            )
            total = await _query_db(
                "SELECT COUNT(*) AS n FROM contributors WHERE name LIKE ?", (like,))
        elif perl_type == "album":
            rows = await _query_db(
                "SELECT id, title AS name FROM albums WHERE title LIKE ? "
                "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, limit, offset),
            )
            total = await _query_db(
                "SELECT COUNT(*) AS n FROM albums WHERE title LIKE ?", (like,))
        else:  # genre
            rows = await _query_db(
                "SELECT DISTINCT genre AS name FROM tracks WHERE genre LIKE ? "
                "ORDER BY genre COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, limit, offset),
            )
            total = await _query_db(
                "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre LIKE ?",
                (like,))
    except Exception:  # noqa: BLE001
        return _echo("search", args)
    total_n = total[0]["n"] if total else 0
    loop = [
        {f"{perl_type}_id": r["id"] if "id" in r else r["name"],
         perl_type: r["name"]}
        for r in rows
    ]
    return _command_line(
        ["search"], args, [],
        results=[(f"{perl_type}s_count", total_n),
                 (f"{perl_type}s_loop", loop),
                 ("count", total_n)],
    )


@register_command("rescan")
async def cmd_rescan(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """rescan [<mode>] [<target>] | rescan ? — ONE escaped line.

    ``rescanQuery`` adds the bare result ``_rescan`` (``1`` while a scan runs,
    else ``0``; ``Slim/Control/Queries.pm:3214-3229``) and
    ``['rescan','_mode','_target']`` (:606) is the command entry, which adds no
    result → the request is echoed (Plugin/CLI/Plugin.pm:692-698).  Live Perl
    9.1.1, read-only, 2026-09-12: ``rescan ?`` → ``rescan 0``.

    Perls ``rescanCommand`` (``Slim/Control/Commands.pm:2679-2830``) startet
    den Scan: Modus ``full`` als Default (:2689), ``album``/``track`` ohne ID
    = Fehler (:2693-2699), ein laufender Scan macht den Request zu einer
    *queued* Scan-Aufgabe statt zu einem zweiten parallelen Scan (:2712-2720).
    Die Verdrahtung selbst liegt in :mod:`lyrion.control.rescan`, damit CLI
    und JSON-RPC denselben Weg gehen (Perl: dieselbe ``rescanCommand``,
    ``Slim/Web/JSONRPC.pm:400-520``).
    """
    from lyrion.control import rescan as scan

    if _is_query_echo(args):
        return _command_line(["rescan"], [], [],
                             results=[("_rescan", 1 if scan.is_scanning() else 0)])

    mode = args[0] if args else scan.DEFAULT_MODE
    target = args[1] if len(args) > 1 else None
    # A failure is logged with its traceback (Perl: the scanner error plus the
    # Progress 'failure' row, Queries.pm:3291-3296); it must never be silent.
    scan.request_scan(mode, target)
    return _command_echo(["rescan"], args, ["_mode", "_target"])


@register_command("wipecache")
async def cmd_wipecache(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """wipecache [<queue>] — ONE escaped line (Perl adds no result).

    ``addDispatch(['wipecache','_queue'],[0,0,0,wipecacheCommand])``
    (``Slim/Control/Request.pm:631``); ``wipecacheCommand`` only launches the
    scan and adds no result, so the request is echoed — the unset ``_queue``
    still occupies its slot and renders as an empty token
    (Request.pm:1026-1028).  Live Perl 9.1.1, read-only, 2026-09-12:
    ``wipecache`` → ``wipecache `` (one trailing space).
    """
    try:
        from pathlib import Path

        from lyrion.config import get_config

        cache_dir = Path(get_config().cache_dir)
        if cache_dir.is_dir():
            for p in cache_dir.rglob("*"):
                if p.is_file():
                    try:
                        p.unlink()
                    except OSError:
                        pass
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["wipecache"], args, ["_queue"])


# ---------------------------------------------------------------------------
# Display / IR / Misc player commands
# ---------------------------------------------------------------------------


@register_command("display")
async def cmd_display(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """display <line1> <line2> [<duration>] | display ? — ONE escaped line.

    ``displayQuery`` adds the bare results ``_line1`` and ``_line2``
    (``Slim/Control/Queries.pm:1550-1568``) and is registered as
    ``['display','?','?']`` (``Slim/Control/Request.pm:492``); the setter
    ``['display','_line1','_line2','_duration']`` (:493) adds nothing → the
    request is echoed.  Live Perl 9.1.1, read-only, 2026-09-12: ``display ?`` →
    ``24%3A0a%3Ac4%3A29%3A77%3A90 display  `` (two empty values), ``display x y
    5`` → ``24%3A… display x y 5``.
    """
    if _is_query_echo(args):
        return _command_line(["display"], [], [], clientid=ctx.player_id,
                             results=[("_line1", ""), ("_line2", "")])
    if ctx.player_id:
        try:
            line1 = str(args[0]) if args else ""
            line2 = str(args[1]) if len(args) > 1 else ""
            duration = int(args[2]) if len(args) > 2 and str(args[2]).isdigit() else 3
            from lyrion.player import PlayerManager

            await PlayerManager().show_display(ctx.player_id, line1, line2, duration)
        except Exception:  # noqa: BLE001
            pass
    return _command_echo(["display"], args,
                         ["_line1", "_line2", "_duration"],
                         clientid=ctx.player_id)


@register_command("ir")
async def cmd_ir(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """ir <button_code> — simulate an IR button press; ONE escaped line.

    ``addDispatch(['ir','_ircode','_time'],[1,0,0,irCommand])``
    (``Slim/Control/Request.pm:506``); ``irCommand`` adds no result, so the
    request is echoed with the client id — the unset ``_time`` slot renders as
    an empty token (:1026-1028).  Live Perl 9.1.1, read-only, 2026-09-12:
    ``ir 123`` → ``24%3A0a%3Ac4%3A29%3A77%3A90 ir 123 `` (one trailing space).

    The button code is a numeric SlimProto IR code; named buttons
    ('play','arrow_up',…) are mapped here so clients can use either form.
    """
    if _is_query_echo(args):
        return _echo("ir", args)
    if not ctx.player_id:
        return _command_echo(["ir"], args, ["_ircode", "_time"])
    try:
        code = _resolve_ir_code(args[0]) if args else 0
        from lyrion.player import PlayerManager

        await PlayerManager().send_ir(ctx.player_id, code)
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["ir"], args, ["_ircode", "_time"],
                         clientid=ctx.player_id)


# Common Squeezebox IR button codes (Slim::Hardware::IRBLaster / default map)
_IR_CODES: dict[str, int] = {
    "play": 0x7689C,
    "pause": 0x76899,
    "stop": 0x76893,
    "skip": 0x76897,
    "fwd": 0x76897,
    "rew": 0x76891,
    "prev": 0x76891,
    "arrow_up": 0x7685A,
    "arrow_down": 0x7685B,
    "arrow_left": 0x7685C,
    "arrow_right": 0x7685D,
    "up": 0x7685A,
    "down": 0x7685B,
    "left": 0x7685C,
    "right": 0x7685D,
    "select": 0x76858,
    "center": 0x76858,
    "power": 0x76880,
    "add": 0x76854,
    "volume_up": 0x76855,
    "volume_down": 0x76856,
    "voldown": 0x76856,
    "volup": 0x76855,
    "sleep": 0x76888,
    "shuffle": 0x76852,
    "repeat": 0x76853,
    "size": 0x76851,
    "brightness": 0x76850,
    "now_playing": 0x7685E,
    "search": 0x7685F,
    "browse": 0x76860,
    "favorites": 0x76861,
    "zero": 0x76862,
    "display": 0x7685E,
}


def _resolve_ir_code(token: str) -> int:
    """Resolve an IR token (name or decimal/hex code) to a numeric code."""
    t = str(token).strip().lower()
    if t in _IR_CODES:
        return _IR_CODES[t]
    # bare code: decimal "768989" or hex "0x768989" / "768989h"
    s = t
    if s.endswith("h"):
        s = s[:-1]
    base = 16 if (s.startswith("0x") or s.endswith("h")) else 10
    try:
        return int(s if not s.startswith("0x") else s[2:], base)
    except ValueError:
        raise ValueError(token)


# ---------------------------------------------------------------------------
# Prefs
# ---------------------------------------------------------------------------


@register_command("pref")
async def cmd_pref(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """pref <key> [<value>|?] — ONE escaped line.

    ``prefQuery`` always adds the bare result ``_p2`` (the preference value)
    (``Slim/Control/Queries.pm:3009-3046``; ``addDispatch(['pref','_prefname',
    '?'])`` Request.pm:602), the setter ``['pref','_prefname','_newvalue']``
    (:604) declares no result.  Live Perl 9.1.1, read-only, 2026-09-12:
    ``pref ?`` → ``pref %3F `` — the bare ``_prefname`` '?' plus the empty
    ``_p2`` value, i.e. one trailing space and no client id (needsClient=0).
    """
    if not args:
        # Live Perl 2026-09-12: 'pref' → 'pref  ' (both declared slots empty).
        return _command_echo(["pref"], [], ["_prefname", "_newvalue"], has_tags=True)
    key = str(args[0])
    # LMS 'pref <key> ?' is a QUERY — set value to None so we read instead of
    # writing '?'.
    value: Optional[str] = None
    query_form = len(args) == 1 or (len(args) == 2 and str(args[1]) == "?")
    if len(args) > 1 and str(args[1]) != "?":
        value = str(args[1])
    if handler._dispatcher:
        try:
            await handler._dispatcher.get_set_preference(key, value)
        except Exception:  # noqa: BLE001
            pass
    if query_form:
        current = ""
        try:
            from lyrion.config import get_config

            current = str(get_config().get(key, "") or "")
        except Exception:  # noqa: BLE001
            current = ""
        return _command_line(["pref"], args, ["_prefname", "?"],
                             results=[("_p2", current)])
    return _command_echo(["pref"], args, ["_prefname", "_newvalue"],
                         has_tags=True)


def _alarm_loop_entry(index: int, a: Any) -> dict[str, Any]:
    """One ``alarms_loop`` item in Perl's key order.

    ``alarmsQuery`` adds id, dow, enabled, repeat, shufflemode, time, volume and
    url per item (``Slim/Control/Queries.pm:235-242``).  Perl's ``time`` is
    seconds since midnight and ``url`` falls back to ``CURRENT_PLAYLIST``
    (:242); live Perl 9.1.1, read-only, 2026-09-12: ``alarms 0 5`` →
    ``<clientid> alarms 0 5 fade%3A1 count%3A1 id%3Af205b436
    dow%3A1%2C2%2C3%2C4%2C5 enabled%3A1 repeat%3A1 shufflemode%3A0 time%3A21600
    volume%3A34 url%3Ahttp%3A%2F%2Fhirschmilch.de%3A7000%2Fchillout.mp3``.
    """
    days = str(getattr(a, "days", "") or "")
    # ``Alarm.days`` ist Monday-first (alarms.py:37, :72); Perl zählt dow
    # 0=Sonntag..6=Samstag (Alarm.pm:116-118) → Index i ist Perl-Tag (i+1)%7.
    # Die Live-Zeile im Docstring (Mo-Fr-Alarm → dow 1,2,3,4,5) bestätigt das.
    dow = ",".join(str((i + 1) % 7) for i in range(7)
                   if i < len(days) and days[i] == "1")
    time_str = str(getattr(a, "time", "0") or "0")
    try:
        hh, mm = time_str.split(":", 1)
        seconds = int(hh) * 3600 + int(mm) * 60
    except ValueError:
        seconds = 0
    wake = str(getattr(a, "wake", "") or "")
    url = wake.split(":", 1)[1] if wake.startswith("url:") else wake
    return {
        "id": index,
        "dow": dow,
        "enabled": 1 if getattr(a, "enabled", False) else 0,
        "repeat": 1 if getattr(a, "repeat", False) else 0,
        # Kein fester Wert: Jive speichert ihn (alarms.py:50-51, Command
        # ``jiveupdatealarm``, Commands.pm:188) und ``alarmsQuery`` gibt ihn
        # aus (Queries.pm:235-242).
        "shufflemode": int(getattr(a, "shufflemode", 0) or 0),
        "time": seconds,
        "volume": int(getattr(a, "volume", -1) or 0),
        "url": url or "CURRENT_PLAYLIST",
    }


@register_command("alarms")
async def cmd_alarms(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """alarms [<index> <quantity>] — ONE line, ``alarms_loop`` unrolled inline.

    ``addDispatch(['alarms','_index','_quantity'],[1,1,1,alarmsQuery])``
    (``Slim/Control/Request.pm:477``); ``alarmsQuery`` (Queries.pm:185-246) adds
    the result ``fade`` (the global fade-in seconds, :207) and ``count`` (:219)
    *before* the ``alarms_loop`` (:235-242), which is unrolled on the same line
    (Request.pm:2264-2281).  Perl's '?' would select a query entry that does not
    exist for 'alarms', so ``alarms ?`` is echoed.
    """
    if _is_query_echo(args):
        # ``['alarms','_index','_quantity']`` (Request.pm:477) is a *command*
        # entry, so '?' selects a query variant that does not exist → status
        # 104 → the request is echoed bare (Request.pm:1093-1100,
        # Plugin/CLI/Plugin.pm:657-663).  Live Perl 2026-09-12:
        # ``alarms ?`` → ``alarms %3F``; ``<mac> alarms ?`` → prefixed, because
        # the id then comes from the request line (Stdio.pm:96-116).
        return _echo("alarms", args, clientid=ctx.request_clientid)
    from lyrion.alarms import AlarmManager

    mac = ctx.player_id or ""
    if mac == "-":
        mac = ""
    alarms = AlarmManager().alarms_for(mac)
    loop = [_alarm_loop_entry(idx, alarms[idx]) for idx in sorted(alarms)]
    return _command_line(
        ["alarms"], args, ["_index", "_quantity"],
        clientid=ctx.player_id, has_tags=True,
        results=[("fade", 0), ("count", len(loop)), ("alarms_loop", loop)],
    )


@register_command("alarm")
async def cmd_alarm(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """alarm <index> [key:value …|delete|?] — ONE escaped line.

    ``addDispatch(['alarm','_cmd'],[1,0,1,alarmCommand])``
    (``Slim/Control/Request.pm:475``) — a *command* with tags and no query
    entry, so the parameters are echoed and ``alarmCommand`` only adds the
    result ``id`` for the caller (``Slim/Control/Commands.pm alarmCommand:161``).
    Perl has no '?' entry for 'alarm' (:475-476 — only 'alarm playlists' is a
    query), so the field query below is our own one-line extension of the same
    ``alarms_loop`` keys.
    """
    from lyrion.alarms import AlarmManager, _alarm_from_parts

    if not args:
        return _echo("alarm", args, clientid=ctx.player_id)
    try:
        idx = int(args[0])
    except ValueError:
        return _command_echo(["alarm"], args, ["_cmd"],
                             clientid=ctx.player_id, has_tags=True)

    mac = ctx.player_id or ""
    if mac == "-":
        mac = ""
    mgr = AlarmManager()

    if len(args) == 1 or (len(args) == 2 and args[1] == "?"):
        a = mgr.get(mac, idx)
        if a is None:
            return [render_line(
                clientid=ctx.player_id,
                terms=["alarm", *args],
            )]
        entry = _alarm_loop_entry(idx, a)
        return _command_line(
            ["alarm"], args, ["_cmd"],
            clientid=ctx.player_id, has_tags=True,
            results=[("id", entry.pop("id")), *entry.items()],
        )

    if len(args) >= 2 and args[1] == "delete":
        mgr.delete(mac, idx)
        return _command_line(["alarm"], args, ["_cmd"],
                             clientid=ctx.player_id, has_tags=True,
                             results=[("id", idx)])

    # Set form: collect key:value pairs (and tolerate a bare '0'/'1' toggle).
    parts: dict[str, str] = {}
    for tok in args[1:]:
        if ":" in str(tok):
            k, v = str(tok).split(":", 1)
            parts[k] = v
    current = mgr.get(mac, idx)
    a = _alarm_from_parts(idx, parts)
    # Preserve unspecified fields from the existing alarm.
    if current:
        for f in ("enabled", "days", "time", "volume", "fade", "duration",
                  "repeat", "wake"):
            if f not in parts:
                setattr(a, f, getattr(current, f))
    mgr.set(mac, idx, a)
    return _command_line(["alarm"], args, ["_cmd"],
                         clientid=ctx.player_id, has_tags=True,
                         results=[("id", idx)])


# ---------------------------------------------------------------------------
# Subscribe / Unsubscribe
# ---------------------------------------------------------------------------


@register_command("subscribe")
async def cmd_subscribe(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """subscribe <functions> — ONE escaped line (Perl echoes the request).

    ``addDispatch(['subscribe','_functions'],[0,0,0,subscribeCommand])``
    (``Slim/Plugin/CLI/Plugin.pm:107-108``); ``subscribeCommand`` (:846-880)
    only registers the notification terms and adds no result, so the request is
    echoed (Plugin/CLI/Plugin.pm:692-698).

    Our CLI subscription below is a different mechanism (per-player status
    pushes); the answer follows Perl.
    """
    if args:
        player_id = str(args[0])
        interval = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 5
        ctx.subscribed_player = player_id
        ctx.subscribe_interval = max(1, interval)
        if player_id not in handler._subscriptions:
            handler._subscriptions[player_id] = asyncio.Queue()
    return _command_echo(["subscribe"], args, ["_functions"])


@register_command("unsubscribe")
async def cmd_unsubscribe(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """unsubscribe — Perl has no such request, so it is echoed verbatim.

    No dispatch entry (``Slim/Control/Request.pm:474-637``) and no CLI-plugin
    registration → status 104 → echo (Plugin/CLI/Plugin.pm:657-663).  Live Perl
    9.1.1, read-only, 2026-09-12: ``unsubscribe`` → ``unsubscribe``.
    """
    player_id = ctx.subscribed_player
    ctx.subscribed_player = None
    ctx.subscribe_interval = 0
    if player_id:
        handler._subscriptions.pop(player_id, None)
    return _echo("unsubscribe", args)


# ---------------------------------------------------------------------------
# Info commands (format queries)
# ---------------------------------------------------------------------------


@register_command("info")
async def cmd_info(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """info total <genres|artists|albums|songs|duration> ? — ONE escaped line.

    Five separate dispatch entries (``Slim/Control/Request.pm:501-505``), each
    ``['info','total','<entity>','?']`` with ``[0,1,0]`` — no client, no tags.
    ``infoTotalQuery`` (``Slim/Control/Queries.pm:2019-2051``) adds the bare
    result ``_<entity>``, so the answer is ``info total songs 80134`` (live
    Perl 9.1.1, read-only, 2026-09-12).  'info' or 'info total' alone match no
    leaf — live Perl: ``info total ?`` → ``info total %3F``.
    """
    want = " ".join(str(a).lower() for a in args)
    if not _is_query_echo(args) or not want.startswith("total "):
        return _echo("info", args)
    key = want.split(" ", 1)[1].rstrip("?").strip()
    if key not in ("genres", "artists", "albums", "songs", "duration"):
        return _echo("info", args)
    value: Any = 0
    try:
        if key == "songs":
            rows = await _query_db("SELECT COUNT(*) AS n FROM tracks")
            value = int(rows[0]["n"]) if rows else 0
        elif key == "duration":
            rows = await _query_db("SELECT COALESCE(SUM(duration),0) AS n FROM tracks")
            value = rows[0]["n"] if rows else 0
        elif key == "albums":
            rows = await _query_db("SELECT COUNT(*) AS n FROM albums")
            value = int(rows[0]["n"]) if rows else 0
        elif key == "artists":
            rows = await _query_db(
                "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
                "JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
            )
            value = int(rows[0]["n"]) if rows else 0
        else:  # genres
            rows = await _query_db(
                "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre != ''"
            )
            value = int(rows[0]["n"]) if rows else 0
    except Exception:  # noqa: BLE001
        value = 0
    return _command_line(["info", "total", key], [], [],
                         results=[(f"_{key}", value)])


# ---------------------------------------------------------------------------
# Library queries (artists / albums / songs / genres / playlists)
#
# These read the scanned library database directly so they work both over
# the CLI wire protocol and through the web JSON-RPC slim.request passthrough
# (where no RequestDispatcher is attached).
# ---------------------------------------------------------------------------

def _library_db_path() -> str:
    """Resolve the library DB path from the active config (test/dev runs use
    LYRION_SERVERDATA; the production default stays /root/.lyrion)."""
    try:
        from lyrion.config import get_config
        return str(get_config().db_path)
    except Exception:  # noqa: BLE001
        return "/root/.lyrion/Lyrion/Prefs/lyrion.db"


_LIBRARY_DB = "/root/.lyrion/Lyrion/Prefs/lyrion.db"


def _parse_query_args(args: list[str]) -> tuple[int, int, dict[str, str]]:
    """Parse 'artists 0 5 tags:' style args → (offset, limit, filters).

    Robust against non-string args (the web UI sends numbers).
    """
    offset, limit = 0, 100
    filters: dict[str, str] = {}
    for i, arg in enumerate(args):
        s = str(arg)
        if s.isdigit():
            if i == 0:
                offset = int(s)
            elif i == 1:
                limit = int(s)
        elif ":" in s:
            key, _, val = s.partition(":")
            filters[key] = val
    return offset, limit, filters


async def _query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query against the library DB (in a thread)."""

    def _run() -> list[dict]:
        import sqlite3

        con = sqlite3.connect(f"file:{_library_db_path()}?mode=ro", uri=True, timeout=30)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Library query failed: %s", exc)
        return []


async def _write_db(sql: str, params: tuple = ()) -> bool:
    """Run a write query against the library DB (in a thread)."""

    def _run() -> bool:
        import sqlite3

        con = sqlite3.connect(_library_db_path(), timeout=30)
        try:
            con.execute(sql, params)
            con.commit()
            return True
        finally:
            con.close()

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Library write failed: %s", exc)
        return False


def _search_conditions(tokens: list[str], exprs: list[str]) -> tuple[list[str], list[str]]:
    """AND of per-token ``(col LIKE ? OR col LIKE ? …)`` conditions.

    Perl's full-text search ANDs the tokens (``parseSearchTerm`` joins them
    with ``AND``, Plugin.pm:454) and a ``-`` prefixed token becomes ``NOT``
    (:457).  Candidate matching is a case-folded substring LIKE here; the
    ranking that follows is Perl's ``fulltextweight`` (see
    :func:`lyrion.control.queries.fulltext_weight`).
    """
    conds: list[str] = []
    params: list[str] = []
    included = [t for t in tokens if not t.startswith("-")]
    excluded = [t[1:] for t in tokens if t.startswith("-")]
    for token in included:
        if not token:
            continue
        conds.append("(" + " OR ".join(f"UPPER({e}) LIKE ?" for e in exprs) + ")")
        params.extend(f"%{token}%" for _ in exprs)
    for token in excluded:
        if not token:
            continue
        conds.append("(" + " AND ".join(f"UPPER({e}) NOT LIKE ?" for e in exprs) + ")")
        params.extend(f"%{token}%" for _ in exprs)
    return conds, params


def _rank_search_rows(
    rows: list[dict],
    tokens: list[str],
    *,
    title_key: str,
    w5_keys: tuple[str, ...] = (),
    w3_keys: tuple[str, ...] = (),
    w1_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Order candidate rows by Perl's ``fulltextweight`` (descending).

    ``searchQuery`` orders every entity by ``fulltextweight DESC``
    (``Slim/Control/Queries.pm:3597``); the weight is ``_getWeight``
    (``Slim/Plugin/FullTextSearch/Plugin.pm:486-503``) — see
    :func:`lyrion.control.queries.fulltext_weight`.  Python's sort is stable,
    so equal-weight rows keep the DB order, like SQLite does for ties.
    """
    included = [t for t in tokens if not t.startswith("-")]
    if not included:
        return rows

    def weight(row: dict) -> int:
        return fulltext_weight(
            included,
            w10=str(row.get(title_key) or ""),
            w5=tuple(str(row.get(k) or "") for k in w5_keys),
            w3=tuple(str(row.get(k) or "") for k in w3_keys),
            w1=tuple(str(row.get(k) or "") for k in w1_keys),
        )

    return sorted(rows, key=weight, reverse=True)


def _browse_where(filters: dict[str, str], cols: dict[str, Any]) -> tuple[str, tuple]:
    """Build WHERE clause + params for library browse queries.

    cols maps a filter name to an SQL expression, e.g.
    {"search": "t.title", "year": "t.year", "genre": "t.genre",
     "track_id": "t.id", "album_id": "ta.album", "artist_id": "tc.contributor"}.
    ``search`` may be a list of expressions — Perl's search spans several
    columns (``searchQuery`` searches title, artist and album, Queries.pm:3653).

    ``genre_id`` resolves against the (now populated) ``genres`` table;
    ``genre`` keeps the text match for callers that pass a genre name.
    """
    conds: list[str] = []
    params: list[str] = []
    for key, expr in cols.items():
        val = filters.get(key)
        if not val:
            continue
        if key == "search":
            exprs = [expr] if isinstance(expr, str) else list(expr)
            token_conds, token_params = _search_conditions(
                search_tokens(str(val)), exprs)
            conds.extend(token_conds)
            params.extend(token_params)
        elif key == "genre":
            conds.append(f"{expr} LIKE ?")
            params.append(f"%{val}%")
        else:  # year, track_id, album_id, artist_id
            conds.append(f"{expr} = ?")
            params.append(val)
    return (" WHERE " + " AND ".join(conds)) if conds else "", tuple(params)


def _track_search_predicate(tokens: list[str], alias: str = "t") -> tuple[str, list[str]]:
    """WHERE fragment + params for a tokenized track search.

    Perl's FTS indexes a track's title (w10), its album title + genre (w5) and
    its contributors (w3) — ``Slim/Plugin/FullTextSearch/Plugin.pm:31-47`` — and
    a record matches when every token hits at least one of those columns
    (``MATCH 'type:track token1 AND token2'``, :329).  We have no FTS index, so
    each token becomes a ``UNION`` over the four cheap per-table scans instead
    of one 60k-row scan with correlated sub-selects (which is minutes on a real
    library).  A ``-`` token is a ``NOT IN`` (Plugin.pm:454-457).
    """
    def _union(like: str, table: str) -> str:
        return (
            f"SELECT {table}2.id FROM tracks {table}2 "
            f"WHERE UPPER({table}2.title) LIKE ? OR UPPER({table}2.genre) LIKE ? "
            f"UNION SELECT ta2.track FROM tracks_albums ta2 "
            f"JOIN albums al2 ON al2.id = ta2.album WHERE UPPER(al2.title) LIKE ? "
            f"UNION SELECT tc2.track FROM tracks_contributors tc2 "
            f"JOIN contributors c2 ON c2.id = tc2.contributor "
            f"WHERE UPPER(c2.name) LIKE ? AND tc2.role = 1")

    frags: list[str] = []
    params: list[str] = []
    for token in tokens:
        if not token or token.startswith("-"):
            continue
        frags.append(f"{alias}.id IN ({_union(token, alias)})")
        params.extend([f"%{token}%"] * 4)
    for token in tokens:
        if not token.startswith("-") or len(token) < 2:
            continue
        frags.append(f"{alias}.id NOT IN ({_union(token[1:], alias)})")
        params.extend([f"%{token[1:]}%"] * 4)
    return " AND ".join(frags), params


def _track_weight_cols(alias: str = "t") -> str:
    """SELECT suffix for Perl's track relevance columns.

    ``Slim/Plugin/FullTextSearch/Plugin.pm:31-47``: the album title is w5, the
    contributor tuples are w3 and the track title is w10.  The correlated
    sub-selects only run for rows already matched by the candidate predicate
    (:func:`_track_search_predicate`), so they stay cheap.
    """
    return (
        f", (SELECT GROUP_CONCAT(al.title, ' ') FROM tracks_albums ta "
        f"JOIN albums al ON al.id = ta.album WHERE ta.track = {alias}.id) "
        f"AS album_title"
        f", (SELECT GROUP_CONCAT(c.name, ' ') FROM tracks_contributors tc "
        f"JOIN contributors c ON c.id = tc.contributor "
        f"WHERE tc.track = {alias}.id AND tc.role = 1) AS artist_name")


@register_command("artists")
async def cmd_artists(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """artists [<offset> <limit>] [search:|artist_id:|album_id:|year:|genre:] — list artists.

    Perl CLI text format, one line: request terms, the loop items unrolled
    inline, then ``count`` (``artistsQuery``; live probe 2026-09-12:
    ``artists 0 1 id%3A5620 artist%3A%3F favorites_url%3Adb%3A… count%3A11151``).
    """
    if _is_query_echo(args):
        return [render_line(terms=["artists", *args])]
    offset, limit, filters = _parse_query_args(args)
    where, params = _browse_where(filters, {
        "search": "c.name", "artist_id": "tc.contributor",
        "album_id": "ta.album", "year": "t.year", "genre": "t.genre",
    })
    joins = " JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
    if filters.get("album_id") or filters.get("year") or filters.get("genre"):
        joins += " JOIN tracks t ON t.id = tc.track"
    if filters.get("album_id"):
        joins += " JOIN tracks_albums ta ON ta.track = t.id"
    rows = await _query_db(
        "SELECT DISTINCT c.id, c.name FROM contributors c" + joins + where +
        " ORDER BY c.name COLLATE NOCASE LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    total = await _query_db(
        "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c" + joins + where,
        params,
    )
    total_n = total[0]["n"] if total else 0
    # Perl: the loop items are appended first, 'count' afterwards
    # (albumsQuery, Queries.pm:989); kind name = '<verb>_loop' (Request.pm:2264).
    return [
        render_line(
            terms=["artists"],
            params=query_params(args, ["_index", "_quantity"]),
            results=[
                ("artists_loop", [{"id": r["id"], "artist": r["name"] or ""} for r in rows]),
                ("count", total_n),
            ],
        )
    ]


@register_command("albums")
async def cmd_albums(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """albums [<offset> <limit>] [search:|album_id:|artist_id:|year:|genre:] — list albums.

    Perl CLI text format, one line (live probe 2026-09-12: ``albums 0 1 id%3A45
    performance%3A favorites_url%3A… album%3AKein%20Album count%3A7181`` — the
    loop items first, ``count`` last, Queries.pm:989).
    """
    if _is_query_echo(args):
        return [render_line(terms=["albums", *args])]
    offset, limit, filters = _parse_query_args(args)
    where, params = _browse_where(filters, {
        "search": "al.title", "album_id": "al.id", "artist_id": "tc.contributor",
        "year": "al.year", "genre": "t.genre",
    })
    joins = ""
    if filters.get("artist_id") or filters.get("genre"):
        joins += " JOIN tracks_albums ta ON ta.album = al.id" \
                 " JOIN tracks t ON t.id = ta.track"
    if filters.get("artist_id"):
        joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
    rows = await _query_db(
        "SELECT DISTINCT al.id, al.title, al.year FROM albums al" + joins + where +
        " ORDER BY al.title COLLATE NOCASE LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    total = await _query_db(
        "SELECT COUNT(DISTINCT al.id) AS n FROM albums al" + joins + where,
        params,
    )
    total_n = total[0]["n"] if total else 0
    loop = []
    for r in rows:
        entry: dict[str, Any] = {"id": r["id"], "album": r["title"] or ""}
        if r["year"]:
            entry["year"] = r["year"]
        loop.append(entry)
    return [
        render_line(
            terms=["albums"],
            params=query_params(args, ["_index", "_quantity"]),
            results=[("albums_loop", loop), ("count", total_n)],
        )
    ]


@register_command("songs")
@register_command("titles")
async def cmd_songs(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """songs [<offset> <limit>] [search:|track_id:|album_id:|artist_id:|year:|genre:] — list tracks.

    Perl CLI text format, one line (live probe 2026-09-12: ``songs 0 1 id%3A6417
    title%3A!!!!!!! genre%3AAlternative artist%3ABillie%20Eilish album%3A… 
    duration%3A13.609 count%3A80134``).  'titles' is a registered alias and is
    echoed as invoked (Perl dispatches both, Request.pm:617/628).

    With a ``search:`` term the hits are ordered by Perl's relevance weight
    (``searchQuery``: ``ORDER BY quickSearch.fulltextweight DESC``,
    ``Slim/Control/Queries.pm:3597``; the weight is ``_getWeight``,
    ``Slim/Plugin/FullTextSearch/Plugin.pm:486-503``) and the search spans
    title, album, artist and genre (LIB-16/17).
    """
    name = getattr(ctx, "command", "") or "songs"
    if _is_query_echo(args):
        return [render_line(terms=[name, *args])]
    offset, limit, filters = _parse_query_args(args)
    search = filters.get("search", "")
    other = {k: v for k, v in filters.items() if k != "search"}
    where, params = _browse_where(other, {
        "track_id": "t.id", "album_id": "ta.album",
        "artist_id": "tc.contributor", "year": "t.year", "genre": "t.genre",
    })
    joins = ""
    if other.get("album_id"):
        joins += " JOIN tracks_albums ta ON ta.track = t.id"
    if other.get("artist_id"):
        joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
    if search:
        pred, pred_params = _track_search_predicate(search_tokens(search))
        if pred:
            where = (where + " AND " if where else " WHERE ") + pred
            params = tuple(params) + tuple(pred_params)
    rows = await _query_db(
        "SELECT DISTINCT t.id, t.title, t.genre, t.year, t.tracknum, t.duration"
        + _track_weight_cols("t") + " FROM tracks t"
        + joins + where +
        (" ORDER BY t.id LIMIT ? OFFSET ?" if search
         else " ORDER BY t.title COLLATE NOCASE LIMIT ? OFFSET ?"),
        params + (limit, offset),
    )
    if search:
        rows = _rank_search_rows(
            rows, search_tokens(search), title_key="title",
            w5_keys=("album_title", "genre"), w3_keys=("artist_name", "comment"))
    total = await _query_db(
        "SELECT COUNT(DISTINCT t.id) AS n FROM tracks t" + joins + where,
        params,
    )
    total_n = total[0]["n"] if total else 0
    loop = []
    for r in rows:
        entry: dict[str, Any] = {"id": r["id"], "title": r["title"] or ""}
        if r.get("genre"):
            entry["genre"] = r["genre"]
        if r.get("year"):
            entry["year"] = r["year"]
        if r.get("tracknum"):
            entry["tracknum"] = r["tracknum"]
        if r.get("duration"):
            entry["duration"] = int(r["duration"])
        loop.append(entry)
    return [
        render_line(
            terms=[name],
            params=query_params(args, ["_index", "_quantity"]),
            results=[(f"{name}_loop", loop), ("count", total_n)],
        )
    ]


@register_command("genres")
async def cmd_genres(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """genres [<offset> <limit>] [search:] — list genres with real ids (LIB-10).

    Perl CLI text format, one line (live probe 2026-09-12: ``genres 0 1
    id%3A497 genre%3A favorites_url%3Adb%3Agenre.name%3D count%3A762``).
    ``genresQuery`` selects ``DISTINCT(genres.id), genres.name,
    genres.namesort`` and orders by ``namesort``
    (``Slim/Control/Queries.pm:1910-1911``) — the ids are the real
    ``genres`` table ids (``Slim/Schema/Genre.pm:20-33``), not an offset
    into a DISTINCT track-text list.
    """
    if _is_query_echo(args):
        return [render_line(terms=["genres", *args])]
    offset, limit, filters = _parse_query_args(args)

    conds: list[str] = []
    params: list[str] = []
    search = filters.get("search", "")
    if search:
        # No FTS path for genres (Plugin.pm:3516): Perl matches the
        # namesearch column with searchStringSplit word prefixes. Die Muster
        # EINES Suchbegriffs werden mit OR zu EINER Bedingung verknüpft:
        # `push @{$w}, '(' . join(' OR ', map { 'genres.namesearch LIKE ?' } ...)
        # . ')'` (Queries.pm:1814-1823, vgl. :1817); die äußeren Bedingungen
        # (z. B. genre_id) hängen weiter mit AND dran. Vorher stand hier ein
        # AND je Muster → jede Mehrwortsuche lieferte 0 Treffer.
        patterns = search_string_split(search)
        if patterns:
            conds.append("(" + " OR ".join("g.namespell LIKE ?" for _ in patterns)
                         + ")")
            params.extend(patterns)
    genre_id = filters.get("genre_id", "")
    if genre_id:
        conds.append("g.id = ?")
        params.append(genre_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""

    rows = await _query_db(
        "SELECT g.id AS id, g.name AS name, g.sortkey AS namesort FROM genres g"
        + where + " ORDER BY g.sortkey COLLATE NOCASE LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    total = await _query_db(
        "SELECT COUNT(*) AS n FROM genres g" + where, tuple(params))
    total_n = total[0]["n"] if total else 0
    return [
        render_line(
            terms=["genres"],
            params=query_params(args, ["_index", "_quantity"]),
            results=[
                ("genres_loop", [
                    {"id": r.get("id"), "genre": r.get("name") or ""}
                    for r in rows
                ]),
                ("count", total_n),
            ],
        )
    ]


@register_command("playlists")
async def cmd_playlists(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlists [<index> <quantity>] | playlists <new|delete|rename|tracks> — ONE line.

    ``addDispatch(['playlists','_index','_quantity'],[0,1,1,playlistsQuery])``
    (``Slim/Control/Request.pm:593``); ``playlistsQuery`` (Queries.pm:2855-2900)
    fills ``playlists_loop`` with the keys ``id`` and ``playlist`` (:2888-2889)
    and adds ``count`` *after* the loop (:2930).  ``playlists tracks`` is a query
    of its own (``playlistsTracksQuery``, Request.pm:598) and uses the loop name
    ``playlisttracks_loop`` with ``count`` last (Queries.pm:2776-2850).
    ``playlists new`` (Commands.pm:playlistsNewCommand) adds ``playlist_id`` /
    ``overwritten_playlist_id``, ``rename`` (playlistsRenameCommand) likewise.
    Live Perl 9.1.1, read-only, 2026-09-12: ``playlists 0 2`` →
    ``playlists 0 2 rescan%3A1`` (nothing indexed while the scan runs).
    """
    if not args:
        args = ["0", "100"]
    sub = str(args[0]).lower() if args else ""
    if sub == "tracks" and len(args) >= 2 and str(args[1]).isdigit():
        pid = int(args[1])
        rows = await _query_db(
            "SELECT pi.position, pi.track, pi.url, t.title, t.duration "
            "FROM playlist_items pi LEFT JOIN tracks t ON t.id = pi.track "
            "WHERE pi.playlist = ? ORDER BY pi.position",
            (pid,),
        )
        loop = []
        for r in rows:
            entry: dict[str, Any] = {}
            if r["title"]:
                entry["title"] = r["title"]
            if r["url"]:
                entry["url"] = r["url"]
            if r["duration"]:
                entry["duration"] = int(r["duration"])
            loop.append(entry)
        return _command_line(["playlists", "tracks", str(pid)], [], [],
                             results=[("playlisttracks_loop", loop),
                                      ("count", len(rows))])
    if sub == "new" and len(args) >= 2:
        name = " ".join(str(a) for a in args[1:])
        if name.startswith("name:"):
            name = name[5:]
        if not name.strip():
            return _command_echo(["playlists", "new"], args[1:], [], has_tags=True)
        # remote/disabled are NOT NULL without DB defaults — omitting them
        # raised IntegrityError on every 'playlists new'.
        await _write_db(
            "INSERT INTO playlists (playlist, name, changed, pl_type, "
            "remote, disabled) VALUES (?, ?, datetime('now'), 0, 0, 0)",
            (name, name),
        )
        row = await _query_db(
            "SELECT id FROM playlists WHERE name = ? ORDER BY id DESC LIMIT 1", (name,)
        )
        new_id = row[0]["id"] if row else "?"
        return _command_line(["playlists", "new"], args[1:], [], has_tags=True,
                             results=[("playlist_id", new_id)])
    if sub == "delete" and len(args) >= 2 and str(args[1]).isdigit():
        pid = int(args[1])
        await _write_db("DELETE FROM playlist_items WHERE playlist = ?", (pid,))
        await _write_db("DELETE FROM playlists WHERE id = ?", (pid,))
        return _command_echo(["playlists", "delete"], args[1:], [], has_tags=True)
    if sub == "rename" and len(args) >= 3 and str(args[1]).isdigit():
        pid = int(args[1])
        name = " ".join(str(a) for a in args[2:])
        await _write_db(
            "UPDATE playlists SET name = ?, playlist = ? WHERE id = ?",
            (name, name, pid),
        )
        return _command_echo(["playlists", "rename"], args[1:], [], has_tags=True)
    # default: list playlists
    offset, limit, _ = _parse_query_args(args)
    rows = await _query_db(
        "SELECT id, name FROM playlists ORDER BY name COLLATE NOCASE "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    )
    total = await _query_db("SELECT COUNT(*) AS n FROM playlists")
    total_n = total[0]["n"] if total else 0
    loop = [{"id": r["id"], "playlist": r["name"]} for r in rows]
    return _command_line(
        ["playlists"], args, ["_index", "_quantity"], has_tags=True,
        results=[("playlists_loop", loop), ("count", total_n)],
    )


# ---------------------------------------------------------------------------
# Internet radio
# ---------------------------------------------------------------------------


def _station_loop_entry(s: Any) -> dict[str, Any]:
    """One radio-station item for the CLI loop (our own shape).

    Perl has no 'radio' CLI request (``Slim/Control/Request.pm:474-637``);
    radio lives in ``Slim::Plugin::InternetRadio``.  The keys follow the
    browse convention Perl uses for plugin menus (``item_loop`` items with
    ``id``/``name``/``url``), so a line stays parsable like every other answer.
    """
    entry: dict[str, Any] = {
        "id": s.id if getattr(s, "id", None) is not None else "-",
        "name": getattr(s, "name", "") or "",
        "url": getattr(s, "url", "") or "",
    }
    for field in ("genre", "country", "bitrate", "codec"):
        val = getattr(s, field, None)
        if val:
            entry[field] = val
    return entry


@register_command("radio")
async def cmd_radio(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio [list|add|delete|search|top|play] — ONE escaped line per answer.

    Perl has no 'radio' request (``Slim/Control/Request.pm:474-637``); the
    internet-radio menu is served by ``Slim::Plugin::InternetRadio``.  Every
    answer here therefore follows the Perl *wire* shape of the browse menus
    (request terms + ``count`` + an unrolled ``item_loop``,
    ``Slim/Control/Request.pm:2264-2281``).
    """
    if args and not _is_query_echo(args) and str(args[0]).isdigit():
        # 'radio <n> …' from the browse menus → treat the number as the index.
        return await cmd_radio_list(handler, ctx, args)
    sub = str(args[0]).lower() if args else "list"
    rest = args[1:] if args else []

    if sub in ("list", "ls", "") or sub.isdigit():
        return await cmd_radio_list(handler, ctx, rest)
    if sub == "add":
        return await cmd_radio_add(handler, ctx, rest)
    if sub == "delete" or sub == "remove":
        return await cmd_radio_delete(handler, ctx, rest)
    if sub == "search" or sub == "find":
        return await cmd_radio_search(handler, ctx, rest)
    if sub == "top" or sub == "popular":
        return await cmd_radio_top(handler, ctx, rest)
    if sub == "play":
        return await cmd_radio_play(handler, ctx, rest)
    return _command_echo(["radio", sub], rest, [])


async def cmd_radio_list(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio list — ONE line: ``radio list <index> <quantity> count:<n> <loop>``."""
    from lyrion.music.radio import get_radio_manager

    try:
        stations = await get_radio_manager().list_stations()
    except Exception:  # noqa: BLE001
        stations = []
    offset, limit, _ = _parse_query_args(args)
    page = stations[offset:offset + limit]
    return _command_line(
        ["radio", "list"], args, ["_index", "_quantity"], has_tags=True,
        results=[("count", len(stations)),
                 ("item_loop", [_station_loop_entry(s) for s in page])],
    )


# Sub-command "radio add" — routed via the base handler, not the registry.
async def cmd_radio_add(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio add <url> [name] — ONE line (our own verb; Perl has no 'radio')."""
    if not args:
        return _command_echo(["radio", "add"], args, [])
    url = str(args[0])
    name = " ".join(str(a) for a in args[1:]) if len(args) > 1 else url
    new_id: Any = ""
    try:
        from lyrion.music.radio import get_radio_manager

        station = await get_radio_manager().add_station(name, url)
        new_id = station.id if station.id is not None else ""
    except Exception:  # noqa: BLE001
        pass
    return _command_line(["radio", "add"], args, [],
                         results=[("id", new_id)])


# Sub-command "radio delete" — routed via the base handler, not the registry.
async def cmd_radio_delete(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio delete <id> — ONE line (our own verb; Perl has no 'radio')."""
    if not args or not str(args[0]).isdigit():
        return _command_echo(["radio", "delete"], args, [])
    try:
        from lyrion.music.radio import get_radio_manager

        await get_radio_manager().remove_station(int(str(args[0])))
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["radio", "delete"], args, [])


# Sub-command "radio search" — routed via the base handler, not the registry.
async def cmd_radio_search(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio search <query> [limit] [tag:<tag>] [country:<CC>] — ONE line."""
    if not args:
        return _command_echo(["radio", "search"], args, [], has_tags=True)
    name_parts: list[str] = []
    tag = ""
    country = ""
    limit = 20
    for a in args:
        s = str(a)
        if s.lower().startswith("tag:") and len(s) > 4:
            tag = s[4:]
        elif s.lower().startswith("country:") and len(s) > 8:
            country = s[8:]
        elif s.isdigit():
            limit = min(int(s), 100)
        else:
            name_parts.append(s)
    query = " ".join(name_parts).strip()
    stations: list[Any] = []
    try:
        from lyrion.music.radio import get_radio_manager

        stations = await get_radio_manager().directory.search(
            name=query, tag=tag, country=country, limit=limit
        )
    except Exception:  # noqa: BLE001
        stations = []
    return _command_line(
        ["radio", "search"], args, ["_index", "_quantity"], has_tags=True,
        results=[("count", len(stations)),
                 ("item_loop", [_station_loop_entry(s) for s in stations])],
    )


# Sub-command "radio top" — routed via the base handler, not the registry.
async def cmd_radio_top(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio top [n] — ONE line (our own verb; Perl has no 'radio')."""
    limit = 20
    if args and str(args[0]).isdigit():
        limit = min(int(str(args[0])), 100)
    stations: list[Any] = []
    try:
        from lyrion.music.radio import get_radio_manager

        stations = await get_radio_manager().directory.top(limit=limit)
    except Exception:  # noqa: BLE001
        stations = []
    return _command_line(
        ["radio", "top"], args, ["_index", "_quantity"], has_tags=True,
        results=[("count", len(stations)),
                 ("item_loop", [_station_loop_entry(s) for s in stations])],
    )


# Sub-command "radio play" — routed via the base handler, not the registry.
async def cmd_radio_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio play <station_id> [player_id] — ONE line (our own verb)."""
    if not args or not str(args[0]).isdigit():
        return _command_echo(["radio", "play"], args, [], has_tags=True)
    station_id = int(str(args[0]))
    player_id = str(args[1]) if len(args) > 1 else ctx.player_id
    station: Any = None
    if player_id:
        try:
            from lyrion.music.radio import get_radio_manager

            station = await get_radio_manager().play_station(
                player_id, station_id=station_id)
        except Exception:  # noqa: BLE001
            station = None
    results: list[tuple[str, Any]] = []
    if station is not None:
        results = [("name", getattr(station, "name", "") or ""),
                   ("url", getattr(station, "url", "") or "")]
    return _command_line(["radio", "play"], args, [], has_tags=True,
                         results=results)


# ---------------------------------------------------------------------------
# Radio directory sub-feeds (Perl's dynamically registered OPMLBased plugins)
# ---------------------------------------------------------------------------
# Perl's InternetRadio plugin creates ONE *dynamic* OPMLBased plugin per TuneIn
# directory item (``Slim/Plugin/InternetRadio/Plugin.pm:78-205``, tag =>
# lc(subclass)); every one of them registers the same CLI dispatch pair
# (``Slim/Plugin/OPMLBased.pm:108-133``):
#
#     [ $args{tag}, 'items', '_index', '_quantity' ]   [1, 1, 1, $cliQuery]
#     [ $args{tag}, 'playlist', '_method' ]            [1, 1, 1, $cliQuery]
#
# plus ``[ $args{menu}, '_index', '_quantity' ]`` for the ``radios`` menu
# itself (``:129-132``, handler ``cliRadiosQuery`` :181-280).  Both point at
# ``Slim::Control::XMLBrowser::cliQuery`` (``:111-114``) — the SAME handler the
# JSON transports use (``Jive.pm:1359-1385``) — so the CLI answer differs from
# the JSON answer only in its *serialization* (``Request.pm:2226-2296``).
# This port therefore renders the result dict the JSON-RPC path builds
# (``JSONRPCAPI._slim_request``, exactly the pattern :func:`cmd_menu` uses)
# through the CLI renderer instead of re-implementing the feed walk.
#
# Client prefix (Perl ``Slim/Plugin/CLI/Plugin.pm:581-585``): the ``items`` /
# ``playlist`` dispatches are ``needsClient``, so a line without a player token
# makes the CLI allocate ``Slim::Player::Client::clientRandom()``
# (``Slim/Player/Client.pm:411-415`` = ``(clients())[0]``) and prefix that id
# (``Plugin.pm:694`` → ``Stdio.pm:131``); with no client at all the prefix is
# omitted.  ``radios`` is registered with ``needsClient = 0`` and therefore
# never gets one.  Live Perl 9.1.1, read-only 2026-09-18::
#
#     radios 0 20                         → radios 0 20 sort%3Aweight count%3A10 …
#                                           (no prefix)
#     local items 0 20                    → 24:0a:c4:29:77:90 local items 0 20 …
#                                           (the only client's MAC, clientRandom)
#     local items 0 20 menu:local         → <mac> local items 0 20 menu%3Alocal
#                                           offset%3A0 title%3A… base%3AHASH(0x…)
#                                           … count%3A2 window%3AHASH(0x…)
#     local items 0 20                    → <mac> local items 0 20
#                                           title%3ALokale%20Sender id%3A<sid>.0
#                                           name%3ASender isaudio%3A0 hasitems%3A1 …
#                                           count%3A2
#     local playlist play menu:local item_id:<sid>.0.0
#                                         → <mac> local playlist play menu%3Alocal
#                                           item_id%3A<sid>.0.0   (echo, no results)
#     local 0 3                           → local 0 3   (nothing dispatches a
#                                           bare tag → status 104 echo)
#     search items 0 20                   → <mac> search items 0 20 count%3A0
#     doctor items 0 20                   → doctor items 0 20  (no such plugin)

#: CLI tags of the radio sub-feeds this module registers.  Identical to
#: :data:`lyrion.web.radiobrowser.RADIO_FEEDS` (Perl's TuneIn ``MENUS`` table
#: plus the ``Sounds`` app feed) minus ``search``, which this module already
#: registers as the library search — Perl's radio search plugin owns a
#: ``['search','items',…]`` leaf that wins over the library entry
#: (``Request.pm:1010-1014``), so that case is branched inside
#: :func:`cmd_search`.  ``tests/test_cli_radio_feeds.py`` asserts the identity.
_RADIO_FEED_TAGS: tuple[str, ...] = (
    "presets", "local", "music", "sports", "news", "talk", "location",
    "language", "podcast", "sounds",
)


def _radio_feed_clientid(ctx: CLIContext, *, needs_client: bool = True) -> Optional[str]:
    """``$request->clientid`` of a radio request (``Plugin/CLI/Plugin.pm:581-585``).

    ``ctx.request_clientid`` is the player token the request line started with
    (``Stdio.pm:96-116``); ``ctx.player_id`` is this port's ``clientRandom``
    (the first connected client, ``RequestDispatcher.get_default_player`` —
    see ``tests/test_cli_default_player.py``).  ``needs_client=False``
    (the ``radios`` dispatch, flags ``[0, 1, 1, …]``) never allocates one.
    """
    if ctx.request_clientid:
        return ctx.request_clientid
    return ctx.player_id if needs_client else None


def _as_dict(value: Any) -> dict[str, Any]:
    """``value`` when it is a dict, else an empty one (item-row plumbing)."""
    return value if isinstance(value, dict) else {}


def _flat_row(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One ``<feed> items`` row in XMLBrowser's *non-menu* shape.

    Without a ``menu:`` token Perl renders the flat OPML rows
    (``Slim/Control/XMLBrowser.pm:1378-1413``, a ``Tie::IxHash`` in this exact
    field order): ``id`` (:1382, set unconditionally), ``name`` (:1383),
    ``type`` (:1384), ``title`` (:1385), ``image`` (:1386-1387 — the *proxied*
    OPML image/icon), ``isaudio`` (:1394 ``defined(hasAudio($item)) + 0``) and
    ``hasitems`` (:1396-1410: ``1`` for every type that is not
    ``text``/``audio``).  Live Perl 9.1.1, read-only 2026-09-18::

        local items 0 4 item_id:<sid>.0   →  title%3ASender id%3A<sid>.0.0
        name%3A… type%3Aaudio image%3A%2Fimageproxy%2F… isaudio%3A1 hasitems%3A0 …

    ``None`` means "Perl would not print this row": the ``EMPTY`` placeholder
    is built in the *menu* branch only (:841-846 — the flat answer of an empty
    feed is ``count%3A0`` alone, live ``search items 0 20``).
    """
    actions = _as_dict(item.get("actions"))
    go = _as_dict(actions.get("go"))
    params = _as_dict(item.get("params"))
    item_id = params.get("item_id")
    if item_id is None:
        item_id = _as_dict(go.get("params")).get("item_id")
    if item_id is None and str(item.get("type") or "") == "text":
        return None
    kind = item.get("type")
    row: dict[str, Any] = {"id": item_id if item_id is not None else ""}
    if item.get("text") is not None:
        row["name"] = item["text"]
    if kind is not None:
        row["type"] = kind
    if item.get("title") is not None:
        row["title"] = item["title"]
    image = item.get("icon") or item.get("icon-id")
    if image:
        row["image"] = image
    row["isaudio"] = 1 if kind == "audio" else 0
    row["hasitems"] = 0 if kind in ("text", "audio") else 1
    return row


async def _radio_feed_answer(
    tag: str,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """``<tag> items …`` / ``<tag> playlist …`` — one radio sub-feed answer.

    See the module section above for the Perl dispatch this mirrors.  The
    result dict comes from the shared request pipeline
    (``JSONRPCAPI._slim_request``), so every protocol answers the same data;
    the CLI shape rules are ``Request.pm:2264-2291`` (loops unrolled inline,
    refs interpolated as ``HASH(0x…)``, tokens percent-escaped).
    """
    sub = str(args[0]).lower() if args else ""
    if sub not in ("items", "playlist"):
        # Penalty for a request no Perl dispatch matches: status 104, echoed
        # verbatim, without an allocated client (Plugin/CLI/Plugin.pm:657-663;
        # live `local 0 3` → `local 0 3`).  A line that *did* start with a
        # player token keeps its prefix (Stdio.pm:111-116 + :131).
        return _echo(tag, args, clientid=ctx.request_clientid)

    rest = [str(a) for a in args[1:]]
    clientid = _radio_feed_clientid(ctx)

    from lyrion.web.api import JSONRPCAPI

    res = await JSONRPCAPI()._slim_request(clientid or "", [tag, sub, *rest])

    if sub == "playlist":
        # cliQuery marks that request (``XMLBrowser.pm:291-293``), walks the
        # row's item path and hands its URL to the player (:778-790), then
        # answers ``setStatusDone()`` without a result body (:1512).  Live
        # Perl: `local playlist play menu:local item_id:<sid>.0.0` →
        # `<mac> local playlist play menu%3Alocal item_id%3A<sid>.0.0`.
        return _command_line([tag, "playlist"], rest, ["_method"],
                             clientid=clientid, has_tags=True)

    if not isinstance(res, dict):  # pragma: no cover - _slim_request answers dicts
        return _echo(tag, args, clientid=ctx.request_clientid)

    menu_mode = any(a.startswith("menu:") for a in rest)
    results: list[tuple[str, Any]] = []
    if menu_mode:
        # The jive/menu envelope (``XMLBrowser.pm:1420-1452``) as the CLI
        # prints it: ``offset``/``title`` first, then ``base``/``window`` as
        # *references* (Perl interpolates the hash refs, :2291 — the live
        # answers carry exactly those addresses), the unrolled ``item_loop``
        # and ``count`` (``$totalCount``, :1420).
        if res.get("offset") is not None:
            results.append(("offset", res["offset"]))
        if res.get("title"):
            results.append(("title", res["title"]))
        if "base" in res:
            results.append(("base", res["base"]))
        results.append(("item_loop", res.get("item_loop") or []))
        if res.get("count") is not None:
            results.append(("count", res["count"]))
        if "window" in res:
            results.append(("window", res["window"]))
    else:
        # Flat form: ``title`` first, one row per item, ``count`` last — also
        # for a feed without rows (``XMLBrowser.pm:1420`` adds ``count`` to
        # every answer; the loop simply stays empty).
        if res.get("title"):
            results.append(("title", res["title"]))
        raw = res.get("item_loop") or []
        rows = [r for r in (_flat_row(i) for i in raw) if r is not None]
        if rows:
            results.append(("item_loop", rows))
            results.append(("count", res.get("count") if res.get("count") is not None
                            else len(rows)))
        else:
            # The only rows were the menu-only ``Leer`` placeholder, so Perl's
            # flat answer is the empty feed: ``count:0`` and no row at all
            # (live ``search items 0 20`` → ``count%3A0``).
            results.append(("count", 0))

    return _command_line([tag, "items"], rest, ["_index", "_quantity"],
                         clientid=clientid, has_tags=True, results=results)


@register_command("radios")
async def cmd_radios(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radios [<start> <count>] [menu:…] — ONE line, the loop unrolled.

    Perl: ``OPMLBased.pm:129-132`` (``cliRadiosQuery`` :181-280) over
    ``dynamicAutoQuery``, whose loop is ``$query . 's_loop'`` →
    ``radioss_loop`` in the plain form and ``item_loop`` with a ``menu:``
    token (``Queries.pm:5403``), ``sort:weight`` as a request *parameter*
    (``OPMLBased.pm:193``) and ``count`` last (:5443).  ``[0, 1, 1, …]`` means
    needsClient = 0 → no client prefix (live ``radios 0 20`` → ``radios 0 20
    sort%3Aweight count%3A10 …``, 2026-09-18).
    """
    clientid = _radio_feed_clientid(ctx, needs_client=False)

    from lyrion.web.api import JSONRPCAPI

    res = await JSONRPCAPI()._slim_request(
        clientid or "", ["radios", *[str(a) for a in args]])
    if not isinstance(res, dict):  # pragma: no cover - _slim_request answers dicts
        return _echo("radios", args, clientid=clientid)

    menu_mode = any(str(a).startswith("menu:") for a in args)
    loop_key = "item_loop" if menu_mode else "radioss_loop"
    results: list[tuple[str, Any]] = [("sort", "weight")]
    if res.get("count") is not None:
        results.append(("count", res["count"]))
    rows = res.get(loop_key)
    if rows:
        results.append((loop_key, rows))
    return _command_line(["radios"], args, [], clientid=clientid,
                         has_tags=True, results=results)


def _register_radio_feed(tag: str) -> None:
    """Register one ``<tag> items …`` dispatch (``OPMLBased.pm:117-120``)."""

    async def _handler(
        handler: CLIHandler,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        return await _radio_feed_answer(tag, ctx, args)

    _handler.__name__ = f"cmd_radio_feed_{tag}"
    _handler.__doc__ = (
        f"``{tag} items <start> <count> [params…]`` — Perl's ``{tag}`` "
        "OPMLBased sub-feed (``Slim/Plugin/OPMLBased.pm:108-133``)."
    )
    register_command(tag)(_handler)


def _register_radio_feeds() -> None:
    """Register every radio sub-feed tag (``OPMLBased.pm:117-120``)."""
    for tag in _RADIO_FEED_TAGS:
        _register_radio_feed(tag)


_register_radio_feeds()


# ---------------------------------------------------------------------------
# Favorites (radio streams + folders, original LMS Favorites logic)
# ---------------------------------------------------------------------------


def _parse_parent_id(value: str) -> Optional[int]:
    """Parse a parent id — '0'/'-' means root (None)."""
    s = str(value).strip()
    if s in ("", "0", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


@register_command("favorites")
async def cmd_favorites(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    favorites [items|add|addfolder|delete|move|rename|play|exists|playlist]

    Manage radio favorites with nested folders (original LMS logic):
      favorites items [parent_id] [want_url:1]
      favorites add <url> <title> [parent_id] | add url:<u> title:<t> [parent:<id>]
      favorites addfolder <title> [parent_id]
      favorites delete <id>
      favorites move <id> <parent_id> [position]
      favorites rename <id> <title>
      favorites play <id> [player_id]
      favorites exists <url|id>
      favorites playlist <play|load|insert|add> item_id:<id>
    """
    sub = str(args[0]).lower() if args else "items"
    rest = args[1:] if args else []

    if sub in ("items", "list", "") or sub.isdigit():
        return await _fav_items(handler, ctx, rest)
    if sub == "add":
        return await _fav_add(handler, ctx, rest)
    if sub in ("addfolder", "folder", "mkdir"):
        return await _fav_add_folder(handler, ctx, rest)
    if sub in ("delete", "remove"):
        return await _fav_delete(handler, ctx, rest)
    if sub == "move":
        return await _fav_move(handler, ctx, rest)
    if sub == "rename":
        return await _fav_rename(handler, ctx, rest)
    if sub == "play":
        return await _fav_play(handler, ctx, rest)
    if sub == "exists":
        return await _fav_exists(handler, ctx, rest)
    if sub == "playlist":
        return await _fav_playlist(handler, ctx, rest)
    return _command_echo(["favorites", sub], rest, [])


async def _fav_items(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites items [<parent>] [want_url:1] — ONE line, ``item_loop`` inline.

    ``Slim::Plugin::Favorites::Plugin`` builds the CLI answer like every other
    browse menu (``Slim/Control/Request.pm:2264-2281``); live Perl 9.1.1,
    read-only, 2026-09-12::

        favorites items 0 5 title%3AFavorites id%3Aab9c31e0.0 name%3AChill
        image%3Ahtml%2Fimages%2Ffavorites.png isaudio%3A0 hasitems%3A1 …
        count%3A8

    i.e. the menu ``title`` first, then one ``item_loop`` run per entry with
    ``id``/``name``/``image``/``isaudio``/``hasitems``, then ``count`` last.
    """
    parent = _parse_parent_id(args[0]) if args else None
    try:
        from lyrion.music.favorites import get_favorites_manager

        items = await get_favorites_manager().list_items(parent)
    except Exception:  # noqa: BLE001
        items = []
    loop = []
    # Perl's ``image`` of a favourites row is ``proxiedImage($item->{icon})``
    # (``XMLBrowser.pm:1386-1387``; the entry has no ``image`` of its own) —
    # a stored ``/imageproxy/…`` path stays as it is, a raw ``http…`` logo
    # becomes the same proxied route the JSON shapes hand out.
    from lyrion.web.radiobrowser import proxied_image

    # Perl files the logo of every rendered playable row under the row's URL
    # while the list is built (``XMLBrowser.pm:1043-1049``); the CLI list goes
    # through the same ``Slim::Control::XMLBrowser::cliQuery`` item loop
    # (``Plugin/Favorites/Plugin.pm:75``/``:763``).  Without it a favourite that
    # is only ADDED to the playlist (never played) answered the handler's
    # ``html/images/radio.png`` placeholder instead of its row logo.  One
    # helper, shared with the jive/JSON-RPC list (``web/api.py``).
    player = None
    if ctx.player_id:
        try:
            from lyrion.player.manager import PlayerManager

            player = PlayerManager().get_player(ctx.player_id)
        except Exception:  # noqa: BLE001 — ohne Player bleibt die Liste ungecacht
            player = None
    try:
        from lyrion.web.api import _register_favorites_row_images

        _register_favorites_row_images(player, items)
    except Exception:  # noqa: BLE001
        pass

    for item in items:
        is_folder = item.get("type") == "folder"
        loop.append({
            "id": item["id"],
            "name": item["title"],
            # The OPML ``icon`` of the entry (``OpmlFavorites.pm:133-136``);
            # live Perl answers it for folders over the CLI too (docstring).
            "image": proxied_image(item.get("icon")
                                   or "html/images/favorites.png"),
            "isaudio": 0 if is_folder else 1,
            "hasitems": 1 if is_folder else 0,
        })
    return _command_line(
        ["favorites", "items"], args, ["_index", "_quantity"], has_tags=True,
        results=[("title", "Favorites"), ("item_loop", loop),
                 ("count", len(items))],
    )


async def _fav_add(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites add … — ONE escaped line (the new id as a result).

    ``Slim::Plugin::Favorites::Plugin`` reports the created item through the
    request results; Perl's CLI never prints a multi-line answer
    (``Slim/Control/Request.pm:2226-2296``).
    """
    if not args:
        return _command_echo(["favorites", "add"], args, [], has_tags=True)
    filters: dict[str, str] = {}
    positional: list[str] = []
    for a in args:
        s = str(a)
        if ":" in s and s.split(":", 1)[0] in ("url", "title", "parent", "item_id"):
            k, _, v = s.partition(":")
            filters[k] = v
        else:
            positional.append(s)
    if filters.get("url") and filters.get("title"):
        url = filters["url"]
        title = filters["title"]
        parent = _parse_parent_id(filters.get("parent")) if filters.get("parent") else None
    elif len(positional) >= 2:
        url = positional[0]
        title = positional[1]
        parent = _parse_parent_id(positional[2]) if len(positional) > 2 else None
    else:
        return _command_echo(["favorites", "add"], args, [], has_tags=True)
    new_id: Any = ""
    try:
        from lyrion.music.favorites import get_favorites_manager

        new_id = await get_favorites_manager().add(title, url, parent)
    except Exception:  # noqa: BLE001
        new_id = None
    return _command_line(["favorites", "add"], args, [], has_tags=True,
                         results=[("id", new_id if new_id is not None else "")])


async def _fav_add_folder(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites addfolder <title> [parent_id] — ONE escaped line."""
    if not args:
        return _command_echo(["favorites", "addfolder"], args, [], has_tags=True)
    title = str(args[0])
    parent = _parse_parent_id(args[1]) if len(args) > 1 else None
    new_id: Any = ""
    try:
        from lyrion.music.favorites import get_favorites_manager

        new_id = await get_favorites_manager().add(title, None, parent)
    except Exception:  # noqa: BLE001
        new_id = None
    return _command_line(["favorites", "addfolder"], args, [], has_tags=True,
                         results=[("id", new_id if new_id is not None else "")])


async def _fav_resolve_id(fm: Any, val: str) -> Optional[int]:
    """Resolve a favorite id: LMS hierarchical path ('0.3.1') or DB id."""
    if "." in val:
        return await fm.resolve_path(val)
    if val.isdigit():
        return int(val)
    return None


async def _fav_delete(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites delete <item_id>|<url> — ONE escaped line.

    Perl ``cliDelete`` (``Slim/Plugin/Favorites/Plugin.pm:924-964``) reads the
    **tagged** params of the command::

        my $index  = $request->getParam('item_id');
        my $url    = $request->getParam('url');
        ...
        if (!defined $index || !defined $favs->entry($index)) {
            if ($url) { $favs->deleteUrl($url); }
            else { $request->setStatusBadParams(); return; }
        }
        else { $favs->deleteIndex($index); }

    so ``favorites delete item_id:ab9c31e0.3`` is the CLI form a controller
    sends back after ``favorites items`` (Perl's ``id:`` is the session crumb).
    The bare positional form (``favorites delete 12``) is our documented
    addition and stays accepted; an id that names no existing entry falls back
    to the URL exactly like Perl, and a request that names neither is bad
    params (no result on the wire).
    """
    if not args:
        return _command_echo(["favorites", "delete"], args, [], has_tags=True)
    tagged: dict[str, str] = {}
    positional: list[str] = []
    for a in args:
        s = str(a)
        if ":" in s and s.split(":", 1)[0] in ("item_id", "url", "title"):
            k, _, v = s.partition(":")
            tagged[k] = v
        else:
            positional.append(s)
    target = tagged.get("item_id") or (positional[0] if positional else "")
    url = tagged.get("url") or ""
    try:
        from lyrion.music.favorites import get_favorites_manager

        fm = get_favorites_manager()
        fav_id: Optional[int] = None
        if target:
            fav_id = await _fav_resolve_id(fm, str(target))
            if fav_id is not None and await fm.get(fav_id) is None:
                fav_id = None            # ``!$favs->entry($index)``
        if fav_id is None:
            if not url:
                return _command_echo(["favorites", "delete"], args, [],
                                     has_tags=True)
            fav_id = await fm.find_url(url)  # ``$favs->deleteUrl($url)``
        if fav_id is None:
            return _command_echo(["favorites", "delete"], args, [],
                                 has_tags=True)
        await fm.delete(fav_id)
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["favorites", "delete"], args, [], has_tags=True)


async def _fav_move(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites move <id> <parent_id> [position] — ONE escaped line."""
    if not args:
        return _command_echo(["favorites", "move"], args, [], has_tags=True)
    try:
        from lyrion.music.favorites import get_favorites_manager

        fm = get_favorites_manager()
        fav_id = await _fav_resolve_id(fm, str(args[0]))
        if fav_id is None:
            return _command_echo(["favorites", "move"], args, [], has_tags=True)
        parent = None
        if len(args) > 1:
            parent = await _fav_resolve_id(fm, str(args[1])) if str(args[1]) != "0" else None
        position = int(str(args[2])) if len(args) > 2 and str(args[2]).isdigit() else None
        await fm.move(fav_id, parent, position)
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["favorites", "move"], args, [], has_tags=True)


async def _fav_rename(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites rename <id> <title> [url] — ONE escaped line."""
    if len(args) < 2:
        return _command_echo(["favorites", "rename"], args, [], has_tags=True)
    try:
        from lyrion.music.favorites import get_favorites_manager

        fm = get_favorites_manager()
        fav_id = await _fav_resolve_id(fm, str(args[0]))
        if fav_id is None:
            return _command_echo(["favorites", "rename"], args, [], has_tags=True)
        url = str(args[2]) if len(args) > 2 else None
        await fm.rename(fav_id, str(args[1]), url)
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["favorites", "rename"], args, [], has_tags=True)


async def _fav_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites play <id> [player_id] — ONE escaped line."""
    if not args:
        return _command_echo(["favorites", "play"], args, [], has_tags=True)
    player_id = str(args[1]) if len(args) > 1 else ctx.player_id
    if player_id:
        try:
            from lyrion.music.favorites import get_favorites_manager

            fm = get_favorites_manager()
            fav_id = await _fav_resolve_id(fm, str(args[0]))
            if fav_id is not None:
                await fm.play(player_id, fav_id)
        except Exception:  # noqa: BLE001
            pass
    return _command_echo(["favorites", "play"], args, [], has_tags=True)


async def _fav_exists(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites exists <url|id> — ONE escaped line with ``exists``/``index``.

    ``Slim/Plugin/Favorites/Plugin.pm:811-815`` adds ``exists`` (1/0) and, on a
    hit, ``index``.
    """
    if not args:
        return _command_echo(["favorites", "exists"], args, [], has_tags=True)
    val = str(args[0])
    rows: list[dict] = []
    try:
        if val.isdigit():
            rows = await _query_db(
                "SELECT id FROM favorites WHERE id = ? LIMIT 1", (int(val),))
        else:
            rows = await _query_db(
                "SELECT id FROM favorites WHERE url = ? LIMIT 1", (val,))
    except Exception:  # noqa: BLE001
        rows = []
    results: list[tuple[str, Any]] = [("exists", 1 if rows else 0)]
    if rows:
        results.append(("index", rows[0]["id"]))
    return _command_line(["favorites", "exists"], args, [], has_tags=True,
                         results=results)


async def _fav_folder_children(fm: Any, fav_id: int) -> list[dict[str, Any]]:
    """Direct children of a favourite FOLDER, in OPML order.

    Perl's folder branch walks ``$subFeed->{'items'}`` — the rows of the
    opened feed, i.e. exactly the direct children (``XMLBrowser.pm:712``) —
    and skips every item without a URL (:727 ``if (!$url …) { next; }``).
    Only a folder (``url`` undefined, ``_fav_to_dict`` type 'folder') has
    children worth collecting: a leaf with a URL is the file branch.
    """
    getter = getattr(fm, "get", None)
    if getter is not None:
        row = await getter(fav_id) or {}
        if row.get("url"):
            return []
    kids = await fm.list_items(fav_id)
    return [k for k in kids if str(k.get("url") or "")]


async def _fav_playlist_children(player_id: str, children: list[dict[str, Any]],
                                 action: str) -> bool:
    """Perl's folder branch: the children as ONE ``listref``.

    ``Slim/Control/XMLBrowser.pm:749-766`` builds the URL list and runs
    ``playlist loadtracks|addtracks|inserttracks listref <urls>`` — the whole
    block, not one entry at a time:

    * ``loadtracks`` (``play|load``): clear the queue, load the block, start
      at index 0 (``$playIndex`` is undefined here, ``Commands.pm:1681-1796``
      jumps to 0);
    * ``addtracks`` (``add``): append the block
      (``Slim/Player/Playlist.pm:239``);
    * ``inserttracks`` (else, i.e. ``insert``): append the block and move it
      behind the running song (``Playlist.pm:265-284`` ``_insert_done``).

    Before the verb every child's name and logo is filed under its URL
    (``XMLBrowser.pm:734-742`` ``setRemoteMetadata``), like the file branch
    does for its single row.  Returns False when the player is gone.
    """
    from lyrion.player.manager import PlayerManager
    from lyrion.web.api import JSONRPCAPI

    pm = PlayerManager()
    player = pm.get_player(player_id)
    if player is None:
        return False
    urls = [str(k["url"]) for k in children]
    if not urls:
        return False
    for k in children:
        u = str(k["url"])
        JSONRPCAPI._set_stream_title(player, u, str(k.get("title") or ""))
        JSONRPCAPI._set_stream_image(player, u, str(k.get("icon") or ""))
    if action in ("play", "load"):
        player.playlist = list(urls)
        player.playlist_total = len(urls)
        player.playlist_position = 0
        player.last_activity = time.time()
        # ``playlist jump`` to the (undefined → 0) ``$playIndex``: this is the
        # path that actually opens the stream (``_play_playlist_item`` for a
        # URL entry / ``manager.playlist_play``).
        return await pm.playlist_play(player_id, 0)
    playlist = list(player.playlist or [])
    if action == "add":
        playlist.extend(urls)
    else:
        pos = int(player.playlist_position or 0) + 1
        pos = max(0, min(pos, len(playlist)))
        playlist[pos:pos] = urls
    player.playlist = playlist
    player.playlist_total = len(playlist)
    player.last_activity = time.time()
    return True


async def _fav_playlist(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites playlist <play|load|insert|add> item_id:<id> [player:<mac>] — ONE line.

    Perl registers ``['favorites','playlist','_method']`` on ``cliBrowse``
    (``Slim/Plugin/Favorites/Plugin.pm:81`` → :746-764) and hands the request to
    ``Slim::Control::XMLBrowser::cliQuery`` (:763).  There the item id is a
    **path through the whole feed tree**: ``@index = split /\\./, $item_id`` with
    the browse-session handle shifted off (``Slim/Control/XMLBrowser.pm:331-340``),
    then one descent per crumb —

        $subFeed = $subFeed->{'items'}->[$in - $subFeed->{'offset'}];   (:384)

    — so the *last* crumb selects the row at ANY depth, root or folder
    (:373-405).  For every ``_method`` in add/insert/play the row's URL is the
    same and only the playlist verb differs (:667-702)::

        $client->execute([ 'playlist', $method, $url ]);

    ``play``/``load`` clear the playlist and start the stream
    (``Slim/Control/Commands.pm:1483-1491``), ``add`` appends it (:1495-1503 →
    ``Slim/Player/Playlist.pm:239``) and ``insert`` appends it and then moves the
    new block behind the running song (``Playlist.pm:265-284`` ``_insert_done``).
    Before the verb Perl files the row's name and logo under the URL for all
    three methods alike (:693-700 ``setRemoteMetadata``).

    Our former ``add``/``insert`` branch searched ``fm.list_items(None)`` — the
    ROOT list only — so the row of a favourite inside a folder was never found
    and the verb stayed a silent no-op (``play`` was unaffected: it takes the DB
    id straight from ``resolve_path``).

    FOLDER rows (a favourite *folder*, ``url`` undefined) take Perl's **folder
    branch** (``Slim/Control/XMLBrowser.pm:709-779``): the row is no single
    stream, so the feed's direct children are collected —

        :712  for my $item ( @{ $subFeed->{'items'} } ) {
        :715      if ( $item->{'type'} eq 'audio' && $item->{'url'} ) { … }
        :727      if (!$url || …) { next; }          # no URL → no entry
        :735      Slim::Music::Info::setRemoteMetadata( $url, { title => …,
        :736          cover => $subFeed->{'image'} || … } );

    — and handed to the playlist as one ``listref``:

        :752  if ( $method =~ /play|load/i ) { $cmd = 'loadtracks'; }
        :754  elsif ($method =~ /add/)       { $cmd = 'addtracks'; }
        :758  else                           { $cmd = 'inserttracks'; }
        :766  $client->execute([ 'playlist', $cmd, 'listref', \@urls, undef,
        :767                       $playIndex ]);

    ``loadtracks`` clears the queue and starts at the first child
    (``Commands.pm:1681-1796`` ``playlist jump`` with an undefined index → 0),
    ``addtracks`` appends the block (``Slim/Player/Playlist.pm:239``),
    ``inserttracks`` appends it and moves it behind the running song
    (``Playlist.pm:265-284`` ``_insert_done``).  Perl also renders
    ``_addingToPlaylist`` (a showBriefly) for add/insert (:769-773) — not part
    of the file branch of this port either, so it stays out here.

    The FILE branch (a row with its own URL) is untouched: its single URL is
    still the whole queue entry (``XMLBrowser.pm:667-702``).
    """
    if not args:
        return _command_echo(["favorites", "playlist"], args, [], has_tags=True)
    action = str(args[0]).lower()
    filters: dict[str, str] = {}
    for a in args[1:]:
        s = str(a)
        if ":" in s:
            k, _, v = s.partition(":")
            filters[k] = v
    fav_id_raw = filters.get("item_id")
    if not fav_id_raw:
        return _command_echo(["favorites", "playlist"], args, [], has_tags=True)
    player_id = filters.get("player") or ctx.player_id
    try:
        from lyrion.player.manager import PlayerManager

        from lyrion.music.favorites import get_favorites_manager

        fm = get_favorites_manager()
        fav_id = await _fav_resolve_id(fm, fav_id_raw)
        if fav_id is None or not player_id:
            return _command_echo(["favorites", "playlist"], args, [], has_tags=True)
        # A favourite FOLDER has no URL of its own (``url`` undefined,
        # ``_fav_to_dict`` type 'folder'): Perl's folder branch takes its
        # CHILDREN as the list (``XMLBrowser.pm:709-779``).  A row WITH a URL
        # keeps the untouched file branch below.  ``fm.get`` is optional so a
        # stand-in without it keeps working (it then has no folder rows).
        getter = getattr(fm, "get", None)
        row: dict[str, Any] = {}
        if getter is not None:
            row = await getter(fav_id) or {}
        url = str(row.get("url") or "")
        children: list[dict[str, Any]] = []
        # ``XMLBrowser.pm:667``: the branch runs for
        # add|addall|play|playall|insert|load only — a verb outside that set
        # stays a no-op (the CLI registers play/load/insert/add).
        if not url and action in ("play", "load", "add", "insert"):
            children = await _fav_folder_children(fm, fav_id)
        if children:
            await _fav_playlist_children(player_id, children, action)
        elif action in ("play", "load"):
            await fm.play(player_id, fav_id)
        elif action in ("insert", "add") and url:
            # The row the item id points at — ``resolve_path`` walks the index
            # path over the WHOLE tree (Perl ``XMLBrowser.pm:331-405``), the
            # former root-only ``list_items(None)`` scan found no row inside a
            # folder and the verb died silently.
            player = PlayerManager().get_player(player_id)
            if player is not None:
                # Perl files the row's name AND logo under the URL before
                # the playlist verb — for add/insert/play alike
                # (``XMLBrowser.pm:693-700`` setRemoteMetadata).
                from lyrion.web.api import JSONRPCAPI

                JSONRPCAPI._set_stream_title(player, url,
                                             str(row.get("title") or ""))
                JSONRPCAPI._set_stream_image(player, url,
                                             str(row.get("icon") or ""))
                if action == "add":
                    # Commands.pm:1495-1503 → Playlist.pm:239 (append).
                    player.playlist.append(url)
                else:
                    # Commands.pm:1535-1560 → Playlist.pm:265-284
                    # ``_insert_done``: appended, then moved to
                    # ``playingSongIndex + 1``.
                    playlist = list(player.playlist or [])
                    pos = int(player.playlist_position or 0) + 1
                    pos = max(0, min(pos, len(playlist)))
                    playlist[pos:pos] = [url]
                    player.playlist = playlist
                player.playlist_total = len(player.playlist)
                player.last_activity = time.time()
    except Exception:  # noqa: BLE001
        pass
    return _command_echo(["favorites", "playlist"], args, [], has_tags=True)


__all__ = [
    "cmd_login",
    "cmd_exit",
    "cmd_version",
    "cmd_can",
    "cmd_serverstatus",
    "cmd_players",
    "cmd_playercount",
    "cmd_player",
    "cmd_play",
    "cmd_pause",
    "cmd_stop",
    "cmd_prev",
    "cmd_next",
    "cmd_power",
    "cmd_volume",
    "cmd_mixer",
    "cmd_status",
    "cmd_mode",
    "cmd_time",
    "cmd_current_title",
    "cmd_playlist",
    "cmd_playlist_play",
    "cmd_playlist_add",
    "cmd_playlist_clear",
    "cmd_playlist_save",
    "cmd_playlist_load",
    "cmd_favorites",
    "cmd_search",
    "cmd_rescan",
    "cmd_wipecache",
    "cmd_display",
    "cmd_ir",
    "cmd_pref",
    "cmd_subscribe",
    "cmd_unsubscribe",
    "cmd_info",
    "cmd_radio",
    "cmd_radio_list",
    "cmd_radio_add",
    "cmd_radio_delete",
    "cmd_radio_search",
    "cmd_radio_top",
    "cmd_radio_play",
]
