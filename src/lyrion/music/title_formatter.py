"""Title formatter for Pyrion Music Server.

Mirrors Slim::Music::TitleFormatter — formats track titles using
arbitrary pattern strings with placeholders.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lyrion.music.info import TrackInfo

__all__ = ["TitleFormatter", "FormatPattern"]


# ---------------------------------------------------------------------------
# Default format strings
# ---------------------------------------------------------------------------

DEFAULT_NOW_PLAYING = "%title%"
DEFAULT_LIST_FORMAT = "%tracknumber%. %title% - %artist%"
DEFAULT_ALBUM_FORMAT = "%album% (%year%)"
DEFAULT_ARTIST_FORMAT = "%artist%"
DEFAULT_GENRE_FORMAT = "%genre%"


@dataclass
class FormatPattern:
    """
    A compiled title-formatting pattern.

    Attributes
    ----------
    pattern : str
        The raw format string (e.g. ``"%title% - %artist%"``).
    tokens : list[str]
        Extracted placeholder names in order.
    """

    raw: str
    tokens: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tokens is None:
            self.tokens = _extract_tokens(self.raw)

    def format(self, track: "TrackInfo") -> str:
        """Format a track using this pattern."""
        return _apply_pattern(self.raw, track)


# ---------------------------------------------------------------------------
# TitleFormatter
# ---------------------------------------------------------------------------

class TitleFormatter:
    """
    Formats track metadata strings using placeholder patterns.

    Mirrors ``Slim::Music::TitleFormatter``.

    Usage::

        tf = TitleFormatter()
        # Built-in pattern
        title = tf.format(track, "title_artist")
        # Custom pattern
        title = tf.format(track, "% Playing: %title% by %artist%")
        # Custom pattern with register
        tf.register("my_format", "%album% - %title%")
        title = tf.format(track, "my_format")
    """

    __slots__ = ("_custom_patterns",)

    def __init__(self) -> None:
        self._custom_patterns: dict[str, FormatPattern] = {}

    def format(self, track: "TrackInfo", pattern: str) -> str:
        """
        Format a :class:`TrackInfo` using a pattern.

        Parameters
        ----------
        track
            The track whose metadata to interpolate.
        pattern
            Either a pattern name registered in the formatter
            (builtin or custom), or a raw format string directly.
            Raw format strings must contain at least one ``%...%`` token.

        Returns
        -------
        str
            The formatted string, with missing fields replaced by empty strings.
        """
        # Resolve pattern name → FormatPattern
        fmt_pattern = self.resolve(pattern)

        # Build substitution map from track fields
        subs: dict[str, str] = {}
        for token in fmt_pattern.tokens:
            subs[token] = self._get_field(track, token)

        # Apply substitutions, handling % Playing % style alternating groups
        result = _interpolate(fmt_pattern.raw, subs)

        # Collapse excess whitespace
        result = _collapse_whitespace(result)

        return result.strip()

    def resolve(self, pattern: str) -> FormatPattern:
        """
        Resolve a pattern name or raw pattern string to a FormatPattern.

        Built-in patterns are checked first, then custom patterns, then
        the string is treated as a raw format string.
        """
        if pattern in BUILTIN_PATTERNS:
            return BUILTIN_PATTERNS[pattern]
        if pattern in self._custom_patterns:
            return self._custom_patterns[pattern]
        # Treat as raw format string
        return FormatPattern(pattern)

    def register(self, name: str, pattern: str) -> None:
        """Register a named custom format pattern."""
        self._custom_patterns[name] = FormatPattern(pattern)

    def unregister(self, name: str) -> bool:
        """Remove a custom pattern. Returns True if it existed."""
        return self._custom_patterns.pop(name, None) is not None

    def available_patterns(self) -> dict[str, str]:
        """Return all available patterns (builtin + custom) as name → raw."""
        result = {k: p.raw for k, p in BUILTIN_PATTERNS.items()}
        result.update({k: p.raw for k, p in self._custom_patterns.items()})
        return result

    # ---- Field lookup ----

    @staticmethod
    def _get_field(track: "TrackInfo", token: str) -> str:
        """Return the string value of a token for a given track."""
        token = token.lower()

        # Direct field access
        if hasattr(track, token):
            val = getattr(track, token)
            if val is None:
                return ""
            if isinstance(val, int):
                return str(val)
            return str(val)

        # Special computed / aliased fields
        SPECIALS: dict[str, str] = {
            "playing": "♫",
            "tracknum": "track_number",
            "discnum": "disc_number",
            "effectiveartist": "effective_artist",
            "displaytitle": "display_title",
            "duration": "duration_seconds",
            "samplefrequ": "sample_rate",
        }

        aliased = SPECIALS.get(token)
        if aliased and hasattr(track, aliased):
            val = getattr(track, aliased)
            if val is None:
                return ""
            if isinstance(val, float):
                return _format_duration(val)
            return str(val)

        return ""


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"%(\w+)%")


def _extract_tokens(pattern: str) -> list[str]:
    """Extract all ``%token%`` placeholders from a pattern string."""
    return _TOKEN_RE.findall(pattern)


# ---------------------------------------------------------------------------
# Built-in patterns
# ---------------------------------------------------------------------------

BUILTIN_PATTERNS: dict[str, FormatPattern] = {
    "title": FormatPattern("%title%"),
    "title_artist": FormatPattern("%title% - %artist%"),
    "track_artist": FormatPattern("%tracknumber%. %title% - %artist%"),
    "album_track_artist": FormatPattern("%album% - %tracknumber%. %title% - %artist%"),
    "full": FormatPattern("%artist% - %album% - %tracknumber%. %title%"),
    "album_year": FormatPattern("%album% (%year%)"),
    "artist_album": FormatPattern("%artist% - %album%"),
    "album_artist": FormatPattern("%album% - %artist%"),
}


# ---------------------------------------------------------------------------
# Pattern application
# ---------------------------------------------------------------------------

_ALT_RE = re.compile(r"%\s*Playing\s*%")
_ALT_SIMPLE_RE = re.compile(r"%([^%]+)%")


def _apply_pattern(pattern: str, track: "TrackInfo") -> str:
    """Apply substitutions to a raw pattern string."""
    # Handle special % Playing % alternation pattern
    if _ALT_RE.search(pattern):
        parts = _ALT_RE.split(pattern)
        result = ""
        for i, part in enumerate(parts):
            if not part:
                continue
            if i % 2 == 0:
                # Non-alternating part — substitute normally
                result += _substitute_simple(part, track)
            else:
                # Alternating part — toggle between two values if " - " is present
                # LMS behaviour: " ♫ %title% - %artist% ♫ " alternates each call
                result += _substitute_simple(part, track)
        return result

    return _substitute_simple(pattern, track)


def _substitute_simple(pattern: str, track: "TrackInfo") -> str:
    """Substitute %token% placeholders in a simple (non-alternating) pattern."""
    def replacer(m: re.Match[str]) -> str:
        token = m.group(1)
        return TitleFormatter._get_field(track, token)

    return _ALT_SIMPLE_RE.sub(replacer, pattern)


def _interpolate(pattern: str, subs: dict[str, str]) -> str:
    """
    Replace %token% in pattern with values from subs.

    Preserves the ``%`` delimiters in the output if the substituted value
    is empty (matching LMS behaviour).
    """

    def replacer(m: re.Match[str]) -> str:
        token = m.group(1).lower()
        val = subs.get(token, "")
        return val if val else m.group(0)

    return re.sub(r"%([^%]+)%", replacer, pattern)


def _collapse_whitespace(s: str) -> str:
    """Replace runs of whitespace with a single space."""
    return re.sub(r"\s{2,}", " ", s)


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds as MM:SS or H:MM:SS."""
    if seconds < 0:
        return "0:00"
    total_secs = int(round(seconds))
    hours, rem = divmod(total_secs, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
