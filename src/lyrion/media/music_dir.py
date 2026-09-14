"""Shared helpers for resolving the music library directory.

Used by the CLI rescan command and the JSON-RPC rescan/serverpref
handlers so every path applies the same resolution chain:
  1. 'musicdir' / 'audiodir' preference (when set)
  2. the OS music folder — Perl ``Slim::Utils::OSDetect::dirsFor('music')``
     (``Slim/Utils/OS/OSX.pm:183-199`` → ``~/Music``,
     ``Slim/Utils/OS/Win32.pm:189-197`` → ``CSIDL_MYMUSIC``), ``~/Music`` on
     Unix where Perl deliberately has *no* default
     (``Slim/Utils/OS/Unix.pm:68-71``).
The original LMS refuses to scan without a media dir; we log an error and
report it to the caller instead of silently using a hardcoded path.  Since the
port must distinguish "the folder is gone" from "the share is not mounted" the
diagnostics come from :mod:`lyrion.platform.paths`.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _pref_value(name: str) -> str:
    from lyrion.config import get_config

    try:
        value = get_config().get(name, "") or ""
    except Exception:  # pragma: no cover - defensive, config may be absent
        return ""
    if isinstance(value, (list, tuple)):
        value = next((v for v in value if str(v).strip()), "")
    return str(value).strip()


def resolve_music_dir() -> Path | None:
    """Resolve the configured music directory, or None when unset/unusable.

    On a failure the reason is logged with the platform-specific diagnosis
    (:func:`lyrion.platform.paths.explain_missing_path`): a plain missing
    folder reads differently from a gvfs/SMB mount that is not attached, which
    is the case that used to look like "metadata but no sound".
    """
    from lyrion.platform import paths as platform_paths

    musicdir = _pref_value("musicdir") or _pref_value("audiodir")
    if not musicdir:
        fallback = platform_paths.default_music_dir() or (Path.home() / "Music")
        logger.warning(
            "Preference 'musicdir' is empty — falling back to %s "
            "(set it via serverpref)", fallback,
        )
        musicdir = str(fallback)

    p = Path(musicdir)
    if not p.is_dir():
        code, message = platform_paths.explain_missing_path(p)
        logger.error("Music directory unusable (%s): %s", code, message)
        return None
    return p
