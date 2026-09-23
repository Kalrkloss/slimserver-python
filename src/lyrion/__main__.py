"""
Pyrion Music Server entry point.

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
    Run the Pyrion Music Server.

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
    log.info("Pyrion Music Server v%s (build %s)", __version__, __build_date__)
    log.info("Python %s", sys.version)
    log.info("Server data directory: %s (from %s)",
             cfg.serverdata_dir, cfg.serverdata_source)
    log.info("Prefs directory: %s", cfg.prefs_dir)
    log.info("Log directory: %s", cfg.log_dir)
    log.info("Cache directory: %s", cfg.cache_dir)
    log.info("=" * 60)

    # Warn when a configured music folder is missing, unmounted, unreadable or
    # empty — the "mount gone → metadata yes, sound no" case.  Read-only, no
    # system change (lyrion.platform.paths).
    _warn_about_media_dirs(cfg, log)

    # Initialize database
    await init_db(cfg.db_path)
    log.info("Database initialized at %s", cfg.db_path)

    # Determine server features — CLI flag always overrides DB pref.
    # Use None as sentinel so absent arg != explicit False.
    cli_noweb: bool | None = getattr(cfg.cli_args, "noweb", None)
    noweb = cli_noweb if cli_noweb is not None else bool(cfg.get("noweb", False))
    # ONE public port (Perl parity): the native, pipelining-capable frontend
    # owns `public_http_port` and uvicorn serves `internal_http_port` behind it
    # (loopback only). `http_port` below is always the PUBLIC one — it is what
    # players are pointed at for /stream.mp3, what the TLV/beacon announce and
    # what serverstatus reports.
    from lyrion.config import frontend_enabled, http_ports
    public_port, internal_port = http_ports(cfg)
    http_port = public_port
    frontend = frontend_enabled(cfg)
    cli_port = int(getattr(cfg.cli_args, "cliport", None)
                   or cfg.get("cliport", 9090))

    log.info("Starting server on http://:%s (web=%s, frontend=%s, internal=%s)",
             http_port, not noweb, frontend, internal_port)

    _uvicorn_server: uvicorn.Server | None = None

    # Start CLI port
    try:
        from lyrion.control.cli_server import start_cli_server
        asyncio.create_task(start_cli_server(cli_port))
        log.info("CLI server listening on port %d", cli_port)
    except ImportError:
        log.debug("CLI server module not available yet")

    # Alarm clock scheduler: powers on players + starts their wake source
    # when a configured alarm fires (runs once per ~20 s).
    try:
        from lyrion.alarms import AlarmScheduler
        asyncio.create_task(AlarmScheduler(polling_interval=20.0).run())
        log.info("Alarm scheduler started (interval 20s)")
    except Exception as exc:  # noqa: BLE001
        log.warning("Alarm scheduler not started: %s", exc)

    # Start Slimproto TCP server (port 3483 — players connect here)
    slimproto_port = int(getattr(cfg.cli_args, "slimprotoport", None)
                         or cfg.get("slimproto_port", 3483))
    try:
        from lyrion.networking.protocol import SlimProtoClient
        slimproto = SlimProtoClient(web_port=http_port)
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

        # Periodic server announcement broadcast so remote apps find us.
        # The JSON beacon names the SAME port the discovery TLV announces:
        # the ONE public port. It is served by the native frontend, which
        # answers the Bayeux POSTs directly (SqueezePlay pipelines several on
        # ONE socket, which uvicorn/h11 cannot, h11_impl.py:191-197) and
        # relays everything else (/jsonrpc.js, artwork, /stream.mp3) to the
        # internal ASGI app. Two beacons naming different ports send apps to
        # a front-end that is not the intended one — measured 2026-09-14: the
        # TLV said 9080 while this beacon said 9000, so JSON-beacon apps
        # landed on uvicorn and SqueezePlay kept two sessions in parallel.
        asyncio.create_task(_broadcast_server_presence(
            log, slimproto_port, http_port))
    except Exception as exc:
        log.warning("Could not start discovery service: %s", exc)

    # No automatic library scan at startup: it monopolises the DB write lock
    # and starves player/app requests for hours on large libraries. The scan
    # runs on demand via the "rescan" JSON-RPC method / CLI command.

    # Plugins: discover manifests, resolve pending enable/disable states, load
    # and initialize (Perl ``Slim::Utils::PluginManager->init``/``load``,
    # PluginManager.pm:46-410).  Runs before the web server so a plugin's
    # ``/settings/...`` page and hooks exist when the first request arrives.
    try:
        from lyrion.plugins.manager import PluginManager

        plugin_manager = PluginManager()
        # Third-party plugins below the server data dir (Perl's
        # ``dirsFor('Plugins')`` / ``InstalledPlugins/Plugins``,
        # PluginManagerDownloader.pm:74-151).
        plugin_manager.add_plugin_dir(cfg.serverdata_dir / "Plugins")
        await plugin_manager.startup()
    except Exception as exc:  # noqa: BLE001 — a broken plugin must not block boot
        log.warning("PluginManager not started: %s", exc, exc_info=True)

    # Start web server
    if not noweb:
        try:
            import uvicorn
            from lyrion.web.app import create_config
            from lyrion.web.api import JSONRPCAPI
            from lyrion.web.cometd import CometdManager
            from lyrion.web.lifecycle import run_web_server_until_shutdown

            # Shared JSON-RPC + Cometd manager (uvicorn AND the native
            # streaming server must see the same clients/subscriptions).
            jsonrpc_api = JSONRPCAPI()
            cometd_mgr = CometdManager(jsonrpc_api)
            # subscribe:N keep-alive — pushes fresh status to controller
            # subscriptions on their requested interval (else the apps
            # see no updates while a player is idle and reconnect).
            asyncio.create_task(cometd_mgr.keepalive_loop())

            # html/ lives at the repo root for editable/checkout installs
            # (src/lyrion -> root) and INSIDE the package for wheel installs
            # (hatchling force-include "html" -> "lyrion/html").
            import lyrion as _lyrion_pkg
            _pkg_dir = Path(_lyrion_pkg.__file__).resolve().parent
            html_dir = _pkg_dir / "html"          # wheel install
            if not html_dir.is_dir():
                html_dir = _pkg_dir.parent.parent / "html"  # checkout layout
            if not html_dir.is_dir():
                html_dir = Path.cwd() / "html"    # last resort
            static_dir = str(html_dir)
            # uvicorn is the INTERNAL app in the consolidated layout: it owns
            # routing/JSON-RPC/static files and listens on loopback only, while
            # the native frontend owns the public port and relays to it. In the
            # rollback layout (frontend disabled) uvicorn is the public server
            # again and binds 0.0.0.0, exactly as before.
            from lyrion.config import asgi_bind
            asgi_host, asgi_port = asgi_bind(cfg)
            def _make_asgi_server():
                """Frische interne ASGI-Instanz (erster Start und Neustart)."""
                log.info("Web server (intern) starting on http://%s:%d",
                         asgi_host, asgi_port)
                return uvicorn.Server(config=create_config(
                    host=asgi_host,
                    port=asgi_port,
                    static_dir=static_dir,
                    jsonrpc=jsonrpc_api,
                    cometd=cometd_mgr,
                ))

            # The native server is the FRONTEND on the public port: Perl
            # serves its one HTTP port itself and pipelines requests on it
            # (Slim/Web/HTTP.pm:2065-2072), which uvicorn cannot
            # (h11_impl.py:191-197). It answers /cometd natively and relays
            # every other request to the internal ASGI port above. Started as
            # soon as that ASGI app has bound its socket.
            async def _start_native_frontend() -> None:
                try:
                    if frontend:
                        from lyrion.networking.cometd_stream import start_cometd_server
                        asyncio.create_task(start_cometd_server(
                            cometd_mgr, "0.0.0.0", public_port, asgi_port))
                    else:
                        log.info("Native frontend disabled (public=%d internal=%d) "
                                 "— uvicorn serves the public port alone",
                                 public_port, internal_port)
                except Exception as exc:
                    log.warning("Could not start native frontend: %s", exc)

            # Serve until a shutdown is requested (SIGTERM/SIGINT →
            # controlled, logged shutdown; see lyrion.web.lifecycle). The ASGI
            # server must never end this process on its own: it used to stop
            # after uvicorn captured the signal without setting our flag, and
            # the process stayed behind with a dead ASGI app and bound ports —
            # every web request answered 502 until SIGKILL (live 2026-09-22
            # 16:14:14, and 15:53:12 / 14:12:32 the same day).
            _uvicorn_server = await run_web_server_until_shutdown(
                _make_asgi_server,
                request_shutdown=_bootstrap_mod.request_shutdown,
                is_running=lambda: _running,
                is_shutdown_requested=lambda: _bootstrap_mod._shutdown_requested,
                log=log,
                on_server_ready=_start_native_frontend,
            )
            log.info("Web wait loop exited (running=%s shutdown=%s should_exit=%s)",
                     _running, _bootstrap_mod._shutdown_requested,
                     None if _uvicorn_server is None
                     else _uvicorn_server.should_exit)
        except ImportError:
            log.warning("uvicorn not installed — web interface disabled")
            noweb = True

    # Wait for shutdown signal (also the fallback path when uvicorn is not
    # available and never started).
    while _running and not _bootstrap_mod._shutdown_requested:
        await asyncio.sleep(0.5)

    log.info("Shutting down...")

    # Plugins first (Perl ``shutdownPlugins``, PluginManager.pm:412-428) so a
    # plugin still sees a live config/DB while it tears down.
    try:
        from lyrion.plugins.manager import PluginManager

        await PluginManager().shutdown()
    except Exception as exc:  # noqa: BLE001
        log.warning("PluginManager shutdown failed: %s", exc)

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
    """Periodically broadcast server presence via UDP so remote apps can discover us.

    ``http_port`` is the ONE public port (the native frontend), never the
    internal ASGI port — JSON-beacon apps must land on the same server the
    TLV discovery names.
    """
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
                    "name": "Pyrion Music Server",
                    "version": "9.2.0",
                    "host": server_ip,
                    "port": http_port,
                    "jsonrpc": f"http://{server_ip}:{http_port}/jsonrpc.js",
                }).encode()
                await _asyncio.to_thread(sock.sendto, msg, ("255.255.255.255", 3483))
                log.debug("Server presence broadcast sent to 255.255.255.255:3483 "
                          "(port %d, jsonrpc %s)", http_port,
                          f"http://{server_ip}:{http_port}/jsonrpc.js")
            except Exception as exc:
                log.debug("Broadcast error: %s", exc)
            await _asyncio.sleep(30)
    except Exception as exc:
        log.warning("Server broadcast task failed: %s", exc)


# -----------------------------------------------------------------------------
# Daemonization (POSIX only — Windows uses a service, like Perl)
# -----------------------------------------------------------------------------

def _daemonize() -> bool:
    """Daemonize the process. Returns True if we are the daemon.

    POSIX only: the double fork needs ``os.fork``/``os.setsid``.  Windows has
    neither, and Perl does not daemonize there either — it runs under the
    Windows Service Manager instead (``Slim/Utils/OS/Win64.pm:55-89``
    ``runService``, ``:122-125`` restart via the service manager).  On Windows
    the flag is therefore a logged no-op and the server keeps running in the
    foreground, which is what a service wrapper expects.
    """
    if os.name != "posix":
        _bootstrap_log(
            "--daemon is not supported on this platform; staying in the "
            "foreground (Windows services are installed by the installer, "
            "like Perl's Slim::Utils::OS::Win64::runService)",
            level=logging.WARNING,
        )
        return True

    # Double-fork daemonization.
    try:
        pid = os.fork()
        if pid > 0:
            # Parent exits
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"First fork failed: {e}\n")
        sys.exit(1)

    # First child: detach from process group.  '/'-chdir is a POSIX convention;
    # the pid file (written later) is absolute, so it still lands correctly.
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

    # Redirect standard file descriptors to the platform's null device
    # (os.devnull is 'nul' on Windows, '/dev/null' elsewhere).
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)  # stdin
    os.dup2(devnull, 1)  # stdout
    os.dup2(devnull, 2)  # stderr
    os.close(devnull)

    return True  # We are the daemon


# -----------------------------------------------------------------------------
# Start-up check of the configured music folders
# -----------------------------------------------------------------------------

def _configured_media_dirs(cfg: object) -> list[str]:
    """The music folders the server is configured to use, de-duplicated.

    Mirrors Perl's preference shape: ``mediadirs`` is the current array
    preference (``Slim/Utils/Prefs.pm:102,163``), ``audiodir`` the legacy one
    it migrates from (``Prefs.pm:687-698``) and ``musicdir`` the per-client/old
    CLI name the port kept.
    """
    dirs: list[str] = []
    from lyrion.platform import paths as _platform_paths

    for name in ("mediadirs", "audiodir", "musicdir"):
        try:
            value = cfg.get(name, "")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            continue
        if isinstance(value, (list, tuple, set)):
            candidates = [str(v) for v in value]
        elif value:
            # The store serialises list preferences comma separated
            # (web/settings.py mirrors Server/Basic.pm:88-121); a .conf file
            # hands back the raw line.  Both go through the shared splitter so
            # a gvfs mount id (…,share=…) is not torn apart.
            candidates = _platform_paths.split_path_list(value)
        else:
            candidates = []
        for candidate in candidates:
            text = candidate.strip()
            if text and text not in dirs:
                dirs.append(text)
    return dirs


def _warn_about_media_dirs(cfg: object, log: logging.Logger) -> list[tuple[str, str, str]]:
    """Warn at start-up when a configured music folder is unusable.

    Perl only skips a *missing default* folder silently
    (``Slim/Utils/Prefs.pm:700-710``); the port additionally reports it,
    because with network mounts "metadata but no sound" is otherwise
    indistinguishable from "never scanned".  Distinguishes a missing file from
    a missing mount (``lyrion.platform.paths.explain_missing_path``) and never
    changes the system.
    """
    from lyrion.platform import paths as _paths

    dirs = _configured_media_dirs(cfg)
    problems = _paths.warn_about_media_dirs(dirs, log=log)
    if problems:
        log.warning(
            "Music folder check: %d of %d configured folder(s) are not usable "
            "(see the warnings above) — the library will be incomplete until "
            "they are back", len(problems), len(dirs),
        )
    else:
        log.info("Music folder check: %d configured folder(s) are usable: %s",
                 len(dirs), dirs)
    return problems


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int | NoReturn:
    """
    Main entry point for Pyrion Music Server.

    Parses CLI arguments, sets up the event loop, and runs the server.
    """
    global _pid_file, _running

    import argparse

    parser = argparse.ArgumentParser(
        prog="lyrion",
        description="Pyrion Music Server",
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
        "--public-http-port", type=int, metavar="PORT",
        help=("The ONE HTTP port every client is told about (default: 9000, "
              "served by the native frontend). 0 — or the same value as "
              "--internal-http-port — rolls the single-port consolidation "
              "back: uvicorn serves the public port alone."),
    )
    parser.add_argument(
        "--internal-http-port", type=int, metavar="PORT",
        help=("Internal port of the ASGI app behind the native frontend "
              "(default: 9001, loopback only, never announced)."),
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
    parser.add_argument(
        "--slimprotoport", type=int, metavar="PORT",
        help="SlimProto TCP port for players (default: 3483)",
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

    # Set up signal handlers.  SIGHUP does not exist on Windows (Python only
    # defines it there from 3.13 onward, and only for consoles), so it is
    # installed only when the platform has it — Perl does the same thing: its
    # Windows code never touches POSIX signals (Slim/Utils/OS/Win32.pm:331
    # ``dontSetUserAndGroup``, :715-765 restart via the Service Manager).
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    _sighup = getattr(signal, "SIGHUP", None)
    if _sighup is not None:
        try:
            signal.signal(_sighup, _signal_handler)
        except (OSError, ValueError, AttributeError):  # pragma: no cover
            pass

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
