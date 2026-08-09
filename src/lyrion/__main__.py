"""
Lyrion Music Server entry point.

Usage:
    python -m lyrion [--noweb] [--localfile FILE] [--loglevel LEVEL] ...
    lyrion [--noweb] [--localfile FILE] [--loglevel LEVEL] ...  (after install)
"""

from __future__ import annotations

import sys
import os
import signal
import asyncio
import logging
from pathlib import Path
from typing import NoReturn

import anyio

from lyrion.version import __version__, __build_date__


# ---------------------------------------------------------------------------
# Logger bootstrap (before full logging is initialized)
# ---------------------------------------------------------------------------

_bootstrap_logger: logging.Logger | None = None


def _bootstrap_log(message: str, *args: object, level: int = logging.INFO, exc_info: bool = False) -> None:
    global _bootstrap_logger
    if _bootstrap_logger is None:
        # Use basicConfig for pre-init logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _bootstrap_logger = logging.getLogger("lyrion.boot")
    _bootstrap_logger.log(level, message, *args, exc_info=exc_info)


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------

_pid_file: Path | None = None


def _write_pidfile(path: Path) -> None:
    """Write current process ID to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    _bootstrap_log("Wrote PID %d to %s", os.getpid(), path)


def _remove_pidfile(path: Path) -> None:
    """Remove the PID file if it belongs to this process."""
    try:
        if path.exists():
            pid = int(path.read_text(encoding="utf-8").strip())
            if pid == os.getpid():
                path.unlink()
    except (ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_running = True


def _signal_handler(signum: int, frame: object) -> None:
    sig_name = signal.Signals(signum).name
    _bootstrap_log("Received %s, initiating shutdown...", sig_name, level=logging.INFO)
    global _running
    _running = False


# ---------------------------------------------------------------------------
# Main async server coroutine
# ---------------------------------------------------------------------------

async def _run_server(
    config: "lyrion.config.LyrionConfig",  # noqa: F821
    log_level: str,
) -> None:
    """
    Run the Lyrion Music Server.

    This coroutine:
    1. Initializes the config system
    2. Sets up logging
    3. Initializes the database
    4. Starts the web server (if not --noweb)
    5. Starts the CLI port (if configured)
    6. Runs until shutdown
    """
    # Import here to avoid circular imports
    from lyrion.config import get_config, LyrionConfig
    from lyrion.utils.log import init_logging, get_logger
    from lyrion.database.sqlite_helper import init_db, close_db
    import lyrion.bootstrap as _bootstrap_mod

    # Used by both shutdown wait loops below. The active SIGTERM/SIGINT
    # handler is bootstrap.request_shutdown, which only sets the
    # _shutdown_requested flag (it no longer hard-stops the event loop).

    cfg = config if isinstance(config, LyrionConfig) else get_config()
    # Attach CLI args to config so services can read them
    cfg.set_cli_args(config)

    # Initialize config
    await cfg.init()

    # Initialize logging
    await init_logging(cfg.log_dir, log_level)
    log = get_logger("lyrion")

    log.info("=" * 60)
    log.info("Lyrion Music Server v%s (build %s)", __version__, __build_date__)
    log.info("Python %s", sys.version)
    log.info("Server data directory: %s", cfg.serverdata_dir)
    log.info("Prefs directory: %s", cfg.prefs_dir)
    log.info("Log directory: %s", cfg.log_dir)
    log.info("Cache directory: %s", cfg.cache_dir)
    log.info("=" * 60)

    # Initialize database
    await init_db(cfg.db_path)
    log.info("Database initialized at %s", cfg.db_path)

    # Determine server features — CLI flag always overrides DB pref.
    # Use None as sentinel so absent arg != explicit False.
    cli_noweb: bool | None = getattr(cfg.cli_args, "noweb", None)
    noweb = cli_noweb if cli_noweb is not None else bool(cfg.get("noweb", False))
    http_port = int(cfg.get("serverport", 9000))
    cli_port = int(cfg.get("cliport", 9090))

    log.info("Starting server on http://:%s (web=%s)", http_port, not noweb)

    _uvicorn_server: uvicorn.Server | None = None

    # Start CLI port
    try:
        from lyrion.control.cli_server import start_cli_server
        asyncio.create_task(start_cli_server(cli_port))
        log.info("CLI server listening on port %d", cli_port)
    except ImportError:
        log.debug("CLI server module not available yet")

    # Start Slimproto TCP server (port 3483 — players connect here)
    slimproto_port = int(cfg.get("slimproto_port", 3483))
    try:
        from lyrion.networking.protocol import SlimProtoClient
        slimproto = SlimProtoClient()
        asyncio.create_task(slimproto.serve(host="0.0.0.0", port=slimproto_port))
        log.info("Slimproto server listening on port %d", slimproto_port)

        # Wire the protocol handler into the PlayerManager so playback
        # commands (play/stop/pause) can reach connected players.
        from lyrion.player.manager import PlayerManager
        PlayerManager().set_protocol_handler(slimproto)
    except Exception as exc:
        log.warning("Could not start Slimproto server: %s", exc)

    # Start UDP discovery service (port 3483 — player beacon listener)
    try:
        from lyrion.player.manager import PlayerManager
        from lyrion.networking.discovery import DiscoveryService
        player_mgr = PlayerManager()
        discovery = DiscoveryService(discovery_port=slimproto_port)
        # Wire discovered players → PlayerManager
        def on_player_discovered(p):
            player_mgr.register_player(
                mac=p.mac.upper(),
                name=p.name,
                ip=p.ip,
                port=p.port,
                model=p.model,
            )
            log.info("Discovered player: %s (%s) at %s:%d", p.name, p.mac, p.ip, p.port)
        discovery.on_player(on_player_discovered)
        await discovery.start()
        log.info("DiscoveryService started on port %d", slimproto_port)

        # Periodic server announcement broadcast so remote apps find us
        asyncio.create_task(_broadcast_server_presence(log, slimproto_port, http_port))
    except Exception as exc:
        log.warning("Could not start discovery service: %s", exc)

    # No automatic library scan at startup: it monopolises the DB write lock
    # and starves player/app requests for hours on large libraries. The scan
    # runs on demand via the "rescan" JSON-RPC method / CLI command.

    # Start web server
    if not noweb:
        try:
            import uvicorn
            from lyrion.web.app import create_config

            base_dir = Path(__file__).parent.parent.parent
            static_dir = str(base_dir / "html")
            config_uvicorn = create_config(
                host="0.0.0.0",
                port=http_port,
                static_dir=static_dir,
            )
            _uvicorn_server = uvicorn.Server(config=config_uvicorn)
            log.info("Web server starting on http://0.0.0.0:%d", http_port)
            # Run uvicorn as a background task so our shutdown loop below can
            # observe both `_running` (our SIGTERM handler) and uvicorn's own
            # should_exit. A plain `await serve()` blocks forever because
            # uvicorn's signal handlers conflict with ours (systemd restart
            # would hang in "deactivating").
            uvicorn_task = asyncio.create_task(_uvicorn_server.serve())

            # Wait for shutdown signal or uvicorn stopping itself.
            # Active SIGTERM/SIGINT handler is bootstrap.request_shutdown,
            # which only sets the _shutdown_requested flag (no loop.stop()).
            while _running and not _bootstrap_mod._shutdown_requested and not _uvicorn_server.should_exit:
                await asyncio.sleep(0.5)
            log.info("Web wait loop exited (running=%s shutdown=%s should_exit=%s)",
                     _running, _bootstrap_mod._shutdown_requested,
                     _uvicorn_server.should_exit)

            # Graceful uvicorn shutdown with a hard timeout
            _uvicorn_server.should_exit = True
            try:
                await asyncio.wait_for(uvicorn_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                uvicorn_task.cancel()
                try:
                    # An active /stream request (player is streaming) can make
                    # uvicorn's shutdown hang — bound the await, then os._exit
                    # in main() guarantees the process terminates anyway.
                    await asyncio.wait_for(uvicorn_task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
        except ImportError:
            log.warning("uvicorn not installed — web interface disabled")
            noweb = True

    # Wait for shutdown signal (also the fallback path when uvicorn is not
    # available and never started).
    while _running and not _bootstrap_mod._shutdown_requested:
        await asyncio.sleep(0.5)

    log.info("Shutting down...")

    # Cancel all remaining background tasks (CLI server, slimproto server,
    # background scans) so no DB connections stay open. Without this,
    # close_db() -> engine.dispose() can block forever on active sessions.
    current_task = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current_task]
    log.info("Cancelling %d background task(s)", len(pending))
    for task in pending:
        task.cancel()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=5
            )
        except asyncio.TimeoutError:
            log.warning("%d task(s) did not stop within 5s", len(pending))
    log.info("Background tasks stopped")

    # Cleanup (with a hard timeout so shutdown can never hang)
    try:
        await asyncio.wait_for(close_db(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Database close timed out")
    log.info("Database closed")


async def _background_scan(log: logging.Logger) -> None:
    """Run initial library scan in the background after startup."""
    try:
        from lyrion.media.importer import MusicImporter, ImportConfig
        config = ImportConfig()
        importer = MusicImporter(config=config)
        log.info("Starting initial library scan of %s ...", config.source_path)
        stats = await importer.import_music()
        log.info(
            "Initial scan complete: imported=%d updated=%d skipped=%d errors=%d",
            stats.imported_files, stats.updated_files,
            stats.skipped_files, stats.error_files,
        )
    except Exception as exc:
        log.warning("Background scan failed: %s", exc)


async def _broadcast_server_presence(log: logging.Logger, slimproto_port: int, http_port: int) -> None:
    """Periodically broadcast server presence via UDP so remote apps can discover us."""
    import json as _json, socket as _socket, asyncio as _asyncio
    try:
        # Get primary network IP
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            s.connect(("192.168.1.1", 1))
            server_ip = s.getsockname()[0]
        except Exception:
            server_ip = "127.0.0.1"
        finally:
            s.close()

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)

        while True:
            try:
                msg = _json.dumps({
                    "name": "Lyrion Music Server",
                    "version": "9.2.0",
                    "host": server_ip,
                    "port": http_port,
                    "jsonrpc": f"http://{server_ip}:{http_port}/jsonrpc.js",
                }).encode()
                await _asyncio.to_thread(sock.sendto, msg, ("255.255.255.255", 3483))
                log.debug("Server presence broadcast sent to 255.255.255.255:3483")
            except Exception as exc:
                log.debug("Broadcast error: %s", exc)
            await _asyncio.sleep(30)
    except Exception as exc:
        log.warning("Server broadcast task failed: %s", exc)


# -----------------------------------------------------------------------------
# Daemonization (Unix only)
# ---------------------------------------------------------------------------

def _daemonize() -> bool:
    """Daemonize the process (Unix only). Returns True if we are the daemon."""
    if os.name != "posix":
        return True  # Not supported, continue normally

    # Double-fork daemonization
    try:
        pid = os.fork()
        if pid > 0:
            # Parent exits
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"First fork failed: {e}\n")
        sys.exit(1)

    # First child: detach from process group
    os.chdir("/")
    os.setsid()
    os.umask(0o022)

    try:
        pid = os.fork()
        if pid > 0:
            # Second parent exits
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"Second fork failed: {e}\n")
        sys.exit(1)

    # Redirect standard file descriptors
    devnull = os.open("/dev/null", os.O_RDWR)
    os.dup2(devnull, 0)  # stdin
    os.dup2(devnull, 1)  # stdout
    os.dup2(devnull, 2)  # stderr
    os.close(devnull)

    return True  # We are the daemon


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int | NoReturn:
    """
    Main entry point for Lyrion Music Server.

    Parses CLI arguments, sets up the event loop, and runs the server.
    """
    global _pid_file, _running

    import argparse

    parser = argparse.ArgumentParser(
        prog="lyrion",
        description="Lyrion Music Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  lyrion                        Start the server
  lyrion --noweb                Start without web interface
  lyrion --loglevel debug       Start with debug logging
  lyrion --localfile my.conf    Use custom config file
  lyrion --daemon --pidfile /run/lyrion.pid   Daemonize
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--noweb", action="store_true",
        help="Disable the web interface",
    )
    parser.add_argument(
        "--localfile", metavar="FILE",
        help="Load configuration from FILE instead of default location",
    )
    parser.add_argument(
        "--serverdata", metavar="DIR",
        help="Set server data directory (prefs, cache, logs)",
    )
    parser.add_argument(
        "--nobrowsecache", action="store_true",
        help="Disable the browse cache",
    )
    parser.add_argument(
        "--prefsfile", metavar="FILE",
        help="SQLite preferences database path",
    )
    parser.add_argument(
        "--logfile", metavar="FILE",
        help="Log file path (overrides default)",
    )
    parser.add_argument(
        "--loglevel",
        choices=["debug", "info", "warning", "error", "critical"],
        default="info",
        help="Set logging level (default: info)",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run as a background daemon (Unix only)",
    )
    parser.add_argument(
        "--pidfile", metavar="FILE",
        help="Write process ID to FILE",
    )
    parser.add_argument(
        "--playeraddr", metavar="ADDR",
        help="Bind to specific address for player discovery",
    )
    parser.add_argument(
        "--httpport", type=int, metavar="PORT",
        help="HTTP port for web interface (default: 9000)",
    )
    parser.add_argument(
        "--cliport", type=int, metavar="PORT",
        help="CLI port for telnet/text protocol (default: 9090)",
    )
    parser.add_argument(
        "--language", metavar="LANG",
        help="Set interface language (default: en)",
    )
    parser.add_argument(
        "--novirtualCL", action="store_true",
        help="Disable virtual CL plugin",
    )
    parser.add_argument(
        "--upnp", action="store_true",
        help="Enable UPnP/DLNA media server",
    )

    args = parser.parse_args(argv)

    # Version check (--version triggers argparse to print and exit)
    # Just verify the args parsed correctly
    if not hasattr(args, "noweb"):
        parser.print_help()
        sys.exit(1)

    # Daemonize if requested (before any logging)
    if args.daemon:
        if _daemonize():
            # Daemon process continues
            pass
        else:
            # Parent process exits
            return 0

    # Write PID file
    if args.pidfile:
        _pid_file = Path(args.pidfile)
        _write_pidfile(_pid_file)

    # Set up signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGHUP, _signal_handler)

    # Bootstrap: paths, environment, event loop
    try:
        loop = _bootstrap()
    except Exception as e:
        _bootstrap_log("Bootstrap failed: %s", e, level=logging.ERROR)
        return 1

    # Run the server
    exit_code = 0
    try:
        loop.run_until_complete(_run_server(args, args.loglevel))
    except KeyboardInterrupt:
        _bootstrap_log("Interrupted by user")
    except Exception as e:
        _bootstrap_log("Server error: %s", e, level=logging.ERROR, exc_info=True)
        exit_code = 1
    finally:
        loop.close()
        if _pid_file:
            _remove_pidfile(_pid_file)
        _bootstrap_log("Server stopped")

    # Force-exit: lingering non-daemon threads (music-scan executors,
    # aiosqlite workers, uvloop internals) keep the process alive after the
    # event loop stopped. Without this, `systemctl restart` hangs for the
    # full TimeoutStopSec (90s) until systemd sends SIGKILL.
    os._exit(exit_code)


def _bootstrap() -> asyncio.AbstractEventLoop:
    """Internal bootstrap function."""
    from lyrion.bootstrap import bootstrap, _setup_paths, _setup_environment, _setup_signals, _init_event_loop

    _setup_paths()
    _setup_environment()
    _setup_signals()
    return _init_event_loop()


if __name__ == "__main__":
    sys.exit(main())
