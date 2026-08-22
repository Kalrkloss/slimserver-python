"""Shared helpers for resolving the music library directory.

Used by the CLI rescan command and the JSON-RPC rescan/serverpref
handlers so every path applies the same resolution chain:
  1. 'musicdir' preference (when set)
  2. ~/Music fallback
The original LMS refuses to scan without a media dir; we log an error
and report it to the caller instead of silently using a hardcoded path.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_music_dir() -> Path | None:
    """Resolve the configured music directory, or None when unset/invalid."""
    from lyrion.config import get_config

    musicdir = str(get_config().get("musicdir", "") or "").strip()
    if not musicdir:
        fallback = Path.home() / "Music"
        logger.warning(
            "Preference 'musicdir' is empty — falling back to %s "
            "(set it via serverpref)", fallback,
        )
        musicdir = str(fallback)

    p = Path(musicdir)
    if not p.is_dir():
        logger.error("Music directory does not exist: %s", p)
        return None
    return p
