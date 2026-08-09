"""
XML Browser for Lyrion Music Server.

Generates XML responses for the classic LMS/SqueezeCenter web UI.
Handles browse requests and returns XML-formatted menus and lists.

The XML browser protocol is used by legacy web browsers connecting
to the LMS web interface on port 9000.
"""
from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# XML namespaces
# ---------------------------------------------------------------------------

NS = {
    "xml": "http://www.w3.org/XML/1998/namespace",
    "lm": "http://lyrionmusic.com/slim/",
}

ET.register_namespace("", "http://lyrionmusic.com/slim/")


# ---------------------------------------------------------------------------
# XML response types
# ---------------------------------------------------------------------------


class XMLItemStyle(str, Enum):
    """Item style for XML browse responses."""

    ITEM = "item"
    AUDIO = "audio"
    IMAGE = "image"
    PLAYLIST = "playlist"
    ALBUM = "album"
    ARTIST = "artist"
    GENRE = "genre"
    FOLDER = "folder"
    LINK = "link"
    INPUT = "input"
    TEXT = "text"
    DIVIDER = "divider"


@dataclass
class XMLItem:
    """
    Represents a single item in an XML browse response.

    Attributes:
        text: Primary display text.
        subtext: Secondary/description text.
        image: Icon/cover image URL.
        href: Navigation link.
        style: Item style (audio, playlist, folder, etc.).
        params: Additional link parameters.
        id: Unique identifier for the item.
        type_hint: Content type hint.
        cmd: CLI command to execute.
        params_xml: XML parameters for the command.
    """

    text: str = ""
    subtext: str = ""
    image: str = ""
    href: str = ""
    style: XMLItemStyle = XMLItemStyle.ITEM
    params: dict = field(default_factory=dict)
    id: Optional[str] = None
    type_hint: Optional[str] = None
    cmd: Optional[List[str]] = None
    params_xml: Optional[str] = None
    count: Optional[int] = None  # item count for containers

    def to_element(self, parent: Optional[ET.Element] = None) -> ET.Element:
        """Convert to an XML <item> element."""
        item = ET.SubElement(parent or ET.Element("item"), "item")
        ET.SubElement(item, "text").text = self.text
        if self.subtext:
            ET.SubElement(item, "subtext").text = self.subtext
        if self.image:
            ET.SubElement(item, "image").text = self.image
        if self.href:
            ET.SubElement(item, "href").text = self.href
        if self.id:
            ET.SubElement(item, "id").text = self.id
        if self.type_hint:
            ET.SubElement(item, "type").text = self.type_hint
        if self.style != XMLItemStyle.ITEM:
            ET.SubElement(item, "style").text = self.style.value
        if self.count is not None:
            ET.SubElement(item, "count").text = str(self.count)
        if self.cmd:
            ET.SubElement(item, "cmd").text = " ".join(self.cmd)
        if self.params_xml:
            item.append(ET.fromstring(self.params_xml))
        return item


@dataclass
class XMLBrowseResult:
    """
    A complete XML browse response.

    Attributes:
        base_url: Server base URL for building links.
        level: Navigation level (home=0, detail=1, etc.).
       窓
        count: Total number of items in the result.
        window: Pagination window (start, end).
    """

    base_url: str = "http://localhost:9000"
    level: int = 0
    title: str = ""
    items: List[XMLItem] = field(default_factory=list)
    total_count: Optional[int] = None
    window_start: int = 0
    window_end: int = 0
    # Page navigation
    page: int = 1
    per_page: int = 50
    has_next: bool = False
    has_prev: bool = False
    # Extras
    refresh: bool = False
    extras: dict = field(default_factory=dict)

    def to_element(self) -> ET.Element:
        """Serialise to XML element tree."""
        root = ET.Element("results")
        ET.SubElement(root, "base_url").text = self.base_url
        ET.SubElement(root, "level").text = str(self.level)
        ET.SubElement(root, "title").text = self.title
        ET.SubElement(root, "page").text = str(self.page)
        ET.SubElement(root, "perPage").text = str(self.per_page)
        ET.SubElement(root, "window").text = f"{self.window_start},{self.window_end}"

        if self.total_count is not None:
            ET.SubElement(root, "count").text = str(self.total_count)
        if self.refresh:
            ET.SubElement(root, "refresh").text = "1"
        if self.has_prev:
            ET.SubElement(root, "hasPrev").text = "1"
        if self.has_next:
            ET.SubElement(root, "hasNext").text = "1"

        for item in self.items:
            item.to_element(root)

        # Append any extras as children
        for key, value in self.extras.items():
            ET.SubElement(root, key).text = str(value)

        return root

    def to_xml_string(self, pretty: bool = True) -> str:
        """Serialise to XML string."""
        root = self.to_element()
        if pretty:
            return ET.tostring(root, encoding="unicode", xml_declaration=True)
        return ET.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# XML Browser
# ---------------------------------------------------------------------------


class XMLBrowser:
    """
    Generates XML browse responses for the classic web UI.

    The XML browser is used by:
    - The LMS web interface (port 9000)
    - Third-party apps using the old XML API
    - AJAX polling for live status updates

    Usage::

        browser = XMLBrowser(dispatcher)
        xml = await browser.browse(path="/music/artists", params={"artist_id": "5"})
        # xml is a string of XML
    """

    # Route table: URL path -> handler method
    ROUTES: dict[str, str] = {}

    def __init__(
        self,
        dispatcher: Optional["RequestDispatcher"] = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._base_url = "http://localhost:9000"
        # Cache for browse results
        self._cache: dict[str, Tuple[float, XMLBrowseResult]] = {}
        self._cache_ttl = 30.0  # seconds

    def set_dispatcher(self, dispatcher: "RequestDispatcher") -> None:
        self._dispatcher = dispatcher

    def set_base_url(self, url: str) -> None:
        self._base_url = url

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    async def browse(
        self,
        path: str,
        params: Optional[dict[str, str]] = None,
    ) -> str:
        """
        Handle a browse request and return XML string.

        Args:
            path: Browse path (e.g. "/music/artists").
            params: Query parameters.

        Returns:
            XML response string.
        """
        params = params or {}
        try:
            result = await self._dispatch(path, params)
            # Cache the result
            self._cache[path] = (time.time(), result)
            return result.to_xml_string()
        except Exception as exc:
            logger.exception("XML browse error for %s: %s", path, exc)
            return self._error_xml(str(exc))

    async def browse_element(
        self,
        path: str,
        params: Optional[dict[str, str]] = None,
    ) -> ET.Element:
        """Return an XML element tree instead of a string."""
        result = await self._dispatch(path, params or {})
        return result.to_element()

    # -----------------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------------

    async def _dispatch(
        self,
        path: str,
        params: dict[str, str],
    ) -> XMLBrowseResult:
        """Route a browse request to the appropriate handler."""
        path = path.lstrip("/")

        # Check cache
        if path in self._cache:
            cached_at, cached = self._cache[path]
            if time.time() - cached_at < self._cache_ttl:
                return cached

        # Route by path prefix
        if path.startswith("music/"):
            subpath = path[6:]
            return await self._browse_music(subpath, params)
        elif path.startswith("players/"):
            return await self._browse_players(path[8:], params)
        elif path.startswith("search/"):
            return await self._browse_search(path[7:], params)
        elif path.startswith("status/"):
            return await self._browse_status(path[7:], params)
        elif path == "home" or path == "":
            return await self._browse_home(params)
        elif path.startswith("playlist/"):
            return await self._browse_playlist(path[9:], params)
        elif path.startswith("albums/"):
            return await self._browse_album(path[7:], params)
        elif path.startswith("artists/"):
            return await self._browse_artist(path[8:], params)
        else:
            return await self._browse_default(path, params)

    # -----------------------------------------------------------------------
    # Browse handlers
    # -----------------------------------------------------------------------

    async def _browse_home(self, params: dict) -> XMLBrowseResult:
        """Home / root browse page."""
        result = XMLBrowseResult(
            base_url=self._base_url,
            level=0,
            title="Lyrion Music",
        )
        result.items = [
            XMLItem(
                text="Now Playing",
                href=f"{self._base_url}/status/current",
                image="html/images/now-playing.png",
                style=XMLItemStyle.AUDIO,
            ),
            XMLItem(
                text="Music Library",
                href=f"{self._base_url}/music/artists",
                image="html/images/library.png",
                style=XMLItemStyle.FOLDER,
            ),
            XMLItem(
                text="Playlists",
                href=f"{self._base_url}/playlists",
                image="html/images/playlist.png",
                style=XMLItemStyle.FOLDER,
            ),
            XMLItem(
                text="Radio",
                href=f"{self._base_url}/radio",
                image="html/images/radio.png",
                style=XMLItemStyle.FOLDER,
            ),
            XMLItem(
                text="Settings",
                href=f"{self._base_url}/settings",
                image="html/images/settings.png",
                style=XMLItemStyle.FOLDER,
            ),
        ]
        return result

    async def _browse_music(
        self,
        subpath: str,
        params: dict,
    ) -> XMLBrowseResult:
        """Music library browse."""
        parts = subpath.split("/")
        if not parts or parts[0] in ("", "index"):
            return await self._browse_music_index()
        if parts[0] == "artists":
            return await self._browse_artists_index(params)
        if parts[0] == "albums":
            return await self._browse_albums_index(params)
        if parts[0] == "genres":
            return await self._browse_genres_index(params)
        if parts[0] == "songs":
            return await self._browse_songs(params)
        return XMLBrowseResult(base_url=self._base_url, title="Music")

    async def _browse_music_index(self) -> XMLBrowseResult:
        return XMLBrowseResult(
            base_url=self._base_url,
            level=1,
            title="Music Library",
            items=[
                XMLItem(
                    text="Artists",
                    href=f"{self._base_url}/music/artists",
                    image="html/images/artists.png",
                    style=XMLItemStyle.FOLDER,
                ),
                XMLItem(
                    text="Albums",
                    href=f"{self._base_url}/music/albums",
                    image="html/images/albums.png",
                    style=XMLItemStyle.FOLDER,
                ),
                XMLItem(
                    text="Genres",
                    href=f"{self._base_url}/music/genres",
                    image="html/images/genres.png",
                    style=XMLItemStyle.FOLDER,
                ),
                XMLItem(
                    text="All Songs",
                    href=f"{self._base_url}/music/songs",
                    image="html/images/songs.png",
                    style=XMLItemStyle.FOLDER,
                ),
                XMLItem(
                    text="New Music",
                    href=f"{self._base_url}/music/newmusic",
                    image="html/images/newmusic.png",
                    style=XMLItemStyle.FOLDER,
                ),
            ],
        )

    async def _browse_artists_index(self, params: dict) -> XMLBrowseResult:
        """Browse artists list."""
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", 50))
        offset = (page - 1) * per_page

        result = XMLBrowseResult(
            base_url=self._base_url,
            level=2,
            title="Artists",
            page=page,
            per_page=per_page,
            window_start=offset,
            window_end=offset + per_page - 1,
        )

        if self._dispatcher:
            raw = await self._dispatcher.query_artists(offset, per_page, {})
            result.items = self._parse_items(raw, "artist")
            result.total_count = self._count_from_raw(raw)

        return result

    async def _browse_albums_index(self, params: dict) -> XMLBrowseResult:
        """Browse albums list."""
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", 50))
        offset = (page - 1) * per_page
        filters = {}
        if params.get("artist_id"):
            filters["artist_id"] = params["artist_id"]
        if params.get("genre_id"):
            filters["genre_id"] = params["genre_id"]

        result = XMLBrowseResult(
            base_url=self._base_url,
            level=2,
            title="Albums",
            page=page,
            per_page=per_page,
        )

        if self._dispatcher:
            raw = await self._dispatcher.query_albums(
                offset, per_page, filters, "aAlL"
            )
            result.items = self._parse_items(raw, "album")
            result.total_count = self._count_from_raw(raw)

        return result

    async def _browse_genres_index(self, params: dict) -> XMLBrowseResult:
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", 50))
        offset = (page - 1) * per_page

        result = XMLBrowseResult(
            base_url=self._base_url, level=2, title="Genres"
        )
        if self._dispatcher:
            raw = await self._dispatcher.query_genres(offset, per_page, {})
            result.items = self._parse_items(raw, "genre")
            result.total_count = self._count_from_raw(raw)
        return result

    async def _browse_songs(self, params: dict) -> XMLBrowseResult:
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", 50))
        offset = (page - 1) * per_page
        filters = {}
        for key in ("artist_id", "album_id", "genre_id", "year"):
            if params.get(key):
                filters[key] = params[key]

        result = XMLBrowseResult(
            base_url=self._base_url, level=2, title="Songs"
        )
        if self._dispatcher:
            raw = await self._dispatcher.query_tracks(
                offset, per_page, filters, "aAlLttd"
            )
            result.items = self._parse_items(raw, "audio")
            result.total_count = self._count_from_raw(raw)
        return result

    async def _browse_artist(
        self,
        artist_id: str,
        params: dict,
    ) -> XMLBrowseResult:
        """Browse an artist's albums."""
        result = XMLBrowseResult(
            base_url=self._base_url, level=3, title="Artist Albums"
        )
        if self._dispatcher and artist_id:
            raw = await self._dispatcher.query_albums(
                0, 100, {"artist_id": artist_id}, "aAlL"
            )
            result.items = self._parse_items(raw, "album")
        return result

    async def _browse_album(
        self,
        album_id: str,
        params: dict,
    ) -> XMLBrowseResult:
        """Browse an album's tracks."""
        result = XMLBrowseResult(
            base_url=self._base_url, level=4, title="Album Tracks"
        )
        if self._dispatcher and album_id:
            raw = await self._dispatcher.query_tracks(
                0, 500, {"album_id": album_id}, "aAlLttd"
            )
            result.items = self._parse_items(raw, "audio")
        return result

    async def _browse_playlist(
        self,
        playlist_id: str,
        params: dict,
    ) -> XMLBrowseResult:
        """Browse a playlist's tracks."""
        result = XMLBrowseResult(
            base_url=self._base_url, level=2, title="Playlist"
        )
        if self._dispatcher and playlist_id:
            raw = await self._dispatcher.query_playlist_tracks(
                playlist_id, 0, 500, "aAlLttd"
            )
            result.items = self._parse_items(raw, "audio")
        return result

    async def _browse_players(
        self,
        player_id: str,
        params: dict,
    ) -> XMLBrowseResult:
        """Browse connected players."""
        result = XMLBrowseResult(
            base_url=self._base_url, level=1, title="Players"
        )
        if self._dispatcher:
            players = await self._dispatcher.list_players()
            for p in players:
                result.items.append(XMLItem(
                    text=p.get("name", p["id"]),
                    subtext=f"{p.get('model', 'unknown')} — {p.get('ip', '')}",
                    href=f"{self._base_url}/player/{p['id']}/status",
                    style=XMLItemStyle.ITEM,
                    id=p["id"],
                ))
        return result

    async def _browse_search(
        self,
        query: str,
        params: dict,
    ) -> XMLBrowseResult:
        """Browse search results."""
        result = XMLBrowseResult(
            base_url=self._base_url, level=1, title=f"Search: {query}"
        )
        # TODO: wire up search
        return result

    async def _browse_status(
        self,
        status_path: str,
        params: dict,
    ) -> XMLBrowseResult:
        """Browse player status."""
        player_id = params.get("player_id")
        result = XMLBrowseResult(
            base_url=self._base_url, level=0, title="Status"
        )
        if self._dispatcher and player_id:
            raw = await self._dispatcher.player_command(player_id, "status", [])
            result.items = self._parse_items(raw, "item")
        return result

    async def _browse_default(
        self,
        path: str,
        params: dict,
    ) -> XMLBrowseResult:
        """Default browse handler."""
        return XMLBrowseResult(
            base_url=self._base_url, title=path or "Browse"
        )

    # -----------------------------------------------------------------------
    # Parsing helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_items(
        lines: list[str],
        style: str,
    ) -> List[XMLItem]:
        """
        Parse CLI-style response lines into XMLItem list.

        Lines are in the format: primary_text [ | field2 | field3 ...]
        """
        items: List[XMLItem] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Skip count/summary lines
            if line.startswith(("artists ", "albums ", "tracks ", "genres ",
                               "playlists ", "years ", "newmusic ")):
                continue
            if line.startswith("serverstatus"):
                continue

            parts = line.split(" | ")
            text = parts[0] if parts else ""
            subtext = " | ".join(parts[1:]) if len(parts) > 1 else ""

            item_style = XMLItemStyle.AUDIO if style == "audio" else XMLItemStyle.ITEM
            items.append(XMLItem(
                text=text,
                subtext=subtext,
                style=item_style,
                href=f"#{style}:{text}",  # placeholder href
            ))
        return items

    @staticmethod
    def _count_from_raw(lines: list[str]) -> int:
        """Extract count from CLI response first line."""
        for line in lines:
            for prefix in ("artists ", "albums ", "tracks ", "genres ",
                          "playlists ", "years ", "newmusic "):
                if line.startswith(prefix):
                    parts = line.split(" ", 1)
                    if len(parts) > 1 and parts[1].isdigit():
                        return int(parts[1])
        return 0

    # -----------------------------------------------------------------------
    # Error handling
    # -----------------------------------------------------------------------

    def _error_xml(self, message: str) -> str:
        """Return a minimal error XML response."""
        root = ET.Element("error")
        ET.SubElement(root, "message").text = message
        return ET.tostring(root, encoding="unicode", xml_declaration=True)


__all__ = [
    "XMLBrowser",
    "XMLItem",
    "XMLBrowseResult",
    "XMLItemStyle",
]
