"""Local playlist files (``.m3u``/``.m3u8``/``.pls``) — Perl ``Slim::Formats::Playlists``.

Why this module exists
----------------------
A ``.m3u`` in the music folder is a first-class child of Perl's
``musicfolder``/``bmf`` listing — ``fileFilter`` keeps it because
``types.conf`` calls the type a ``playlist`` (``Slim/Utils/Misc.pm:900-903``,
``Slim/Music/Info.pm:1345-1388``) — and ``folder_loop`` reports it as
``type 'playlist'`` (``Slim/Control/Queries.pm:2441-2443``).  Tapping it plays
the tracks it names, because Perl expands the FILE:

* ``Slim/Control/Commands.pm:1309`` ``playlistXitemCommand`` — the verb behind
  ``playlist play <item>``; a local playlist URL is not a stream.
* ``Slim/Control/Commands.pm:3686-3693`` — ``isPlaylist($url) &&
  !isRemoteURL($url)`` resolves to a ``Playlist`` object (remote ``.m3u``
  *streams* stay URLs).
* ``Slim/Formats/Playlists/M3U.pm:31-186`` / ``PLS.pm:29-93`` — the file
  grammar (``#``-comments, ``#EXTINF``/``#EXTURL``, ``File<n>=``/``Title<n>=``).
* ``Slim/Utils/Misc.pm:447-540`` ``fixPath`` — a relative entry resolves
  against the playlist's own directory
  (``stripRel(catfile($base, $file))``); ``M3U.pm:153-166`` retries it against
  each audio dir.
* ``Slim/Formats/Playlists/Base.pm:109-131`` ``playlistEntryIsValid`` — a
  local entry must exist on disk, a self-reference is skipped.
* ``Slim/Player/Playlist.pm:188`` ``addTracks`` — the expanded tracks land in
  the queue.

Deviation, stated honestly
--------------------------
Perl *creates* a ``tracks`` row for every playlist entry it cannot find
(``Slim/Formats/Playlists/Base.pm:56-68`` ``updateOrCreate``) and one for the
playlist file itself (``Slim/Control/Queries.pm:2255-2268``).  This port never
writes while browsing or playing, so:

* an entry that **is** in the library DB (the normal case — the scanner
  imported it) resolves to its ``tracks.id`` and plays;
* an entry that is **not** in the DB is dropped and logged
  (``logger.warning``), instead of being imported on the fly;
* the ``.m3u`` file itself keeps the file URL as its ``id`` in the listing —
  the tap resolves it through this module, which is why it plays anyway.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import urllib.parse

logger = logging.getLogger(__name__)

#: ``Slim/Formats/Playlists/M3U.pm`` handles ``m3u`` and ``m3u8``
#: (``types.conf``: ``m3u  m3u,m3u8 … playlist``).
M3U_SUFFIXES: frozenset[str] = frozenset({"m3u", "m3u8"})

#: ``Slim/Formats/Playlists/PLS.pm``.
PLS_SUFFIXES: frozenset[str] = frozenset({"pls"})

#: Formats this module expands.  Perl has a parser per playlist type
#: (``WPL``/``ASX``/``XSPF``/``CUE``); those keep Perl's other branch here and
#: are played as what they are until their grammar is ported.
EXPANDABLE_SUFFIXES: frozenset[str] = M3U_SUFFIXES | PLS_SUFFIXES

#: ``Slim/Formats/Playlists/M3U.pm:126-128``: a playlist that is really an
#: HTML error page is abandoned at the first ``<``/``<!DOCTYPE html`` line.
_HTML_START_RE = re.compile(r"^<(?:!DOCTYPE\s*)?html", re.IGNORECASE)
_EXTURL_RE = re.compile(r"^#EXTURL:(.*?)$")
_PLS_FILE_RE = re.compile(r"File(\d+)=(.*)", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def suffix_of(path_or_url: str) -> str:
    """Lower-case suffix of a path or URL, ``""`` when there is none."""
    text = str(path_or_url or "")
    if "://" in text:
        text = urllib.parse.urlparse(text).path
    text = urllib.parse.unquote(text)
    name = text.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def local_path(path_or_url: str) -> str:
    """The local filesystem path of a value, or ``""``.

    A remote URL (``http://…``) has no local path — Perl keeps it a URL
    (``Slim/Control/Commands.pm:3686`` ``!isRemoteURL``), and so do we.
    """
    text = str(path_or_url or "").strip()
    if not text:
        return ""
    if text.startswith("file://"):
        from lyrion.platform import paths as platform_paths

        return platform_paths.path_from_file_url(text)
    if _SCHEME_RE.match(text) and not os.path.isabs(text):
        return ""                      # http:, https:, tmp:, db:, …
    return text


def is_local_playlist(path_or_url: str) -> bool:
    """Is this a local file this module can expand (``.m3u``/``.m3u8``/``.pls``)?"""
    if suffix_of(path_or_url) not in EXPANDABLE_SUFFIXES:
        return False
    return bool(local_path(path_or_url))


# ---------------------------------------------------------------------------
# Entry resolution (Perl ``fixPath`` + ``playlistEntryIsValid``)
# ---------------------------------------------------------------------------


def _strip_rel(path: str) -> str:
    """Perl ``stripRel`` — drop ``name/../`` sequences (``Slim/Utils/Misc.pm:586-598``)."""
    while re.search(r"[/\\]\.\.[/\\]", path):
        path = re.sub(r"[^/\\]+[/\\]\.\.[/\\]", "", path)
    return path


def _resolve_entry(value: str, base_dir: str) -> str:
    """One playlist line → an existing local path, a remote URL, or ``""``.

    ``Slim/Utils/Misc.pm:466-476``: a URL is returned as it is.  Otherwise
    (``:487-540``) a Windows drive letter and backslashes are normalised, an
    absolute path is kept, and a relative one becomes
    ``stripRel(catfile($base, $file))``.
    """
    entry = str(value or "").strip()
    if not entry:
        return ""
    if _SCHEME_RE.match(entry) and not os.path.isabs(entry):
        # Remote (``isRemoteURL``) — Perl keeps the URL; a local ``file://``
        # entry is turned into a path below.
        if not entry.startswith("file://"):
            return entry
        from lyrion.platform import paths as platform_paths

        entry = platform_paths.path_from_file_url(entry)
        if not entry:
            return ""
    entry = re.sub(r"^[C-Z]:", "", entry, flags=re.IGNORECASE)
    entry = entry.replace("\\", "/")
    if not os.path.isabs(entry):
        entry = _strip_rel(os.path.join(base_dir, entry))
    return os.path.normpath(entry)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_m3u(text: str) -> list[str]:
    """``Slim/Formats/Playlists/M3U.pm:31-186`` ``read`` — the raw entries."""
    out: list[str] = []
    checked_bom = False
    trackurl = ""
    for raw in text.splitlines():
        entry = raw.replace("\r", "").strip()
        if not checked_bom:
            entry = entry.lstrip("\ufeff")
            checked_bom = True
        if entry.startswith("#"):
            # ``#EXTINF:``/``#ADDEDFROMWORK:`` only carry metadata;
            # ``#EXTURL:<x>`` replaces the next line as the entry URL
            # (M3U.pm:110-116).
            hit = _EXTURL_RE.match(entry)
            if hit:
                trackurl = hit.group(1)
            continue
        if entry == "":
            continue
        if _HTML_START_RE.match(entry):
            break                      # an HTML error page is not a playlist
        if entry.startswith("<"):
            continue
        out.append(trackurl or entry)
        trackurl = ""
    return out


def _parse_pls(text: str) -> list[str]:
    """``Slim/Formats/Playlists/PLS.pm:29-93`` ``read`` — the raw entries.

    ``File<n>=<value>`` lines are collected by index and read back from ``1``
    up to the last index — Perl's ``for (my $i = 1; $i <= $#urls; $i++)``.
    """
    urls: dict[int, str] = {}
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        hit = _PLS_FILE_RE.match(line.rstrip())
        if hit:
            try:
                urls[int(hit.group(1))] = hit.group(2).strip()
            except ValueError:  # pragma: no cover - regex guarantees digits
                continue
    return [urls[i] for i in sorted(urls) if i >= 1 and urls[i]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def entries(path_or_url: str, media_dirs: list[str] | None = None) -> list[str]:
    """The entries of a local playlist file, in file order.

    Local entries are returned as paths, remote entries as URLs.  A relative
    entry is resolved against the playlist's own directory first
    (``Slim/Utils/Misc.pm:447-540``) and, when that does not exist, against
    each audio dir (``Slim/Formats/Playlists/M3U.pm:153-166``).  An entry that
    still does not exist is dropped like Perl
    (``Slim/Formats/Playlists/Base.pm:124-128``).
    """
    path = local_path(path_or_url)
    suffix = suffix_of(path_or_url)
    if not path or suffix not in EXPANDABLE_SUFFIXES:
        return []
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        logger.warning("playlist %s cannot be read: %s", path, exc)
        return []
    text = _decode(raw)
    base_dir = os.path.dirname(path)
    parsed = _parse_m3u(text) if suffix in M3U_SUFFIXES else _parse_pls(text)

    resolved: list[str] = []
    for entry in parsed:
        candidate = _resolve_entry(entry, base_dir)
        if candidate and _is_valid_entry(candidate, path):
            resolved.append(candidate)
            continue
        # Perl retries the ORIGINAL entry against every audio dir before it
        # gives up on it (M3U.pm:155-166).
        for audio_dir in (media_dirs or []):
            candidate = _resolve_entry(entry, audio_dir)
            if candidate and _is_valid_entry(candidate, path):
                resolved.append(candidate)
                break
    return resolved


def _decode(raw: bytes) -> str:
    """Decode playlist bytes the way Perl guesses an encoding (``M3U.pm:63-84``)."""
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")       # pragma: no cover - latin-1 covers all


def _is_valid_entry(entry: str, playlist_path: str) -> bool:
    """Perl ``playlistEntryIsValid`` — remote passes, local must exist."""
    if _SCHEME_RE.match(entry) and not os.path.isabs(entry):
        return True
    if os.path.normpath(entry) == os.path.normpath(playlist_path):
        logger.warning("playlist %s references itself - skipped", playlist_path)
        return False
    if not os.path.isfile(entry):
        logger.warning("%s from playlist %s does not exist - skipped",
                       entry, playlist_path)
        return False
    return True


def track_ids(path_or_url: str, media_dirs: list[str] | None = None,
              db_path: str | os.PathLike[str] | None = None) -> list[int]:
    """The ``tracks.id`` of every entry of a local playlist file, in order.

    Read-only: the library DB is opened with ``mode=ro`` (see the module
    docstring for the deviation from Perl's ``updateOrCreate``).  Entries that
    are not in the library are logged and skipped, never invented.
    """
    paths = entries(path_or_url, media_dirs=media_dirs)
    if not paths:
        return []
    from lyrion.media import folders

    urls: list[str] = []
    for entry in paths:
        if _SCHEME_RE.match(entry) and not os.path.isabs(entry):
            urls.append(entry)                       # already a URL
        else:
            urls.append(folders.file_url_from_path(entry))
    ids = _ids_for_urls(urls, db_path=db_path)
    resolved: list[int] = []
    for url, tid in zip(urls, ids):
        if tid is None:
            logger.warning("playlist entry %s is not in the library - skipped", url)
            continue
        resolved.append(tid)
    return resolved


def _ids_for_urls(urls: list[str],
                  db_path: str | os.PathLike[str] | None = None) -> list[int | None]:
    """``tracks.id`` per URL (one query), ``None`` for an unknown URL.

    The playlist names the files exactly as the scanner stored them, so one
    ``IN`` lookup answers the whole list; the misses are retried per URL so a
    case-insensitive filesystem (Windows/macOS) still resolves
    (``folders._track_id_by_url``, Perl ``noCaseFilename``).
    """
    found: dict[str, int] = {}
    if urls:
        try:
            con = _ro_connection(db_path)
        except sqlite3.Error:                       # pragma: no cover - defensive
            con = None
        if con is not None:
            marks = ",".join("?" * len(urls))
            try:
                rows = con.execute(
                    f"SELECT id, url FROM tracks WHERE url IN ({marks})",
                    tuple(urls),
                ).fetchall()
                found = {str(r[1]): int(r[0]) for r in rows if r[1]}
            except sqlite3.Error:
                found = {}
    out: list[int | None] = []
    for url in urls:
        hit = found.get(url)
        if hit is None:
            from lyrion.media import folders

            hit = folders._track_id_by_url(url)
        out.append(hit)
    return out


def _ro_connection(db_path: str | os.PathLike[str] | None = None
                   ) -> sqlite3.Connection | None:
    """Read-only connection to the library DB (``None`` when unreachable)."""
    if db_path is None:
        try:
            from lyrion.config import get_config

            db_path = get_config().db_path
        except Exception:  # pragma: no cover - defensive
            return None
    path = str(db_path)
    if not path or not os.path.exists(path):
        return None
    return sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True)
