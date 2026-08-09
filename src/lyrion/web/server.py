"""HTTP server for Lyrion Music Server web UI."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import urllib.parse
from pathlib import Path
from typing import Optional

import anyio
from anyio import BrokenResourceError
from anyio.streams.memory import MemoryObjectReceiveStream

logger = logging.getLogger(__name__)

# Default ports
DEFAULT_HTTP_PORT = 9000
DEFAULT_CLI_PORT = 9090


class WebServer:
    """Async HTTP server for the LMS web UI.

    Serves:
    - Static files from html/ directory
    - JSON-RPC API at /jsonrpc.js (POST)
    - Artwork at /music/<id>/cover.jpg
    - Audio stream at /stream.mp3
    - Player status at /status.txt
    """

    def __init__(
        self,
        http_port: int = DEFAULT_HTTP_PORT,
        html_root: Optional[Path] = None,
    ) -> None:
        self.http_port = http_port
        self.html_root = html_root or Path("/root/lyrion-python/html")
        self._running = False
        self._server: Optional[anyio.abc.Server] = None
        self._shutdown_event = asyncio.Event()
        self._jsonrpc_handler = None
        self._scope: Optional[dict] = None

        # Register default MIME types
        mimetypes.add_type("text/css", ".css")
        mimetypes.add_type("application/javascript", ".js")
        mimetypes.add_type("image/svg+xml", ".svg")
        mimetypes.add_type("image/x-icon", ".ico")

    def set_jsonrpc_handler(self, handler) -> None:
        """Set the JSON-RPC handler (JSONRPCAPI instance)."""
        self._jsonrpc_handler = handler

    async def start(self) -> None:
        """Start the HTTP server."""
        logger.info("Starting web server on port %d", self.http_port)
        self._running = True

        async def handle_request(scope: dict, receive: MemoryObjectReceiveStream, send):
            """ASGI-compatible request handler."""
            await self._handle_request(scope, receive, send)

        self._server = await anyio.create_server(
            handle_request,
            host="0.0.0.0",
            port=self.http_port,
            reuse_address=True,
        )
        logger.info("Web server started on http://0.0.0.0:%d", self.http_port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.aclose()
            self._server = None
        logger.info("Web server stopped")

    async def _handle_request(
        self,
        scope: dict,
        receive: MemoryObjectReceiveStream,
        send,
    ) -> None:
        """Handle a single HTTP request."""
        path = scope["path"]
        method = scope["method"]

        # Read request body
        body = b""
        try:
            async for chunk in receive:
                if isinstance(chunk, bytes):
                    body += chunk
                else:
                    body += chunk.get(b"body", b"")
        except (BrokenResourceError, Exception):
            pass

        # Route request
        if path == "/jsonrpc.js" and method == "POST":
            response = await self._handle_jsonrpc(body)
        elif path == "/status.txt" and method == "GET":
            response = await self._handle_status()
        elif path.startswith("/music/") and "/cover" in path:
            response = await self._handle_artwork(path)
        elif path.startswith("/stream"):
            response = await self._handle_stream(scope)
        elif path.startswith("/api/"):
            response = await self._handle_api(path, method, body)
        else:
            response = await self._handle_static(path, method)

        # Send response
        status, headers, body_out = response
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(k.encode(), v.encode()) for k, v in headers],
        })
        await send({
            "type": "http.response.body",
            "body": body_out,
        })

    async def _handle_jsonrpc(self, body: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
        """Handle JSON-RPC requests."""
        if self._jsonrpc_handler is None:
            return 500, [("Content-Type", "text/plain")], b"JSON-RPC handler not configured"

        try:
            response_body = await self._jsonrpc_handler.handle_request(body)
            return 200, [("Content-Type", "application/json")], response_body
        except Exception as e:
            logger.exception("JSON-RPC error")
            return 500, [("Content-Type", "application/json")], b'{"error": "internal error"}'

    async def _handle_status(self) -> tuple[int, list[tuple[str, str]], bytes]:
        """Return plain-text server/player status."""
        lines = ["Lyrion Music Server\n", "version: 9.2.0\n"]

        # Import here to avoid circular
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            players = pm.get_connected_players()
            for p in players:
                power = "on" if p.power else "off"
                lines.append(f"player count: {len(players)}\n")
                lines.append(
                    f"player ip: {p.ip} {p.mac} {p.name} {p.model} {power} {p.mode}\n"
                )
        except Exception:
            lines.append("player count: 0\n")

        body = "".join(lines).encode()
        return 200, [("Content-Type", "text/plain")], body

    async def _handle_artwork(self, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
        """Handle artwork requests like /music/<id>/cover.jpg."""
        # Parse track ID from path: /music/12345/cover.jpg
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            track_id = parts[1]
        else:
            return 404, [], b"not found"

        try:
            from lyrion.media.artwork import ArtworkHandler
            handler = ArtworkHandler()
            data = await handler.get_cover_artwork(int(track_id))
            if data:
                return 200, [("Content-Type", "image/jpeg")], data
        except Exception:
            pass

        # Fallback: serve default cover
        default_cover = self.html_root / "html" / "images" / "cover_default.jpg"
        if default_cover.exists():
            return 200, [("Content-Type", "image/jpeg")], default_cover.read_bytes()

        return 404, [], b"not found"

    async def _handle_stream(self, scope: dict) -> tuple[int, list[tuple[str, str]], bytes]:
        """Handle /stream.mp3 and related streaming endpoints."""
        # TODO: Implement actual streaming with range requests
        return 200, [("Content-Type", "audio/mpeg")], b""

    async def _handle_api(
        self, path: str, method: str, body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        """Handle REST API endpoints."""
        return 404, [("Content-Type", "text/plain")], b"not found"

    async def _handle_static(
        self, path: str, method: str
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        """Serve static files from html/ directory."""
        # Normalize path
        if path == "/" or path == "/html" or path == "/html/index.html":
            target = self.html_root / "html" / "index.html"
        elif path.startswith("/html/"):
            target = self.html_root / path.lstrip("/")
        else:
            target = self.html_root / "html" / path.lstrip("/")

        # Security: prevent path traversal
        try:
            resolved = target.resolve()
            html_resolved = self.html_root.resolve()
            if not str(resolved).startswith(str(html_resolved)):
                return 403, [], b"Forbidden"
        except (ValueError, OSError):
            return 404, [], b"Not found"

        if not target.exists():
            return 404, [], b"Not found"

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(str(target))
        if mime_type is None:
            mime_type = "application/octet-stream"

        try:
            content = target.read_bytes()
        except OSError:
            return 500, [], b"Could not read file"

        # CORS headers. HTML is served without cache so UI updates are
        # picked up immediately (a stale index.html broke playlist adds).
        headers = [("Content-Type", mime_type), ("Access-Control-Allow-Origin", "*")]
        if mime_type == "text/html":
            headers.append(("Cache-Control", "no-cache, no-store, must-revalidate"))
        return 200, headers, content


class CLIServer:
    """TCP CLI server on port 9090.

    Accepts text-based CLI commands (one per line) and returns
    text responses. Used by the web UI and CLI tools.
    """

    def __init__(self, port: int = DEFAULT_CLI_PORT) -> None:
        self.port = port
        self._running = False
        self._server: Optional[anyio.abc.Server] = None
        self._cli_handler = None

    def set_cli_handler(self, handler) -> None:
        self._cli_handler = handler

    async def start(self) -> None:
        logger.info("Starting CLI server on port %d", self.port)
        self._running = True

        async def handle_client(reader, writer):
            try:
                while self._running:
                    line_bytes = await reader.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8").strip()
                    if not line:
                        continue

                    if self._cli_handler:
                        try:
                            result = await self._cli_handler.handle_raw(line)
                            if result is not None:
                                response = result + "\n"
                            else:
                                response = ""
                        except Exception as e:
                            response = f"ERR: {e}\n"
                    else:
                        response = ""

                    writer.write(response.encode("utf-8"))
                    await writer.drain()
            except (BrokenResourceError, ConnectionResetError):
                pass
            finally:
                writer.close()
                try:
                    await writer.aclose()
                except Exception:
                    pass

        self._server = await anyio.create_server(
            handle_client,
            host="127.0.0.1",
            port=self.port,
            reuse_address=True,
        )
        logger.info("CLI server started on tcp://127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.aclose()
            self._server = None
        logger.info("CLI server stopped")
