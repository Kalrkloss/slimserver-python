"""
LMS-compatible CLI server for Lyrion Music Server.

Telnet/text-mode CLI on port 9090 compatible with the Logitech Media
Server CLI specification (see PROTOCOL.md, chapter 4).

All command processing is delegated to the shared CLIHandler
(control/cli.py): line-oriented reading (one command per line, LF/CR/0x00
as line end), percent-decoding of parameters, the @register_command
registry (control/cli_commands.py) and blank-line terminated responses.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("lyrion.control.cli")


async def start_cli_server(port: int = 9090) -> None:
    """Start the LMS-compatible CLI server."""
    server = await asyncio.start_server(handle_client, host="0.0.0.0", port=port)
    logger.info("LMS CLI server listening on port %d", port)
    async with server:
        await server.serve_forever()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle a single CLI client connection (LMS-compatible).

    Delegates to CLIHandler: read_commands() yields one command per line
    (percent-decoded), dispatch() runs the registered handler, and
    write_responses() terminates each reply with a blank line.
    """
    from lyrion.control.cli import CLIHandler

    handler = CLIHandler()
    try:
        async with handler.connect(reader, writer) as ctx:
            async for cmd, args in handler.read_commands(reader):
                if cmd in ("exit", "quit"):
                    break
                lines = await handler.dispatch(ctx, (cmd, args))
                await handler.write_responses(writer, ctx, lines)
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("CLI client error: %s", exc)
