"""ReplayGain: Tag-Munging und Scanner-Extraktion (Perl Schema.pm:2915-2945).

Perl-Kette:
* Tag-Namen ``REPLAYGAIN_TRACK_GAIN/PEAK`` und ``REPLAYGAIN_ALBUM_GAIN/PEAK``,
  je Format anders geschrieben (FLAC/Ogg „REPLAY GAIN"/„PEAK LEVEL",
  ``Formats/FLAC.pm:64-65``, ``Ogg.pm:58-59``; MP3 „MEDIA JUKEBOX: REPLAY GAIN",
  ``MP3.pm:55-57``; WMA ``replaygain_track_gain``, ``WMA.pm:34-36``).
* Wert-Munging (``Slim/Schema.pm:2925-2940``): Array → erster Wert,
  ``s/\\s*dB//gi``, ``s/\\s//g``, ``s/,/\\./g``, dann nur behalten wenn
  ``^[\\d\\-\\+\\.]+$`` (Bug 15483).
* Album-Werte werden genauso gemungt (:1299-1322) und laut Kommentar dort
  aktualisiert, wenn sie sich ändern.
"""

from __future__ import annotations

from typing import Any

from lyrion.media.replaygain import extract_replaygain, parse_replaygain_value
from lyrion.media.scanner import MediaScanner


# ── Wert-Munging ───────────────────────────────────────────────────────────


def test_plain_values():
    assert parse_replaygain_value("-7.32 dB") == -7.32
    assert parse_replaygain_value("+2.00 dB") == 2.0
    assert parse_replaygain_value("0") == 0.0
    assert parse_replaygain_value("  -3.5dB ") == -3.5


def test_perl_bug_fixes():
    # Bug 6900: Komma als Dezimaltrenner
    assert parse_replaygain_value("1,5 dB") == 1.5
    # Bug 15965: Leerzeichen mitten im Wert
    assert parse_replaygain_value("- 7.32 dB") == -7.32


def test_arrays_take_the_first_value():
    assert parse_replaygain_value(["-7.32 dB", "-9 dB"]) == -7.32
    assert parse_replaygain_value(["-6.0 dB"]) == -6.0


def test_invalid_values_are_dropped():
    # Bug 15483: nicht-numerische Tags werden verworfen
    assert parse_replaygain_value("abc") is None
    assert parse_replaygain_value("") is None
    assert parse_replaygain_value(None) is None
    assert parse_replaygain_value([]) is None
    # Grenzfall: besteht Perls Zeichen-Regex, ist aber keine Zahl — wir
    # verwerfen statt eine kaputte Zahl in die DB zu schreiben.
    assert parse_replaygain_value("1.2.3 dB") is None


# ── Tag-Schreibweisen ──────────────────────────────────────────────────────


def test_extract_replaygain_reads_the_format_spellings():
    flac = {"replay gain": "-6.00 dB", "peak level": "0.98765"}
    assert extract_replaygain(flac) == (-6.0, 0.98765, None, None)

    mp3 = {"MEDIA JUKEBOX: REPLAY GAIN": "-3.21 dB",
           "MEDIA JUKEBOX: ALBUM GAIN": "-4.00 dB",
           "MEDIA JUKEBOX: PEAK LEVEL": "1.0000"}
    assert extract_replaygain(mp3) == (-3.21, 1.0, -4.0, None)

    wma = {"replaygain_track_gain": "-2.50 dB",
           "replaygain_track_peak": "0.95",
           "replaygain_album_gain": "-2.00 dB",
           "replaygain_album_peak": "0.98"}
    assert extract_replaygain(wma) == (-2.5, 0.95, -2.0, 0.98)

    id3 = {"TXXX:REPLAYGAIN_TRACK_GAIN": "-5.00 dB"}
    assert extract_replaygain(id3)[0] == -5.0

    # Echtfund aus der Bibliothek des Users (ID3-TXXX-Beschreibung klein):
    # "TXXX:replaygain_track_gain": "-6.60 dB"
    real = {"TXXX:replaygain_track_gain": "-6.60 dB"}
    assert extract_replaygain(real)[0] == -6.6


def test_extract_replaygain_without_tags():
    assert extract_replaygain(None) == (None, None, None, None)
    assert extract_replaygain({}) == (None, None, None, None)


# ── Scanner-Extraktion (mutagen-Ersatz) ────────────────────────────────────


class _FakeInfo:
    length = 180.0
    bitrate = 320000
    sample_rate = 44100
    channels = 2


class _FakeAudio:
    def __init__(self, tags: Any) -> None:
        self.tags = tags
        self.info = _FakeInfo()

    def close(self) -> None:
        return None


def test_scanner_extracts_replaygain(monkeypatch, tmp_path):
    tags = {
        "TIT2": ["Testtitel"],
        "TPE1": ["Testartist"],
        "TALB": ["Testalbum"],
        "REPLAYGAIN_TRACK_GAIN": ["-7.32 dB"],
        "REPLAYGAIN_TRACK_PEAK": ["0.9888"],
        "REPLAYGAIN_ALBUM_GAIN": ["-6.00 dB"],
        "REPLAYGAIN_ALBUM_PEAK": ["0.99"],
    }
    import lyrion.media.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "MutagenFile", lambda path: _FakeAudio(tags))
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fLaC")

    (title, artist, album, genre, year, track, duration, bitrate,
     sample_rate, channels, rg_gain, rg_peak, rg_album_gain,
     rg_album_peak) = MediaScanner()._extract_tags(audio)

    assert title == "Testtitel" and album == "Testalbum"
    assert (rg_gain, rg_peak) == (-7.32, 0.9888)
    assert (rg_album_gain, rg_album_peak) == (-6.0, 0.99)


def test_scanner_without_replaygain_tags(monkeypatch, tmp_path):
    import lyrion.media.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "MutagenFile",
                        lambda path: _FakeAudio({"TIT2": ["x"]}))
    audio = tmp_path / "plain.mp3"
    audio.write_bytes(b"ID3")

    values = MediaScanner()._extract_tags(audio)
    assert values[10:] == (None, None, None, None)
