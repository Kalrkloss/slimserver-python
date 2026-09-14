"""Cross-platform paths, directories and file URLs — Perl's rules, in one place.

Why this module exists
----------------------
Perl LMS isolates every OS assumption in ``Slim::Utils::OS*``: the base class
``Slim::Utils::OS`` plus one subclass per platform, dispatched by
``Slim::Utils::OSDetect``.  Each subclass answers ``dirsFor($kind)`` for
``prefs``/``cache``/``log``/``music``/``playlists`` and the platform quirks
(user/group switching, log rotation, shortcut/file-name handling).

The Python port used to hardcode a Linux home path (``~/.lyrion``) in
``config.py`` and to build ``file://`` URLs by string concatenation.  Both break
on Windows and on macOS.  This module is the single place where that knowledge
now lives; ``config.py``, ``media/*`` and ``database/*`` delegate to it.

Perl reference (read-only checkout ``/tmp/lms-ref``, upstream ``public/9.0``)
---------------------------------------------------------------------------
* ``Slim/Utils/OSDetect.pm:47-135``   OS dispatch (``darwin`` → OSX,
  ``m?s?win`` → Win32/Win64, ``linux`` → Linux/Debian/…, else Unix) and the
  ``dirsFor``/``details`` passthroughs.
* ``Slim/Utils/OS.pm:173-207``        base ``dirsFor`` (``Plugins``, ``updates``).
* ``Slim/Utils/OS.pm:215-242``        ``logRotate`` — Windows/macOS only; a
  no-op on Unix (``Slim/Utils/OS/Unix.pm:130`` "leave log rotation to the
  system"), ``MAX_LOGSIZE`` 100 MB (``OS.pm:17``).
* ``Slim/Utils/OS.pm:388-390``        ``noCaseFilename`` = ``lc(Info::fileName)``.
* ``Slim/Utils/OS.pm:368-383``        ``sortFilename`` (locale collation).
* ``Slim/Utils/OS.pm:302-307``        ``ignoredItems`` (Linux: ``lost+found``).
* ``Slim/Utils/OS/Unix.pm:50-113``    Unix ``dirsFor``: ``log`` → ``$::logdir``
  or ``$Bin/Logs``, ``cache`` → ``$::cachedir`` or ``$Bin/Cache``, ``prefs`` →
  ``$::prefsdir`` or ``$Bin/prefs``, ``music``/``playlists`` → ``''`` (no
  default on Unix).
* ``Slim/Utils/OS/Debian.pm:77-91``   packaged layout:
  ``/var/lib/squeezeboxserver/prefs``, ``/var/log/squeezeboxserver``,
  ``/var/lib/squeezeboxserver/cache``.
* ``Slim/Utils/OS/OSX.pm:153-210``    macOS: prefs →
  ``$HOME/Library/Application Support/Squeezebox``, log → ``$HOME/Library/Logs/Squeezebox``,
  cache → ``$HOME/Library/Caches/Squeezebox``, music → ``$HOME/Music``,
  playlists → ``$HOME/Music/Playlists``.
* ``Slim/Utils/OS/Win32.pm:144-234``  Windows ``dirsFor``: prefs/log/cache →
  ``writablePath(...)``, music → ``Win32::GetFolderPath(CSIDL_MYMUSIC)``,
  playlists → ``music\\Playlists``.
* ``Slim/Utils/OS/Win32.pm:485-560``  ``writablePath`` = registry ``DataPath``
  else ``CSIDL_COMMON_APPDATA`` (``%ProgramData%``) + ``Lyrion``.
* ``Slim/Utils/OS/Win32.pm:331``      ``dontSetUserAndGroup`` — no setuid on
  Windows; ``:318`` ``noCaseFilename`` via locale encoding.
* ``Slim/Utils/OS/Win32.pm:364-379``  ``ignoredItems`` (``System Volume
  Information``, ``RECYCLER``, ``Recycled``, ``$Recycle.Bin``).
* ``Slim/Utils/OS/Win32.pm:247-289``  ``getFileName`` — 8.3 short names and
  locale-encoded names; ``:236`` ``decodeExternalHelperPath`` =
  ``Win32::GetShortPathName``.
* ``Slim/Utils/OS/Win64.pm:55-89``    ``runService`` — Windows services are
  installed by the installer, not by the server itself.
* ``Slim/Utils/Misc.pm:307-351``      ``fileURLFromPath`` (``URI::file``, host
  cleared, trailing-whitespace workaround).
* ``Slim/Utils/Misc.pm:223-301``      ``pathFromFileURL`` (backslashes in
  URLs, ``..`` rejected, ``$uri->path``/``$uri->file`` semantics).
* ``Slim/Utils/Prefs.pm:85-95``       ``prefsdir`` from the command line, else
  ``OSDetect::dirsFor('prefs')``; ``:687-712`` ``defaultMediaDirs`` uses
  ``dirsFor('music')``.

Deliberate deviations (documented, not accidental)
--------------------------------------------------
1. **Linux data root.**  Perl running from source keeps prefs in ``$Bin/prefs``
   (``Unix.pm:107-109``) and the Debian package in
   ``/var/lib/squeezeboxserver/prefs`` (``Debian.pm:79``).  A Python
   installation is a package inside ``site-packages``/a venv, where writing is
   not allowed, so the port keeps its per-user data root
   ``~/.lyrion/Lyrion`` — the equivalent of the packaged ``/var/lib`` location.
   Moving it to ``/var/lib/lyrion`` once a package exists is *proposed*, not
   performed (it would move a live installation's data).
2. **Migration wins.**  When an existing data directory from an earlier
   version is found, it keeps being used
   (:func:`resolve_serverdata_dir`), so an upgrade never silently starts with
   an empty library.
3. **Library DB location.**  Perl stores ``library.db`` in the *cache* dir; the
   port stores ``Prefs/lyrion.db``.  Kept (a live installation's data), noted
   in ``references/cross-platform.md``.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import stat
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OS families — the same names Perl's OS classes report
# ---------------------------------------------------------------------------

#: ``Slim::Utils::OS::Win32->name`` returns ``'win'`` (OSDetect.pm:132).
WIN = "win"
#: ``Slim::Utils::OS::OSX->name`` returns ``'mac'`` (OSDetect.pm:133).
MAC = "mac"
#: ``Slim::Utils::OS::Linux`` reports ``os eq 'Linux'`` (OSDetect.pm:134).
LINUX = "linux"
#: ``Slim::Utils::OS::Unix->name`` (Unix.pm:16-18) — every other POSIX.
UNIX = "unix"

#: Perl's package-specific Linux flavours that own fixed system directories
#: (``Slim/Utils/OS/Debian.pm:77-91``, ``RedHat.pm``, ``Suse.pm``); they are
#: only used when the server was started from the packaged launcher
#: (``OSDetect.pm:94-108``).
PACKAGED_DIRS = {
    "prefs": Path("/var/lib/squeezeboxserver/prefs"),
    "cache": Path("/var/lib/squeezeboxserver/cache"),
    "log": Path("/var/log/squeezeboxserver"),
}

#: Perl ``ignoredItems`` — ``Slim/Utils/OS.pm:302-307`` (Linux).
IGNORED_ITEMS_UNIX = frozenset({"lost+found"})
#: Perl ``ignoredItems`` — ``Slim/Utils/OS/Win32.pm:364-379``.
IGNORED_ITEMS_WIN = frozenset({
    "System Volume Information", "RECYCLER", "Recycled", "$Recycle.Bin",
})

#: Perl ``MAX_LOGSIZE`` — ``Slim/Utils/OS.pm:17`` (100 MB).
MAX_LOG_SIZE = 1024 * 1024 * 100


def current_os() -> str:
    """Return the OS family the way Perl's ``OSDetect`` names it.

    ``Slim/Utils/OSDetect.pm:72-127``: ``darwin`` → OSX (``mac``), ``m?s?win``
    → Win32/Win64 (``win``), ``linux`` → Linux, everything else → Unix.
    """
    system = platform.system().lower()
    if system == "windows":
        return WIN
    if system == "darwin":
        return MAC
    if system == "linux":
        return LINUX
    return UNIX


def _os_name(os_name: str | None) -> str:
    return (os_name or current_os()).lower()


def is_windows(os_name: str | None = None) -> bool:
    return _os_name(os_name) in (WIN, "windows", "mswin32")


def is_mac(os_name: str | None = None) -> bool:
    return _os_name(os_name) in (MAC, "darwin", "macos")


def is_linux(os_name: str | None = None) -> bool:
    return _os_name(os_name) in (LINUX, "linux")


def install_root() -> Path:
    """The equivalent of Perl's ``$Bin`` — the directory the server runs from.

    ``FindBin::Bin`` points at the *installation* directory in Perl
    (``Slim/Utils/OS/Unix.pm:11``); for the port that is the repository or
    site-packages root: ``<root>/src/lyrion/platform/paths.py`` → ``<root>``.
    """
    return Path(__file__).resolve().parents[3]


def _home(home: str | os.PathLike[str] | None) -> Path:
    if home is not None:
        return Path(home)
    return Path.home()


def _win_path(*parts: str) -> Path:
    """Build a Windows path with forward slashes.

    ``pathlib.Path`` on a POSIX host would keep the backslashes of
    ``C:\\ProgramData`` as literal name characters, so the drive prefix is
    normalised first.  On a real Windows host both spellings are identical.
    """
    return Path("/".join(str(p).replace("\\", "/") for p in parts))


# ---------------------------------------------------------------------------
# dirsFor — Perl's per-OS directory table
# ---------------------------------------------------------------------------


def dirs_for(
    kind: str,
    *,
    os_name: str | None = None,
    home: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    packaged: bool = False,
) -> list[Path]:
    """Perl ``dirsFor($kind)`` — the candidate directories for ``kind``.

    ``kind`` is one of ``prefs``, ``cache``, ``log``, ``music``, ``playlists``.
    The first element is the directory Perl uses; the list shape mirrors
    Perl's ``wantarray`` return (``Slim/Utils/OS.pm:206``).  An empty list
    means "Perl has no default for this kind on this OS" — on Unix that is
    ``music``/``playlists`` (``Slim/Utils/OS/Unix.pm:68-71``).

    ``packaged=True`` selects the Linux distributor layout
    (``Slim/Utils/OS/Debian.pm:77-91``), which Perl only picks when the server
    was started from ``/usr/sbin/squeezeboxserver``/``/usr/libexec/lyrionmusicserver``
    (``Slim/Utils/OSDetect.pm:94-108``).
    """
    osname = _os_name(os_name)
    environ = env if env is not None else os.environ
    h = _home(home)

    if is_windows(osname):
        # Win32.pm:485-560 writablePath(): registry DataPath, else
        # CSIDL_COMMON_APPDATA (%ProgramData%) + 'Lyrion' (Win32.pm:559).
        data_root = _win_path(environ.get("PROGRAMDATA") or "C:/ProgramData", "Lyrion")
        if kind == "prefs":
            # Win32.pm:185-187 → writablePath('prefs'); note the lowercase name.
            return [data_root / "prefs"]
        if kind == "cache":
            # Win32.pm:157-159 → writablePath('Cache').
            return [data_root / "Cache"]
        if kind == "log":
            # Win32.pm:153-155 → writablePath('Logs'); logRotate is enabled on
            # Windows (OS.pm:211-242).
            return [data_root / "Logs"]
        if kind in ("music", "playlists"):
            # Win32.pm:189-223 → Win32::GetFolderPath(CSIDL_MYMUSIC), i.e.
            # %USERPROFILE%\Music; playlists is '<music>\Playlists' (:219-221).
            profile = environ.get("USERPROFILE")
            music = _win_path(profile, "Music") if profile else h / "Music"
            return [music / "Playlists"] if kind == "playlists" else [music]
        return []

    if is_mac(osname):
        app_support = h / "Library" / "Application Support" / "Squeezebox"
        if kind == "prefs":
            # OSX.pm:173-175 — no 'Prefs' subdirectory on macOS.
            return [app_support]
        if kind == "cache":
            # OSX.pm:171-174.
            return [h / "Library" / "Caches" / "Squeezebox"]
        if kind == "log":
            # OSX.pm:169-171.
            return [h / "Library" / "Logs" / "Squeezebox"]
        if kind == "music":
            # OSX.pm:183-199 — ~/Music (alias expansion at :190-195 is a
            # Finder-alias feature with no Python equivalent; noted in
            # references/cross-platform.md).
            return [h / "Music"]
        if kind == "playlists":
            # OSX.pm:203-205.
            return [h / "Music" / "Playlists"]
        return []

    # Linux and the other Unixes.
    if packaged and kind in PACKAGED_DIRS:
        return [PACKAGED_DIRS[kind]]
    bin_dir = install_root()
    if kind == "prefs":
        # Unix.pm:75-77 uses $::prefsdir (the --prefsdir command line option,
        # Prefs.pm:89-92) and falls through to $Bin/prefs (:107-109).
        return [bin_dir / "prefs"]
    if kind == "cache":
        # Unix.pm:64-66.
        return [bin_dir / "Cache"]
    if kind == "log":
        # Unix.pm:60-62.
        return [bin_dir / "Logs"]
    # Unix.pm:68-71 — Perl returns the empty string here: no default music
    # folder on Unix/Linux.
    return []


def default_music_dir(
    *,
    os_name: str | None = None,
    home: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Perl ``OSDetect::dirsFor('music')`` — the OS music folder, or ``None``.

    ``None`` mirrors Perl returning ``''`` on Unix; the caller must not invent
    a folder (``Slim/Utils/Prefs.pm:700-710`` only pushes the path when it is a
    real directory).
    """
    dirs = dirs_for("music", os_name=os_name, home=home, env=env)
    return dirs[0] if dirs else None


# ---------------------------------------------------------------------------
# The port's own data root (see the module docstring, deviations 1 and 2)
# ---------------------------------------------------------------------------

#: Data roots of earlier versions of the port, newest first.  Used by
#: :func:`resolve_serverdata_dir` so an upgrade keeps its library.
LEGACY_SERVERDATA_DIRS: tuple[str, ...] = ("Lyrion", "")


def _looks_like_serverdata(path: Path) -> bool:
    """True when ``path`` holds data of an existing installation.

    A directory counts when one of the three sub-directories or the prefs DB
    is there — the same set the initialisation code writes.
    """
    if not path.is_dir():
        return False
    markers = ("Prefs", "prefs", "Cache", "cache", "Logs", "logs", "Lyrion.conf")
    if any((path / m).exists() for m in markers):
        return True
    return (path / "prefs.db").is_file()


def default_serverdata_dir(
    *,
    os_name: str | None = None,
    home: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """The port's per-OS data root.

    * Windows — ``%ProgramData%\\Lyrion`` (Perl ``writablePath``,
      ``Win32.pm:485-560``).
    * macOS — ``~/Library/Application Support/Squeezebox`` (``OSX.pm:175``).
    * Linux/Unix — ``~/.lyrion/Lyrion`` (deviation 1 in the module docstring:
      Perl's ``$Bin/prefs`` is not writable inside a Python installation, and
      Perl's packaged ``/var/lib/squeezeboxserver`` needs a package).
    """
    osname = _os_name(os_name)
    h = _home(home)
    if is_windows(osname):
        return dirs_for("prefs", os_name=osname, env=env)[0].parent
    if is_mac(osname):
        return dirs_for("prefs", os_name=osname, home=h, env=env)[0]
    return h / ".lyrion" / "Lyrion"


def resolve_serverdata_dir(
    *,
    cli_value: str | os.PathLike[str] | None = None,
    env_value: str | None = None,
    environ: Mapping[str, str] | None = None,
    os_name: str | None = None,
    home: str | os.PathLike[str] | None = None,
) -> tuple[Path, str]:
    """Resolve the server data directory, Perl-shaped, with migration.

    Order (each step documented with its Perl counterpart):

    1. ``--serverdata DIR`` — the port's equivalent of Perl's ``--prefsdir``
       (``Slim/Utils/Prefs.pm:89-92``) and ``--cachedir``/``--logdir``.
    2. ``$LYRION_SERVERDATA`` — same idea for service managers.
    3. **Migration**: an existing data directory of an earlier version
       (``~/.lyrion/Lyrion``, then ``~/.lyrion``) that actually contains data.
       Without this an upgrade would start with an empty library.
    4. :func:`default_serverdata_dir` for the current OS.

    Returns ``(path, source)`` where ``source`` names the rule that won — it is
    logged at start-up and asserted in the tests.
    """
    environ = environ if environ is not None else os.environ
    if cli_value:
        return Path(cli_value), "cli"
    if env_value is None:
        env_value = environ.get("LYRION_SERVERDATA")
    if env_value:
        return Path(env_value), "env"

    h = _home(home)
    base = h / ".lyrion"
    for suffix in LEGACY_SERVERDATA_DIRS:
        candidate = base / suffix if suffix else base
        if _looks_like_serverdata(candidate):
            return candidate, "migration"

    default = default_serverdata_dir(os_name=os_name, home=h, env=environ)
    return default, "os-default"


#: Sub-directory names inside an explicitly configured ``--serverdata`` root.
#: Perl has no single root, but accepts ``--prefsdir``/``--cachedir``/``--logdir``
#: separately (``Slim/Utils/Prefs.pm:89-92``, ``OS/Unix.pm:60-77``); a single
#: root keeps the port's CLI contract while mapping onto the same three dirs.
PORT_SUBDIRS: dict[str, str] = {"prefs": "Prefs", "cache": "Cache", "log": "Logs"}


def port_dir(
    kind: str,
    *,
    serverdata: str | os.PathLike[str] | None = None,
    os_name: str | None = None,
    home: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """The directory the port uses for ``kind`` (``prefs``/``cache``/``log``).

    * An explicit serverdata root (``--serverdata``/``$LYRION_SERVERDATA``) wins
      and gets ``Prefs``/``Cache``/``Logs`` below it — that is how a running
      installation and the test suite are laid out.
    * Otherwise the OS decides: Windows ``%ProgramData%\\Lyrion\\{prefs,Cache,Logs}``
      and macOS ``~/Library/{Application Support,Caches,Logs}/Squeezebox``
      follow Perl exactly (``Win32.pm:153-187``, ``OSX.pm:169-175``); on
      Linux/Unix the per-user root ``~/.lyrion/Lyrion/{Prefs,Cache,Logs}`` is
      kept (deviation 1 in the module docstring).
    """
    if serverdata:
        return Path(serverdata) / PORT_SUBDIRS[kind]
    osname = _os_name(os_name)
    if is_windows(osname) or is_mac(osname):
        dirs = dirs_for(kind, os_name=osname, home=home, env=env)
        if dirs:
            return dirs[0]
    return default_serverdata_dir(os_name=osname, home=home, env=env) / PORT_SUBDIRS[kind]


# ---------------------------------------------------------------------------
# Path lists
# ---------------------------------------------------------------------------

#: A comma that starts a ``key=value`` pair belongs to a gvfs mount id
#: (``smb-share:server=host,share=name``), not to a list separator.
_LIST_COMMA_RE = re.compile(r",(?![A-Za-z0-9_.\-]+=)")


def split_path_list(value: object) -> list[str]:
    """Split a stored path list into entries, keeping gvfs mount ids intact.

    Perl keeps ``mediadirs``/``ignoreInAudioScan`` as real array refs
    (``Slim/Utils/Prefs.pm:383-403``); this port serialises them comma
    separated, which collides with the commas inside a gvfs mount id —
    ``/run/user/1000/gvfs/smb-share:server=h,share=s/Musik`` would become two
    bogus entries and the whole share would silently disappear from folder
    browsing.  A comma followed by ``key=`` is therefore kept.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw: list[str] = [str(v) for v in value]
    else:
        text = str(value).strip()
        if not text:
            return []
        raw = _LIST_COMMA_RE.split(text)
    return [part.strip() for part in raw if part and part.strip()]


# ---------------------------------------------------------------------------
# File URL <-> path (Perl Slim::Utils/Misc.pm:223-351)
# ---------------------------------------------------------------------------

#: Schemes Perl treats as "already a URL" — ``Info::isURL`` is consulted first
#: in ``fileURLFromPath`` (``Misc.pm:314``) and the browse layer round-trips
#: these untouched.
URL_SCHEMES = ("file://", "tmp://", "http://", "https://", "db:", "spotify:")

#: Segments Perl refuses to turn into a path: ``Misc.pm:271-274`` "(only)
#: allow absolute file URLs and don't allow .. in files...".
_TRAVERSAL_RE = re.compile(r"[\\/]\.\.[\\/]")


def is_url(text: str) -> bool:
    """Perl ``Slim::Music::Info::isURL`` — does ``text`` carry a scheme?"""
    return str(text).startswith(URL_SCHEMES)


def file_url_from_path(
    path: str | os.PathLike[str],
    *,
    os_name: str | None = None,
) -> str:
    """Perl ``fileURLFromPath`` — ``Slim/Utils/Misc.pm:307-351``.

    ``URI::file`` escapes every character that is not path-safe, so a gvfs
    mount id becomes ``smb-share%3Aserver%3Dx%2Cshare%3Dy`` (:322-325) — the
    exact encoding our ``tracks.url`` values use.

    OS handling:

    * POSIX — ``/a/b`` → ``file:///a/b``.
    * Windows — ``C:\\Users\\x\\Music`` → ``file:///C:/Users/x/Music``;
      a UNC path ``\\\\server\\share\\dir`` → ``file://server/share/dir``
      (``URI::file`` keeps the host component).
    * macOS — ``/Volumes/Ext/…`` → ``file:///Volumes/Ext/…`` (a mounted volume
      is just an absolute POSIX path, ``OSX.pm:183-199`` deals in the same
      path space).
    """
    text = str(path)
    if is_url(text):
        # Misc.pm:314 — already a URL, return unchanged.
        return text

    win = is_windows(os_name)
    if win:
        text = text.replace("\\", "/")

    # Misc.pm:331-336, Bug 15511: URI::file strips trailing whitespace, so Perl
    # appends (and later removes) a '/' to defeat that.
    added_slash = False
    if text and (text[-1].isspace() or text[-1] == '"'):
        text = text + "/"
        added_slash = True

    if win and text.startswith("//"):
        # UNC: \\server\share\dir -> file://server/share/dir
        rest = text[2:]
        server, _, tail = rest.partition("/")
        encoded = "file://" + server + "/" + urllib.parse.quote(tail, safe="/")
        return encoded[:-1] if added_slash else encoded

    if win and len(text) >= 2 and text[1] == ":":
        # Drive letter: C:/x -> /C:/x so the URL is absolute.  URI::file keeps
        # the drive colon literal (file:///C:/x), unlike a POSIX path where ':'
        # is escaped (%3A, and that is what our gvfs/SMB URLs already carry).
        text = "/" + text
        safe = "/:"
    else:
        safe = "/"

    if not text.startswith("/"):
        text = "/" + text

    encoded = "file://" + urllib.parse.quote(text, safe=safe)
    if added_slash:
        encoded = encoded[:-1]
    return encoded


def path_from_file_url(
    url: str | os.PathLike[str],
    *,
    os_name: str | None = None,
    allow_traversal: bool = False,
) -> str:
    """Perl ``pathFromFileURL`` — ``Slim/Utils/Misc.pm:223-301``.

    Handles the three shapes that occur in practice:

    * gvfs/SMB — ``file:///run/user/1000/gvfs/smb-share%3Aserver%3D1.2.3.4%2Cshare%3Dmusik/Musik``
      → ``/run/user/1000/gvfs/smb-share:server=1.2.3.4,share=musik/Musik``.
      Percent-decoding uses :func:`urllib.parse.unquote`, **not**
      ``unquote_plus``: a ``+`` in a path component is a literal plus
      (``URI::file`` escapes it as ``%2B`` when going the other way, so the
      round trip is exact — see ``tests/test_musicfolder.py::test_urllib_quote_is_used_for_plus``).
    * Windows — ``file:///C:/Users/x/Music`` → ``C:\\Users\\x\\Music``;
      ``file://server/share/dir`` (UNC) → ``\\\\server\\share\\dir``.
    * macOS — ``file:///Volumes/Ext/Music`` → ``/Volumes/Ext/Music``.

    A URL containing a ``..`` segment is rejected (Perl ``Misc.pm:271-274``):
    the function logs a warning and returns ``""`` unless
    ``allow_traversal=True``.  A non-``file://`` URL is returned unchanged, like
    Perl (``Misc.pm:231-236``).
    """
    text = str(url)
    lowered = text.lower()

    if lowered.startswith("tmp://"):
        # The port's own pseudo scheme (see media/folders.py); Perl has no
        # equivalent, keep the historical behaviour.
        return urllib.parse.unquote(text[6:])

    if not lowered.startswith("file://"):
        # Misc.pm:231-236 — "Path isn't a file URL", returned unchanged.
        return text

    # Misc.pm:255, Bug 3589 — accept Windows backslashes inside URLs.
    body = text[7:].replace("\\", "/")

    if _TRAVERSAL_RE.search(body) and not allow_traversal:
        logger.warning(
            "Refusing file URL with '..' segment (Perl Misc.pm:271-274): %s", text
        )
        return ""

    # URI->path drops the query/fragment; our own URLs percent-encode '?' and
    # '#' so a literal one can only have come from a hand-written URL, exactly
    # like in Perl.
    for sep in ("?", "#"):
        cut = body.find(sep)
        if cut >= 0:
            body = body[:cut]

    win = is_windows(os_name)
    host = ""
    if body and not body.startswith("/"):
        # 'file://host/path' — authority present (note: 'file:///x' yields
        # body '/x' because the scheme is exactly 7 characters long).
        host, _, body = body.partition("/")
        body = "/" + body

    path = urllib.parse.unquote(body)

    if win:
        # Bug 3589 & URI::file: 'file:///C:/x' -> 'C:\x'.
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        if host and host.lower() != "localhost":
            # UNC: 'file://server/share/dir' -> '\\server\share\dir'.
            path = "\\\\" + host + path.replace("/", "\\")
        else:
            path = path.replace("/", "\\")
    elif host and host.lower() != "localhost":
        # POSIX ignores the authority (File::Spec::Unix), 'file://localhost/x'
        # behaves like 'file:///x' (Misc.pm:260-263).
        pass

    if not path:
        return path
    return path


# ---------------------------------------------------------------------------
# gvfs / FUSE mounts
# ---------------------------------------------------------------------------

#: GVfs exposes its mounts under ``$XDG_RUNTIME_DIR/gvfs`` = ``/run/user/<uid>/gvfs``;
#: the mount id is one directory level, e.g.
#: ``smb-share:server=192.168.1.90,share=musik`` or
#: ``sftp:host=example.com,user=x``.
_GVFS_ROOT_RE = re.compile(r"^(?:/run/user/\d+/gvfs|/var/run/user/\d+/gvfs|/run/gvfs)/")
_GVFS_MOUNT_RE = re.compile(
    r"^(?P<root>(?:/run/user/\d+/gvfs|/var/run/user/\d+/gvfs|/run/gvfs)/(?P<id>[^/]+))"
)


def gvfs_runtime_dir(
    *,
    environ: Mapping[str, str] | None = None,
    os_name: str | None = None,
) -> Path | None:
    """The GVfs base directory (``$XDG_RUNTIME_DIR/gvfs``), or ``None``.

    ``None`` on Windows/macOS: gvfs is a Linux (GNOME) FUSE daemon, so a file
    URL pointing at ``/run/user/.../gvfs`` can never be valid there.
    """
    if not is_linux(os_name):
        return None
    environ = environ if environ is not None else os.environ
    runtime = environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "gvfs"
    try:
        return Path(f"/run/user/{os.getuid()}") / "gvfs"
    except AttributeError:  # pragma: no cover - Windows is handled above
        return None


def is_gvfs_path(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` lives inside a GVfs mount tree."""
    return bool(_GVFS_ROOT_RE.match(str(path)))


def gvfs_mount_root(path: str | os.PathLike[str]) -> Path | None:
    """The mount point containing ``path`` — the directory GVfs created.

    For ``/run/user/1000/gvfs/smb-share:server=h,share=s/Musik`` this is
    ``/run/user/1000/gvfs/smb-share:server=h,share=s``.  ``None`` when the path
    is not a gvfs path at all.
    """
    match = _GVFS_MOUNT_RE.match(str(path))
    return Path(match.group("root")) if match else None


def _proc_mount_points() -> set[str]:
    """Mount points from ``/proc/self/mounts`` (read-only, Linux only)."""
    points: set[str] = set()
    try:  # pragma: no cover - /proc is Linux-only but the read is trivial
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    points.add(parts[1].replace("\\040", " "))
    except OSError:
        pass
    return points


def _is_mount_point(path: Path) -> bool:
    """True when ``path`` is a mount point.

    ``os.path.ismount`` covers FUSE/gvfs on Linux and drive letters/UNC roots
    on Windows; ``/proc/self/mounts`` is consulted as a second, independent
    source on Linux (both read-only, no shelling out to ``gio``/``mount``).
    """
    try:
        if os.path.ismount(str(path)):
            return True
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    return str(path) in _proc_mount_points()


def gvfs_mount_state(path: str | os.PathLike[str]) -> str:
    """Classify a gvfs path: ``mounted``, ``not-mounted``, ``empty`` or ``not-gvfs``.

    ``GVfs`` (the FUSE daemon behind ``/run/user/<uid>/gvfs``) **removes** the
    mount directory when the share is unmounted, so the interesting cases are
    distinguishable without any system change:

    * ``not-mounted`` — the mount directory is gone, or it exists as a plain
      (empty, non-FUSE) directory: the share is not attached.
    * ``empty`` — the mount point exists and is a real mount, but lists nothing.
    * ``mounted`` — mount point exists, is FUSE-mounted and has entries.

    This is what makes "file missing" distinguishable from "mount not
    mounted" (:func:`explain_missing_path`), and it never mounts anything.
    """
    root = gvfs_mount_root(path)
    if root is None:
        return "not-gvfs"
    if not root.exists():
        return "not-mounted"
    if not _is_mount_point(root):
        return "not-mounted"
    try:
        with os.scandir(root) as it:
            return "mounted" if any(True for _ in it) else "empty"
    except OSError:
        return "not-mounted"


# ---------------------------------------------------------------------------
# Missing-path diagnostics
# ---------------------------------------------------------------------------

#: Reasons :func:`explain_missing_path` can report.
OK = "ok"
MISSING = "missing"
MOUNT_NOT_MOUNTED = "mount-not-mounted"
NOT_READABLE = "not-readable"
EMPTY = "empty"


def explain_missing_path(path: str | os.PathLike[str]) -> tuple[str, str]:
    """Why ``path`` cannot be used — ``(code, message)``.

    Distinguishes the two situations that look identical from the outside:

    * ``missing`` — the directory is there, the item simply is not in it.
    * ``mount-not-mounted`` — a gvfs/FUSE share is not attached, so the whole
      tree (folder *and* its files) is unreachable.  This is the "mount gone →
      metadata yes, sound no" case; the server never auto-mounts
      (``references/cross-platform.md``).

    Also reports ``not-readable`` (exists, but ``stat``/listing is denied) and
    ``empty`` (usable directory without entries).  ``ok`` means "fine".
    """
    target = Path(path)
    text = str(target)

    if is_gvfs_path(text):
        state = gvfs_mount_state(text)
        root = gvfs_mount_root(text)
        if state in ("not-mounted",):
            return MOUNT_NOT_MOUNTED, (
                f"gvfs mount is not mounted: {root} — {text} is unreachable "
                f"(the server does not mount shares by itself)"
            )
        if state == "empty" and not target.exists():
            return MOUNT_NOT_MOUNTED, (
                f"gvfs mount {root} is attached but empty — {text} is unreachable"
            )

    try:
        st = os.stat(text)
    except FileNotFoundError:
        return MISSING, f"missing: {text} (the parent folder exists and is mounted)"
    except PermissionError:
        return NOT_READABLE, f"not readable (permissions): {text}"
    except OSError as exc:
        return MISSING, f"missing: {text} ({exc})"

    if stat.S_ISDIR(st.st_mode):
        try:
            with os.scandir(text) as it:
                if not any(True for _ in it):
                    return EMPTY, f"exists but is empty: {text}"
        except PermissionError:
            return NOT_READABLE, f"not readable (permissions): {text}"
        except OSError as exc:
            return NOT_READABLE, f"not readable: {text} ({exc})"
    return OK, ""


def media_dir_problems(
    dirs: list[str] | tuple[str, ...] | None,
) -> list[tuple[str, str, str]]:
    """Check configured music folders — ``[(path, code, message), …]``.

    Only entries that are *not* ``ok`` are returned.  Used by the start-up
    warning (:func:`warn_about_media_dirs`) so an operator sees immediately
    that the configured music folder is missing, unmounted, unreadable or
    empty — instead of wondering why the library is silent.
    """
    problems: list[tuple[str, str, str]] = []
    for raw in dirs or []:
        text = str(raw).strip()
        if not text:
            continue
        code, message = explain_missing_path(text)
        if code != OK:
            problems.append((text, code, message))
    return problems


def warn_about_media_dirs(
    dirs: list[str] | tuple[str, ...] | None,
    *,
    log: logging.Logger | None = None,
) -> list[tuple[str, str, str]]:
    """Log one warning per unusable configured music folder.

    Perl sets the default media dir from the OS music folder and simply skips
    it when it is not a directory (``Slim/Utils/Prefs.pm:700-710``); the port
    additionally shouts at start-up, because "metadata but no sound" (mount
    gone) is otherwise indistinguishable from a library that was never
    scanned.

    Returns the problems list so callers (and tests) can assert on it.
    """
    loggr = log or logger
    problems = media_dir_problems(dirs)
    if not (dirs or []):
        loggr.warning(
            "No music folder is configured (pref 'mediadirs'/'audiodir'/'musicdir' "
            "are all empty) — the library will stay empty"
        )
        return problems
    for text, code, message in problems:
        if code == MOUNT_NOT_MOUNTED:
            loggr.warning("Music folder not mounted: %s", message)
        elif code == MISSING:
            loggr.warning(
                "Music folder does not exist: %s — check the share and the "
                "'mediadirs' preference", message,
            )
        elif code == NOT_READABLE:
            loggr.warning("Music folder not readable: %s", message)
        elif code == EMPTY:
            loggr.warning("Music folder is empty: %s", message)
    return problems


# ---------------------------------------------------------------------------
# Case-insensitive filesystems (Windows, macOS)
# ---------------------------------------------------------------------------


def fs_is_case_insensitive(
    os_name: str | None = None,
    *,
    path: Path | None = None,
) -> bool:
    """True on the filesystems that fold case (Windows, macOS default).

    Perl solves this with ``noCaseFilename`` (``Slim/Utils/OS.pm:388-390``:
    ``lc(Info::fileName)``), overridden by Win32 to encode the locale first
    (``Win32.pm:318-321``).  We mirror the ``lc()`` semantics — a plain
    ``str.lower()`` (:func:`casefold_key`), *not* ``casefold()``: Perl lowercases
    one code point at a time, and the DB values were written that way.
    """
    if os_name is None and path is not None:
        # Probe: a case-folded lookup of an existing entry.
        try:
            with os.scandir(path.parent) as it:
                for entry in it:
                    if entry.name.lower() == path.name.lower() and entry.name != path.name:
                        return True
        except OSError:
            pass
    return is_windows(os_name) or is_mac(os_name)


def casefold_key(name: str) -> str:
    """Perl ``noCaseFilename`` — ``lc()`` of a file name (``OS.pm:388-390``)."""
    return str(name).lower()


def resolve_existing_case(
    path: str | os.PathLike[str],
    *,
    os_name: str | None = None,
) -> Path:
    """Return ``path`` with the *real* case of every component.

    On a case-insensitive filesystem a client (or an old DB row) may carry a
    differently-cased path — Perl normalises with ``noCaseFilename``
    (``OS.pm:388-390``) and ``getFileName`` on Windows expands the name the
    filesystem reports (``Win32.pm:247-289``).  This walks the components and
    substitutes the on-disk spelling; when nothing matches, the input is
    returned unchanged so callers can still produce a proper "missing" message.
    """
    p = Path(path)
    if not fs_is_case_insensitive(os_name):
        return p
    if p.exists():
        return p
    parts = p.parts
    if not parts:
        return p
    current = Path(parts[0])
    for part in parts[1:]:
        try:
            with os.scandir(current) as it:
                lookup = {entry.name.lower(): entry.name for entry in it}
        except OSError:
            return p
        real = lookup.get(part.lower())
        if real is None:
            return p
        current = current / real
    return current


def sqlite_url_for_path(path: str | os.PathLike[str], *, read_only: bool = False) -> str:
    """Build an SQLAlchemy/sqlite3 URL for ``path``.

    ``pathlib``/``str`` + naive concatenation produces ``sqlite:///C:\\x\\y.db``
    which is invalid; the path must be percent-encoded (and drive letters kept
    verbatim).  ``read_only=True`` produces the ``file:…?mode=ro`` URI that the
    read-only browse connections use.
    """
    text = str(Path(path))
    quoted = urllib.parse.quote(text.replace("\\", "/"), safe="/:")
    if read_only:
        return f"file:{quoted}?mode=ro"
    return f"sqlite+aiosqlite:///{text}"


def safe_filename(name: str, *, replacement: str = "_") -> str:
    """Make ``name`` usable as a file name on every supported OS.

    Windows rejects ``<>:"/\\|?*`` and trailing dots/spaces; macOS and Linux
    additionally treat ``:`` and ``/`` specially in Finder/URL contexts.  Perl
    avoids the question by never deriving names from arbitrary text
    (``Slim/Utils/OS/Win32.pm:247-289`` only *reads* names); the port writes
    artwork/playlist files, so it needs one guard.
    """
    text = str(name)
    for ch in '<>:"/\\|?*':
        text = text.replace(ch, replacement)
    for ch in "\r\n\t\0":
        text = text.replace(ch, replacement)
    text = text.rstrip(" .")
    return text or replacement
