"""Music-folder browsing — Perl parity for ``musicfolder`` / ``mediafolder``.

Why this module exists
----------------------
``musicfolder 0 N`` must list the children of the configured music folder(s)
so that *every* controller (Squeezer, SqueezeCtrl, Squeeze Commander,
SqueezePlay) can browse "Musikordner".  Our old handlers derived the top
level from the first path component of the ``tracks.url`` values and answered
``count=1`` (``file:///run``) while Perl answers ``count=303``, because Perl
starts from the *media dirs* (``mediadirs``) and simply reads that directory.
This module is that missing piece: it resolves the media dirs, falls back to
the scanner's library roots when the pref is unset, and emits the Perl loop
shape.

Perl reference (read-only, /tmp/lms-ref)
----------------------------------------
* ``Slim/Control/Queries.pm:2161-2167``  ``musicfolderQuery`` → ``mediafolderQuery``
* ``Slim/Control/Queries.pm:2169-2350``  parameter handling, media dirs, root vs. child listing
* ``Slim/Control/Queries.pm:2384-2470``  ``folder_loop`` items: ``id``, ``filename``, ``type``
* ``Slim/Control/Queries.pm:2507``       ``count`` is added last
* ``Slim/Utils/Misc.pm:727-756``         ``getMediaDirs`` (mediadirs minus ignoreInAudioScan)
* ``Slim/Utils/Misc.pm:753-755``         ``getDirsPref`` (``$prefs->get($name) || ['']``)
* ``Slim/Utils/Misc.pm:758-760``         ``getInactiveAudioDirs`` = ``ignoreInAudioScan``
* ``Slim/Utils/Misc.pm:973-1043``        ``readDirectory`` — the dirent listing
* ``Slim/Utils/Misc.pm:835-909``         ``fileFilter`` — dirs always, files only for known types
* ``Slim/Utils/Misc.pm:292-331``         ``fileURLFromPath`` — URI::file escaping
* ``Slim/Utils/Misc.pm:1060-1160``       ``findAndScanDirectoryTree`` → ``readDirectory``
* ``Slim/Utils/OS.pm:334-353``           ``sortFilename`` — native collation of ``lc(name)``
* ``Slim/Utils/OS.pm:268-274``           ``ignoredItems`` (linux: ``lost+found``)
* ``Slim/Utils/Prefs.pm:163,207``        defaults: ``mediadirs``→``defaultMediaDirs``, ``ignoreInAudioScan``→``[]``
* ``Slim/Utils/Prefs.pm:383-403``        validation: array of unique, existing folders
* ``Slim/Utils/Prefs.pm:687-712``        ``defaultMediaDirs``: ``audiodir`` else OS music folder
* ``Slim/Web/Settings/Server/Basic.pm:88-121``  what the settings page stores
* ``Slim/Control/Request.pm``  ``normalize`` — index/quantity → start..end

Deviation, stated honestly
--------------------------
Perl gives every browsed folder a numeric id by *creating* a ``Track`` row of
content type ``dir`` (``findAndScanDirectoryTree`` → ``objectForUrl(create=>1)``,
``Slim/Utils/Misc.pm:1082-1087``).  This port stores those rows too — the scan
writes one per walked directory (``media/dir_rows.py``) and the browse creates
the row of every listed folder on demand, like Perl's ``Queries.pm:2263-2268``
— so ``folder_loop`` items carry the row's ``tracks.id``.  When the library DB
is not writable (or a lean/foreign schema has no ``content_type`` column) the
row cannot be established and the folder's **file URL** is emitted instead:
the drill (``folder_id``/``url``) accepts that token as well, so browsing keeps
working, and ``media/dir_rows.py`` logs the reason.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

from lyrion.media import dir_rows
from lyrion.media.scanner import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

#: Playlist-ish suffixes Perl accepts through ``validTypeExtensions('list|audio')``
#: (``Slim/Music/Info.pm:1345-1375``); ``readDirectory`` keeps them next to dirs.
PLAYLIST_EXTENSIONS: frozenset[str] = frozenset({
    "m3u", "m3u8", "pls", "wpl", "asx", "xspf", "cue",
})

#: Perl ``validTypeExtensions`` — the audio + playlist set (Info.pm:1345-1375).
#:
#: Perl's ``readDirectory`` calls ``fileFilter`` with the **default**
#: ``$validRE = Slim::Music::Info::validTypeExtensions()``
#: (``Slim/Utils/Misc.pm:973-975``), i.e. every suffix whose ``types.conf``
#: slim-type matches ``list|audio`` (``Slim/Music/Info.pm:1345-1375``:
#: ``next unless $type =~ /$findTypes/`` with ``$findTypes || 'list|audio'``,
#: ``types.conf`` column 4).  Deriving the set from ``SUPPORTED_EXTENSIONS``
#: alone was short by 14 of Perl's suffixes, so the drill silently lost the
#: children that carry them: live 192.168.1.90 ``musicfolder 0 400
#: folder_id:<Video>`` → 6 children, ours 4 — the two ``.mp4`` files were
#: missing (``types.conf:40`` ``mp4  m4a,mp4,m4b  …  audio``).  Same for the
#: ``.dsf``/``.dff``/``.wv``/``.mp2``/``.wave``/``.pcm`` rows (types.conf:20,
#: :22, :62, :42, :57, :47).  ``lnk`` stays out: Perl adds it only on Windows
#: (``Info.pm:1371-1374`` ``if (main::ISWINDOWS …)``).
PERL_LISTABLE_SUFFIXES: frozenset[str] = frozenset({
    "dff", "dsf", "fla", "flc", "l16", "l24", "lpcm", "mp2", "mp4",
    "ogf", "pcm", "wave", "wax", "wv",
})

LISTABLE_EXTENSIONS: frozenset[str] = (
    SUPPORTED_EXTENSIONS | PLAYLIST_EXTENSIONS | PERL_LISTABLE_SUFFIXES
)

#: ``types.conf`` suffixes whose slim-type is ``audio`` — Perl's ``isSong``
#: (``Slim/Music/Info.pm:1262-1276``) is exactly
#: ``$slimTypes{$type} eq 'audio'``, and that is what makes a ``folder_loop``
#: item ``type 'track'`` (``Slim/Control/Queries.pm:2483-2484``).  A ``.mp4``
#: is one of them (``types.conf:40`` ``mp4   m4a,mp4,m4b   …   audio``): live
#: 192.168.1.90 ``musicfolder 0 4 folder_id:<Video>`` answers ``type: 'track'``
#: for its ``.mp4`` files, deriving the set from ``SUPPORTED_EXTENSIONS`` alone
#: answered ``unknown``.
PERL_SONG_SUFFIXES: frozenset[str] = frozenset({
    "aac", "aif", "aiff", "ape", "dff", "dsf", "fla", "flac", "flc",
    "l16", "l24", "lpcm", "m4a", "m4b", "mp+", "mp2", "mp3", "mp4",
    "mpc", "oga", "ogf", "ogg", "opus", "pcm", "wav", "wave", "wma", "wv",
})

#: Perl ``fileFilter`` always drops these (``Slim/Utils/OS.pm:268-274`` + Misc.pm:835-845).
IGNORED_ITEMS: frozenset[str] = frozenset({"lost+found"})

#: Perl ``fileFilter``: ``return 0 if $item =~ /^__\S+\.m3u$/`` (Misc.pm:853).
_OLD_HISTORY_RE = re.compile(r"^__\S+\.m3u$")

#: Perl ``getMediaDirs`` maps the media type to its ignore-list pref (Misc.pm:735-738).
_IGNORE_PREF_FOR_TYPE = {"audio": "ignoreInAudioScan"}

_URL_SCHEMES = ("file://", "tmp://", "http://", "https://", "db:", "spotify:")


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def _pref(name: str, default: Any = "") -> Any:
    """Read a preference like Perl ``$prefs->get($name)``.

    ``lyrion.config.get_config()`` checks the ``.conf`` file and then the
    SQLite preference store — the same order the media settings page uses.
    Imported lazily so this module stays importable without a running store.
    """
    try:
        from lyrion.config import get_config

        value = get_config().get(name, default)
    except Exception:  # pragma: no cover - defensive, config is always there
        return default
    if value is None:
        return default
    return value


def as_path_list(value: Any) -> list[str]:
    """Coerce a stored preference to a list of non-empty paths.

    Perl keeps ``mediadirs``/``ignoreInAudioScan`` as array refs
    (``Prefs.pm:383-403``); our store serialises them comma separated
    (``web/settings.py`` ``_save_server_basic``).  Both shapes are accepted.

    The splitting happens in :func:`lyrion.platform.paths.split_path_list`,
    which keeps the comma inside a gvfs mount id
    (``smb-share:server=host,share=name``) — otherwise a mounted SMB share would
    silently vanish from folder browsing.

    NOTE: a path containing a literal comma *followed by* ``key=`` still cannot
    be represented in the comma-separated form — a known limitation of our
    store, not of this code.
    """
    from lyrion.platform import paths as platform_paths

    return platform_paths.split_path_list(value)


def get_dir_pref(name: str) -> list[str]:
    """Perl ``getDirsPref`` — ``Slim/Utils/Misc.pm:753-755``."""
    return as_path_list(_pref(name))


def get_media_dirs(media_type: str = "") -> list[str]:
    """Perl ``getMediaDirs`` — ``Slim/Utils/Misc.pm:727-756``.

    Returns the ``mediadirs`` preference, minus the folders disabled for the
    requested media type (``audio`` → ``ignoreInAudioScan``, Misc.pm:735-741).
    """
    dirs = get_dir_pref("mediadirs")
    if media_type:
        ignore_pref = _IGNORE_PREF_FOR_TYPE.get(media_type)
        if ignore_pref:
            ignore = {os.path.normpath(p) for p in get_dir_pref(ignore_pref)}
            dirs = [d for d in dirs if os.path.normpath(d) not in ignore]
    return dirs


def get_inactive_audio_dirs() -> list[str]:
    """Perl ``getInactiveAudioDirs`` — ``Slim/Utils/Misc.pm:758-760``."""
    return get_dir_pref("ignoreInAudioScan")


# ---------------------------------------------------------------------------
# Library roots (the fallback when ``mediadirs`` is unset)
# ---------------------------------------------------------------------------


def _scanner_root_candidates() -> list[str]:
    """Paths the scanner could be configured to walk (see :func:`scanner_configured_roots`).

    Split out so tests can substitute a fake scanner configuration: the
    dataclass defaults are baked into ``ImportConfig.__init__`` at class
    creation time, so patching the class attribute has no effect on a fresh
    instance.
    """
    from lyrion.media.importer import ImportConfig
    from lyrion.media.scanner import ScanConfig

    # Read ScanConfig's dataclass default instead of instantiating it:
    # ``__post_init__`` logs a warning when the path is missing
    # (scanner.py:169-186), which would spam the log on every browse request.
    base_default = ScanConfig.__dataclass_fields__["base_path"].default
    # NOTE: the OS music folder is deliberately NOT added here — it is the
    # *last* resort in :func:`library_roots` (Perl's defaultMediaDirs order,
    # ``Slim/Utils/Prefs.pm:687-712``).  On this host ``~/Music`` exists but is
    # an empty decoy while the library lives on a gvfs/SMB mount; listing it as
    # a "configured" root would hide the real library from ``musicfolder``.
    return [str(ImportConfig().source_path or ""), str(base_default or "")]


def scanner_configured_roots() -> list[str]:
    """The scanner's own configured library roots.

    ``ImportConfig.source_path`` (``media/importer.py:39-52``) is the folder the
    importer walks; ``ScanConfig.base_path`` (``media/scanner.py:162``) is the
    scanner default.  Only roots that exist on this machine are returned so the
    caller can fall through to :func:`db_library_roots`.
    """
    seen: list[str] = []
    for candidate in _scanner_root_candidates():
        text = str(candidate or "")
        if text and text not in seen:
            seen.append(text)
    return [p for p in seen if os.path.isdir(p)]


def db_library_roots(db_path: str | os.PathLike[str] | None = None) -> list[str]:
    """Derive the scanned library root from the ``tracks`` table.

    The scanner stores absolute ``file://`` URLs, so the longest common
    directory prefix of the smallest and largest URL is the folder the scanner
    actually walked.  Used only when no *configured* root exists on disk (e.g.
    the library lives on a removable/gvfs mount that is not under the compiled
    default path).  Read-only: the DB is opened with ``mode=ro``.
    """
    if db_path is None:
        try:
            from lyrion.config import get_config

            db_path = get_config().db_path
        except Exception:  # pragma: no cover - defensive
            return []
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True
        )
    except sqlite3.Error:
        return []
    try:
        row = con.execute(
            "SELECT MIN(url), MAX(url) FROM tracks WHERE url LIKE 'file://%'"
        ).fetchone()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    if not row or not row[0] or not row[1]:
        return []
    low, high = str(row[0]), str(row[1])
    prefix = []
    for a, b in zip(low, high):
        if a != b:
            break
        prefix.append(a)
    longest = "".join(prefix)
    # Drop the (necessarily partial) last path segment.
    cut = longest.rfind("/")
    if cut > len("file://"):
        longest = longest[:cut]

    root = path_from_file_url(longest)
    return [root] if root and os.path.isdir(root) else []


def library_roots() -> list[str]:
    """Fallback chain when ``mediadirs`` is unset — the scanner's real roots.

    Order (each step only wins when it yields an existing folder):

    1. ``audiodir`` / ``musicdir`` — an explicitly chosen folder.  Perl
       migrates ``audiodir`` into ``mediadirs`` (``Prefs.pm:687-698``).
    2. The scanner's configured roots (``ImportConfig.source_path``,
       ``ScanConfig.base_path``) — what the importer actually walks.
    3. The root the current library was scanned from, derived from
       ``tracks.url`` (``db_library_roots``).
    4. The OS music folder — Perl's ``defaultMediaDirs`` last resort
       (``Prefs.pm:700-710`` → ``OSDetect::dirsFor('music')``).

    Deliberate deviation from Perl: Perl's ``defaultMediaDirs`` puts the OS
    music folder *before* anything else, but it only ever runs when the pref
    was never set.  Here the pref can be empty while a fully populated library
    (80k tracks on a gvfs/SMB mount) already exists, and ``~/Music`` on such a
    host is an empty decoy — preferring it would answer ``musicfolder`` with a
    single unrelated folder instead of the library.  The scanner's real roots
    therefore come first, and ``~/Music`` is kept as the last resort, so a
    fresh install still behaves like Perl.
    """
    legacy = str(_pref("audiodir", "") or _pref("musicdir", "") or "").strip()
    if legacy and os.path.isdir(legacy):
        return [legacy]

    configured = scanner_configured_roots()
    if configured:
        return configured

    scanned = db_library_roots()
    if scanned:
        return scanned

    default_music = _platform_music_dir()
    if default_music is not None and default_music.is_dir():
        return [str(default_music)]

    return []


def _platform_music_dir() -> Path | None:
    """The OS music folder — Perl ``OSDetect::dirsFor('music')``.

    ``~/Music`` on macOS (``OSX.pm:183-199``), ``%USERPROFILE%\\Music``
    (``CSIDL_MYMUSIC``) on Windows (``Win32.pm:189-197``); Perl has **no**
    default on Unix/Linux (``Unix.pm:68-71``), where the historical ``~/Music``
    is kept as the last resort.
    """
    from lyrion.platform import paths as platform_paths

    music = platform_paths.default_music_dir()
    return music if music is not None else Path.home() / "Music"


def effective_media_dirs(media_type: str = "audio") -> list[str]:
    """``mediadirs`` if set, else the library roots.

    Deliberate deviation from Perl: Perl's ``mediadirs`` default already *is*
    the OS music folder (``defaultMediaDirs``, ``Prefs.pm:687-712``), so it is
    never empty as long as a music folder exists.  Our store can hand back an
    empty list (``pref mediadirs ?`` → ``{"mediadirs": ""}`` on the live
    server), which used to make ``musicfolder`` answer with a single bogus
    entry instead of the library.  Answering "nothing" would be worse than
    answering with the roots the scanner actually uses, so we fall back.
    """
    dirs = get_media_dirs(media_type)
    if dirs:
        return dirs
    roots = library_roots()
    if roots:
        logger.info(
            "Preference 'mediadirs' is empty — falling back to the scanner's "
            "library root(s) %s (Perl defaultMediaDirs, "
            "Slim/Utils/Prefs.pm:687-712)",
            roots,
        )
    else:
        logger.warning(
            "Preference 'mediadirs' is empty and no library root could be "
            "resolved — folder browsing will be empty"
        )
    return roots


# ---------------------------------------------------------------------------
# Path <-> URL
# ---------------------------------------------------------------------------


def file_url_from_path(path: str | os.PathLike[str]) -> str:
    """Perl ``fileURLFromPath`` — ``Slim/Utils/Misc.pm:292-331``.

    Delegates to :func:`lyrion.platform.paths.file_url_from_path`: ``URI::file``
    escapes ``:``/``=``/``,``/space, so
    ``/run/.../smb-share:server=x,share=y/Musik`` becomes
    ``file:///run/.../smb-share%3Aserver%3Dx%2Cshare%3Dy/Musik`` — exactly the
    encoding our ``tracks.url`` values already use.  The platform module also
    covers the Windows drive-letter (``file:///C:/…``) and UNC
    (``file://server/share/…``) shapes that plain concatenation got wrong.
    """
    from lyrion.platform import paths as platform_paths

    return platform_paths.file_url_from_path(path)


def path_from_file_url(url: str | os.PathLike[str]) -> str:
    """Perl ``pathFromFileURL`` — inverse of :func:`file_url_from_path`.

    Delegates to :func:`lyrion.platform.paths.path_from_file_url`; that module
    documents the gvfs/Windows/UNC cases and rejects ``..`` URLs like Perl
    (``Slim/Utils/Misc.pm:271-274``) by returning an empty string.
    """
    from lyrion.platform import paths as platform_paths

    return platform_paths.path_from_file_url(url)


# ---------------------------------------------------------------------------
# Directory listing (Perl ``readDirectory``)
# ---------------------------------------------------------------------------


def name_collation_key(name: str) -> tuple[str, str]:
    """Sort key of one file/folder *name* — Perl ``noCaseFilename``.

    Perl ``sortFilename`` (``Slim/Utils/OS.pm:334-353``) lowercases every name
    (:340 with :354-356 ``noCaseFilename`` = ``lc(fileName)``) and compares the
    results with ``cmp`` under ``use locale``; for that comparison it swaps
    ``LC_COLLATE`` for the ``LC_CTYPE`` locale (Bug 14906, :344-345) so the
    *native* collation sequence of the character encoding decides.  So the rule
    is **case-insensitive**, and the order of two names that only differ in
    case is decided by the sort's stability, i.e. the ``readdir`` order —
    ``perl -e 'print join " ", sort {lc($a) cmp lc($b)} qw(ABBA abba)'`` keeps
    ``ABBA abba``, and the reversed input keeps ``abba ABBA``.

    The process locale must not decide the order: the server runs under ``C``
    or ``C.UTF-8`` (where the collation is plain byte order, so ``Zebra`` would
    come before ``apple``) while the Perl reference runs under a glibc UTF-8
    locale.  So the *native* collation is reproduced deterministically here.
    On a glibc UTF-8 locale that collation ignores punctuation and accents at
    its primary level and folds case; live Perl (192.168.1.90) therefore lists
    ``Accept`` before ``AC+DC``, ``Boy_Harsher_-_Careful…`` before
    ``Boy_Harsher-Country_Girl…`` and ``6MzM6F.…`` before ``(Nils_Petter_…``.
    :func:`sort_filenames` checks that against the live server: this key has no
    ordering conflict with Perl on any of the 442 real entries of
    ``/mnt/media/Musik``, ``/tmp`` and ``/mnt/media``, while a plain
    ``lower()``/``casefold()`` key has 317.

    Two levels, like the collation's primary and secondary weight:

    * ``NFKD`` decomposition, combining marks removed, case folded, then
      everything that is not alphanumeric dropped — the primary level;
    * the case-folded name — the secondary level, so names that differ only in
      punctuation keep a defined order.

    Equal keys stay in input order because :func:`sort_filenames` sorts
    stably, which is Perl's tie-break.  ``str.lower()`` is used because it is
    the locale-independent equivalent of Perl's ``lc`` (``casefold`` would also
    fold ``ß``→``ss``, which ``lc`` does not).
    """
    folded = name.lower()
    primary = "".join(
        ch for ch in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(ch) and ch.isalnum()
    )
    return (primary, folded)


def sort_filenames(names: list[str]) -> list[str]:
    """Perl ``sortFilename`` — ``Slim/Utils/OS.pm:334-353``.

    Case-insensitive (``lc``) and locale-independent; see
    :func:`name_collation_key` for the derivation and the live-Perl check.
    Python's sort is stable, so names that fold to the same key keep the
    ``readdir`` order — Perl's tie-break.  This is *the* sort of every file and
    folder listing Perl builds from the file system (``readDirectory``,
    ``Slim/Utils/Misc.pm:1037``); it is not used for the album/artist lists,
    which Perl sorts in the database (see :func:`name_collation_key` notes in
    ``media/folders.py`` and ``web/api.py``).
    """
    return sorted(names, key=name_collation_key)


def _entry_is_listable(directory: str, name: str) -> bool:
    """Perl ``fileFilter`` — ``Slim/Utils/Misc.pm:835-909``."""
    if name in IGNORED_ITEMS:
        return False                                   # Misc.pm:856-857 / OS.pm:268
    if _OLD_HISTORY_RE.match(name):
        return False                                   # Misc.pm:853
    if name.startswith(".") and len(name) > 1:         # Misc.pm:854
        return False
    ignore_re = str(_pref("ignoreDirRE", "") or "")
    if ignore_re:
        try:
            if re.search(ignore_re, name):
                return False                           # Misc.pm:859-861
        except re.error:
            pass
    full = os.path.join(directory, name)
    if os.path.isdir(full):
        return True                                    # dirs always pass (Misc.pm:895-899)
    try:
        if not os.path.isfile(full) or not os.access(full, os.R_OK):
            return False                               # Misc.pm:879-890
    except OSError:
        return False
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return suffix in LISTABLE_EXTENSIONS               # Misc.pm:900-903


def list_directory_entries(
    directory: str | os.PathLike[str], *, recursive: bool = False
) -> list[str]:
    """Perl ``readDirectory`` — ``Slim/Utils/Misc.pm:973-1043``.

    Returns the entry *names* (not paths) of one directory, filtered by
    :func:`_entry_is_listable` and sorted with :func:`sort_filenames`.  This is
    the primitive ``findAndScanDirectoryTree`` feeds into ``mediafolderQuery``
    (``Misc.pm:1131``) — it is **not** the ``readdirectory`` query (that one is
    ``Slim/Control/Queries.pm:3093-3213`` and lives in ``web/api.py``).
    """
    if recursive:
        # Perl's recursive branch returns full paths from findFilesMatching
        # (Misc.pm:999-1003); the browse loop however keeps treating the values
        # as entry names, so we return basenames for both modes (controllers
        # never request a recursive BMF root — Queries.pm:2207 keeps
        # ``$params->{recursive}`` unset unless the client sends it).
        names: list[str] = []
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = [d for d in dirnames
                           if _entry_is_listable(dirpath, d)]
            for entry in dirnames + filenames:
                if _entry_is_listable(dirpath, entry):
                    names.append(entry)
        return names

    try:
        entries = os.listdir(str(directory))
    except OSError:
        logger.debug("readDirectory: opendir on [%s] failed", directory)
        return []
    kept = [name for name in entries
            if _entry_is_listable(str(directory), name)]
    return sort_filenames(kept)


# ---------------------------------------------------------------------------
# folder_id resolution
# ---------------------------------------------------------------------------


def resolve_folder_id(folder_id: Any) -> str | None:
    """Resolve a ``folder_id`` token to a directory path.

    Perl resolves it as a numeric ``Track`` id
    (``Queries.pm:2311-2316`` → ``findAndTrackDirectoryTree``, ``Misc.pm:1067-1074``).
    Our browse items carry the file URL as ``id`` (see the module docstring), so
    a client can send back three shapes: a numeric track id, a ``file://`` URL,
    or a plain path.  All three are accepted; ``None`` means "not resolvable".
    """
    token = str(folder_id).strip()
    if not token:
        return None

    if token.lstrip("-").isdigit():                    # Perl: numeric Track id
        url = _track_url_by_id(int(token))
        if url is None:
            return None
        return _real_case(path_from_file_url(url))

    if token.startswith(_URL_SCHEMES):
        path = path_from_file_url(token)
        # ``path_from_file_url`` returns '' for a URL Perl rejects
        # (``Slim/Utils/Misc.pm:271-274``: no '..' in file URLs).
        return _real_case(path) if path else None

    return token


def _real_case(path: str) -> str:
    """The path with the case the filesystem actually uses.

    On Windows/macOS a client may hand back a differently-cased path for a
    folder that exists; Perl resolves that with ``noCaseFilename``
    (``Slim/Utils/OS.pm:388-390``) and ``Win32::GetLongPathName``
    (``Slim/Utils/OS/Win32.pm:247-289``).  On a case-sensitive filesystem the
    input is returned unchanged.
    """
    if not path:
        return path
    from lyrion.platform import paths as platform_paths

    if not platform_paths.fs_is_case_insensitive():
        return path
    return str(platform_paths.resolve_existing_case(path))


_RO_CONN: sqlite3.Connection | None = None
_RO_CONN_PATH: str | None = None


def _ro_connection(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection | None:
    """A cached **read-only** SQLite connection to the library DB.

    Browsing one folder asks for the numeric id of every entry (Perl's
    ``objectForUrl`` — ``Slim/Utils/Misc.pm:1098-1103``); opening a fresh
    connection per entry would mean hundreds of opens per request.  The
    connection is opened with ``mode=ro``: folder browsing never writes.
    """
    global _RO_CONN, _RO_CONN_PATH
    if db_path is None:
        try:
            from lyrion.config import get_config

            db_path = get_config().db_path
        except Exception:  # pragma: no cover - defensive
            return None
    path = str(db_path)
    if not os.path.exists(path):
        return None
    if _RO_CONN is not None and _RO_CONN_PATH == path:
        return _RO_CONN
    try:
        con = sqlite3.connect(
            f"file:{urllib.parse.quote(path)}?mode=ro", uri=True
        )
    except sqlite3.Error:
        return None
    _RO_CONN = con
    _RO_CONN_PATH = path
    return con


def _rw_connection(db_path: str | os.PathLike[str] | None = None
                   ) -> sqlite3.Connection | None:
    """A **writable** connection to the library DB (Perl's ``commit => 1``).

    ``objectForUrl({create => 1, commit => 1})`` writes the directory row
    (``Slim/Utils/Misc.pm:1082-1090``), so the folder-id path needs a writable
    handle — unlike :func:`_ro_connection`, which the browse reads with.
    ``None`` when no library DB is reachable; the caller then keeps its
    non-Perlish URL token.
    """
    path = str(db_path) if db_path is not None else None
    if path is None:
        try:
            from lyrion.config import get_config

            path = str(get_config().db_path)
        except Exception:  # pragma: no cover - defensive, config is always there
            return None
    if not path or not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        return None
    return con


def _folder_id(path: str, fallback: str,
               dir_ids: dict[str, int] | None = None) -> Any:
    """Perl folder id of a directory: its ``tracks`` row id.

    ``Slim/Control/Queries.pm:2429`` adds ``id => $item->id()`` for an item
    built with ``objectForUrl({url, create => 1})`` (:2263-2268) — the row is
    created while browsing.  ``dir_ids`` is the batch of rows written for one
    listing by :func:`_dir_ids_for`; without a row the pre-D4 URL token is
    kept (see the module docstring).
    """
    if dir_ids:
        hit = dir_ids.get(os.path.normpath(path))
        if hit is not None:
            return hit
    return fallback


def _dir_ids_for(directories: Iterable[str]) -> dict[str, int]:
    """Write/look up the directory rows of one listing (Perl ``create => 1``)."""
    con = _rw_connection()
    if con is None:
        return {}
    try:
        return dir_rows.ensure_dir_ids(directories, conn=con)
    finally:
        try:
            con.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


def reset_caches() -> None:
    """Drop the cached read-only connection (tests / after a rescan)."""
    global _RO_CONN, _RO_CONN_PATH
    if _RO_CONN is not None:
        try:
            _RO_CONN.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
    _RO_CONN = None
    _RO_CONN_PATH = None


def _track_url_by_id(track_id: int) -> str | None:
    """Look up ``tracks.url`` by id (read-only), or ``None``."""
    con = _ro_connection()
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT url FROM tracks WHERE id = ? LIMIT 1", (track_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def _track_id_by_url(url: str) -> int | None:
    """Numeric ``tracks.id`` for a file URL — Perl uses it as the folder id.

    Exact match first.  On a case-insensitive filesystem (Windows, macOS) a
    path that differs only in case still denotes the same object, so a
    ``COLLATE NOCASE`` retry follows — Perl normalises the same way with
    ``noCaseFilename`` = ``lc(Info::fileName)`` (``Slim/Utils/OS.pm:388-390``,
    ``Slim/Utils/OS/Win32.pm:318-321``).  SQLite's ``NOCASE`` folds ASCII only,
    which is the same limit as Perl's byte-wise ``lc`` on these names.
    """
    con = _ro_connection()
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT id FROM tracks WHERE url = ? LIMIT 1", (url,)
        ).fetchone()
        if row is None or row[0] is None:
            from lyrion.platform import paths as platform_paths

            if platform_paths.fs_is_case_insensitive():
                row = con.execute(
                    "SELECT id FROM tracks WHERE url = ? COLLATE NOCASE LIMIT 1",
                    (url,),
                ).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


# ---------------------------------------------------------------------------
# The query itself
# ---------------------------------------------------------------------------


def item_type(path: str) -> str:
    """The ``type`` Perl puts on a ``folder_loop`` item.

    ``Slim/Control/Queries.pm:2472-2485``: ``folder`` for a directory,
    ``playlist`` for a playlist file (``isPlaylist``, ``Info.pm:1313-1318``),
    ``track`` for a song (``isSong`` ⇒ slim-type ``audio``,
    ``Info.pm:1262-1276``), ``unknown`` for everything else.
    """
    if os.path.isdir(path):
        return "folder"
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix in PLAYLIST_EXTENSIONS:
        return "playlist"
    if suffix in PERL_SONG_SUFFIXES or suffix in SUPPORTED_EXTENSIONS:
        return "track"
    return "unknown"


def _is_dir_entry(path: str) -> bool:
    """Is this listing entry a directory (Perl ``isDir``)?

    ``Slim/Music/Info.pm:1278-1281`` ``sub isDir`` → ``_isType($pathOrObj,
    'dir')``, which comes out of ``typeFromPath``'s ``# sanity check for
    folders`` block (``if ($filepath && -d $filepath) { $type = 'dir' }``,
    ``Slim/Music/Info.pm:1482-1484``).  That is exactly
    :func:`item_type`, so the id decision and the ``type`` decision of a
    ``folder_loop`` item use the same test.
    """
    return item_type(path) == "folder"


def _folder_item(folder: str, *, volatile: bool = False, tags: str = "",
                 dir_ids: dict[str, int] | None = None) -> dict:
    """One ``folder_loop`` entry for a directory, Perl shaped.

    Perl: ``id`` (:2429), ``filename`` (:2430), ``type`` (:2472-2487) and the
    optional tag fields ``coverid``/``duration``/``textkey``/``url``/``title``
    (:2489-2497) plus ``ct`` for the ``o`` tag (:2494-2496).  ``filename`` is
    the *basename* (``Info::fileName``), and a not-yet-scanned ("volatile")
    folder in the browse root is shown bracketed (:2423-2427).
    """
    url = file_url_from_path(folder)
    name = os.path.basename(folder.rstrip("/")) or folder
    display = f"[{name}]" if volatile else name
    entry: dict = {
        "id": _folder_id(folder, url, dir_ids),
        "filename": display,
        "type": "folder",
    }
    if "s" in tags:
        entry["textkey"] = display[:1].upper()
    if "u" in tags:
        entry["url"] = url
    if "t" in tags:
        entry["title"] = display
    if "o" in tags:
        # Perl ``$item->content_type`` — 'dir' for a directory (Info.pm:1482-1484).
        entry["ct"] = dir_rows.DIR_CONTENT_TYPE
    return entry


def normalize(index: Any, quantity: Any, count: int) -> tuple[bool, int, int]:
    """Perl ``Request::normalize`` — index/quantity → ``(valid, start, end)``."""
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0
    if quantity is None:
        quantity = count
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return (False, 0, 0)
    if not quantity or not count:
        return (False, 0, 0)
    last = count - 1
    if index > last:
        return (False, 0, 0)
    if index < 0:
        index = 0
    start = index
    end = start + quantity - 1
    if end > last:
        end = last
    return (True, start, end)


def mediafolder_result(
    index: Any = 0,
    quantity: Any = 0,
    *,
    folder_id: Any = None,
    url: Any = None,
    media_type: str = "",
    recursive: bool = False,
    tags: str = "",
) -> dict:
    """Perl ``mediafolderQuery`` — ``Slim/Control/Queries.pm:2169-2507``.

    Returns exactly the Perl result shape for the JSON-RPC handlers::

        {"count": 303,
         "folder_loop": [{"id": …, "filename": "Accept", "type": "folder"}, …]}

    ``quantity == 0`` yields ``{"count": n}`` only, like Perl (the ``normalize``
    guard at ``Queries.pm:2385`` skips the loop).
    """
    media_dirs = effective_media_dirs(media_type or "audio")

    # Perl :2208-2215 — folders disabled for audio are browsable "volatile"
    # (tmp://) entries, but only for the audio/unspecified media type.
    volatile_dirs: list[str] = []
    if not media_type or media_type == "audio":
        volatile_dirs = [d for d in get_inactive_audio_dirs() if d]
    all_dirs = media_dirs + volatile_dirs

    target: str | None = None
    listing_roots = False
    if url:
        target = path_from_file_url(str(url))
    elif folder_id not in (None, ""):
        target = resolve_folder_id(folder_id)
    elif len(all_dirs) > 1:
        # Perl :2272-2278 — more than one root and no datum: list the roots.
        listing_roots = True
    elif all_dirs:
        target = all_dirs[0]

    if listing_roots:
        # One directory test for id *and* ``type``: ``item_type`` is the port's
        # ``Slim::Music::Info::typeFromPath``/``isDir`` (``Slim/Music/Info.pm:
        # 1278-1281`` ``sub isDir``, ``:1482-1484`` ``$type = 'dir'`` for
        # ``-d $filepath``).  ``os.path.isdir`` here would bypass it and hand
        # out a ``type 'folder'`` item without the ``tracks`` row Perl creates
        # for it (:2263-2268).
        dir_paths = [d for d in all_dirs if _is_dir_entry(d)]
        dir_ids = _dir_ids_for(dir_paths)
        items = [
            _folder_item(d, volatile=(d in volatile_dirs), tags=tags,
                         dir_ids=dir_ids)
            for d in dir_paths
        ]
    else:
        names = (list_directory_entries(target, recursive=recursive)
                 if target else [])
        # Perl writes a ``dir`` row for every directory it lists (the browse
        # creates them, ``Slim/Control/Queries.pm:2263-2268`` /
        # ``Slim/Utils/Misc.pm:1082-1090``); one batch for the whole listing.
        # Same directory test as ``_child_item`` (Perl ``isDir``).
        dir_ids: dict[str, int] = {}
        if target and names:
            dir_ids = _dir_ids_for(
                [os.path.join(target, n) for n in names
                 if _is_dir_entry(os.path.join(target, n))])
        items = ([_child_item(target, name, tags=tags, dir_ids=dir_ids)
                  for name in names] if target else [])

    count = len(items)
    valid, start, end = normalize(index, quantity, count)
    result: dict = {"count": count}
    if valid:
        result["folder_loop"] = items[start:end + 1]
    return result


def _child_item(directory: str, name: str, *, tags: str = "",
                dir_ids: dict[str, int] | None = None) -> dict:
    """One ``folder_loop`` entry for a child of ``directory`` (Perl :2429-2487).

    A child directory gets its ``tracks`` row id (Perl creates the ``dir`` row
    while browsing, :2263-2268); a file keeps its own ``tracks.id``
    (``media/folders.py`` ``_track_id_by_url``).
    """
    full = os.path.join(directory, name)
    url = file_url_from_path(full)
    # One decision for "is a directory" — ``item_type`` is Perl's
    # ``isDir``/``isSong`` branch (``Slim/Control/Queries.pm:2472-2485``); a
    # *folder* row gets the id of its ``tracks`` row, a file keeps its own
    # ``tracks.id`` (``_track_id_by_url``).
    kind = item_type(full)
    is_dir = _is_dir_entry(full)
    entry: dict = {
        "id": (_folder_id(full, url, dir_ids) if is_dir
               else (_track_id_by_url(url) or url)),
        "filename": name,
        "type": kind,
    }
    if "s" in tags:
        entry["textkey"] = name[:1].upper()
    if "u" in tags:
        entry["url"] = url
    if "t" in tags:
        entry["title"] = name
    if "o" in tags:
        # Perl ``$item->content_type`` for the ``o`` tag (:2494-2496): 'dir'
        # for a directory, otherwise the type derived from the file suffix
        # (``Slim/Music/Info.pm:1445-1453`` ``typeFromSuffix``).
        entry["ct"] = _content_type_for(full) if not is_dir else "dir"
    return entry


def _content_type_for(path: str) -> str:
    """Perl 3-letter content type of a file (``typeFromSuffix``, Info.pm:1445)."""
    try:
        from lyrion.formats.lms_types import type_from_suffix

        return type_from_suffix(path) or ""
    except Exception:  # noqa: BLE001 - type table optional here
        return ""


#: ``musicfolderQuery`` is a thin alias of ``mediafolderQuery``
#: (``Slim/Control/Queries.pm:2161-2167``).
musicfolder_result = mediafolder_result
