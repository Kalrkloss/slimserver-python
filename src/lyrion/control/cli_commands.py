"""
Built-in CLI command implementations for Lyrion Music Server.

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


# ---------------------------------------------------------------------------
# Server info
# ---------------------------------------------------------------------------


@register_command("version")
async def cmd_version(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """version — return server version string."""
    return ["9.2.0 Lyrion Music Server"]


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
    Return server status with optional pagination window.
    """
    # Wire up to actual server state
    try:
        from lyrion.player import PlayerManager
        player_count = PlayerManager().get_connected_count()
    except Exception:
        player_count = 0
    return [
        "serverstatus",
        "version: 9.2.0",
        "uuid: lyrion-local",
        "name: Lyrion",
        "info total duration: 0",
        f"player count: {player_count}",
        "",
    ]


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
    player [<playerid>]
    Set or query the default player for this CLI session.
    """
    if args:
        ctx.player_id = args[0]
        return [f"player: {ctx.player_id}"]
    if ctx.player_id:
        return [f"player: {ctx.player_id}"]
    return ["player: "]


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
        if handler._dispatcher:
            return await handler._dispatcher.player_command(ctx.player_id, "mixer", args)
        return [f"mixer: unsupported parameter '{args[0] if args else ''}'", ""]
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
    status [<subscribe:<seconds>>] [<window:<start>:<end>>]
    Return current playback status for the default player.
    """
    if not ctx.player_id:
        return ["no player selected"]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        if player is None:
            return ["player not found", ""]
        mode_map = {"play": "playing", "pause": "paused", "stop": "stopped",
                    "loading": "loading"}
        out = [
            "status",
            f"player_name: {player.name}",
            f"player_connected: {'1' if player.connected else '0'}",
            f"power: {'1' if player.power else '0'}",
            f"mode: {mode_map.get(player.mode, player.mode)}",
            f"time: 0",
            f"rate: 1",
            f"volume: {player.volume}",
            f"duration: 0",
            f"playlist_tracks: {player.playlist_total}",
            f"playlist_cur_index: {player.playlist_position}",
        ]
        if player.current_track_id is not None:
            out.append(f"playlist_cur_id: {player.current_track_id}")
        if getattr(player, "current_title", None):
            out.append(f"current_title: {player.current_title}")
        if getattr(player, "current_url", None):
            out.append(f"current_url: {player.current_url}")
        out.append("")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


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
        return [f"playlist: unknown subcommand '{sub}'", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("playlist play")
async def cmd_playlist_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlist play <trackId|index> — play a track in the playlist."""
    if not ctx.player_id:
        return ["no player selected"]
    if not args:
        return ["playlist play <trackId> — missing id", ""]
    try:
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(ctx.player_id)
        # If the arg is a valid playlist index, use it; otherwise treat as track id
        if str(args[0]).isdigit():
            idx = int(str(args[0]))
            if player and idx < len(player.playlist):
                ok = await pm.playlist_play(ctx.player_id, idx)
                return [f"playlist play {idx}", ""] if ok else ["cli error: could not play", ""]
        track_id = int(args[0])
        pm.playlist_add(ctx.player_id, track_id)
        ok = await pm.play_track(ctx.player_id, track_id)
        return [f"playlist play {track_id}", ""] if ok else ["cli error: could not play", ""]
    except ValueError:
        return ["cli error: track id must be a number", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("playlist add")
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
            try:
                pm.playlist_add(ctx.player_id, int(a))
                added += 1
            except ValueError:
                continue
        return [f"added: {added}", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


@register_command("playlist clear")
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


@register_command("playlist save")
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


@register_command("playlist load")
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


@register_command("search")
async def cmd_search(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """
    search <type> <query> [0 <limit>]
    Search the media library.
    Types: tracks, artists, albums, genres, playlists, titles
    """
    if not args:
        return ["search: "]
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
                imp = MusicImporter(ImportConfig())
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
        cache_dir = Path("/root/.lyrion/Lyrion/Cache")
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
    """display <line1> <line2> — show text on player display."""
    if not ctx.player_id:
        return ["no player selected"]
    if handler._dispatcher:
        return await handler._dispatcher.player_command(ctx.player_id, "display", args)
    return []


@register_command("ir")
async def cmd_ir(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """ir <button_code> — simulate an IR button press."""
    if not ctx.player_id:
        return ["no player selected"]
    if handler._dispatcher:
        return await handler._dispatcher.player_command(ctx.player_id, "ir", args)
    return []


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
    value = args[1] if len(args) > 1 else None
    if handler._dispatcher:
        return await handler._dispatcher.get_set_preference(key, value)
    return []


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
    subscribe <playerId> [<timeout>]
    Subscribe to status updates for a player.
    """
    if not args:
        return ["subscribe: "]
    player_id = args[0]
    timeout = int(args[1]) if len(args) > 1 else 0
    ctx.subscribed_player = player_id
    return [f"subscribe: {player_id} {timeout}"]


@register_command("unsubscribe")
async def cmd_unsubscribe(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """unsubscribe — cancel player subscription."""
    ctx.subscribed_player = None
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
    """info [total duration] [genres] [songs] [ratings] — return library stats."""
    try:
        rows = await _query_db(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration),0) AS d FROM tracks"
        )
        songs = int(rows[0]["n"]) if rows else 0
        total_duration = int(rows[0]["d"]) if rows else 0

        out = ["info total duration: " + str(total_duration)]
        want = " ".join(a.lower() for a in args)
        if "songs" in want or not args:
            out.append(f"info songs: {songs}")
        if "artists" in want:
            r2 = await _query_db("SELECT COUNT(*) AS n FROM contributors")
            out.append(f"info artists: {int(r2[0]['n']) if r2 else 0}")
        if "albums" in want:
            r3 = await _query_db("SELECT COUNT(*) AS n FROM albums")
            out.append(f"info albums: {int(r3[0]['n']) if r3 else 0}")
        if "genres" in want:
            r4 = await _query_db(
                "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre != ''"
            )
            out.append(f"info genres: {int(r4[0]['n']) if r4 else 0}")
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

        con = sqlite3.connect(f"file:{_LIBRARY_DB}?mode=ro", uri=True, timeout=30)
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


@register_command("artists")
async def cmd_artists(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """artists [0 <limit>] [search:<term>] — list artists in the library."""
    offset, limit, filters = _parse_query_args(args)
    search = filters.get("search", "")
    where, params = "", ()
    if search:
        where = "WHERE name LIKE ?"
        params = (f"%{search}%",)
    rows = await _query_db(
        "SELECT id, name FROM contributors "
        f"{where} ORDER BY name COLLATE NOCASE "
        "LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    out = [str(len(rows)), f"{offset} {limit}"]
    for r in rows:
        out.append(f"id:{r['id']}")
        out.append(f"artist:{r['name']}")
    return out


@register_command("albums")
async def cmd_albums(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """albums [0 <limit>] [search:<term>] — list albums in the library."""
    offset, limit, filters = _parse_query_args(args)
    search = filters.get("search", "")
    where, params = "", ()
    if search:
        where = "WHERE title LIKE ?"
        params = (f"%{search}%",)
    rows = await _query_db(
        "SELECT id, title, year FROM albums "
        f"{where} ORDER BY title COLLATE NOCASE "
        "LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    out = [str(len(rows)), f"{offset} {limit}"]
    for r in rows:
        out.append(f"id:{r['id']}")
        out.append(f"album:{r['title']}")
        if r["year"]:
            out.append(f"year:{r['year']}")
    return out


@register_command("songs")
async def cmd_songs(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """songs [0 <limit>] [search:<term>] — list tracks in the library."""
    offset, limit, filters = _parse_query_args(args)
    search = filters.get("search", "")
    where, params = "", ()
    if search:
        where = "WHERE title LIKE ?"
        params = (f"%{search}%",)
    rows = await _query_db(
        "SELECT id, title, genre, year, tracknum, duration FROM tracks "
        f"{where} ORDER BY title COLLATE NOCASE "
        "LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    out = [str(len(rows)), f"{offset} {limit}"]
    for r in rows:
        out.append(f"id:{r['id']}")
        out.append(f"title:{r['title']}")
        if r["genre"]:
            out.append(f"genre:{r['genre']}")
        if r["year"]:
            out.append(f"year:{r['year']}")
        if r["tracknum"]:
            out.append(f"tracknum:{r['tracknum']}")
        if r["duration"]:
            out.append(f"duration:{int(r['duration'])}")
    return out


@register_command("genres")
async def cmd_genres(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """genres [0 <limit>] — list genres in the library."""
    offset, limit, _ = _parse_query_args(args)
    rows = await _query_db(
        "SELECT DISTINCT genre AS name FROM tracks "
        "WHERE genre != '' ORDER BY genre COLLATE NOCASE LIMIT ? OFFSET ?",
        (limit, offset),
    )
    out = [str(len(rows)), f"{offset} {limit}"]
    for i, r in enumerate(rows):
        out.append(f"id:{offset + i + 1}")
        out.append(f"genre:{r['name']}")
    return out


@register_command("playlists")
async def cmd_playlists(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    """playlists [0 <limit>] — list saved playlists."""
    offset, limit, _ = _parse_query_args(args)
    rows = await _query_db(
        "SELECT id, name FROM playlists ORDER BY name COLLATE NOCASE "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    )
    out = [str(len(rows)), f"{offset} {limit}"]
    for r in rows:
        out.append(f"id:{r['id']}")
        out.append(f"playlist:{r['name']}")
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


@register_command("radio add")
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


@register_command("radio delete")
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


@register_command("radio search")
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


@register_command("radio top")
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


@register_command("radio play")
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
    favorites [items|add|addfolder|delete|move|rename|play]

    Manage radio favorites with nested folders (original LMS logic):
      favorites items [parent_id]
      favorites add <url> <title> [parent_id]
      favorites addfolder <title> [parent_id]
      favorites delete <id>
      favorites move <id> <parent_id> [position]
      favorites rename <id> <title>
      favorites play <id>
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
    return [
        f"favorites: unknown sub-command '{sub}'",
        "favorites [items|add|addfolder|delete|move|rename|play]",
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
    if len(args) < 2:
        return ["favorites add <url> <title> [parent_id] — missing url/title", ""]
    url = str(args[0])
    title = str(args[1])
    parent = _parse_parent_id(args[2]) if len(args) > 2 else None
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


async def _fav_delete(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if not args or not str(args[0]).isdigit():
        return ["favorites delete <id> — missing id", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        ok = await get_favorites_manager().delete(int(str(args[0])))
        return ["deleted"] if ok else ["cli error: favorite not found", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_move(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if not args or not str(args[0]).isdigit():
        return ["favorites move <id> <parent_id> [position] — missing id", ""]
    fav_id = int(str(args[0]))
    parent = _parse_parent_id(args[1]) if len(args) > 1 else None
    position = int(str(args[2])) if len(args) > 2 and str(args[2]).isdigit() else None
    try:
        from lyrion.music.favorites import get_favorites_manager
        ok = await get_favorites_manager().move(fav_id, parent, position)
        return ["moved"] if ok else ["cli error: move failed", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_rename(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if len(args) < 2 or not str(args[0]).isdigit():
        return ["favorites rename <id> <title> [url] — missing id/title", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        url = str(args[2]) if len(args) > 2 else None
        ok = await get_favorites_manager().rename(int(str(args[0])), str(args[1]), url)
        return ["renamed"] if ok else ["cli error: favorite not found", ""]
    except Exception as e:  # noqa: BLE001
        return [f"cli error: {e}", ""]


async def _fav_play(
    handler: CLIHandler,
    ctx: CLIContext,
    args: list[str],
) -> list[str]:
    if not args or not str(args[0]).isdigit():
        return ["favorites play <id> — missing id", ""]
    player_id = args[1] if len(args) > 1 else ctx.player_id
    if not player_id:
        return ["no player selected — pass player_id or call via slim.request <player_id>", ""]
    try:
        from lyrion.music.favorites import get_favorites_manager
        ok = await get_favorites_manager().play(player_id, int(str(args[0])))
        if ok:
            return []
        return [
            "cli error: could not start playback",
            "(favorite missing, not a stream, or player not connected)",
            "",
        ]
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
