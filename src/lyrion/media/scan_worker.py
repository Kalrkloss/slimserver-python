"""Standalone library scan worker — the port's ``scanner.pl``.

Run as ``python -m lyrion.media.scan_worker --source DIR --mode full --db FILE``;
started by :func:`lyrion.media.scan_process.spawn_scan_worker` from the
``rescan`` path (Perl: ``Slim::Music::Import->launchScan``,
``Slim/Music/Import.pm:106-240`` → ``scanner.pl``).

What this process owns (Perl evidence, all in ``scanner.pl``):

* a **database handle of its own** — ``scanner.pl:228-231``
  ``$sqlHelperClass->init()``, after ``:291`` ``beforeScan``; the port calls
  ``init_db(db_path)`` with the explicit path the server passed
  (Perl passes ``prefsdir=`` so the scanner resolves the same DB,
  ``Slim/Music/Import.pm:124-126``),
* the **process priority** — ``scanner.pl:213-218``
  ``Slim::Utils::Misc::setPriority(...)``,
* the **progress publication** — ``Slim/Utils/Progress.pm:298-341`` writes the
  Progress rows and a progress JSON file; :mod:`lyrion.media.scan_state`
  publishes the same snapshot for the server to read,
* the **abort handshake** — the scanner shuts down when the server answers its
  progress notify with ``abort`` (``Slim/Utils/SQLiteHelper.pm:429-444``,
  ``scanner.pl:436-461`` ``cleanup`` → ``setIsScanning(0)`` + ``exitScan``).

``scanner.pl:233-239`` refuses to start when a scan is already running
(``stillScanning``); the same guard is here, against the published state.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

#: Exit codes (Perl's scanner exits 0, or -1 after a fatal wipe error,
#: ``scanner.pl:316``; the "already running" path exits 0 after a message,
#: ``scanner.pl:233-239``).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ALREADY_SCANNING = 0

#: Perl stops notifying the server after MAX_RETRIES failed notifies
#: (``Slim/Utils/SQLiteHelper.pm:401``, ``:462-470``).
MAX_RETRIES = 3

logger = logging.getLogger("lyrion.media.scan_worker")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lyrion.media.scan_worker")
    parser.add_argument("--source", required=True,
                        help="directory to scan (Perl: singledir/media dirs)")
    parser.add_argument("--mode", default="full",
                        help="Perl rescan mode (Commands.pm:2689)")
    parser.add_argument("--db", required=True,
                        help="library DB path (Perl: prefsdir/…, Import.pm:124)")
    parser.add_argument("--serverdata", default=None,
                        help="server data root (Perl: --prefsdir)")
    parser.add_argument("--priority", type=int, default=0,
                        help="nice value, -20 (high) .. 20 (low) "
                             "(scanner.pl:139/:418)")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="the server process this scan reports to")
    parser.add_argument("--loglevel", default="info")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Resolve the server's data root before anything reads the config
    # (Perl: the scanner is handed --prefsdir/--logdir, Import.pm:120-137).
    if args.serverdata:
        os.environ["LYRION_SERVERDATA"] = str(args.serverdata)
    # Mark the process as a scan process for the DB pragmas
    # (Perl: the compile-time ``main::SCANNER`` constant, scanner.pl:23).
    os.environ["LYRION_SCANNER"] = "1"

    logging.basicConfig(
        level=getattr(logging, str(args.loglevel).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    from lyrion.media.scan_process import apply_process_priority

    nice = apply_process_priority(args.priority)

    try:
        return asyncio.run(_run(Path(args.source), args, nice))
    except KeyboardInterrupt:  # pragma: no cover - signal path
        return EXIT_FAILED


async def _run(source: Path, args: argparse.Namespace,
               nice: int | None) -> int:
    from lyrion.database.sqlite_helper import close_db, init_db
    from lyrion.media.importer import ImportConfig, MusicImporter
    from lyrion.media.scan_state import (
        SCAN_STATE,
        read_published,
        scan_channel_paths,
        set_channels,
    )

    logger.info(
        "Starting scan worker (pid=%d, mode=%s, nice=%s, source=%s, db=%s) — "
        "Perl: scanner.pl (Slim/Utils/OS.pm:233-235)",
        os.getpid(), args.mode, nice, source, args.db)

    # The progress/abort channels are resolved first: the "already scanning"
    # guard below reads the *published* state, which only works when the
    # channel path is known (Perl reads metainformation.isScanning from the
    # DB, which needs no setup, Slim/Music/Import.pm:233-239 + :760-791).
    set_channels(*scan_channel_paths())

    # scanner.pl:233-239 — there is no point in a second concurrent scanner.
    published = read_published()
    if (published and published.get("scanning")
            and int(published.get("pid") or 0) not in (0, os.getpid())):
        logger.error("Import: There appears to be an existing scanner running.")
        return EXIT_ALREADY_SCANNING

    # Its own DB handle: ``scanner.pl:228-231`` $sqlHelperClass->init().
    await init_db(Path(args.db))

    importer = MusicImporter(ImportConfig(source_path=source, mode=args.mode))
    parent_pid = int(args.parent_pid or 0)
    failed_notifies = 0

    def _on_progress(_stats) -> None:
        """Per-batch hook (Perl: the scanner's progress notify)."""
        nonlocal failed_notifies
        if not parent_pid:
            return
        if _parent_alive(parent_pid):
            failed_notifies = 0
            return
        failed_notifies += 1
        if failed_notifies == MAX_RETRIES:
            # Perl gives up notifying after MAX_RETRIES (the scan itself runs
            # to completion): Slim/Utils/SQLiteHelper.pm:401, :462-470.
            logger.warning(
                "Server process %d is gone — no progress reader left "
                "(Perl: MAX_RETRIES, Slim/Utils/SQLiteHelper.pm:401)", parent_pid)
            from lyrion.media.scan_state import clear_channels

            clear_channels()

    importer.add_progress_callback(_on_progress)

    code = EXIT_OK
    try:
        stats = await importer.import_music()
        logger.info(
            "Scan worker finished: imported=%d updated=%d skipped=%d errors=%d "
            "deleted=%d", stats.imported_files, stats.updated_files,
            stats.skipped_files, stats.error_files, stats.deleted_files)
        if SCAN_STATE.abort_requested:
            # Perl stores SCAN_ABORTED as the failure info
            # (Slim/Control/Queries.pm:6250-6252, SQLiteHelper.pm:436-441).
            logger.warning("SCAN_ABORTED")
    except Exception as exc:  # noqa: BLE001 - the server reports the failure
        logger.exception("Library scan failed (%s): %s", args.mode, exc)
        code = EXIT_FAILED
    finally:
        SCAN_STATE.finish()
        try:
            await close_db()
        except Exception as exc:  # noqa: BLE001
            logger.debug("close_db: %s", exc)
    return code


def _parent_alive(pid: int) -> bool:
    """Perl's ``scanningProcess->alive`` counter-piece (Import.pm:772-786)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:  # pragma: no cover
        return False
    return True


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
