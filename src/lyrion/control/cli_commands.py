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

if TYPE_CHECKING:
    from lyrion.control.request import RequestDispatcher

logger = logging.getLogger(__name__)

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
    auth_ok = True  # TODO: compare against server settings
    if auth_ok:
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
    """exit — close the CLI connection."""
    return []  # empty list signals connection close


@register_command("ping")
async def cmd_ping(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """ping — connection liveness (SqueezePlay/controllers poll it)."""
    return ["ping"]


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
    """listen <on|off> — subscribe/unsubscribe a client to server events."""
    return ["listen: ok"]


# ---------------------------------------------------------------------------
# Server info
# ---------------------------------------------------------------------------


@register_command("version", "ver")
async def cmd_version(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """version — return server version string."""
    return ["9.2.0 Pyrion Music Server"]


@register_command("can", "capabilities")
async def cmd_can(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    can <capability>
    Query whether the server supports a given capability.
    """
    capabilities = {
        "album_art": True,
        "bitmap": True,
        "digital_volume_control": True,
        "exit": True,
        "flac": True,
        "favicon": True,
        "icons": True,
        "jpeg": True,
        "mp3": True,
        "mixer": True,
        "png": True,
        "reconnect": True,
        "remote": True,
        "resume": True,
        "save": True,
        "sync": True,
        "tcp": True,
        "wav": True,
    }
    if not args:
        return [f"can: {len(capabilities)}"]
    cap = args[0].lower()
    result = "1" if capabilities.get(cap, False) else "0"
    return [f"can {cap}: {result}"]


@register_command("serverstatus")
async def cmd_serverstatus(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    serverstatus [0 100]
    Return server status (LMS format): version/uuid/name/httpport, info
    totals, player count and the players_loop (one line per player).
    """
    try:
        from lyrion import __version__
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        players = pm.get_all_players()
        player_count = len(players)
    except Exception:
        __version__ = "9.2.0"
        players = []
        player_count = 0

    # info totals
    info_lines: list[str] = []
    try:
        rows = await _query_db(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration),0) AS d FROM tracks"
        )
        info_lines.append(f"info total songs: {int(rows[0]['n']) if rows else 0}")
        info_lines.append(f"info total duration: {int(rows[0]['d']) if rows else 0}")
        r_art = await _query_db(
            "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
        )
        info_lines.append(f"info total artists: {int(r_art[0]['n']) if r_art else 0}")
        r_alb = await _query_db("SELECT COUNT(*) AS n FROM albums")
        info_lines.append(f"info total albums: {int(r_alb[0]['n']) if r_alb else 0}")
        r_gen = await _query_db(
            "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre != ''"
        )
        info_lines.append(f"info total genres: {int(r_gen[0]['n']) if r_gen else 0}")
        # lastscan: the timestamp of the most recent scan (Perl parity)
        r_scan = await _query_db(
            "SELECT MAX(last_rescan) AS t FROM tracks"
        )
        info_lines.append(f"lastscan: {int(r_scan[0]['t']) if r_scan and r_scan[0]['t'] else 0}")
    except Exception:  # noqa: BLE001
        pass

    # Server IP (Perl sends the server's address at top level)
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        server_ip = s.getsockname()[0]
        s.close()
    except Exception:
        server_ip = "127.0.0.1"

    out = [
        f"serverstatus version:{__version__ if '__version__' in dir() else '9.2.0'}",
        f"serverstatus uuid:{getattr(pm, 'server_uuid', 'lyrion-local')}",
        f"serverstatus name:Lyrion",
        f"serverstatus ip:{server_ip}",
        f"serverstatus httpport:9000",
    ]
    out.extend(f"serverstatus {l}" for l in info_lines)
    out.append(f"serverstatus player count:{player_count}")
    out.append(f"serverstatus sn.player count:{player_count}")
    out.append(f"serverstatus other player count:0")
    # players_loop
    for i, p in enumerate(players):
        mac = p.mac if p.mac else ""          # raw MAC — Perl does NOT url-encode
        name = p.name or p.mac or ""
        out.append(
            f"playerindex:{i} playerid:{mac} uuid: ip:{p.ip or '0.0.0.0'}:{p.port or 0} "
            f"name:{name} model:{p.model or 'squeezebox'} modelname:{p.model or 'squeezebox'} "
            f"isplaying:{1 if p.mode == 'play' else 0} displaytype:none isplayer:1 "
            f"canpoweroff:1 connected:{1 if p.connected else 0} "
            f"power:{1 if p.power else 0} firmware:1 seq_no:0 "
            f"sn.player.count:{player_count}"
        )
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Player management
# ---------------------------------------------------------------------------


@register_command("players")
async def cmd_players(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """players — list all connected players."""
    # Prefer the PlayerManager singleton (filled by the slimproto layer),
    # fall back to the dispatcher's own registry.
    players: list[dict] = []
    try:
        from lyrion.player import PlayerManager
        pm_players = PlayerManager().get_all_players()
        if pm_players:
            players = [p.to_dict() if hasattr(p, "to_dict") else p for p in pm_players]
    except Exception:
        pass
    if not players:
        dispatcher = handler._dispatcher
        if dispatcher:
            players = await dispatcher.list_players()

    lines = [f"players: {len(players)}"]
    for i, p in enumerate(players):
        pid = p.get("mac") or p.get("id") or "?"
        lines.append(
            f"player index: {i}"
            f" ip: {p.get('ip', '?')}"
            f" name: {p.get('name', pid)}"
            f" model: {p.get('model', 'unknown')}"
            f" isplayer: 1"
            f" uuid: {pid}"
            f" firmware: {p.get('firmware', 'unknown')}"
        )
    lines.append("")
    return lines


@register_command("playercount")
async def cmd_playercount(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playercount — return number of connected players."""
    dispatcher = handler._dispatcher
    if dispatcher:
        count = await dispatcher.count_players()
    else:
        count = 0
    return [f"playercount: {count}"]


@register_command("player")
async def cmd_player(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    player [<playerid>] | player count|id|name|model [<playerid>]
    Set or query the default player for this CLI session.
    """
    if not args:
        if ctx.player_id:
            return [f"player: {ctx.player_id}"]
        return ["player: "]
    sub = str(args[0]).lower()
    if sub == "count":
        try:
            from lyrion.player import PlayerManager
            return [f"player count: {PlayerManager().get_player_count()}", ""]
        except Exception:
            return ["player count: 0", ""]
    if sub in ("id", "name", "model"):
        try:
            from lyrion.player import PlayerManager
            pid = args[1] if len(args) > 1 and args[1] != "?" else ctx.player_id
            p = PlayerManager().get_player(pid) if pid else None
            if p is None:
                return [f"player {sub}: ", ""]
            val = {"id": p.mac, "name": p.name or "", "model": p.model or ""}[sub]
            return [f"player {sub}: {val}", ""]
        except Exception as e:  # noqa: BLE001
            return [f"cli error: {e}", ""]
    ctx.player_id = args[0]
    return [f"player: {ctx.player_id}"]


@register_command("name")
async def cmd_name(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """name [<new name>] — query or set the player name. 'name ?' queries."""
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        if args and str(args[0]) != "?":
            new_name = " ".join(args)
            pm.rename_player(player.mac, new_name)
            return [f"name: {new_name}", ""]
        return [f"name: {player.name}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("sync")
async def cmd_sync(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """sync <masterMac> <slaveMac> — synchronize two players."""
    if len(args) >= 2:
        try:
            from lyrion.player import PlayerManager
            PlayerManager().sync_players(args[0], [args[1]])
            return [f"sync: {args[0]} {args[1]}", ""]
        except Exception as e:  # noqa: BLE001
            return [f"cli error: {e}", ""]
    return ["sync: ", ""]


@register_command("unsync")
async def cmd_unsync(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """unsync <playerMac> — remove a player from its sync group."""
    target = args[0] if args else ctx.player_id
    if target:
        try:
            from lyrion.player import PlayerManager
            PlayerManager().unsync_player(target)
            return [f"unsync: {target}", ""]
        except Exception as e:  # noqa: BLE001
            return [f"cli error: {e}", ""]
    return ["unsync: ", ""]


@register_command("syncgroups")
async def cmd_syncgroups(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """syncgroups — list all sync groups (master -> slaves)."""
    try:
        from lyrion.player import PlayerManager
        groups: dict[str, list[str]] = {}
        for p in PlayerManager().get_all_players():
            if p.sync_master:
                groups.setdefault(p.sync_master, []).append(p.mac)
        if not groups:
            return ["syncgroups count:0", ""]
        out = [f"syncgroups count:{len(groups)}"]
        for master, slaves in groups.items():
            out.append(f"sync_master: {master}")
            out.append("sync_slaves: " + ",".join(slaves))
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("songinfo")
async def cmd_songinfo(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """songinfo <start> <count> track_id:<id> — detailed track info.

    Perl-LMS format: one line PER FIELD (songinfo_loop items), e.g.
        songinfo 0 100 count:27
        id:2
        title:Sabkah
        artist:Nils Petter Molvær
        duration:314.546
        ...
    Fields present depend on the track; 'id' and 'title' always come.
    """
    offset, limit, filters = _parse_query_args(args)
    tid = filters.get("track_id")
    if not tid or not str(tid).isdigit():
        return ["songinfo: no track_id", ""]
    rows = await _query_db(
        "SELECT t.id, t.title, t.url, t.duration, t.year, t.tracknum, t.genre, "
        "t.filesize, t.samplerate, t.bitspersample AS samplesize, t.channels, "
        "t.content_type AS ctype, t.modtime, t.remote AS lossless "
        "FROM tracks t WHERE t.id = ? LIMIT 1",
        (int(tid),),
    )
    if not rows:
        return [f"songinfo {offset} {limit} count:0", ""]
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

    out = [f"songinfo {offset} {limit} count:{len(fields)}"]
    for key, value in fields:
        out.append(f"{key}:{value}")
    out.append("")
    return out


@register_command("years")
async def cmd_years(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """years [<offset> <limit>] — list release years in the library."""
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
    # Perl parity: years_loop items carry year + favorites_url
    # (db:year.id=<year>); no id field.
    out = [f"years {offset} {limit} count:{total_n}"]
    for r in rows:
        y = r["y"]
        out.append(f"year:{y} favorites_url:db:year.id={y}")
    out.append("")
    return out


@register_command("musicfolder")
async def cmd_musicfolder(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """musicfolder [<offset> <limit>] [folder_id:<path>] — browse folders.

    The folder hierarchy is derived from the scanned track URLs
    (file:///...). folder_id is the parent directory URL; the root lists
    the top-level directories.
    """
    offset, limit, filters = _parse_query_args(args)
    folder = filters.get("folder_id", "")
    parent_prefix = folder.rstrip("/")
    if folder:
        # tracks directly in this folder + one subfolder level
        rows = await _query_db(
            "SELECT DISTINCT url FROM tracks WHERE url LIKE ? "
            "ORDER BY url LIMIT ? OFFSET ?",
            (parent_prefix + "/%", limit, offset),
        )
        items: list[str] = []
        for r in rows:
            rel = r["url"][len(parent_prefix) + 1:]
            items.append(rel.split("/", 1)[0])
        total = await _query_db(
            "SELECT COUNT(DISTINCT url) AS n FROM tracks WHERE url LIKE ?",
            (parent_prefix + "/%",),
        )
        out = [f"musicfolder {offset} {limit} count:{total[0]['n'] if total else 0}"]
        for i, name in enumerate(dict.fromkeys(items)):
            # Perl parity: folder items = id + filename + type (no title).
            out.append(f"id:{offset + i + 1} filename:{name} type:folder")
        out.append("")
        return out
    # root: distinct first path components under file:// roots
    rows = await _query_db(
        "SELECT DISTINCT url FROM tracks WHERE url LIKE 'file://%' "
        "ORDER BY url LIMIT 500",
    )
    roots: dict[str, str] = {}
    for r in rows:
        path = r["url"][len("file://"):].lstrip("/")
        parts = path.split("/")
        if len(parts) >= 2:
            root = parts[0]
            roots.setdefault(root, f"file:///{root}")
    names = sorted(roots.keys())
    page = names[offset:offset + limit]
    out = [f"musicfolder {offset} {limit} count:{len(names)}"]
    for i, name in enumerate(page):
        # Perl parity: id + filename + type (no title).
        out.append(f"id:{offset + i + 1} filename:{name} type:folder")
    out.append("")
    return out


@register_command("rescanprogress")
async def cmd_rescanprogress(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """rescanprogress — report rescan progress (LMS tagged format).

    Real scan state from the shared ScanState singleton; while idle:
    'rescanprogress progress:0 scanning:0'.
    """
    try:
        from lyrion.media.scan_state import SCAN_STATE
        st = SCAN_STATE.snapshot()
        return [
            "rescanprogress progress:%d scanning:%d" % (
                int(st.get("progress", 0)),
                1 if st.get("scanning") else 0,
            ),
            "",
        ]
    except Exception:  # noqa: BLE001
        return ["rescanprogress progress:0 scanning:0", ""]


@register_command("abortscan")
async def cmd_abortscan(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """abortscan — cancel a running rescan (best effort)."""
    return ["abortscan: ok", ""]


@register_command("menu")
async def cmd_menu(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """menu [<start> <count>] [direct:1] — return the home menu (text form).

    Mirrors the JSON-RPC menu handler for telnet clients. The JSON-RPC
    response uses 'item_loop' (not 'loop_loop'), so read that key.
    """
    try:
        from lyrion.web.api import JSONRPCAPI
        res = await JSONRPCAPI()._slim_request(ctx.player_id or "", ["menu"] + list(args))
        # JSON-RPC menu returns item_loop + count (+ offset). Fall back to
        # the bare home-menu list if the shape differs.
        loop = res.get("item_loop")
        if loop is None:
            home = JSONRPCAPI()._home_menu()
            start = int(args[0]) if args and str(args[0]).isdigit() else 0
            count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else len(home)
            loop = home[start:start + count]
        total = res.get("count")
        if total is None:
            total = len(loop)
        out = [f"menu count:{total}"]
        for i, item in enumerate(loop):
            text = item.get("text") or item.get("name", "")
            node = item.get("node") or item.get("id", "")
            browse_id = item.get("browse", {}).get("id", "") if isinstance(item.get("browse"), dict) else item.get("browse", "")
            out.append(f"menu index:{i} text:{text} browse:{browse_id}")
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


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
    """displaystatus [<playerId>] — display/now-playing info (popup stub)."""
    return ["displaystatus: ", ""]


# ---------------------------------------------------------------------------
# Playback control
# ---------------------------------------------------------------------------


@register_command("play")
async def cmd_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    play [trackId]
    Start playback, optionally at a specific track index (DB id).
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        if args:
            track_id = int(args[0])
            ok = await pm.play_track(ctx.player_id, track_id)
            if not ok:
                return ["cli error: could not start playback (player not connected?)"]
            return [f"play {track_id}", ""]
        # No track id — resume whatever is selected
        player = pm.get_player(ctx.player_id)
        if player is not None:
            # Paused -> resume in place (LMS play button behaviour)
            if player.mode == "pause":
                ok = await pm.pause_player(ctx.player_id, False)
                return ["play", ""] if ok else ["cli error: playback failed"]
            # Radio stream (current_track_id None, current_url set)
            if player.current_track_id is None and getattr(player, "current_url", None):
                ok = await pm.play_url(ctx.player_id, player.current_url,
                                       getattr(player, "current_title", "") or "")
                return [f"play {player.current_url[:60]}", ""] if ok \
                    else ["cli error: playback failed"]
            if player.current_track_id is not None:
                ok = await pm.play_track(ctx.player_id, player.current_track_id)
                return [f"play {player.current_track_id}", ""] if ok else ["cli error: playback failed"]
            # Fallback: resume the current playlist entry
            if player.playlist and 0 <= player.playlist_position < len(player.playlist):
                entry = player.playlist[player.playlist_position]
                if isinstance(entry, str):
                    ok = await pm.play_url(ctx.player_id, entry, "")
                else:
                    ok = await pm.play_track(ctx.player_id, entry)
                return ["play", ""] if ok else ["cli error: playback failed"]
        return ["play", ""]
    except ValueError:
        return ["cli error: track id must be a number", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("pause")
async def cmd_pause(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    pause [0|1]
    Toggle pause, or force pause on (1) / off (0).
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        if args and args[0] == "0":
            ok = await pm.pause_player(ctx.player_id, False)
        elif args and args[0] == "1":
            ok = await pm.pause_player(ctx.player_id, True)
        else:
            player = pm.get_player(ctx.player_id)
            currently_paused = player is not None and player.mode == "pause"
            ok = await pm.pause_player(ctx.player_id, not currently_paused)
        return [] if ok else ["cli error: could not pause/resume"]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("stop")
async def cmd_stop(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """stop — stop playback."""
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        ok = await PlayerManager().stop_player(ctx.player_id)
        return [] if ok else ["cli error: could not stop"]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("prev")
async def cmd_prev(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """prev — skip to previous track."""
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        ok = await PlayerManager().playlist_prev(ctx.player_id)
        return ["prev"] if ok else ["no playlist", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("next")
async def cmd_next(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """next — skip to next track."""
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        ok = await PlayerManager().playlist_next(ctx.player_id)
        return ["next"] if ok else ["no playlist", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("power")
async def cmd_power(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    power [0|1]
    Query or set player power state. Turning off stops playback.
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        if args and str(args[0]) in ("0", "1"):
            on = str(args[0]) == "1"
            player.power = on
            if not on:
                await pm.stop_player(ctx.player_id)
            return ["power: 1" if on else "power: 0", ""]
        return [f"power: {'1' if player.power else '0'}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# ---------------------------------------------------------------------------
# Volume / Mixer
# ---------------------------------------------------------------------------


@register_command("volume")
async def cmd_volume(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    volume [<0-100>]
    Query or set player volume (sends audg frame to the player).
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if args and str(args[0]).replace(".", "").isdigit():
            new_volume = int(float(str(args[0])))
            ok = await pm.set_volume(ctx.player_id, new_volume)
            return [] if ok else ["cli error: could not set volume"]
        current = player.volume if player else 50
        return [f"volume: {current}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("mixer")
async def cmd_mixer(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    mixer <parameter> [value]
    Query or set a mixer parameter. 'mixer volume [0-100]' works directly
    (audg frame); other parameters fall back to the dispatcher.
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player.manager import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        if args and str(args[0]).lower() == "volume":
            if len(args) > 1 and str(args[1]).isdigit():
                ok = await pm.set_volume(ctx.player_id, int(str(args[1])))
                return [] if ok else ["cli error: could not set volume", ""]
            return [f"mixer volume: {player.volume}", ""]
        # bass/treble/pitch/muting: stored on the player state (no SlimProto
        # frame for these; SqueezePlay-only equalizer in the real LMS)
        if args and str(args[0]).lower() in ("bass", "treble", "pitch", "muting"):
            key = str(args[0]).lower()
            attr = f"mixer_{key}"
            if len(args) > 1 and str(args[1]).isdigit():
                setattr(player, attr, int(str(args[1])))
                return [f"mixer {key}: {args[1]}", ""]
            return [f"mixer {key}: {getattr(player, attr, 0)}", ""]
        if handler._dispatcher:
            return await handler._dispatcher.player_command(ctx.player_id, "mixer", args)
        return [f"mixer: unsupported parameter '{args[0] if args else ''}'", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("playerpref")
async def cmd_playerpref(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playerpref <key> [<value>] — query or set a per-player preference.

    LMS 'playerpref volume ?' returns _p2 '<value>'. 'playerpref <key> <v>'
    sets it. Prefs are stored on the player state dict (in-memory).
    """
    if not ctx.player_id:
        return ["no player selected"]
    if not args:
        return ["playerpref: ", ""]
    key = str(args[0])
    try:
        from lyrion.player import PlayerManager
        player = PlayerManager().get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        prefs = getattr(player, "playerprefs", None)
        if prefs is None:
            prefs = {}
            player.playerprefs = prefs
        # Query form: '?'
        if len(args) == 1 or (len(args) == 2 and str(args[1]) == "?"):
            return [f"playerpref {key}: {prefs.get(key, '')}", ""]
        # Set form: value may be multi-word (join the rest)
        value = " ".join(str(a) for a in args[1:])
        prefs[key] = value
        return [f"playerpref {key}: {value}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("button")
async def cmd_button(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """button <name> — simulate a front-panel button press on the player.

    Named buttons map to transport actions (play/pause/stop/prev/next/
    power). LMS 'button play' returns {} (control command); we echo the
    action result. Unknown button -> "button: unhandled".
    """
    if not ctx.player_id:
        return ["no player selected"]
    if not args:
        return ["button: no button", ""]
    name = str(args[0]).lower()
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        if name == "play":
            player = pm.get_player(ctx.player_id)
            if player is not None and player.mode != "play":
                await pm.pause_player(ctx.player_id, False)
            return ["play", ""]
        if name == "pause":
            player = pm.get_player(ctx.player_id)
            if player is not None:
                await pm.pause_player(ctx.player_id, player.mode != "pause")
            return ["pause", ""]
        if name == "power":
            player = pm.get_player(ctx.player_id)
            if player is not None:
                pm.set_power(ctx.player_id, not player.power)
            return ["power", ""]
        if name == "stop":
            await pm.stop_player(ctx.player_id)
            return ["stop", ""]
        if name == "prev":
            await pm.playlist_prev(ctx.player_id)
            return ["prev", ""]
        if name == "next":
            await pm.playlist_next(ctx.player_id)
            return ["next", ""]
        return [f"button: unhandled '{args[0]}'", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@register_command("status")
async def cmd_status(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    status [- <count>] [tags:<code>] [subscribe:<seconds>]
    Return current playback status for the default player. 'mode' is
    unmapped (play/pause/stop) as per the LMS CLI spec; the playlist
    loop is included when tags contains 'l' (or always when no tags).
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        # tags: t=title a=artist l=album d=duration u=url g=genre y=year n=tracknum
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
        # Duration: prefer the live player state (filled on track load),
        # fall back to the DB row of the current track.
        duration = int(getattr(player, "duration", 0) or 0)
        if not duration and player.current_track_id is not None:
            rows_d = await _query_db(
                "SELECT duration FROM tracks WHERE id = ?",
                (player.current_track_id,),
            )
            if rows_d:
                duration = int(rows_d[0]["duration"] or 0)
                try:
                    player.duration = float(duration)
                except Exception:
                    pass
        out = [
            "status",
            f"player_name: {player.name}",
            f"player_connected: {'1' if player.connected else '0'}",
            f"power: {'1' if player.power else '0'}",
            f"mode: {player.mode or 'stop'}",
            f"time: {elapsed}",
            f"rate: 1",
            f"volume: {player.volume}",
            f"duration: {duration}",
            f"playlist_tracks: {player.playlist_total}",
            # LMS reports playlist_cur_index as a string (1-based).
            f"playlist_cur_index: {player.playlist_position}",
            f"playlist_timestamp: {int(getattr(player, 'playlist_timestamp', 0) or 0)}",
            f"playlist mode: {getattr(player, 'playlist_mode', 'none')}",
            f"playlist_shuffle: {getattr(player, 'shuffle', 0)}",
            f"playlist_repeat: {getattr(player, 'repeat', 0)}",
            f"seq_no: {getattr(player, 'seq_no', 0)}",
            f"signalstrength: {getattr(player, 'signal_strength', 0)}",
            f"remote: {getattr(player, 'remote', 0)}",
            f"randomplay: {getattr(player, 'randomplay', 0)}",
            f"use_volume_control: {1 if getattr(player, 'use_volume_control', 1) else 0}",
            f"digital_volume_control: {1 if getattr(player, 'digital_volume_control', 1) else 0}",
            f"mixer volume: {player.volume}",
        ]
        if player.current_track_id is not None:
            out.append(f"playlist_cur_id: {player.current_track_id}")
        if getattr(player, "current_title", None):
            out.append(f"current_title: {player.current_title}")
        if getattr(player, "current_url", None):
            out.append(f"current_url: {player.current_url}")
        # remoteMeta: the now-playing stream metadata (HTTP headers / ICY)
        remote_meta = getattr(player, "remote_meta", None)
        if remote_meta:
            for k, v in remote_meta.items():
                out.append(f"remoteMeta.{k}: {v}")
        # Playlist loop: always unless tags explicitly omit it (no 'l')
        if "l" in tags or not tags:
            out.extend(await _status_playlist_loop(player, tags))
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _status_playlist_loop(player: Any, tags: str) -> list[str]:
    """Build 'playlist index:N ...' lines for the status reply.

    Each playlist entry is one line; the included tags depend on the
    tags: code (t=title, a=artist, l=album, d=duration, u=url, g=genre,
    y=year, n=tracknum). With an empty code every known field is added.
    """
    playlist = getattr(player, "playlist", []) or []
    if not playlist:
        return []
    lines: list[str] = []
    want_all = not tags
    for i, item in enumerate(playlist):
        parts = [f"playlist index:{i}"]
        if isinstance(item, int):
            rows = await _query_db(
                "SELECT id, title, url, duration, genre, year, tracknum "
                "FROM tracks WHERE id = ?",
                (item,),
            )
            if not rows:
                lines.append(" ".join(parts))
                continue
            r = rows[0]
            if want_all or "t" in tags:
                parts.append(f"title:{r['title'] or ''}")
            if want_all or "d" in tags:
                parts.append(f"duration:{int(r['duration'] or 0)}")
            if want_all or "u" in tags:
                parts.append(f"url:{r['url'] or ''}")
            if want_all or "g" in tags:
                if r["genre"]:
                    parts.append(f"genre:{r['genre']}")
            if want_all or "y" in tags:
                if r["year"]:
                    parts.append(f"year:{r['year']}")
            if want_all or "n" in tags:
                if r["tracknum"]:
                    parts.append(f"tracknum:{r['tracknum']}")
            if want_all or "a" in tags:
                rows_a = await _query_db(
                    "SELECT c.name FROM contributors c JOIN tracks_contributors tc "
                    "ON tc.contributor = c.id AND tc.role = 1 "
                    "WHERE tc.track = ? ORDER BY c.name LIMIT 1",
                    (item,),
                )
                if rows_a and rows_a[0]["name"]:
                    parts.append(f"artist:{rows_a[0]['name']}")
            if want_all or "l" in tags:
                rows_al = await _query_db(
                    "SELECT al.title FROM albums al JOIN tracks_albums ta "
                    "ON ta.album = al.id WHERE ta.track = ? LIMIT 1",
                    (item,),
                )
                if rows_al and rows_al[0]["title"]:
                    parts.append(f"album:{rows_al[0]['title']}")
        else:
            # URL item (radio/favorite)
            parts.append(f"url:{item}")
        lines.append(" ".join(parts))
    return lines


@register_command("mode")
async def cmd_mode(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """mode — return current playback mode (play, pause, stop)."""
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        player = PlayerManager().get_player(ctx.player_id)
        mode = player.mode if player else "stop"
        return [f"mode: {mode}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("time")
async def cmd_time(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    time [<seconds>|<mm:ss>|?<delta>]
    Query or set the playback position. 'time ?' queries, a plain number
    seeks to that second, '<n>-'/'<n>+' jumps relative (LMS format).
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        if not args or str(args[0]) == "?":
            return [f"time: {int(getattr(player, 'elapsed', 0) or 0)}", ""]
        val = str(args[0])
        if val.startswith("?"):  # '?-5' / '?+10' relative query
            try:
                delta = int(val[2:] if val[1] in "+-" else val[1:])
                cur = int(getattr(player, "elapsed", 0) or 0)
                return [f"time: {max(0, cur + delta)}", ""]
            except ValueError:
                return [f"time: {int(getattr(player, 'elapsed', 0) or 0)}", ""]
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
        return ["time: %d" % int(max(0, seconds)), ""]
    except ValueError:
        return ["cli error: time must be a number", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("sleep")
async def cmd_sleep(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """sleep [<seconds>|off|?] — query or set the sleep timer.

    LMS 'sleep ?' returns the remaining seconds (_sleep), 'sleep off'
    cancels, 'sleep <n>' sets a countdown. We report the player's
    sleep_remaining field (0 = no timer).
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        player = PlayerManager().get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        remaining = int(getattr(player, "sleep_remaining", 0) or 0)
        if not args or str(args[0]) == "?":
            return [f"sleep: {remaining}", ""]
        t = str(args[0]).lower()
        if t == "off":
            player.sleep_remaining = 0
            return ["sleep: 0", ""]
        try:
            player.sleep_remaining = int(t)
        except ValueError:
            return ["cli error: sleep must be a number or off", ""]
        return [f"sleep: {player.sleep_remaining}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("signalstrength")
async def cmd_signalstrength(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """signalstrength [?] — return the player's WiFi signal strength.

    LMS 'signalstrength ?' returns '_signalstrength <pct>'. Wired /
    software players report 0.
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        player = PlayerManager().get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        sig = int(getattr(player, "signal_strength", 0) or 0)
        return [f"signalstrength: {sig}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("randomplay")
async def cmd_randomplay(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """randomplay [<mode>] — query or set the random-play (DJ) mode.

    LMS 'randomplay ?' returns the current mode index ("random playlist"
    / "similar songs"); 'randomplay <mode>' enables it.
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        player = PlayerManager().get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        mode = int(getattr(player, "randomplay", 0) or 0)
        if not args or str(args[0]) == "?":
            labels = ["", "random", "similar"]
            return [f"randomplay: {mode}", ""]
        try:
            player.randomplay = max(0, min(2, int(str(args[0]))))
        except ValueError:
            return ["cli error: randomplay must be 0-2", ""]
        return [f"randomplay: {player.randomplay}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("current_title")
async def cmd_current_title(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """current_title — return the title of the currently playing track."""
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        player = PlayerManager().get_player(ctx.player_id)
        if player is None or player.current_track_id is None:
            return ["", ""]
        rows = await _query_db(
            "SELECT title FROM tracks WHERE id = ?", (player.current_track_id,)
        )
        if rows:
            return [rows[0]["title"], ""]
        return ["", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("artist")
async def cmd_artist(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """artist [?] — return the artist of the currently playing track."""
    return await _current_metadata(ctx, "artist")


@register_command("album")
async def cmd_album(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """album [?] — return the album of the currently playing track."""
    return await _current_metadata(ctx, "album")


@register_command("genre")
async def cmd_genre(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """genre [?] — return the genre of the currently playing track."""
    return await _current_metadata(ctx, "genre")


async def _current_metadata(ctx: CLIContext, field: str) -> list[str]:
    """Return a single metadata field (artist/album/genre) of the current track."""
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        player = PlayerManager().get_player(ctx.player_id)
        if player is None or player.current_track_id is None:
            return ["", ""]
        # Cache on player state, but fall back to the DB row.
        cached = getattr(player, f"current_{field}", None)
        if cached:
            return [str(cached), ""]
        if field == "artist":
            sql = ("SELECT c.name AS v FROM contributors c "
                   "JOIN tracks_contributors tc ON tc.contributor = c.id "
                   "AND tc.role = 1 WHERE tc.track = ? LIMIT 1")
        elif field == "album":
            # tracks has no album column; resolve via tracks_albums -> albums.
            sql = ("SELECT al.title AS v FROM albums al "
                   "JOIN tracks_albums ta ON ta.album = al.id "
                   "WHERE ta.track = ? LIMIT 1")
        else:
            sql = f"SELECT {field} AS v FROM tracks WHERE id = ?"
        rows = await _query_db(sql, (player.current_track_id,))
        if rows and rows[0]["v"]:
            return [str(rows[0]["v"]), ""]
        return ["", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


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


@register_command("playlist")
async def cmd_playlist(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    playlist <subcommand> [args...]

    Subcommands:
        play <trackId>    — play a track (or index if in playlist)
        add <trackId>...  — add tracks to playlist
        insert <trackId>  — insert track after current
        delete <index>    — remove track at index
        move <from> <to>  — move track
        clear             — clear playlist
        save <name>       — save playlist
        load <name>       — load playlist
        resume <name>     — resume saved playlist
        tracks            — list playlist tracks
        next / prev       — skip in playlist
    """
    if not ctx.player_id:
        return ["no player selected"]
    if not args:
        return ["playlist: "]
    sub = args[0].lower()
    rest = args[1:]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        if sub == "play":
            return await cmd_playlist_play(handler, ctx, rest)
        if sub == "add":
            return await cmd_playlist_add(handler, ctx, rest)
        if sub == "insert":
            if rest:
                return await cmd_playlist_add(handler, ctx, rest)
            return ["playlist insert <trackId> — missing id", ""]
        if sub == "delete":
            if not rest or not str(rest[0]).isdigit():
                return ["playlist delete <index> — missing index", ""]
            pm.playlist_remove(ctx.player_id, int(rest[0]))
            return ["deleted", ""]
        if sub == "clear":
            pm.playlist_clear(ctx.player_id)
            return ["ok", ""]
        if sub == "next":
            ok = await pm.playlist_next(ctx.player_id)
            return ["next"] if ok else ["playlist empty", ""]
        if sub == "prev":
            ok = await pm.playlist_prev(ctx.player_id)
            return ["prev"] if ok else ["playlist empty", ""]
        if sub == "tracks":
            player = pm.get_player(ctx.player_id)
            tracks = player.playlist if player else []
            out = [f"playlist tracks: {len(tracks)}"]
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
            for i, entry in enumerate(tracks):
                out.append(f"id: {i}")
                if isinstance(entry, str):
                    # Stream-URL entry (radio favorite)
                    out.append(f"url: {entry}")
                    if i == player.playlist_position and player.current_title:
                        out.append(f"title: {player.current_title}")
                    else:
                        out.append("title: Radio Stream")
                else:
                    out.append(f"track_id: {entry}")
                    if entry in track_titles:
                        out.append(f"title: {track_titles[entry]}")
            out.append("")
            return out
        if sub == "move":
            if len(rest) >= 2 and str(rest[0]).isdigit() and str(rest[1]).isdigit():
                ok = _move_playlist_item(pm, ctx.player_id, int(rest[0]), int(rest[1]))
                return ["moved"] if ok else ["cli error: move failed", ""]
            return ["playlist move <from> <to> — missing indices", ""]
        if sub == "save":
            if not rest:
                return ["playlist save <name> — missing name", ""]
            ok = await pm.save_playlist(ctx.player_id, rest[0])
            return ["saved"] if ok else ["cli error: could not save (empty playlist?)", ""]
        if sub in ("load", "resume"):
            if not rest:
                return [f"playlist {sub} <name> — missing name", ""]
            ok = await pm.load_playlist(ctx.player_id, rest[0])
            if ok:
                return [f"playlist {sub}: {rest[0]}", ""]
            return [f"cli error: playlist '{rest[0]}' not found", ""]
        if sub == "url":
            # playlist url <url> [title] — replace queue with a stream URL
            if not rest:
                player2 = pm.get_player(ctx.player_id)
                return ["playlist url: %s" % (getattr(player2, "current_url", "") or ""), ""]
            title = " ".join(rest[1:]) if len(rest) > 1 else ""
            ok = await pm.play_url(ctx.player_id, rest[0], title)
            return [] if ok else ["cli error: could not play url", ""]
        if sub in ("index", "jump"):
            # playlist index <n> — jump to a playlist index (no restart of
            # an identical index; LMS 'index' only plays when changed).
            # 'playlist index ?' returns the current index (LMS parity).
            player3 = pm.get_player(ctx.player_id)
            if not rest or str(rest[0]) == "?":
                cur = getattr(player3, "playlist_position", 0) if player3 else 0
                return [f"playlist index: {cur}", ""]
            if not str(rest[0]).lstrip("-").isdigit():
                return [f"playlist {sub} <index> — missing index", ""]
            idx = int(rest[0])
            cur = getattr(player3, "playlist_position", 0) if player3 else 0
            if sub == "index" and idx == cur and player3 is not None \
                    and player3.mode == "play":
                return []
            ok = await pm.playlist_play(ctx.player_id, idx)
            return [] if ok else ["cli error: could not jump", ""]
        if sub == "shuffle":
            # playlist shuffle <0|1|2|?> — off/song/album shuffle state
            player4 = pm.get_player(ctx.player_id)
            if player4 is None:
                return ["player not found", ""]
            if not rest or str(rest[0]) == "?":
                return [f"playlist shuffle: {getattr(player4, 'shuffle', 0)}", ""]
            try:
                player4.shuffle = max(0, min(2, int(str(rest[0]))))
                return ["playlist shuffle: %d" % player4.shuffle, ""]
            except ValueError:
                return ["cli error: shuffle must be a number", ""]
        if sub == "repeat":
            # playlist repeat <0|1|2|?> — off/repeat-one/repeat-all
            player5 = pm.get_player(ctx.player_id)
            if player5 is None:
                return ["player not found", ""]
            if not rest or str(rest[0]) == "?":
                return [f"playlist repeat: {getattr(player5, 'repeat', 0)}", ""]
            try:
                player5.repeat = max(0, min(2, int(str(rest[0]))))
                return ["playlist repeat: %d" % player5.repeat, ""]
            except ValueError:
                return ["cli error: repeat must be a number", ""]
        if sub == "loop":
            player6 = pm.get_player(ctx.player_id)
            if player6 is None:
                return ["player not found", ""]
            rep = getattr(player6, "repeat", 0)
            shu = getattr(player6, "shuffle", 0)
            return [
                f"loop: {['off','song','playlist'][min(2, rep)]}",
                f"shuffle: {['none','track','album'][min(2, shu)]}",
                "",
            ]
        if sub in ("genres", "genre"):
            # LMS: playlist genres ? → comma-separated genre list
            player7 = pm.get_player(ctx.player_id)
            ids = [e for e in (player7.playlist if player7 else [])
                   if isinstance(e, int)] if player7 else []
            if not ids:
                return ["genres: ", ""]
            placeholders = ",".join("?" * len(ids))
            g_rows = await _query_db(
                "SELECT DISTINCT t.genre AS g FROM tracks t "
                f"WHERE t.id IN ({placeholders}) AND t.genre != '' "
                "ORDER BY t.genre",
                tuple(ids),
            )
            names = ",".join(r["g"] for r in g_rows)
            return [f"genres: {names}", ""]
        return [f"playlist: unknown subcommand '{sub}'", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# Sub-command "playlist play" — routed via the base handler, not the registry.
async def cmd_playlist_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist play <trackId|index|tag:value> — play a track.

    Extended (tagged) parameters from the Jive actions:
      track_id:<n>   play a library track
      item_id:<n>    play a favorite (stream)
      album_id:<n>   play all tracks of an album
      artist_id:<n>  play all tracks of an artist
      index:<n>      jump to a playlist index
    """
    if not ctx.player_id:
        return ["no player selected"]
    if not args:
        return ["playlist play <trackId> — missing id", ""]
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
                if k in ("track_id", "item_id", "album_id", "artist_id", "index"):
                    tags[k] = v
                    continue
            positional.append(s)

        if "item_id" in tags:
            # Play a favorite (stream or folder entry)
            try:
                from lyrion.music.favorites import get_favorites_manager
                from lyrion.control.cli_commands import _fav_resolve_id
                fm = get_favorites_manager()
                fav_id = await _fav_resolve_id(fm, tags["item_id"])
                if fav_id is not None:
                    ok = await fm.play(ctx.player_id, fav_id)
                    return [f"playlist play {tags['item_id']}", ""] if ok \
                        else ["cli error: could not play favorite", ""]
                # Fallback: radio station id
                from lyrion.music.radio import get_radio_manager
                station = await get_radio_manager().get_station(int(tags["item_id"]))
                if station is not None:
                    ok = await pm.play_url(ctx.player_id, station.url, station.name)
                    return [f"playlist play {station.name}", ""] if ok \
                        else ["cli error: could not play station", ""]
                return ["favorites play: unknown id", ""]
            except Exception as exc:  # noqa: BLE001
                return [f"cli error: {exc}", ""]
        if "track_id" in tags:
            tid = int(tags["track_id"])
            pm.playlist_add(ctx.player_id, tid)
            ok = await pm.play_track(ctx.player_id, tid)
            return [f"playlist play {tid}", ""] if ok else ["cli error: could not play", ""]
        if "album_id" in tags or "artist_id" in tags:
            # Expand to all tracks of the album/artist (one query)
            try:
                import sqlite3
                db = sqlite3.connect(
                    f"file:{_library_db_path()}?mode=ro", uri=True)
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
                db.close()
                ids = [r[0] for r in rows]
                if not ids:
                    return ["cli error: no tracks found", ""]
                pm.playlist_clear(ctx.player_id)
                for tid in ids:
                    pm.playlist_add(ctx.player_id, tid)
                ok = await pm.play_track(ctx.player_id, ids[0])
                return [f"playlist play {len(ids)} tracks", ""] if ok \
                    else ["cli error: could not play", ""]
            except Exception as exc:  # noqa: BLE001
                return [f"cli error: {exc}", ""]
        if "index" in tags:
            idx = int(tags["index"])
            ok = await pm.playlist_play(ctx.player_id, idx)
            return [f"playlist play {idx}", ""] if ok else ["cli error: could not play", ""]

        # Positional form: playlist index if valid, else track id
        if positional and str(positional[0]).isdigit():
            idx = int(str(positional[0]))
            if player and idx < len(player.playlist):
                ok = await pm.playlist_play(ctx.player_id, idx)
                return [f"playlist play {idx}", ""] if ok else ["cli error: could not play", ""]
            track_id = idx
        elif positional:
            first = str(positional[0])
            # Bare stream URL (SqueezePlay sends playlist play <url> for a
            # favorite whose url it uses directly, no item_id) — play it.
            if "://" in first:
                url = first
                ok = await pm.play_url(ctx.player_id, url, "")
                return [f"playlist play {url}", ""] if ok \
                    else ["cli error: could not play stream", ""]
            track_id = int(first)
        else:
            return ["playlist play <trackId> — missing id", ""]
        pm.playlist_add(ctx.player_id, track_id)
        ok = await pm.play_track(ctx.player_id, track_id)
        return [f"playlist play {track_id}", ""] if ok else ["cli error: could not play", ""]
    except ValueError:
        return ["cli error: track id must be a number", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# Sub-command "playlist add" — routed via the base handler, not the registry.
async def cmd_playlist_add(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist add <trackId>... — add tracks to playlist."""
    if not ctx.player_id:
        return ["no player selected"]
    if not args:
        return ["playlist add <trackId> — missing id", ""]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        added = 0
        for a in args:
            # Accept both plain ids and 'track_id:<n>' tagged form
            s = str(a)
            if ":" in s:
                k, _, v = s.partition(":")
                if k in ("track_id", "item_id"):
                    s = v
            try:
                pm.playlist_add(ctx.player_id, int(s))
                added += 1
            except ValueError:
                continue
        return [f"added: {added}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# Sub-command "playlist clear" — routed via the base handler, not the registry.
async def cmd_playlist_clear(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist clear — clear the current playlist."""
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        PlayerManager().playlist_clear(ctx.player_id)
        return ["ok", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# Sub-command "playlist save" — routed via the base handler, not the registry.
async def cmd_playlist_save(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist save <name> — save the current playlist."""
    if not args:
        return ["playlist save: "]
    if handler._dispatcher:
        return await handler._dispatcher.player_command(
            ctx.player_id, "playlist save", args
        )
    return []


# Sub-command "playlist load" — routed via the base handler, not the registry.
async def cmd_playlist_load(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist load <name> — load a saved playlist."""
    if not args:
        return ["playlist load: "]
    if handler._dispatcher:
        return await handler._dispatcher.player_command(
            ctx.player_id, "playlist load", args
        )
    return []


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def _search_lms(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """LMS grouped search: 'search <start> <count> term:<begriff>'.

    Response: 'search <start> <count> count:<total> artists_count:N
    albums_count:N genres_count:N tracks_count:N' followed by the page
    items per group.
    """
    nums = [int(a) for a in args if str(a).isdigit()]
    start = nums[0] if nums else 0
    count = nums[1] if len(nums) > 1 else 20
    term = next((str(a)[5:] for a in args if str(a).startswith("term:")), "")
    if not term:
        return ["search: no term", ""]
    like = f"%{term}%"
    try:
        artists = await _query_db(
            "SELECT DISTINCT c.id, c.name FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1 "
            "WHERE c.name LIKE ? ORDER BY c.name COLLATE NOCASE LIMIT ? OFFSET ?",
            (like, count, start),
        )
        albums = await _query_db(
            "SELECT id, title FROM albums WHERE title LIKE ? "
            "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
            (like, count, start),
        )
        genres = await _query_db(
            "SELECT DISTINCT genre AS name FROM tracks WHERE genre LIKE ? "
            "ORDER BY genre COLLATE NOCASE LIMIT ? OFFSET ?",
            (like, count, start),
        )
        tracks = await _query_db(
            "SELECT id, title FROM tracks WHERE title LIKE ? "
            "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
            (like, count, start),
        )
        a_total = await _query_db(
            "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1 "
            "WHERE c.name LIKE ?", (like,))
        al_total = await _query_db(
            "SELECT COUNT(*) AS n FROM albums WHERE title LIKE ?", (like,))
        g_total = await _query_db(
            "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre LIKE ?",
            (like,))
        t_total = await _query_db(
            "SELECT COUNT(*) AS n FROM tracks WHERE title LIKE ?", (like,))
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]
    a_n = a_total[0]["n"] if a_total else 0
    al_n = al_total[0]["n"] if al_total else 0
    g_n = g_total[0]["n"] if g_total else 0
    t_n = t_total[0]["n"] if t_total else 0
    out = [
        f"search {start} {count} count:{a_n + al_n + g_n + t_n} "
        f"artists_count:{a_n} albums_count:{al_n} genres_count:{g_n} "
        f"tracks_count:{t_n}",
    ]
    for r in artists:
        out.append(f"id:{r['id']} artist:{r['name']}")
    for r in albums:
        out.append(f"id:{r['id']} album:{r['title']}")
    for i, r in enumerate(genres):
        out.append(f"id:{i + 1} genre:{r['name']}")
    for r in tracks:
        out.append(f"id:{r['id']} title:{r['title']}")
    out.append("")
    return out


@register_command("search")
async def cmd_search(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    search <type> <query> [0 <limit>] — legacy typed search
    search <start> <count> term:<begriff> — LMS grouped search
    Types: tracks, artists, albums, genres, playlists, titles
    """
    if not args:
        return ["search: "]
    # LMS grouped format: search <start> <count> term:<begriff>
    if any(str(a).startswith("term:") for a in args):
        return await _search_lms(handler, ctx, args)
    search_type = args[0].lower()
    query = args[1] if len(args) > 1 else ""
    offset = int(args[2]) if len(args) > 2 else 0
    limit = int(args[3]) if len(args) > 3 else 100

    # Direct library search (works without a RequestDispatcher)
    try:
        like = f"%{query}%"
        if search_type in ("tracks", "songs", "titles"):
            rows = await _query_db(
                "SELECT id, title, genre, year FROM tracks "
                "WHERE title LIKE ? ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, limit, offset),
            )
            out = [str(len(rows)), f"{offset} {limit}"]
            for r in rows:
                out.append(f"id:{r['id']}")
                out.append(f"title:{r['title']}")
                if r["year"]:
                    out.append(f"year:{r['year']}")
        elif search_type == "artists":
            rows = await _query_db(
                "SELECT id, name FROM contributors WHERE name LIKE ? "
                "ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, limit, offset),
            )
            out = [str(len(rows)), f"{offset} {limit}"]
            for r in rows:
                out.append(f"id:{r['id']}")
                out.append(f"artist:{r['name']}")
        elif search_type == "albums":
            rows = await _query_db(
                "SELECT id, title, year FROM albums WHERE title LIKE ? "
                "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, limit, offset),
            )
            out = [str(len(rows)), f"{offset} {limit}"]
            for r in rows:
                out.append(f"id:{r['id']}")
                out.append(f"album:{r['title']}")
        elif search_type == "genres":
            rows = await _query_db(
                "SELECT DISTINCT genre AS name FROM tracks WHERE genre LIKE ? "
                "ORDER BY genre COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, limit, offset),
            )
            out = [str(len(rows)), f"{offset} {limit}"]
            for i, r in enumerate(rows):
                out.append(f"id:{offset + i + 1}")
                out.append(f"genre:{r['name']}")
        else:
            return [f"search: unknown type '{search_type}'", ""]
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("rescan")
async def cmd_rescan(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """rescan [once] — trigger a media library rescan in the background."""
    mode = args[0] if args else "normal"
    try:
        import asyncio

        from lyrion.media.importer import ImportConfig, MusicImporter

        async def _do() -> None:
            try:
                import logging as _logging
                from pathlib import Path as _Path
                from lyrion.config import get_config
                from lyrion.media.importer import ImportConfig, MusicImporter
                musicdir = get_config().get("musicdir", "") or ""
                if not str(musicdir).strip():
                    fallback = _Path.home() / "Music"
                    _logging.getLogger("lyrion").warning(
                        "Preference 'musicdir' is empty — falling back to %s "
                        "(set it via serverpref)", fallback)
                    musicdir = str(fallback)
                imp = MusicImporter(ImportConfig(source_path=_Path(musicdir)))
                await imp.import_music()
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger("lyrion").warning("Rescan failed: %s", exc)

        asyncio.create_task(_do())
        return [f"rescan: {mode} started"]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]

@register_command("wipecache")
async def cmd_wipecache(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """wipecache — clear the album art and other cached data."""
    try:
        from pathlib import Path
        from lyrion.config import get_config
        cache_dir = Path(get_config().cache_dir)
        removed = 0
        if cache_dir.is_dir():
            for p in cache_dir.rglob("*"):
                if p.is_file():
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
        return [f"wipecache: removed {removed} files", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# ---------------------------------------------------------------------------
# Display / IR / Misc player commands
# ---------------------------------------------------------------------------


@register_command("display")
async def cmd_display(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """display <line1> <line2> [<duration>] — show text on player display.

    Sent as a slimproto 'grfe' frame (two-line text). Software players
    (squeezelite/jive) render it on their UI.
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        line1 = args[0] if args else ""
        line2 = args[1] if len(args) > 1 else ""
        duration = int(args[2]) if len(args) > 2 and str(args[2]).isdigit() else 3
        from lyrion.player import PlayerManager
        ok = await PlayerManager().show_display(ctx.player_id, line1, line2, duration)
        return [] if ok else ["cli error: could not display"]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}"]


@register_command("ir")
async def cmd_ir(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """ir <button_code> — simulate an IR button press on the player.

    The button code is a numeric SlimProto IR code. Named buttons
    ('play','pause','arrow_up',...) are mapped to their codes here so
    clients can use either form. Sent as an 'irm' slimproto frame.
    """
    if not ctx.player_id:
        return ["no player selected"]
    if not args:
        return ["ir requires a button code or name"]
    try:
        code = _resolve_ir_code(args[0])
        from lyrion.player import PlayerManager
        ok = await PlayerManager().send_ir(ctx.player_id, code)
        return [] if ok else ["cli error: could not send ir"]
    except ValueError:
        return [f"ir: unknown button '{args[0]}'"]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}"]


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
    """
    pref <key> [<value>]
    Query or set a server preference.
    """
    if not args:
        return []
    key = args[0]
    # LMS 'pref <key> ?' is a QUERY — set value to None so the
    # dispatcher reads the current value instead of writing '?'.
    value = None
    if len(args) > 1 and args[1] != "?":
        value = args[1]
    if handler._dispatcher:
        return await handler._dispatcher.get_set_preference(key, value)
    return []


@register_command("alarms")
async def cmd_alarms(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """alarms [<start> <count>] — list alarm clocks.

    LMS returns count + fade (global fade-in seconds). Each configured
    alarm is reported via an 'alarm <index>' line like SqueezePlay expects.
    """
    from lyrion.alarms import AlarmManager, alarm_query_string

    mac = ctx.player_id or ""
    if mac == "-":
        mac = ""
    mgr = AlarmManager()
    alarms = mgr.alarms_for(mac)
    lines = [f"alarms count:{len(alarms)}", "alarms fade:0", ""]
    for idx in sorted(alarms):
        lines.append(alarm_query_string(idx, alarms[idx]) + "\n")
    lines.append("")
    return lines


@register_command("alarm")
async def cmd_alarm(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """alarm <index> [key:value ...] — query or set a single alarm.

    Query form:  'alarm <index> ?'  → returns the alarm's fields.
    Set form:    'alarm <index> enabled:1 days:1111111 time:06:30 ...'
    Delete form: 'alarm <index> delete'
    """
    from lyrion.alarms import (AlarmManager, _alarm_from_parts,
                               alarm_query_string)

    if not args:
        return ["alarm: ", ""]
    try:
        idx = int(args[0])
    except ValueError:
        return ["alarm: invalid index", ""]

    mac = ctx.player_id or ""
    if mac == "-":
        mac = ""
    mgr = AlarmManager()

    if len(args) == 1 or (len(args) == 2 and args[1] == "?"):
        a = mgr.get(mac, idx)
        return [alarm_query_string(idx, a), ""]

    if len(args) >= 2 and args[1] == "delete":
        mgr.delete(mac, idx)
        return [alarm_query_string(idx, None), ""]

    # Set form: collect key:value pairs (and tolerate a bare '0'/'1' toggle).
    parts: dict[str, str] = {}
    for tok in args[1:]:
        if ":" in tok:
            k, v = tok.split(":", 1)
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
    return [alarm_query_string(idx, a), ""]


# ---------------------------------------------------------------------------
# Subscribe / Unsubscribe
# ---------------------------------------------------------------------------


@register_command("subscribe")
async def cmd_subscribe(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    subscribe <playerId> [<interval>]
    Subscribe to status updates for a player. The server pushes the
    current status whenever the player state changes (STAT event) and at
    least every <interval> seconds (keep-alive). Unsubscribe with
    'unsubscribe'.
    """
    if not args:
        return ["subscribe: "]
    player_id = args[0]
    interval = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 5
    ctx.subscribed_player = player_id
    ctx.subscribe_interval = max(1, interval)
    if player_id not in handler._subscriptions:
        handler._subscriptions[player_id] = asyncio.Queue()
    return [f"subscribe: {player_id} {ctx.subscribe_interval}"]


@register_command("unsubscribe")
async def cmd_unsubscribe(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """unsubscribe — cancel player subscription."""
    player_id = ctx.subscribed_player
    ctx.subscribed_player = None
    ctx.subscribe_interval = 0
    if player_id:
        handler._subscriptions.pop(player_id, None)
    return ["unsubscribe: done"]


# ---------------------------------------------------------------------------
# Info commands (format queries)
# ---------------------------------------------------------------------------


@register_command("info")
async def cmd_info(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """info total <genres|artists|albums|songs|duration> [?] — library stats.

    Response (LMS format, no colon): 'info total songs <n>'.
    'info' without args returns all totals.
    """
    try:
        want = " ".join(a.lower() for a in args)
        totals: dict[str, str] = {}

        rows = await _query_db(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration),0) AS d FROM tracks"
        )
        songs = int(rows[0]["n"]) if rows else 0
        totals["songs"] = str(songs)
        totals["duration"] = str(int(rows[0]["d"]) if rows else 0)

        r_art = await _query_db(
            "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
        )
        totals["artists"] = str(int(r_art[0]["n"]) if r_art else 0)

        r_alb = await _query_db("SELECT COUNT(*) AS n FROM albums")
        totals["albums"] = str(int(r_alb[0]["n"]) if r_alb else 0)

        r_gen = await _query_db(
            "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre != ''"
        )
        totals["genres"] = str(int(r_gen[0]["n"]) if r_gen else 0)

        # info total X [?] — single query; info — all totals
        if want.startswith("total "):
            key = want.split(" ", 1)[1].rstrip("?").strip()
            if key in totals:
                return [f"info total {key} {totals[key]}", ""]
            return ["info total ?", ""]
        if not want:
            out = [f"info total {k} {v}" for k, v in totals.items()]
            out.append("")
            return out
        out = [f"info total {k} {v}" for k, v in totals.items() if k in want]
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


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


def _browse_where(filters: dict[str, str], cols: dict[str, str]) -> tuple[str, tuple]:
    """Build WHERE clause + params for library browse queries.

    cols maps a filter name to an SQL expression, e.g.
    {"search": "t.title", "year": "t.year", "genre": "t.genre",
     "track_id": "t.id", "album_id": "ta.album", "artist_id": "tc.contributor"}.
    genre_id is intentionally not supported: the genres table is not
    populated, tracks carry the genre as text — use genre:<text>.
    """
    conds: list[str] = []
    params: list[str] = []
    for key, expr in cols.items():
        val = filters.get(key)
        if not val:
            continue
        if key == "search":
            conds.append(f"{expr} LIKE ?")
            params.append(f"%{val}%")
        elif key == "genre":
            conds.append(f"{expr} LIKE ?")
            params.append(f"%{val}%")
        else:  # year, track_id, album_id, artist_id
            conds.append(f"{expr} = ?")
            params.append(val)
    return (" WHERE " + " AND ".join(conds)) if conds else "", tuple(params)


@register_command("artists")
async def cmd_artists(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """artists [<offset> <limit>] [search:|artist_id:|album_id:|year:|genre:] — list artists.

    Response (LMS tagged format): 'artists <offset> <limit> count:<total>'
    followed by one line per artist: 'id:<n> artist:<name>'.
    """
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
    out = [f"artists {offset} {limit} count:{total_n}"]
    for r in rows:
        out.append(f"id:{r['id']} artist:{r['name'] or ''}")
    return out


@register_command("albums")
async def cmd_albums(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """albums [<offset> <limit>] [search:|album_id:|artist_id:|year:|genre:] — list albums.

    Response (LMS tagged format): 'albums <offset> <limit> count:<total>'
    followed by one line per album: 'id:<n> album:<title> [year:<y>]'.
    """
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
    out = [f"albums {offset} {limit} count:{total_n}"]
    for r in rows:
        line = f"id:{r['id']} album:{r['title'] or ''}"
        if r["year"]:
            line += f" year:{r['year']}"
        out.append(line)
    return out


@register_command("songs")
@register_command("titles")
async def cmd_songs(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """songs [<offset> <limit>] [search:|track_id:|album_id:|artist_id:|year:|genre:] — list tracks.

    Response (LMS tagged format): 'songs <offset> <limit> count:<total>'
    followed by one line per track: 'id:<n> title:<t> [genre:] [year:] [tracknum:] [duration:]'.
    'titles' is a registered alias.
    """
    offset, limit, filters = _parse_query_args(args)
    where, params = _browse_where(filters, {
        "search": "t.title", "track_id": "t.id", "album_id": "ta.album",
        "artist_id": "tc.contributor", "year": "t.year", "genre": "t.genre",
    })
    joins = ""
    if filters.get("album_id"):
        joins += " JOIN tracks_albums ta ON ta.track = t.id"
    if filters.get("artist_id"):
        joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
    rows = await _query_db(
        "SELECT DISTINCT t.id, t.title, t.genre, t.year, t.tracknum, t.duration FROM tracks t"
        + joins + where +
        " ORDER BY t.title COLLATE NOCASE LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    total = await _query_db(
        "SELECT COUNT(DISTINCT t.id) AS n FROM tracks t" + joins + where,
        params,
    )
    total_n = total[0]["n"] if total else 0
    # Echo the invoked command name ('songs' or the 'titles' alias).
    name = getattr(ctx, "command", "") or "songs"
    out = [f"{name} {offset} {limit} count:{total_n}"]
    for r in rows:
        line = f"id:{r['id']} title:{r['title'] or ''}"
        if r["genre"]:
            line += f" genre:{r['genre']}"
        if r["year"]:
            line += f" year:{r['year']}"
        if r["tracknum"]:
            line += f" tracknum:{r['tracknum']}"
        if r["duration"]:
            line += f" duration:{int(r['duration'])}"
        out.append(line)
    return out


@register_command("genres")
async def cmd_genres(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """genres [<offset> <limit>] [search:] — list genres (from track genre text).

    Response (LMS tagged format): 'genres <offset> <limit> count:<total>'
    followed by one line per genre: 'id:<n> genre:<name>'.
    """
    offset, limit, filters = _parse_query_args(args)
    where, params = _browse_where(filters, {"search": "t.genre"})
    base = "FROM tracks t WHERE t.genre != ''"
    if where:
        where = where.replace(" WHERE ", " AND ", 1)
    rows = await _query_db(
        "SELECT DISTINCT t.genre AS name " + base + where +
        " ORDER BY t.genre COLLATE NOCASE LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    total = await _query_db(
        "SELECT COUNT(DISTINCT t.genre) AS n " + base + where,
        params,
    )
    total_n = total[0]["n"] if total else 0
    out = [f"genres {offset} {limit} count:{total_n}"]
    for i, r in enumerate(rows):
        out.append(f"id:{offset + i + 1} genre:{r['name'] or ''}")
    return out


@register_command("playlists")
async def cmd_playlists(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    playlists [<offset> <limit>] — list saved playlists
    playlists tracks <id> — list the tracks of a playlist
    playlists new <name> | delete <id> | rename <id> <newname>
    """
    sub = str(args[0]).lower() if args else ""
    if sub == "tracks" and len(args) >= 2 and str(args[1]).isdigit():
        pid = int(args[1])
        rows = await _query_db(
            "SELECT pi.position, pi.track, pi.url, t.title, t.duration "
            "FROM playlist_items pi LEFT JOIN tracks t ON t.id = pi.track "
            "WHERE pi.playlist = ? ORDER BY pi.position",
            (pid,),
        )
        out = [f"playlists tracks {pid} count:{len(rows)}"]
        for r in rows:
            line = f"id:{r['track'] or r['url'] or ''} position:{r['position']}"
            if r["title"]:
                line += f" title:{r['title']}"
            if r["url"]:
                line += f" url:{r['url']}"
            if r["duration"]:
                line += f" duration:{int(r['duration'])}"
            out.append(line)
        out.append("")
        return out
    if sub == "new" and len(args) >= 2:
        name = " ".join(args[1:])
        await _write_db(
            "INSERT INTO playlists (playlist, name, changed, pl_type) "
            "VALUES (?, ?, datetime('now'), 0)",
            (name, name),
        )
        row = await _query_db(
            "SELECT id FROM playlists WHERE name = ? ORDER BY id DESC LIMIT 1", (name,)
        )
        new_id = row[0]["id"] if row else "?"
        return [f"playlists new: {new_id}", ""]
    if sub == "delete" and len(args) >= 2 and str(args[1]).isdigit():
        pid = int(args[1])
        await _write_db("DELETE FROM playlist_items WHERE playlist = ?", (pid,))
        await _write_db("DELETE FROM playlists WHERE id = ?", (pid,))
        return [f"playlists delete: {pid}", ""]
    if sub == "rename" and len(args) >= 3 and str(args[1]).isdigit():
        pid = int(args[1])
        name = " ".join(args[2:])
        await _write_db(
            "UPDATE playlists SET name = ?, playlist = ? WHERE id = ?",
            (name, name, pid),
        )
        return [f"playlists rename: {pid} {name}", ""]
    # default: list playlists (LMS tagged format)
    offset, limit, _ = _parse_query_args(args)
    rows = await _query_db(
        "SELECT id, name FROM playlists ORDER BY name COLLATE NOCASE "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    )
    total = await _query_db("SELECT COUNT(*) AS n FROM playlists")
    total_n = total[0]["n"] if total else 0
    out = [f"playlists {offset} {limit} count:{total_n}"]
    for r in rows:
        out.append(f"id:{r['id']} playlist:{r['name']}")
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Internet radio
# ---------------------------------------------------------------------------


@register_command("radio")
async def cmd_radio(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio [list|add|delete|search|top|play] — manage internet radio.

    The CLI parser matches the bare "radio" command and passes the rest
    in args, so this router dispatches the sub-commands itself.
    """
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
    return [f"radio: unknown sub-command '{sub}'", "radio [list|add|delete|search|top|play]", ""]


async def cmd_radio_list(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio list — list saved radio stations."""
    from lyrion.music.radio import get_radio_manager

    stations = await get_radio_manager().list_stations()
    out = [f"radio count: {len(stations)}"]
    for s in stations:
        out.extend(s.to_cli_lines())
    out.append("")
    return out


# Sub-command "radio add" — routed via the base handler, not the registry.
async def cmd_radio_add(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio add <url> [name] — manually add a radio stream."""
    if not args:
        return ["radio add <url> [name] — missing URL", ""]
    url = args[0]
    name = " ".join(args[1:]) if len(args) > 1 else url
    try:
        from lyrion.music.radio import get_radio_manager
        station = await get_radio_manager().add_station(name, url)
        return [
            "added",
            f"id: {station.id}",
            f"name: {station.name}",
            f"url: {station.url}",
            "",
        ]
    except ValueError as e:
        return [f"cli error: {e}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# Sub-command "radio delete" — routed via the base handler, not the registry.
async def cmd_radio_delete(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio delete <id> — remove a saved station."""
    if not args:
        return ["radio delete <id> — missing id", ""]
    try:
        station_id = int(args[0])
        from lyrion.music.radio import get_radio_manager
        ok = await get_radio_manager().remove_station(station_id)
        return ["deleted" if ok else "not found", ""]
    except ValueError:
        return ["cli error: id must be a number", ""]


# Sub-command "radio search" — routed via the base handler, not the registry.
async def cmd_radio_search(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio search <query> [limit] [tag:<tag>] [country:<CC>] — search radio directory."""
    if not args:
        return ["radio search <query> [limit] [tag:<tag>] [country:<CC>] — missing query", ""]
    name_parts: list[str] = []
    tag = ""
    country = ""
    limit = 20
    for a in args:
        if a.lower().startswith("tag:") and len(a) > 4:
            tag = a[4:]
        elif a.lower().startswith("country:") and len(a) > 8:
            country = a[8:]
        elif str(a).isdigit():
            limit = min(int(str(a)), 100)
        else:
            name_parts.append(a)
    query = " ".join(name_parts).strip()
    try:
        from lyrion.music.radio import get_radio_manager
        stations = await get_radio_manager().directory.search(
            name=query, tag=tag, country=country, limit=limit
        )
        out = [f"radio search results: {len(stations)}"]
        for s in stations:
            out.extend(s.to_cli_lines())
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# Sub-command "radio top" — routed via the base handler, not the registry.
async def cmd_radio_top(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio top [n] — top stations from public directory."""
    limit = 20
    if args and str(args[0]).isdigit():
        limit = min(int(str(args[0])), 100)
    try:
        from lyrion.music.radio import get_radio_manager
        stations = await get_radio_manager().directory.top(limit=limit)
        out = [f"radio top: {len(stations)}"]
        for s in stations:
            out.extend(s.to_cli_lines())
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


# Sub-command "radio play" — routed via the base handler, not the registry.
async def cmd_radio_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """radio play <station_id> [player_id] — play a saved station.

    The player defaults to the request context (LMS style:
    slim.request <player_id> ["radio", "play", "<station_id>"]).
    """
    if not args:
        return ["radio play <station_id> [player_id] — missing station id", ""]
    try:
        station_id = int(args[0])
        player_id = args[1] if len(args) > 1 else ctx.player_id
        if not player_id:
            return ["no player selected — pass player_id or call via slim.request <player_id>", ""]
        from lyrion.music.radio import get_radio_manager
        station = await get_radio_manager().play_station(player_id, station_id=station_id)
        if station is None:
            return ["cli error: station not found", ""]
        return [f"playing: {station.name}", f"url: {station.url}", ""]
    except ValueError:
        return ["cli error: station id must be a number", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


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
    return [
        f"favorites: unknown sub-command '{sub}'",
        "favorites [items|add|addfolder|delete|move|rename|play|exists|playlist]",
        "",
    ]


async def _fav_items(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    parent = _parse_parent_id(args[0]) if args else None
    try:
        from lyrion.music.favorites import get_favorites_manager
        items = await get_favorites_manager().list_items(parent)
        out = [f"favorites count: {len(items)}"]
        for item in items:
            out.append(f"favorite id: {item['id']}")
            out.append(f"  title: {item['title']}")
            out.append(f"  type: {item['type']}")
            if item["url"]:
                out.append(f"  url: {item['url']}")
            out.append(f"  position: {item['position']}")
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_add(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if not args:
        return ["favorites add [url:<u> title:<t> parent:<id>] — missing url/title", ""]
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
        return ["favorites add [url:<u> title:<t> parent:<id>] — missing url/title", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        new_id = await get_favorites_manager().add(title, url, parent)
        if new_id is None:
            return ["cli error: could not add favorite", ""]
        return [f"added: {new_id}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_add_folder(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if not args:
        return ["favorites addfolder <title> [parent_id] — missing title", ""]
    title = str(args[0])
    parent = _parse_parent_id(args[1]) if len(args) > 1 else None
    try:
        from lyrion.music.favorites import get_favorites_manager
        new_id = await get_favorites_manager().add(title, None, parent)
        if new_id is None:
            return ["cli error: could not add folder", ""]
        return [f"added: {new_id}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


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
    if not args:
        return ["favorites delete <id> — missing id", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        fm = get_favorites_manager()
        fav_id = await _fav_resolve_id(fm, str(args[0]))
        if fav_id is None:
            return ["favorites delete <id> — missing id", ""]
        ok = await fm.delete(fav_id)
        return ["deleted"] if ok else ["cli error: favorite not found", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_move(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if not args:
        return ["favorites move <id> <parent_id> [position] — missing id", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        fm = get_favorites_manager()
        fav_id = await _fav_resolve_id(fm, str(args[0]))
        if fav_id is None:
            return ["favorites move <id> <parent_id> [position] — missing id", ""]
        parent = None
        if len(args) > 1:
            parent = await _fav_resolve_id(fm, str(args[1])) if str(args[1]) != "0" else None
        position = int(str(args[2])) if len(args) > 2 and str(args[2]).isdigit() else None
        ok = await fm.move(fav_id, parent, position)
        return ["moved"] if ok else ["cli error: move failed", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_rename(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if len(args) < 2:
        return ["favorites rename <id> <title> [url] — missing id/title", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        fm = get_favorites_manager()
        fav_id = await _fav_resolve_id(fm, str(args[0]))
        if fav_id is None:
            return ["favorites rename <id> <title> [url] — missing id/title", ""]
        url = str(args[2]) if len(args) > 2 else None
        ok = await fm.rename(fav_id, str(args[1]), url)
        return ["renamed"] if ok else ["cli error: favorite not found", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if not args:
        return ["favorites play <id> — missing id", ""]
    player_id = args[1] if len(args) > 1 else ctx.player_id
    if not player_id:
        return ["no player selected — pass player_id or call via slim.request <player_id>", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        fm = get_favorites_manager()
        fav_id = await _fav_resolve_id(fm, str(args[0]))
        if fav_id is None:
            return ["favorites play <id> — missing id", ""]
        ok = await fm.play(player_id, fav_id)
        if ok:
            return []
        return [
            "cli error: could not start playback",
            "(favorite missing, not a stream, or player not connected)",
            "",
        ]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_exists(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites exists <url|id> — 'exists:1' if the favorite exists, else 'exists:0'."""
    if not args:
        return ["favorites exists <url|id> — missing value", ""]
    val = str(args[0])
    try:
        if val.isdigit():
            rows = await _query_db("SELECT id FROM favorites WHERE id = ? LIMIT 1", (int(val),))
        else:
            rows = await _query_db("SELECT id FROM favorites WHERE url = ? LIMIT 1", (val,))
        return [f"exists: {1 if rows else 0}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_playlist(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """favorites playlist <play|load|insert|add> item_id:<id> [player:<mac>]."""
    if not args:
        return ["favorites playlist <play|load|insert|add> item_id:<id>", ""]
    action = str(args[0]).lower()
    filters: dict[str, str] = {}
    for a in args[1:]:
        s = str(a)
        if ":" in s:
            k, _, v = s.partition(":")
            filters[k] = v
    fav_id_raw = filters.get("item_id")
    if not fav_id_raw:
        return ["favorites playlist: missing item_id:<id>", ""]
    player_id = filters.get("player") or ctx.player_id
    if not player_id:
        return ["no player selected", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        from lyrion.player.manager import PlayerManager
        fm = get_favorites_manager()
        fav_id = await _fav_resolve_id(fm, fav_id_raw)
        if fav_id is None:
            return ["favorites playlist: unknown item_id", ""]
        if action in ("play", "load"):
            ok = await fm.play(player_id, fav_id)
            return [] if ok else ["cli error: could not start playback", ""]
        if action in ("insert", "add"):
            items = await fm.list_items(None)
            target = next((i for i in items if int(i["id"]) == fav_id), None)
            if not target or not target["url"]:
                return ["cli error: favorite not a stream", ""]
            player = PlayerManager().get_player(player_id)
            if player is None:
                return ["player not found", ""]
            player.playlist.append(target["url"])
            player.playlist_total = len(player.playlist)
            return []
        return [f"favorites playlist: unknown action '{action}'", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


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
