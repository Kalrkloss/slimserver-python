"""
ASGI/anyio web application for Lyrion Music Server.

Uses uvicorn as the HTTP server library, wrapped in anyio for consistency
with the project's async/await model.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import uvicorn

from .api import JSONRPCAPI, WebAPIHandler

logger = logging.getLogger(__name__)


def create_app(
    host: str = "0.0.0.0",
    port: int = 9000,
    static_dir: Optional[str] = None,
    jsonrpc: Optional[JSONRPCAPI] = None,
) -> Callable:
    """Create and return the ASGI application callable.

    This is a plain function (ASGI app), not a uvicorn.Config.
    Use create_config() from this module to get a ready-to-run uvicorn.Config.
    """
    jsonrpc_api = jsonrpc or JSONRPCAPI()
    api_handler = WebAPIHandler(jsonrpc_api)

    if static_dir:
        api_handler.set_static_dir(static_dir)

    async def app(scope: dict, receive, send) -> None:
        """ASGI application entry point."""
        method = scope.get("method", "GET")
        path = scope.get("path", "/")

        # Audio streaming gets a dedicated path (needs chunked body sends).
        if path.startswith("/stream") and method == "GET":
            from .stream import stream_track
            await stream_track(scope, receive, send)
            return

        # Read request body
        body = b""
        more_body = True
        while more_body:
            event = await receive()
            if event.get("type") == "http.request":
                body += event.get("body", b"")
                more_body = event.get("more_body", False)

        # Handle via API handler
        status, headers, response_body = await api_handler.handle(
            method, path, body
        )

        # Build ASGI response
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (k.encode(), v.encode()) for k, v in headers.items()
            ],
        })
        await send({
            "type": "http.response.body",
            "body": response_body,
        })

    logger.info(
        "ASGI app created: %s:%d, static=%s",
        host, port, static_dir,
    )
    return app


def create_config(
    host: str = "0.0.0.0",
    port: int = 9000,
    static_dir: Optional[str] = None,
    jsonrpc: Optional[JSONRPCAPI] = None,
    log_level: str = "info",
) -> uvicorn.Config:
    """Create a uvicorn.Config ready for uvicorn.Server.

    Args:
        host: Bind address.
        port: Bind port.
        static_dir: Path to serve static files from (html/, etc.).
        jsonrpc: Pre-configured JSONRPCAPI instance.
        log_level: Logging level for uvicorn.

    Returns:
        uvicorn.Config object ready for uvicorn.Server.
    """
    app = create_app(host=host, port=port, static_dir=static_dir, jsonrpc=jsonrpc)
    return uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level=log_level,
        access_log=True,
        loop="asyncio",
        lifespan="off",
    )


class WebServer:
    """ASGI-based web server for Lyrion Music Server.

    Wraps uvicorn and runs it as an anyio task.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
        static_dir: Optional[str] = None,
        jsonrpc: Optional[JSONRPCAPI] = None,
    ):
        self.host = host
        self.port = port
        self.static_dir = static_dir
        self.jsonrpc = jsonrpc
        self._config: Optional[uvicorn.Config] = None
        self._server: Optional[uvicorn.Server] = None
        self._stopping = False

    async def start(self) -> None:
        """Start the web server."""
        self._config = create_config(
            host=self.host,
            port=self.port,
            static_dir=self.static_dir,
            jsonrpc=self.jsonrpc,
        )
        self._server = uvicorn.Server(config=self._config)
        logger.info("WebServer starting on %s:%d", self.host, self.port)
        await self._server.serve()

    async def stop(self) -> None:
        """Stop the web server gracefully."""
        if self._server is None:
            return
        self._stopping = True
        self._server.should_exit = True
        logger.info("WebServer stopping")

    @property
    def running(self) -> bool:
        return self._server is not None and not self._stopping
