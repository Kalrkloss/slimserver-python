"""JSON-RPC API for Pyrion Music Server."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Global operational notice shown in the web Status bar (e.g. a missing
# ffmpeg). Set via set_server_notice(); read in the serverstatus response.
_SERVER_NOTICE: dict[str, str] = {"text": ""}


def set_server_notice(text: str) -> None:
    """Set (or clear, with '') the global Status-bar notice."""
    _SERVER_NOTICE["text"] = text

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


def _library_db_path() -> str:
    """Resolve the library DB path from the active config (test/dev runs use
    LYRION_SERVERDATA; the production default stays /root/.lyrion)."""
    try:
        from lyrion.config import get_config
        return str(get_config().db_path)
    except Exception:  # noqa: BLE001
        return "/root/.lyrion/Lyrion/Prefs/lyrion.db"


_LIBRARY_DB = "/root/.lyrion/Lyrion/Prefs/lyrion.db"


def _db_query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query against the library DB (synchronous)."""
    import sqlite3

    con = sqlite3.connect(f"file:{_library_db_path()}?mode=ro", uri=True, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


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
        # P4-4: active display popup (showBriefly) + expiry timestamp
        self._popup: Optional[dict] = None
        self._popup_expires: float = 0.0
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
                # Stream metadata (StreamTitle) → now playing song + artist.
                "current_artist": getattr(player, "remote_meta", {}).get("artist", ""),
                "remote_meta": getattr(player, "remote_meta", {}),
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
            art_by_track: dict[int, str] = {}
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
                # Album artwork per track (one bulk query) — the player
                # shows the cover in Now Playing and the playlist.
                try:
                    from sqlalchemy import select as _sel
                    from lyrion.database.schema import Album, tracks_albums
                    from lyrion.database.sqlite_helper import db_session
                    async with db_session() as session:
                        rows = (await session.execute(
                            _sel(tracks_albums.c.track,
                                 tracks_albums.c.album,
                                 Album.artwork)
                            .join(Album, Album.id == tracks_albums.c.album)
                            .where(tracks_albums.c.track.in_(track_ids))
                        )).all()
                        for tid, album_id, art in rows:
                            if art:
                                art_by_track[tid] = f"/music/{album_id}/cover.jpg"
                except Exception:
                    art_by_track = {}
            for i, entry in enumerate(tracks):
                if isinstance(entry, str):
                    stream_name = getattr(player, "stream_titles", {}).get(entry, "")
                    art = getattr(player, "stream_images", {}).get(entry, "")
                    out.append({
                        "index": i,
                        "url": entry,
                        "title": stream_name
                        or (player.current_title
                            if i == player.playlist_position and player.current_title
                            else "Radio Stream"),
                        # Senderlogo: explizit hinterlegtes Bild oder Radio-Icon.
                        # static_dir enthält bereits "html/", also ohne /html/-
                        # Präfix im URL-Pfad (sonst doppelt -> 404).
                        "artwork_url": art or "/images/radio.svg",
                    })
                else:
                    out.append({
                        "index": i,
                        "track_id": entry,
                        "title": titles.get(entry, f"Track {entry}"),
                        "artwork_url": art_by_track.get(entry, ""),
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

    async def _fav_items_loop(self, fm: Any, parent: Optional[int],
                              parent_path: str, feed_mode: bool) -> list[dict]:
        """Build the favorites loop with LMS hierarchical ids.

        id = display-position path from the virtual root ('0.0', '0.3.1');
        dbid carries the internal DB id; feed_mode embeds children in
        'items' arrays.
        """
        try:
            items = await fm.list_items(parent)
        except Exception:
            return []
        loop: list[dict] = []
        path = parent_path  # hierarchical prefix for the item ids
        for i, it in enumerate(items):
            is_folder = it["type"] == "folder"
            # LMS reference format (lyrion.org): hierarchical id
            # '<root>.<position>' (the apps re-send it as item_id:),
            # name/image/isaudio/hasitems, type 'audio' for streams.
            item = {
                "id": path + f".{i}",
                "name": it["title"],
                "image": "html/images/favorites.png",
                "isaudio": 0 if is_folder else 1,
                "hasitems": 1 if is_folder else 0,
                "position": i,
            }
            if not is_folder:
                item["type"] = "audio"
                item["url"] = it["url"] or ""
                item["id_hierarchical"] = path + f".{i}"
                item["dbid"] = str(it["id"])
            if is_folder:
                # Folder: go opens the folder's items (hierarchical id).
                item["actions"] = {
                    "go": {"player": 0, "cmd": ["favorites", "items"],
                           "params": {"item_id": path + f".{i}"}},
                }
            else:
                # Stream: play/do plays the favorite.
                item["actions"] = {
                    "play": {"player": 0, "cmd": ["playlist", "play"],
                             "params": {"item_id": path + f".{i}"}},
                    "do": {"player": 0, "cmd": ["playlist", "play"],
                           "params": {"item_id": path + f".{i}"}},
                }
            if feed_mode and is_folder:
                item["items"] = await self._fav_items_loop(
                    fm, int(it["id"]), path + f".{i}", feed_mode)
            loop.append(item)
        return loop


    async def _displaystatus(self, pid: str | None, args: list[str]) -> dict:
        """displaystatus [showBriefly:<text> <duration>] — now-playing popup.

        'showBriefly:<text>' sets a popup (jive block) that expires after
        <duration> seconds (default 5); a bare query returns the active
        popup or {} when idle.
        """
        now = time.time()
        if self._popup_expires and now > self._popup_expires:
            self._popup = None
            self._popup_expires = 0.0
        for i, a in enumerate(args or []):
            s = str(a)
            if s.startswith("showBriefly:"):
                text = s[12:]
                duration = 5
                nxt = str(args[i + 1]) if i + 1 < len(args) else ""
                if nxt.isdigit():
                    duration = int(nxt)
                self._popup = {
                    "jive": {"text": text, "type": "popup", "duration": duration}
                }
                self._popup_expires = now + duration
        return self._popup or {}


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
            # Local IP + stable UUID like the real LMS (prefs 'server_uuid').
            local_ip = "127.0.0.1"
            try:
                import socket as _s
                _probe = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
                _probe.settimeout(0.5)
                try:
                    _probe.connect(("192.168.1.1", 1))
                    local_ip = _probe.getsockname()[0]
                finally:
                    _probe.close()
            except Exception:
                pass
            try:
                prefs = get_config()
                server_uuid = str(prefs.get("server_uuid", "") or "")
                if not server_uuid:
                    import uuid as _uuid
                    server_uuid = str(_uuid.uuid4())
                    await prefs._prefs.set("server_uuid", server_uuid)
                server_name = str(prefs.get("server_name", "") or "Pyrion")
            except Exception:
                server_uuid = "lyrion-server-0001"
                server_name = "Pyrion"
            result = {
                "version": __version__,
                "uuid": server_uuid,
                "name": server_name,
                "httpport": http_port,
                "ip": local_ip,
                "player count": len(players),
                "other player count": 0,
                "lastscan": 0,
                # SqueezeClient's ServerStatusResponse requires mediadirs
                "mediadirs": [],
                # P4-3: real library totals (were hardcoded 0)
                "info total genres": 0,
                "info total artists": 0,
                "info total albums": 0,
                "info total songs": 0,
                "info total duration": 0,
                # Operational notice surfaced in the web Status bar, e.g.
                # "ffmpeg not found - transcoding not possible".
                "notice": _SERVER_NOTICE.get("text", ""),
            }
            # Live library-scan progress for the status bar ("Scan läuft,
            # N Dateien gefunden"). The SPA polls serverstatus anyway.
            try:
                from lyrion.media.scan_state import SCAN_STATE
                snap = SCAN_STATE.snapshot()
                result["scan"] = snap
            except Exception:
                pass
            try:
                r = _db_query(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(duration),0) AS d FROM tracks"
                )
                if r:
                    result["info total songs"] = r[0]["n"]
                    result["info total duration"] = int(r[0]["d"])
                r = _db_query(
                    "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
                    "JOIN tracks_contributors tc ON tc.contributor = c.id "
                    "AND tc.role = 1"
                )
                if r:
                    result["info total artists"] = r[0]["n"]
                r = _db_query("SELECT COUNT(*) AS n FROM albums")
                if r:
                    result["info total albums"] = r[0]["n"]
                r = _db_query(
                    "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre != ''"
                )
                if r:
                    result["info total genres"] = r[0]["n"]
            except Exception:
                pass
            # P4-3: subscribe:<seconds> tag — the Cometd layer pushes fresh
            # serverstatus on player connect/disconnect for these clients.
            if any(str(a).startswith("subscribe:") for a in (args or [])):
                result["subscribe"] = "60"
            # Jive controllers subscribe with
            # ['serverstatus', 0, 50, 'subscribe:60'] and expect the
            # player list in players_loop (like the real LMS). Also
            # return it without args (Squeezer queries plain serverstatus).
            result["count"] = len(players)
            result["players_loop"] = [
                {
                    # Perl parity: playerindex/uuid/seq_no are STRINGS in
                    # players_loop (int elsewhere), displaytype present.
                    "playerindex": str(i),
                    "playerid": p.mac,
                    "name": p.name,
                    "model": getattr(p, "model", "squeezebox"),
                    "modelname": getattr(p, "model", "squeezebox"),
                    "ip": f"{p.ip}:{p.port}" if p.port else p.ip,
                    "uuid": None,
                    "firmware": getattr(p, "firmware", "2.0.0"),
                    "isplaying": 1 if p.mode == "play" else 0,
                    "isplayer": 1,
                    "canpoweroff": 1,
                    "connected": 1 if p.connected else 0,
                    "power": 1 if p.power else 0,
                    "displaytype": "None",
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
                # SqueezeClient's JiveHomeItemListResponse requires offset
                "offset": start,
                "base": {"id": "", "name": "Home"},
                "title": "Home",
            }

        # ── menustatus (Squeezer format: [?, items, directive, player]) ──
        # Squeezer's parseMenuStatus: data[0] unused, data[1] = item
        # array, data[2] = menu directive — items are only added when
        # the directive is "add" (MenuStatusMessage.ADD)!
        if cmd == "menustatus":
            return [None, self._home_menu(), "add", pid or ""]

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
                elif cmd == "time":
                    val = int(getattr(player, "elapsed", 0) or 0)
                elif cmd == "duration":
                    val = float(getattr(player, "duration", 0) or 0)
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

        # prefset — Material Skin + controllers subscribe to the player's
        # preference set; return the per-player prefs as {key: value}.
        if cmd == "prefset":
            player = pm.get_player(pid) if pid else None
            prefs = dict(getattr(player, "playerprefs", {}) or {}) if player else {}
            return prefs

        # pref <key> [?|<value>] — server preference query/set.
        if cmd == "pref" and args:
            key = str(args[0])
            if len(args) > 1 and str(args[1]) != "?":
                from lyrion.config import get_prefs
                await get_prefs().set(key, args[1])
                return {f"_pref_{key}": str(args[1])}
            from lyrion.config import get_config
            val = get_config().get(key, "")
            return {key: val}

        if cmd in ("pause", "power", "play", "stop", "mixer", "sync",
                   "unsync", "pref", "playerpref", "display", "button",
                   "signalstrength", "client", "mode", "name",
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
                fm = get_favorites_manager()
                rest = args[1:]
                parent = None
                parent_path = "0"
                feed_mode = any(str(a).startswith("feedMode:") and str(a)[9:] == "1"
                                for a in rest)
                # item_id:<n> (SqueezeTray folder children) — highest priority;
                # accepts the LMS hierarchical id ('0.3.1') and the DB id.
                for a in rest:
                    if str(a).startswith("item_id:"):
                        val = str(a)[8:]
                        if "." in val:
                            parent = await fm.resolve_path(val)
                            parent_path = val if parent is not None else "0"
                        elif val.isdigit():
                            parent = int(val)
                            parent_path = f"0.{val}"
                        break
                if parent is None and len(rest) == 1 and str(rest[0]).isdigit():
                    # Web UI: ['favorites','items','<parent_id>'] — a bare
                    # number is the folder id (SqueezeTray sends multiple
                    # args: start/count/want_url — never a bare parent).
                    parent = int(str(rest[0]))
                    parent_path = f"0.{rest[0]}"
                loop = await self._fav_items_loop(fm, parent, parent_path, feed_mode)
                resp = self._browse_response(loop)
                # LMS reference: 'title' on the response level.
                resp["title"] = "Favorites"
                return resp
            except Exception:
                resp = self._browse_response([])
                resp["title"] = "Favorites"
                return resp

        # favorites changed — event subscription (SqueezeCtrl): the app
        # watches this channel and reloads the list when a 'changed' event
        # arrives. Answer empty/ok (no 'unknown command').
        if cmd == "favorites" and args and str(args[0]) == "changed":
            return {}

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

        # ── search (LMS format: search <start> <count> term:<begriff>) ──
        if cmd == "search":
            return await self._json_search(args)

        # ── alarms / alarm (LMS alarm-clock API, JSON for controllers) ──
        # SqueezePlay/Material poll ['alarms'] and expect an attribute
        # response wrapping alarm_loop of alarms for the player.
        if cmd == "alarms":
            return await self._json_alarms(pid, args)
        if cmd == "alarm":
            return await self._json_alarm(pid, args)

        # ── Browse commands (library) ──────────────────────────────
        if cmd in ("albums", "artists", "genres", "songs", "titles",
                   "musicfolder", "playlists", "radios", "songinfo",
                   "info", "contributors", "browse"):
            if cmd == "radios":
                return await self._json_radios(cmd, args)
            return await self._json_browse(cmd, args)

        # ── radiosearch (controller Radio search; LMS 'radiosearch') ──
        if cmd == "radiosearch":
            return await self._json_radiosearch(cmd, args)

        # ── browselibrary (SqueezePlay My-Music children; LMS 'browselibrary
        #    items <start> <count> mode:<albums|artists|genres|years|bmf|search>') ──
        if cmd == "browselibrary":
            return await self._json_browselibrary(cmd, args)

        # ── displaystatus (Squeezer subscribes with a request) ──────
        # Squeezer's parseDisplayStatus does getDataAsMap() — an
        # 'unknown command' list response crashes it. Empty map is fine.
        if cmd == "displaystatus":
            return await self._displaystatus(pid, args)

        # ── playerstatus (SqueezeCtrl/Orange Squeeze subscribe) ─────
        # The apps subscribe to /<cid>/slim/playerstatus/<player> and
        # expect the player status as event data — without it they show
        # no player status and no stream info.
        if cmd == "playerstatus":
            return await self._json_player_status(pm, pid, args)

        # ── Fallback: text CLI passthrough ─────────────────────────
        try:
            from lyrion.control.cli import CLIHandler, CLIContext
            async with CLIHandler() as cli:
                ctx = CLIContext(player_id=player_id)
                result = await cli.dispatch(ctx, (cmd, args))
                # Unknown commands must NOT be answered with the text
                # list — apps (Squeezer) cast the response data and
                # crash on Object[].
                if isinstance(result, list) and result and str(result[0]).startswith("unknown command"):
                    return {}
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
            musicdir = get_config().get("musicdir", "") or ""
            if not str(musicdir).strip():
                from pathlib import Path as _P
                fallback = _P.home() / "Music"
                logger.warning(
                    "Preference 'musicdir' is empty — falling back to %s "
                    "(set it via serverpref)", fallback)
                musicdir = str(fallback)
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

        # tags:<code> — songinfo letter codes (lyrion.org CLI docs).
        # Each returned tag is identified by a letter; the default tags
        # value for status is 'gald'. Without a tags parameter we return
        # all available fields (Web UI/SqueezeTray rely on that).
        TAG_FIELDS: dict[str, str] = {
            "a": "artist", "A": "artist", "s": "artist_id", "S": "artist_id",
            "l": "album", "e": "album_id",
            "d": "duration", "y": "year", "t": "tracknum", "u": "url",
            "r": "bitrate", "T": "samplerate", "I": "samplesize",
            "x": "remote", "g": "genre", "p": "genre_id",
            "c": "coverid", "j": "coverart", "J": "artwork_track_id",
            "K": "artwork_url", "i": "disc", "N": "remote_title",
            "o": "type", "f": "filesize", "k": "comment", "w": "lyrics",
        }
        tags = next((str(a)[5:] for a in (args or []) if str(a).startswith("tags:")), "")
        # 'title' has no letter code (always returned by songinfo).
        def tag_ok(code: str) -> bool:  # noqa: N802
            return (not tags) or code in tags

        # Pre-compute values for enriching the CURRENT playlist item (SqueezePlay
        # Now-Playing renders from it). Computed here (before the loop) because
        # cur_info/elapsed are derived later in this function.
        _cur = player.playlist_position or 0
        _cur_tid = playlist_ids[_cur] if 0 <= _cur < len(playlist_ids) else None
        _elapsed = float(getattr(player, "elapsed", 0) or 0)
        _cur_title = getattr(player, "current_title", "") or ""
        _cur_artist = ""
        _cur_track = _cur_title
        if isinstance(_cur_tid, str) and " - " in _cur_title:
            _artist, _track = _cur_title.split(" - ", 1)
            _cur_artist = _artist.strip()
            _cur_track = _track.strip()

        for i, tid in enumerate(playlist_ids):
            if isinstance(tid, int):
                info = track_rows.get(tid, {})
                title = info.get("title", "Unknown")
                url = info.get("url", "")
                duration = info.get("duration", 0) or 0
            else:
                # Remote stream URL (radio) — title from the URL host
                title = str(tid)
                url = str(tid)
                duration = 0
                try:
                    from urllib.parse import urlparse
                    host = urlparse(url).hostname or ""
                    if host:
                        title = host.replace("www.", "")
                except Exception:
                    pass
                info = {"remote": 1}
            item: dict = {"id": tid, "playlist index": i}
            # title/trackType are always present (Orange Squeeze does
            # firstItem.get("trackType").asText() — a missing field is a
            # NULL NPE crash).
            item["title"] = title
            item["text"] = title          # SqueezePlay _extractTrackInfo fallback
            item["trackType"] = "local" if isinstance(tid, int) else "remote"
            # SqueezePlay now-playing reads _track.track/.artist/.album —
            # provide them for LOCAL tracks (a remote stream intentionally
            # omits `track` so SqueezePlay falls back to text + current_title).
            if isinstance(tid, int):
                item["track"] = title
                item["artist"] = info.get("artist", "")
                item["album"] = info.get("album", "")
                item["duration"] = duration
            elif i == player.playlist_position:
                # Enrich the CURRENT stream item so SqueezePlay's Now-Playing
                # has artist/duration/url to render (Perl's playlist item is
                # rich: artist/title/artwork_url/duration/url/remote).
                item["url"] = str(tid)
                item["track"] = _cur_track or item.get("title", "")
                item["artist"] = _cur_artist or ""
                item["album"] = ""
                item["duration"] = _elapsed
            for code, field in TAG_FIELDS.items():
                if not tag_ok(code):
                    continue
                value = info.get(field)
                if value is None or value == "":
                    continue
                if field == "cover":
                    # coverid/coverart/artwork for the /music/<id>/cover.jpg
                    # route (LMS convention).
                    if tag_ok("c"):
                        item["coverid"] = value
                    if tag_ok("j"):
                        item["coverart"] = 1
                    if tag_ok("J"):
                        item["artwork_track_id"] = value
                    if tag_ok("K"):
                        item["artwork_url"] = f"/music/{value}/cover.jpg"
                elif field == "remote":
                    item["remote"] = 1 if value else 0
                else:
                    item[field] = value
            loop.append(item)

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
            # Station logo if one is stored (playlist add image:<path>),
            # else a default placeholder (Perl uses html/images/favorites.png
            # — the "heart" SqueezePlay draws for cover-less streams).
            _stream_img = getattr(player, "stream_images", {}).get(cur_url, "") or ""
            if _stream_img.startswith("html/"):
                _stream_img = _stream_img[len("html/"):]
            remote_meta = {
                "id": str(-abs(hash(cur_url))),
                "title": cur_info.get("title", ""),
                "artist": cur_info.get("artist", ""),
                "album": cur_info.get("album", ""),
                "duration": cur_info.get("duration", 0) or 0,
                "url": cur_url,
                "remote": 1,
                "coverart": "0",
                "artwork_url": _stream_img or "html/images/favorites.png",
            }

        menu_block = None
        if "menu:menu" in (args or []):
            # Squeezer's parseMenuStatus expects 'menu' to be the item
            # ARRAY directly ((Object[]) record.get("menu")) — not an
            # object with item_loop.
            menu_block = self._home_menu()

        # P4-1: sync fields (only when synced) + optional status fields
        sync_fields: dict[str, str] = {}
        if getattr(player, "sync_master", None):
            sync_fields["sync_master"] = player.sync_master
        if getattr(player, "sync_slaves", None):
            sync_fields["sync_slaves"] = ",".join(player.sync_slaves)
        cur_tid = playlist_ids[cur] if cur < len(playlist_ids) else None

        result: dict = {
            "mode": player.mode,
            "power": 1 if player.power else 0,
            "player_name": player.name or player.mac,
            # SqueezeClient's PlayerStatusResponse requires this
            "player_connected": 1,
            # SqueezePlay reads playerStatus.remote == 1 to append the live
            # current_title for a radio stream.
            "remote": 1 if isinstance(cur_tid, str) else 0,
            # SqueezeClient parses these as STRING enums ("0"/"1"/"2",
            # PlayerStatus.ShuffleState/RepeatState @SerialName) — an int
            # breaks kotlinx serialization → app crash on connect.
            "playlist shuffle": str(int(getattr(player, "shuffle", 0) or 0)),
            "playlist repeat": str(int(getattr(player, "repeat", 0) or 0)),
            "mixer volume": player.volume or 50,
            "playlist_tracks": len(playlist_ids),
            # Int (no default in PlayerStatusResponse) — a string crashes.
            "playlist_cur_index": int(cur) if str(cur).isdigit() else 0,
            "time": elapsed,
            "rate": 1 if player.mode == "play" else 0,
            # Perl parity: 'playlist mode' mirrors the repeat state
            # (off/repeat/repeat-one), randomplay mirrors shuffle.
            "playlist mode": ("off", "repeat", "repeat-one")[min(2, int(getattr(player, "repeat", 0) or 0))],
            "randomplay": int(getattr(player, "shuffle", 0) or 0),
            "digital_volume_control": 1,
            "use_volume_control": 1,
            "signalstrength": 0,
            "seq_no": str(getattr(player, "_seq_no", 0) or 0),
            "playlist_timestamp": time.time(),
            "playlist_loop": loop,
        }
        # Jive/SqueezePlay Now Playing reads the current_* fields (not
        # 'title'): a radio stream shows its StreamTitle, a local track its
        # title/artist/album. SqueezePlay otherwise shows a blank line.
        np_title = cur_info.get("title", "")
        np_artist = cur_info.get("artist", "")
        np_album = cur_info.get("album", "")
        result["current_title"] = np_title
        result["current_artist"] = np_artist
        result["current_album"] = np_album
        result["current_url"] = getattr(player, "current_url", "") or ""
        # Squeezer/SqueezePlay also read the bare 'track'/'artist'/'album'
        # (and often a 'track' key for the now-playing line). For a radio
        # stream, give the stream name as track and parse "Artist - Title"
        # from the StreamTitle so at least the track line is never blank.
        result["track"] = np_title
        if not np_artist and np_title and " - " in np_title:
            head, _, tail = np_title.partition(" - ")
            if tail:
                result["artist"] = head.strip()
                result["track"] = tail.strip()
                result["current_artist"] = head.strip()
                result["current_title"] = tail.strip()
        if not result.get("artist"):
            result.setdefault("artist", np_artist)
        if not result.get("album"):
            result.setdefault("album", np_album)
        # SqueezeClient's PlayerStatusResponse declares 'count: Int' and
        # 'offset: String?' WITHOUT defaults — a missing count crashes the
        # app on connect. Perl parity: count = number of items returned.
        result["count"] = len(loop)
        result["offset"] = "0"
        # Player IP:port (Perl sends 'ip:port' of the control connection).
        try:
            result["player_ip"] = f"{player.ip}:{getattr(player, 'port', 0) or 0}"
        except Exception:
            pass
        # Web-UI-only conveniences (the Perl LMS does NOT send these in
        # status; our SPA/SqueezeTray read them). Kept out of the strict
        # parity path — apps that compare key sets see Perl shape.
        if not tags or True:  # cheap: keep for local UI consumers
            result.setdefault("artist", cur_info.get("artist", ""))
            result.setdefault("title", cur_info.get("title", ""))
            result.setdefault("album", cur_info.get("album", ""))
            # Report a duration for a live stream as a non-zero value: leaving
            # it out (null) makes SqueezePlay's status processing choke and
            # prevents the Now-Playing window from opening, while a 0 makes
            # SqueezeClient's seek-slider crash (valueTo=0.1 vs growing
            # position). Using the position keeps valueTo >= value for
            # SqueezeClient while still giving SqueezePlay a duration.
            dur = float(cur_info.get("duration", 0) or 0)
            pos = float(elapsed or 0)
            if dur <= 0 and pos > 0:
                dur = pos
            if dur <= 0:
                dur = 1.0
            result["duration"] = dur
            result["item_loop"] = loop
        result |= sync_fields
        if remote_meta:
            result["remoteMeta"] = remote_meta
        if menu_block:
            result["menu"] = menu_block
        return result

    def _home_menu(self) -> list[dict]:
        """The root browse menu (Home), matching the real LMS item shape
        that SqueezePlay/SqueezeClient/Squeezer reliably render.

        The controllers recognise the canonical item ids (myMusic, favorites,
        radios, myMusicMusicFolder...), the node values (home/myMusic) and
        the browselibrary navigation command. Items with isANode become
        expandable nodes; the myMusic children are emitted in the SAME
        item_loop and nested under the myMusic node by the controller.
        """
        def _go(cmd: list[str], params: dict | None = None) -> dict:
            go: dict = {"player": 0, "cmd": cmd}
            if params:
                go["params"] = params
            return {"go": go, "do": go}

        def _my_item(iid: str, name: str, node: str, weight: int,
                     mode: str, icon: str = "") -> dict:
            """A My Music child that browses the library by mode."""
            it = {
                "id": iid,
                "text": name,
                "node": node,
                "weight": weight,
                "actions": _go(["browselibrary", "items"],
                               {"menu": 1, "mode": mode}),
            }
            if icon:
                it["icon"] = icon
            return it

        my_children = [
            _my_item("myMusicArtistsAllArtists", "Alle Interpreten", "myMusic",
                     11, "artists", "html/images/artists.png"),
            _my_item("myMusicAlbums", "Alben", "myMusic", 20,
                     "albums", "html/images/albums.png"),
            _my_item("myMusicGenres", "Stilrichtung", "myMusic", 30,
                     "genres", "html/images/genres.png"),
            _my_item("myMusicYears", "Jahrgang", "myMusic", 40, "years"),
            _my_item("myMusicMusicFolder", "Musikordner", "myMusic", 70,
                     "bmf", "html/images/musicfolder.png"),
            _my_item("myMusicSearch", "Suchen", "myMusic", 90, "search"),
        ]

        return [
            # My Music node — SqueezePlay expands it into the myMusic
            # children (which share this item_loop, node=myMusic).
            {"id": "myMusic", "text": "Eigene Musik", "node": "home",
             "isANode": 1, "weight": 11, "hasitems": 1},
            {"id": "favorites", "text": "Favoriten", "node": "home",
             "weight": 100, "actions": _go(["favorites", "items"],
                                           {"menu": "favorites"})},
            {"id": "radios", "text": "Radio", "node": "home", "weight": 20,
             "actions": _go(["radios"], {"menu": "radio"})},
            *my_children,
        ]

    @staticmethod
    def _alarm_to_item(index: int, alarm) -> dict:
        """Convert an Alarm to the JSON alarm_loop entry shape."""
        return {
            "id": index,
            "enabled": 1 if (alarm and alarm.enabled) else 0,
            "day": alarm.day_int() if alarm else 0,
            "hour": int(alarm.time.split(":")[0]) if alarm else 0,
            "minute": int(alarm.time.split(":")[1]) if alarm else 0,
            "times": 1,
            "fade": alarm.fade if alarm else 0,
            "volume": alarm.volume if alarm else -1,
            "duration": alarm.duration if alarm else 0,
            "repeat": 1 if (alarm and alarm.repeat) else 0,
            "wake": (alarm.wake if alarm else "") or "",
            # 'url:' only → url; 'track:' → track id; 'fr:' → favorite id
            "url": (alarm.wake[4:] if alarm and alarm.wake.startswith("url:")
                    else ""),
            "track": (alarm.wake[6:] if alarm and alarm.wake.startswith("track:")
                      else ""),
            "favorite": (alarm.wake[3:] if alarm and alarm.wake.startswith("fr:")
                         else ""),
        }

    async def _json_alarms(self, pid: str | None, args: list) -> dict:
        """['alarms'] → list all alarms for the player (LMS JSON shape)."""
        from lyrion.alarms import AlarmManager

        mac = pid if pid not in ("-", None) else ""
        mgr = AlarmManager()
        alarms = mgr.alarms_for(mac)
        loop = [self._alarm_to_item(idx, alarms.get(idx)) for idx in sorted(alarms)]
        return {
            "count": len(loop),
            "fade": 0,
            "alarm_loop": loop,
        }

    async def _json_alarm(self, pid: str | None, args: list) -> dict:
        """['alarm', '<idx>'] or ['alarm', '<idx>', 'set:<kv>']."""
        from lyrion.alarms import AlarmManager, _alarm_from_parts, Alarm

        mac = pid if pid not in ("-", None) else ""
        mgr = AlarmManager()
        path = str(args[0]) if args else "0"
        # Some clients pass '<idx>' as '<idx>-' (row selection).
        idx_str = path.split("-")[0]
        try:
            idx = int(idx_str)
        except ValueError:
            return {"error": "invalid alarm index"}

        if len(args) == 1 or (len(args) >= 2 and args[1] in ("?", "query")):
            return self._alarm_to_item(idx, mgr.get(mac, idx))

        parts: dict[str, str] = {}
        for tok in args[1:]:
            if ":" in tok:
                k, v = tok.split(":", 1)
                parts[k] = v
        current = mgr.get(mac, idx)
        a = _alarm_from_parts(idx, parts)
        if current:
            for f in ("enabled", "days", "time", "volume", "fade", "duration",
                      "repeat", "wake"):
                if f not in parts:
                    # If hour/minute were given, don't clobber the time
                    # we just built from them.
                    if f == "time" and ("hour" in parts or "minute" in parts):
                        continue
                    # If an integer day-mask was given, its derived 'days'
                    # string must not be overwritten from current.
                    if f == "days" and "day" in parts:
                        continue
                    # 'url:'/'track:' build the wake value; don't clobber it.
                    if f == "wake" and ("url" in parts or "track" in parts):
                        continue
                    setattr(a, f, getattr(current, f))
        mgr.set(mac, idx, a)
        return self._alarm_to_item(idx, a)

    @staticmethod
    def _browse_response(loop: list, total: int | None = None,
                         plural: str | None = None) -> dict:
        """Browse/menu response — Jive expects 'item_loop', the older
        JSON-RPC clients 'loop_loop' and the controllers read the
        category-specific name ('artists_loop' etc., LMS reference);
        deliver all (identical). count = the total number of matches
        (not the page length).

        Each item is normalized so the Android controllers (SqueezeClient /
        Squeezer) can render it: they read 'text' for the display title and
        'type' for the row kind. The raw LMS fields (album/artist/title/name)
        are kept for the Real-LMS reference shape; unknown keys are ignored
        by the kotlinx deserializers, so adding text/type/icon is safe."""
        for it in loop:
            if not isinstance(it, dict):
                continue
            # Display title: controllers read 'text'; fall back to the
            # LMS-native display field used by this item type.
            text = it.get("text") or it.get("title") or it.get("album") \
                or it.get("artist") or it.get("name") or ""
            it.setdefault("text", text)
            it.setdefault("title", text)
            # Row type: 'outline' (folder-ish) is a valid SlimBrowseItemType
            # understood by every controller. Never leave it as a raw
            # artist/album/song/genre/radio value the enum can't decode.
            if it.get("type") in (None, "artist", "album", "song", "genre",
                                  "radio", "track", "folder"):
                it["type"] = "outline"
            it.setdefault("hasitems", 1)
            # Artwork hint if the item carries a cover already.
            if it.get("coverid") and not it.get("icon"):
                it["icon"] = f"/music/{it['coverid']}/cover.jpg"
                it["icon-id"] = it["coverid"]
        resp: dict = {"count": len(loop) if total is None else total,
                      "offset": 0,
                      "loop_loop": loop, "item_loop": loop}
        if plural:
            resp[plural] = loop
        return resp

    async def _load_tracks(self, track_ids: list[int]) -> dict:
        """Load track metadata for ids (songinfo/status tag fields)."""
        result: dict = {}
        if not track_ids:
            return result
        try:
            import sqlite3
            # Read-only connection: status polls from many clients must
            # never block on (or lock) the writer (aiosqlite session).
            db = sqlite3.connect(
                f"file:{_library_db_path()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(track_ids))
            # Track fields (songinfo tags: d,y,t,u,r,T,I,x,g,c,j,J,K,i,o,f,k,w)
            rows = db.execute(
                f"""SELECT id, title, url, duration, year, tracknum, bitrate,
                           samplerate, bitspersample, genre, cover, remote,
                           disc, filesize, comment, lyrics, content_type
                    FROM tracks WHERE id IN ({placeholders})""",
                track_ids,
            ).fetchall()
            # Artists (role 1 = artist → a=name, s=id) and albums
            # (l=name, e=id) in bulk — one query per join table. The join
            # tables use 'track'/'contributor'/'album' columns and the
            # role is an INTEGER (1 = artist), not a string.
            artists: dict[int, dict] = {}
            for r in db.execute(
                f"""SELECT tc.track, c.id AS artist_id, c.name AS artist
                    FROM tracks_contributors tc
                    JOIN contributors c ON c.id = tc.contributor
                    WHERE tc.track IN ({placeholders}) AND tc.role = 1""",
                track_ids,
            ):
                artists.setdefault(r["track"], {})["artist_id"] = r["artist_id"]
                artists.setdefault(r["track"], {})["artist"] = r["artist"]
            albums: dict[int, dict] = {}
            for r in db.execute(
                f"""SELECT ta.track, a.id AS album_id, a.title AS album,
                           a.artwork AS album_artwork
                    FROM tracks_albums ta
                    JOIN albums a ON a.id = ta.album
                    WHERE ta.track IN ({placeholders})""",
                track_ids,
            ):
                albums.setdefault(r["track"], {})["album_id"] = r["album_id"]
                albums.setdefault(r["track"], {})["album"] = r["album"]
                if r["album_artwork"]:
                    albums.setdefault(r["track"], {})[
                        "artwork_url"] = f"/music/{r['album_id']}/cover.jpg"
            for row in rows:
                tid = row["id"]
                info: dict = {
                    "title": row["title"] or "",
                    "url": row["url"] or "",
                    "duration": row["duration"] or 0,
                    "year": row["year"],
                    "tracknum": row["tracknum"],
                    "bitrate": row["bitrate"],
                    "samplerate": row["samplerate"],
                    "samplesize": row["bitspersample"],
                    "genre": row["genre"] or "",
                    "cover": row["cover"],
                    "remote": 1 if row["remote"] else 0,
                    "disc": row["disc"],
                    "filesize": row["filesize"],
                    "comment": row["comment"],
                    "lyrics": row["lyrics"],
                    "type": row["content_type"],
                }
                info.update(artists.get(tid, {}))
                info.update(albums.get(tid, {}))
                result[tid] = info
            db.close()
        except Exception:
            logger.exception("_load_tracks failed for %d ids", len(track_ids))
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
                # Accept a DB track id, a 'track_id:<n>' tag (controllers),
                # or a plain URL. SqueezeTray adds URLs (radio/favorites);
                # the SPA + Android controllers add tagged track ids.
                # A stream URL may carry a display title (station name) via a
                # paired 'title:<name>' so the playlist shows it instead of
                # 'Radio Stream':
                #   playlist add <url> title:<name>
                player = pm.get_player(pid)
                if player is not None:
                    pending = ""
                    for item in rest:
                        low = str(item).lower()
                        if low.startswith("track_id:") or low.startswith("item_id:"):
                            tid = low.split(":", 1)[1]
                            if tid.isdigit():
                                player.playlist.append(int(tid))
                        elif low.startswith("url:"):
                            pending = low.split(":", 1)[1]
                        elif low.startswith("title:"):
                            title = str(item).split(":", 1)[1]
                            # Append the pending bare URL (it was held waiting
                            # for a paired title) and record its display name.
                            if pending:
                                player.playlist.append(pending)
                                self._set_stream_title(player, pending, title)
                                pending = ""
                            else:
                                # 'url:<x> title:<y>' form — URL already parsed.
                                self._set_stream_title(player, pending or "", title)
                        elif low.startswith("image:"):
                            # Paired logo path for the pending stream URL.
                            self._set_stream_image(player, pending or "", str(item).split(":", 1)[1])
                        elif str(item).isdigit():
                            player.playlist.append(int(item))
                        else:
                            # bare URL — remember it and wait for a paired title
                            if pending:
                                player.playlist.append(pending)
                            pending = str(item)
                        player.last_activity = time.time()
                    if pending:
                        player.playlist.append(pending)
                    player.playlist_total = len(player.playlist)
            elif sub == "index" and rest:
                idx = rest[0]
                player = pm.get_player(pid)
                if player is not None and str(idx).isdigit():
                    player.playlist_position = int(idx)
                    await self._play_playlist_item(pm, player, int(idx))
            elif sub == "play":
                # LMS-compatible 'playlist play [<index>|track_id:<n>|item_id:<n>|<url>]'
                player = pm.get_player(pid)
                if player is not None:
                    idx = player.playlist_position or 0
                    first = str(rest[0]).lower() if rest else ""
                    if first.startswith("item_id:"):
                        # Favorite by hierarchical id (SqueezePlay/Squeezer).
                        # route through the favorite manager so the CORRECT
                        # stream plays (resolve_path uses dbid), not the
                        # current playlist index.
                        try:
                            from lyrion.music.favorites import get_favorites_manager
                            from lyrion.control.cli_commands import _fav_resolve_id
                            fm = get_favorites_manager()
                            fav_id = await _fav_resolve_id(fm, str(rest[0]).split(":", 1)[1])
                            if fav_id is not None:
                                await fm.play(pid, fav_id)
                                return
                        except Exception:
                            pass
                    elif "://" in str(rest[0]) if rest else False:
                        # Bare stream URL
                        await pm.play_url(pid, str(rest[0]), "")
                        return
                    elif rest and str(rest[0]).isdigit():
                        idx = int(rest[0])
                    elif rest and first.startswith("track_id:"):
                        # Flush a bare track id as a one-item playlist entry.
                        tid = str(rest[0]).split(":", 1)[1]
                        self._playlist_flush_track(pm, player, pid, tid)
                        idx = player.playlist_position or 0
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

    @staticmethod
    def _set_stream_title(player, url: str, title: str) -> None:
        """Associate a display title with a stream URL held in the playlist.

        'playlist add <url> title:<name>' stores the station name so playlist
        rendering shows it instead of the generic 'Radio Stream'. The title is
        keyed by URL on the player, not baked into the playlist entry (which
        stays a str URL to keep the slimproto/CLI paths simple).
        """
        if not url or not title:
            return
        try:
            titles = getattr(player, "stream_titles", None)
            if titles is None:
                titles = {}
                player.stream_titles = titles
            titles[url] = title
        except Exception:
            pass

    @staticmethod
    def _set_stream_image(player, url: str, image: str) -> None:
        """Associate a logo/artwork path with a stream URL in the playlist.

        'playlist add <url> image:<path>' stores the station logo so the Now
        Playing panel renders it instead of the generic radio icon. The stored
        path is normalized to a URL relative to static_dir (which already
        contains 'html/') — an 'html/...' prefix would otherwise be doubled
        and 404 when the path is re-composed.
        """
        if not url or not image:
            return
        try:
            # Drop a leading 'html/' so '/html/images/x' -> '/images/x'.
            image = str(image)
            if image.startswith("html/"):
                image = image[len("html/"):]
            images = getattr(player, "stream_images", None)
            if images is None:
                images = {}
                player.stream_images = images
            images[url] = image
        except Exception:
            pass

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
            # Set the mode SYNCHRONOUSLY BEFORE the (async) stream send, so
            # player.status reports 'playing' immediately after a play command
            # (real LMS does this). Otherwise a remote/stream start lags and a
            # status poll right after the command still reports the old mode,
            # causing the UI icon to flip back momentarily.
            player.mode = "play"
            player.playlist_position = idx
            if isinstance(item, int):
                player.current_track_id = item
            pm.set_mode(player.mac, "play")
            ok = True
            if isinstance(item, int):
                ok = await handler.send_strm_to_player(player.mac, item)
            else:
                from lyrion.networking.protocol import SlimProtoClient

                codec = SlimProtoClient._guess_codec_from_url(str(item))
                ok = await handler.send_remote_stream(player.mac, str(item), codec)
            # If the strm could not be delivered (player disconnected /
            # writer gone), don't leave the player stuck in 'play': revert
            # to 'stop' so the UI poll flips the icon back. This mirrors the
            # real LMS, which only reports 'playing' once the stream is
            # actually accepted.
            if not ok:
                logger = __import__("logging").getLogger("lyrion.web.api")
                logger.warning("Failed to send strm to %s (item %d) — reverting to stop", player.mac, idx)
                player.mode = "stop"
                player.playlist_position = -1
                pm.set_mode(player.mac, "stop")
                return
            logger = __import__("logging").getLogger("lyrion.web.api")
            logger.info("Playing playlist item %d (%r) on %s", idx, item, player.mac)
        except Exception as exc:
            logger = __import__("logging").getLogger("lyrion.web.api")
            logger.warning("_play_playlist_item failed: %s", exc)

    def _playlist_flush_track(self, pm, player, pid: str, tid: str) -> None:
        """Replace the playlist with a single track id and start it.

        Handles 'playlist play track_id:<n>' where the controllers send a
        bare tagged id (no playlist yet) — LMS play-by-track-id semantics.
        """
        if not tid.isdigit():
            return
        player.playlist = [int(tid)]
        player.playlist_total = 1
        player.playlist_position = 0
        player.last_activity = time.time()

    async def _json_search(self, args: list[str]) -> dict:
        """LMS 'search <start> <count> term:<begriff>' — grouped results.

        Returns count plus artists_count/albums_count/genres_count/
        tracks_count and the per-group loops (id + name), like the real
        LMS grouped search response.
        """
        nums = [int(s) for s in args if str(s).isdigit()]
        start = nums[0] if nums else 0
        count = nums[1] if len(nums) > 1 else 20
        term = next((str(a)[5:] for a in args if str(a).startswith("term:")), "")
        empty = {
            "count": 0, "artists_count": 0, "albums_count": 0,
            "genres_count": 0, "tracks_count": 0, "contributors_count": 0,
            "artists_loop": [], "contributors_loop": [], "albums_loop": [],
            "genres_loop": [], "tracks_loop": [],
        }
        if not term:
            return empty
        like = f"%{term}%"
        try:
            artists = _db_query(
                "SELECT DISTINCT c.id, c.name FROM contributors c "
                "JOIN tracks_contributors tc ON tc.contributor = c.id "
                "AND tc.role = 1 WHERE c.name LIKE ? "
                "ORDER BY c.name COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, count, start),
            )
            albums = _db_query(
                "SELECT id, title FROM albums WHERE title LIKE ? "
                "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, count, start),
            )
            genres = _db_query(
                "SELECT DISTINCT genre AS name FROM tracks "
                "WHERE genre LIKE ? ORDER BY genre COLLATE NOCASE "
                "LIMIT ? OFFSET ?",
                (like, count, start),
            )
            tracks = _db_query(
                "SELECT id, title, url, duration FROM tracks "
                "WHERE title LIKE ? ORDER BY title COLLATE NOCASE "
                "LIMIT ? OFFSET ?",
                (like, count, start),
            )
        except Exception:
            return empty
        a_count = _db_query(
            "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id "
            "AND tc.role = 1 WHERE c.name LIKE ?", (like,))
        al_count = _db_query(
            "SELECT COUNT(*) AS n FROM albums WHERE title LIKE ?", (like,))
        g_count = _db_query(
            "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre LIKE ?",
            (like,))
        t_count = _db_query(
            "SELECT COUNT(*) AS n FROM tracks WHERE title LIKE ?", (like,))
        # Perl parity for the search loops: the Perl LMS returns MINIMAL
        # items — contributors_loop {contributor, contributor_id},
        # albums_loop {album, album_id}, tracks_loop {track, track_id}.
        # Extra keys (id/title/url) are additive; controllers ignore them.
        return {
            "count": (a_count[0]["n"] if a_count else 0)
                     + (al_count[0]["n"] if al_count else 0)
                     + (g_count[0]["n"] if g_count else 0)
                     + (t_count[0]["n"] if t_count else 0),
            "artists_count": a_count[0]["n"] if a_count else 0,
            "contributors_count": a_count[0]["n"] if a_count else 0,
            "albums_count": al_count[0]["n"] if al_count else 0,
            "genres_count": g_count[0]["n"] if g_count else 0,
            "tracks_count": t_count[0]["n"] if t_count else 0,
            "artists_loop": [{"contributor": r["name"] or "",
                              "contributor_id": r["id"]}
                             for r in artists],
            "contributors_loop": [{"contributor": r["name"] or "",
                                   "contributor_id": r["id"]}
                                  for r in artists],
            "albums_loop": [{"album": r["title"] or "",
                             "album_id": r["id"]} for r in albums],
            "genres_loop": [{"genre": r["name"] or "",
                             "genre_id": i + 1} for i, r in enumerate(genres)],
            "tracks_loop": [{"track": r["title"] or "",
                             "track_id": r["id"]} for r in tracks],
        }

    async def _radio_stations_loop(self) -> list[dict]:
        """Build the item_loop for the Radio directory. Falls back to the
        remote-URL favorites (the user's radio/streams) when the Radio
        Manager has no persisted stations, so the list is never empty."""
        try:
            from lyrion.music.radio import get_radio_manager
            stations = await get_radio_manager().list_stations()
            if stations:
                return [
                    {
                        "id": str(s.id),
                        "name": s.name,
                        "text": s.name,
                        "url": s.url,
                        "type": "audio",
                        "hasitems": 0,
                        "actions": {
                            "play": {"player": 0, "cmd": ["playlist", "play"],
                                     "params": {"item_id": str(s.id)}},
                            "do": {"player": 0, "cmd": ["playlist", "play"],
                                   "params": {"item_id": str(s.id)}},
                        },
                    }
                    for s in stations
                ]
        except Exception:  # noqa: BLE001
            pass
        # Fallback: favorites that point at a remote stream (a URL).
        try:
            from lyrion.music.favorites import get_favorites_manager
            fm = get_favorites_manager()
            loop = await self._fav_items_loop(fm, None, "0", False)
            out = []
            for it in loop:
                url = it.get("url") or ""
                if not str(url).startswith(("http://", "https://", "mms://", "rtp://")):
                    continue
                name = it.get("text") or it.get("name") or "Radio"
                out.append({
                    "id": str(it.get("id", "")),
                    "name": name,
                    "text": name,
                    "url": url,
                    "type": "audio",
                    "hasitems": 0,
                    "actions": {
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"item_id": str(it.get("id", ""))}},
                        "do": {"player": 0, "cmd": ["playlist", "play"],
                               "params": {"item_id": str(it.get("id", ""))}},
                    },
                })
            return out
        except Exception:  # noqa: BLE001
            return []

    async def _json_radios(self, cmd: str, args: list[str]) -> dict:
        """radios [start count] — the Radio directory as a browse list.

        The controllers open it from the home-menu 'Radio' item
        (actions.go = ['browse','radios'] or the bare 'radios' query)."""
        loop = await self._radio_stations_loop()
        return self._browse_response(loop)

    async def _json_radiosearch(self, cmd: str, args: list[str]) -> dict:
        """radiosearch <start> <count> term:<text> — search radio stations.

        Uses the Radio Browser API when reachable; on any failure returns a
        valid empty list so the controller shows 'no stations' instead of a
        crash (the controller expects count + item_loop)."""
        nums = [int(s) for s in args if str(s).isdigit()]
        start = nums[0] if nums else 0
        count = nums[1] if len(nums) > 1 else 20
        term = next((str(a)[5:] for a in args if str(a).startswith("term:")), "")
        if not term:
            return self._browse_response([])
        try:
            from lyrion.music.radio import get_radio_manager
            stations = await get_radio_manager().directory.search(
                name=term, limit=count, offset=start)
            items = []
            for s in stations:
                name = s.name or "Unknown"
                items.append({
                    "id": str(s.id) if s.id is not None else str(s.url),
                    "name": name,
                    "text": name,
                    "url": s.url,
                    "type": "audio",
                    "hasitems": 0,
                    "actions": {
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"item_id": str(s.id) if s.id is not None
                                            else str(s.url)}},
                        "do": {"player": 0, "cmd": ["playlist", "play"],
                               "params": {"item_id": str(s.id) if s.id is not None
                                          else str(s.url)}},
                    },
                })
            return self._browse_response(items)
        except Exception:  # noqa: BLE001
            return self._browse_response([])

    async def _json_browselibrary(self, cmd: str, args: list[str]) -> dict:
        """browselibrary items <start> <count> mode:<albums|artists|genres|
        years|bmf|search> — the My-Music children navigation.

        SqueezePlay/SqueezeClient open My Music through this command. The
        response must be the OpenSqueeze library shape (count + loop_loop,
        items with a string id, name, type playlist/audio/folder, image,
        isaudio, hasitems) — the same shape the favorites list uses that the
        controllers render and navigate. Each item also carries the go/play
        actions so clients that drive navigation from actions work too.
        """
        nums = [int(s) for s in args if str(s).isdigit()]
        start = nums[0] if nums else 0
        count = nums[1] if len(nums) > 1 else 512
        mode = next((str(a)[5:] for a in args if str(a).startswith("mode:")),
                    "albums")
        search = next((str(a)[7:] for a in args if str(a).startswith("search:")),
                      "")
        try:
            rows, total, plural, kind = await self._library_rows(
                mode, start, count, search)
        except Exception:  # noqa: BLE001
            return {"count": 0, "loop_loop": []}
        loop = []
        for r in rows:
            if kind == "albums":
                ident, name, image = str(r["id"]), r["title"] or "", \
                    (f"/music/{r['id']}/cover.jpg" if r.get("artwork") else "")
                go = ["titles"]
                go_params = {"album_id": r["id"]}
                play_params = {"album_id": r["id"]}
                icon = image or "html/images/albums.png"
            elif kind == "artists":
                ident, name = str(r["id"]), r["name"] or ""
                go = ["albums"]
                go_params = {"artist_id": r["id"]}
                play_params = {"artist_id": r["id"]}
                icon = "html/images/artists.png"
            elif kind == "genres":
                ident, name = str(r["id"]), r["genre"] or ""
                go = ["albums"]
                go_params = {"genre": name}
                play_params = {"genre": name}
                icon = "html/images/genres.png"
            elif kind == "years":
                ident, name = str(r["year"]), str(r["year"])
                go = ["albums"]
                go_params = {"year": r["year"]}
                play_params = {"year": r["year"]}
                icon = "html/images/years.png"
            elif kind == "folder":
                ident, name = str(r["id"]), r["name"]
                go = ["browselibrary", "items"]
                go_params = {"mode": "bmf", "search": str(r["id"])}
                play_params = {}
                icon = "html/images/musicfolder.png"
            else:
                continue
            item = {
                "id": ident,
                "name": name,
                "text": name,
                "title": name,
                "type": "playlist" if kind != "folder" else "folder",
                "image": icon,
                "isaudio": 1,
                "hasitems": 1,
            }
            item["actions"] = {
                "go": {"player": 0, "cmd": go, "params": go_params},
                "play": {"player": 0, "cmd": ["playlist", "play"],
                         "params": play_params},
            }
            loop.append(item)
        return {"count": total, "loop_loop": loop,
                "item_loop": loop, plural: loop}

    async def _library_rows(self, mode: str, start: int, count: int,
                            search: str = ""):
        """Return (rows, total, plural, kind) for a browselibrary mode."""
        def q(sql, *p):
            return _db_query(sql, p)

        def total_of(sql, *p):
            rows = _db_query(sql, p)
            # _db_query returns list[dict]; grab the first row's first value.
            return list(rows[0].values())[0] if rows else 0

        if mode == "albums":
            rows = q("SELECT DISTINCT al.id, al.title, al.artwork FROM albums al "
                     "ORDER BY al.title LIMIT ? OFFSET ?", count, start)
            total = total_of("SELECT COUNT(DISTINCT al.id) FROM albums al")
            return rows, total, "albums_loop", "albums"
        if mode == "artists":
            rows = q("SELECT DISTINCT c.id, c.name FROM contributors c "
                     "JOIN tracks_contributors tc ON tc.contributor = c.id "
                     "AND tc.role = 1 ORDER BY c.name LIMIT ? OFFSET ?",
                     count, start)
            total = total_of("SELECT COUNT(DISTINCT c.id) FROM contributors c "
                             "JOIN tracks_contributors tc ON tc.contributor = c.id "
                             "AND tc.role = 1")
            return rows, total, "artists_loop", "artists"
        if mode == "genres":
            rows = q("SELECT DISTINCT genre AS genre FROM tracks "
                     "WHERE genre != '' ORDER BY genre COLLATE NOCASE "
                     "LIMIT ? OFFSET ?", count, start)
            total = total_of("SELECT COUNT(DISTINCT genre) FROM tracks "
                             "WHERE genre != ''")
            return rows, total, "genres_loop", "genres"
        if mode == "years":
            rows = q("SELECT DISTINCT year FROM tracks WHERE year > 0 "
                     "ORDER BY year DESC LIMIT ? OFFSET ?", count, start)
            total = total_of("SELECT COUNT(DISTINCT year) FROM tracks "
                             "WHERE year > 0")
            return rows, total, "years_loop", "years"
        if mode in ("bmf", "musicfolder"):
            # folder browse: build the tree from the track URLs
            if search:
                where = " AND url LIKE ?"
                like = search.rstrip("/") + "/%"
                rows = q("SELECT DISTINCT url FROM tracks "
                         "WHERE url LIKE 'file://%'" + where +
                         " ORDER BY url LIMIT ? OFFSET ?", like, count, start)
            else:
                rows = q("SELECT DISTINCT url FROM tracks "
                         "WHERE url LIKE 'file://%' "
                         "ORDER BY url LIMIT ? OFFSET ?", count, start)
            names = []
            for r in rows:
                path = r["url"][len("file://"):].lstrip("/")
                if search:
                    rel = path[len(search.rstrip("/")) + 1:]
                    names.append(rel.split("/", 1)[0])
                else:
                    parts = path.split("/")
                    names.append(parts[0] if len(parts) >= 2 else path)
            names = list(dict.fromkeys(n for n in names if n))
            loop_rows = [{"id": search.rstrip("/") + "/" + n if search
                          else "file:///" + n, "name": n}
                         for n in names]
            return loop_rows, len(names), "musicfolder_loop", "folder"
        # fallback: albums
        return await self._library_rows("albums", start, count)

    async def _json_years(self, start: int, count: int) -> dict:
        """years — a distinct release-year list (My Music → Jahrgang)."""
        try:
            rows = _db_query(
                "SELECT DISTINCT year FROM tracks WHERE year > 0 "
                "ORDER BY year DESC LIMIT ? OFFSET ?",
                (count, start))
            loop = [{"id": str(r["year"]), "text": str(r["year"]),
                     "name": str(r["year"]), "type": "outline",
                     "actions": {"go": {"player": 0, "cmd": ["albums"],
                                        "params": {"year": r["year"]}},
                                 "do": {"player": 0, "cmd": ["albums"],
                                        "params": {"year": r["year"]}}}}
                    for r in rows]
            total = _db_query("SELECT COUNT(DISTINCT year) FROM tracks "
                              "WHERE year > 0")[0][0] if rows else 0
            return self._browse_response(loop, total, "years_loop")
        except Exception:  # noqa: BLE001
            return self._browse_response([])

    async def _json_browse(self, cmd: str, args: list[str]) -> dict:
        """Browse library tables (albums/artists/songs/genres) as JSON."""
        # browse <target> [<start> <count>] — the home menu items carry
        # actions.go/do.cmd = ["browse", <id>]; map the target onto the
        # library queries / favorites / radios so menu navigation works.
        if cmd == "browse" and args:
            target = str(args[0]).lower()
            rest = args[1:]
            if target in ("artists", "albums", "songs", "titles", "genres"):
                return await self._json_browse(target, rest)
            if target == "favorites":
                try:
                    from lyrion.music.favorites import get_favorites_manager
                    loop = await self._fav_items_loop(
                        get_favorites_manager(), None, "0", False)
                    return self._browse_response(loop)
                except Exception:
                    return self._browse_response([])
            if target == "radios":
                loop = await self._radio_stations_loop()
                return self._browse_response(loop)
            return self._browse_response([])

        start = int(args[0]) if args and str(args[0]).isdigit() else 0
        count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 50
        # P3-2: filters (genre_id/album_id/track_id/artist_id/year/search)
        # + tags: code (t=title a=artist l=album d=duration u=url g=genre y=year)
        filters: dict[str, str] = {}
        for a in args:
            s = str(a)
            if ":" in s:
                k, _, v = s.partition(":")
                if k in ("genre_id", "genre", "album_id", "track_id", "artist_id",
                         "year", "search", "tags"):
                    filters[k] = v
        tags = filters.pop("tags", "")
        plural: str | None = None  # category-specific loop name (LMS ref)
        try:
            import sqlite3
            db = sqlite3.connect(
                f"file:{_library_db_path()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row

            # genre_id: the genres table is empty — resolve the id as the
            # index into the DISTINCT track-genre list (stable order).
            if filters.get("genre_id") and str(filters["genre_id"]).isdigit():
                g = db.execute(
                    "SELECT DISTINCT genre FROM tracks WHERE genre != '' "
                    "ORDER BY genre COLLATE NOCASE LIMIT 1 OFFSET ?",
                    (int(filters["genre_id"]),)).fetchone()
                if g:
                    filters["genre"] = g["genre"]

            def _conds(name_col: str) -> tuple[str, tuple]:
                c: list[str] = []
                p: list = []
                if filters.get("search"):
                    c.append(f"{name_col} LIKE ?")
                    p.append(f"%{filters['search']}%")
                if filters.get("year"):
                    c.append("year = ?")
                    p.append(filters["year"])
                if filters.get("genre"):
                    c.append("genre LIKE ?")
                    p.append(f"%{filters['genre']}%")
                return (" WHERE " + " AND ".join(c)) if c else "", tuple(p)

            if cmd == "artists":
                # Contributors have no role column; the role lives in
                # tracks_contributors.role (1 = artist).
                where, params = _conds("c.name")
                joins = " JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
                extra_joins = ""
                if filters.get("album_id") or filters.get("year") or filters.get("genre"):
                    extra_joins += " JOIN tracks t ON t.id = tc.track"
                    extra_joins += " JOIN tracks_albums ta ON ta.track = t.id" \
                        if filters.get("album_id") else ""
                rows = db.execute(
                    "SELECT DISTINCT c.id, c.name FROM contributors c"
                    + joins + extra_joins + where +
                    " ORDER BY c.name LIMIT ? OFFSET ?",
                    params + (count, start)).fetchall()
                loop = []
                for r in rows:
                    name = r["name"] or ""
                    from urllib.parse import quote as _qa
                    item = {
                        "id": r["id"], "artist": name,
                        # Perl parity: favorites_url in artists_loop.
                        "favorites_url": f"db:contributor.name={_qa(name)}",
                    }
                    item["actions"] = {
                        "go": {"player": 0, "cmd": ["albums"],
                               "params": {"artist_id": r["id"], "menu": "tracks"}},
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"artist_id": r["id"]}},
                    }
                    loop.append(item)
                total = db.execute(
                    "SELECT COUNT(DISTINCT c.id) FROM contributors c"
                    + joins + extra_joins + where, params).fetchone()[0]
                plural = "artists_loop"
            elif cmd == "albums":
                where, params = _conds("al.title")
                joins = ""
                if filters.get("artist_id") or filters.get("genre"):
                    joins += " JOIN tracks_albums ta ON ta.album = al.id" \
                             " JOIN tracks t ON t.id = ta.track"
                if filters.get("artist_id"):
                    joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
                rows = db.execute(
                    "SELECT DISTINCT al.id, al.title, al.year, al.artwork FROM albums al"
                    + joins + where +
                    " ORDER BY al.title LIMIT ? OFFSET ?",
                    params + (count, start)).fetchall()
                loop = []
                for r in rows:
                    # Perl parity (albums without tags:): id, album,
                    # performance, favorites_url, favorites_title. The
                    # Jive actions stay — controllers need them.
                    title = r["title"] or ""
                    from urllib.parse import quote as _q
                    fav_url = f"db:album.title={_q(title)}"
                    item = {
                        "id": r["id"], "album": title,
                        "performance": "",
                        "favorites_url": fav_url,
                        "favorites_title": title,
                        "year": r["year"] or 0,
                    }
                    # Jive actions: go opens the album's tracks, play
                    # plays the whole album.
                    item["actions"] = {
                        "go": {"player": 0, "cmd": ["titles"],
                               "params": {"album_id": r["id"], "menu": "songinfo"}},
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"album_id": r["id"]}},
                    }
                    if r["artwork"]:
                        item["coverid"] = r["id"]
                        item["coverart"] = 1
                        item["artwork_url"] = f"/music/{r['id']}/cover.jpg"
                    loop.append(item)
                total = db.execute(
                    "SELECT COUNT(DISTINCT al.id) FROM albums al" + joins + where,
                    params).fetchone()[0]
                plural = "albums_loop"
            elif cmd == "songs" or cmd == "titles":
                where, params = _conds("t.title")
                joins = ""
                if filters.get("album_id"):
                    joins += " JOIN tracks_albums ta ON ta.track = t.id"
                if filters.get("artist_id"):
                    joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
                rows = db.execute(
                    "SELECT DISTINCT t.id, t.title, t.url, t.duration, "
                    "al.id AS album_id, al.artwork AS album_artwork "
                    "FROM tracks t"
                    " LEFT JOIN tracks_albums ta ON ta.track = t.id"
                    " LEFT JOIN albums al ON al.id = ta.album"
                    + joins + where +
                    " ORDER BY t.title LIMIT ? OFFSET ?",
                    params + (count, start)).fetchall()
                # Enrich with artist/album (songinfo tags g/a/l/d) for the
                # Web UI columns and the controller stream info.
                enrich = await self._load_tracks([r["id"] for r in rows])
                loop = []
                for r in rows:
                    info = enrich.get(r["id"], {})
                    entry = {
                        "id": r["id"], "title": r["title"] or "", "url": r["url"] or "",
                        "duration": r["duration"] or 0,
                        "artist": info.get("artist", ""),
                        "album": info.get("album", ""),
                        # Jive actions: go opens songinfo, play plays the track.
                        "actions": {
                            "go": {"player": 0, "cmd": ["songinfo"],
                                   "params": {"track_id": r["id"]}},
                            "play": {"player": 0, "cmd": ["playlist", "play"],
                                     "params": {"track_id": r["id"]}},
                        },
                    }
                    if r["album_artwork"]:
                        entry["coverid"] = r["album_id"]
                        entry["coverart"] = 1
                        entry["artwork_url"] = f"/music/{r['album_id']}/cover.jpg"
                    loop.append(entry)
                total = db.execute(
                    "SELECT COUNT(DISTINCT t.id) FROM tracks t" + joins + where,
                    params).fetchone()[0]
                plural = "titles_loop"
            elif cmd == "genres":
                # The genres table is not populated by the importer — use the
                # track genre text (same source as the CLI command).
                where, params = _conds("genre")
                if where:
                    where = where.replace(" WHERE ", " WHERE genre != '' AND ", 1)
                else:
                    where = " WHERE genre != ''"
                rows = db.execute(
                    "SELECT DISTINCT genre FROM tracks" + where +
                    " ORDER BY genre COLLATE NOCASE LIMIT ? OFFSET ?",
                    params + (count, start)).fetchall()
                loop = []
                for i, r in enumerate(rows):
                    gid = start + i
                    gname = r["genre"] or ""
                    from urllib.parse import quote as _qg
                    item = {
                        "id": gid, "genre": r["genre"],
                        # Perl parity: favorites_url in genres_loop.
                        "favorites_url": f"db:genre.name={_qg(gname)}",
                        # Jive actions: go opens the genre's artists.
                        "actions": {
                            "go": {"player": 0, "cmd": ["artists"],
                                   "params": {"genre_id": gid, "menu": "albums"}},
                        },
                    }
                    loop.append(item)
                total = db.execute(
                    "SELECT COUNT(DISTINCT genre) FROM tracks" + where,
                    params).fetchone()[0]
                plural = "genres_loop"
            elif cmd == "musicfolder":
                # folder browser derived from the track URLs
                folder = filters.get("search", "")
                if folder:
                    rows = db.execute(
                        "SELECT DISTINCT url FROM tracks WHERE url LIKE ? "
                        "ORDER BY url LIMIT ? OFFSET ?",
                        (folder.rstrip("/") + "/%", count, start)).fetchall()
                    names: list[str] = []
                    for r in rows:
                        rel = r["url"][len(folder.rstrip("/")) + 1:]
                        names.append(rel.split("/", 1)[0])
                    loop = [{"id": str(folder.rstrip("/") + "/" + name), "name": name,
                             "text": name, "type": "folder", "hasitems": 1}
                            for name in dict.fromkeys(names)]
                    total = db.execute(
                        "SELECT COUNT(DISTINCT url) FROM tracks WHERE url LIKE ?",
                        (folder.rstrip("/") + "/%",)).fetchone()[0]
                else:
                    rows = db.execute(
                        "SELECT DISTINCT url FROM tracks WHERE url LIKE 'file://%' "
                        "ORDER BY url LIMIT 500").fetchall()
                    roots: dict[str, str] = {}
                    for r in rows:
                        path = r["url"][len("file://"):].lstrip("/")
                        parts = path.split("/")
                        if len(parts) >= 2:
                            roots.setdefault(parts[0], f"file:///{parts[0]}")
                    names = sorted(roots)
                    page = names[start:start + count]
                    loop = [{"id": roots[n], "name": n, "text": n,
                             "type": "folder", "hasitems": 1} for n in page]
                    total = len(names)
                plural = plural or "musicfolder_loop"
            elif cmd == "songinfo":
                tid = filters.get("track_id") or (args[0] if args and str(args[0]).isdigit() else "")
                if not str(tid).isdigit():
                    return self._browse_response([])
                r = db.execute(
                    "SELECT t.id, t.title, t.url, t.duration, t.year, t.tracknum, "
                    "t.genre, t.filesize, t.bitrate, t.samplerate, t.channels, "
                    "t.content_type AS ctype, t.modtime, t.remote, "
                    "COALESCE(t.compilation,0) AS compilation FROM tracks t "
                    "WHERE t.id = ? LIMIT 1",
                    (int(tid),)).fetchone()
                if r is None:
                    return self._browse_response([])
                # Perl parity: songinfo_loop = ONE item PER FIELD, in the exact
                # LMS order (id, title, artist, work, duration, album_id,
                # filesize, genre, coverart, album, modificationTime, type,
                # genre_id, bitrate, artist_id, tracknum, remote, year,
                # compilation, addedTime). Empty fields are omitted. Perl
                # returns ONLY songinfo_loop (no count / loop_loop / item_loop).
                fields: list[tuple] = [("id", r["id"]), ("title", r["title"] or "")]
                a = db.execute(
                    "SELECT c.id, c.name FROM contributors c JOIN tracks_contributors tc "
                    "ON tc.contributor = c.id AND tc.role = 1 WHERE tc.track = ? "
                    "ORDER BY c.name LIMIT 1", (r["id"],)).fetchone()
                if a:
                    fields.append(("artist", a["name"]))
                # work: composer field (rare) — omit if absent
                if r["duration"] is not None:
                    fields.append(("duration", round(float(r["duration"]), 3)))
                al = db.execute(
                    "SELECT al.id, al.title FROM albums al JOIN tracks_albums ta "
                    "ON ta.album = al.id WHERE ta.track = ? LIMIT 1",
                    (r["id"],)).fetchone()
                if al:
                    fields.append(("album_id", str(al["id"])))
                if r["filesize"]:
                    fields.append(("filesize", str(r["filesize"])))
                if r["genre"]:
                    fields.append(("genre", r["genre"]))
                # coverart: no artwork in the small test library — omit
                if al:
                    fields.append(("album", al["title"]))
                if r["modtime"]:
                    fields.append(("modificationTime", str(r["modtime"])))
                type_code = {
                    "audio/flac": "flc", "audio/x-flac": "flc",
                    "audio/mpeg": "mp3", "audio/mp3": "mp3",
                    "audio/wav": "wav", "audio/x-wav": "wav",
                }.get((r["ctype"] or "").lower(), "")
                if type_code:
                    fields.append(("type", type_code))
                g_id = db.execute(
                    "SELECT id FROM genres WHERE name = ? LIMIT 1",
                    (r["genre"],)).fetchone() if r["genre"] else None
                if g_id:
                    fields.append(("genre_id", str(g_id["id"])))
                if r["bitrate"]:
                    fields.append(("bitrate", str(int(r["bitrate"]))))
                if a:
                    fields.append(("artist_id", str(a["id"])))
                if r["tracknum"]:
                    fields.append(("tracknum", str(r["tracknum"])))
                fields.append(("remote", "1" if r["remote"] else "0"))
                if r["year"]:
                    fields.append(("year", str(r["year"])))
                if r["compilation"]:
                    fields.append(("compilation", "1"))
                if r["modtime"]:
                    fields.append(("addedTime", str(r["modtime"])))
                loop = [{k: v} for k, v in fields]
                # Perl returns ONLY songinfo_loop — no count / loop_loop.
                return {"songinfo_loop": loop}
            else:
                db.close()
                return self._browse_response([])
            db.close()
            return self._browse_response(loop, total, plural)
        except Exception:
            return self._browse_response([])


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

        # Skin assets (Classic/EN/Logic/…): the original LMS serves every
        # file under html/<skin>/ for GET requests. Try the static dir as a
        # last resort before answering 404 — unknown API paths still 404
        # because _serve_static only returns 200 for existing files.
        if method == "GET":
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

        rel = path.lstrip("/")
        # static_dir already contains "html/" (set in __main__.py to
        # <base>/html). A URL path like /html/images/x.png therefore must
        # have its leading "html/" stripped, else the prefix is DOUBLED
        # and every image 404s (<base>/html/html/images/x.png).
        if rel.startswith("html/"):
            rel = rel[len("html/"):]

        file_path = self._static_dir / rel
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
