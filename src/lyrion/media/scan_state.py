"""Shared library-scan progress state (rescan / rescanprogress).

A tiny module-level singleton both the CLI commands and the JSON-RPC
layer read. The importer updates it during a scan so controllers can
poll 'rescanprogress' like they do against the real LMS.

Because the scan runs in its own process (Perl: ``scanner.pl``,
``Slim/Music/Import.pm:106-240`` ``launchScan``), this singleton also *publishes*
its state to a file so the server process can read the progress of a scan it did
not start.  Perl does exactly that: the scanner writes its Progress rows to the
DB and mirrors them into a progress JSON file
(``Slim/Utils/Progress.pm:298-341`` ``_update_db``/``_write_json``, consumed via
``Slim/Utils/OS.pm:508`` ``progressJSON``), and the server reads
``metainformation.isScanning`` (``Slim/Music/Import.pm:362-377``
``setIsScanning`` / ``:760-791`` ``stillScanning``).  The abort flag travels the
other way (server → scanner): Perl answers the scanner's progress notify with
``abort`` (``Slim/Utils/SQLiteHelper.pm:404-460``) after ``abortScan`` set
``$ABORT`` (``Slim/Music/Import.pm:257-270``); here the server drops a marker
file the scanner polls.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-process channels
# ---------------------------------------------------------------------------

#: Progress file (written by whichever process runs the scan) and abort
#: marker (written by the server process).
_PROGRESS_FILE: Path | None = None
_ABORT_FILE: Path | None = None

#: Set once when a stale publisher was reaped, so the log stays readable.
_reaped_stale = False

#: How long a published state stays fresh.  Perl writes progress rows at most
#: every ``UPDATE_DB_INTERVAL = 5`` seconds (``Slim/Utils/Progress.pm:17``,
#: ``:221``) and the server polls its scanner every ``POLL_INTERVAL = 5``
#: seconds (``Slim/Music/Import.pm:60``, ``:242``), so a fraction of a second
#: is *fresher* than Perl — and it keeps the every-request read off the disk
#: (a ``rescan ?`` that reads a file on each query stalls when the disk is
#: busy with the scan's own writes).
PUBLISHED_TTL = 0.2

#: ``(monotonic timestamp, path, payload)`` of the last published-state read.
_published_cache: tuple[float, Path | None, dict[str, Any] | None] | None = None


def scan_channel_paths() -> tuple[Path, Path]:
    """``(progress file, abort marker)`` inside the server's cache dir.

    The cache dir is the port's ``dirsFor('cache')``
    (``Slim/Utils/OS/Unix.pm:50-113``; ``config.LyrionConfig.cache_dir``), so a
    child process started with the server's serverdata finds the same files.
    """
    from lyrion.config import get_config

    cache = Path(get_config().cache_dir)
    return cache / "scan-progress.json", cache / "scan-abort"


def set_channels(progress: Path | None, abort: Path | None) -> None:
    """Point this process's scan state at the shared progress/abort files."""
    global _PROGRESS_FILE, _ABORT_FILE, _published_cache
    _PROGRESS_FILE = Path(progress) if progress else None
    _ABORT_FILE = Path(abort) if abort else None
    # Nothing cached for the new channel (a test or a fresh scan may write the
    # file again immediately).
    _published_cache = None


def clear_channels() -> None:
    """Detach from the shared files (used when a scan ends)."""
    set_channels(None, None)


def prepare_channels() -> tuple[Path, Path]:
    """Arm the channels for a scan this process is about to start.

    Leftovers of a finished scan are dropped first — Perl clears the previous
    progress before a scan starts (``Slim/Music/Import.pm:385-412``
    ``Progress->clear``, called from ``runScan``/``launchScan``).  A stale file
    must never make a *new* scan abort immediately.
    """
    progress, abort = scan_channel_paths()
    for path in (progress, abort):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # noqa: BLE001 - a missing file must not stop a scan
            logger.warning("Scan-Kanal %s nicht entfernbar: %s", path, exc)
    set_channels(progress, abort)
    return progress, abort


def read_published(path: Path | None = None,
                   max_age: float | None = None) -> dict[str, Any] | None:
    """Read the published scan state of another process (or ``None``).

    The result is cached for :data:`PUBLISHED_TTL` seconds: controllers poll
    ``rescan ?``/``rescanprogress`` in a loop, and a filesystem read per query
    is both unnecessary (Perl's own progress is seconds behind) and a way to
    inherit the disk latency of the running scan.  ``max_age=0`` forces a fresh
    read (used when the caller must know whether a scan really ended).
    """
    global _published_cache
    target = Path(path) if path else _PROGRESS_FILE
    if target is None:
        return None
    ttl = PUBLISHED_TTL if max_age is None else max_age
    now = time.monotonic()
    if (_published_cache is not None and _published_cache[1] == target
            and ttl > 0 and now - _published_cache[0] < ttl):
        return _published_cache[2]
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        data = None
    else:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        data = parsed if isinstance(parsed, dict) else None
    _published_cache = (now, target, data)
    return data


def _pid_alive(pid: Any) -> bool:
    """Perl's ``$class->scanningProcess->alive`` (Import.pm:772-786)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except OSError:
        return False
    return True


def live_published_state(max_age: float | None = None) -> dict[str, Any] | None:
    """The published state of a *live* scan process, else ``None``.

    A file whose publisher is gone is a crash leftover: Perl detects it in
    ``stillScanning``, logs ``"External scanner exited without completing the
    scan."``, clears the flag and notifies ``rescan done``
    (``Slim/Music/Import.pm:772-786``).  We do the same — a stale file must
    never answer ``rescan ?`` with ``1``.
    """
    global _reaped_stale
    published = read_published(max_age=max_age)

    if not published or not published.get("scanning"):
        return None
    pid = published.get("pid")
    if int(pid or 0) == os.getpid():
        # Our own publication: the in-process state is the truth.
        return None
    if _pid_alive(pid):
        return published
    if not _reaped_stale:
        _reaped_stale = True
        logger.error(
            "External scanner exited without completing the scan "
            "(stale progress file %s, pid %s) — rescan flag dropped "
            "(Perl: Slim/Music/Import.pm:772-786)",
            _PROGRESS_FILE, published.get("pid"))
    return None


def reset_published_reap_flag() -> None:
    """Allow the stale-publisher log line again (tests)."""
    global _reaped_stale
    _reaped_stale = False


class ScanState:
    """Thread-safe scan progress holder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    # ---- cross-process publication ---------------------------------------

    def _publish_locked(self) -> None:
        """Write the current state to the progress file (atomic replace)."""
        if _PROGRESS_FILE is None:
            return
        payload = {
            "scanning": self.scanning,
            "progress": self.progress,
            "total_files": self.total_files,
            "done_files": self.done_files,
            "pid": os.getpid(),
            "updated": time.time(),
        }
        tmp = _PROGRESS_FILE.with_name(_PROGRESS_FILE.name + f".{os.getpid()}.tmp")
        try:
            _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, _PROGRESS_FILE)
        except OSError as exc:  # noqa: BLE001 - progress is best effort
            logger.debug("Scan-Fortschritt nicht schreibbar (%s): %s",
                         _PROGRESS_FILE, exc)

    def _publish(self) -> None:
        with self._lock:
            self._publish_locked()

    # ---- state -----------------------------------------------------------

    def reset(self) -> None:
        global _reaped_stale
        with self._lock:
            self.scanning: bool = False
            self.progress: int = 0  # 0..100
            self.total_files: int = 0
            self.done_files: int = 0
            self._abort = False
        self._clear_abort_marker()
        self._publish()
        _reaped_stale = False

    def start(self, total: int = 0) -> None:
        with self._lock:
            self.scanning = True
            self.progress = 0
            self.total_files = max(0, int(total))
            self.done_files = 0
            # Abort is per-scan: a fresh scan starts with a clean flag
            # (Perl Slim::Music::Import->abortScan semantics).
            self._abort = False
        self._publish()

    def request_abort(self) -> None:
        """Ask the running scan to stop (abortscan).

        The flag is published for a scan that lives in another process: the
        server drops the marker file, the scanner polls it (Perl: ``$ABORT``
        plus the ``abort`` answer to the scanner's progress notify,
        ``Slim/Music/Import.pm:257-270`` / ``Slim/Utils/SQLiteHelper.pm:429-444``).
        """
        with self._lock:
            self._abort = True
        if _ABORT_FILE is not None:
            try:
                _ABORT_FILE.parent.mkdir(parents=True, exist_ok=True)
                _ABORT_FILE.write_text("abort\n", encoding="utf-8")
            except OSError as exc:  # noqa: BLE001
                logger.warning("Abort-Marker nicht schreibbar (%s): %s",
                               _ABORT_FILE, exc)

    def _clear_abort_marker(self) -> None:
        if _ABORT_FILE is not None:
            try:
                _ABORT_FILE.unlink()
            except FileNotFoundError:
                pass
            except OSError:  # noqa: BLE001
                pass

    @property
    def abort_requested(self) -> bool:
        """True when an abort was asked for — locally or via the marker file."""
        with self._lock:
            if self._abort:
                return True
        if _ABORT_FILE is not None:
            try:
                return _ABORT_FILE.exists()
            except OSError:  # noqa: BLE001
                return False
        return False

    def update(self, done: int, total: int | None = None) -> None:
        with self._lock:
            if total is not None:
                self.total_files = max(0, int(total))
            self.done_files = max(0, int(done))
            if self.total_files > 0:
                self.progress = min(100, int(self.done_files * 100 / self.total_files))
            else:
                self.progress = 0
        self._publish()

    def finish(self) -> None:
        with self._lock:
            self.scanning = False
            self.progress = 100 if self.done_files else 0
        self._clear_abort_marker()
        self._publish()

    def snapshot(self) -> dict[str, Any]:
        # A scan running in another process wins: its published state is the
        # live one (Perl: the server reads the scanner's Progress rows).
        published = live_published_state()
        if published is not None:
            return {
                "scanning": True,
                "progress": int(published.get("progress") or 0),
                "total_files": int(published.get("total_files") or 0),
                "done_files": int(published.get("done_files") or 0),
            }
        with self._lock:
            return {
                "scanning": self.scanning,
                "progress": self.progress,
                "total_files": self.total_files,
                "done_files": self.done_files,
            }


#: Module-level singleton shared by all callers.
SCAN_STATE = ScanState()
