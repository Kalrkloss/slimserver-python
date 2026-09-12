"""JSON-RPC API for Pyrion Music Server."""
from __future__ import annotations

import json
import logging
import posixpath
import re
import time
import urllib.parse
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


# Perl's numeric conversion of a string (``0 + $str``): skip leading
# whitespace, then the longest leading ASCII number — optional sign,
# integer/fraction part and exponent (``"1e3"`` → 1000, ``"2.9"`` → 2.9,
# ``"010"`` → 10, ``"0x10"`` stops at 'x' → 0). A Unicode digit is NOT a
# digit (``"٢"`` → 0) and anything without a leading number is 0.
_PLAYCTL_NUM_RE = re.compile(
    r"[ \t]*([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
# Perl holds the value as a double, so a token longer than a double
# overflows to ±Inf and is out of range. Keep that as a plain int sentinel
# (never a float, never a Python ``int()`` digit-limit error).
_PLAYCTL_INF = 1 << 62


def _playctl_index(value: str) -> int:
    """Coerce the ``xmlbrowserPlayControl`` token the way Perl does.

    Perl feeds the raw param into arithmetic (``$i = $xmlbrowserPlayControl
    - $subFeed->{offset}``, Slim/Control/XMLBrowser.pm:808), so the token
    numifies exactly like ``0 + $str``: a leading number is used, anything
    else becomes 0 (``"abc"`` → item 0, ``"-1"`` stays negative → out of
    range). Two divergences from naive ``int()``/``\\d`` are deliberate and
    live-probed against Perl 9.1.1:

    * ``"٢"`` (Arabic-Indic 2) is *not* an ASCII digit → 0 (item 0), while
      Python's ``\\d`` matched it and addressed row 2;
    * a very long number (5000 digits) overflows a double to ``Inf`` →
      out of range (``count 0``, no ``item_loop``), while Python ``int()``
      raised and turned the request into JSON-RPC ``-32603``.
    """
    m = _PLAYCTL_NUM_RE.match(str(value))
    if not m:
        return 0
    text = m.group(1)
    try:
        # Perl keeps plain integer tokens exact (IV/UV). Python's only limit
        # is the int/str digit cap (~4300) — beyond that (and for any
        # exponent form, which is not an int literal) fall through to the
        # double path below.
        return int(text)
    except ValueError:
        pass
    try:
        num = float(text)
    except ValueError:  # cannot happen for the matched ASCII form
        return 0
    if num == float("inf"):
        return _PLAYCTL_INF
    if num == float("-inf"):
        return -_PLAYCTL_INF
    return int(num)  # int use truncates toward zero, like Perl


def _genre_id_to_text(genre_id) -> str:
    """Resolve a browselibrary genre_id (index into the DISTINCT track-genre
    list, stable order) to the genre text — the genres table is empty."""
    try:
        if str(genre_id).isdigit():
            rows = _db_query("SELECT DISTINCT genre FROM tracks "
                             "WHERE genre != '' ORDER BY genre COLLATE "
                             "NOCASE LIMIT 1 OFFSET ?", (int(genre_id),))
            return rows[0]["genre"] if rows else ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _expand_track_ids(tagged: dict) -> list[int]:
    """Resolve album_id/artist_id/year/genre_id/folder_id filter tokens to
    the full ordered track-id list (SqueezePlay 'playlistcontrol' +
    playlist play/add expansion, Perl playlist control parity)."""
    where: list[str] = []
    params: list = []
    if tagged.get("album_id"):
        where.append("t.id IN (SELECT track FROM tracks_albums WHERE album = ?)")
        params.append(int(tagged["album_id"]))
    elif tagged.get("artist_id"):
        where.append("t.id IN (SELECT track FROM tracks_contributors "
                     "WHERE contributor = ? AND role = 1)")
        params.append(int(tagged["artist_id"]))
    if tagged.get("year"):
        where.append("t.year = ?")
        params.append(int(tagged["year"]))
    if tagged.get("genre_id"):
        genre_text = _genre_id_to_text(tagged["genre_id"])
        if genre_text:
            where.append("t.genre = ?")
            params.append(genre_text)
    if tagged.get("folder_id"):
        # „Musikordner“ (mode:bmf): every track below that directory —
        # Perl's folderId expansion in playlistControl.
        where.append("t.url LIKE ?")
        params.append(_bmf_encoded_prefix(tagged["folder_id"]) + "%")
    if not where:
        return []
    sql = ("SELECT t.id FROM tracks t WHERE " + " AND ".join(where)
           + " ORDER BY t.tracknum, t.title")
    return [int(r["id"]) for r in _db_query(sql, tuple(params))]


# ---------------------------------------------------------------------------
# „Musikordner“ (mode:bmf) — the folder tree, aggregated from the track URLs
# (never from a filesystem walk: the library lives on an SMB share with
# 100k+ files, so the directory levels are derived from tracks.url).
# ---------------------------------------------------------------------------


def _bmf_musicdir_pref() -> str:
    """The runtime ``musicdir`` preference ('' when unset).

    Read through lyrion.config's runtime preference store — the same source
    the importer/rescan uses — NOT the library DB: the prefs table lives in
    prefs.db, lyrion.db has none. Tests monkeypatch this function.
    """
    try:
        from lyrion.config import get_config
        return str(get_config().get("musicdir", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _bmf_path(value: str) -> str:
    """Decoded absolute path for a ``file://`` URI or plain path token.

    Track URLs are percent-encoded the way the importer stores them
    (``Path.as_uri()``: ``smb-share%3Aserver%3D…``, ``%20`` for spaces,
    ``%2B`` for '+'), while the ``musicdir`` pref and the drill tokens are
    plain paths — so every comparison decodes first. Exactly this
    encoding mismatch, plus taking the first URL component as a folder
    name, produced the bogus ``home``/``run`` top level.
    """
    s = str(value or "").strip()
    if s[:7].lower() == "file://":
        s = s[7:]
    if "%" in s:
        try:
            s = urllib.parse.unquote(s)
        except Exception:  # noqa: BLE001
            pass
    s = s.replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    if s and not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


def _bmf_encoded_prefix(directory: str) -> str:
    """``file://``-prefixed, percent-encoded ``directory`` + '/' (LIKE bound)."""
    return "file://" + urllib.parse.quote(_bmf_path(directory), safe="/") + "/"


def _bmf_count_under(root: str) -> int:
    """Tracks whose URL lives below ``root`` (0 on any error)."""
    try:
        rows = _db_query("SELECT COUNT(*) AS n FROM tracks WHERE url LIKE ?",
                         (_bmf_encoded_prefix(root) + "%",))
        return int(list(rows[0].values())[0]) if rows else 0
    except Exception:  # noqa: BLE001
        return 0


def _bmf_music_root() -> str:
    """Browse root of „Musikordner“.

    Preferred source is the runtime ``musicdir`` preference. Without it the
    longest common directory root of the ``file://`` track URLs is used —
    but only when it really holds more than one track and is not the
    filesystem root: a few tracks outside the library (e.g. three test
    files under ``~/Music``) must not drag the browse root up to ``/``.
    """
    pref = _bmf_path(_bmf_musicdir_pref())
    if pref:
        return pref
    dirs: list[str] = []
    try:
        for row in _db_query("SELECT DISTINCT url FROM tracks "
                             "WHERE url LIKE 'file://%'"):
            d = posixpath.dirname(_bmf_path(row.get("url") or ""))
            if d and d != "/":
                dirs.append(d)
    except Exception:  # noqa: BLE001
        return ""
    if not dirs:
        return ""
    root = posixpath.commonpath(dirs) if len(dirs) > 1 else dirs[0]
    if root in ("", "/"):
        # Unrelated trees side by side (library + strays): keep the tree
        # holding the most tracks and use its own common root.
        groups: dict[str, list[str]] = {}
        for d in dirs:
            parts = d.split("/")
            groups.setdefault(parts[1] if len(parts) > 1 else d, []).append(d)
        main = max(groups.values(), key=len)
        root = posixpath.commonpath(main) if len(main) > 1 else main[0]
    if root in ("", "/"):
        return ""
    # "nur wenn sie mehr als einen Track umfasst": a single track must not
    # define a browse root.
    return root if _bmf_count_under(root) > 1 else ""


def _bmf_resolve_dir(token: str, root: str) -> str:
    """Directory a ``mode:bmf`` drill token (folder_id/search/url) points at.

    Accepts an absolute path below ``root``, a ``file://`` URI or a path
    relative to ``root``; anything else falls back to ``root`` (browse the
    top level instead of leaking a foreign directory).
    """
    p = _bmf_path(token)
    if not p or p == "/":
        return root
    if p == root or p.startswith(root + "/"):
        return p
    cand = _bmf_path(root + "/" + p.lstrip("/"))
    if cand == root or cand.startswith(root + "/"):
        return cand
    return root


def _bmf_children(directory: str, start: int = 0,
                  count: int = 200) -> tuple[list, int]:
    """Children of ``directory``, aggregated from the track URLs.

    Returns ``(rows, total)``: ``type 'folder'`` subdirectories (``id`` =
    absolute path, ``name`` = decoded folder name) followed by ``type
    'audio'`` files directly in the directory (``id`` = track id) — Perl's
    bmf lists files there too. Everything comes from one prefix ``LIKE``
    over ``tracks.url`` (``DISTINCT``/``GROUP BY`` on the path segment);
    the filesystem is never touched.
    """
    prefix = _bmf_encoded_prefix(directory)
    like = prefix + "%"
    off = len(prefix) + 1          # 1-based index behind "<dir>/"
    try:
        rows = _db_query(
            "SELECT seg, COUNT(*) AS n FROM ("
            " SELECT CASE WHEN instr(substr(url, ?), '/') > 0"
            "             THEN substr(url, ?, instr(substr(url, ?), '/') - 1)"
            "             ELSE NULL END AS seg"
            " FROM tracks WHERE url LIKE ?"
            ") WHERE seg IS NOT NULL AND seg != ''"
            " GROUP BY seg ORDER BY seg COLLATE NOCASE",
            (off, off, off, like))
    except Exception:  # noqa: BLE001
        rows = []
    folders: list[tuple[str, str]] = []
    for r in rows:
        name = urllib.parse.unquote(str(r["seg"]))
        path = _bmf_path(directory + "/" + name)
        if path == directory or not path.startswith(directory + "/"):
            continue               # encoding drift / foreign tree → drop
        folders.append((name, path))
    folders.sort(key=lambda t: t[0].casefold())
    try:
        cnt = _db_query("SELECT COUNT(*) AS n FROM tracks WHERE url LIKE ?"
                        " AND instr(substr(url, ?), '/') = 0", (like, off))
        loose_total = int(list(cnt[0].values())[0]) if cnt else 0
    except Exception:  # noqa: BLE001
        loose_total = 0
    total = len(folders) + loose_total
    # Folders first, then the loose files of this directory (Perl returns
    # both in one list).
    out: list[dict] = [{"id": path, "name": name, "title": name,
                        "type": "folder"}
                       for name, path in folders[start:start + count]]
    left = count - len(out)
    if left > 0:
        t_off = 0 if start < len(folders) else start - len(folders)
        trows: list = []
        try:
            trows = _db_query(
                "SELECT t.id, t.url FROM tracks t WHERE t.url LIKE ?"
                " AND instr(substr(t.url, ?), '/') = 0"
                " ORDER BY t.url COLLATE NOCASE, t.id LIMIT ? OFFSET ?",
                (like, off, left, t_off))
        except Exception:  # noqa: BLE001
            trows = []
        for r in trows:
            # Perl's bmf shows the decoded file name for files (tags are
            # what the album/year views are for).
            name = posixpath.basename(_bmf_path(r.get("url") or ""))
            out.append({"id": r["id"], "name": name, "title": name,
                        "type": "audio"})
    return out, total


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
        self.register("abortscan", self._abortscan)

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
                              parent_path: str, feed_mode: bool,
                              menu_mode: bool = False) -> list[dict]:
        """Build the favorites loop with LMS hierarchical ids.

        Two shapes, mirroring Perl's ``XMLBrowser::_cliQuery_done``:

        * ``menu_mode=True`` (the request carried a ``menu:`` token, i.e.
          SqueezePlay/Android browse menus): the jive menu item shape —
          ``text``/``addAction``/``icon-id``/``actions`` for folders and
          ``text``/``type``/``style``/``goAction``/``icon-id``/``params``/
          ``presetParams`` for stations (``XMLBrowser.pm:1051-1267``).
          See :mod:`lyrion.web.favorites_menu`.
        * ``menu_mode=False``: the classic flat shape (``id``/``name``/
          ``image``/``isaudio``/``hasitems``, ``XMLBrowser.pm:1378-1410``)
          other controllers (SqueezeTray, Squeezer, SPA) read.

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
            hier = path + f".{i}"
            if menu_mode:
                from lyrion.web import favorites_menu
                if is_folder:
                    item = favorites_menu.folder_item(it["title"], hier)
                else:
                    item = favorites_menu.audio_item(it["title"], hier,
                                                     url=it["url"] or "")
            else:
                # LMS reference format (lyrion.org): hierarchical id
                # '<root>.<position>' (the apps re-send it as item_id:),
                # name/image/isaudio/hasitems, type 'audio' for streams.
                item = {
                    "id": hier,
                    "name": it["title"],
                    "image": "html/images/favorites.png",
                    "isaudio": 0 if is_folder else 1,
                    "hasitems": 1 if is_folder else 0,
                    "position": i,
                }
                if not is_folder:
                    item["type"] = "audio"
                    item["url"] = it["url"] or ""
                    item["id_hierarchical"] = hier
                    item["dbid"] = str(it["id"])
                if is_folder:
                    # Folder: go opens the folder's items (hierarchical id).
                    item["actions"] = {
                        "go": {"player": 0, "cmd": ["favorites", "items"],
                               "params": {"item_id": hier}},
                    }
                else:
                    # Stream: play/do plays the favorite.
                    item["actions"] = {
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"item_id": hier}},
                        "do": {"player": 0, "cmd": ["playlist", "play"],
                               "params": {"item_id": hier}},
                    }
            if feed_mode and is_folder:
                item["items"] = await self._fav_items_loop(
                    fm, int(it["id"]), hier, feed_mode, menu_mode)
            loop.append(item)
        return loop


    async def _json_favorites_items(self, pid: str | None,
                                    rest: list) -> dict:
        """``['favorites','items',<start>,<qty>,…]`` — see favorites_menu.

        Two answer shapes, exactly like Perl's ``XMLBrowser::_cliQuery_done``:

        * a ``menu:`` token (SqueezePlay/Android browse menus) ⇒ the jive
          menu shape with ``base``/``window`` — including
          ``base.actions.play.nextWindow == "nowPlaying"``, which is what
          makes SqueezePlay switch to "Aktueller Titel" after a tap
          (SlimBrowserApplet.lua:1924-1928, 2044-2048).
        * no ``menu:`` token ⇒ the classic ``loop_loop`` shape other
          controllers read (unchanged).

        ``item_id:`` accepts the hierarchical id ('0.3.1') and a bare DB
        id; a bare numeric first argument (Web UI) is a folder id.
        """
        try:
            from lyrion.music.favorites import get_favorites_manager
            fm = get_favorites_manager()
            parent = None
            parent_path = "0"
            menu_mode = any(str(a) == "menu" or str(a).startswith("menu:")
                            for a in rest)
            feed_mode = any(str(a).startswith("feedMode:")
                            and str(a)[9:] == "1" for a in rest)
            # Perl answers feedMode with a nested outline feed
            # ({"favorites":…,"items":…,"type":…}) — neither shape is parity,
            # so leave that path exactly as it was before.
            if feed_mode:
                menu_mode = False
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
            loop = await self._fav_items_loop(fm, parent, parent_path,
                                              feed_mode, menu_mode)
            if menu_mode:
                from lyrion.web import favorites_menu
                # Perl echoes the request's tagged params into the
                # playControl base action (XMLBrowser.pm:978).  The two
                # leading positionals are the named slots of
                # ``['favorites','items','_index','_quantity']``
                # (Request.pm:75 / Favorites/Plugin.pm:75).
                tagged: dict[str, Any] = {}
                positional = [str(a) for a in rest[:2]]
                if len(positional) == 2 and all(p.isdigit()
                                                for p in positional):
                    tagged["_index"], tagged["_quantity"] = positional
                for a in rest:
                    s = str(a)
                    if ":" in s:
                        k, _, v = s.partition(":")
                        tagged[k] = v
                return favorites_menu.render_favorites_menu(
                    loop, playcontrol_params=tagged)
            resp = self._browse_response(loop)
            # LMS reference: 'title' on the response level.
            resp["title"] = "Favorites"
            return resp
        except Exception:
            from lyrion.web import favorites_menu
            if any(str(a) == "menu" or str(a).startswith("menu:")
                   for a in (rest or [])):
                return favorites_menu.render_favorites_menu([])
            resp = self._browse_response([])
            resp["title"] = "Favorites"
            return resp

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
                   "playlist", "playlistcontrol"):
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
            return await self._json_favorites_items(pid, args[1:])

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

        # ── Saved playlists (Perl playlistsQuery / Commands.pm parity) ──
        if cmd == "playlists":
            return await self._json_playlists(pid, args)

        # ── Browse commands (library) ──────────────────────────────
        if cmd in ("albums", "artists", "genres", "songs", "titles",
                   "musicfolder", "radios", "songinfo",
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

        # ── contextmenu (SqueezePlay press-and-hold context menus) ─────
        # Perl parity: a *wrapper* around '<menu>info items <index> <qty>
        # <params>' (Slim/Control/Queries.pm:6171 contextMenuQuery). Without
        # this the modal window opened empty (black dialog, only X).
        # See lyrion/web/contextmenu.py.
        if cmd == "contextmenu":
            from lyrion.web.contextmenu import handle_contextmenu
            return await handle_contextmenu(self, pm, pid, args)

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

    async def _rescan(self, mode: str = "full") -> Any:
        """Direct rescan — triggers MusicImporter in background.

        ``mode`` mirrors the LMS rescan modes (Commands.pm rescanCommand):
        the default full rescan reconciles deletions; other modes are
        additive refreshes that never delete.
        """
        import asyncio as _asyncio
        from pathlib import Path as _Path

        mode = (str(mode) or "full").strip().lower() or "full"

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
            importer = MusicImporter(ImportConfig(source_path=_Path(musicdir),
                                                  mode=mode))
            stats = await importer.import_music()
            return stats

        _asyncio.create_task(_do())
        return {"status": "rescan started", "mode": mode}

    async def _abortscan(self) -> Any:
        """Direct abortscan — stops the running library scan (Perl parity)."""
        from lyrion.media.scan_state import SCAN_STATE
        SCAN_STATE.request_abort()
        return {"status": "abort requested"}

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

        # Track metadata from DB for the playlist loop (local tracks only).
        # A playlist entry is local when it is an int — or a digit-only
        # string (CLI/plugin paths hand us "51994"; a bare number is never
        # a stream URL, so the coercion is safe). Without it a numeric
        # string was mistaken for a radio URL: title = "51994", duration 0,
        # no artwork, no params.track_id — the exact SqueezePlay symptoms
        # (progress bar at maximum, no title switch).
        def _local_id(entry: object) -> int | None:
            """Return the DB track id for a local playlist entry, else None."""
            if isinstance(entry, bool):
                return None
            if isinstance(entry, int):
                return entry
            text = str(entry)
            return int(text) if text.isdigit() else None

        loop = []
        playlist_ids = getattr(player, "playlist", []) or []
        int_ids = [x for x in (_local_id(e) for e in playlist_ids) if x is not None]
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
        if _cur_tid is not None and _local_id(_cur_tid) is None and " - " in _cur_title:
            _artist, _track = _cur_title.split(" - ", 1)
            _cur_artist = _artist.strip()
            _cur_track = _track.strip()

        for i, tid in enumerate(playlist_ids):
            tid_local = _local_id(tid)
            if tid_local is not None:
                info = track_rows.get(tid_local, {})
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
            item: dict = {"id": tid_local if tid_local is not None else tid,
                          "playlist index": i}
            # title/trackType are always present (Orange Squeeze does
            # firstItem.get("trackType").asText() — a missing field is a
            # NULL NPE crash).
            item["title"] = title
            item["text"] = title          # SqueezePlay _extractTrackInfo fallback
            item["trackType"] = "local" if tid_local is not None else "remote"
            # SqueezePlay now-playing reads _track.track/.artist/.album —
            # provide them for LOCAL tracks (a remote stream intentionally
            # omits `track` so SqueezePlay falls back to text + current_title).
            if tid_local is not None:
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
                # Artwork so the Now-Playing shows a placeholder for
                # cover-less streams (Perl provides artwork_url). Prefer a
                # stored station logo, else the heart placeholder.
                _simg = getattr(player, "stream_images", {}).get(str(tid), "") or ""
                item["artwork_url"] = _simg or "/html/images/favorites.png"
                item["coverart"] = 1
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
            # Jive item params (Perl _addJiveSong, Queries.pm:5637-5641):
            # SqueezePlay's Now-Playing reads params.track_id
            # (Player.lua:269-282, NowPlayingApplet.lua:117) — without it the
            # "current title" screen never follows the playing track.
            params: dict = {"playlist_index": i}
            if tid_local is not None:
                params["track_id"] = tid_local
                if info.get("album_id"):
                    params["album_id"] = info["album_id"]
                if info.get("artist_id"):
                    params["artist_id"] = info["artist_id"]
            else:
                # Perl: 'track_id' => ($songData->{'id'} + 0) — a URL
                # stringifies to 0, so a remote item carries track_id 0.
                params["track_id"] = 0
            item["params"] = params
            # Perl marks every playable jive item as style 'itemplay'.
            item["style"] = "itemplay"
            # Artwork (Perl _addJiveSong, Queries.pm:5619-5629):
            # artwork_url -> 'icon', else coverid/artwork_track_id ->
            # 'icon-id', else the radio placeholder for a cover-less remote
            # item. SqueezePlay reads both (Player.lua:282).
            _art = item.get("artwork_url") or info.get("artwork_url") or ""
            if _art:
                item["icon"] = _art
            elif info.get("cover"):
                # Jive builds '/music/' .. iconId .. '/cover' .. size from
                # `icon-id`/`icon` (SlimServer.lua:1189) — a URL here would
                # be concatenated into garbage and the cover never loads.
                item["icon-id"] = str(info["cover"])
                item["icon"] = f"music/{info['cover']}/cover"
            elif tid_local is None:
                item["icon-id"] = "/html/images/favorites.png"
            loop.append(item)

        # A negative/absent position must never index the list (-1 would hit
        # the LAST entry, an empty playlist raises IndexError). A stopped
        # player with an empty queue is the normal case after the client
        # starts — it must still answer with a valid status.
        cur = player.playlist_position if player.playlist_position is not None else 0
        cur_valid = 0 <= cur < len(playlist_ids)
        cur_local = _local_id(playlist_ids[cur]) if cur_valid else None
        if cur_local is not None:
            cur_info = track_rows.get(cur_local, {})
        else:
            cur_info = {}
        if cur_valid and cur_local is None:
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
        # Only a remote stream's StreamTitle may override the now-playing
        # line — a local track's title comes from the DB row (a stale radio
        # StreamTitle must not linger over local tracks).
        if (getattr(player, "current_title", "")
                and cur_valid
                and cur_local is None):
            cur_info["title"] = player.current_title

        # Playback progress. Perl's Slim::Player::Source::songTime() keeps
        # the position while paused (StreamingController::playingSongElapsed
        # returns resumeTime) and reports 0 (int) when stopped
        # (Squeezebox2::songElapsedSeconds returns 0 if isStopped, else
        # elapsedMilliseconds/1000 — a fractional value). SqueezePlay must
        # not reset the progress bar on pause.
        elapsed = float(getattr(player, "elapsed", 0) or 0)
        if player.mode == "stop":
            elapsed = 0

        # remoteMeta: SqueezeClient / ioBroker expect the live-stream
        # metadata block for remote streams (radio).
        remote_meta = {}
        cur_url = getattr(player, "current_url", None)
        if cur_url and cur_local is None:
            # Station logo if one is stored (playlist add image:<path>),
            # else a default placeholder (Perl uses html/images/favorites.png
            # — the "heart" SqueezePlay draws for cover-less streams).
            _stream_img = getattr(player, "stream_images", {}).get(cur_url, "") or ""
            if _stream_img.startswith("html/"):
                _stream_img = _stream_img[len("html/"):]
            remote_meta = {
                "title": cur_info.get("title", ""),
                "artist": cur_info.get("artist", ""),
                "album": cur_info.get("album", ""),
                "duration": cur_info.get("duration", 0) or 0,
                "url": cur_url,
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

        # Perl returns the requested window, and its FIRST item is the
        # playing track for `status - <qty>`: SqueezePlay's _whatsPlaying
        # reads item_loop[1], Material's server.js:606 / SqueezeJS Base.js:389
        # read playlist_loop[0] as the current song, and the Material queue
        # pages through `status <start> <count>` (lmsList). Each item keeps
        # its absolute "playlist index" so the client can map window → queue.
        # (Perl: normalize($index, $quantity, $songCount), Queries.pm:4425.)
        win_start, win_end = self._status_window(args, int(cur), len(playlist_ids))
        item_loop = loop[win_start:win_end + 1] if win_start <= win_end else []

        result: dict = {
            "mode": player.mode,
            "power": 1 if player.power else 0,
            "player_name": player.name or player.mac,
            # SqueezeClient's PlayerStatusResponse requires this
            "player_connected": 1,
            # SqueezePlay reads playerStatus.remote == 1 to append the live
            # current_title for a radio stream.
            "remote": 1 if cur_local is None else 0,
            # Perl Queries.pm:4186-4190 sends these as INTEGERS
            # (`$repeat += 0; $shuffle += 0`) — live probe (docs/
            # status-parity-analysis.md, gap-analysis CTRL-03) confirms
            # `"playlist shuffle": 0` / `"playlist repeat": 0` as ints.
            "playlist shuffle": int(getattr(player, "shuffle", 0) or 0),
            "playlist repeat": int(getattr(player, "repeat", 0) or 0),
            "mixer volume": player.volume or 50,
            "playlist_tracks": len(playlist_ids),
            # Perl sends this as a STRING (`"playlist_cur_index": "0"`,
            # Queries.pm:4208 + live probe gap-analysis CTRL-04);
            # SqueezePlay does tonumber(event.data.playlist_cur_index).
            "playlist_cur_index": str(int(cur)),
            "time": elapsed,
            "rate": 1 if player.mode == "play" else 0,
            # Perl parity: 'playlist mode' mirrors the repeat state
            # (off/repeat/repeat-one), randomplay mirrors shuffle.
            "playlist mode": ("off", "repeat", "repeat-one")[min(2, int(getattr(player, "repeat", 0) or 0))],
            "randomplay": int(getattr(player, "shuffle", 0) or 0),
            "digital_volume_control": 1,
            "use_volume_control": 1,
            "signalstrength": 0,
            "seq_no": int(getattr(player, "_seq_no", 0) or 0),
            "playlist_timestamp": time.time(),
            "playlist_loop": item_loop,
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
        result["count"] = len(item_loop)
        result["offset"] = str(win_start)
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
            # Duration: for a LOCAL track the real DB length (Perl
            # `$song->duration()`, Queries.pm:4101) — never 0 and never the
            # elapsed position (that pinned SqueezePlay's progress bar at
            # maximum and froze the remaining-time display). For a live
            # stream keep the non-zero rule: leaving it out (null) makes
            # SqueezePlay's status processing choke and prevents the
            # Now-Playing window from opening, while a 0 makes
            # SqueezeClient's seek-slider crash (valueTo=0.1 vs growing
            # position). Using the position keeps valueTo >= value for
            # SqueezeClient while still giving SqueezePlay a duration.
            dur = float(cur_info.get("duration", 0) or 0)
            pos = float(elapsed or 0)
            if dur <= 0 and cur_local is None and pos > 0:
                dur = pos
            if dur <= 0:
                dur = 1.0
            result["duration"] = dur
            # Never emit an EMPTY item_loop: Jive's `_whatsPlaying`
            # (share/jive/jive/slim/Player.lua:272-273) does
            # `if obj.item_loop then obj.item_loop[1].params ...` — with an
            # empty array `item_loop[1]` is nil and Lua raises
            # "attempt to index field '?' (a nil value)", which aborts the
            # artwork/now-playing sink (seen live as a missing cover and
            # `RequestHttp.lua:71 Response sink` error). Perl omits the key
            # in that case.
            if item_loop:
                result["item_loop"] = item_loop
        result |= sync_fields
        if remote_meta:
            result["remoteMeta"] = remote_meta
        if menu_block:
            result["menu"] = menu_block
        return result

    @staticmethod
    def _status_window(args: list[str] | None, cur: int, total: int) -> tuple[int, int]:
        """Perl ``Slim::Control::Request::normalize`` for the status loop.

        ``status - <qty>`` (index ``-``) starts the window at the playing
        track (Queries.pm:4425 ``normalize($playlist_cur_index, $quantity,
        $songCount)``); ``status <n> <qty>`` starts at ``n``. Returns an
        inclusive ``(start, end)`` pair; ``start > end`` means "no items".
        """
        tokens = [str(a) for a in (args or [])]
        index_tok = tokens[0] if tokens else "-"
        qty = 0
        if len(tokens) > 1 and tokens[1].lstrip("-").isdigit():
            qty = int(tokens[1])
        if index_tok == "-":
            start = int(cur)
        elif index_tok.lstrip("-").isdigit():
            start = int(index_tok)
        else:
            start = 0
        if qty <= 0:
            qty = int(total)          # Perl: $numofitems = $count when undef
        if not total:
            return 0, -1
        last = int(total) - 1
        if start > last:
            return 0, -1
        if start < 0:
            start = 0
        return start, min(start + qty - 1, last)

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
        """['alarm', '<idx>'] / ['alarm', '<idx>', '<k:v>…'] / Perl cmds.

        Perl alarmCommand forms (Commands.pm:55, used by Material):
          ['alarm', 'add',    'time:HHMM', 'dow:0,2', 'url:<stream>']
          ['alarm', 'update', 'id:<idx>',  'k:v'…]
          ['alarm', 'delete', 'id:<idx>']
          ['alarm', 'enableall' | 'disableall']
        plus the index form (own SPA/SqueezePlay) incl. a bare 'delete'.
        """
        from lyrion.alarms import AlarmManager, _alarm_from_parts, Alarm

        mac = pid if pid not in ("-", None) else ""
        mgr = AlarmManager()
        first = str(args[0]) if args else ""

        if first in ("add", "update", "delete", "enableall", "disableall",
                     "defaultvolume"):
            return await self._alarm_command(mac, first, args[1:])

        # ── index form ────────────────────────────────────────────────
        # Some clients pass '<idx>' as '<idx>-' (row selection).
        idx_str = first.split("-")[0]
        try:
            idx = int(idx_str)
        except ValueError:
            return {"error": "invalid alarm index"}

        if len(args) == 1 or (len(args) >= 2 and args[1] in ("?", "query")):
            return self._alarm_to_item(idx, mgr.get(mac, idx))

        # Own web UI delete: ['alarm', '<idx>', 'delete']
        if args[1] in ("delete", "delete:1", "del"):
            mgr.delete(mac, idx)
            return {"deleted": idx, "id": idx}

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
                    if f == "days" and ("day" in parts or "dow" in parts
                                        or "dowAdd" in parts or "dowDel" in parts):
                        continue
                    # 'url:'/'track:' build the wake value; don't clobber it.
                    if f == "wake" and ("url" in parts or "track" in parts):
                        continue
                    setattr(a, f, getattr(current, f))
        mgr.set(mac, idx, a)
        return self._alarm_to_item(idx, a)

    async def _alarm_command(self, mac: str, cmd: str, toks: list) -> dict:
        """Perl alarmCommand: add/update/delete/enableall/disableall."""
        from lyrion.alarms import AlarmManager, _alarm_from_parts

        mgr = AlarmManager()
        parts: dict[str, str] = {}
        for tok in toks:
            if ":" in tok:
                k, v = tok.split(":", 1)
                parts[k] = v

        if cmd == "delete":
            raw = parts.get("id")
            if raw is None:
                return {"error": "alarm delete needs id:<index>"}
            try:
                idx = int(str(raw).split("-")[0])
            except ValueError:
                return {"error": "invalid alarm index"}
            mgr.delete(mac, idx)
            return {"deleted": idx, "id": idx}

        if cmd == "enableall":
            for i in list(mgr.alarms_for(mac)):
                a = mgr.get(mac, i)
                if a:
                    a.enabled = True
                    mgr.set(mac, i, a)
            return {"count": len(mgr.alarms_for(mac))}

        if cmd == "disableall":
            for i in list(mgr.alarms_for(mac)):
                a = mgr.get(mac, i)
                if a:
                    a.enabled = False
                    mgr.set(mac, i, a)
            return {"count": len(mgr.alarms_for(mac))}

        if cmd == "add":
            existing = mgr.alarms_for(mac)
            idx = max(existing) + 1 if existing else 0
            a = _alarm_from_parts(idx, parts)
            mgr.set(mac, idx, a)
            return self._alarm_to_item(idx, a)

        # update — Perl requires id:<idx>; merge like the index set-form.
        raw = parts.get("id")
        if raw is None:
            return {"error": "alarm update needs id:<index>"}
        try:
            idx = int(str(raw).split("-")[0])
        except ValueError:
            return {"error": "invalid alarm index"}
        current = mgr.get(mac, idx)
        a = _alarm_from_parts(idx, parts)
        if current:
            for f in ("enabled", "days", "time", "volume", "fade", "duration",
                      "repeat", "wake"):
                if f not in parts:
                    if f == "time" and ("hour" in parts or "minute" in parts):
                        continue
                    if f == "days" and ("day" in parts or "dow" in parts
                                        or "dowAdd" in parts or "dowDel" in parts):
                        continue
                    if f == "wake" and ("url" in parts or "track" in parts):
                        continue
                    setattr(a, f, getattr(current, f))
        mgr.set(mac, idx, a)
        return self._alarm_to_item(idx, a)

    # ─────────────────────────────────────────────────────────────
    # Saved playlists (Perl: playlistsQuery → 'playlists_loop' with
    # id + playlist, playlists tracks → 'playlist_tracks_loop')
    # ─────────────────────────────────────────────────────────────

    async def _json_playlists(self, pid: str | None, args: list) -> dict:
        """['playlists', <start>, <count>, 'tags:…'] and the subcommands
        'new' / 'rename' / 'delete' / 'tracks' (Slim/Control/Commands.pm
        playlistsNew/Rename/DeleteCommand + playlistsQuery parity)."""
        if args and str(args[0]).lower() in ("new", "rename", "delete",
                                             "tracks"):
            sub = str(args[0]).lower()
            if sub == "tracks" and len(args) >= 2 and str(args[1]).isdigit():
                return await self._json_playlist_tracks(int(args[1]))
            return await self._json_playlist_mutation(sub, args[1:])

        nums = [int(s) for s in args if str(s).isdigit()]
        start = nums[0] if nums else 0
        count = nums[1] if len(nums) > 1 else 10
        tags = next((str(a)[5:] for a in args if str(a).startswith("tags:")), "")
        import sqlite3
        db = sqlite3.connect(f"file:{_library_db_path()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT id, playlist, name, remote FROM playlists "
                "ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                (count, start)).fetchall()
            total = db.execute(
                "SELECT COUNT(*) AS n FROM playlists").fetchone()["n"]
        finally:
            db.close()
        loop = []
        for r in rows:
            item = {"id": int(r["id"]),
                    "playlist": (r["name"] or r["playlist"] or "")}
            if "x" in tags:
                item["remote"] = int(r["remote"] or 0)
            loop.append(item)
        return {"count": int(total or 0), "playlists_loop": loop}

    async def _json_playlist_tracks(self, pid: int) -> dict:
        """['playlists', 'tracks', <id>] → playlist_tracks_loop."""
        import sqlite3
        db = sqlite3.connect(f"file:{_library_db_path()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT pi.position, pi.track, pi.url, pi.title, "
                "t.title AS ttitle, t.duration AS tduration "
                "FROM playlist_items pi "
                "LEFT JOIN tracks t ON t.id = pi.track "
                "WHERE pi.playlist = ? ORDER BY pi.position",
                (pid,)).fetchall()
        finally:
            db.close()
        loop = []
        for r in rows:
            title = r["title"] or r["ttitle"]
            item: dict = {}
            if r["track"] is not None:
                item["id"] = int(r["track"])
            else:
                item["id"] = r["url"] or ""
            if title:
                item["title"] = title
            if r["url"]:
                item["url"] = r["url"]
            if r["tduration"]:
                item["duration"] = int(r["tduration"] or 0)
            loop.append(item)
        return {"count": len(loop), "playlist_tracks_loop": loop}

    async def _json_playlist_mutation(self, sub: str, toks: list) -> dict:
        """new/rename/delete on saved playlists (Perl Commands.pm)."""
        parts: dict[str, str] = {}
        for tok in toks:
            if ":" in tok:
                k, v = tok.split(":", 1)
                parts[k] = v
        import sqlite3
        db = sqlite3.connect(_library_db_path(), timeout=30)
        try:
            if sub == "new":
                name = parts.get("name", "").strip()
                if not name:
                    return {"error": "playlists new needs name:<title>"}
                try:
                    cur = db.execute(
                        "INSERT INTO playlists (playlist, name, changed, "
                        "pl_type, remote, disabled) "
                        "VALUES (?, ?, datetime('now'), 0, 0, 0)",
                        (name, name))
                    db.commit()
                    return {"playlist_id": int(cur.lastrowid)}
                except sqlite3.IntegrityError:
                    db.rollback()
                    return {"error": "playlist already exists"}
            if sub == "delete":
                pid = int(parts.get("id", "-1").split("-")[0])
                if pid < 0:
                    return {"error": "playlists delete needs id:<n>"}
                db.execute("DELETE FROM playlist_items WHERE playlist = ?",
                           (pid,))
                db.execute("DELETE FROM playlists WHERE id = ?", (pid,))
                db.commit()
                return {"deleted": pid}
            if sub == "rename":
                pid = int(parts.get("id", "-1").split("-")[0])
                name = parts.get("name", "").strip()
                if pid < 0 or not name:
                    return {"error": "playlists rename needs id:<n> name:<x>"}
                db.execute(
                    "UPDATE playlists SET name = ?, playlist = ? WHERE id = ?",
                    (name, name, pid))
                db.commit()
                return {"renamed": pid, "name": name}
            return {"error": f"unknown playlists subcommand {sub}"}
        finally:
            db.close()

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
            player = pm.get_player(pid)
            if player is None:
                send(f"pause {args[0]}" if args else "pause")
            else:
                # Perl parity (Slim/Control/Commands.pm:731-753):
                #   explicit value -> truthy = 'pause', falsy = 'play'
                #   no value       -> toggle ('play' from pause/stop, else 'pause')
                #   'play' while paused becomes 'resume' (position kept),
                #   'play' while stopped starts the current playlist item.
                curmode = player.mode or "stop"
                if args:
                    raw = str(args[0])
                    try:
                        want = "pause" if float(raw) != 0 else "play"
                    except ValueError:
                        want = "pause" if raw else "play"
                else:
                    want = "play" if curmode in ("pause", "stop") else "pause"
                if want == "pause":
                    # only from 'play' (Perl: pause cannot pause a stopped player)
                    if curmode == "play":
                        await pm.pause_player(pid, True)
                else:
                    if curmode == "pause":
                        await pm.pause_player(pid, False)   # Perl's 'resume'
                    elif curmode == "stop":
                        player.power = True
                        pm.set_mode(pid, "play")
                        await self._play_playlist_item(
                            pm, player, player.playlist_position or 0
                        )
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
            # Only 'mixer volume <n>' touches the volume. bass/treble/pitch/
            # muting are separate mixer controls and must NOT be routed to
            # volume (Perl registers them independently in Slim/Control/Request.pm).
            if args and str(args[0]).lower() == "volume" and len(args) > 1:
                val = str(args[1])
                if val.isdigit():
                    player = pm.get_player(pid)
                    if player is not None:
                        player.volume = int(val)
                        # audg frame — text CLI does not exist on the
                        # SlimProto channel.
                        await pm.set_volume(pid, int(val))
                    else:
                        send(f"mixer volume {val}")
        elif cmd == "playlistcontrol":
            # SqueezePlay's My-Music play/add (base.actions → cmd
            # playlistcontrol cmd:load|add + the item's commonParams ids).
            # Route onto the playlist command with the same filter tokens.
            tokens = [str(a) for a in args]
            op = next((t.split(":", 1)[1] for t in tokens
                       if t.startswith("cmd:")), "load")
            sub = "play" if op == "load" else "add"
            rest = [t for t in tokens
                    if not t.startswith(("cmd:", "menu:", "useContextMenu"))]
            await self._json_control(pm, pid, "playlist", [sub] + rest)
        elif cmd == "playlist":
            sub = args[0] if args else ""
            rest = args[1:] if len(args) > 1 else []
            if sub == "add" and rest:
                # Accept a DB track id, a 'track_id:<n>' tag (controllers),
                # a plain URL, or album/artist/year/genre filters (SqueezePlay
                # playlistcontrol add) that expand to every matching track.
                player = pm.get_player(pid)
                if player is not None:
                    tagged = {}
                    for _a in rest:
                        _s = str(_a)
                        if ":" in _s:
                            _k, _, _v = _s.partition(":")
                            tagged[_k] = _v
                    if any(k in tagged for k in
                           ("album_id", "artist_id", "year", "genre_id",
                            "folder_id")):
                        try:
                            _ids = _expand_track_ids(tagged)
                            for _tid in _ids:
                                if _tid not in player.playlist:
                                    player.playlist.append(_tid)
                            player.playlist_total = len(player.playlist)
                            player.last_activity = time.time()
                            return
                        except Exception:  # noqa: BLE001
                            pass
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
                # LMS-compatible 'playlist play [<index>|track_id:<n>|item_id:<n>|album_id:<n>|artist_id:<n>|<url>]'
                player = pm.get_player(pid)
                if player is not None:
                    tagged = {}
                    for a in rest:
                        s = str(a)
                        if ":" in s:
                            k, _, v = s.partition(":")
                            tagged[k] = v
                    if "album_id" in tagged or "artist_id" in tagged \
                            or "year" in tagged or "genre_id" in tagged \
                            or "folder_id" in tagged:
                        # Expand to all matching tracks (one query).
                        try:
                            ids = _expand_track_ids(tagged)
                            if ids:
                                player.playlist = list(ids)
                                player.playlist_total = len(ids)
                                player.playlist_position = 0
                                await self._play_playlist_item(pm, player, 0)
                                return
                        except Exception:
                            pass
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
            elif sub == "shuffle":
                player = pm.get_player(pid)
                if player is not None and rest:
                    try:
                        player.shuffle = max(0, min(2, int(str(rest[0]))))
                    except ValueError:
                        pass
            elif sub == "repeat":
                player = pm.get_player(pid)
                if player is not None and rest:
                    try:
                        player.repeat = max(0, min(2, int(str(rest[0]))))
                    except ValueError:
                        pass
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
                player.remote = 0  # local track
            else:
                player.remote = 1  # live stream: never "track end"
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
        # „Musikordner“ drill token: SqueezePlay sends the tapped row's
        # item params back (Perl: folder_id; older shapes: url / item_id).
        # Accept all of them for bmf so a folder tap really descends;
        # a purely numeric item_id is a track id, not a folder path.
        if mode in ("bmf", "musicfolder") and not search:
            for _k in ("folder_id", "url", "item_id"):
                _tok = next((str(a)[len(_k) + 1:] for a in args
                             if str(a).startswith(_k + ":")
                             and str(a)[len(_k) + 1:].strip()), "")
                if _tok and not (_k == "item_id" and _tok.isdigit()):
                    search = _tok
                    break
        # Drill-down ids SqueezePlay merges from the parent item's
        # commonParams (album_id:45, artist_id:…, year:…, genre_id:…).
        filters: dict = {}
        for a in args:
            s = str(a)
            for key in ("album_id", "artist_id", "genre_id", "year"):
                if s.startswith(f"{key}:") and s[len(key) + 1:].strip():
                    filters[key] = s[len(key) + 1:].strip()
        # Play-Control context menu (MENU-02): a SqueezePlay tap on a row
        # sends `useContextMenu:1` + `xmlbrowserPlayControl:<itemIndex>`
        # (merged from the row's playControlParams). Perl answers with the
        # Add / Play-next / Play (+ Play-all) menu for that row whenever the
        # browser is in MENU mode and the xmlbrowserPlayControl token is
        # present — the value is used numerically, so an empty or
        # non-numeric token still taps item 0, and useContextMenu is not
        # required (Slim/Control/XMLBrowser.pm:805 `if ($menuMode &&
        # defined $xmlbrowserPlayControl)`).
        play_ctl = next((str(a)[22:] for a in args
                         if str(a).startswith("xmlbrowserPlayControl:")),
                        None)
        is_menu = any(str(a) == "menu:1" for a in args)
        try:
            rows, total, plural, kind = await self._library_rows(
                mode, start, count, search, filters or None)
        except Exception:  # noqa: BLE001
            return {"count": 0, "loop_loop": []}

        # menu:1 → SqueezePlay/Jive MENU window (Perl BrowseLibrary shape:
        # window.style + base.actions + text/type/commonParams items), NOT
        # the OpenSqueeze loop_loop. SqueezePlay's My-Music renders these
        # and drills via base.actions.go + each item's commonParams — a
        # numeric 'window' or a missing base made the list crash / taps
        # navigate nowhere ("Alben leer, keine Lieder").
        if is_menu:
            # Perl serves the tap menu for every audio feed (tracks,
            # albums, artists, genres, years — live probes); folder/search
            # items are not audio and keep the plain list.
            if play_ctl is not None and kind in ("tracks", "albums",
                                                 "artists", "genres", "years"):
                return self._playcontrol_context_menu(rows, start,
                                                      _playctl_index(play_ctl),
                                                      kind, filters)
            menu = self._browselibrary_menu_items(kind, rows, mode, search,
                                                  start)
            return {
                "base": {"actions": self._browselibrary_menu_actions(
                    kind, filters, start, count)},
                "count": int(total or len(menu)),
                "offset": start,
                "window": {"windowStyle": "icon_list"},
                "item_loop": menu,
            }

        loop = []
        for r in rows:
            if kind == "folder" and r.get("type") == "audio":
                # A file inside the browsed „Musikordner“ directory — Perl's
                # bmf lists files next to folders. It is a leaf, so the tap
                # plays the track instead of descending.
                loop.append({
                    "id": str(r["id"]),
                    "name": r["name"],
                    "text": r["name"],
                    "title": r["name"],
                    "type": "audio",
                    "isaudio": 1,
                    "hasitems": 0,
                    "actions": {
                        "go": {"player": 0, "cmd": ["songinfo"],
                               "params": {"track_id": r["id"]}},
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"track_id": r["id"]}},
                    },
                })
                continue
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
            elif kind == "search":
                ident, name = str(r["id"]), r.get("name") or ""
                go = ["songinfo"]
                go_params = {"track_id": r["id"]}
                play_params = {"track_id": r["id"]}
                icon = "html/images/search.png"
            elif kind == "folder":
                ident, name = str(r["id"]), r["name"]
                go = ["browselibrary", "items"]
                # 'search:' is this server's original drill token,
                # 'folder_id:' the Perl one — emit both, accept both.
                go_params = {"mode": "bmf", "search": ident,
                             "folder_id": ident}
                play_params = {"folder_id": ident}
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

    @staticmethod
    def _browselibrary_menu_items(kind: str, rows: list, mode: str,
                                  search: str = "", start: int = 0) -> list:
        """Build the SqueezePlay/Jive MENU shape for browselibrary items
        (request token menu:1) — Perl Slim::Menu::BrowseLibrary parity:
        text/type/commonParams instead of the OpenSqueeze loop_loop shape.
        SqueezePlay drills into an entry through commonParams.<id>, so a
        missing menu shape makes taps navigate nowhere ("no songs").

        ``start`` is the absolute index of the first row (paging), used for
        the track rows' ``xmlbrowserPlayControl`` / ``play_index``
        (Perl :1003 ``$itemIndex = $start - 1``)."""
        out: list[dict] = []
        ids = []
        for r in rows:
            if kind == "albums" and r.get("id") is not None:
                ids.append(r["id"])
        artist_line: dict = {}
        if kind == "albums" and ids:
            try:
                import sqlite3
                db = sqlite3.connect(
                    f"file:{_library_db_path()}?mode=ro", uri=True)
                db.row_factory = sqlite3.Row
                marks = ",".join("?" * len(ids))
                art = db.execute(
                    "SELECT ta.album AS aid, COUNT(DISTINCT c.id) AS n, "
                    "MIN(c.name) AS name FROM tracks_albums ta "
                    "JOIN tracks_contributors tc ON tc.track = ta.track "
                    "AND tc.role = 1 JOIN contributors c ON c.id = tc.contributor "
                    f"WHERE ta.album IN ({marks}) "
                    "GROUP BY ta.album", ids).fetchall()
                for row in art:
                    if row["n"] == 1:
                        artist_line[row["aid"]] = row["name"] or ""
                    elif row["n"] > 1:
                        artist_line[row["aid"]] = "Diverse Interpreten"
                db.close()
            except Exception:  # noqa: BLE001
                pass
        # Track rows carry presetParams (the preset/favorite base actions
        # read presetParams), which need the file URL. Looked up separately
        # and defensively: minimal/test DBs may expose tracks without url.
        track_urls: dict = {}
        if kind == "tracks":
            tids = [r["id"] for r in rows if r.get("id") is not None]
            if tids:
                try:
                    marks = ",".join("?" * len(tids))
                    for row in _db_query(
                            f"SELECT id, url FROM tracks WHERE id IN ({marks})",
                            tuple(tids)):
                        track_urls[row["id"]] = row["url"]
                except Exception:  # noqa: BLE001
                    # _db_query closes its connection in a finally block, so
                    # a missing `url` column cannot leak a sqlite handle.
                    pass
        for pos, r in enumerate(rows):
            item: dict = {"type": "playlist"}
            if kind == "albums":
                title = r["title"] or ""
                artist = artist_line.get(r["id"], "")
                item["text"] = f"{title}\n{artist}" if artist else title
                item["commonParams"] = {"album_id": str(r["id"])}
                if r.get("artwork"):
                    # Jive builds '/music/' .. iconId .. '/cover' .. size
                    # (SlimServer.lua:1189 fetchArtwork) from these fields —
                    # `icon-id` must be the id OUR /music/<id>/cover endpoint
                    # accepts (the album id), and `icon` stays in Perl's
                    # relative form. Without them SqueezePlay never requests
                    # artwork and every album row shows the generic disc.
                    item["icon-id"] = str(r["id"])
                    item["icon"] = f"music/{r['id']}/cover"
            elif kind == "artists":
                item["text"] = r["name"] or ""
                item["commonParams"] = {"artist_id": str(r["id"])}
                item["icon"] = "html/images/artists.png"
            elif kind == "genres":
                item["text"] = r["genre"] or ""
                item["commonParams"] = {"genre_id": str(r["id"])}
            elif kind == "years":
                item["text"] = str(r["year"])
                item["commonParams"] = {"year": int(r["year"])}
            elif kind == "tracks":
                # Album/artist drill target: one row per song. Perl gives
                # every audio row goAction=playControl + playControlParams so
                # a tap opens the play-control context menu for THAT row
                # instead of re-opening the list (XMLBrowser.pm:1270-1271).
                item["type"] = "audio"
                item["text"] = r["title"] or ""
                item["goAction"] = "playControl"
                item["playControlParams"] = {
                    "xmlbrowserPlayControl": str(start + pos)}
                item["playallParams"] = {"play_index": start + pos}
                item["commonParams"] = {"track_id": int(r["id"])}
                url = track_urls.get(r["id"])
                # Perl always ships presetParams on audio items (the
                # base.actions.set-preset-* entries read them); emit at
                # least the title/type so the item shape stays usable when
                # the library has no (or an empty) file URL.
                preset = {"favorites_type": "audio",
                          "favorites_title": r["title"] or ""}
                if url:
                    preset["favorites_url"] = str(url)
                item["presetParams"] = preset
            elif kind == "folder" and r.get("type") == "audio":
                # A file in the browsed folder (Perl bmf lists files too):
                # an audio leaf carrying the track, not a drill target.
                text = r["name"] or ""
                item["type"] = "audio"
                item["text"] = text
                item["textkey"] = text[:1].upper()
                item["commonParams"] = {"track_id": int(r["id"])}
                item["actions"] = {
                    "play": {"player": 0, "cmd": ["playlistcontrol"],
                             "params": {"cmd": "load", "menu": 1,
                                        "track_id": str(r["id"])},
                             "nextWindow": "nowPlaying"},
                    "add": {"player": 0, "cmd": ["playlistcontrol"],
                            "params": {"cmd": "add", "menu": 1,
                                       "track_id": str(r["id"])}},
                }
            elif kind == "folder":
                text = r["name"] or ""
                ident = str(r["id"])
                item["text"] = text
                item["textkey"] = text[:1].upper()
                item["id"] = ident
                # Two drill vocabularies on purpose: 'folder_id' (Perl) and
                # 'url' (this server's earlier shape) — the bmf branch
                # accepts both, so the roundtrip works for old and new taps.
                item["commonParams"] = {"folder_id": ident, "url": ident}
                # Perl bmf folder item (BrowseLibrary): add/add-hold/play
                # carry the folder id, so they load the whole folder.
                item["actions"] = {
                    "add": {"player": 0, "cmd": ["playlistcontrol"],
                            "params": {"cmd": "add", "menu": 1,
                                       "folder_id": ident}},
                    "add-hold": {"player": 0, "cmd": ["playlistcontrol"],
                                 "params": {"cmd": "insert", "menu": 1,
                                            "folder_id": ident}},
                    "play": {"player": 0, "cmd": ["playlistcontrol"],
                             "params": {"cmd": "load", "menu": 1,
                                        "folder_id": ident},
                             "nextWindow": "nowPlaying"},
                }
                item["icon"] = "html/images/musicfolder.png"
            else:
                continue
            out.append(item)
        return out

    @staticmethod
    def _browselibrary_menu_actions(kind: str, filters: dict | None = None,
                                    start: int = 0,
                                    count: int = 1) -> dict:
        """Perl base.actions for a browselibrary menu window — SqueezePlay
        uses 'go' to drill (album→mode:tracks, artist→mode:albums, …) and
        'play'/'add' to load the commonParams item into the playlist.
        For a TRACK list the plain go must NOT drill (tracks are leaves):
        Perl marks it context-only (window.isContextMenu), otherwise every
        single tap on a song re-opens the same list (infinite recursion).

        ``filters``/``start``/``count`` are echoed into the context action's
        params, like Perl's ``$request->getParamsCopy()``
        (Slim/Control/XMLBrowser.pm:978): the tap's follow-up request repeats
        them merged with the row's playControlParams, and without the drill
        filter it would list the whole library instead of the tapped album."""
        go_mode = {"albums": "tracks", "artists": "albums",
                   "genres": "albums", "years": "albums",
                   "folder": "bmf", "tracks": "tracks"}.get(kind, "albums")
        go_params: dict = {"mode": go_mode, "menu": 1}
        if kind == "artists":
            go_params["menu_mode"] = "artists"
        actions: dict = {
            "go": {"player": 0, "cmd": ["browselibrary", "items"],
                   "itemsParams": "commonParams",
                   "params": go_params},
            "play": {"player": 0, "cmd": ["playlistcontrol"],
                     "itemsParams": "commonParams",
                     "params": {"cmd": "load", "menu": 1},
                     "nextWindow": "nowPlaying"},
            "add": {"player": 0, "cmd": ["playlistcontrol"],
                    "itemsParams": "commonParams",
                    "params": {"cmd": "add", "menu": 1}},
        }
        if kind in ("tracks", "folder"):
            # Context-menu only (press-and-hold) — a plain tap on an audio
            # row or folder child must not re-open the same list.
            cm_params: dict = {"mode": go_mode, "menu": 1,
                               "useContextMenu": 1,
                               "_index": start, "_quantity": count}
            if kind == "tracks":
                cm_params.update(filters or {})
            cm_action: dict = {"player": 0, "cmd": ["browselibrary", "items"],
                               "itemsParams": "playControlParams",
                               "window": {"isContextMenu": 1},
                               "params": cm_params}
            # 'go' and 'playControl' are identical in Perl (XMLBrowser.pm:973):
            # SqueezePlay rewrites a tap's action name onto the row's
            # goAction ('playControl') and then looks that key up in
            # base.actions (SlimBrowserApplet.lua:1846-1913) — without
            # base.actions.playControl the tap aborts with EVENT_UNUSED.
            actions["go"] = cm_action
            actions["playControl"] = cm_action
        return actions

    @staticmethod
    def _playcontrol_context_menu(rows: list, start: int,
                                  index: int, kind: str = "tracks",
                                  filters: dict | None = None) -> dict:
        """Answer a SqueezePlay tap on an audio row with the play-control
        context menu (MENU-02) — Perl ``_playlistControlContextMenu``
        (Slim/Control/XMLBrowser.pm:1811) reached via the
        ``xmlbrowserPlayControl`` branch (:805-830).

        ``index`` is the absolute row index from the request
        (``$xmlbrowserPlayControl - $subFeed->{'offset'}``), so a paged
        request addresses the right row; an index outside the fetched
        window (negative, e.g. ``-1``) yields Perl's empty menu.

        Texts/styles/actions mirror the real Perl responses
        (tests/fixtures/perl_browselibrary_playcontrol_*.json):

        * one-item window (``items 0 1``) → Add to end / Play next / Play;
        * window with more than one item → the third text switches to
          "Diesen Titel wiedergeben" and a fourth "Alle Titel wiedergeben"
          entry is appended. Perl's condition is
          ``playalbum && defined subItemId && @{$subFeed->{items}} > 1 &&
          (subFeed/item playall)`` (:1839-1844), i.e. the *window size*, not
          the request's total ``count``.

        The per-mode target params come from the feed item action
        variables (:1623 ``_makePlayAction``; live probes: ``mode:albums`` →
        album_id + performance, ``mode:artists`` → artist_id + menu_mode,
        ``mode:genres`` → genre_id + role_id, ``mode:years`` → year,
        ``mode:tracks`` → track_id).
        """
        window = {"windowStyle": "text_list"}
        pos = index - start
        if pos < 0 or pos >= len(rows):
            # Perl only adds item_loop inside the in-range branch
            # (XMLBrowser.pm:813-830); out of range it returns just
            # window/offset/count (live probe + fixture: keys are exactly
            # ['count', 'offset', 'window']). Clients read item_loop via
            # ``.get``, so omitting the key is safe.
            return {"window": window, "offset": 0, "count": 0}
        row = rows[pos]
        if kind == "tracks":
            target: dict = {"track_id": str(row["id"])}
        elif kind == "artists":
            target = {"artist_id": str(row["id"]), "menu_mode": "artists"}
        elif kind == "genres":
            target = {"genre_id": str(row["id"]), "role_id": "ALBUMARTIST"}
        elif kind == "years":
            target = {"year": int(row["year"])}
        else:  # albums
            target = {"album_id": str(row["id"]), "performance": ""}

        def entry(cmd: str, next_window: str) -> dict:
            params = {"cmd": cmd, "menu": 1}
            params.update(target)
            return {"player": 0, "cmd": ["playlistcontrol"],
                    "params": params, "nextWindow": next_window}

        # Play-all needs >1 item in the current window (Perl); it replays
        # the request's drill filter and the tapped absolute index
        # (probe: params {cmd: load, menu: 1, play_index: 5,
        # sort: "albumtrack", album_id: "45"}).
        play_all = kind == "tracks" and len(rows) > 1
        item_loop = [
            {"text": "Am Ende hinzufügen", "style": "item_add",
             "actions": {"go": entry("add", "parentNoRefresh")}},
            {"text": "Als nächstes wiedergeben", "style": "itemNoAction",
             "actions": {"go": entry("insert", "parentNoRefresh")}},
            {"text": "Diesen Titel wiedergeben" if play_all else "Wiedergabe",
             "style": "item_play",
             "actions": {"go": entry("load", "nowPlaying")}},
        ]
        if play_all:
            all_params: dict = {"cmd": "load", "menu": 1, "play_index": index,
                                "sort": "albumtrack"}
            all_params.update(filters or {})
            item_loop.append({
                "text": "Alle Titel wiedergeben", "style": "itemNoAction",
                "actions": {"go": {"player": 0, "cmd": ["playlistcontrol"],
                                   "params": all_params,
                                   "nextWindow": "nowPlaying"}}})
        return {"window": window, "offset": 0, "count": len(item_loop),
                "item_loop": item_loop}

    async def _library_rows(self, mode: str, start: int, count: int,
                            search: str = "", filters: dict | None = None):
        """Return (rows, total, plural, kind) for a browselibrary mode.

        ``filters`` carries the drill-down ids SqueezePlay merges from the
        item's commonParams (album_id/artist_id/year/genre_id/…)."""
        filters = filters or {}
        f_artist = filters.get("artist_id")
        f_album = filters.get("album_id")
        f_genre = filters.get("genre_id")
        f_year = filters.get("year")

        def q(sql, *p):
            return _db_query(sql, p)

        def total_of(sql, *p):
            rows = _db_query(sql, p)
            # _db_query returns list[dict]; grab the first row's first value.
            return list(rows[0].values())[0] if rows else 0

        if mode == "tracks" or mode in ("songs", "titles"):
            # Album/artist/year drill → the track list (Perl mode:tracks).
            where, params = [], []
            if f_album:
                where.append("t.id IN (SELECT track FROM tracks_albums "
                             "WHERE album = ?)")
                params.append(f_album)
            if f_artist:
                where.append("t.id IN (SELECT track FROM tracks_contributors "
                             "WHERE contributor = ? AND role = 1)")
                params.append(f_artist)
            if f_year:
                where.append("t.year = ?")
                params.append(f_year)
            if f_genre:
                genre_text = ""
                if str(f_genre).isdigit():
                    g = _db_query("SELECT DISTINCT genre FROM tracks "
                                  "WHERE genre != '' ORDER BY genre COLLATE "
                                  "NOCASE LIMIT 1 OFFSET ?", (int(f_genre),))
                    genre_text = g[0]["genre"] if g else ""
                if genre_text:
                    where.append("t.genre = ?")
                    params.append(genre_text)
            if search:
                where.append("t.title LIKE ?")
                params.append(f"%{search}%")
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
            order = (" ORDER BY t.tracknum" if f_album
                     else " ORDER BY t.title COLLATE NOCASE")
            rows = q("SELECT DISTINCT t.id, t.title, t.year FROM tracks t"
                     + where_sql + order + " LIMIT ? OFFSET ?",
                     *(params + [count, start]))
            total = total_of(
                "SELECT COUNT(DISTINCT t.id) FROM tracks t" + where_sql,
                *params)
            return rows, total, "tracks_loop", "tracks"
        if mode == "albums":
            where, params = [], []
            if f_artist:
                where.append("al.id IN (SELECT album FROM tracks_albums "
                             "WHERE track IN (SELECT track FROM "
                             "tracks_contributors WHERE contributor = ? "
                             "AND role = 1))")
                params.append(f_artist)
            if f_year:
                where.append("al.year = ?")
                params.append(f_year)
            if f_genre:
                genre_text = ""
                if str(f_genre).isdigit():
                    g = _db_query("SELECT DISTINCT genre FROM tracks "
                                  "WHERE genre != '' ORDER BY genre COLLATE "
                                  "NOCASE LIMIT 1 OFFSET ?", (int(f_genre),))
                    genre_text = g[0]["genre"] if g else ""
                if genre_text:
                    where.append("al.id IN (SELECT album FROM tracks_albums "
                                 "WHERE track IN (SELECT id FROM tracks "
                                 "WHERE genre = ?))")
                    params.append(genre_text)
            if search:
                where.append("al.title LIKE ?")
                params.append(f"%{search}%")
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
            rows = q("SELECT DISTINCT al.id, al.title, al.artwork "
                     "FROM albums al" + where_sql +
                     " ORDER BY al.title LIMIT ? OFFSET ?",
                     *(params + [count, start]))
            total = total_of(
                "SELECT COUNT(DISTINCT al.id) FROM albums al" + where_sql,
                *params)
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
            # The genres table is not populated by the importer (LIB-10/R5),
            # so there is no real numeric genre id to hand out. Perl sends a
            # NUMERIC ``genre_id`` (live probe 192.168.1.90: the genre row is
            # ``commonParams.genre_id "497"``; the tap params
            # ``{genre_id: "497", role_id: "ALBUMARTIST"}``). We expose the
            # index into the sorted DISTINCT genre-text list instead: NUMERIC
            # and STABLE, derived from the DB (ROW_NUMBER over the same
            # ``ORDER BY genre COLLATE NOCASE`` used by _genre_id_to_text and
            # the genre drill filters), so ``genre_id:<n>`` round-trips to
            # exactly that genre text. DOCUMENTED DIVERGENCE: this is not
            # Perl's real genre id, only a stable local id.
            rows = q("SELECT genre, "
                     "ROW_NUMBER() OVER (ORDER BY genre COLLATE NOCASE) - 1 "
                     "AS id FROM (SELECT DISTINCT genre FROM tracks "
                     "WHERE genre != '') "
                     "ORDER BY genre COLLATE NOCASE LIMIT ? OFFSET ?",
                     count, start)
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
            # „Musikordner“: the tree is aggregated from the track URLs
            # below the musicdir root. Without a root (no pref, no
            # multi-track library) the folder stays empty instead of
            # showing the first URL component ("home"/"run" bug).
            root = _bmf_music_root()
            if not root:
                return [], 0, "musicfolder_loop", "folder"
            directory = _bmf_resolve_dir(search, root) if search else root
            rows, total = _bmf_children(directory, start, count)
            return rows, total, "musicfolder_loop", "folder"
        if mode == "search":
            # My Music → Suchen: search track titles (falling back to an
            # empty list instead of dumping every album).
            if not search:
                return [], 0, "search_loop", "search"
            like = f"%{search}%"
            rows = q("SELECT DISTINCT t.id, t.title FROM tracks t "
                     "WHERE t.title LIKE ? ORDER BY t.title COLLATE NOCASE "
                     "LIMIT ? OFFSET ?", like, count, start)
            total = total_of(
                "SELECT COUNT(DISTINCT t.id) FROM tracks t WHERE t.title LIKE ?",
                like)
            rows = [{"id": r["id"], "name": r["title"] or ""} for r in rows]
            return rows, total, "search_loop", "search"
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
                if filters.get("artist_id") and str(filters["artist_id"]).isdigit():
                    joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
                    # The JOIN alone is not enough — add the actual
                    # contributor predicate, else every artist's albums leak.
                    cond = "tc.contributor = ?"
                    where = ((" WHERE " + cond) if not where else where + " AND " + cond)
                    params = params + (int(filters["artist_id"]),)
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
                # tracks_albums ta is joined in the base query below — do
                # NOT add a second JOIN tracks_albums ta here (alias clash,
                # which made the album drill return count 0 / no tracks).
                _w: list[str] = []
                _p: list = []
                if filters.get("search"):
                    _w.append("t.title LIKE ?"); _p.append(f"%{filters['search']}%")
                joins = ""
                if filters.get("artist_id") and str(filters["artist_id"]).isdigit():
                    joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
                    _w.append("tc.contributor = ?"); _p.append(int(filters["artist_id"]))
                if filters.get("album_id") and str(filters["album_id"]).isdigit():
                    _w.append("ta.album = ?"); _p.append(int(filters["album_id"]))
                where = (" WHERE " + " AND ".join(_w)) if _w else ""
                params = tuple(_p)
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
                        "genre": info.get("genre", ""),
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
                    "SELECT COUNT(DISTINCT t.id) FROM tracks t"
                    " LEFT JOIN tracks_albums ta ON ta.track = t.id"
                    + joins + where,
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

        # Resolve the real path and enforce it stays inside the static root.
        # A naive string-prefix check on the unresolved path is bypassable
        # (e.g. "html/../secret.txt" literally starts with "html/").
        base = self._static_dir.resolve()
        file_path = (self._static_dir / rel).resolve()
        if not file_path.is_relative_to(base):
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
