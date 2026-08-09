"""
Query handler for Lyrion Music Server CLI.

Handles query-style CLI commands (often preceded by '?') that return
library data in a structured, line-oriented format.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

import orjson

from lyrion.control.cli import CLIContext, CLIHandler, ResponseFormat

if False:
    from lyrion.control.request import RequestDispatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query registry
# ---------------------------------------------------------------------------

_QUERY_HANDLERS: dict[str, Callable[..., Awaitable[list[str]]]] = {}


def register_query(
    name: str,
) -> Callable[[Callable[..., Awaitable[list[str]]]], Callable[..., Awaitable[list[str]]]]:
    """Decorator to register a query handler."""

    def decorator(
        func: Callable[..., Awaitable[list[str]]],
    ) -> Callable[..., Awaitable[list[str]]]:
        _QUERY_HANDLERS[name] = func
        return func

    return decorator


# ---------------------------------------------------------------------------
# Tag string utilities
# ---------------------------------------------------------------------------

# LMS tag field map — maps CLI tag chars to DB column names
_TAG_MAP: dict[str, str] = {
    "a": "album",
    "A": "album_id",
    "l": "artist",
    "L": "artist_id",
    "t": "title",
    "T": "tracknum",
    "d": "duration",
    "D": "disc",
    "y": "year",
    "g": "genre",
    "G": "genre_id",
    "c": "composer",
    "C": "comment",
    "b": "bitrate",
    "B": "bins",
    "r": "samplerate",
    "R": "rating",
    "s": "size",
    "S": "suffix",
    "Z": "filesize",
    "p": "path",
    "P": "playcount",
    "u": "lastplayed",
    "i": "artwork_path",
    "I": "track_id",
    "k": "keywords",
    "x": "extid",
    "o": "added",
    "O": "updated",
    "M": "modificationTime",
    "e": "album_replay_gain",
    "E": "track_replay_gain",
    "N": "bpm",
    "Y": "lyrics",
    "n": "ln",
    "m": "media",
    "f": "lyrics",
    "q": "lyrics",
    "H": "playback_order",
    "h": "samplesize",
    "v": "dlna",
    "w": "dlna_explicit",
    "j": "conductor",
    "0": "work",
    "1": "movement",
    "2": "movementnumber",
    "3": "discc",
    "4": "originalyear",
    "5": "originalartist",
    "6": "remixer",
    "7": "isrc",
    "8": "mood",
    "9": "catalog",
    "!": "db",
    "@": "coverid",
    "=": "artwork_front",
    "?": "artwork_track",
    "~": "lyrics_format",
    "+": "url",
    "*": "replay_gain",
    "%": "lossless",
    "^": "lyrics_sync",
    ">": "lyrics_language",
    "$": "sort_key",
    "(": "genre_ex",
    ")": "genre_ex",
    "|": "remote",
    "\\": "remote_key",
    "]": "playable",
    "[": "type",
    "{": "label",
    "}": "language",
    "<": "country",
    "F": "fulltext",
    "W": "work_performance",
    "V": "instrument",
    "X": "discs",
    "n": "track_title_sort",
    "K": "genre_list",
    "z": "album_list",
    "j": "contributor_list",
}

# Default tags for CLI responses
DEFAULT_TAGS = "aAyAlLttdDygGcC"


def expand_tags(tag_str: str) -> list[str]:
    """Expand a tag string into individual field identifiers."""
    fields: list[str] = []
    for c in tag_str:
        if c == "K":
            fields.extend(["genre", "genre_id"])
        elif c == "z":
            fields.extend(["album", "album_id"])
        elif c == "j":
            fields.extend(["contributor", "contributor_id"])
        elif c == "n":
            fields.append("tracknum")
        elif c == "l":
            fields.append("ln")
        elif c in _TAG_MAP:
            fields.append(_TAG_MAP[c])
        else:
            fields.append(c)
    return fields


def format_tags(
    row: dict[str, Any],
    tag_str: str,
    separator: str = " | ",
) -> str:
    """Format a database row as a delimited tag string."""
    fields = expand_tags(tag_str)
    values: list[str] = []
    for f in fields:
        val = row.get(f, "")
        values.append(str(val) if val is not None else "")
    return separator.join(values)


# ---------------------------------------------------------------------------
# Query handler
# ---------------------------------------------------------------------------


class QueryHandler:
    """
    Handles CLI query commands (the '?' style and direct query names).

    Query commands are of the form::

        artists 0 100
        albums 0 100 tags:aAlL
        tracks 0 100 genre_id:5 artist_id:10

    Responses are one line per item, blank line terminated.
    """

    # Regex to parse filter param: field:value or field:value:value...
    RE_FILTER = re.compile(r"^(\w+):(.+)$")

    def __init__(self, dispatcher: Optional["RequestDispatcher"] = None) -> None:
        self._dispatcher = dispatcher

    def set_dispatcher(self, dispatcher: "RequestDispatcher") -> None:
        self._dispatcher = dispatcher

    # -----------------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------------

    async def handle_query(
        self,
        ctx: CLIContext,
        cmd: str,
        args: list[str],
    ) -> list[str]:
        """
        Dispatch a query command.

        Args:
            ctx: CLI session context.
            cmd: Query name (e.g. "albums", "tracks").
            args: Command arguments, e.g. ["0", "100", "genre_id:5"]

        Returns:
            Response lines.
        """
        # Normalise: strip leading '?' if present
        cmd = cmd.lstrip("?").lower()

        if cmd in _QUERY_HANDLERS:
            handler = _QUERY_HANDLERS[cmd]
            try:
                return await handler(self, ctx, args)
            except Exception as exc:
                logger.exception("Query %s raised: %s", cmd, exc)
                return [f"query error: {exc}"]

        # Built-in fallbacks
        method = f"query_{cmd}"
        if hasattr(self, method):
            return await getattr(self, method)(ctx, args)

        return [f"unknown query: {cmd}"]

    # -----------------------------------------------------------------------
    # Pagination helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def parse_pagination(args: list[str]) -> tuple[int, int, str]:
        """
        Parse offset, limit from the start of args.

        Also handles 'chunk:nnnnn' which specifies a page window.
        Returns (offset, limit, chunk_id).
        """
        offset = 0
        limit = 100
        chunk_id = ""
        remaining: list[str] = []
        for arg in args:
            if arg.startswith("chunk:"):
                chunk_id = arg[6:]
            elif offset == 0 and arg.isdigit():
                offset = int(arg)
            elif offset > 0 and limit == 100 and arg.isdigit():
                limit = int(arg)
            else:
                remaining.append(arg)
        return offset, limit, chunk_id

    @staticmethod
    def parse_filters(args: list[str]) -> dict[str, str]:
        """Parse field:value filter arguments."""
        filters: dict[str, str] = {}
        for arg in args:
            m = QueryHandler.RE_FILTER.match(arg)
            if m:
                filters[m.group(1)] = m.group(2)
        return filters

    # -----------------------------------------------------------------------
    # Built-in queries (sample implementations)
    # -----------------------------------------------------------------------

    async def query_artists(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """
        artists [0 <limit>] [search:<term>]
        Return artists from the library.
        """
        offset, limit, _ = self.parse_pagination(args)
        filters = self.parse_filters(args)

        if self._dispatcher:
            return await self._dispatcher.query_artists(offset, limit, filters)
        return self._empty_response("artists")

    async def query_albums(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """
        albums [0 <limit>] [tags:<tags>] [artist_id:<id>] [genre_id:<id>] [search:<term>]
        """
        offset, limit, _ = self.parse_pagination(args)
        filters = self.parse_filters(args)
        tag_str = filters.pop("tags", DEFAULT_TAGS)

        if self._dispatcher:
            return await self._dispatcher.query_albums(
                offset, limit, filters, tag_str
            )
        return self._empty_response("albums")

    async def query_tracks(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """
        tracks [0 <limit>] [genre_id:<id>] [artist_id:<id>] [album_id:<id>]
                [year:<yyyy>] [search:<term>] [tags:<tags>]
        """
        offset, limit, _ = self.parse_pagination(args)
        filters = self.parse_filters(args)
        tag_str = filters.pop("tags", DEFAULT_TAGS)

        if self._dispatcher:
            return await self._dispatcher.query_tracks(
                offset, limit, filters, tag_str
            )
        return self._empty_response("tracks")

    async def query_genres(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """genres [0 <limit>] [search:<term>]"""
        offset, limit, _ = self.parse_pagination(args)
        filters = self.parse_filters(args)

        if self._dispatcher:
            return await self._dispatcher.query_genres(offset, limit, filters)
        return self._empty_response("genres")

    async def query_years(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """years [0 <limit>]"""
        offset, limit, _ = self.parse_pagination(args)

        if self._dispatcher:
            return await self._dispatcher.query_years(offset, limit)
        return self._empty_response("years")

    async def query_playlists(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """playlists [0 <limit>] [search:<term>]"""
        offset, limit, _ = self.parse_pagination(args)
        filters = self.parse_filters(args)

        if self._dispatcher:
            return await self._dispatcher.query_playlists(offset, limit, filters)
        return self._empty_response("playlists")

    async def query_playlisttracks(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """playlisttracks <playlist_id> [0 <limit>] [tags:<tags>]"""
        if not args:
            return ["playlisttracks: "]
        playlist_id = args[0]
        offset, limit, _ = self.parse_pagination(args[1:])
        tag_str = self.parse_filters(args).pop("tags", DEFAULT_TAGS)

        if self._dispatcher:
            return await self._dispatcher.query_playlist_tracks(
                playlist_id, offset, limit, tag_str
            )
        return self._empty_response("playlisttracks")

    async def query_newmusic(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """newmusic [0 <limit>]"""
        offset, limit, _ = self.parse_pagination(args)

        if self._dispatcher:
            return await self._dispatcher.query_new_music(offset, limit)
        return self._empty_response("newmusic")

    async def query_rescanprogress(
        self,
        ctx: CLIContext,
        args: list[str],
    ) -> list[str]:
        """rescanprogress — return current rescan progress."""
        if self._dispatcher:
            return await self._dispatcher.rescan_progress()
        return ["rescanprogress: 0 done 0 0"]

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _empty_response(self, query_name: str) -> list[str]:
        """Return an empty result set."""
        return [f"{query_name} 0", ""]

    def _count_response(self, query_name: str, count: int) -> list[str]:
        return [f"{query_name} {count}", ""]

    def _format_items(
        self,
        items: list[dict[str, Any]],
        tag_str: str,
        count_line: str,
    ) -> list[str]:
        """Format a list of items as CLI response lines."""
        lines = [count_line]
        for item in items:
            lines.append(format_tags(item, tag_str))
        lines.append("")
        return lines


# ---------------------------------------------------------------------------
# Built-in query registrations
# ---------------------------------------------------------------------------

# The QueryHandler class handles all queries via query_<name> methods,
# so we don't need additional decorators here. The handlers are
# registered in __init__ by importing cli_commands.


__all__ = [
    "QueryHandler",
    "register_query",
    "expand_tags",
    "format_tags",
    "DEFAULT_TAGS",
]
