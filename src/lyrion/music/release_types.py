"""Album / release type classification for Lyrion Music Server."""
from __future__ import annotations

from enum import Enum, auto


class ReleaseType(Enum):
    """
    Release type classifications mirroring Slim::Music::ReleaseTypes.

    Used to categorise an album as a full album, single, EP, compilation, etc.
    """

    ALBUM = auto()
    SINGLE = auto()
    EP = auto()
    COMPILATION = auto()
    LIVE = auto()
    REMIX = auto()
    DEMO = auto()
    BOOTLEG = auto()
    INTERVIEW = auto()
    CONCERT = auto()
    AUDIOBOOK = auto()
    SPOKENWORD = auto()
    PODCAST = auto()
    MIXTAPE = auto()
    VIDEO_ALBUM = auto()
    UNKNOWN = auto()

    # ---- display helpers ----

    @property
    def label(self) -> str:
        """Human-readable label for UI display."""
        return _LABELS.get(self, self.name.title())

    @property
    def sort_key(self) -> int:
        """Numeric sort key for consistent ordering."""
        return _SORT_KEYS.get(self, 99)

    @property
    def is_special(self) -> bool:
        """True for non-standard release types (compilations, live, remix, etc.)."""
        return self in _SPECIAL_TYPES


# --------------------------------------------------------------------------+
# Static lookup tables                                                         |
# --------------------------------------------------------------------------+

_LABELS: dict[ReleaseType, str] = {
    ReleaseType.ALBUM: "Album",
    ReleaseType.SINGLE: "Single",
    ReleaseType.EP: "EP",
    ReleaseType.COMPILATION: "Compilation",
    ReleaseType.LIVE: "Live",
    ReleaseType.REMIX: "Remix",
    ReleaseType.DEMO: "Demo",
    ReleaseType.BOOTLEG: "Bootleg",
    ReleaseType.INTERVIEW: "Interview",
    ReleaseType.CONCERT: "Concert Recording",
    ReleaseType.AUDIOBOOK: "Audiobook",
    ReleaseType.SPOKENWORD: "Spoken Word",
    ReleaseType.PODCAST: "Podcast",
    ReleaseType.MIXTAPE: "Mixtape",
    ReleaseType.VIDEO_ALBUM: "Video Album",
    ReleaseType.UNKNOWN: "Unknown",
}

# Display ordering (lower = higher priority in lists)
_SORT_KEYS: dict[ReleaseType, int] = {
    ReleaseType.ALBUM: 1,
    ReleaseType.EP: 2,
    ReleaseType.SINGLE: 3,
    ReleaseType.COMPILATION: 4,
    ReleaseType.LIVE: 5,
    ReleaseType.CONCERT: 6,
    ReleaseType.REMIX: 7,
    ReleaseType.DEMO: 8,
    ReleaseType.MIXTAPE: 9,
    ReleaseType.BOOTLEG: 10,
    ReleaseType.INTERVIEW: 11,
    ReleaseType.AUDIOBOOK: 12,
    ReleaseType.SPOKENWORD: 13,
    ReleaseType.PODCAST: 14,
    ReleaseType.VIDEO_ALBUM: 15,
    ReleaseType.UNKNOWN: 99,
}

_SPECIAL_TYPES: set[ReleaseType] = {
    ReleaseType.COMPILATION,
    ReleaseType.LIVE,
    ReleaseType.REMIX,
    ReleaseType.BOOTLEG,
    ReleaseType.MIXTAPE,
    ReleaseType.AUDIOBOOK,
    ReleaseType.SPOKENWORD,
    ReleaseType.PODCAST,
    ReleaseType.VIDEO_ALBUM,
}


# --------------------------------------------------------------------------+
# Detection helpers                                                           |
# --------------------------------------------------------------------------+

def detect_from_metadata(
    album_type: str | None = None,
    compilation: bool = False,
    media: str | None = None,
    comment: str | None = None,
) -> ReleaseType:
    """
    Classify a release from parsed metadata fields.

    Parameters
    ----------
    album_type
        Free-text album type tag (e.g. "album", "single", "live", "remix").
        May come from ID3v2 TXXX:MusicBrainz album type or similar.
    compilation
        True when the compilation flag is set (e.g. ID3v2 TCMP frame).
    media
        The media-type tag (e.g. "CD", "Vinyl", "Digital Media").
    comment
        Free-text comment tag for heuristic clues.

    Returns
    -------
    ReleaseType
    """
    if compilation:
        return ReleaseType.COMPILATION

    if album_type:
        at = album_type.lower()
        if _matches(at, "album", "studio"):
            return ReleaseType.ALBUM
        if _matches(at, "single"):
            return ReleaseType.SINGLE
        if _matches(at, "ep"):
            return ReleaseType.EP
        if _matches(at, "live", "concert recording"):
            return ReleaseType.LIVE
        if _matches(at, "remix"):
            return ReleaseType.REMIX
        if _matches(at, "demo"):
            return ReleaseType.DEMO
        if _matches(at, "bootleg"):
            return ReleaseType.BOOTLEG
        if _matches(at, "interview"):
            return ReleaseType.INTERVIEW
        if _matches(at, "audiobook"):
            return ReleaseType.AUDIOBOOK
        if _matches(at, "spoken word"):
            return ReleaseType.SPOKENWORD
        if _matches(at, "podcast"):
            return ReleaseType.PODCAST
        if _matches(at, "mixtape"):
            return ReleaseType.MIXTAPE
        if _matches(at, "video", "dvd"):
            return ReleaseType.VIDEO_ALBUM

    # Media-based heuristics
    if media:
        md = media.lower()
        if "video" in md:
            return ReleaseType.VIDEO_ALBUM
        if "dvd" in md:
            return ReleaseType.VIDEO_ALBUM

    # Comment-based heuristics (last resort)
    if comment:
        c = comment.lower()
        if "[live]" in c or "live at" in c:
            return ReleaseType.LIVE
        if "[remix]" in c or "remix" in c:
            return ReleaseType.REMIX

    return ReleaseType.ALBUM  # default


def detect_from_folder_name(folder_name: str) -> ReleaseType:
    """
    Classify a release from its containing folder name.

    This mimics the LMS behaviour of scanning folder names for keywords
    that indicate a special release type.
    """
    name_lower = folder_name.lower()

    # Strip disc indicators like "CD1", "Disc 2" for cleaner matching
    import re
    name_lower = re.sub(r"cd\d+", "", name_lower)
    name_lower = re.sub(r"disc\s*\d+", "", name_lower)
    name_lower = re.sub(r"vol\.?\s*\d+", "", name_lower)

    # Check keywords
    if "compilation" in name_lower:
        return ReleaseType.COMPILATION
    if any(k in name_lower for k in ("live", "concert", "in concert")):
        return ReleaseType.LIVE
    if "remix" in name_lower:
        return ReleaseType.REMIX
    if "bootleg" in name_lower:
        return ReleaseType.BOOTLEG
    if "demo" in name_lower:
        return ReleaseType.DEMO
    if "interview" in name_lower:
        return ReleaseType.INTERVIEW
    if "audiobook" in name_lower:
        return ReleaseType.AUDIOBOOK
    if "spoken" in name_lower:
        return ReleaseType.SPOKENWORD
    if "podcast" in name_lower:
        return ReleaseType.PODCAST
    if "mixtape" in name_lower:
        return ReleaseType.MIXTAPE
    if "ep" in name_lower:
        return ReleaseType.EP
    if "single" in name_lower:
        return ReleaseType.SINGLE

    return ReleaseType.UNKNOWN


def _matches(value: str, *keywords: str) -> bool:
    """Return True if value matches any of the given keywords."""
    return any(kw in value for kw in keywords)
