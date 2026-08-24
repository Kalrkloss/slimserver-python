"""
Input validation utilities for Pyrion Music Server.

Ported from Slim::Utils::Validate. Provides validators for common
data types and patterns used throughout the LMS codebase.
"""

from __future__ import annotations

import re
import ipaddress
from typing import Any, Callable, Protocol
from pathlib import Path


# ---------------------------------------------------------------------------
# Validator protocol
# ---------------------------------------------------------------------------

class Validator(Protocol):
    """Protocol for validation functions."""
    def __call__(self, value: Any) -> bool: ...


def is_valid(value: Any, validator: Validator) -> bool:
    """Return True if value passes the validator."""
    try:
        return bool(validator(value))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Scalar validators
# ---------------------------------------------------------------------------

def is_int(value: Any) -> bool:
    """Return True if value is a valid integer."""
    try:
        int(value)
        return True
    except (ValueError, TypeError):
        return False


def is_float(value: Any) -> bool:
    """Return True if value is a valid float."""
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def is_bool(value: Any) -> bool:
    """Return True if value is a boolean or bool-like string."""
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.lower() in {"0", "1", "yes", "no", "true", "false", "on", "off"}
    return False


def is_string(value: Any) -> bool:
    """Return True if value is a non-empty string."""
    return isinstance(value, str) and bool(value.strip())


def is_empty(value: Any) -> bool:
    """Return True if value is None or empty string/list/dict."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple, dict, set, frozenset)) and not value:
        return True
    return False


# ---------------------------------------------------------------------------
# Pattern validators
# ---------------------------------------------------------------------------

def is_email(value: str) -> bool:
    """Return True if value is a valid email address."""
    if not isinstance(value, str):
        return False
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, value))


def is_url(value: str) -> bool:
    """Return True if value is a valid HTTP/HTTPS URL."""
    if not isinstance(value, str):
        return False
    pattern = r"^https?://[^\s/$.?#].[^\s]*$"
    return bool(re.match(pattern, value)) or value.startswith("file://")


def is_uuid(value: str) -> bool:
    """Return True if value is a valid UUID string."""
    if not isinstance(value, str):
        return False
    pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    return bool(re.match(pattern, value.lower()))


def is_musicbrainz_id(value: str) -> bool:
    """Return True if value is a valid MusicBrainz ID."""
    if not isinstance(value, str):
        return False
    return bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value.lower()))


def is_ip_address(value: str) -> bool:
    """Return True if value is a valid IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def is_ipv4(value: str) -> bool:
    """Return True if value is a valid IPv4 address."""
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def is_ipv6(value: str) -> bool:
    """Return True if value is a valid IPv6 address."""
    try:
        ipaddress.IPv6Address(value)
        return True
    except ValueError:
        return False


def is_port(value: Any) -> bool:
    """Return True if value is a valid TCP/UDP port number."""
    try:
        port = int(value)
        return 1 <= port <= 65535
    except (ValueError, TypeError):
        return False


def is_mac_address(value: str) -> bool:
    """Return True if value looks like a MAC address."""
    if not isinstance(value, str):
        return False
    # Accept : or - separators
    pattern = r"^([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}$"
    return bool(re.match(pattern, value))


# ---------------------------------------------------------------------------
# File/path validators
# ---------------------------------------------------------------------------

def is_file_path(value: str) -> bool:
    """Return True if value is a readable file path."""
    try:
        p = Path(value).expanduser()
        return p.is_file()
    except OSError:
        return False


def is_dir_path(value: str) -> bool:
    """Return True if value is a readable directory path."""
    try:
        p = Path(value).expanduser()
        return p.is_dir()
    except OSError:
        return False


def is_absolute_path(value: str) -> bool:
    """Return True if value is an absolute path."""
    try:
        return Path(value).is_absolute()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Audio metadata validators
# ---------------------------------------------------------------------------

def is_duration(value: Any) -> bool:
    """Return True if value is a valid duration in seconds (non-negative float)."""
    try:
        d = float(value)
        return d >= 0
    except (ValueError, TypeError):
        return False


def is_bitrate(value: Any) -> bool:
    """Return True if value is a valid audio bitrate (kbps)."""
    try:
        b = int(value)
        return b > 0 and b <= 5120  # Max ~5 Mbps
    except (ValueError, TypeError):
        return False


def is_samplerate(value: Any) -> bool:
    """Return True if value is a valid audio sample rate."""
    try:
        s = int(value)
        return s in {
            8000, 11025, 16000, 22050, 24000,
            32000, 44100, 48000, 88200, 96000,
            176400, 192000, 352800, 384000,
        }
    except (ValueError, TypeError):
        return False


def is_content_type(value: str) -> bool:
    """Return True if value is a recognized audio/video MIME type."""
    valid = {
        "audio/mpeg", "audio/mp3", "audio/flac", "audio/ogg",
        "audio/aac", "audio/mp4", "audio/x-m4a",
        "audio/wav", "audio/wave", "audio/x-wav",
        "audio/aiff", "audio/x-aiff",
        "audio/opus", "audio/oga",
        "video/mp4", "video/mpeg", "video/ogg",
        "application/ogg",
    }
    return value.lower() in valid


def is_audio_file_extension(value: str) -> bool:
    """Return True if extension is a recognized audio format."""
    valid = {
        "mp3", "flac", "ogg", "oga", "opus",
        "m4a", "aac", "mp4", "alac",
        "wav", "aiff", "aif",
        "wma", "wmv", "asf",
        "spc", "ay", "gbs", "gym", "hes", "kss",
        "nsf", "nsfe", "sap", "snd", "vgm", "vgz",
        "mod", "xm", "s3m", "it", "mtm",
        "shn", "wv", "tta", "ape",
    }
    ext = value.lower().lstrip(".")
    return ext in valid


# ---------------------------------------------------------------------------
# Composite validators
# ---------------------------------------------------------------------------

def matches_regex(pattern: str, flags: int = 0) -> Callable[[str], bool]:
    """Return a validator that matches a regex pattern."""
    compiled = re.compile(pattern, flags)
    def validator(value: str) -> bool:
        return bool(compiled.match(str(value)))
    return validator


def in_range(min_val: float, max_val: float) -> Callable[[float], bool]:
    """Return a validator that checks if a number is in range."""
    def validator(value: Any) -> bool:
        try:
            v = float(value)
            return min_val <= v <= max_val
        except (ValueError, TypeError):
            return False
    return validator


def one_of(choices: list[Any]) -> Callable[[Any], bool]:
    """Return a validator that checks if value is in choices."""
    def validator(value: Any) -> bool:
        return value in choices
    return validator


def length_between(min_len: int, max_len: int) -> Callable[[str], bool]:
    """Return a validator that checks string length."""
    def validator(value: Any) -> bool:
        try:
            return min_len <= len(str(value)) <= max_len
        except (ValueError, TypeError):
            return False
    return validator


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe in filenames."""
    # Remove path separators and null bytes
    name = name.replace("/", "_").replace("\\", "_")
    name = name.replace("\x00", "")
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def coerce_to_string(value: Any, default: str = "") -> str:
    """Coerce a value to string, returning default on failure."""
    try:
        return str(value) if value is not None else default
    except Exception:
        return default


def coerce_to_int(value: Any, default: int = 0) -> int:
    """Coerce a value to int, returning default on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def coerce_to_float(value: Any, default: float = 0.0) -> float:
    """Coerce a value to float, returning default on failure."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
