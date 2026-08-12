"""JSON-RPC API for Lyrion Music Server."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

try:
    import orjson

    def _json_loads(data: bytes | str) -> Any:
        return orjson.loads(data)

    def _json_dumps(obj: Any) -> bytes:
        return orjson.dumps(obj)

except ImportError:
    import json

    def _json_loads(data: bytes | str) -> Any:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return json.loads(data)

    def _json_dumps(obj: Any) -> bytes:
        return json.dumps(obj).encode("utf-8")


class JSONRPCError(Exception):
    """JSON-RPC error exception."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "data": self.data,
        }


class JSONRPCAPI:
    """JSON-RPC 2.0 API handler.

    Handles both single requests and batch requests. Methods are registered
    via register() and are called with positional parameters from the params
    array.
    """

    # Standard JSON-RPC error codes
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    def __init__(self) -> None:
        self._methods: dict[str, Callable] = {}
        # Short-TTL cache for status/serverstatus/players polls
        # (misbehaving clients can flood the server otherwise).
        self._status_cache: dict[tuple, tuple[float, Any]] = {}
        self._register_default_methods()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register_default_methods(self) -> None:
        """Register the built-in method set."""
        self.register("server.version", self._server_version)
        self.register("server.status", self._server_status)
        self.register("player.list", self._player_list)
        self.register("player.count", self._player_count)
        self.register("player.name", self._player_name)
        self.register("player.power", self._player_power)
        self.register("player.volume", self._player_volume)
        self.register("player.mode", self._player_mode)
        self.register("player.status", self._player_status)
        self.register("playlist.tracks", self._playlist_tracks)
        self.register("playlist.play", self._playlist_play)
        self.register("playlist.stop", self._playlist_stop)
        self.register("playlist.pause", self._playlist_pause)
        self.register("playlist.next", self._playlist_next)
        self.register("playlist.prev", self._playlist_prev)
        self.register("slim.request", self._slim_request)
        self.register("rescan", self._rescan)

    def register(self, name: str, method: Callable) -> None:
        """Register a method.

        Args:
            name: Fully-qualified method name (e.g. "player.power").
            method: Async callable accepting *params.
        """
        self._methods[name] = method

    def unregister(self, name: str) -> None:
        """Remove a registered method."""
        self._methods.pop(name, None)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def handle_request(self, request_data: bytes | str) -> bytes:
        """Parse and handle a JSON-RPC request.

        Args:
            request_data: Raw request body.

        Returns:
            JSON-RPC response as bytes.
        """
        try:
            request = _json_loads(request_data)

            # Batch request
            if isinstance(request, list):
                responses = [await self._handle_single(r) for r in request]
                # Filter out null results that some transports prefer
                responses = [r for r in responses if r is not None]
                return _json_dumps(responses) if responses else b"[]"

            response = await self._handle_single(request)
            return _json_dumps(response)

        except (ValueError, Exception) as e:
            return _json_dumps({
                "jsonrpc": "2.0",
                "error": {
                    "code": self.PARSE_ERROR,
                    "message": f"Parse error: {e}",
                },
                "id": None,
            })

    async def _handle_single(self, request: dict) -> Optional[dict]:
        """Handle one JSON-RPC request/notification.

        Compatible with real LMS: accepts requests with or without the
        "jsonrpc" version field ("2.0" or "1.0" both accepted) — SqueezeCtrl,
        Squeezer and similar apps omit it. Responses always include "2.0".
        """
        jsonrpc = request.get("jsonrpc")
        if jsonrpc not in (None, "2.0", "1.0"):
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.INVALID_REQUEST,
                    "message": "Invalid JSON-RPC version",
                },
                "id": request.get("id"),
            }

        method_name = request.get("method", "")
        params = request.get("params", [])
        id = request.get("id")

        # Notification (no id) — don't send response
        if id is None and method_name in self._methods:
            try:
                await self._methods[method_name](*params)
            except Exception:
                pass
            return None

        if method_name not in self._methods:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.METHOD_NOT_FOUND,
                    "message": f"Method not found: {method_name}",
                },
                "id": id,
            }

        try:
            result = await self._methods[method_name](*params)
            return {"jsonrpc": "2.0", "result": result, "id": id}
        except TypeError as e:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.INVALID_PARAMS,
                    "message": str(e),
                },
                "id": id,
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.INTERNAL_ERROR,
                    "message": str(e),
                },
                "id": id,
            }

    # ------------------------------------------------------------------
    # Built-in methods
    # ------------------------------------------------------------------

    async def _server_version(self) -> str:
        from lyrion.version import __version__
        return __version__

    async def _server_status(self) -> dict:
        from lyrion import __version__
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            players = pm.get_connected_count()
        except Exception:
            players = 0
        return {"version": __version__, "players": players}

    async def _player_list(self) -> list[dict]:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            return [p.to_dict() for p in pm.get_all_players()]
        except Exception:
            return []

    async def _player_count(self) -> int:
        try:
            from lyrion.player import PlayerManager
            return PlayerManager().get_connected_count()
        except Exception:
            return 0

    async def _player_name(self, mac: str) -> str:
        try:
            from lyrion.player import PlayerManager
            player = PlayerManager().get_player(mac)
            return player.name if player else ""
        except Exception:
            return ""

    async def _player_power(self, mac: str, power: Optional[bool] = None) -> bool:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            if power is None:
                player = pm.get_player(mac)
                return player.power if player else False
            pm.set_power(mac, power)
            return power
        except Exception:
            return False

    async def _player_volume(
        self, mac: str, level: Optional[int] = None
    ) -> int:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            if level is None:
                player = pm.get_player(mac)
                return player.volume if player else 0
            await pm.set_volume(mac, level)
            return level
        except Exception:
            return 0

    async def _player_mode(self, mac: str) -> str:
        try:
            from lyrion.player import PlayerManager
            player = PlayerManager().get_player(mac)
            return player.mode if player else "stop"
        except Exception:
            return "stop"

    async def _player_status(self, mac: str) -> dict:
        """Full playback status for a player (used by the web UI)."""
        try:
            from lyrion.player import PlayerManager
            player = PlayerManager().get_player(mac)
            if player is None:
                return {"mode": "stop", "playlist_cur_index": -1}
            mode_map = {"play": "playing", "pause": "paused",
                        "stop": "stopped", "loading": "loading"}
            return {
                "mode": mode_map.get(player.mode, player.mode),
                "player_connected": bool(player.connected),
                "power": bool(player.power),
                "volume": player.volume,
                "playlist_tracks": player.playlist_total,
                "playlist_cur_index": player.playlist_position,
                "current_track_id": player.current_track_id,
                "current_title": getattr(player, "current_title", None),
                "current_url": getattr(player, "current_url", None),
            }
        except Exception:
            return {"mode": "stop", "playlist_cur_index": -1}

    async def _playlist_tracks(self, mac: str) -> list[dict]:
        """Return the player's playlist as JSON objects (used by the web UI)."""
        try:
            from lyrion.player import PlayerManager
            player = PlayerManager().get_player(mac)
            if player is None:
                return []
            tracks = player.playlist
            out: list[dict] = []
            # Track titles from the DB (one query for all local tracks)
            track_ids = [e for e in tracks if not isinstance(e, str)]
            titles: dict[int, str] = {}
            if track_ids:
                try:
                    from sqlalchemy import select
                    from lyrion.database.schema import Track
                    from lyrion.database.sqlite_helper import db_session
                    async with db_session() as session:
                        result = await session.execute(
                            select(Track.id, Track.title).where(Track.id.in_(track_ids))
                        )
                        titles = {tid: t for tid, t in result.all()}
                except Exception:
                    titles = {}
            for i, entry in enumerate(tracks):
                if isinstance(entry, str):
                    out.append({
                        "index": i,
                        "url": entry,
                        "title": player.current_title
                        if i == player.playlist_position and player.current_title
                        else "Radio Stream",
                    })
                else:
                    out.append({
                        "index": i,
                        "track_id": entry,
                        "title": titles.get(entry, f"Track {entry}"),
                    })
            return out
        except Exception:
            return []

    async def _playlist_play(self, mac: str, index: int = 0) -> bool:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            # If the index points into a populated playlist, send a strm frame
            player = pm.get_player(mac)
            if player is not None and getattr(player, "playlist", []):
                await self._play_playlist_item(pm, player, index)
                return True
            # Fallback: direct track play
            return await pm.play_track(mac, index)
        except Exception:
            return False

    async def _playlist_stop(self, mac: str) -> bool:
        try:
            from lyrion.player import PlayerManager
            # Real SlimProto frame (strm 'q') — send_command with CLI text
            # does nothing on Squeezelite. stop_player also sets mode=stop.
            return await PlayerManager().stop_player(mac)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lyrion.web.api").warning("_playlist_stop failed: %s", e)
            return False

    async def _playlist_pause(self, mac: str, state: int = -1) -> bool:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            if state == 1:
                # pause on -> real strm 'p' frame
                return await pm.pause_player(mac, True)
            if state == 0:
                # resume -> strm 'p' 0 (Squeezelite resumes in place)
                return await pm.pause_player(mac, False)
            # toggle
            player = pm.get_player(mac)
            currently_paused = player is not None and player.mode == "pause"
            return await pm.pause_player(mac, not currently_paused)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lyrion.web.api").warning("_playlist_pause failed: %s", e)
            return False

    async def _playlist_next(self, mac: str) -> bool:
        try:
            from lyrion.player import PlayerManager
            return await PlayerManager().playlist_next(mac)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lyrion.web.api").warning("_playlist_next failed: %s", e)
            return False

    async def _playlist_prev(self, mac: str) -> bool:
        try:
            from lyrion.player import PlayerManager
            return await PlayerManager().playlist_prev(mac)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lyrion.web.api").warning("_playlist_prev failed: %s", e)
            return False

    async def _slim_request(self, player_id: str, command: list[str]) -> Any:
        """LMS-compatible slim.request — returns structured JSON dicts.

        SqueezeCtrl / Squeezer / SqueezeTray expect dict responses
        (players_loop, playlist_loop, mixer volume, etc.), not text lines.
        """
        if not command:
            return {}
        cmd = command[0]
        args = command[1:] if len(command) > 1 else []
        pid = player_id if player_id and player_id != "-" else None

        # Status/serverstatus polls from remote apps (a misbehaving
        # SqueezeTray can issue hundreds of identical requests per
        # second) — cache the response for 1s to keep the server
        # responsive for everyone else.
        cacheable = cmd in ("status", "serverstatus", "players")
        cache_key: tuple | None = None
        if cacheable:
            cache_key = (str(pid), str(cmd), json.dumps(args, sort_keys=True))
            cached = self._status_cache.get(cache_key)
            now = time.time()
            if cached and now - cached[0] < 1.0:
                return cached[1]
            self._cache_hit = False

        try:
            from lyrion.player.manager import PlayerManager
            pm = PlayerManager()
        except Exception:
            pm = None

        # ── players ────────────────────────────────────────────────
        if cmd == "players":
            players = pm.get_all_players() if pm else []
            start = int(args[0]) if args and str(args[0]).isdigit() else 0
            count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 100
            loop = [
                {
                    "playerindex": i,
                    "playerid": p.mac,
                    "name": getattr(p, "name", "") or p.mac,
                    "model": getattr(p, "model", "squeezebox") or "squeezebox",
                    "modelname": getattr(p, "model", "squeezebox") or "Squeezebox",
                    "ip": f"{p.ip}:{p.port}" if getattr(p, "port", 0) else p.ip,
                    "uuid": p.mac,
                    "firmware": getattr(p, "firmware", "2.0.0") or "1",
                    "isplaying": 1 if p.mode == "play" else 0,
                    "isplayer": 1 if getattr(p, "is_player", True) else 0,
                    "canpoweroff": 1 if getattr(p, "can_power_off", True) else 0,
                    "connected": 1 if p.connected else 0,
                    "power": 1 if p.power else 0,
                    "seq_no": 0,
                }
                for i, p in enumerate(players[start:start + count])
            ]
            # playerindex must be the GLOBAL index (LMS semantics), not
            # the position within the paginated slice.
            for i, entry in enumerate(loop):
                entry["playerindex"] = start + i
            result = {"count": len(players), "players_loop": loop}
            if cacheable:
                self._status_cache[cache_key] = (time.time(), result)
            return result

        # ── serverstatus ───────────────────────────────────────────
        if cmd == "serverstatus":
            from lyrion import __version__
            from lyrion.config import get_config
            players = pm.get_all_players() if pm else []
            try:
                http_port = int(get_config().get("serverport", 9000))
            except Exception:
                http_port = 9000
            result = {
                "version": __version__,
                "uuid": "lyrion-server-0001",
                "name": "Lyrion Music Server",
                "httpport": http_port,
                "player count": len(players),
                "info total genres": 0,
                "info total artists": 0,
                "info total albums": 0,
                "info total songs": 0,
            }
            # Jive controllers subscribe with
            # ['serverstatus', 0, 50, 'subscribe:60'] and expect the
            # player list in players_loop (like the real LMS). Also
            # return it without args (Squeezer queries plain serverstatus).
            result["count"] = len(players)
            result["players_loop"] = [
                {
                    "playerindex": i,
                    "playerid": p.mac,
                    "name": p.name,
                    "model": getattr(p, "model", "squeezebox"),
                    "modelname": getattr(p, "model", "squeezebox"),
                    "ip": f"{p.ip}:{p.port}" if p.port else p.ip,
                    "uuid": p.mac,
                    "firmware": getattr(p, "firmware", "2.0.0"),
                    "isplaying": 1 if p.mode == "play" else 0,
                    "isplayer": 1,
                    "canpoweroff": 1,
                    "connected": 1 if p.connected else 0,
                    "power": 1 if p.power else 0,
                    "seq_no": 0,
                }
                for i, p in enumerate(players)
            ]
            if cacheable:
                self._status_cache[cache_key] = (time.time(), result)
            return result

        # ── status (player) ────────────────────────────────────────
        if cmd == "status":
            result = await self._json_player_status(pm, pid, args)
            if cacheable:
                self._status_cache[cache_key] = (time.time(), result)
            return result

        # ── menu (home menu for Jive/Material/OpenSqueeze apps) ────
        # LMS 'menu <start> <count> [direct:1]' returns the root browse
        # items in item_loop. Apps hang on 'Loading Menus…' without it.
        if cmd == "menu":
            items = self._home_menu()
            start = int(args[0]) if args and str(args[0]).isdigit() else 0
            count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 512
            loop = items[start:start + count]
            for i, it in enumerate(loop):
                it["index"] = start + i
            return {
                "item_loop": loop,
                "count": len(items),
                "base": {"id": "", "name": "Home"},
                "title": "Home",
            }

        # ── menustatus (OpenSqueeze home menu) ─────────────────────
        if cmd == "menustatus":
            items = self._home_menu()
            return {
                "item_loop": items,
                "count": len(items),
                "base": {"id": "", "name": "Home"},
                "title": "Home",
            }

        # ── Control commands (return {} — LMS convention) ──────────
        # ── CLI query commands: <cmd> ? → {"_<cmd>": value} ──────
        # LMS JSON-RPC convention (ioBroker.squeezeboxrpc, Squeezer,
        # SqueezeClient): single-value queries are answered with the
        # command name prefixed by '_' as the result key.
        if args and len(args) == 1 and str(args[0]) == "?":
            player = pm.get_player(pid) if pid else None
            if player is not None:
                val: Any = ""
                if cmd == "mode":
                    val = player.mode
                elif cmd == "name":
                    val = player.name or ""
                elif cmd == "power":
                    val = 1 if player.power else 0
                elif cmd == "current_title":
                    val = getattr(player, "current_title", "") or ""
                elif cmd in ("current_url", "url"):
                    val = getattr(player, "current_url", "") or ""
                elif cmd == "playlist":
                    val = len(getattr(player, "playlist", []) or [])
                elif cmd == "version":
                    # Orange Squeeze probes the server with 'version ?'
                    # and requires {"_version": "..."} to connect at all.
                    from lyrion import __version__
                    val = __version__
                elif cmd in ("artist", "album"):
                    tid = getattr(player, "current_track_id", None)
                    if tid is not None:
                        info = await self._load_tracks([tid])
                        val = (info.get(tid, {}) or {}).get(cmd, "") or ""
                return {f"_{cmd}": val}
            # Player-independent queries (Orange Squeeze sends
            # ["", ["version", "?"]] to probe the server).
            if cmd == "version":
                from lyrion import __version__
                return {"_version": __version__}
            return {f"_{cmd}": ""}

        # mixer volume ? → {"_volume": N}
        if cmd == "mixer" and args and len(args) == 2 \
                and str(args[0]) == "volume" and str(args[1]) == "?":
            player = pm.get_player(pid) if pid else None
            return {"_volume": player.volume if player else 0}

        if cmd in ("pause", "power", "play", "stop", "mixer", "sync",
                   "unsync", "pref", "playerpref", "display", "button",
                   "alarm", "signalstrength", "client", "mode", "name",
                   "playlist"):
            # Invalidate the status cache: a poll right after a control
            # command must see the NEW state, not the stale cached one
            # (Squeezer otherwise shows 'playing' until the TTL expires).
            self._status_cache.clear()
            await self._json_control(pm, pid, cmd, args)
            return {}

        # ── favorites ──────────────────────────────────────────────
        # JSON-RPC clients (SqueezeTray/SqueezeCtrl/SPA) expect the LMS
        # loop_loop format: {"count": N, "loop_loop": [{id, name, url,
        # hasitems, ...}]}. items is DB-backed (FavoritesManager); the
        # other subcommands go through the CLI handler.
        if cmd == "favorites" and args and str(args[0]) == "items":
            try:
                from lyrion.music.favorites import get_favorites_manager
                rest = args[1:]
                parent = None
                # item_id:<n> (SqueezeTray folder children) — highest priority
                for a in rest:
                    if str(a).startswith("item_id:"):
                        try:
                            parent = int(str(a)[8:])
                        except ValueError:
                            parent = None
                        break
                if parent is None and len(rest) == 1 and str(rest[0]).isdigit():
                    # Web UI: ['favorites','items','<parent_id>'] — a bare
                    # number is the folder id (SqueezeTray sends multiple
                    # args: start/count/want_url — never a bare parent).
                    parent = int(str(rest[0]))
                items = await get_favorites_manager().list_items(parent)
                loop = []
                for it in items:
                    is_folder = it["type"] == "folder"
                    loop.append({
                        "id": str(it["id"]),
                        "name": it["title"],
                        "url": it["url"] or "",
                        "hasitems": 1 if is_folder else 0,
                        "isItem": 0 if is_folder else 1,
                        "isFolder": 1 if is_folder else 0,
                        "image": "",
                        "type": it["type"],
                        "parent_id": str(it["parent_id"]) if it["parent_id"] is not None else "",
                        "position": it["position"],
                    })
                return {"count": len(loop), "loop_loop": loop}
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}
        if cmd == "favorites":
            try:
                from lyrion.control.cli import CLIHandler, CLIContext
                async with CLIHandler() as cli:
                    ctx = CLIContext(player_id=pid or "-")
                    result = await cli.dispatch(ctx, (cmd, args))
                    return result if isinstance(result, list) else [str(result)]
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}

        # ── serverpref: get/set server preferences (e.g. library paths) ──
        #   ["serverpref", "musicdir"]            → {"musicdir": "/mnt/..."}
        #   ["serverpref", "musicdir", "/pfad"]   → set + return
        if cmd == "serverpref":
            try:
                from lyrion.config import PreferenceStore
                prefs = PreferenceStore.instance()
                if len(args) >= 2:
                    value = " ".join(str(a) for a in args[1:])
                    await prefs.set(args[0], value)
                    return {str(args[0]): value}
                if args:
                    return {str(args[0]): prefs.get(str(args[0])) or ""}
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}
            return {}

        # ── Browse commands (library) ──────────────────────────────
        if cmd in ("albums", "artists", "genres", "songs", "titles",
                   "musicfolder", "playlists", "radios", "songinfo",
                   "info", "contributors", "browse"):
            return await self._json_browse(cmd, args)

        # ── Fallback: text CLI passthrough ─────────────────────────
        try:
            from lyrion.control.cli import CLIHandler, CLIContext
            async with CLIHandler() as cli:
                ctx = CLIContext(player_id=player_id)
                result = await cli.dispatch(ctx, (cmd, args))
                return result if isinstance(result, list) else [str(result)]
        except Exception as e:
            return {"error": str(e)}

    async def _rescan(self, mode: str = "normal") -> Any:
        """Direct rescan — triggers MusicImporter in background."""
        import asyncio as _asyncio
        from pathlib import Path as _Path
        async def _do():
            from lyrion.config import get_config
            from lyrion.media.importer import MusicImporter, ImportConfig
            musicdir = get_config().get("musicdir", "/mnt/media2/Musik") or "/mnt/media2/Musik"
            importer = MusicImporter(ImportConfig(source_path=_Path(musicdir)))
            stats = await importer.import_music()
            return stats
        _asyncio.create_task(_do())
        return {"status": "rescan started", "mode": mode}

    # ─────────────────────────────────────────────────────────────
    # slim.request JSON helpers
    # ─────────────────────────────────────────────────────────────

    async def _json_player_status(self, pm, pid: str | None, args: list[str]) -> dict:
        """Build a player status dict (LMS 'status' command)."""
        from lyrion.player.manager import PlayerManager
        pm = pm or PlayerManager()
        if not pid:
            players = pm.get_all_players()
            pid = players[0].mac if players else None
        player = pm.get_player(pid) if pid else None
        if player is None:
            return {"mode": "stop", "power": 0, "player_name": "", "playlist_tracks": 0}

        # Track metadata from DB for playlist_loop (only int ids)
        loop = []
        playlist_ids = getattr(player, "playlist", []) or []
        int_ids = [i for i in playlist_ids if isinstance(i, int)]
        track_rows = await self._load_tracks(int_ids) if int_ids else {}

        for i, tid in enumerate(playlist_ids):
            if isinstance(tid, int):
                info = track_rows.get(tid, {})
                title = info.get("title", "Unknown")
                artist = info.get("artist", "")
                album = info.get("album", "")
                duration = info.get("duration", 0) or 0
                url = info.get("url", "")
            else:
                # Remote stream URL (radio) — title from the URL host
                title = str(tid)
                artist = ""
                album = ""
                duration = 0
                url = str(tid)
                try:
                    from urllib.parse import urlparse
                    host = urlparse(url).hostname or ""
                    if host:
                        title = host.replace("www.", "")
                except Exception:
                    pass
            loop.append({
                "id": tid,
                "index": i,
                "title": title,
                "artist": artist,
                "album": album,
                "duration": duration,
                "url": url,
            })

        cur = player.playlist_position or 0
        if cur < len(playlist_ids) and isinstance(playlist_ids[cur], int):
            cur_info = track_rows.get(playlist_ids[cur], {})
        else:
            cur_info = {}
        if cur < len(playlist_ids) and not isinstance(playlist_ids[cur], int):
            # Radio stream: title = station name (current_title if set,
            # else host) — never the full URL.
            url_str = str(playlist_ids[cur])
            try:
                from urllib.parse import urlparse
                host = urlparse(url_str).hostname or url_str
                title = host.replace("www.", "")
            except Exception:
                title = url_str
            cur_info = {"title": title, "url": url_str}
        if getattr(player, "current_title", ""):
            cur_info["title"] = player.current_title

        elapsed = getattr(player, "elapsed", 0) or 0
        if player.mode != "play":
            elapsed = 0

        # remoteMeta: SqueezeClient / ioBroker expect the live-stream
        # metadata block for remote streams (radio).
        remote_meta = {}
        cur_url = getattr(player, "current_url", None)
        if cur_url and not isinstance(playlist_ids[cur] if cur < len(playlist_ids) else None, int):
            remote_meta = {
                "title": cur_info.get("title", ""),
                "artist": cur_info.get("artist", ""),
                "album": cur_info.get("album", ""),
                "duration": cur_info.get("duration", 0) or 0,
                "url": cur_url,
            }

        menu_block = None
        if "menu:menu" in (args or []):
            items = self._home_menu()
            menu_block = {
                "item_loop": items,
                "count": len(items),
                "base": {"id": "", "name": "Home"},
                "title": "Home",
            }
        return {
            "mode": player.mode,
            "power": 1 if player.power else 0,
            "player_name": player.name or player.mac,
            "mixer volume": player.volume or 50,
            "playlist_tracks": len(playlist_ids),
            "playlist_cur_index": cur,
            "time": elapsed,
            "rate": 1 if player.mode == "play" else 0,
            "duration": cur_info.get("duration", 0) or 0,
            "artist": cur_info.get("artist", ""),
            "title": cur_info.get("title", ""),
            "album": cur_info.get("album", ""),
            "playlist_loop": loop,
        } | ({"remoteMeta": remote_meta} if remote_meta else {}) \
          | ({"menu": menu_block} if menu_block else {})

    def _home_menu(self) -> list[dict]:
        """The root browse menu (Home) shared by menu/menustatus/status."""

        def _home_item(browse_id: str, name: str, typ: str) -> dict:
            return {
                "id": f"browse://{browse_id}",
                "name": name,
                "text": name,  # OpenSqueeze shows getText()
                "type": typ,
                "hasitems": 1,
                "browse": {"id": browse_id, "name": name, "type": typ},
                "image": f"html/images/{browse_id}.png",
            }

        return [
            _home_item("artists", "Artists", "artist"),
            _home_item("albums", "Albums", "album"),
            _home_item("songs", "Songs", "song"),
            _home_item("genres", "Genres", "genre"),
            _home_item("favorites", "Favorites", "link"),
            _home_item("radios", "Radio", "link"),
        ]

    async def _load_tracks(self, track_ids: list[int]) -> dict:
        """Load track metadata (title/artist/album/duration/url) for ids."""
        result: dict = {}
        if not track_ids:
            return result
        try:
            import sqlite3
            # Read-only connection: status polls from many clients must
            # never block on (or lock) the writer (aiosqlite session).
            db = sqlite3.connect(
                "file:/root/.lyrion/Lyrion/Prefs/lyrion.db?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(track_ids))
            rows = db.execute(
                f"SELECT id, title, url, duration FROM tracks WHERE id IN ({placeholders})",
                track_ids,
            ).fetchall()
            # Artist/album via join tables
            for row in rows:
                tid = row["id"]
                artist = ""
                album = ""
                try:
                    a = db.execute(
                        "SELECT c.name FROM contributors c JOIN tracks_contributors tc ON tc.contributor_id = c.id "
                        "WHERE tc.track_id = ? AND tc.role = 'artist' LIMIT 1", (tid,)
                    ).fetchone()
                    if a: artist = a["name"]
                except Exception:
                    pass
                try:
                    al = db.execute(
                        "SELECT a.name FROM albums a JOIN tracks_albums ta ON ta.album_id = a.id "
                        "WHERE ta.track_id = ? LIMIT 1", (tid,)
                    ).fetchone()
                    if al: album = al["name"]
                except Exception:
                    pass
                result[tid] = {
                    "title": row["title"] or "",
                    "url": row["url"] or "",
                    "duration": row["duration"] or 0,
                    "artist": artist,
                    "album": album,
                }
            db.close()
        except Exception:
            pass
        return result

    async def _json_control(self, pm, pid: str | None, cmd: str, args: list[str]) -> None:
        """Execute control commands (pause/power/play/stop/mixer/playlist)."""
        from lyrion.player.manager import PlayerManager
        pm = pm or PlayerManager()
        if not pid:
            players = pm.get_all_players()
            pid = players[0].mac if players else None
        if not pid:
            return

        def send(cmd_str: str) -> None:
            try:
                pm.send_command(pid, cmd_str)
            except Exception:
                pass

        if cmd == "power":
            val = str(args[0]) if args else "1"
            player = pm.get_player(pid)
            if player is not None:
                player.power = val in ("1", "on", "toggle", "")
                pm.set_power(pid, player.power)
            else:
                send(f"power {val}")
        elif cmd == "pause":
            val = str(args[0]) if args else "0"
            player = pm.get_player(pid)
            if player is not None:
                if val == "1":
                    # Real frame to the player (strm 'p'), not just state
                    await pm.pause_player(pid, True)
                elif val == "0":
                    # Resume: power on + (re)send strm for the current track
                    player.power = True
                    player.mode = "play"
                    pm.set_mode(pid, "play")
                    await self._play_playlist_item(pm, player, player.playlist_position or 0)
            else:
                send(f"pause {val}")
        elif cmd == "play":
            player = pm.get_player(pid)
            if player is not None:
                player.power = True
                player.mode = "play"
                pm.set_mode(pid, "play")
                await self._play_playlist_item(pm, player, player.playlist_position or 0)
            else:
                send("play")
        elif cmd == "stop":
            player = pm.get_player(pid)
            if player is not None:
                player.mode = "stop"
                pm.set_mode(pid, "stop")
                # Real frame to the player (strm 'q') — state alone does
                # not stop Squeezelite.
                await pm.stop_player(pid)
            else:
                send("stop")
        elif cmd == "mixer":
            val = str(args[-1]) if args else ""
            if val.isdigit():
                player = pm.get_player(pid)
                if player is not None:
                    player.volume = int(val)
                    # audg frame — text CLI does not exist on the
                    # SlimProto channel.
                    await pm.set_volume(pid, int(val))
                else:
                    send(f"mixer volume {val}")
        elif cmd == "playlist":
            sub = args[0] if args else ""
            rest = args[1:] if len(args) > 1 else []
            if sub == "add" and rest:
                # Accept both a DB track id and a plain URL. SqueezeTray adds
                # URLs (radio/favorites); the SPA adds track ids.
                item = rest[0]
                player = pm.get_player(pid)
                if player is not None:
                    if str(item).isdigit():
                        player.playlist.append(int(item))
                    else:
                        player.playlist.append(item)
                    player.playlist_total = len(player.playlist)
            elif sub == "index" and rest:
                idx = rest[0]
                player = pm.get_player(pid)
                if player is not None and str(idx).isdigit():
                    player.playlist_position = int(idx)
                    await self._play_playlist_item(pm, player, int(idx))
            elif sub == "play":
                # LMS-compatible 'playlist play [<index>]' (CLI/JSON path)
                player = pm.get_player(pid)
                if player is not None:
                    if rest and str(rest[0]).isdigit():
                        idx = int(rest[0])
                    else:
                        idx = player.playlist_position or 0
                    player.playlist_position = idx
                    await self._play_playlist_item(pm, player, idx)
            elif sub == "stop":
                player = pm.get_player(pid)
                if player is not None:
                    await pm.stop_player(pid)
                else:
                    send("stop")
            elif sub == "clear":
                player = pm.get_player(pid)
                if player is not None:
                    player.playlist = []
                    player.playlist_total = 0
        else:
            send(f"{cmd} {' '.join(args)}")

    async def _play_playlist_item(self, pm, player, idx: int) -> None:
        """Send a strm frame for playlist item idx (track id or stream URL)."""
        try:
            items = getattr(player, "playlist", []) or []
            if idx < 0 or idx >= len(items):
                return
            item = items[idx]
            handler = getattr(pm, "_protocol_handler", None)
            if handler is None:
                return
            # Playing implies power-on (like real LMS)
            if not player.power:
                player.power = True
            if isinstance(item, int):
                await handler.send_strm_to_player(player.mac, item)
            else:
                await handler.send_remote_stream(player.mac, str(item))
            player.mode = "play"
            player.playlist_position = idx
            if isinstance(item, int):
                player.current_track_id = item
            pm.set_mode(player.mac, "play")
            logger = __import__("logging").getLogger("lyrion.web.api")
            logger.info("Playing playlist item %d (%r) on %s", idx, item, player.mac)
        except Exception as exc:
            logger = __import__("logging").getLogger("lyrion.web.api")
            logger.warning("_play_playlist_item failed: %s", exc)

    async def _json_browse(self, cmd: str, args: list[str]) -> dict:
        """Browse library tables (albums/artists/songs/genres) as JSON."""
        start = int(args[0]) if args and str(args[0]).isdigit() else 0
        count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 50
        try:
            import sqlite3
            db = sqlite3.connect(
                "file:/root/.lyrion/Lyrion/Prefs/lyrion.db?mode=ro", uri=True)
            db.row_factory = sqlite3.Row

            if cmd == "artists":
                rows = db.execute(
                    "SELECT id, name FROM contributors WHERE role = 'artist' "
                    "ORDER BY name LIMIT ? OFFSET ?", (count, start)).fetchall()
                loop = [{"id": r["id"], "artist": r["name"] or ""} for r in rows]
                total = db.execute(
                    "SELECT COUNT(*) FROM contributors WHERE role = 'artist'").fetchone()[0]
            elif cmd == "albums":
                rows = db.execute(
                    "SELECT id, name, year FROM albums ORDER BY name LIMIT ? OFFSET ?",
                    (count, start)).fetchall()
                loop = [{"id": r["id"], "album": r["name"] or "", "year": r["year"] or 0} for r in rows]
                total = db.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
            elif cmd == "songs" or cmd == "titles":
                rows = db.execute(
                    "SELECT id, title, url, duration FROM tracks ORDER BY title LIMIT ? OFFSET ?",
                    (count, start)).fetchall()
                loop = [{"id": r["id"], "title": r["title"] or "", "url": r["url"] or "",
                         "duration": r["duration"] or 0} for r in rows]
                total = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            elif cmd == "genres":
                rows = db.execute(
                    "SELECT id, name FROM genres ORDER BY name LIMIT ? OFFSET ?",
                    (count, start)).fetchall()
                loop = [{"id": r["id"], "genre": r["name"] or ""} for r in rows]
                total = db.execute("SELECT COUNT(*) FROM genres").fetchone()[0]
            else:
                db.close()
                return {"count": 0, "loop_loop": []}
            db.close()
            return {"count": total, "loop_loop": loop}
        except Exception:
            return {"count": 0, "loop_loop": []}


class WebAPIHandler:
    """HTTP request handler that routes to JSON-RPC or the web UI.

    Routes:
      POST /jsonrpc.js  → JSONRPCAPI.handle_request()
      GET  /api/v1/*    → REST passthrough (subclass for details)
      GET  /            → Serve index.html from html/
    """

    def __init__(self, jsonrpc: Optional[JSONRPCAPI] = None) -> None:
        self.jsonrpc = jsonrpc or JSONRPCAPI()
        self._static_dir = None

    def set_static_dir(self, path: str) -> None:
        """Set the directory for static file serving."""
        from pathlib import Path
        self._static_dir = Path(path)

    async def handle(self, method: str, path: str, body: bytes) -> tuple[int, dict, bytes]:
        """Handle an incoming HTTP request.

        Returns:
            (status_code, headers_dict, body_bytes)
        """
        from pathlib import Path

        if path == "/jsonrpc.js" or path == "/api/jsonrpc":
            return await self._handle_jsonrpc(method, body)

        if path.startswith("/api/v1/"):
            return await self._handle_rest(method, path, body)

        if path == "/" or path.startswith("/html/") or path.startswith("/material"):
            return self._serve_static(path)

        # Fallback: 404
        return (
            404,
            {"Content-Type": "application/json"},
            b'{"error": "Not found"}',
        )

    async def _handle_jsonrpc(self, method: str, body: bytes) -> tuple[int, dict, bytes]:
        if method != "POST":
            return 405, {"Content-Type": "application/json"}, b'{"error": "Method not allowed"}'
        response = await self.jsonrpc.handle_request(body)
        return 200, {"Content-Type": "application/json"}, response

    async def _handle_rest(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, dict, bytes]:
        route = path[8:]  # strip "/api/v1/"
        if route == "status":
            result = await self.jsonrpc._server_status()
            return 200, {"Content-Type": "application/json"}, _json_dumps(result)
        if route == "players":
            result = await self.jsonrpc._player_list()
            return 200, {"Content-Type": "application/json"}, _json_dumps(result)
        return 404, {"Content-Type": "application/json"}, b'{"error": "Not found"}'

    def _serve_static(self, path: str) -> tuple[int, dict, bytes]:
        if self._static_dir is None:
            return 404, {}, b"Static dir not configured"

        from pathlib import Path

        if path == "/":
            path = "/index.html"
        elif path == "/material" or path == "/material/":
            # Material Skin (Jive controller UI) SPA entry
            path = "/material/index.html"

        file_path = self._static_dir / path.lstrip("/")
        # Security: prevent directory traversal
        if not str(file_path).startswith(str(self._static_dir.resolve())):
            return 403, {}, b"Forbidden"

        if not file_path.is_file():
            return 404, {}, b"Not found"

        import mimetypes
        mime, _ = mimetypes.guess_type(str(file_path))
        try:
            content = file_path.read_bytes()
            headers = {"Content-Type": mime or "application/octet-stream"}
            # HTML without cache so UI updates are picked up immediately
            if mime == "text/html":
                headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return 200, headers, content
        except Exception as e:
            return 500, {}, str(e).encode()
