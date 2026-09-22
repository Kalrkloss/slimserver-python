"""Lebenszyklus der internen ASGI-Instanz (uvicorn).

Die interne ASGI-App (``lyrion.web.app``) läuft als uvicorn-Task im
Serverprozess (``lyrion/__main__.py``).  Dieses Modul kapselt zwei Dinge, die
dort sonst leicht falsch verdrahtet werden:

1. ``install_shutdown_signal_forwarding`` — ``uvicorn.Server.serve()``
   ersetzt beim Start die Prozess-Handler für SIGINT/SIGTERM durch
   ``Server.handle_exit`` (uvicorn/server.py:330) und setzt damit **nur**
   ``should_exit``.  Unser eigener Shutdown-Flag
   (``lyrion.bootstrap.request_shutdown``) erfährt davon nichts; uvicorn
   reicht das Signal erst weiter, wenn ``serve()`` *regulär* zurückkehrt
   (uvicorn/server.py:336-340) — was ausbleibt, wenn der graceful shutdown
   noch auf offene Verbindungen wartet (``timeout_graceful_shutdown`` ist
   standardmäßig ``None``, uvicorn/config.py:231) und die Task nach unserem
   harten Timeout abgebrochen wird.
2. ``run_web_server_until_shutdown`` — die Warteschleife.  Sie endet
   ausschließlich bei einem angeforderten Shutdown und niemals "von allein":
   stoppt die ASGI-Instanz ohne Anforderung (Startfehler, abgebrochene Task),
   wird sie neu gestartet, statt einen halbtoten Prozess zu hinterlassen
   (ASGI-App tot, öffentliche Ports weiter belegt, jede Anfrage 502).

Belegter Schaden ohne diese Verdrahtung (live, 2026-09-22, PID 1703832):
16:14:14 ``Web wait loop exited (running=True shutdown=False should_exit=True)``,
danach *keine* weitere Shutdown-Zeile — der Prozess lief weiter, die ASGI-App
war zu, und erst 94 s später (SIGKILL durch ``tools/restart_server.sh``) kam
ein neuer Server hoch.  Dieselbe Signatur trat am Tag dreimal auf
(14:12:32, 15:53:12, 16:14:14).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import signal
from typing import Any, Callable, Optional

__all__ = [
    "install_shutdown_signal_forwarding",
    "run_web_server_until_shutdown",
]


def install_shutdown_signal_forwarding(
    server: Any,
    request_shutdown: Callable[[], None],
    log: logging.Logger,
) -> None:
    """Let uvicorn's captured SIGINT/SIGTERM also set *our* shutdown flag.

    ``uvicorn.Server.serve()`` installs ``Server.handle_exit`` as the process
    handler for SIGINT/SIGTERM.  That handler only sets ``should_exit``; the
    signal is re-raised to the original handler only when ``serve()`` returns
    normally (uvicorn/server.py:336-340).  Whenever that re-raise does not
    happen — the graceful shutdown is still waiting for open connections
    (``timeout_graceful_shutdown=None``) and our hard timeout cancels it — the
    process was left behind with a dead ASGI app and bound ports (502).
    Wrapping ``handle_exit`` makes the signal a controlled, logged shutdown
    request on the same flag the rest of the process observes.
    """
    original_handle_exit = server.handle_exit

    def handle_exit(sig: int, frame: Any) -> None:
        try:
            signal_name = signal.Signals(sig).name
        except ValueError:  # pragma: no cover - unknown signal number
            signal_name = str(sig)
        log.info("Shutdown signal %s received — stopping the server", signal_name)
        request_shutdown()
        original_handle_exit(sig, frame)

    server.handle_exit = handle_exit


async def run_web_server_until_shutdown(
    make_server: Callable[[], Any],
    *,
    request_shutdown: Callable[[], None],
    is_running: Callable[[], bool],
    is_shutdown_requested: Callable[[], bool],
    log: logging.Logger,
    on_server_ready: Optional[Callable[[], Any]] = None,
    poll_s: float = 0.5,
    graceful_timeout_s: float = 10.0,
    restart_backoff_s: float = 1.0,
    max_restart_backoff_s: float = 30.0,
) -> Any:
    """Serve the internal ASGI app until a shutdown is requested.

    Returns only when ``is_running()`` is False or ``is_shutdown_requested()``
    is True.  An ASGI instance that stops without such a request is started
    again (with backoff) — the process must never be left behind with a dead
    ASGI app, which is what every web request answered with 502.

    ``on_server_ready`` runs once, as soon as the first instance has bound its
    socket (used to start the native frontend on the public port, which relays
    to this app).

    Returns the last ``uvicorn.Server`` instance (for logging the final state).
    """
    server: Any = None
    task: Optional[asyncio.Task[Any]] = None
    ready_notified = False
    backoff_s = restart_backoff_s

    while True:
        if not is_running() or is_shutdown_requested():
            break

        server = make_server()
        install_shutdown_signal_forwarding(server, request_shutdown, log)
        task = asyncio.create_task(server.serve())

        while is_running() and not is_shutdown_requested():
            if on_server_ready is not None and server.started and not ready_notified:
                ready_notified = True
                result = on_server_ready()
                if inspect.isawaitable(result):
                    await result
            if server.should_exit:
                # uvicorn captured a signal (see above) or the app asked to
                # stop: that is a shutdown request, not a reason to end the
                # process half-way.
                log.info("ASGI server reports should_exit — requesting shutdown")
                request_shutdown()
                break
            if task.done():
                break
            await asyncio.sleep(poll_s)

        if not is_running() or is_shutdown_requested():
            break

        error: Any = None
        if task.done() and not task.cancelled():
            error = task.exception()
        log.error(
            "ASGI server stopped without a shutdown request "
            "(should_exit=%s, error=%r) — restarting in %.1fs",
            server.should_exit, error, backoff_s,
        )
        await _cancel_task(task)
        await asyncio.sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, max_restart_backoff_s)

    if server is not None:
        await _shutdown_server(server, task, log, graceful_timeout_s)
    return server


async def _shutdown_server(
    server: Any,
    task: Optional[asyncio.Task[Any]],
    log: logging.Logger,
    graceful_timeout_s: float,
) -> None:
    """Stop the ASGI instance; never hang, never raise."""
    server.should_exit = True
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=graceful_timeout_s)
    except asyncio.TimeoutError:
        log.warning(
            "ASGI server did not stop within %.0fs (open connections/tasks) — "
            "cancelling it",
            graceful_timeout_s,
        )
        await _cancel_task(task)
    except BaseException as exc:  # noqa: BLE001 - shutdown must never raise
        log.warning("ASGI server shutdown ended with %r", exc)


async def _cancel_task(task: Optional[asyncio.Task[Any]], timeout_s: float = 5.0) -> None:
    """Cancel a task and wait for it, bounded; swallows its result/error."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout_s)
    except BaseException:  # noqa: BLE001 - the task is being thrown away
        pass
