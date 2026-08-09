"""
Jive / SqueezePlay protocol handler for Lyrion Music Server.

Handles the ZIP-based (Zeroconf Interactive Protocol) requests from
SqueezePlay/Jive UI clients. Uses XML/JSON-RPC style request/response
over HTTP.

The Jive protocol is used by native SqueezePlay applications (desktop,
mobile) and implements a tree-structured menu system with screen
rendering requests.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Jive request / response types
# ---------------------------------------------------------------------------


class JiveMethod(str, Enum):
    """Jive RPC method names."""

    GET_VERSION = "getVersion"
    GET_MODEL = "getModel"
    GET_SCREEN = "getScreen"
    BUTTON = "button"
    CONTEXT = "context"
    ALBUM_SONGS = "albumSongs"
    ARTIST_SONGS = "artistSongs"
    PLAYLIST_SONGS = "playlistSongs"
    GENRE_ARTISTS = "genreArtists"
    ARTIST_ALBUMS = "artistAlbums"
    SEARCH_SONGS = "searchSongs"
    PREFERENCE_GET = "preferenceGet"
    PREFERENCE_SET = "preferenceSet"
    PLAYLIST_ADD = "playlist_add"
    PLAYLIST_INSERT = "playlist_insert"
    PLAYLIST_LOAD = "playlist_load"
    PLAYLIST_SAVE = "playlist_save"
    PLAYLIST_DELETE = "playlist_delete"
    ALBUM_ART = "album_art"
    STATION_SONGS = "stationSongs"


@dataclass
class JiveRequest:
    """Parsed Jive RPC request."""

    method: str
    id: Optional[str] = None
    params: dict = field(default_factory=dict)
    player_id: Optional[str] = None

    @classmethod
    def parse(cls, raw: dict) -> "JiveRequest":
        """Parse a raw JSON-RPC 1.0/2.0 dict into a JiveRequest."""
        method = raw.get("method", "")
        req_id = raw.get("id")
        params = raw.get("params", {}) or {}
        player_id = params.get("player", {}).get("mac") if isinstance(params.get("player"), dict) else None
        return cls(method=method, id=req_id, params=params, player_id=player_id)


@dataclass
class JiveResponse:
    """Jive RPC response."""

    result: Any = None
    error: Optional[Any] = None
    id: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialise to JSON-RPC 2.0 dict."""
        if self.error is not None:
            return {
                "jsonrpc": "2.0",
                "error": self.error,
                "id": self.id,
            }
        return {
            "jsonrpc": "2.0",
            "result": self.result,
            "id": self.id,
        }


# ---------------------------------------------------------------------------
# Screen / menu models
# ---------------------------------------------------------------------------


@dataclass
class JiveScreen:
    """
    Represents a Jive UI screen.

    Attributes:
        style: Screen style ('home', 'list', 'text', 'image', etc.)
        title: Screen title text.
        lines: List of line items (for list screens).
        windowed: Whether the screen is in a popup window.
        header: Optional header image/text.
        footer: Optional footer text.
        icon: Icon identifier.
        timeout: Auto-dismiss timeout in seconds.
    """

    style: str = "list"
    title: str = ""
    lines: List[dict] = field(default_factory=list)
    windowed: bool = False
    header: Optional[dict] = None
    footer: Optional[str] = None
    icon: Optional[str] = None
    timeout: int = 0
    # Layout parameters
    num_items: int = 0
    overlay_text: Optional[str] = None
    # Pagination
    page_start: int = 0
    page_end: int = 0
    # Arbitrary extras
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise to Jive wire format."""
        result: dict[str, Any] = {
            "style": self.style,
            "title": self.title,
            "windowed": self.windowed,
        }
        if self.header:
            result["header"] = self.header
        if self.footer:
            result["footer"] = self.footer
        if self.icon:
            result["icon"] = self.icon
        if self.timeout:
            result["timeout"] = self.timeout
        if self.num_items:
            result["numItems"] = self.num_items
        if self.page_start or self.page_end:
            result["page_start"] = self.page_start
            result["page_end"] = self.page_end
        if self.overlay_text:
            result["overlayText"] = self.overlay_text
        if self.extras:
            result.update(self.extras)

        if self.lines:
            result["text"] = [
                self._line_to_jive(l) for l in self.lines
            ]
        return result

    @staticmethod
    def _line_to_jive(line: dict) -> dict:
        """Convert a line dict to Jive format."""
        return {
            "text": line.get("text", ""),
            "subtext": line.get("subtext", ""),
            "icon": line.get("icon"),
            "style": line.get("style", "item"),
            "actions": line.get("actions", {}),
            "nextWindow": line.get("next_window", "parent"),
        }


# ---------------------------------------------------------------------------
# Jive handler
# ---------------------------------------------------------------------------


class JiveHandler:
    """
    Handles Jive / SqueezePlay protocol interactions.

    The Jive protocol uses JSON-RPC over HTTP with special wire formats
    for menus, screens, and player events.

    Usage::

        handler = JiveHandler(dispatcher)
        # On HTTP request for Jive endpoint:
        response = await handler.handle_request(request_body, player_id)
    """

    def __init__(
        self,
        dispatcher: Optional["RequestDispatcher"] = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._player_screens: dict[str, JiveScreen] = {}
        self._menu_stack: dict[str, List[JiveScreen]] = {}
        # Preference cache
        self._prefs: dict[str, Any] = {}
        # Menu item handler registry
        self._menu_handlers: dict[str, Callable[..., Awaitable[JiveScreen]]] = {}
        self._register_default_handlers()

    def set_dispatcher(self, dispatcher: "RequestDispatcher") -> None:
        self._dispatcher = dispatcher

    # -----------------------------------------------------------------------
    # Default menu handlers
    # -----------------------------------------------------------------------

    def _register_default_handlers(self) -> None:
        """Register built-in menu handler functions."""
        handlers = {
            "home": self._menu_home,
            "players": self._menu_players,
            "now_playing": self._menu_now_playing,
            "albums": self._menu_albums,
            "artists": self._menu_artists,
            "genres": self._menu_genres,
            "playlists": self._menu_playlists,
            "music": self._menu_music,
            "favorites": self._menu_favorites,
            "search": self._menu_search,
            "settings": self._menu_settings,
            "album_songs": self._menu_album_songs,
            "artist_songs": self._menu_artist_songs,
            "playlist_songs": self._menu_playlist_songs,
            "search_songs": self._menu_search_songs,
        }
        self._menu_handlers.update(handlers)

    # -----------------------------------------------------------------------
    # Request handling
    # -----------------------------------------------------------------------

    async def handle_request(
        self,
        raw: dict,
    ) -> dict:
        """
        Handle an incoming Jive JSON-RPC request.

        Args:
            raw: Parsed JSON body from the HTTP request.

        Returns:
            JSON-RPC response dict (to be JSON-encoded).
        """
        try:
            request = JiveRequest.parse(raw)
        except Exception as exc:
            return JiveResponse(
                error={"code": -32700, "message": f"Parse error: {exc}"},
                id=None,
            ).to_dict()

        # Route to method handler
        try:
            result = await self._dispatch(request)
            return JiveResponse(result=result, id=request.id).to_dict()
        except Exception as exc:
            logger.exception("Jive handler error for %s: %s", request.method, exc)
            return JiveResponse(
                error={"code": -32603, "message": str(exc)},
                id=request.id,
            ).to_dict()

    async def handle_batch(
        self,
        batch: list[dict],
    ) -> list[dict]:
        """Handle a batch of JSON-RPC requests."""
        return [await self.handle_request(item) for item in batch]

    async def _dispatch(self, request: JiveRequest) -> Any:
        """Dispatch a JiveRequest to the appropriate handler."""
        method = request.method

        # Core Jive methods
        if method == JiveMethod.GET_VERSION:
            return self._get_version()
        elif method == JiveMethod.GET_MODEL:
            return self._get_model(request)
        elif method == JiveMethod.GET_SCREEN:
            return self._get_screen(request)
        elif method == JiveMethod.BUTTON:
            return await self._handle_button(request)
        elif method == JiveMethod.CONTEXT:
            return self._get_context(request)
        elif method == JiveMethod.PLAYLIST_ADD:
            return await self._playlist_add(request)
        elif method == JiveMethod.PLAYLIST_INSERT:
            return await self._playlist_insert(request)
        elif method == JiveMethod.PLAYLIST_LOAD:
            return await self._playlist_load(request)
        elif method == JiveMethod.PLAYLIST_SAVE:
            return await self._playlist_save(request)
        elif method == JiveMethod.ALBUM_SONGS:
            return await self._album_songs(request)
        elif method == JiveMethod.ARTIST_SONGS:
            return await self._artist_songs(request)
        elif method == JiveMethod.ARTIST_ALBUMS:
            return await self._artist_albums(request)
        elif method == JiveMethod.GENRE_ARTISTS:
            return await self._genre_artists(request)
        elif method == JiveMethod.PLAYLIST_SONGS:
            return await self._playlist_songs(request)
        elif method == JiveMethod.SEARCH_SONGS:
            return await self._search_songs(request)
        elif method == JiveMethod.PREFERENCE_GET:
            return self._preference_get(request)
        elif method == JiveMethod.PREFERENCE_SET:
            return self._preference_set(request)
        elif method == JiveMethod.ALBUM_ART:
            return self._album_art(request)

        # Menu navigation
        if method.startswith("menu:"):
            menu_name = method[5:]
            return await self._show_menu(menu_name, request)

        # Unknown method
        raise ValueError(f"Unknown Jive method: {method}")

    # -----------------------------------------------------------------------
    # Core methods
    # -----------------------------------------------------------------------

    def _get_version(self) -> dict:
        return {
            "version": "9.2.0",
            "build": "Lyrion",
            "uuid": "lyrion-local",
        }

    def _get_model(self, request: JiveRequest) -> dict:
        return {
            "model": "Lyrion",
            "version": "9.2.0",
            "uuid": "lyrion-local",
        }

    def _get_context(self, request: JiveRequest) -> dict:
        """Return current player context (track, playlist position, etc.)."""
        player_id = request.player_id
        if not player_id:
            return {}
        # TODO: real context from player
        return {
            "player": {
                "mac": player_id,
                "name": self._players.get(player_id, {}).get("name", "Player"),
                "model": "SqueezePlay",
            },
            "playlist": {
                "position": 0,
                "count": 0,
            },
        }

    def _get_screen(self, request: JiveRequest) -> dict:
        """Return the current screen for a player."""
        player_id = request.player_id or "default"
        screen = self._player_screens.get(player_id)
        if screen:
            return screen.to_dict()
        # Return home screen by default
        return self._build_home_screen().to_dict()

    async def _handle_button(self, request: JiveRequest) -> dict:
        """
        Handle a button press event.

        Button codes: left, right, up, down, ok, back, play, pause,
        add, shuffle, repeat, power, volup, voldown, etc.
        """
        button = request.params.get("button", "")
        player_id = request.player_id

        logger.debug("Jive button: %s player=%s", button, player_id)

        # Map button to transport command
        transport_map = {
            "play": "play",
            "pause": "pause",
            "stop": "stop",
            "prev": "prev",
            "next": "next",
            "power": "power",
        }
        if button in transport_map and player_id and self._dispatcher:
            cmd = transport_map[button]
            await self._dispatcher.player_command(player_id, cmd)

        return {"handled": True}

    # -----------------------------------------------------------------------
    # Menu building helpers
    # -----------------------------------------------------------------------

    async def _show_menu(
        self,
        menu_name: str,
        request: JiveRequest,
    ) -> dict:
        """Build and return a menu screen."""
        player_id = request.player_id or "default"
        handler = self._menu_handlers.get(menu_name)
        if not handler:
            raise ValueError(f"Unknown menu: {menu_name}")
        screen = await handler(request)
        self._player_screens[player_id] = screen
        return screen.to_dict()

    def _build_home_screen(self) -> JiveScreen:
        """Build the default home screen."""
        return JiveScreen(
            style="home",
            title="Lyrion Music",
            lines=[
                {
                    "text": "Now Playing",
                    "icon": "music",
                    "style": "item",
                    "next_window": "nowPlaying",
                    "actions": {
                        "do": {"cmd": ["now_playing"], "player": 0},
                    },
                },
                {
                    "text": "Music Library",
                    "icon": "library",
                    "style": "submenu",
                    "next_window": "music",
                    "actions": {
                        "go": {"cmd": ["music"], "player": 0},
                    },
                },
                {
                    "text": "Playlists",
                    "icon": "playlist",
                    "style": "submenu",
                    "next_window": "playlists",
                    "actions": {
                        "go": {"cmd": ["playlists"], "player": 0},
                    },
                },
                {
                    "text": "Settings",
                    "icon": "settings",
                    "style": "submenu",
                    "next_window": "settings",
                    "actions": {
                        "go": {"cmd": ["settings"], "player": 0},
                    },
                },
            ],
        )

    async def _menu_home(self, request: JiveRequest) -> JiveScreen:
        return self._build_home_screen()

    async def _menu_players(self, request: JiveRequest) -> JiveScreen:
        """Build the players selection screen."""
        lines: list[dict] = []
        if self._dispatcher:
            players = await self._dispatcher.list_players()
            for p in players:
                pid = p.get("id") or p.get("mac") or "?"
                lines.append({
                    "text": p.get("name", pid),
                    "subtext": p.get("ip", ""),
                    "style": "item",
                    "next_window": "playerStatus",
                    "actions": {
                        "go": {"cmd": ["player", pid], "player": 0},
                    },
                })
        return JiveScreen(
            style="list",
            title="Players",
            lines=lines,
        )

    async def _menu_now_playing(self, request: JiveRequest) -> JiveScreen:
        """Build the now playing screen."""
        player_id = request.player_id or "default"
        # TODO: real player state
        return JiveScreen(
            style="nowPlaying",
            title="Now Playing",
            lines=[],
            extras={"player": player_id},
        )

    async def _menu_music(self, request: JiveRequest) -> JiveScreen:
        return JiveScreen(
            style="list",
            title="Music Library",
            lines=[
                {
                    "text": "Artists",
                    "icon": "artists",
                    "style": "submenu",
                    "actions": {"go": {"cmd": ["artists"], "player": 0}},
                },
                {
                    "text": "Albums",
                    "icon": "albums",
                    "style": "submenu",
                    "actions": {"go": {"cmd": ["albums"], "player": 0}},
                },
                {
                    "text": "Genres",
                    "icon": "genres",
                    "style": "submenu",
                    "actions": {"go": {"cmd": ["genres"], "player": 0}},
                },
                {
                    "text": "All Songs",
                    "icon": "songs",
                    "style": "submenu",
                    "actions": {"go": {"cmd": ["all_songs"], "player": 0}},
                },
                {
                    "text": "New Music",
                    "icon": "newmusic",
                    "style": "submenu",
                    "actions": {"go": {"cmd": ["newmusic"], "player": 0}},
                },
            ],
        )

    async def _menu_albums(self, request: JiveRequest) -> JiveScreen:
        """Browse albums."""
        lines = await self._browse_albums(0, 100)
        return JiveScreen(style="list", title="Albums", lines=lines)

    async def _menu_artists(self, request: JiveRequest) -> JiveScreen:
        lines = await self._browse_artists(0, 100)
        return JiveScreen(style="list", title="Artists", lines=lines)

    async def _menu_genres(self, request: JiveRequest) -> JiveScreen:
        lines = await self._browse_genres(0, 100)
        return JiveScreen(style="list", title="Genres", lines=lines)

    async def _menu_playlists(self, request: JiveRequest) -> JiveScreen:
        lines = await self._browse_playlists(0, 100)
        return JiveScreen(style="list", title="Playlists", lines=lines)

    async def _menu_favorites(self, request: JiveRequest) -> JiveScreen:
        """List saved radio stations (favorites)."""
        lines: list[dict] = []
        try:
            from lyrion.music.radio import get_radio_manager
            stations = await get_radio_manager().list_stations()
            for s in stations:
                lines.append({
                    "text": s.name,
                    "subtext": s.url,
                    "style": "item",
                    "actions": {
                        "play": {"cmd": ["radio", "play", request.player_id or "0", str(s.id)]},
                        "go": {"cmd": ["radio", "play", request.player_id or "0", str(s.id)]},
                    },
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug("Favorites menu unavailable: %s", exc)
        return JiveScreen(style="list", title="Favorites", lines=lines)

    async def _menu_search(self, request: JiveRequest) -> JiveScreen:
        return JiveScreen(
            style="input",
            title="Search",
            extras={"inputStyle": "keyboard"},
        )

    async def _menu_settings(self, request: JiveRequest) -> JiveScreen:
        return JiveScreen(style="list", title="Settings", lines=[])

    async def _menu_album_songs(self, request: JiveRequest) -> JiveScreen:
        album_id = request.params.get("album_id", "")
        lines = await self._browse_tracks({"album_id": album_id}, 0, 100)
        return JiveScreen(style="list", title="Album", lines=lines)

    async def _menu_artist_songs(self, request: JiveRequest) -> JiveScreen:
        artist_id = request.params.get("artist_id", "")
        lines = await self._browse_tracks({"artist_id": artist_id}, 0, 100)
        return JiveScreen(style="list", title="Artist", lines=lines)

    async def _menu_playlist_songs(self, request: JiveRequest) -> JiveScreen:
        playlist_id = request.params.get("playlist_id", "")
        lines = await self._browse_playlist_tracks(playlist_id, 0, 100)
        return JiveScreen(style="list", title="Playlist", lines=lines)

    async def _menu_search_songs(self, request: JiveRequest) -> JiveScreen:
        query = request.params.get("query", "")
        lines = await self._browse_tracks({"search": query}, 0, 100)
        return JiveScreen(style="list", title=f"Search: {query}", lines=lines)

    # -----------------------------------------------------------------------
    # Browse helpers (fetch from dispatcher/db)
    # -----------------------------------------------------------------------

    async def _browse_albums(self, offset: int, limit: int) -> List[dict]:
        if self._dispatcher:
            raw = await self._dispatcher.query_albums(offset, limit, {}, "aAl")
            return self._parse_item_list(raw)
        return []

    async def _browse_artists(self, offset: int, limit: int) -> List[dict]:
        if self._dispatcher:
            raw = await self._dispatcher.query_artists(offset, limit, {})
            return self._parse_item_list(raw)
        return []

    async def _browse_genres(self, offset: int, limit: int) -> List[dict]:
        if self._dispatcher:
            raw = await self._dispatcher.query_genres(offset, limit, {})
            return self._parse_item_list(raw)
        return []

    async def _browse_playlists(self, offset: int, limit: int) -> List[dict]:
        if self._dispatcher:
            raw = await self._dispatcher.query_playlists(offset, limit, {})
            return self._parse_item_list(raw)
        return []

    async def _browse_tracks(
        self,
        filters: dict,
        offset: int,
        limit: int,
    ) -> List[dict]:
        if self._dispatcher:
            raw = await self._dispatcher.query_tracks(offset, limit, filters, "aAlLttd")
            return self._parse_item_list(raw)
        return []

    async def _browse_playlist_tracks(
        self,
        playlist_id: str,
        offset: int,
        limit: int,
    ) -> List[dict]:
        if self._dispatcher:
            raw = await self._dispatcher.query_playlist_tracks(
                playlist_id, offset, limit, "aAlLttd"
            )
            return self._parse_item_list(raw)
        return []

    @staticmethod
    def _parse_item_list(lines: list[str]) -> List[dict]:
        """Parse CLI-style lines into dict items."""
        items: List[dict] = []
        for line in lines:
            if not line or " " not in line:
                continue
            parts = line.split(" | ")
            if len(parts) >= 2:
                items.append({
                    "text": parts[0],
                    "subtext": " | ".join(parts[1:]),
                    "style": "item",
                })
        return items

    # -----------------------------------------------------------------------
    # Playlist operations
    # -----------------------------------------------------------------------

    async def _playlist_add(self, request: JiveRequest) -> dict:
        track_id = request.params.get("track_id")
        player_id = request.player_id
        if self._dispatcher and player_id and track_id:
            await self._dispatcher.player_command(
                player_id, "playlist add", [track_id]
            )
        return {"success": True}

    async def _playlist_insert(self, request: JiveRequest) -> dict:
        track_id = request.params.get("track_id")
        player_id = request.player_id
        if self._dispatcher and player_id and track_id:
            await self._dispatcher.player_command(
                player_id, "playlist insert", [track_id]
            )
        return {"success": True}

    async def _playlist_load(self, request: JiveRequest) -> dict:
        playlist_id = request.params.get("playlist_id")
        player_id = request.player_id
        if self._dispatcher and player_id and playlist_id:
            await self._dispatcher.player_command(
                player_id, "playlist load", [playlist_id]
            )
        return {"success": True}

    async def _playlist_save(self, request: JiveRequest) -> dict:
        name = request.params.get("name", "")
        player_id = request.player_id
        if self._dispatcher and player_id and name:
            await self._dispatcher.player_command(
                player_id, "playlist save", [name]
            )
        return {"success": True}

    async def _album_songs(self, request: JiveRequest) -> dict:
        screen = await self._menu_album_songs(request)
        return screen.to_dict()

    async def _artist_songs(self, request: JiveRequest) -> dict:
        screen = await self._menu_artist_songs(request)
        return screen.to_dict()

    async def _artist_albums(self, request: JiveRequest) -> dict:
        artist_id = request.params.get("artist_id", "")
        lines = []  # TODO
        return JiveScreen(style="list", title="Albums", lines=lines).to_dict()

    async def _genre_artists(self, request: JiveRequest) -> dict:
        genre_id = request.params.get("genre_id", "")
        lines = []  # TODO
        return JiveScreen(style="list", title="Artists", lines=lines).to_dict()

    async def _playlist_songs(self, request: JiveRequest) -> dict:
        screen = await self._menu_playlist_songs(request)
        return screen.to_dict()

    async def _search_songs(self, request: JiveRequest) -> dict:
        screen = await self._menu_search_songs(request)
        return screen.to_dict()

    # -----------------------------------------------------------------------
    # Preferences
    # -----------------------------------------------------------------------

    def _preference_get(self, request: JiveRequest) -> dict:
        key = request.params.get("key", "")
        return {"key": key, "value": self._prefs.get(key)}

    def _preference_set(self, request: JiveRequest) -> dict:
        key = request.params.get("key", "")
        value = request.params.get("value")
        self._prefs[key] = value
        return {"success": True}

    # -----------------------------------------------------------------------
    # Album art
    # -----------------------------------------------------------------------

    def _album_art(self, request: JiveRequest) -> dict:
        artwork_track_id = request.params.get("track_id")
        # TODO: return artwork URL/path
        return {"artwork_url": f"/music/{artwork_track_id}/cover.jpg"}

    # -----------------------------------------------------------------------
    # Internal player cache
    # -----------------------------------------------------------------------

    @property
    def _players(self) -> dict:
        if self._dispatcher:
            return {p["id"]: p for p in self._dispatcher._players}
        return {}


__all__ = [
    "JiveHandler",
    "JiveRequest",
    "JiveResponse",
    "JiveScreen",
    "JiveMethod",
]
