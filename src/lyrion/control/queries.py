"""
Query handler for Pyrion Music Server CLI.

Handles query-style CLI commands (often preceded by '?') that return
library data in Perl's CLI text format: ONE line per answer, the request
terms first, then percent-escaped ``key:value`` pairs, loops unrolled
inline.  See :func:`render_line` for the Perl citations.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from urllib.parse import quote

import orjson

if TYPE_CHECKING:
    # Only used in annotations (deferred by ``from __future__ import
    # annotations``): importing cli at runtime would make the module
    # unimportable on its own (cli imports cli_commands, which imports us).
    from lyrion.control.cli import CLIContext, CLIHandler, ResponseFormat
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
# Perl CLI wire format  (Slim/Control/Stdio.pm + Slim/Control/Request.pm)
# ---------------------------------------------------------------------------
#
# Perl builds every CLI answer from the request object and serialises it as a
# SINGLE escaped line:
#
#   Slim/Plugin/CLI/Plugin.pm:692-698
#     my @elements = $request->renderAsArray();
#     my $output   = Slim::Control::Stdio::array_to_string($request->clientid(), \@elements);
#     ... $output . $connections{$client_socket}{'terminator'}   # LF
#
#   renderAsArray — Slim/Control/Request.pm:2226-2296
#     :2242       the request verbs (command + matched verbs) come first
#     :2245-2255  params: '__*' suppressed, '_*' bare value, else 'key:value'
#     :2258-2293  results: same rules, plus
#                 :2264-2281 key ending in '_loop' → every item of the loop is
#                            unrolled inline (no loop name, no per-item line)
#                 :2284-2286 a plain ARRAY value is joined with ','
#
#   array_to_string — Slim/Control/Stdio.pm:123-138
#     :131  the client id is unshifted in front when it is defined
#     :134  every element is uri_escape_utf8()-escaped (':' → '%3A', ' ' → '%20')
#     :137  elements are joined with a single space
#
# ``?`` semantics — Slim/Control/Request.pm:1021-1061: a query runs only when
# the LAST token is '?'; the '?' occupies the declared parameter slot of the
# request and is consumed without being echoed (:1036), a '_'-named slot echoes
# its token bare (:1027) and every surplus token is echoed as a positional
# parameter ``_p<i>`` (:1059) or, for tag-capable requests, as ``key:value``
# (:1054).

# What URI::Escape::uri_escape_utf8() leaves alone (URI::Escape's default
# unsafe pattern "^A-Za-z0-9\-\._~"): the RFC 3986 unreserved set.
_UNRESERVED = "-._~"


def escape(value: Any) -> str:
    """URI-escape one CLI token exactly like Perl does.

    Perl: ``map { $_ = URI::Escape::uri_escape_utf8($_) } @elements;``
    (``Slim/Control/Stdio.pm:134``).  Live probe 2026-09-12 against the Perl
    LMS (:9090) — ``name%3ASchlafzimmer``, ``uuid%3A`` (empty value),
    ``modelname%3ASB%20Player``, ``name%3AK%EF%BF%BDche`` (UTF-8 bytes).

    ``None`` (Perl ``undef``) renders as the empty string, booleans as
    ``1``/``0`` (Perl numbers).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        value = 1 if value else 0
    return quote(str(value), safe=_UNRESERVED)


def result_tokens(key: str, value: Any) -> list[Any]:
    """Return the CLI tokens one ``(key, value)`` result contributes.

    Perl: ``Slim/Control/Request.pm:2258-2293`` — ``__*`` keys are not output
    (:2261), a key ending in ``_loop`` is unrolled item by item (:2264-2281),
    a plain array is joined with ',' (:2284-2286), a ``_``-prefixed key
    outputs its value alone (:2288-2290), everything else ``key:value``
    (:2291) — without a space, unlike our old ``key: value`` lines.
    """
    if key.startswith("__"):
        return []

    if key.endswith("_loop"):
        tokens: list[Any] = []
        for item in value or ():
            if isinstance(item, dict):
                for sub_key, sub_value in item.items():
                    tokens.extend(result_tokens(sub_key, sub_value))
            else:
                tokens.append(item)
        return tokens

    if isinstance(value, (list, tuple)):
        value = ",".join("" if v is None else str(v) for v in value)

    if value is None:
        value = ""

    if key.startswith("_"):
        return [value]
    return [f"{key}:{value}"]


def render_line(
    clientid: Optional[str] = None,
    terms: Any = (),
    params: Any = (),
    results: Any = (),
) -> str:
    """Render one CLI answer the way Perl does: a single escaped line.

    ``clientid`` is prefixed only when the request resolved to a client
    (``Slim/Control/Stdio.pm:131``), ``terms`` are the request verbs
    (``Slim/Control/Request.pm:2242``), ``params`` the echoed request
    parameters (:2245-2255, see :func:`query_params`) and ``results`` the
    answer fields (:2258-2293).  ``params``/``results`` are sequences of
    ``(key, value)`` pairs, values may be lists (→ ``key:value`` per item for
    ``_loop`` keys).

    Callers pass the *whole* answer, so a CLI handler returns
    ``[render_line(...)]`` — one element, one line.
    """
    elements: list[Any] = []
    if clientid:
        elements.append(clientid)
    elements.extend(terms)
    for key, value in params:
        elements.extend(result_tokens(key, value))
    for key, value in results:
        elements.extend(result_tokens(key, value))
    return " ".join(escape(element) for element in elements)


def query_params(
    tokens: Any,
    declared: Any = (),
    has_tags: bool = True,
) -> list[tuple[str, Any]]:
    """Echo the tokens after the verb the way Perl's parser does.

    Perl splits a request into the matched verbs and everything after them
    (``Slim/Control/Request.pm:1004-1061``).  ``declared`` are the parameter
    names of the matching ``addDispatch`` entry, in order:

    * a ``_``-prefixed name takes the token at its position and is echoed as
      its bare value (:1026-1028) — e.g. ``status <mac> - 1`` echoes
      ``<mac> - 1`` because ``['status','_index','_quantity']`` is declared;
    * the ``?`` name consumes its slot without echoing anything (:1036) —
      that is why ``players ?`` echoes ``players %3F`` (the ``?`` is a surplus
      token there) while ``mixer volume ?`` does not (:523);
    * every surplus token becomes ``key:value`` when the request accepts tags
      and the token contains a colon (``tags:al`` → ``tags%3Aal``, :1051-1054),
      otherwise the positional parameter ``_p<i>`` → bare value (:1059).
    """
    out: list[tuple[str, Any]] = []
    index = 0

    for name in declared:
        if name == "?":
            index += 1  # slot consumed, value dropped (Request.pm:1036)
            continue
        out.append((name if name.startswith("_") else "_" + name,
                    tokens[index] if index < len(tokens) else None))
        index += 1

    for position in range(index, len(tokens)):
        token = tokens[position]
        if has_tags and ":" in token and token.split(":", 1)[0]:
            key, value = token.split(":", 1)
            out.append((key, value))
        else:
            out.append((f"_p{position}", token))

    return out


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

    Responses are ONE percent-escaped line, as in Perl (see
    :func:`render_line`); loops are unrolled inline by
    :meth:`_format_items`.
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
        """Return an empty result set as Perl's single-line answer."""
        return [render_line(terms=[query_name], results=[("count", 0)])]

    def _count_response(self, query_name: str, count: int) -> list[str]:
        return [render_line(terms=[query_name], results=[("count", count)])]

    def _format_items(
        self,
        items: list[dict[str, Any]],
        tag_str: str,
        count_line: str,
        loop_key: str = "item_loop",
    ) -> list[str]:
        """Format a list of items as Perl does: ONE line, loop unrolled.

        Perl never prints a loop name or one line per item — the loop items are
        appended to the same line, each as a run of ``key:value`` tokens
        (``Slim/Control/Request.pm:2264-2281``).  Live Perl probe 2026-09-12
        (:9090): ``artists 0 1 id%3A5620 artist%3A%3F favorites_url%3A…
        count%3A11151`` — ``artists 0 1`` are the request terms, ``count`` the
        last result (``Slim/Control/Queries.pm:989`` adds it after the loop;
        ``playersQuery`` adds it before the loop, ``Queries.pm:2602``).

        ``count_line`` carries the request terms (e.g. ``'artists 0 1'``) as the
        Echo of the request (``Slim/Control/Request.pm:2242``).
        """
        loop = [
            {field: ("" if row.get(field) is None else row.get(field))
             for field in expand_tags(tag_str)}
            for row in items
        ]
        return [
            render_line(
                terms=count_line.split(),
                results=[("count", len(items)), (loop_key, loop)],
            )
        ]


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
    "escape",
    "result_tokens",
    "render_line",
    "query_params",
    "DEFAULT_TAGS",
]
