"""Platform abstraction layer for Pyrion Music Server.

This package holds the code that makes the server behave like Perl's
``Slim::Utils::OS*`` hierarchy: one place that decides *where* the server's
data, prefs, cache and log directories live on each operating system, how a
filesystem path becomes a ``file://`` URL and back, and how to tell a missing
file apart from a missing mount.

Modules
-------
``paths``
    Directory resolution per OS (Linux/macOS/Windows, packaged vs. source),
    file-URL translation (including gvfs/SMB share paths), case-insensitivity
    helpers and the missing-path diagnostics used by the start-up warning.
"""

from __future__ import annotations

from lyrion.platform import paths

__all__ = ["paths"]
