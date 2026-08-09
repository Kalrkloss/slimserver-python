"""
Lyrion Music Server bootstrap and module initialization.

This module handles:
- sys.path setup (including vendor lib/ directories)
- OS detection
- asyncio event loop initialization with uvloop
- Signal handler setup
- Module loading order
- Environment variable initialization
"""

from __future__ import annotations

import os
import sys
import signal
import platform
import logging
from pathlib import Path
from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from lyrion.utils.log import LyrionLogger


#: OS family constants (mirroring Slim::bootstrap)
OS_WINDOWS = "MSWin32"
OS_MAC = "darwin"
OS_LINUX = "linux"
OS_FREEBSD = "freebsd"
OS_NETBSD = "netbsd"
OS_OPENBSD = "openbsd"
OS_SOLARIS = "sunos"
OS_BSD = frozenset({OS_FREEBSD, OS_NETBSD, OS_OPENBSD, "bsd"})


def get_os() -> str:
    """Return the detected OS family."""
    system = platform.system().lower()
    if system == "windows":
        return OS_WINDOWS
    elif system == "darwin":
        return OS_MAC
    elif system == "linux":
        return OS_LINUX
    elif system == "freebsd":
        return OS_FREEBSD
    elif system == "netbsd":
        return OS_NETBSD
    elif system == "openbsd":
        return OS_OPENBSD
    elif system == "sunos":
        return OS_SOLARIS
    return system


#: Current OS family
CURRENT_OS = get_os()

#: True if running on Unix-like OS
IS_UNIX = CURRENT_OS not in {OS_WINDOWS}

#: True if running on Windows
IS_WINDOWS = CURRENT_OS == OS_WINDOWS

#: True if running on macOS
IS_MAC = CURRENT_OS == OS_MAC


# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

def _setup_paths() -> None:
    """
    Add vendor lib/ directories to sys.path.

    In a typical LMS installation, plugin .pm files live under
    CPAN/lib/ and Bin/ directories alongside the main script.
    For the Python port, we use a src/lyrion structure with a
    vendor lib/ directory at the repository root.
    """
    # Determine the installation prefix
    if getattr(sys, "frozen", False):
        # PyInstaller / frozen bundle
        base_dir = Path(sys.executable).parent
    else:
        # Source checkout
        base_dir = Path(__file__).parent.parent.parent  # src/lyrion -> src -> root

    # Standard source layout
    src_dir = base_dir / "src"

    # Vendor library directories (where third-party code lives)
    vendor_dirs = [
        base_dir / "lib",
        base_dir / "vendor" / "lib",
        base_dir / "CPAN" / "lib",
    ]

    # Plugins directory
    plugin_dir = base_dir / "Plugins"

    added_paths: list[str] = []
    for d in vendor_dirs:
        if d.exists() and str(d) not in sys.path:
            sys.path.insert(0, str(d))
            added_paths.append(str(d))

    # Store plugin dir for later use
    if plugin_dir.exists():
        sys.modules.setdefault("_lyrion_plugin_dir", str(plugin_dir))

    # Store source dir
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    return None


# ---------------------------------------------------------------------------
# Environment initialization
# ---------------------------------------------------------------------------

def _setup_environment() -> None:
    """
    Set environment variables needed throughout the server lifecycle.
    Mirrors environment setup from Slim/bootstrap.pm.
    """
    # Disable artwork scanning (can be overridden per-scan)
    os.environ.setdefault("AUDIO_SCAN_NO_ARTWORK", "0")

    # SQLite synchronous mode - NORMAL is a good balance of safety/speed
    os.environ.setdefault("SQLITE_SYNCHRONOUS", "NORMAL")

    # Python UTF-8 mode
    os.environ.setdefault("PYTHONUTF8", "1")

    # Ensure HOME is set (needed for SQLite and other libs)
    os.environ.setdefault("HOME", str(Path.home()))

    # LC_ALL for consistent string sorting
    if not os.environ.get("LC_ALL"):
        os.environ["LC_ALL"] = "C.UTF-8"


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

_shutdown_requested = False
_main_loop: asyncio.AbstractEventLoop | None = None


def request_shutdown(signum: int | None = None, frame: Any = None) -> None:
    """Signal handler that requests server shutdown.

    Only sets the shutdown flag — the server coroutine observes it and
    shuts down gracefully. Do NOT stop the event loop here: stopping the
    loop while `run_until_complete` is executing raises
    "Event loop stopped before Future completed" and can leave the
    process hanging on shutdown (systemd restart stuck in "deactivating").
    """
    global _shutdown_requested
    _shutdown_requested = True


def _stop_loop() -> None:
    """Stop the asyncio event loop."""
    global _main_loop
    if _main_loop is not None:
        _main_loop.stop()


def _setup_signals() -> None:
    """Install signal handlers for graceful shutdown."""
    if IS_WINDOWS:
        # Windows: only handle Ctrl+C
        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGBREAK, request_shutdown)
    else:
        # Unix: handle SIGTERM, SIGINT, SIGHUP
        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGHUP, request_shutdown)
        # Ignore SIGPIPE (raised when writing to closed socket)
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        except (OSError, ValueError):
            pass  # Some platforms don't have SIGPIPE


# ---------------------------------------------------------------------------
# Event loop setup
# ---------------------------------------------------------------------------

def _init_event_loop() -> asyncio.AbstractEventLoop:
    """
    Initialize the asyncio event loop with uvloop if available.

    uvloop provides significant performance improvements over the default
    asyncio loop, especially for I/O-bound operations like network servers.
    """
    global _main_loop

    try:
        import uvloop
        uvloop.install()
        loop = uvloop.new_event_loop()
        asyncio.set_event_loop(loop)
        _main_loop = loop
        return loop
    except ImportError:
        # Fall back to stdlib asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _main_loop = loop
        return loop


# ---------------------------------------------------------------------------
# Module import orchestration
# ---------------------------------------------------------------------------

def _import_utils() -> None:
    """Import all utility modules in dependency order."""
    # Core utilities first (no dependencies on other lyrion modules)
    from lyrion.utils import log as _log_mod  # noqa: F401
    from lyrion.utils import osdetect as _os_mod  # noqa: F401
    from lyrion.utils import strings as _str_mod  # noqa: F401
    from lyrion.utils import datetime as _dt_mod  # noqa: F401
    from lyrion.utils import validators as _val_mod  # noqa: F401
    from lyrion.utils import network as _net_mod  # noqa: F401
    from lyrion.utils import cache as _cache_mod  # noqa: F401
    from lyrion.utils import timers as _timer_mod  # noqa: F401
    from lyrion.utils import firmware as _fw_mod  # noqa: F401
    # prefs depends on cache and log
    from lyrion.utils import prefs as _prefs_mod  # noqa: F401


def _import_database() -> None:
    """Import database modules."""
    from lyrion.database import schema as _schema_mod  # noqa: F401
    from lyrion.database import sqlite_helper as _sqlite_mod  # noqa: F401
    from lyrion.database import dbcache as _dbcache_mod  # noqa: F401


def _import_control() -> None:
    """Import control/protocol modules (CLI, JSON, etc.)."""
    # These will be implemented in future phases
    # from lyrion.control import cli  # noqa: F401
    pass


def _import_media() -> None:
    """Import media processing modules."""
    # from lyrion.media import scanner  # noqa: F401
    # from lyrion.media import artwork  # noqa: F401
    pass


def _import_music() -> None:
    """Import music library modules."""
    # from lyrion.music import library  # noqa: F401
    # from lyrion.music import artwork  # noqa: F401
    pass


# ---------------------------------------------------------------------------
# Bootstrap orchestration
# ---------------------------------------------------------------------------

_imported = False


def bootstrap() -> asyncio.AbstractEventLoop:
    """
    Run full server bootstrap and return the asyncio event loop.

    This is the single entry point for all initialization. It runs exactly
    once, sets up paths, environment, signals, and the event loop, then
    returns so the caller can start the server.
    """
    global _imported

    # Path setup (idempotent)
    _setup_paths()

    # Environment
    _setup_environment()

    # Signals
    _setup_signals()

    # Event loop (idempotent)
    loop = _init_event_loop()

    # Lazy import on first run
    if not _imported:
        _import_utils()
        _import_database()
        _import_control()
        _import_media()
        _import_music()
        _imported = True

    return loop


def is_shutdown_requested() -> bool:
    """Return True if a shutdown signal has been received."""
    return _shutdown_requested


def run_until_shutdown(coro_factory: type[asyncio.Task]) -> None:
    """
    Run an asyncio coroutine factory until shutdown is requested.

    This is a convenience wrapper around asyncio.run() that respects
    signal-based shutdown.
    """
    loop = bootstrap()
    try:
        asyncio.run_sync = loop.run_until_complete  # type: ignore[method-assign]
        task = coro_factory()
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Startup phases (for debugging / testing)
# ---------------------------------------------------------------------------

PHASES = [
    "paths",
    "environment",
    "signals",
    "event_loop",
    "utils",
    "database",
    "control",
    "media",
    "music",
]


async def async_init() -> None:
    """Async initialization after bootstrap (call from event loop)."""
    # Initialize configuration
    from lyrion.config import get_config
    cfg = get_config()
    await cfg.init()

    # Initialize logging
    from lyrion.utils.log import init_logging
    log_dir = cfg.log_dir
    loglevel = getattr(cfg.cli_args, "loglevel", "info") if cfg.cli_args else "info"
    await init_logging(log_dir, loglevel)

    # Initialize database
    from lyrion.database.sqlite_helper import init_db
    await init_db(cfg.db_path)


import asyncio
import contextlib
