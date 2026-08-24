"""Metadata heuristics for music files with sparse/inconsistent tags.

Sources, in priority order (first non-empty wins per field):
1. Embedded tags (id3/Vorbis/MP4 — extracted by the scanner)
2. Folder structure: Genre/Year - Artist/Album or Artist/Album layouts
3. Filename patterns: "Artist - Track", "NN. Artist - Title", "NN - Title",
   "NN_Title", "Title" etc.
4. Last-resort fallbacks (parent folder as album, "Unknown Artist"…)

The heuristics only FILL missing fields — explicit tags always win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ── Filename patterns ─────────────────────────────────────────────────

# "01 - Artist - Title.ext" / "01. Artist - Title" / "01_Artist - Title"
_RE_NUM_ARTIST_TITLE = re.compile(
    r"^\s*(?P<num>\d{1,3})\s*[-_.)\]]+\s*(?P<artist>[^-_]+?)\s+-\s+(?P<title>.+?)\s*$"
)
# "01 - Title.ext" (no artist in the name)
_RE_NUM_TITLE = re.compile(
    r"^\s*(?P<num>\d{1,3})\s*[-_.)\]]+\s*(?P<title>.+?)\s*$"
)
# "Artist - Title.ext" (artist contains no track number)
_RE_ARTIST_TITLE = re.compile(
    r"^\s*(?P<artist>[^-_]+?)\s+-\s+(?P<title>.+?)\s*$"
)
# "(NN) Title" / "NN Title" prefixes without separator
_RE_NUM_PREFIX = re.compile(r"^\s*\(?(?P<num>\d{1,3})\)?[\s._-]+(?P<rest>.+)$")

# Common noise tokens in release names (stripped from album guesses)
_NOISE_TOKENS = re.compile(
    r"[\(\[](?:va|various[\s_-]*artists|cd|disc|web|cda|ep|single|remastered|"
    r"reissue|deluxe|limited[\s_-]*edition|bonus[\s_-]*(?:cd|disc|tracks?)|"
    r"explicit|bootleg|promo|split|mixtape|vinyl|rip|mp3|320\s*kbps|"
    r"192\s*kbps|vbr|lossless|flac|final|proper|repack|internal|"
    r"\d{1,2}\s*cd|\d{4})[\)\]]",
    re.IGNORECASE,
)
# Trailing " - CD1", " (CD 2)", " [Disc 1]" etc. on album names
_RE_DISC_SUFFIX = re.compile(
    r"\s*[-_ ]*\(?[\(\[]?\s*(?:cd|disc)\s*\d+\s*\)?[\)\]]?\s*$", re.IGNORECASE)
# Leading "VA - ", "Various - ", "VA_" release-style prefixes on folders
_RE_VA_PREFIX = re.compile(
    r"^\s*(?:va|various[\s_-]*artists|ost|soundtrack)\s*[-_. ]+\s*", re.IGNORECASE)
# "Artist - Album (2004)" → year in parentheses at the end
_RE_TRAILING_YEAR = re.compile(r"[\(\[\s](\d{4})[\)\]\s]*$")
# "01-artist-album-2008-cover" release-folder style
_RE_RELEASE_FOLDER = re.compile(
    r"^(?P<artist>.+?)(?:\s*[-_]\s*)+(?P<album>.+?)(?:\s*[-_]\s*)?(?P<year>\d{4})?"
    r"(?:\s*[-_]\s*(?:web|cda|cd|ep|single|bootleg|promo|remastered|reissue|"
    r"deluxe|limited|vinyl|lossless|flac|mp3|proper|repack|internal|final).*)?$",
    re.IGNORECASE,
)


@dataclass
class GuessedMeta:
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: int = 0
    track: int = 0
    # Which source filled each field ("tag" | "folder" | "filename" | "fallback")
    sources: dict = field(default_factory=dict)


def _clean(s: str) -> str:
    """Normalize whitespace. Underscores become spaces only when the string
    has no spaces at all (pure filename style 'Artist_Name-Title'); strings
    with real spaces keep their underscores (AC_DC, Guns_N'_Roses)."""
    s = str(s or "").strip()
    if " " not in s and "_" in s:
        s = s.replace("_", " ")
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s.strip("\"'").strip()


def _clean_album(s: str) -> str:
    """Album-name cleanup: strip noise tokens, trailing disc markers and
    glued years ("Miroque Vol. XV-2008" → "Miroque Vol. XV")."""
    s = _clean(s)
    s = _NOISE_TOKENS.sub("", s)
    s = _RE_DISC_SUFFIX.sub("", s)
    s = re.sub(r"[\s_-]+(?:19|20)\d{2}\s*$", "", s)  # trailing -YYYY
    s = re.sub(r"\s{2,}", " ", s).strip(" -_")
    return s


def _parse_year(s: str) -> int:
    m = re.search(r"(19|20)\d{2}", str(s or ""))
    return int(m.group(0)) if m else 0


# ── Folder heuristics ─────────────────────────────────────────────────

def guess_from_folders(parts: list[str]) -> dict:
    """Derive metadata from the folder hierarchy.

    ``parts``: path parts between the library root and the file's folder,
    outermost first (e.g. ["Metal", "Rage - 1996", "End of All Days"]).

    Recognized layouts (checked per folder, most specific wins):
      "Artist - Album (YYYY)" / "Artist - YYYY - Album" / "Artist - Album"
      "YYYY - Artist - Album"
      "Album (YYYY)" / "Album YYYY" / plain album name
      Genre as top-level folder (matched against a common-genre list or
      a single word folder directly above an artist-ish folder).
    """
    out: dict = {}
    if not parts:
        return out

    # Genre candidate: topmost single-word folder that is not a year
    # and not itself artist-like.
    top = parts[0]
    if len(parts) >= 2 and not re.search(r"\d{4}", top) and len(top) < 30:
        out.setdefault("genre", top.strip())

    # Walk from the innermost folder (album-ish) outward.
    album_part = parts[-1] if parts else ""
    artist_part = parts[-2] if len(parts) >= 2 else ""

    # Album folder: "Artist - Album (YYYY)" / "Artist - YYYY - Album"
    m = re.match(r"^(?P<artist>.+?)\s+-\s+(?P<album>.+)$", album_part)
    if m and not _looks_like_track_folder(album_part):
        artist = _clean(m.group("artist"))
        album_raw = m.group("album")
        year = _parse_year(album_raw)
        album_clean = _clean_album(album_raw)
        if artist and album_clean:
            out["artist"] = artist
            out["album"] = album_clean
            if year:
                out["year"] = year
            return out

    # "YYYY - Artist - Album"
    m = re.match(r"^(?P<year>\d{4})\s*[-_]\s*(?P<artist>[^-]+?)\s+-\s+(?P<album>.+)$",
                 album_part)
    if m:
        out["artist"] = _clean(m.group("artist"))
        out["album"] = _clean_album(m.group("album"))
        out["year"] = int(m.group("year"))
        return out

    # Album folder is just "Album (YYYY)" / "Album" — artist from the parent.
    album_clean = _clean_album(_RE_VA_PREFIX.sub("", album_part))
    # "Artist_Album_(Year)_MP3" / "Artist - Album Year MP3" — media-format
    # suffix folders: artist is the leading token, format word at the end.
    m_fmt = re.match(
        r"^(?P<artist>.+?)[-_]+(?P<album>.+?)[-_]*\((?P<year>(?:19|20)\d{2})\)"
        r"[-_]*(?:mp3|flac|web|cd|lossless|320kbps.*)?$",
        album_part, re.IGNORECASE)
    if m_fmt and " " not in album_part.strip():
        out["artist"] = _clean(m_fmt.group("artist"))
        out["album"] = _clean_album(m_fmt.group("album"))
        out["year"] = int(m_fmt.group("year"))
        return out
    # Release-folder style: "Artist--Album-2008-1way" (scene naming) —
    # strip trailing group suffix after the year.
    m_rel = re.match(
        r"^(?P<artist>.+?)[-_]+(?P<album>.+?)[-_]+(?:19|20)\d{2}[-_].*$",
        album_part)
    if m_rel:
        out["artist"] = _clean(m_rel.group("artist"))
        out["album"] = _clean_album(m_rel.group("album"))
        y = _parse_year(album_part)
        if y:
            out["year"] = y
        return out
    year = _parse_year(album_part)
    if album_clean and not _looks_like_track_folder(album_part):
        out["album"] = album_clean
        if year:
            out["year"] = year
        # Parent folder as artist if it looks like "Artist - Album" or a name.
        m2 = re.match(r"^(?P<artist>[^-]+?)\s+-\s+.+$", artist_part)
        if m2:
            out["artist"] = _clean(m2.group("artist"))
        elif artist_part and not re.search(r"\d{4}", artist_part) \
                and len(artist_part) < 40 and artist_part.lower() not in (
                    "musik", "music", "albums", "mp3", "media"):
            out.setdefault("artist", artist_part.strip())
    return out


def _looks_like_track_folder(name: str) -> bool:
    """Heuristic: folder names like 'CD1', 'Disc 2' are disc subfolders."""
    return bool(re.fullmatch(
        r"(?:cd|disc|cd\d|disc\d|cd\s*\d+|disc\s*\d+|side\s*[ab]\d?)\s*\d*",
        name.strip(), re.IGNORECASE))


# ── Filename heuristics ───────────────────────────────────────────────

def guess_from_filename(stem: str) -> dict:
    """Derive metadata from the file name (without extension)."""
    out: dict = {}
    stem = _clean(stem)
    # Release-style names are usually underscore-separated
    # ("14-des_teufels_lockvoegel_-_(title)") — normalize them so the
    # patterns below match: underscores→spaces when there is no space.
    if " " not in stem and "_" in stem:
        stem = stem.replace("_", " ")
        stem = re.sub(r"\s{2,}", " ", stem).strip()

    m = _RE_NUM_ARTIST_TITLE.match(stem)
    if m:
        out["track"] = int(m.group("num"))
        out["artist"] = _clean(m.group("artist"))
        out["title"] = _clean(m.group("title"))
        return out

    m = _RE_NUM_TITLE.match(stem)
    if m:
        out["track"] = int(m.group("num"))
        rest = _clean(m.group("title"))
        m2 = _RE_ARTIST_TITLE.match(rest)
        if m2:
            out["artist"] = _clean(m2.group("artist"))
            out["title"] = _clean(m2.group("title"))
        else:
            out["title"] = rest
        return out

    m = _RE_ARTIST_TITLE.match(stem)
    if m:
        out["artist"] = _clean(m.group("artist"))
        out["title"] = _clean(m.group("title"))
        return out

    m = _RE_NUM_PREFIX.match(stem)
    if m:
        out["track"] = int(m.group("num"))
        out["title"] = _clean(m.group("rest"))
        return out

    out["title"] = stem
    return out


# ── Main entry: merge tag + folder + filename ────────────────────────

def apply_heuristics(
    *,
    file_path: Path | str,
    library_root: str = "",
    title: str = "",
    artist: str = "",
    album: str = "",
    genre: str = "",
    year: int = 0,
    track: int = 0,
) -> GuessedMeta:
    """Fill missing metadata from folder structure, then the filename.

    Tag values (non-empty arguments) always win; only gaps get filled.
    """
    p = Path(file_path)
    src: dict = {}

    def fill(dst: str, value, source: str) -> None:
        # Only set when the field is still empty — tags first.
        if dst == "year" or dst == "track":
            if not value:
                return
            if dst == "year" and year:
                return
            if dst == "track" and track:
                return
        elif dst in ("title", "artist", "album", "genre") and locals().get(dst):
            return
        src[dst] = source
        # write through via nonlocal-ish trick: caller merges below

    # Relative parts between library root and the file (folders only).
    rel = str(p.parent)
    if library_root:
        root = str(Path(library_root))
        if rel.startswith(root):
            rel = rel[len(root):]
    parts = [x for x in re.split(r"[\\/]+", rel) if x]

    g = GuessedMeta(
        title=title, artist=artist, album=album, genre=genre,
        year=year, track=track,
    )

    def set_field(name: str, value, source: str) -> None:
        if name in ("year", "track"):
            if value and not getattr(g, name):
                setattr(g, name, int(value))
                g.sources[name] = source
        else:
            value = _clean(value) if isinstance(value, str) else value
            if value and not getattr(g, name):
                setattr(g, name, value)
                g.sources[name] = source

    # 1) explicit tags (already set by caller) → mark their source
    for f in ("title", "artist", "album", "genre"):
        if getattr(g, f):
            g.sources[f] = "tag"
    if g.year:
        g.sources["year"] = "tag"
    if g.track:
        g.sources["track"] = "tag"

    # 2) folder structure
    folder = guess_from_folders(parts)
    for f in ("genre", "artist", "album", "year"):
        set_field(f, folder.get(f), "folder")

    # 2b) VA/compilation folders: the folder artist is meaningless — let
    # the filename provide the real track artist.
    fname_pre = guess_from_filename(p.stem)
    if (g.artist or "").strip().lower() in ("va", "various", "various artists") \
            and fname_pre.get("artist"):
        g.artist = fname_pre["artist"]
        g.sources["artist"] = "filename"

    # 3) filename
    for f in ("artist", "title", "track"):
        set_field(f, fname_pre.get(f), "filename")

    # 4) last-resort fallbacks
    if not g.title:
        g.title = p.stem
        g.sources["title"] = "fallback"
    if not g.artist and parts:
        g.artist = parts[0]
        g.sources["artist"] = "fallback"
    if not g.album and len(parts) >= 1:
        g.album = _clean_album(parts[-1])
        g.sources["album"] = "fallback"

    return g
