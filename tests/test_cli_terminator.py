"""CLI-Terminator: genau EIN Terminator, und zwar der des Clients.

Perl ``Slim/Plugin/CLI/Plugin.pm``:

* ``:260``   neue Verbindung → ``$connections{$sock}{'terminator'} = $LF``
* ``:390``   beim Lesen wird ``/[\\r|\\n|\\r\\n|\\x00]+/`` gematcht
* ``:404-406`` "Remember the terminator used" — der Client gibt den Terminator
  für die Antworten dieser Verbindung vor
* ``:698``   ``client_socket_buffer($sock, $output . $terminator)`` — genau EIN
  Terminator pro Antwort

Vorher schickten wir ``<zeile>\\n`` **plus** ``REQUEST_END`` (``\\n\\n``), also
drei Zeilenumbrüche statt einem (Live-Diff gegen den Perl-LMS :9090 am
2026-09-12).
"""

from __future__ import annotations

import asyncio

from lyrion.control.cli import CLIHandler, CLIContext


class _FakeWriter:
    def __init__(self) -> None:
        self.data = b""

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        return None


def _run(coro):
    return asyncio.run(coro)


def test_response_ends_with_exactly_one_lf():
    handler = CLIHandler()
    writer = _FakeWriter()
    ctx = CLIContext(client_id="t")
    _run(handler.write_responses(writer, ctx, ["players 0 1 count%3A1"]))
    assert writer.data == b"players 0 1 count%3A1\n"


def test_client_terminator_is_echoed():
    handler = CLIHandler()
    writer = _FakeWriter()
    ctx = CLIContext(client_id="t")
    ctx.terminator = b"\r\n"          # Client sendet CRLF (Plugin.pm:404-406)
    _run(handler.write_responses(writer, ctx, ["version 9.2.0"]))
    assert writer.data == b"version 9.2.0\r\n"


def test_terminator_is_remembered_from_the_request():
    handler = CLIHandler()
    ctx = CLIContext(client_id="t")

    class _Reader:
        def __init__(self, data: bytes) -> None:
            self._data = data

        async def read(self, n: int) -> bytes:
            data, self._data = self._data, b""
            return data

    async def collect():
        out = []
        async for cmd in handler.read_commands(_Reader(b"version ?\r\n"), ctx):
            out.append(cmd)
        return out

    assert _run(collect()) == [("version", ["?"])]
    assert ctx.terminator == b"\r\n"   # nicht LF


def test_lf_client_keeps_lf():
    handler = CLIHandler()
    ctx = CLIContext(client_id="t")

    class _Reader:
        def __init__(self, data: bytes) -> None:
            self._data = data

        async def read(self, n: int) -> bytes:
            data, self._data = self._data, b""   # einmal liefern, dann EOF
            return data

    async def collect():
        out = []
        async for cmd in handler.read_commands(_Reader(b"ping\n"), ctx):
            out.append(cmd)
        return out

    assert _run(collect()) == [("ping", [])]
    assert ctx.terminator == b"\n"
