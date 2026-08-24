"""
ASGI/anyio web application for Pyrion Music Server.

Uses uvicorn as the HTTP server library, wrapped in anyio for consistency
with the project's async/await model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import uvicorn

from .api import JSONRPCAPI, WebAPIHandler
from .cometd import LONG_POLL_TIMEOUT, CometdManager

logger = logging.getLogger(__name__)


async def _handle_streaming_connect(
    cometd: CometdManager,
    cid: str,
    msg: dict,
    replies: list[dict],
    send,
) -> None:
    """Streaming /meta/connect: reply immediately (acks + any queued
    events), then hold the response open and push events as chunks.

    SqueezeClient expects the connect + subscribe acks within 5 seconds
    and then reads the body as a stream of JSON arrays.
    """
    import json as _json

    connect_ack = {
        "channel": "/meta/connect",
        "successful": True,
        "clientId": cid,
        "id": msg.get("id", ""),
        "advice": {"reconnect": "retry", "interval": 0, "timeout": 25},
    }
    first = list(replies) + [connect_ack]
    events = await cometd.wait_for_events(cid, timeout=0)
    first.extend(events)

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"Content-Type", b"application/json"),
                    (b"Cache-Control", b"no-cache")],
    })
    await send({"type": "http.response.body",
                "body": _json.dumps(first).encode("utf-8"), "more_body": True})

    # Keep the stream open and push event batches as they arrive. The
    # uvicorn path (port 9000) serves clients that connect directly to
    # :9000 (Squeezer manual address) — closing after 0.6 s broke their
    # connection. Orange Squeeze (pipelined POSTs over one socket) is
    # served by the native server on 9080.
    try:
        while True:
            # A vanished client (meta/disconnect) makes wait_for_events
            # return [] immediately — without the existence check the
            # loop would spin at 100% CPU and freeze the whole server.
            if cometd.get(cid) is None:
                break
            events = await cometd.wait_for_events(cid, timeout=None)
            if not events:
                continue
            await send({"type": "http.response.body",
                        "body": _json.dumps(events).encode("utf-8"),
                        "more_body": True})
    except Exception:
        pass
    try:
        await send({"type": "http.response.body", "body": b"",
                    "more_body": False})
    except Exception:
        pass


async def _handle_cometd(cometd: CometdManager, path: str, receive, send) -> None:
    """Handle a POST /cometd batch (Jive controller protocol).

    Replies to handshake/subscribe/request immediately; /meta/connect
    long-polls (held open until events arrive or the timeout expires).
    """
    import json as _json

    body = b""
    more_body = True
    while more_body:
        event = await receive()
        if event.get("type") == "http.request":
            body += event.get("body", b"")
            more_body = event.get("more_body", False)

    try:
        messages = _json.loads(body.decode("utf-8", errors="replace"))
        if not isinstance(messages, list):
            messages = [messages]
        logger.info("Cometd POST %s: %.1200s", path, body.decode("utf-8", errors="replace")[:1200])
    except Exception as exc:
        logger.info("Cometd body not JSON (%s): %.160s", exc, body.decode("utf-8", errors="replace"))
        messages = []

    replies: list[dict] = []
    try:
        replies = await cometd.handle_messages(messages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cometd handle_messages failed: %s", exc)

    connect_msgs = [m for m in messages if isinstance(m, dict)
                    and m.get("channel") == "/meta/connect"]

    if connect_msgs:
        # /meta/connect: streaming clients (SqueezeClient, Material) need
        # the reply IMMEDIATELY (5s timeout) and then a held-open stream
        # that pushes events as chunks.
        for msg in connect_msgs:
            cid = msg.get("clientId", "")
            if msg.get("connectionType") == "streaming":
                await _handle_streaming_connect(cometd, cid, msg, replies, send)
                return
            # long-polling: hold until events arrive or timeout
            events = await cometd.wait_for_events(cid)
            replies.append({
                "channel": "/meta/connect",
                "successful": True,
                "clientId": cid,
                "id": msg.get("id", ""),
                "advice": {"reconnect": "retry", "interval": 0,
                           "timeout": LONG_POLL_TIMEOUT},
            })
            replies.extend(events)
    else:
        # No connect in this batch: deliver events pushed by
        # subscribe/request immediately (Jive expects the response in
        # the same reply batch when the request was sent standalone).
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            cid = msg.get("clientId", "")
            if not cid:
                continue
            events = await cometd.wait_for_events(cid, timeout=0)
            replies.extend(events)

    response = _json.dumps(replies).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"Content-Type", b"application/json"),
                    (b"Cache-Control", b"no-cache")],
    })
    await send({"type": "http.response.body", "body": response})


_MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}


async def _serve_album_cover(album_id: int, send) -> None:
    """Serve the cover image stored in Album.artwork for /music/<id>/cover.

    Reads the image file in a thread (SMB reads block) and streams it with
    long-lived cache headers — covers never change for an album id. Falls
    back to 404 when the album has no artwork or the file vanished.
    """
    import asyncio as _asyncio

    try:
        loop = _asyncio.get_running_loop()
        data, mime = await loop.run_in_executor(None, _load_sync_factory(album_id))
    except Exception:
        data, mime = None, None
    if not data:
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"Content-Type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"no artwork"})
        return
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"Content-Type", (mime or "image/jpeg").encode()),
            (b"Cache-Control", b"max-age=86400"),
        ],
    })
    await send({"type": "http.response.body", "body": data})


def _load_sync_factory(album_id: int):
    """Return a zero-arg callable that loads the cover synchronously.

    Runs inside the executor: creates its own event loop for the async
    DB session (aiosqlite needs one) and returns (data, mime).
    """
    def _run():
        import asyncio as _aio

        async def _q():
            from lyrion.database.sqlite_helper import db_session
            from lyrion.database.schema import Album
            from sqlalchemy import select
            from pathlib import Path as _Path

            async with db_session() as session:
                album = (
                    await session.execute(
                        select(Album).where(Album.id == album_id))
                ).scalar_one_or_none()
                if album is None or not album.artwork:
                    return None, None
                p = _Path(album.artwork)
                if not p.is_file():
                    return None, None
                mime = _MIME_BY_EXT.get(p.suffix.lower(), "image/jpeg")
                return p.read_bytes(), mime

        return _aio.run(_q())
    return _run


def create_app(
    host: str = "0.0.0.0",
    port: int = 9000,
    static_dir: Optional[str] = None,
    jsonrpc: Optional[JSONRPCAPI] = None,
    cometd: Optional[CometdManager] = None,
) -> Callable:
    """Create and return the ASGI application callable.

    This is a plain function (ASGI app), not a uvicorn.Config.
    Use create_config() from this module to get a ready-to-run uvicorn.Config.
    """
    jsonrpc_api = jsonrpc or JSONRPCAPI()
    api_handler = WebAPIHandler(jsonrpc_api)
    cometd = cometd or CometdManager(jsonrpc_api)

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

        # Album cover art (LMS convention): /music/<album_id>/cover.jpg.
        # Players/controllers request this URL to display album artwork.
        if path.startswith("/music/") and method == "GET":
            import re as _re

            m = _re.match(r"^/music/(\d+)/cover\.(jpg|png)$", path)
            if m:
                await _serve_album_cover(int(m.group(1)), send)
                return

        # Cometd (Jive controllers + Material Skin). libcometd sends the
        # action as a path suffix (/cometd/handshake, /cometd/connect,
        # /cometd/subscribe) — accept the base path and any suffix.
        if (path == "/cometd" or path.startswith("/cometd/")) and method == "POST":
            await _handle_cometd(cometd, path, receive, send)
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
    cometd: Optional[CometdManager] = None,
    log_level: str = "info",
) -> uvicorn.Config:
    """Create a uvicorn.Config ready for uvicorn.Server.

    Args:
        host: Bind address.
        port: Bind port.
        static_dir: Path to serve static files from (html/, etc.).
        jsonrpc: Pre-configured JSONRPCAPI instance.
        cometd: Pre-configured CometdManager (shared with the native
            streaming server).
        log_level: Logging level for uvicorn.

    Returns:
        uvicorn.Config object ready for uvicorn.Server.
    """
    app = create_app(host=host, port=port, static_dir=static_dir,
                     jsonrpc=jsonrpc, cometd=cometd)
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
    """ASGI-based web server for Pyrion Music Server.

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
