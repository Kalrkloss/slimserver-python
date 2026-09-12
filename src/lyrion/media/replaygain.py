"""ReplayGain-Tags lesen und mungen wie Perl.

Perl ``Slim/Schema.pm:2915-2945`` (``processReplayGainTags``, Tracks) und
``:1299-1322`` (Album-Gain im Album-Hash)::

    $attributes->{$gainTag} = $attributes->{$gainTag}[0] if ref ... eq 'ARRAY';
    $attributes->{$shortTag} =~ s/\\s*dB//gi;
    $attributes->{$shortTag} =~ s/\\s//g;   # bug 15965
    $attributes->{$shortTag} =~ s/,/\\./g;  # bug 6900 (Komma → Punkt)
    # Bug 15483: nicht-numerische Gain-Tags verwerfen
    if ( $attributes->{$shortTag} !~ /^[\\d\\-\\+\\.]+$/ ) { ... delete ... }

Die Tag-Namen sind ``REPLAYGAIN_TRACK_GAIN/PEAK`` bzw.
``REPLAYGAIN_ALBUM_GAIN/PEAK``; mutagen liefert sie je Format unterschiedlich
geschrieben, Perl mappt sie in den Format-Klassen auf diese Namen:
FLAC/Ogg „REPLAY GAIN"/„PEAK LEVEL" (``Formats/FLAC.pm:64-65``,
``Ogg.pm:58-59``), MP3 „MEDIA JUKEBOX: REPLAY GAIN" u. a. (``MP3.pm:55-57``),
WMA ``replaygain_track_gain`` (``WMA.pm:34-36``).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# Perl: ^[\d\-\+\.]+$ (Bug 15483)
_NUMERIC_RE = re.compile(r"^[\d\-\+\.]+$")

# mutagen-Schreibweisen je Gain-Feld, in der Reihenfolge, in der Perl sie
# auflöst (Format-Klassen-Tabellen, s. Modul-Docstring).
TAG_KEYS: dict[str, tuple[str, ...]] = {
    "track_gain": (
        "REPLAYGAIN_TRACK_GAIN", "replaygain_track_gain", "TXXX:REPLAYGAIN_TRACK_GAIN",
        "replay gain", "REPLAY GAIN", "MEDIA JUKEBOX: REPLAY GAIN",
    ),
    "track_peak": (
        "REPLAYGAIN_TRACK_PEAK", "replaygain_track_peak", "TXXX:REPLAYGAIN_TRACK_PEAK",
        "peak level", "PEAK LEVEL", "MEDIA JUKEBOX: PEAK LEVEL",
    ),
    "album_gain": (
        "REPLAYGAIN_ALBUM_GAIN", "replaygain_album_gain", "TXXX:REPLAYGAIN_ALBUM_GAIN",
        "album gain", "MEDIA JUKEBOX: ALBUM GAIN",
    ),
    "album_peak": (
        "REPLAYGAIN_ALBUM_PEAK", "replaygain_album_peak", "TXXX:REPLAYGAIN_ALBUM_PEAK",
        "album peak",
    ),
}


def parse_replaygain_value(raw: Any) -> Optional[float]:
    """Einen ReplayGain-Tag-Wert nach Perl in eine Zahl wandeln.

    ``None`` bedeutet: Tag fehlt/unbrauchbar (Perl löscht ihn und loggt).
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        if not raw:
            return None
        raw = raw[0]
    # mutagen-Objekte (ID3-Frames) exponieren .text
    if hasattr(raw, "text"):
        raw = raw.text
        if isinstance(raw, (list, tuple)):
            if not raw:
                return None
            raw = raw[0]
    text = str(raw).strip()
    if not text:
        return None
    text = re.sub(r"\s*dB", "", text, flags=re.IGNORECASE)   # Schema.pm:2930
    text = re.sub(r"\s", "", text)                            # :2931 (Bug 15965)
    text = text.replace(",", ".")                             # :2932 (Bug 6900)
    if not _NUMERIC_RE.match(text):                           # :2935 (Bug 15483)
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _lookup(tags: Any, keys: Iterable[str]) -> Any:
    """Ersten belegten Tag-Wert finden — exakt, dann ohne Groß/Klein.

    Der Fallback ist nötig, weil echte Dateien die Schreibweise mischen:
    gefunden in der Bibliothek des Users als ``TXXX:replaygain_track_gain``
    (ID3-TXXX-Description klein geschrieben), während Perls MP3-Tabelle
    ``MEDIA JUKEBOX: REPLAY GAIN`` o. ä. führt (``Formats/MP3.pm:55-57``).
    Perl normalisiert die Beschreibung je Format; wir vergleichen deshalb
    zusätzlich case-insensitiv, aber erst nach dem exakten Treffer.
    """
    if not tags:
        return None
    def _value(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, (list, tuple)) and not val:
            return None
        return val

    for key in keys:
        try:
            val = _value(tags.get(key))
        except Exception:  # noqa: BLE001 — fremde Tag-Container
            continue
        if val is not None:
            return val
    # Fallback: case-insensitiver Vergleich (echte TXXX-Schreibweisen).
    try:
        lowered = {str(k).lower(): k for k in tags.keys()}
    except Exception:  # noqa: BLE001
        return None
    for key in keys:
        real = lowered.get(key.lower())
        if real is None:
            continue
        try:
            val = _value(tags.get(real))
        except Exception:  # noqa: BLE001
            continue
        if val is not None:
            return val
    return None


def extract_replaygain(tags: Any) -> tuple[Optional[float], Optional[float],
                                           Optional[float], Optional[float]]:
    """(track_gain, track_peak, album_gain, album_peak) aus einem Tag-Container."""
    return (
        parse_replaygain_value(_lookup(tags, TAG_KEYS["track_gain"])),
        parse_replaygain_value(_lookup(tags, TAG_KEYS["track_peak"])),
        parse_replaygain_value(_lookup(tags, TAG_KEYS["album_gain"])),
        parse_replaygain_value(_lookup(tags, TAG_KEYS["album_peak"])),
    )
