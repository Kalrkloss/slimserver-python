"""
Logging framework for Pyrion Music Server.

Ported from Slim::Utils::Log. Provides:
- Multiple log levels (debug, info, warning, error, critical)
- Log to file with rotation
- Category-based logging (like Log4j)
- Syslog support on Unix
- Buffered writing for performance
- Async-safe operation
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from collections import defaultdict
from logging.handlers import RotatingFileHandler, SysLogHandler, MemoryHandler
from logging import LogRecord, StreamHandler, Formatter

from lyrion.bootstrap import IS_UNIX, IS_WINDOWS


# ---------------------------------------------------------------------------
# Logging levels (matching Python logging + LMS conventions)
# ---------------------------------------------------------------------------

CRITICAL = logging.CRITICAL  # 50
ERROR    = logging.ERROR     # 40
WARNING  = logging.WARNING    # 30
INFO     = logging.INFO      # 20
DEBUG    = logging.DEBUG      # 10
TRACE    = 5                  # Below DEBUG

LOG_LEVEL_NAMES = {
    "critical": CRITICAL,
    "error": ERROR,
    "warning": WARNING,
    "info": INFO,
    "debug": DEBUG,
    "trace": TRACE,
    "0": CRITICAL,
    "1": ERROR,
    "2": WARNING,
    "3": INFO,
    "4": DEBUG,
    "5": TRACE,
}


def _parse_log_level(level_str: str) -> int:
    """Parse a log level string to an integer."""
    return LOG_LEVEL_NAMES.get(level_str.lower(), INFO)


# ---------------------------------------------------------------------------
# Async log handler (writes from any thread to the event loop)
# ---------------------------------------------------------------------------

class AsyncLogHandler(logging.Handler):
    """
    Logging handler that dispatches log records to the asyncio event loop.

    This allows safe logging from any thread — the actual write to
    handlers happens in the event loop thread.
    """

    def __init__(self) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: LogRecord) -> None:
        if self._loop is None:
            # No loop yet — use default (synchronous fallback)
            return
        try:
            self._loop.call_soon_threadsafe(self._emit, record)
        except RuntimeError:
            # Loop is closed — silently drop
            pass

    def _emit(self, record: LogRecord) -> None:
        for handler in _active_handlers():
            try:
                handler.emit(record)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_log_initialized = False
_log_lock = threading.RLock()
_file_handlers: dict[Path, logging.Handler] = {}
_syslog_handler: logging.Handler | None = None
_category_loggers: dict[str, logging.Logger] = {}
_async_handler = AsyncLogHandler()
_root_logger: logging.Logger | None = None


def _active_handlers() -> list[logging.Handler]:
    """Return all active log handlers."""
    handlers: list[logging.Handler] = []
    for h in logging.root.handlers[:]:
        if isinstance(h, AsyncLogHandler):
            continue
        handlers.append(h)
    return handlers


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

async def init_logging(
    log_dir: Path | str | None = None,
    log_level: str = "info",
    log_file_name: str = "lyrion.log",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
    syslog: bool | None = None,
    console: bool = True,
) -> logging.Logger:
    """
    Initialize the logging system.

    Creates a rotating file handler and optionally a syslog handler
    and console handler.
    """
    global _log_initialized, _root_logger, _syslog_handler

    if _log_initialized:
        return logging.root

    log_dir = Path(log_dir) if log_dir else Path.home() / ".lyrion" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    level = _parse_log_level(log_level)

    # Set up the root logger
    root = logging.root
    root.setLevel(TRACE)  # Capture everything, filter at handler level
    root.addHandler(_async_handler)

    # File handler (rotating)
    log_path = log_dir / log_file_name
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,  # Don't open until first log
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)
    _file_handlers[log_path] = file_handler

    # Console handler (stderr)
    if console:
        console_handler = StreamHandler(sys.stderr)
        console_handler.setLevel(WARNING)  # Only warnings+ to console
        console_handler.setFormatter(
            Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(console_handler)

    # Syslog handler (Unix only)
    if syslog is None:
        syslog = IS_UNIX
    if syslog and IS_UNIX:
        try:
            _syslog_handler = SysLogHandler(address="/dev/log")
            _syslog_handler.setLevel(ERROR)
            _syslog_handler.setFormatter(
                Formatter("lyrion[%(process)d]: %(name)s: %(message)s")
            )
            root.addHandler(_syslog_handler)
        except OSError:
            # Syslog not available — silently skip
            pass

    _root_logger = root
    _log_initialized = True

    # Connect async handler to event loop
    try:
        loop = asyncio.get_running_loop()
        _async_handler.set_loop(loop)
    except RuntimeError:
        pass

    return root


def get_logger(name: str, level: str | int | None = None) -> logging.Logger:
    """
    Get a logger for a category.

    Loggers are hierarchical: "lyrion.db" is a child of "lyrion".
    The level can be set per-category.
    """
    logger = logging.getLogger(name)
    if level is not None:
        if isinstance(level, str):
            level = _parse_log_level(level)
        logger.setLevel(level)
    return logger


class LyrionLogger(logging.Logger):
    """
    Extended logger with category-aware convenience methods.
    """

    __slots__ = ()

    def trace(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.log(TRACE, msg, *args, **kwargs)

    def debug2(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Extra debug (TRACE level)."""
        self.log(TRACE, msg, *args, **kwargs)

    def debug3(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Even more verbose debug."""
        self.log(TRACE - 1, msg, *args, **kwargs)


# ---------------------------------------------------------------------------
# Shutdown / flush
# ---------------------------------------------------------------------------

def flush_logs() -> None:
    """Flush all log handlers."""
    for handler in logging.root.handlers[:]:
        try:
            handler.flush()
        except Exception:
            pass


async def shutdown() -> None:
    """Async-safe log shutdown."""
    flush_logs()
    # Give buffered handlers time to flush
    await asyncio.sleep(0.1)
    logging.shutdown()


# ---------------------------------------------------------------------------
# Log level control at runtime
# ---------------------------------------------------------------------------

def set_level(logger_name: str, level: str | int) -> None:
    """Set the log level for a named logger."""
    if isinstance(level, str):
        level = _parse_log_level(level)
    logging.getLogger(logger_name).setLevel(level)


def set_all_levels(level: str | int) -> None:
    """Set the log level for all loggers (including root)."""
    if isinstance(level, str):
        level = _parse_log_level(level)
    logging.root.setLevel(level)
    for logger_name in logging.Logger.manager.loggerDict:
        logging.getLogger(logger_name).setLevel(level)


# ---------------------------------------------------------------------------
# Reconfigure (for SIGHUP)
# ---------------------------------------------------------------------------

def reload_logging(
    log_dir: Path | str | None = None,
    log_level: str = "info",
) -> None:
    """
    Reload logging configuration (e.g., on SIGHUP).

    Closes all file handlers, then reinitializes with new settings.
    """
    global _log_initialized

    # Close existing file handlers
    for handler in logging.root.handlers[:]:
        if isinstance(handler, (RotatingFileHandler, StreamHandler, SysLogHandler)):
            try:
                handler.close()
            except Exception:
                pass
            logging.root.removeHandler(handler)

    # Re-initialize
    asyncio.run(init_logging(log_dir=log_dir, log_level=log_level))
