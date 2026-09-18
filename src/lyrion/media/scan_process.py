"""Launch the library scan in its own process — Perl's ``scanner.pl``.

Perl never scans the library inside the server process when the scan is not a
quick single-album refresh: ``Slim::Music::Import->launchScan`` starts the
external helper program and returns immediately
(``Slim/Music/Import.pm:106-240``), with the scanner's path coming from
``Slim::Utils/OS.pm:233-235`` ``scanner()`` = ``"$Bin/scanner.pl"``:

* ``Slim/Music/Import.pm:216-230`` — ``Proc::Background->new($procArgs,
  $command, @scanArgs)``; ``:203-212`` redirects the scanner's STDERR into
  ``scanner.log`` on non-Windows.
* ``Slim/Music/Import.pm:139-155`` — the scan process gets
  ``priority=$scannerPriority``; that pref defaults to 0
  (``Slim/Utils/Prefs.pm:220``).
* ``scanner.pl:213-218`` — ``Slim::Utils::Misc::setPriority($priority)`` →
  ``Slim/Utils/OS.pm:409-421`` ``setpriority(0, 0, $priority)`` (POSIX);
  on Windows it is a no-op (``Slim/Utils/OS/Win32.pm:617`` ``sub setPriority {}``).
* ``scanner.pl:228-231`` — the scanner opens its **own** database handle
  (``$sqlHelperClass->init()``) instead of sharing the server's.
* ``scanner.pl:150``/``:481-492`` — a pid file marks the running scan; the
  server's ``stillScanning`` uses ``scanningProcess->alive`` to detect a
  scanner that died without finishing (``Slim/Music/Import.pm:760-791``).

This module is the launching side of that: argv, environment, priority and
the stdin/stdout wiring.  The worker itself is
:mod:`lyrion.media.scan_worker`.

Deviation, named and deliberate
-------------------------------
Perl has no ``ionice``; its scan I/O priority follows ``nice``.  The port adds
an *idle* I/O class for the scan process on Linux (``ionice -c3``, best effort,
silently skipped when the tool is missing or the platform is not Linux): the
latency the user sees during a scan comes from DB/FS I/O (WAL checkpoints,
page-cache pressure), which ``nice`` alone does not lower.  macOS/Windows are
untouched — the same no-op behaviour Perl has on Windows.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: Perl's ``scannerPriority`` (``Slim/Utils/Prefs.pm:220``: default ``0``).
SCANNER_PRIORITY_PREF = "scannerPriority"

#: Windows has no ``nice`` and Perl's Win32 helper is a documented no-op
#: (``Slim/Utils/OS/Win32.pm:617``).
_PRIORITY_SUPPORTED = hasattr(os, "setpriority") and os.name == "posix"

#: Perl's ``priority`` range (``scanner.pl:418``: "-20 (high) to 20 (low)").
MIN_PRIORITY, MAX_PRIORITY = -20, 20


def scanner_priority() -> int:
    """The configured scanner priority (Perl ``scannerPriority``, clamped)."""
    from lyrion.config import get_config

    try:
        raw = get_config().get(SCANNER_PRIORITY_PREF, 0)
    except Exception:  # noqa: BLE001 - a config hiccup must not stop a scan
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(MIN_PRIORITY, min(MAX_PRIORITY, value))


def worker_argv(
    source: Path,
    mode: str,
    *,
    db_path: Path | str,
    serverdata: Path | str | None = None,
    priority: int = 0,
    parent_pid: int | None = None,
) -> list[str]:
    """The worker command line (Perl's ``@scanArgs``, Import.pm:157-194)."""
    argv = [
        sys.executable, "-m", "lyrion.media.scan_worker",
        "--source", str(source),
        "--mode", str(mode or "full"),
        "--db", str(db_path),
        "--priority", str(int(priority)),
    ]
    if serverdata:
        argv += ["--serverdata", str(serverdata)]
    if parent_pid:
        argv += ["--parent-pid", str(int(parent_pid))]
    return argv


def _scanner_log_file(serverdata: Path | str | None = None) -> Path | None:
    """Perl's scanner log (``Slim/Utils/Log->scannerLogFile``, Import.pm:203-212).

    Perl hands the scanner its own ``logdir`` (Import.pm:135-137); the port
    puts the file under the same serverdata root the child is given, so the
    child's output never lands anywhere the caller did not ask for.
    """
    try:
        from lyrion.platform import paths as platform_paths

        if serverdata:
            cache = platform_paths.port_dir("cache", serverdata=serverdata)
        else:
            from lyrion.config import get_config

            cache = Path(get_config().cache_dir)
        return Path(cache) / "scanner.log"
    except Exception:  # noqa: BLE001
        return None


def spawn_scan_worker(
    source: Path,
    mode: str,
    *,
    db_path: Path | str,
    serverdata: Path | str | None = None,
    priority: int | None = None,
    parent_pid: int | None = None,
) -> subprocess.Popen:
    """Start the scan worker; return the ``Popen`` handle (never blocks).

    The worker gets its own database handle, its own process priority and a
    copy of the environment (with ``LYRION_SERVERDATA`` pinned to the server's
    data root so the child resolves the same prefs/cache/library paths —
    Perl passes ``prefsdir=`` explicitly, ``Slim/Music/Import.pm:124-126``).
    """
    prio = scanner_priority() if priority is None else int(priority)
    env = dict(os.environ)
    if serverdata:
        env["LYRION_SERVERDATA"] = str(serverdata)

    argv = worker_argv(source, mode, db_path=db_path, serverdata=serverdata,
                       priority=prio, parent_pid=parent_pid)

    stdout = stderr = None
    log_handle = None
    log_path = _scanner_log_file(serverdata)
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "ab", buffering=0)  # noqa: SIM115
            stdout = stderr = log_handle
        except OSError as exc:  # noqa: BLE001 - keep the child's log optional
            logger.warning("scanner.log nicht schreibbar (%s): %s", log_path, exc)

    # ``close_fds`` keeps the server's sockets out of the child; POSIX only.
    kwargs: dict = {"close_fds": True}
    if log_handle is not None:
        kwargs.update(stdout=stdout, stderr=stderr)
    if os.name == "nt":  # pragma: no cover - Windows only
        kwargs["close_fds"] = False

    try:
        proc = subprocess.Popen(argv, env=env, **kwargs)
    finally:
        if log_handle is not None:
            log_handle.close()  # the child holds its own copy of the fd

    logger.info(
        "Started scan process pid=%d (mode=%s, priority=%d, source=%s) — "
        "Perl: launchScan → scanner.pl (Slim/Music/Import.pm:106-240)",
        proc.pid, mode, prio, source)
    if _PRIORITY_SUPPORTED:
        _lower_io_priority(proc.pid)
    return proc


# ---------------------------------------------------------------------------
# priority (child process side + I/O class)
# ---------------------------------------------------------------------------

def apply_process_priority(priority: int) -> int | None:
    """Set this process's nice value — Perl ``setpriority(0, 0, $p)``.

    Returns the effective nice value, or ``None`` where the platform has no
    ``setpriority`` (Windows: Perl's Win32 ``setPriority`` is a no-op,
    ``Slim/Utils/OS/Win32.pm:617``).
    """
    if not _PRIORITY_SUPPORTED:  # pragma: no cover - non-POSIX
        logger.debug("setpriority auf %s nicht verfügbar (Perl: Win32 no-op)",
                     os.name)
        return None
    value = max(MIN_PRIORITY, min(MAX_PRIORITY, int(priority)))
    try:
        os.setpriority(os.PRIO_PROCESS, 0, value)
    except (OSError, AttributeError) as exc:  # pragma: no cover - rights/plat
        logger.warning("Prozesspriorität %d nicht setzbar: %s", value, exc)
    try:
        return os.getpriority(os.PRIO_PROCESS, 0)
    except OSError:  # pragma: no cover
        return None


def _lower_io_priority(pid: int) -> bool:
    """Best-effort ``ionice -c3`` (idle class) for the scan process.

    Linux only; a missing ``ionice`` is not an error (documented deviation
    from Perl, see the module docstring).
    """
    if not sys.platform.startswith("linux"):  # pragma: no cover - other OS
        return False
    ionice = shutil.which("ionice")
    if not ionice:
        logger.debug("ionice nicht installiert — I/O-Priorität bleibt unverändert")
        return False
    try:
        result = subprocess.run(
            [ionice, "-c3", "-p", str(int(pid))],
            capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        logger.debug("ionice -c3 -p %d fehlgeschlagen: %s", pid, exc)
        return False
    if result.returncode != 0:
        logger.debug("ionice -c3 -p %d: %s", pid,
                     result.stderr.decode("utf-8", "replace").strip())
        return False
    return True
