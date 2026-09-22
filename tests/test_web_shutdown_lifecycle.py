"""Warteschleife und Shutdown-Pfad des internen ASGI-Servers.

Deckt den Fehler ab, der den Serverprozess halbtot zurückließ (live
2026-09-22 16:14:14: ``Web wait loop exited (running=True shutdown=False
should_exit=True)``, danach ASGI-App tot, Ports belegt, jede Web-Anfrage 502):

* uvicorns ``handle_exit`` setzt nur ``should_exit`` — unser Shutdown-Flag
  bleibt ohne Weiterleitung False (uvicorn/server.py:342-347).
* Die Warteschleife darf sich nie selbst beenden; stoppt die ASGI-Instanz
  ohne Anforderung, wird sie neu gestartet.
* Ein echtes SIGTERM muss ein kontrollierter, geloggter Shutdown sein
  (Rohprobe ``tests/_asgi_shutdown_probe.py``, beide Zustände).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "_asgi_shutdown_probe.py"
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from lyrion.web.lifecycle import (  # noqa: E402
    install_shutdown_signal_forwarding,
    run_web_server_until_shutdown,
)

TEST_LOG = logging.getLogger("test.web.lifecycle")


async def _app(scope, receive, send) -> None:
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
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _config() -> uvicorn.Config:
    return uvicorn.Config(app=_app, host="127.0.0.1", port=0, log_level="warning",
                          loop="asyncio", lifespan="on", access_log=False)


class _Flags:
    """Die beiden Produktions-Flags (bootstrap._shutdown_requested, _running)."""

    def __init__(self) -> None:
        self.running = True
        self.shutdown = False

    def request(self) -> None:
        self.shutdown = True


class _FakeServer:
    """Minimal-uvicorn: nur die Fläche, die der Lebenszyklus anfasst."""

    def __init__(self, fail_first: bool = False) -> None:
        self.should_exit = False
        self.started = True
        self.fail_first = fail_first
        self.serve_calls = 0

    async def serve(self) -> None:
        self.serve_calls += 1
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("ASGI-Start fehlgeschlagen")
        while not self.should_exit:
            await asyncio.sleep(0.01)

    def handle_exit(self, sig, frame) -> None:  # uvicorn-API, wird umhüllt
        self.should_exit = True


def _http_get(port: int, path: str = "/") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    return data


# ---------------------------------------------------------------------------
# uvicorns Signal-Handler vs. unser Flag
# ---------------------------------------------------------------------------


def test_uvicorn_handle_exit_alone_leaves_our_flag_false():
    """Ohne Weiterleitung setzt SIGTERM nur uvicorns ``should_exit``."""
    flags = _Flags()
    server = uvicorn.Server(config=_config())

    server.handle_exit(signal.SIGTERM, None)

    assert server.should_exit is True
    assert server._captured_signals == [signal.SIGTERM]
    assert flags.shutdown is False


def test_shutdown_forwarding_sets_our_flag_and_uvicorn_exit():
    """Mit Weiterleitung ist das Signal eine Shutdown-Anforderung."""
    flags = _Flags()
    server = uvicorn.Server(config=_config())

    install_shutdown_signal_forwarding(server, flags.request, TEST_LOG)
    server.handle_exit(signal.SIGTERM, None)

    assert flags.shutdown is True
    assert server.should_exit is True
    assert server._captured_signals == [signal.SIGTERM]


# ---------------------------------------------------------------------------
# Warteschleife: kein Selbstbeenden, Neustart statt halbtoter Prozess
# ---------------------------------------------------------------------------


def test_wait_loop_keeps_serving_without_shutdown_request():
    """Ohne Anforderung endet die Warteschleife nicht und der Port antwortet."""
    flags = _Flags()
    holder: dict[str, uvicorn.Server] = {}

    def make_server() -> uvicorn.Server:
        server = uvicorn.Server(config=_config())
        holder["server"] = server
        return server

    async def scenario() -> None:
        task = asyncio.create_task(run_web_server_until_shutdown(
            make_server,
            request_shutdown=flags.request,
            is_running=lambda: flags.running,
            is_shutdown_requested=lambda: flags.shutdown,
            log=TEST_LOG,
            poll_s=0.05,
            graceful_timeout_s=2.0,
        ))
        for _ in range(300):
            server = holder.get("server")
            if server is not None and server.started:
                break
            await asyncio.sleep(0.02)
        port = holder["server"].servers[0].sockets[0].getsockname()[1]
        # Der blockierende HTTP-Aufruf muss in einen Thread: im Event-Loop
        # würde er die Annahme der Verbindung selbst verhindern.
        response = await asyncio.to_thread(_http_get, port)
        assert response.startswith(b"HTTP/1.1 200") and b"ok" in response, response

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        assert not task.done(), "Warteschleife darf sich nicht selbst beenden"
        assert flags.shutdown is False

        flags.request()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_should_exit_without_signal_requests_shutdown():
    """Stoppt die ASGI-Instanz selbst, wird daraus ein Shutdown — kein Hang."""
    flags = _Flags()
    server = _FakeServer()

    async def scenario() -> None:
        async def flip() -> None:
            await asyncio.sleep(0.1)
            server.should_exit = True

        asyncio.create_task(flip())
        await asyncio.wait_for(run_web_server_until_shutdown(
            lambda: server,
            request_shutdown=flags.request,
            is_running=lambda: flags.running,
            is_shutdown_requested=lambda: flags.shutdown,
            log=TEST_LOG,
            poll_s=0.02,
            graceful_timeout_s=1.0,
        ), timeout=5)

    asyncio.run(scenario())
    assert flags.shutdown is True


def test_unexpected_stop_is_restarted_not_exited():
    """Ein Stopp ohne Anforderung startet die ASGI-Instanz neu."""
    flags = _Flags()
    made: list[_FakeServer] = []

    def make_server() -> _FakeServer:
        server = _FakeServer(fail_first=not made)
        made.append(server)
        return server

    async def scenario() -> None:
        task = asyncio.create_task(run_web_server_until_shutdown(
            make_server,
            request_shutdown=flags.request,
            is_running=lambda: flags.running,
            is_shutdown_requested=lambda: flags.shutdown,
            log=TEST_LOG,
            poll_s=0.02,
            graceful_timeout_s=1.0,
            restart_backoff_s=0.05,
        ))
        for _ in range(300):
            if len(made) == 2:
                break
            await asyncio.sleep(0.02)
        assert len(made) == 2, "ausgefallene ASGI-Instanz wurde nicht neu gestartet"
        assert not task.done(), "Warteschleife darf sich nicht selbst beenden"
        assert flags.shutdown is False, "unerwarteter Stopp darf keinen Shutdown auslösen"

        flags.request()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    assert made[0].serve_calls == 1
    assert made[1].serve_calls == 1


# ---------------------------------------------------------------------------
# Rohprobe: echtes SIGTERM in einem eigenen Prozess, beide Zustände
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode,expect_code,expect_text", [
    ("fixed", 0, "shutdown=True"),
    ("legacy", 3, "HANG"),
])
def test_asgi_shutdown_probe(mode: str, expect_code: int, expect_text: str):
    """``legacy`` reproduziert den Hang, ``fixed`` endet kontrolliert (Exit 0)."""
    proc = subprocess.run(
        [sys.executable, str(PROBE), "--mode", mode, "--graceful-timeout", "3"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == expect_code, output
    assert expect_text in output, output
    if mode == "legacy":
        assert "shutdown=False" in output
        assert "asgi_port=closed" in output
