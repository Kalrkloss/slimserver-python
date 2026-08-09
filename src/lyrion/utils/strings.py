"""
Internationalization (i18n) strings for Lyrion Music Server.

Provides string lookup with variable substitution, matching the format
used by LMS strings.txt files (Perl-style gettext-like format).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any
from collections import defaultdict

# ---------------------------------------------------------------------------
# LMS strings.txt format parser
# Lyrion uses a key=value format with English as default language:
#   PLUGIN_NAME             Lyrion Music Server
#   BROWSE_ALBUMS           Browse Albums
#   PLAY_TIME               Play time: %1 seconds
# ---------------------------------------------------------------------------

_STRING_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s+(.+)$", re.IGNORECASE)
_PLURAL_RE = re.compile(r"^(\S+)_PLURAL$")
_VAR_RE = re.compile(r"%(\d+)")
_NEWLINE_RE = re.compile(r"\\n")


def parse_strings_file(path: Path) -> dict[str, str]:
    """Parse a strings.txt file and return {key: value} dict."""
    result: dict[str, str] = {}
    if not path.exists():
        return result

    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue

            match = _STRING_RE.match(line)
            if match:
                key = match.group(1)
                value = match.group(2)
                # Unescape newlines
                value = _NEWLINE_RE.sub("\n", value)
                result[key] = value
            else:
                # Try blank-key or continuation
                pass

    return result


# ---------------------------------------------------------------------------
# String registry
# ---------------------------------------------------------------------------

class StringRegistry:
    """
    Thread-safe i18n string registry with language fallback.
    """

    __slots__ = ("_strings", "_language", "_fallback", "_lock")
    _instance: StringRegistry | None = None

    def __init__(self) -> None:
        self._strings: dict[str, dict[str, str]] = defaultdict(dict)
        self._language = "en"
        self._fallback: dict[str, str] = {}
        self._lock = threading.RLock()

    @classmethod
    def instance(cls) -> StringRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_language(self, lang: str) -> None:
        """Set the active language code."""
        self._language = lang

    @property
    def language(self) -> str:
        return self._language

    def load_strings(self, strings: dict[str, str], lang: str = "en") -> None:
        """Load a dictionary of strings for a language."""
        with self._lock:
            self._strings[lang].update(strings)
            if lang == "en":
                self._fallback.update(strings)

    def load_file(self, path: Path, lang: str = "en") -> int:
        """Load strings from a strings.txt file. Returns count of strings loaded."""
        data = parse_strings_file(path)
        self.load_strings(data, lang)
        return len(data)

    def get(self, key: str, default: str | None = None) -> str:
        """
        Get a string by key.

        Falls back: requested language → "en" → key itself.
        """
        with self._lock:
            # Try requested language
            lang_strings = self._strings.get(self._language, {})
            if key in lang_strings:
                return lang_strings[key]
            # Fall back to English
            if key in self._fallback:
                return self._fallback[key]
            # Return default or key
            return default if default is not None else key

    def __contains__(self, key: str) -> bool:
        with self._lock:
            if key in self._strings.get(self._language, {}):
                return True
            return key in self._fallback

    def all_for_lang(self, lang: str) -> dict[str, str]:
        """Return all strings for a language."""
        with self._lock:
            return dict(self._strings.get(lang, {}))


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_registry = StringRegistry.instance()


def get_string(key: str, *args: Any, default: str | None = None) -> str:
    """
    Get a localized string and optionally substitute positional arguments.

    Supports %1, %2, ... substitution:
        get_string("PLAY_TIME", 120)
        # → "Play time: 120 seconds"

    Supports plural forms via _PLURAL suffix:
        get_string("SONG_COUNT", 5, _plural=("song", "songs"))
        # → "5 songs"
    """
    template = _registry.get(key, default or key)

    # Handle plural substitution: %1_PLURAL(("singular", "plural"))
    processed_args: list[Any] = list(args)
    for i, arg in enumerate(processed_args):
        if isinstance(arg, tuple):
            # Plural form hint: use singular or plural based on count
            singular, plural = arg
            if i > 0 and isinstance(processed_args[i - 1], int):
                count = processed_args[i - 1]
                processed_args[i] = singular if count == 1 else plural

    # Perform %N substitution
    if processed_args:
        def replacer(m: re.Match) -> str:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(processed_args):
                return str(processed_args[idx])
            return m.group(0)

        template = _VAR_RE.sub(replacer, template)

    return template


def get_string_plural(
    singular_key: str,
    plural_key: str,
    count: int,
    *args: Any,
) -> str:
    """Get a plural form string based on count."""
    key = plural_key if count != 1 else singular_key
    return get_string(key, count, *args)


def load_language_strings(lang: str, strings_dir: Path | str) -> int:
    """
    Load all strings for a language from a directory.

    Looks for strings.txt in the given directory.
    """
    strings_path = Path(strings_dir) / "strings.txt"
    return _registry.load_file(strings_path, lang)


def set_language(lang: str) -> None:
    """Set the active UI language."""
    _registry.set_language(lang)


def current_language() -> str:
    """Return the active UI language code."""
    return _registry.language


# ---------------------------------------------------------------------------
# String utilities
# ---------------------------------------------------------------------------

def truncate(s: str, max_len: int, suffix: str = "…") -> str:
    """Truncate a string to max_len, adding suffix if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def strip_html(s: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", s)


def normalize_whitespace(s: str) -> str:
    """Normalize whitespace (collapse multiple spaces/tabs to single space)."""
    return re.sub(r"\s+", " ", s).strip()


def pad_right(s: str, width: int, char: str = " ") -> str:
    """Right-pad a string to a given width."""
    return s.ljust(width, char)


def hex_escape(s: str) -> str:
    """Escape non-printable characters as \\xNN sequences."""
    result = []
    for ch in s:
        code = ord(ch)
        if code < 32 or code >= 127:
            result.append(f"\\x{code:02x}")
        else:
            result.append(ch)
    return "".join(result)
