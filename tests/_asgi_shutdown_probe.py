#!/usr/bin/env python3
"""Rohe Sonde für den Shutdown-Pfad des internen ASGI-Servers.

Nachbau der Verdrahtung aus ``src/lyrion/__main__.py`` — uvicorn als Task,
Warteschleife, harte 10-s-Grenze, zweite Warteschleife — auf einem ephemeren
Loopback-Port, mit einer Anfrage, die nie antwortet (genau wie ein laufender
``/stream``: uvicorns graceful shutdown hat kein Timeout
(``timeout_graceful_shutdown=None``) und wartet darauf für immer).

    --mode legacy  Stand VOR dem Fix: SIGTERM landet bei uvicorns
                   ``handle_exit`` (nur ``should_exit``), unser Flag bleibt
                   False -> erwartet HANG (Exit 3)
    --mode fixed   über ``lyrion.web.lifecycle`` -> erwartet kontrollierten
                   Shutdown (Exit 0)

Aufruf:
    .venv/bin/python3 tests/_asgi_shutdown_probe.py --mode legacy
    .venv/bin/python3 tests/_asgi_shutdown_probe.py --mode fixed
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import uvicorn  # noqa: E402

log = logging.getLogger("probe")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

# Dieselben zwei Flags wie in Produktion: bootstrap._shutdown_requested und
# __main__._running.
running = True
shutdown_requested = False


def request_shutdown(signum: int | None = None, frame: object = None) -> None:
    """Wie lyrion.bootstrap.request_shutdown — setzt nur das Flag."""
    global shutdown_requested
    shutdown_requested = True


def _signal_handler(signum: int, frame: object) -> None:
    """Wie lyrion.__main__._signal_handler — setzt nur _running."""
    global running
    running = False


async def _app(scope: dict, receive, send) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    if scope["path"] == "/hold":
        # Antwortet nie: wie ein Player, der gerade streamt.
        await asyncio.sleep(3600)
        return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _config() -> uvicorn.Config:
    return uvicorn.Config(app=_app, host="127.0.0.1", port=0, log_level="warning",
                          loop="asyncio", lifespan="on", access_log=False)


def _port_of(server: uvicorn.Server) -> int | None:
    for srv in getattr(server, "servers", []):
        for sock in srv.sockets or []:
            return sock.getsockname()[1]
    return None


async def _wait_ready(server: uvicorn.Server, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            port = _port_of(server)
            if port:
                return port
        await asyncio.sleep(0.05)
    raise SystemExit("ASGI-Server wurde nicht bereit")


def _hold(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"GET /hold HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    return sock


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


async def _run_legacy(graceful_timeout: float) -> int:
    signal.signal(signal.SIGTERM, request_shutdown)  # wie bootstrap._setup_signals
    server = uvicorn.Server(config=_config())
    task = asyncio.create_task(server.serve())
    port = await _wait_ready(server)
    print(f"RESULT asgi_ready http://127.0.0.1:{port}")
    sock = _hold(port)  # noqa: F841 - bleibt offen (laufende Antwort)
    await asyncio.sleep(0.3)

    t0 = time.monotonic()
    os.kill(os.getpid(), signal.SIGTERM)  # der auslösende Fall
    print("RESULT sigterm_sent")

    # Warteschleife aus __main__.py
    while running and not shutdown_requested and not server.should_exit:
        await asyncio.sleep(0.2)
    print(f"RESULT wait_loop_exited after={time.monotonic() - t0:.1f}s "
          f"running={running} shutdown={shutdown_requested} "
          f"should_exit={server.should_exit} "
          f"uvicorn_captured_signals={getattr(server, '_captured_signals', None)}")

    # Graceful uvicorn shutdown mit harter Grenze (wie __main__.py: timeout=10)
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=graceful_timeout)
        print("RESULT uvicorn_stopped=gracefully")
    except asyncio.TimeoutError:
        print(f"RESULT uvicorn_stopped=timeout({graceful_timeout:.0f}s) -> cancel")
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    print(f"RESULT asgi_port={'open' if _port_open(port) else 'closed'}")

    # Zweite Warteschleife aus __main__.py (max 6 s beobachten)
    t1 = time.monotonic()
    while running and not shutdown_requested and time.monotonic() - t1 < 6.0:
        await asyncio.sleep(0.2)
    if running and not shutdown_requested:
        print("RESULT second_wait_loop=STUCK process_alive=True -> HANG "
              "(ASGI tot, öffentlicher Port belegt, jede Web-Anfrage 502)")
        return 3
    print("RESULT second_wait_loop=left_cleanly")
    return 0


async def _run_fixed(graceful_timeout: float) -> int:
    from lyrion.web.lifecycle import run_web_server_until_shutdown

    signal.signal(signal.SIGTERM, _signal_handler)  # wie __main__ (running-Flag)
    holder: dict[str, object] = {}

    def make_server() -> uvicorn.Server:
        server = uvicorn.Server(config=_config())
        holder["server"] = server
        return server

    async def on_ready() -> None:
        server = holder["server"]
        assert isinstance(server, uvicorn.Server)
        port = _port_of(server)
        assert port, "ASGI-Port nicht gefunden"
        holder["port"] = port
        holder["sock"] = _hold(port)
        await asyncio.sleep(0.3)
        print(f"RESULT asgi_ready http://127.0.0.1:{port}")
        os.kill(os.getpid(), signal.SIGTERM)
        print("RESULT sigterm_sent")

    t0 = time.monotonic()
    await run_web_server_until_shutdown(
        make_server,
        request_shutdown=request_shutdown,
        is_running=lambda: running,
        is_shutdown_requested=lambda: shutdown_requested,
        log=log,
        on_server_ready=on_ready,
        graceful_timeout_s=graceful_timeout,
    )
    server = holder["server"]
    port = holder.get("port")
    print(f"RESULT wait_loop_exited after={time.monotonic() - t0:.1f}s "
          f"running={running} shutdown={shutdown_requested} "
          f"should_exit={getattr(server, 'should_exit', None)} "
          f"uvicorn_captured_signals={getattr(server, '_captured_signals', None)}")
    print(f"RESULT asgi_port="
          f"{'open' if isinstance(port, int) and _port_open(port) else 'closed'}")
    print("RESULT process_alive=True (kontrollierter Shutdown, kein Hang)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("legacy", "fixed"), required=True)
    parser.add_argument("--graceful-timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    runner = _run_legacy if args.mode == "legacy" else _run_fixed
    return asyncio.run(runner(args.graceful_timeout))


if __name__ == "__main__":
    sys.exit(main())
