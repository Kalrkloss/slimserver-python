"""
LMS-compatible CLI server for Lyrion Music Server.

Provides a telnet/text-mode CLI on port 9090 compatible with
Logitech Media Server CLI specification. This is the PRIMARY
interface used by remote-control apps (SqueezeCtrl, Squeezer,
SqueezeTray, etc.).

Protocol:
  - Connect → server sends nothing (or password prompt)
  - Client sends "command args ?" → server responds with tagged lines
  - Client sends "subscribe command args" → server streams updates
  - Tags: response lines are prefixed with the query + space
"""
import asyncio
import logging
from urllib.parse import quote

logger = logging.getLogger("lyrion.control.cli")

# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────

async def start_cli_server(port: int = 9090) -> None:
    """Start the LMS-compatible CLI server."""
    server = await asyncio.start_server(handle_client, host="0.0.0.0", port=port)
    logger.info("LMS CLI server listening on port %d", port)
    async with server:
        await server.serve_forever()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle a single CLI client connection (LMS-compatible)."""
    addr = writer.get_extra_info("peername")
    logger.debug("CLI client connected from %s", addr)
    # LMS sends no greeting by default (some clients expect nothing)

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode("utf-8", errors="replace").strip()
            if not cmd:
                continue
            if cmd.lower() in ("exit", "logout", "bye"):
                writer.write(b"bye\n")
                await writer.drain()
                break
            response = await process_lms_command(cmd)
            if response:
                writer.write(response.encode("utf-8") + b"\n")
                await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()
        logger.debug("CLI client disconnected from %s", addr)


# ─────────────────────────────────────────────────────────────────────
# Command processing
# ─────────────────────────────────────────────────────────────────────

async def process_lms_command(raw: str) -> str:
    """Process an LMS CLI command and return tagged response lines."""
    raw = raw.rstrip()
    if not raw:
        return ""

    # Handle LMS "subscribe" prefix
    is_subscribe = False
    if raw.startswith("subscribe "):
        is_subscribe = True
        raw = raw[10:].strip()

    # Split: tags come as space-separated hex-encoded tokens (? = query, ! = subscribe)
    parts = raw.split()
    if not parts:
        return ""

    # LMS CLI format: playerid cmd arg1 arg2 ... [tag]
    # Or for server commands: cmd arg1 arg2 ... [tag]
    # Tags can be "?" (query), "!" (subscribe), or arbitrary hex tags

    tag = ""
    used_parts = list(parts)

    # Check if last part is a tag
    if used_parts[-1] == "?":
        tag = quote(used_parts[-1], safe="")
        used_parts = used_parts[:-1]
    elif used_parts[-1].startswith("%") or used_parts[-1] in ("?",):
        tag = used_parts[-1]
        used_parts = used_parts[:-1]

    # Reconstruct the command without tag
    tag_prefix = tag + " " if tag else ""

    cmd = used_parts[0].lower() if used_parts else ""

    # ── SERVER COMMANDS ──────────────────────────────────────────
    if cmd == "version":
        return format_tagged(tag, f"version {await get_version()}")

    if cmd == "serverstatus":
        return format_tagged(tag, await get_server_status())

    if cmd in ("rescan", "wipecache", "abortscan"):
        return format_tagged(tag, f"{cmd} started")

    # ── PLAYER COMMANDS ──────────────────────────────────────────
    if cmd == "players" or cmd == "player":
        return format_tagged(tag, await get_player_list())

    if cmd == "playerpref":
        return format_tagged(tag, f"playerpref {used_parts[1] if len(used_parts)>1 else ''} ok")

    if cmd in ("playlist", "status", "mode", "mixer", "signalstrength",
               "sync", "name", "genres", "albums", "artists", "titles",
               "songs", "songinfo", "titles", "musicfolder", "playlists",
               "radios", "favorites", "alarm", "display", "button"):
        # Generic: return empty or "ok"
        return format_tagged(tag, f"{cmd} {used_parts[1] if len(used_parts)>1 else '0'} ok")

    if cmd in ("info", "pref", "prefset", "playerpref"):
        return format_tagged(tag, f"{cmd} ok")

    # ── SUBSCRIPTION HANDLING ────────────────────────────────────
    if is_subscribe:
        return format_tagged(tag, f"subscribe:{used_parts[0]} ok")

    # ── FALLBACK ─────────────────────────────────────────────────
    # For any unknown command, return empty (LMS convention: no response = unknown)
    return ""


def format_tagged(tag: str, response: str) -> str:
    """Format a tagged CLI response. If tag is empty, return raw response."""
    if not tag:
        return response
    # LMS sends multi-line tagged responses with the tag prefix on each line
    return "\n".join(f"{tag} {line}" for line in response.split("\n") if line.strip())


# ─────────────────────────────────────────────────────────────────────
# Data providers
# ─────────────────────────────────────────────────────────────────────

async def get_version() -> str:
    """Return server version."""
    from lyrion import __version__
    return __version__


async def get_player_list() -> str:
    """Return player list in LMS CLI format."""
    try:
        from lyrion.player.manager import PlayerManager
        players = PlayerManager().get_all_players()
    except Exception:
        return "player count:0"

    if not players:
        return "player count:0"

    lines = [f"player count:{len(players)}"]
    for i, p in enumerate(players):
        mac_encoded = p.mac.replace(":", "%3A")
        name_encoded = quote(p.name or p.mac, safe="")
        model = p.model or "squeezebox"
        ip = p.ip or "0.0.0.0"
        power = "1" if p.power else "0"
        connected = "1" if p.connected else "0"
        playerid = mac_encoded
        lines.append(
            f"playerid:{playerid} "
            f"playerindex:{i} "
            f"uuid: "
            f"ip:{ip}:{p.port or 0} "
            f"name:{name_encoded} "
            f"model:{model} "
            f"modelname:{model} "
            f"isplaying:0 "
            f"displaytype:none "
            f"isplayer:1 "
            f"canpoweroff:1 "
            f"connected:{connected} "
            f"power:{power} "
            f"firmware:1 "
            f"seq_no:0 "
            f"sn.player.count:{len(players)}"
        )
    return "\n".join(lines) + "\n"


async def get_server_status() -> str:
    """Return full server status."""
    from lyrion import __version__
    try:
        from lyrion.player.manager import PlayerManager
        players = PlayerManager().get_all_players()
        player_count = len(players)
    except Exception:
        player_count = 0

    lines = [
        f"serverstatus version:{__version__}",
        f"serverstatus uuid:lyrion-server-0001",
        f"serverstatus httpport:9000",
        f"serverstatus info total genres:0",
        f"serverstatus info total artists:0",
        f"serverstatus info total albums:0",
        f"serverstatus info total songs:0",
        f"serverstatus player count:{player_count}",
        f"serverstatus sn.player count:{player_count}",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Legacy compat (kept for direct imports)
# ─────────────────────────────────────────────────────────────────────

async def process_command(cmd: str) -> str:
    """Legacy simple command processor (kept for backward compat)."""
    result = await process_lms_command(cmd)
    return result if result else f"Unknown command: {cmd.split()[0]}"
