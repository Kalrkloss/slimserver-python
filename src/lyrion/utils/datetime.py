"""
DateTime utilities for Lyrion Music Server.

Ported from Slim::Utils::DateTime. Provides formatting, parsing,
and localization helpers for timestamps, durations, and date displays.
"""

from __future__ import annotations

import time
import calendar
import re
from datetime import datetime, date, timezone, timedelta
from typing import Any

from lyrion.utils.strings import get_string


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def now() -> datetime:
    """Return current UTC datetime."""
    return datetime.utcnow()


def now_ts() -> float:
    """Return current Unix timestamp (seconds since epoch)."""
    return time.time()


def now_local() -> datetime:
    """Return current local datetime."""
    return datetime.now()


def timestamp_to_datetime(ts: float) -> datetime:
    """Convert Unix timestamp to UTC datetime."""
    return datetime.utcfromtimestamp(ts)


def datetime_to_timestamp(dt: datetime) -> float:
    """Convert datetime to Unix timestamp."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_iso8601(s: str) -> datetime | None:
    """Parse an ISO 8601 date string to datetime."""
    try:
        # Handle various ISO 8601 formats
        s = s.strip()
        # Z suffix
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    try:
        # RFC 3339 / ISO without timezone
        fmt = "%Y-%m-%dT%H:%M:%S"
        if "." in s:
            # Remove microseconds part roughly
            base, _, frac = s.partition(".")
            dt = datetime.strptime(base, fmt)
            return dt.replace(tzinfo=timezone.utc)
        else:
            return datetime.strptime(s, fmt)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def format_duration(seconds: float | int, *, show_hours: bool = True) -> str:
    """
    Format a duration in seconds to a human-readable string.

    Examples:
        format_duration(3661) → "1:01:01"
        format_duration(61) → "1:01"
        format_duration(0) → "0:00"
    """
    total = int(seconds)
    sign = "" if total >= 0 else "-"
    total = abs(total)

    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if show_hours or hours > 0:
        return f"{sign}{hours}:{minutes:02d}:{secs:02d}"
    return f"{sign}{minutes}:{secs:02d}"


def format_duration_long(seconds: float | int) -> str:
    """
    Format a duration as a human-readable phrase.

    Examples:
        format_duration_long(90) → "1 minute 30 seconds"
        format_duration_long(3600) → "1 hour"
    """
    total = int(seconds)
    parts = []
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours > 0:
        parts.append(get_string("HOURS", hours, default=f"{hours} hour{'s' if hours != 1 else ''}"))
    if minutes > 0:
        parts.append(get_string("MINUTES", minutes, default=f"{minutes} minute{'s' if minutes != 1 else ''}"))
    if secs > 0 or not parts:
        parts.append(get_string("SECONDS", secs, default=f"{secs} second{'s' if secs != 1 else ''}"))

    return " ".join(parts)


def parse_duration(s: str) -> float | None:
    """
    Parse a duration string to seconds.

    Accepts: "1:23:45", "23:45", "1h30m", "90m", "3600s", "1.5h"
    """
    s = s.strip().lower()

    # HH:MM:SS or MM:SS
    colon_match = re.match(r"^(\d+):(\d{1,2}):(\d{1,2})$", s)
    if colon_match:
        h, m, sec = int(colon_match[1]), int(colon_match[2]), int(colon_match[3])
        return h * 3600 + m * 60 + sec

    min_sec_match = re.match(r"^(\d+):(\d{1,2})$", s)
    if min_sec_match:
        m, sec = int(min_sec_match[1]), int(min_sec_match[2])
        return m * 60 + sec

    # 1h30m, 90m, 3600s
    h_match = re.match(r"^(\d+(?:\.\d+)?)h$", s)
    if h_match:
        return float(h_match[1]) * 3600

    m_match = re.match(r"^(\d+(?:\.\d+)?)m$", s)
    if m_match:
        return float(m_match[1]) * 60

    s_match = re.match(r"^(\d+(?:\.\d+)?)s?$", s)
    if s_match:
        return float(s_match[1])

    return None


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------

def format_date(dt: datetime | date, format: str = "medium") -> str:
    """
    Format a date for display.

    format: "short", "medium", "long", "full"
    """
    if isinstance(dt, date) and not isinstance(dt, datetime):
        dt = datetime.combine(dt, datetime.min.time())

    if format == "short":
        return dt.strftime("%m/%d/%y")
    elif format == "medium":
        return dt.strftime("%b %d, %Y")
    elif format == "long":
        return dt.strftime("%B %d, %Y")
    elif format == "full":
        return dt.strftime("%A, %B %d, %Y at %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_time_ago(seconds_ago: float) -> str:
    """
    Format a time duration as 'X ago' string.

    Examples: "5 seconds ago", "3 minutes ago", "2 hours ago"
    """
    if seconds_ago < 60:
        return get_string("SECONDS_AGO", int(seconds_ago), default=f"{int(seconds_ago)} seconds ago")
    if seconds_ago < 3600:
        mins = int(seconds_ago / 60)
        return get_string("MINUTES_AGO", mins, default=f"{mins} minute{'s' if mins != 1 else ''} ago")
    if seconds_ago < 86400:
        hours = int(seconds_ago / 3600)
        return get_string("HOURS_AGO", hours, default=f"{hours} hour{'s' if hours != 1 else ''} ago")
    days = int(seconds_ago / 86400)
    return get_string("DAYS_AGO", days, default=f"{days} day{'s' if days != 1 else ''} ago")


def format_date_relative(dt: datetime) -> str:
    """Format a datetime as relative to now."""
    now_dt = datetime.utcnow()
    delta = now_dt - dt
    return format_time_ago(delta.total_seconds())


def format_file_mtime(mtime: float) -> str:
    """Format a file modification time as a date string."""
    dt = datetime.utcfromtimestamp(mtime)
    return dt.strftime("%Y-%m-%d %H:%M")


def format_http_date(dt: datetime | None = None) -> str:
    """Format a datetime as HTTP-date (RFC 7231)."""
    if dt is None:
        dt = datetime.utcnow()
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def parse_http_date(s: str) -> datetime | None:
    """Parse an HTTP-date string (RFC 7231)."""
    formats = [
        "%a, %d %b %Y %H:%M:%S GMT",
        "%a, %d-%b-%Y %H:%M:%S GMT",
        "%a %b %d %H:%M:%S %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def start_of_week(dt: datetime) -> datetime:
    """Return the start of the week (Monday) for a date."""
    weekday = dt.weekday()
    return dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=weekday)


def start_of_month(dt: datetime) -> datetime:
    """Return the first day of the month at midnight."""
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def end_of_month(dt: datetime) -> datetime:
    """Return the last day of the month at 23:59:59."""
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    return dt.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)


def is_same_day(a: datetime, b: datetime) -> bool:
    """Return True if two datetimes are on the same calendar day."""
    return a.year == b.year and a.month == b.month and a.day == b.day


def is_today(dt: datetime) -> bool:
    """Return True if the datetime is today."""
    return is_same_day(dt, datetime.utcnow())


def is_yesterday(dt: datetime) -> bool:
    """Return True if the datetime is yesterday."""
    yesterday = datetime.utcnow() - timedelta(days=1)
    return is_same_day(dt, yesterday)


# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """Return current UTC datetime (aware)."""
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    """Return current local datetime."""
    return datetime.now()


def utc_to_local(utc_dt: datetime) -> datetime:
    """Convert a UTC datetime to local time."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone()


def local_to_utc(local_dt: datetime) -> datetime:
    """Convert a local datetime to UTC."""
    if local_dt.tzinfo is None:
        local_dt = local_dt.astimezone()
    return local_dt.astimezone(timezone.utc)
