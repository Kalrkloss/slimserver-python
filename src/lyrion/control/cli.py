"""
CLI command handler for Lyrion Music Server.

Implements the Logitech Media Server CLI protocol over TCP port 9090.
Commands are space-separated with optional arguments. Responses are
plain text lines terminated by a blank line.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

import orjson

if TYPE_CHECKING:
    from lyrion.control.request import RequestDispatcher
else:
    RequestDispatcher: Any = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response format enum
# ---------------------------------------------------------------------------


class ResponseFormat(str, Enum):
    """CLI response format."""

    TEXT = "text"
    JSON = "json"
    JSON_THRESHOLD = "json Threshold"
    STREAM = "stream"  # streaming response (no terminating blank line)


# ---------------------------------------------------------------------------
# Command registration registry
# ---------------------------------------------------------------------------

_COMMAND_REGISTRY: dict[str, tuple[Callable[..., Awaitable[list[str]]], tuple[str, ...]]] = {}
"""Global registry of registered CLI commands."""


def register_command(
    name: str | tuple[str, ...],
    *aliases: str,
) -> Callable[[Callable[..., Awaitable[list[str]]]], Callable[..., Awaitable[list[str]]]]:
    """
    Decorator to register a CLI command handler.

    Usage::

        @register_command('play', 'p')
        async def cmd_play(handler, client, args) -> list[str]:
            ...

    Args:
        name: Primary command name (or tuple of names for compound commands).
        aliases: Optional alias names.

    Returns:
        Decorator that registers the function.
    """

    def decorator(
        func: Callable[..., Awaitable[list[str]]],
    ) -> Callable[..., Awaitable[list[str]]]:
        names = (name,) if isinstance(name, str) else name
        for n in names + aliases:
            _COMMAND_REGISTRY[n] = (func, names)
        return func

    return decorator


def get_registered_commands() -> dict[str, tuple[Callable[..., Awaitable[list[str]]], tuple[str, ...]]]:
    """Return a copy of the command registry."""
    return dict(_COMMAND_REGISTRY)


# ---------------------------------------------------------------------------
# Client context
# ---------------------------------------------------------------------------


@dataclass
class CLIContext:
    """Per-client CLI session context."""

    client_id: str = ""
    player_id: Optional[str] = None  # bound player (default player)
    authenticated: bool = False
    format: ResponseFormat = ResponseFormat.TEXT
    tags: str = ""  # tag string (e.g. "aLyZ" for web UI)
    charset: str = "utf-8"
    subscribed_player: Optional[str] = None


# ---------------------------------------------------------------------------
# Base command handlers
# ---------------------------------------------------------------------------


def _parse_mixer_param(param: str) -> tuple[str, Optional[str]]:
    """Parse a mixer parameter=value or mixer parameter? form."""
    if "?" in param:
        param = param.rstrip("?")
        return param, None
    if "=" in param:
        key, val = param.split("=", 1)
        return key, val
    return param, None


# ---------------------------------------------------------------------------
# CLI Handler
# ---------------------------------------------------------------------------


class CLIHandler:
    """
    Main CLI command processor.

    Handles the Logitech Media Server CLI wire protocol over TCP.
    Each connection gets its own :class:`CLIContext` instance.

    Usage::

        handler = CLIHandler(dispatcher)
        async with handler.connect(reader, writer) as ctx:
            async for command in handler.read_commands(ctx):
                responses = await handler.dispatch(ctx, command)
                await handler.write_responses(ctx, responses)
    """

    RE_REQUEST_END = re.compile(r"^$")
    # LMS uses \n as line terminator; blank line signals end of request
    REQUEST_END = b"\n\n"
    LINE_END = b"\n"

    def __init__(self, dispatcher: Optional["RequestDispatcher"] = None) -> None:
        self._dispatcher = dispatcher
        self._auth_password: Optional[str] = None
        # Subscriptions: player_mac -> asyncio.Queue of events
        self._subscriptions: dict[str, asyncio.Queue[list[str]]] = {}

    # -----------------------------------------------------------------------
    # Configuration
    # -----------------------------------------------------------------------

    def set_auth_password(self, password: Optional[str]) -> None:
        """Set the CLI authentication password (empty/None = no auth)."""
        self._auth_password = password

    def set_dispatcher(self, dispatcher: "RequestDispatcher") -> None:
        """Set the request dispatcher."""
        self._dispatcher = dispatcher

    # -----------------------------------------------------------------------
    # Connection lifecycle
    # -----------------------------------------------------------------------

    async def __aenter__(self) -> "CLIHandler":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.shutdown()

    async def shutdown(self) -> None:
        """Cleanly shut down the CLI handler."""
        for queue in self._subscriptions.values():
            await queue.put(None)  # signal closed
        self._subscriptions.clear()

    # -----------------------------------------------------------------------
    # Connection context manager
    # -----------------------------------------------------------------------

    @asynccontextmanager
    async def connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> AsyncIterator[CLIContext]:
        """Create a CLI context for a new connection."""
        addr = writer.get_extra_info("peername")
        client_id = f"cli-{addr[1]}" if addr else "cli-unknown"
        ctx = CLIContext(client_id=client_id)
        ctx.player_id = self._get_default_player()
        try:
            yield ctx
        finally:
            # Send final blank line
            writer.write(self.REQUEST_END)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    # -----------------------------------------------------------------------
    # Command reading
    # -----------------------------------------------------------------------

    async def read_commands(
        self,
        reader: asyncio.StreamReader,
    ) -> AsyncIterator[tuple[str, list[str]]]:
        """
        Yield (command_name, args_list) tuples from the wire.

        The LMS CLI is line-based; commands are newline-separated and
        terminated by a blank line.
        """
        buf: list[bytes] = []
        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break  # EOF
            line_bytes = line_bytes.rstrip(b"\r\n")
            if not line_bytes:
                # blank line — flush accumulated request
                if buf:
                    request = b" ".join(buf).decode("utf-8", errors="replace")
                    buf.clear()
                    cmd_name, *args = self._parse_request(request)
                    yield cmd_name, args
            else:
                buf.append(line_bytes)

    def _parse_request(self, request: str) -> tuple[str, list[str]]:
        """Split a raw request line into command + args."""
        parts = shlex.split(request)
        if not parts:
            return "", []
        return parts[0].lower(), parts[1:]

    # -----------------------------------------------------------------------
    # Command dispatch
    # -----------------------------------------------------------------------

    async def dispatch(
        self,
        ctx: CLIContext,
        raw: str | tuple[str, list[str]],
    ) -> list[str]:
        """
        Dispatch a CLI command.

        Args:
            ctx: Client session context.
            raw: Either a raw command string or (cmd, args) tuple.

        Returns:
            Response lines (each terminated by \\n in write).
        """
        if isinstance(raw, str):
            cmd, args = self._parse_request(raw)
        else:
            cmd, args = raw

        # Authentication check
        if not ctx.authenticated and cmd not in ("login", "exit", ""):
            if self._auth_password:
                return ["login: "]

        # Look up registered command
        if cmd in _COMMAND_REGISTRY:
            func, _ = _COMMAND_REGISTRY[cmd]
            try:
                return await func(self, ctx, args)
            except Exception as exc:
                logger.exception("CLI command %s raised: %s", cmd, exc)
                return [f"cli error: {exc}"]
        else:
            # Handle compound commands (space-separated)
            return await self._dispatch_compound(ctx, cmd, args)

    async def _dispatch_compound(
        self,
        ctx: CLIContext,
        cmd: str,
        args: list[str],
    ) -> list[str]:
        """Try to match compound commands like 'playlist play'."""
        # Try two-word compound
        # The cmd already includes the first word; check for space-joined variants
        for registry_cmd, (func, _) in _COMMAND_REGISTRY.items():
            if " " in registry_cmd and registry_cmd.startswith(cmd + " "):
                # matched a compound command pattern
                rest = registry_cmd[len(cmd) + 1 :]
                parts = rest.split()
                # args should match the rest
                try:
                    return await func(self, ctx, args)
                except Exception as exc:
                    logger.exception("CLI compound command %s raised: %s", registry_cmd, exc)
                    return [f"cli error: {exc}"]
        return [f"unknown command: {cmd}"]

    # -----------------------------------------------------------------------
    # Response writing
    # -----------------------------------------------------------------------

    async def write_responses(
        self,
        writer: asyncio.StreamWriter,
        ctx: CLIContext,
        lines: list[str],
    ) -> None:
        """Write response lines to the client."""
        for line in lines:
            data = line.encode(ctx.charset, errors="replace") + self.LINE_END
            writer.write(data)
        writer.write(self.REQUEST_END)
        await writer.drain()

    # -----------------------------------------------------------------------
    # Public helper — format a response
    # -----------------------------------------------------------------------

    def format_text(self, lines: list[str]) -> list[str]:
        """Return lines as-is for TEXT format."""
        return lines

    def format_json(self, data: Any) -> list[str]:
        """Return JSON-serialised data as a single line."""
        return [orjson.dumps(data).decode()]

    # -----------------------------------------------------------------------
    # Player helpers (delegate to dispatcher when available)
    # -----------------------------------------------------------------------

    def _get_default_player(self) -> Optional[str]:
        if self._dispatcher:
            return self._dispatcher.get_default_player()
        return None

    async def _player_command(
        self,
        ctx: CLIContext,
        cmd: str,
        *args: str,
    ) -> list[str]:
        """Send a player-level command via the dispatcher."""
        if self._dispatcher is None:
            return ["server not available"]
        player_id = ctx.player_id
        if not player_id:
            return ["no player selected"]
        return await self._dispatcher.player_command(player_id, cmd, list(args))


# ---------------------------------------------------------------------------
# Context manager helper for async iteration
# ---------------------------------------------------------------------------

from typing import AsyncIterator

# Import built-in CLI command implementations so their @register_command
# decorators run and populate the command registry (cli_commands imports
# names from this module, so this must stay after the class definitions).
from lyrion.control import cli_commands as _builtin_commands  # noqa: E402,F401

__all__ = [
    "CLIHandler",
    "CLIContext",
    "ResponseFormat",
    "register_command",
    "get_registered_commands",
]
