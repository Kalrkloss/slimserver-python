"""The library scan behind the ``rescan`` command (Perl's rescanCommand).

One place both the CLI (``lyrion.control.cli_commands``) and the JSON-RPC
layer (``lyrion.web.api``) call, so ``rescan`` behaves identically on both
ports — the same way Perl has ONE ``Slim::Control::Commands::rescanCommand``
behind CLI *and* JSON-RPC (``Slim/Web/JSONRPC.pm:400-520`` executes the same
``Slim::Control::Request`` the CLI does).

Perl's behaviour, as cited below:

* ``Slim/Control/Request.pm:606`` registers ``['rescan', '?']`` on
  ``rescanQuery``; ``:607`` registers ``['rescan','_mode','_target']`` on
  ``rescanCommand`` (and ``:651`` ``['rescan','done']`` as a notification).
* ``Slim/Control/Commands.pm:2679-2830`` ``rescanCommand``: the mode defaults
  to ``full`` (:2689), ``album``/``track`` without an id is an error +
  bad dispatch (:2693-2699), a scan that is already running (or queued) makes
  the request a *queued* scan task instead of a second concurrent scan
  (:2712-2720 ``queueScanTask``/``nextScanTask``), otherwise the in-process
  scan of the media dirs starts (:2786-2830).
* ``Slim/Control/Queries.pm:3214-3229`` ``rescanQuery`` answers the bare
  result ``_rescan`` = ``Slim::Music::Import->stillScanning() ? 1 : 0``.
* ``Slim/Music/Import.pm:760-791`` ``stillScanning`` reads the
  ``metainformation`` row ``isScanning`` and cleans up progress + notifies
  ``rescan done`` when a crashed scanner left the flag set.
* ``Slim/Music/Import.pm:106-240`` ``launchScan`` is the *normal* full-scan
  path: the scan runs in its own process (``scanner.pl``) with its own DB
  handle and its own priority, and the server keeps serving requests.  This
  port now does the same — :mod:`lyrion.media.scan_worker` +
  :mod:`lyrion.media.scan_process`, progress published via
  :mod:`lyrion.media.scan_state` exactly as Perl publishes it through the
  ``progress`` table and the progress JSON (``Slim/Utils/Progress.pm:298-341``).
* ``Slim/Media/MediaFolderScan.pm:43-79`` ``startScan`` is the in-process
  directory scan (``scanName => 'directory'``, ``progress => 1``) that Perl
  only uses for the quick single-object (``album``/``track``) modes
  (``Slim/Control/Commands.pm:2749-2786``).
  LMS 9.1.1 has **no** ``Slim/Plugin/Scanner/Plugin.pm``; the plugin named
  after the scan is ``Slim/Plugin/Rescan/Plugin.pm``, a *scheduler* that
  simply executes the plain ``rescan``/``wipecache``/``rescan playlists``
  request (:600-616).
* ``Slim/Music/Import.pm:385-412`` clears the importer progress (and with it
  the ``failure`` row) before a scan starts; ``Slim/Control/Queries.pm:
  3291-3296`` reports a leftover ``failure`` row through ``_scanFailed``
  (:6247-6258) as ``lastscanfailed`` once the scan is idle again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Perl's ``my $mode = $originalMode = $request->getParam('_mode') || 'full'``
#: (Commands.pm:2689).
DEFAULT_MODE = "full"

#: Perl's quick single-object scan modes: they need an album/track id
#: (Commands.pm:2693-2699).
OBJECT_MODES = ("album", "track")

#: Modes the web UI's rescan type names (``Slim/Plugin/Rescan/Plugin.pm:
#: 600-616``: ``1rescan`` → ``rescan``, ``2wipedb`` → ``wipecache``,
#: ``3playlist`` → ``rescan playlists``).
FULL_ALIASES = ("", "1", "once", "full", "normal")

#: Queue of scans asked for while another scan runs — Perl's scan task queue
#: (``Slim/Music/Import.pm:829-860`` ``queueScanTask``/``nextScanTask``).
_QUEUE: deque[tuple[str, Optional[str]]] = deque()

#: The mode the running scan was started with (Perl's ``metainformation``
#: row ``isScanning`` holds the scan *type* string, Import.pm:760-791).
_RUNNING: Optional[str] = None

#: ``Slim/Utils/Progress``-equivalent: the last failed scan's message, which
#: ``rescanprogressQuery`` reports as ``lastscanfailed`` while idle
#: (Queries.pm:3291-3296 + :6247-6258). Cleared when a scan starts
#: (Import.pm:385-412 ``Progress->clear``).
_LAST_FAILURE: Optional[str] = None

#: Wall-clock start of the current scan, for ``totaltime``
#: (Queries.pm:3282-3285).
_STARTED_AT: Optional[float] = None

#: Watchdog task for a queue that has to wait for a scan this module did not
#: start (Perl's ``nextScanTask`` runs from the scanner's own end handler).
_WATCHER: Optional[asyncio.Task] = None

#: Handle of the running scan process (Perl's ``scanningProcess``,
#: ``Slim/Music/Import.pm:228-231`` + ``:772-786``).
_PROC: Optional[Any] = None

#: How often the scan process is polled for its exit (Perl polls the scanner
#: with a timer, ``Slim/Music/Import.pm:242-258`` ``_watchScanner``).
_POLL_INTERVAL = 0.05


# ---------------------------------------------------------------------------
# Scan state (Perl: metainformation isScanning / Import->stillScanning)
# ---------------------------------------------------------------------------


def _snapshot() -> dict[str, Any]:
    """Read the shared scan progress singleton, tolerating a stub in tests."""
    try:
        from lyrion.media.scan_state import SCAN_STATE

        snap = SCAN_STATE.snapshot()
        return dict(snap) if isinstance(snap, dict) else {}
    except Exception:  # noqa: BLE001 - a broken scan state must not raise here
        return {}


def is_scanning() -> bool:
    """Perl ``Slim::Music::Import->stillScanning()`` (Import.pm:760-791).

    A queued scan counts as scanning too: Perl's ``queueScanTask`` keeps the
    scan going without ever leaving ``isScanning`` unset (the next task starts
    the moment the current one ends, Import.pm:829-860).
    """
    return bool(_snapshot().get("scanning")) or _RUNNING is not None or bool(_QUEUE)


def scanning_mode() -> str:
    """The scan type ``stillScanning()`` reports (empty when idle)."""
    if _RUNNING:
        return _RUNNING
    snap = _snapshot()
    return "full" if snap.get("scanning") else ""


def has_scan_task() -> bool:
    """Perl ``Slim::Music::Import->hasScanTask()`` (Import.pm:849)."""
    return bool(_QUEUE)


def last_failure() -> Optional[str]:
    """The last failed scan's message (Perl's ``failure`` progress info)."""
    return _LAST_FAILURE


def clear_failure() -> None:
    """Forget a previous failure (Perl ``Progress->clear``, Import.pm:385-412)."""
    global _LAST_FAILURE
    _LAST_FAILURE = None


def note_failure(message: str) -> None:
    """Record a failed scan (Perl's ``type='importer', name='failure'`` row)."""
    global _LAST_FAILURE
    _LAST_FAILURE = message


def totaltime() -> str:
    """``totaltime`` as ``HH:MM:SS`` (Queries.pm:3282-3285 ``sprintf``)."""
    if _STARTED_AT is None:
        return "00:00:00"
    elapsed = max(0, int(time.monotonic() - _STARTED_AT))
    hrs, rem = divmod(elapsed, 3600)
    mins, sec = divmod(rem, 60)
    return f"{hrs:02d}:{mins:02d}:{sec:02d}"


def progress_results() -> list[tuple[str, Any]]:
    """The ordered result list of ``rescanprogressQuery`` (Queries.pm:3231-3300).

    Perl adds ``rescan`` first, then one result per Progress row (name →
    percent ``int(done/total*100)``, ``-1`` when the total is unknown), then
    ``steps`` (the joined names) and ``totaltime``; while idle it adds
    ``rescan 0`` plus ``lastscanfailed`` when a failure row is left over.
    """
    snap = _snapshot()
    scanning = bool(snap.get("scanning")) or _RUNNING is not None

    if not scanning:
        results: list[tuple[str, Any]] = [("rescan", 0)]
        if _LAST_FAILURE:
            results.append(("lastscanfailed", _LAST_FAILURE))
        return results

    # Perl's Progress row for the in-process scan (MediaFolderScan.pm:43-79
    # startScan → Scanner::Local with progress => 1); this port tracks it in
    # SCAN_STATE, whose totals are 'total_files'/'done_files'.
    total = int(snap.get("total_files") or snap.get("total") or 0)
    done = int(snap.get("done_files") or 0)
    percent = -1 if not total else int(done / total * 100)

    results = [("rescan", 1), ("importer", percent),
               ("steps", "importer"), ("totaltime", totaltime())]
    return results


# ---------------------------------------------------------------------------
# Starting a scan
# ---------------------------------------------------------------------------


def normalise_mode(mode: Any) -> str:
    """Perl's mode token (Commands.pm:2689) — lower-cased, ``1``/``once`` = full."""
    token = str(mode or "").strip().lower()
    return DEFAULT_MODE if token in FULL_ALIASES else token


def _source_path(mode: str, target: Optional[str]) -> Optional[Path]:
    """The directory the scan walks (Perl ``getMediaDirs``/``getAudioDirs``).

    * a ``_target`` (``rescan _mode _target``) is Perl's ``$singledir``
      (Commands.pm:2700-2708) — that one folder only;
    * ``playlists`` scans the playlists folder (:2800-2808);
    * everything else scans the configured media dirs (:2789-2811), which is
      what :mod:`lyrion.media.music_dir` resolves ('musicdir'/'audiodir', then
      the OS music folder).

    ``None`` means "no usable folder": Perl then skips the scan with
    ``"Skipping media folder scan - no folders defined."``
    (``Slim/Media/MediaFolderScan.pm:47-52``) — it never scans a folder nobody
    configured, and neither do we.
    """
    if target:
        from urllib.parse import unquote

        text = str(target).strip()
        if text.startswith("file://"):
            text = unquote(text[len("file://"):])
        return Path(text)

    if mode == "playlists":
        from lyrion.config import get_config

        try:
            playlistdir = str(get_config().get("playlistdir", "") or "").strip()
        except Exception:  # noqa: BLE001
            playlistdir = ""
        if playlistdir:
            return Path(playlistdir)

    from lyrion.media.music_dir import resolve_music_dir

    return resolve_music_dir()


def request_scan(mode: Any = DEFAULT_MODE, target: Optional[str] = None) -> str:
    """Perl's ``rescanCommand`` wiring — returns ``started``/``queued``/``error``.

    ``started``  the scan process was launched (Commands.pm:2746
                 ``launchScan`` / Import.pm:106-240);
    ``queued``   a scan is already running, so the request became a scan task
                 for the running one (Commands.pm:2712-2720);
    ``error``    ``album``/``track`` without an id (Commands.pm:2693-2699);
                 Perl logs and answers bad dispatch (the CLI echoes).
    """
    token = normalise_mode(mode)

    if token in OBJECT_MODES and not target:
        logger.error("Album or Tracks scan modes need an album or track ID.")
        return "error"

    if is_scanning():
        _QUEUE.append((token, target))
        logger.info(
            "Scan already running (%s) — queued a %s scan "
            "(Perl: queueScanTask, Slim/Control/Commands.pm:2712-2720)",
            scanning_mode() or "scan", token)
        if _RUNNING is None:
            # A scan this module did not start (the importer running from
            # another entry point): watch for its end, then run the queue.
            _ensure_queue_watcher()
        return "queued"

    _launch(token, target)
    return "started"


def request_abort() -> None:
    """Stop the running scan — Perl's ``abortScanCommand`` (Commands.pm:46-49).

    ``Slim::Music::Import->abortScan`` (Import.pm:257-270) sets ``$ABORT``; the
    scanner learns about it from the server's answer to its own progress notify
    (``Slim/Utils/SQLiteHelper.pm:429-444``) and shuts down.  For a scan that
    runs in a child process the flag has to travel through the shared abort
    marker, so the channels are armed first when a scan is under way.
    """
    from lyrion.media.scan_state import SCAN_STATE

    if is_scanning():
        try:
            from lyrion.media.scan_state import scan_channel_paths, set_channels

            set_channels(*scan_channel_paths())
        except Exception as exc:  # noqa: BLE001 - abort must never raise
            logger.warning("Scan-Kanäle nicht verfügbar: %s", exc)
    SCAN_STATE.request_abort()


def _child_paths() -> tuple[Optional[Path], Optional[Path]]:
    """``(db_path, serverdata)`` the scan process must use.

    Perl hands the scanner its own ``prefsdir``/``logdir`` explicitly
    (``Slim/Music/Import.pm:120-137``) so it opens the *same* database; the
    port passes the resolved library DB path and the server data root.
    """
    db_path: Optional[Path] = None
    serverdata: Optional[Path] = None
    try:
        from lyrion.config import get_config

        db_path = Path(get_config().db_path)
    except Exception as exc:  # noqa: BLE001 - resolved again in the child
        logger.warning("Bibliotheks-DB-Pfad nicht ermittelbar: %s", exc)
    try:
        from lyrion.platform import paths as platform_paths

        serverdata = platform_paths.resolve_serverdata_dir()[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Serverdata-Verzeichnis nicht ermittelbar: %s", exc)
    return db_path, serverdata


def _launch(mode: str, target: Optional[str]) -> None:
    """Start the scan **in its own process** — Perl's ``launchScan``.

    ``_RUNNING`` and the published scan state are set *before* the child runs:
    Perl's ``isScanning`` metainformation is written when the scan is started
    (``Slim/Music/Import.pm:206-207``), not when it reaches its first file —
    that is what makes a second ``rescan`` arriving right behind the first one
    a queued task instead of a concurrent scan (Commands.pm:2712-2720).
    """
    global _RUNNING, _STARTED_AT, _PROC

    # A partial walk (one folder / one album) must never reconcile deletions:
    # this port's importer drops every track it did not see when the mode is a
    # full one (``ImportConfig.delete_missing``), which for a single-folder
    # walk would wipe the rest of the library.  Perl reconciles the scanned
    # folder only (Slim/Utils/Scanner/Local.pm:186).
    partial = bool(target) or mode in OBJECT_MODES
    importer_mode = "refresh" if partial else mode

    clear_failure()

    source = _source_path(mode, target)
    if source is None:
        # Perl: no folder defined → no scan (Slim/Media/MediaFolderScan.pm:
        # 47-52); resolve_music_dir() already logged *why* it is unusable.
        logger.error("Skipping media folder scan - no folders defined.")
        return

    from lyrion.media import scan_process, scan_state

    # Arm the cross-process progress/abort channel before the child starts
    # (Perl: the scanner and the server share the progress rows +
    # metainformation.isScanning, Slim/Utils/Progress.pm:298-341).
    try:
        scan_state.prepare_channels()
    except Exception as exc:  # noqa: BLE001 - a scan without progress beats none
        logger.warning("Scan-Kanäle nicht verfügbar (%s) — Scan startet trotzdem",
                       exc)

    _RUNNING = mode
    _STARTED_AT = time.monotonic()

    db_path, serverdata = _child_paths()
    if db_path is None:
        # Without the DB path the child could resolve the wrong library
        # (a legacy DB of another installation); that is not a scan we start.
        note_failure("scan process not started: library DB path unknown")
        _RUNNING = None
        _STARTED_AT = None
        logger.error("Scan-Prozess nicht gestartet: Bibliotheks-DB-Pfad unbekannt")
        return

    priority = scan_process.scanner_priority()
    try:
        _PROC = scan_process.spawn_scan_worker(
            source, importer_mode, db_path=db_path, serverdata=serverdata,
            priority=priority, parent_pid=os.getpid())
    except OSError as exc:
        # Perl: a scanner that dies right away is detected by stillScanning
        # and logged as "External scanner exited without completing the scan."
        # (Slim/Music/Import.pm:772-786).
        _PROC = None
        note_failure(f"scan process not started: {exc}")
        _RUNNING = None
        _STARTED_AT = None
        logger.error("Scan-Prozess nicht startbar (nice=%d): %s", priority, exc)
        return

    logger.info("Starting %s scan of %s in process %d (nice=%d)",
                mode, source, _PROC.pid, priority)
    try:
        asyncio.create_task(_watch_scan_process(_PROC, mode))
    except RuntimeError as exc:
        # No running event loop: the scan itself is fine, its supervision is
        # not (Perl's _watchScanner runs off the server's timer queue,
        # Slim/Music/Import.pm:242-258).  The published state stays readable.
        logger.warning("Scan-Prozess nicht überwachbar (%s)", exc)


async def _watch_scan_process(proc: Any, mode: str) -> None:
    """Wait for the scan process, then clean up and run the queue.

    Perl polls its scanner while a scan runs and treats a process that is gone
    with ``isScanning`` still set as a crash
    (``Slim/Music/Import.pm:242-258`` ``_watchScanner``, ``:760-791``
    ``stillScanning``).
    """
    global _RUNNING, _STARTED_AT, _PROC

    try:
        while proc.poll() is None:
            await asyncio.sleep(_POLL_INTERVAL)
        code = proc.returncode
    except Exception as exc:  # noqa: BLE001 - a watcher must never crash
        logger.warning("Scan-Prozess-Überwachung gestoppt: %s", exc)
        code = None

    from lyrion.media.scan_state import live_published_state

    if code:
        note_failure(f"scan process exited with code {code}")
        logger.error(
            "External scanner exited without completing the scan (exit=%s). "
            "Perl: Slim/Music/Import.pm:772-786", code)
    elif live_published_state(max_age=0) is None:  # forced fresh read
        logger.debug("Scan-Prozess %s beendet", getattr(proc, "pid", "?"))

    _PROC = None
    _RUNNING = None
    _STARTED_AT = None
    # Detach from the shared progress/abort files (Perl clears the progress and
    # drops the scanning flag when the scanner is gone, scanner.pl:436-461
    # ``cleanup``).  A queued scan re-arms them in ``_launch``.
    from lyrion.media.scan_state import clear_channels

    clear_channels()
    _start_next_queued()



def _start_next_queued() -> None:
    """Perl ``Slim::Music::Import->nextScanTask()`` (Import.pm:829-848)."""
    if not _QUEUE:
        return
    mode, target = _QUEUE.popleft()
    logger.info("Starting queued %s scan", mode)
    _launch(mode, target)


def _ensure_queue_watcher() -> None:
    """Wait for an externally started scan, then run the queue."""
    global _WATCHER
    if _WATCHER is not None and not _WATCHER.done():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:  # no running loop: nothing to watch from
        return
    _WATCHER = asyncio.create_task(_watch_queue())


async def _watch_queue() -> None:
    """Poll the shared scan state until the foreign scan ends, then kick."""
    try:
        while _snapshot().get("scanning") and _QUEUE:
            await asyncio.sleep(0.5)
        _start_next_queued()
    except Exception as exc:  # noqa: BLE001 - a watcher must never crash
        logger.warning("Scan queue watcher stopped: %s", exc)


def reset() -> None:
    """Drop queue/failure bookkeeping (tests, and Perl's ``Progress->clear``)."""
    global _RUNNING, _STARTED_AT, _LAST_FAILURE, _WATCHER, _PROC
    _QUEUE.clear()
    _RUNNING = None
    _STARTED_AT = None
    _LAST_FAILURE = None
    _WATCHER = None
    _PROC = None
    # Detach from the shared progress/abort files: a leftover path would make a
    # later scan read a foreign (or deleted) marker as its own state.
    try:
        from lyrion.media.scan_state import clear_channels

        clear_channels()
    except Exception:  # noqa: BLE001 - reset must never raise
        pass
