"""Tests for the CLI wire protocol (line terminators, percent-decoding).

Regression: ``readline()`` only split on LF, so CR- and NUL-terminated
commands (valid per the LMS spec) never dispatched; and the first token
(a percent-encoded player MAC) was not decoded before player resolution.
"""

import asyncio

from lyrion.control.cli import CLIHandler


async def _first_command(feed: bytes) -> tuple:
    reader = asyncio.StreamReader()
    reader.feed_data(feed)
    handler = CLIHandler()
    gen = handler.read_commands(reader)
    return await asyncio.wait_for(gen.__anext__(), timeout=1.0)


def test_read_commands_accepts_cr_terminator():
    assert asyncio.run(_first_command(b"ping\r")) == ("ping", [])


def test_read_commands_accepts_nul_terminator():
    assert asyncio.run(_first_command(b"ping\x00")) == ("ping", [])


def test_parse_request_percent_decodes_first_token():
    handler = CLIHandler()
    cmd, args = handler._parse_request("aa%3Abb%3Acc%3Add%3Aee%3Aff status - 1")
    assert cmd == "aa:bb:cc:dd:ee:ff"
    assert args == ["status", "-", "1"]
