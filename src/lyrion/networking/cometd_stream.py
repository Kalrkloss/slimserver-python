"""Native HTTP frontend: the ONE public port (Perl parity) + Bayeux streaming.

Perl LMS has a single HTTP port (9000) and issues ``Slim/Web/HTTP.pm`` for
every request, Bayeux included. It can pipeline several POSTs on one socket
before reading — ``Slim/Web/HTTP.pm:2065-2072``::

    # Check for additional pipelined GET or HEAD requests we need to process
    # We also support pipelined cometd requets, even though this is against the HTTP RFC
    if ( ${*$httpClient}{httpd_rbuf} =~ m{^(?:GET|HEAD|POST /cometd)} ) {
        ... processHTTP($httpClient);

ASGI/uvicorn cannot serve that: it answers one request per connection and
buffers a request until the previous response has ended (h11_impl.py:191-197,
httptools_impl.py:291-297), no matter how early the streaming response
finishes. That is why this asyncio TCP server exists — and since the single
port is the whole point of the exercise, it is now the FRONTEND on the public
port (``public_http_port``, default 9000, bound on 0.0.0.0) and uvicorn moved
to an internal port (``internal_http_port``, default 9001, 127.0.0.1 only):

- ``POST /cometd`` is answered natively: handshake/subscribe/request via
  CometdManager, with the /meta/connect response kept open while further
  pipelined requests on the same socket are still processed;
- EVERY other method/path (GET/HEAD artwork + static files, the whole web UI,
  ``POST /jsonrpc.js``, ...) is RELAYED to the internal ASGI app — as a
  streaming pipe in both directions, so a body that never ends (``/stream.mp3``:
  a whole album file, up to ~900 MB) flows through instead of being buffered.

Announcements (TLV discovery, the JSON presence beacon, ``serverstatus``'s
``httpport``) all name the public port, which is the only one clients ever
see. Rollback needs no code revert: point ``internal_http_port`` back at 9000
and disable the frontend (``public_http_port`` 0 or equal to the internal one)
and uvicorn serves the public port again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from lyrion.web.cometd import (
    KEEPALIVE_TIMEOUT,
    LONG_POLL_TIMEOUT,
    RETRY_DELAY_MS,
    _client_id_from_channel,
    _http_timestamp,
    connect_ack,
    connect_advice,
    connect_timeout,
    has_invalid_client_advice,
    is_connect_channel,
)


logger = logging.getLogger(__name__)

MAX_BODY = 2 * 1024 * 1024

# Cap for a buffered ("self-delimiting") relay answer on a socket that must
# stay usable for the next pipelined request. Jive's HTTP/1.0 JSON-RPC replies
# are a few kB; anything above this is streamed with ``Connection: close``.
SELF_DELIMIT_MAX = 8 * 1024 * 1024

# A relayed response is written under the socket's write lock as ONE unit only
# while it is small; a big body (artwork, audio) holds the lock per chunk so a
# queued event batch can still go out in between.
ATOMIC_MAX = 512 * 1024

STREAM_CHUNK = 64 * 1024
HEAD_MAX = 1 << 16

# Headers that are connection-specific and must not be forwarded in either
# direction (RFC 7230 6.1 hop-by-hop); ``expect`` is handled locally (we answer
# the client's ``100 Continue`` ourselves) and ``host`` is rebuilt.
HOP_HEADERS = frozenset((
    b"host", b"connection", b"keep-alive", b"proxy-connection",
    b"transfer-encoding", b"upgrade", b"te", b"trailer", b"expect",
    b"proxy-authenticate", b"proxy-authorization",
))


class _TooLarge(Exception):
    """A request body/message exceeded the size this endpoint accepts."""


# ---------------------------------------------------------------------------
# HTTP parsing
# ---------------------------------------------------------------------------

async def _read_http_head(reader: asyncio.StreamReader) -> dict | None:
    """Read one HTTP request head (request line + headers). None on EOF."""
    try:
        line = await reader.readline()
    except (ConnectionError, OSError):
        return None
    if not line:
        return None
    parts = line.split()
    if len(parts) < 3:
        # A stray blank line / garbage line: nothing left to parse, the socket
        # is out of sync — drop the connection instead of looping on it.
        logger.info("NativeCometd: unerwartete Zeile: %.60r", line[:60])
        return None
    return {
        "method": parts[0].decode("ascii", "replace").upper(),
        "target": parts[1],
        "version": parts[2].decode("ascii", "replace"),
        "headers": await _read_head_fields(reader),
    }


async def _read_head_fields(reader: asyncio.StreamReader) -> dict:
    headers: dict[bytes, bytes] = {}
    while True:
        hline = await reader.readline()
        if hline in (b"\r\n", b"\n", b""):
            break
        if b":" in hline:
            k, _, v = hline.partition(b":")
            headers[k.strip().lower()] = v.strip()
    return headers


def _body_length(headers: dict) -> int:
    try:
        return int(headers.get(b"content-length") or 0)
    except ValueError:
        return 0


def _is_chunked_request(headers: dict) -> bool:
    return b"chunked" in (headers.get(b"transfer-encoding") or b"").lower()


def _expects_continue(headers: dict) -> bool:
    return b"100-continue" in (headers.get(b"expect") or b"").lower()


async def _read_chunked_bounded(reader: asyncio.StreamReader, cap: int) -> bytes:
    """De-frame a chunked request body (bounded by ``cap``)."""
    out = bytearray()
    while True:
        line = await reader.readline()
        if not line:
            raise asyncio.IncompleteReadError(bytes(out), len(out) + 1)
        try:
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        except ValueError as exc:
            raise ValueError(f"bad chunk size {line[:20]!r}") from exc
        if size == 0:
            while True:                      # trailers
                trailer = await reader.readline()
                if trailer in (b"\r\n", b"\n", b""):
                    break
            return bytes(out)
        if len(out) + size > cap:
            raise _TooLarge("chunked request body")
        out += await reader.readexactly(size)
        await reader.readexactly(2)          # trailing CRLF


async def _read_body_bounded(reader: asyncio.StreamReader, headers: dict,
                             cap: int = MAX_BODY) -> bytes:
    """Read a complete request body (both framings), de-framed."""
    if _is_chunked_request(headers):
        return await _read_chunked_bounded(reader, cap)
    length = _body_length(headers)
    if length <= 0:
        return b""
    if length > cap:
        raise _TooLarge(f"Content-Length {length}")
    return await reader.readexactly(length)


async def _read_http_request(reader: asyncio.StreamReader) -> dict | None:
    """Read one request INCLUDING its body (bounded, de-framed).

    Kept as the simple entry point for tools/tests: the returned dict carries
    ``method``/``target``/``version``/``headers`` and, when the request had a
    body, ``body``. The connection loop does not use it — it needs the first
    body byte to route Bayeux vs. relay and streams the rest.
    """
    head = await _read_http_head(reader)
    if head is None:
        return None
    if _body_length(head["headers"]) or _is_chunked_request(head["headers"]):
        head["body"] = await _read_body_bounded(reader, head["headers"])
    return head


async def _peek_request_body(reader: asyncio.StreamReader,
                             headers: dict) -> tuple[bytes, int]:
    """Return ``(prefix, remaining)`` of the request body.

    The FIRST body byte decides the route (a Bayeux batch starts with ``[``),
    so it is read here; the rest stays in the reader and is streamed to the
    internal app. A chunked request body is de-framed first (bounded), which
    also gives a relayed request a Content-Length upstream.
    """
    if _is_chunked_request(headers):
        return await _read_chunked_bounded(reader, MAX_BODY), 0
    length = _body_length(headers)
    if length <= 0:
        return b"", 0
    return await reader.readexactly(1), length - 1


async def _read_rest(reader: asyncio.StreamReader, remaining: int) -> bytes:
    if remaining <= 0:
        return b""
    if remaining > MAX_BODY:
        raise _TooLarge(f"body {remaining} bytes")
    return await reader.readexactly(remaining)


# ---------------------------------------------------------------------------
# Writing to the client socket
# ---------------------------------------------------------------------------

async def _send(writer, data: bytes, lock: asyncio.Lock | None = None) -> None:
    """Write ``data`` to ``writer`` and flush it.

    ``lock`` serialises this write against the push task (``_push_events``)
    and a relayed request that runs *while* the chunked connect response is
    still open. Jive pipelines the artwork/icon GET onto the very socket
    that carries the open streaming response, so the two writers share one
    transport; without the lock a pushed event batch could be spliced into
    the middle of a relayed response body (or vice versa).
    """
    if lock is None:
        writer.write(data)
        await writer.drain()
        return
    async with lock:
        writer.write(data)
        await writer.drain()


class _Emitter:
    """Writes a relayed response to the client socket.

    ``atomic`` takes the socket's write lock for the WHOLE response: while a
    ``/meta/connect`` body is open the push task writes to the same transport,
    and a relayed response that interleaves with it half-way would corrupt the
    framing. Perl interleaves independent responses only at response
    boundaries (``addHTTPResponse``, ``Slim/Web/HTTP.pm:1895-1960``). A big
    body (audio) must NOT hold the lock — a queued event batch would be
    delayed for minutes — so those fall back to per-chunk locking.
    """

    def __init__(self, writer, lock, atomic: bool,
                 activity: list | None = None) -> None:
        self.writer = writer
        self.lock = lock
        self.atomic = atomic
        self._held = False
        # ``activity`` is the streaming connection's "last byte written" clock
        # (``[monotonic]``). A relayed response counts as wire activity for the
        # stream that shares this socket, exactly like Perl re-arms its
        # keep-alive timer after every completed send (HTTP.pm:2091-2099).
        self.activity = activity

    async def __aenter__(self) -> "_Emitter":
        if self.atomic and self.lock is not None:
            await self.lock.acquire()
            self._held = True
        return self

    async def __aexit__(self, *exc) -> None:
        if self._held:
            self._held = False
            self.lock.release()

    async def send(self, data: bytes) -> None:
        if not data:
            return
        if self._held:
            self.writer.write(data)
            await self.writer.drain()
        else:
            await _send(self.writer, data, self.lock)
        if self.activity is not None:
            self.activity[0] = time.monotonic()


# ---------------------------------------------------------------------------
# The relay to the internal ASGI app
# ---------------------------------------------------------------------------

def _upstream_head(method: str, target: bytes, headers: dict, port: int,
                   body_len: int) -> bytes:
    """Build the request head for the internal app (HTTP/1.1, one request)."""
    host = headers.get(b"host") or f"127.0.0.1:{port}".encode()
    out = (f"{method} {target.decode('latin-1')} HTTP/1.1\r\n"
           f"Host: {host.decode('latin-1')}\r\n")
    for key, value in headers.items():
        if key in HOP_HEADERS:
            continue
        out += f"{key.decode('latin-1')}: {value.decode('latin-1')}\r\n"
    if b"content-length" not in headers and body_len:
        out += f"Content-Length: {body_len}\r\n"
    # One request per upstream connection: the client side is the one that
    # wants to stay reusable (Jive pools the thumbnail socket).
    return (out + "Connection: close\r\n\r\n").encode("latin-1")


def _parse_response_head(block: bytes) -> tuple[bytes, dict]:
    lines = block.split(b"\r\n")
    status_line = lines[0] if lines else b"HTTP/1.1 502 Bad Gateway"
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
    return status_line, headers


def _framing(headers: dict) -> str:
    """``chunked`` | ``length`` | ``close`` — how the upstream frames its body."""
    if b"chunked" in (headers.get(b"transfer-encoding") or b"").lower():
        return "chunked"
    if b"content-length" in headers:
        return "length"
    return "close"


def _client_head(status_line: bytes, headers: dict, framing: str,
                 length: int | None = None) -> bytes:
    """Re-frame the upstream head for the client.

    Content-Length/Transfer-Encoding are rebuilt for what we are about to
    send, the upstream's connection headers are dropped (the pooled client
    socket must stay open) and only a close-delimited body announces
    ``Connection: close``.
    """
    lines = [status_line]
    for key, value in headers.items():
        if key in HOP_HEADERS or key == b"content-length":
            continue
        lines.append(key + b": " + value)
    if framing == "chunked":
        lines.append(b"Transfer-Encoding: chunked")
    elif framing == "length":
        lines.append(b"Content-Length: " + str(length or 0).encode())
    else:
        lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"


async def _copy_request_body(up, reader: asyncio.StreamReader | None,
                             prefix: bytes, remaining: int) -> None:
    """Stream the client's request body to the upstream socket."""
    if prefix:
        up.write(prefix)
    if reader is None:
        await up.drain()
        return
    while remaining > 0:
        chunk = await reader.read(min(STREAM_CHUNK, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        up.write(chunk)
    await up.drain()


async def _next_chunk(up_reader: asyncio.StreamReader) -> tuple[bytes, bytes, bool]:
    """One chunk of a chunked body: ``(payload, raw, done)``.

    ``raw`` is the framing exactly as it arrived (size line + data + CRLF), so
    a passthrough can forward it unmodified, and ``done`` is True after the
    zero-length chunk and its trailers were consumed.
    """
    line = await up_reader.readline()
    if not line:
        raise asyncio.IncompleteReadError(line, 1)
    try:
        size = int(line.split(b";", 1)[0].strip() or b"0", 16)
    except ValueError as exc:
        raise ValueError(f"bad upstream chunk size {line[:20]!r}") from exc
    if size == 0:
        raw = line
        while True:
            trailer = await up_reader.readline()
            raw += trailer
            if trailer in (b"\r\n", b"\n", b""):
                break
        return b"", raw, True
    data = await up_reader.readexactly(size)
    await up_reader.readexactly(2)
    return data, line + data + b"\r\n", False


async def _pipe_chunked(up_reader: asyncio.StreamReader, emit: _Emitter,
                        verbatim: bool) -> None:
    """Forward a chunked body — framing verbatim, or de-framed payload only."""
    while True:
        payload, raw, done = await _next_chunk(up_reader)
        await emit.send(raw if verbatim else payload)
        if done:
            return


async def _relay_request(method: str, target: bytes, version: str, headers: dict,
                         client_reader: asyncio.StreamReader | None, writer,
                         port: int, *, body_prefix: bytes = b"",
                         body_remaining: int = 0, lock=None,
                         activity: list | None = None,
                         self_delimit: bool = False,
                         atomic: bool = False) -> bool:
    """Relay one non-Bayeux request to the internal web app (uvicorn).

    Perl serves its single HTTP port itself; this frontend answers only the
    Bayeux endpoint natively and hands everything else to the ASGI app that
    owns routing/JSON-RPC/static files. The relay is a STREAMING pipe in both
    directions: the request body is copied chunk by chunk and the response
    body is forwarded as it arrives. Reading a response to its end first would
    stall ``/stream.mp3`` (a whole album file, up to ~900 MB) and kill
    playback.

    Framing — Perl sets ``Transfer-Encoding: chunked`` only for HTTP/1.1
    clients (``Slim/Web/HTTP.pm:2838-2845``) and disables keep-alive for
    stream.mp3 (``:1191``), so:

    * upstream ``Content-Length`` → passed through, exactly N bytes copied;
    * upstream chunked + HTTP/1.1 client → chunk framing passed through
      verbatim (the client frames the body itself, socket stays reusable);
    * upstream chunked + older client → de-chunked and streamed, then closed;
    * upstream close-delimited → streamed to EOF, ``Connection: close``.

    ``self_delimit`` is for sockets that must serve the NEXT pipelined request
    (an open /meta/connect body, or Jive's HTTP/1.0 ``POST /jsonrpc.js``
    behind it): the answer is re-framed with a Content-Length, buffering up to
    ``SELF_DELIMIT_MAX`` and falling back to a close-delimited stream.

    Returns True when the client connection may be reused, False when the
    socket must be closed.
    """
    try:
        up_reader, up = await asyncio.open_connection("127.0.0.1", port,
                                                      limit=HEAD_MAX)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NativeCometd relay failed (%s %s): %s",
                       method, target[:40], exc)
        await _send(writer, b"HTTP/1.1 502 Bad Gateway\r\n"
                            b"Content-Length: 0\r\n\r\n", lock)
        return True
    body_len = len(body_prefix) + body_remaining
    async with _Emitter(writer, lock, atomic and not body_remaining,
                        activity) as emit:
        try:
            up.write(_upstream_head(method, target, headers, port, body_len))
            await _copy_request_body(up, client_reader, body_prefix,
                                     body_remaining)
            try:
                block = await up_reader.readuntil(b"\r\n\r\n")
                status_line, up_headers = _parse_response_head(block)
                # Skip interim (1xx) responses — we answered ``100 Continue``
                # to the client ourselves.
                while status_line.split(b" ")[1:2] and \
                        status_line.split(b" ")[1].startswith(b"1"):
                    block = await up_reader.readuntil(b"\r\n\r\n")
                    status_line, up_headers = _parse_response_head(block)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                    ConnectionError, OSError):
                logger.warning("NativeCometd relay: no upstream response for %s",
                               target[:60])
                await emit.send(b"HTTP/1.1 502 Bad Gateway\r\n"
                                b"Content-Length: 0\r\n\r\n")
                return True

            framing = _framing(up_headers)
            if method == "HEAD":
                # No body follows a HEAD: just re-frame and keep the socket.
                await emit.send(_client_head(status_line, up_headers, "length",
                                             _body_length(up_headers)))
                return True

            if framing == "length":
                total = _body_length(up_headers)
                await emit.send(_client_head(status_line, up_headers, "length",
                                             total))
                sent = 0
                while sent < total:
                    chunk = await up_reader.read(min(STREAM_CHUNK, total - sent))
                    if not chunk:
                        break
                    sent += len(chunk)
                    await emit.send(chunk)
                if sent != total:
                    # Truncated upstream body: the client cannot frame the rest
                    # of this response, so the socket is not reusable.
                    logger.warning("NativeCometd relay: %s truncated (%d/%d)",
                                   target[:40], sent, total)
                    return False
                return True

            if framing == "chunked":
                if self_delimit:
                    parts: list[bytes] = []
                    size_seen = 0
                    complete = False
                    while size_seen < SELF_DELIMIT_MAX:
                        payload, _raw, done = await _next_chunk(up_reader)
                        if done:
                            complete = True
                            break
                        parts.append(payload)
                        size_seen += len(payload)
                    if complete:
                        await emit.send(_client_head(status_line, up_headers,
                                                     "length", size_seen))
                        await emit.send(b"".join(parts))
                        return True
                    logger.info("NativeCometd relay: %s exceeds %d B, closing",
                                target[:40], SELF_DELIMIT_MAX)
                    await emit.send(_client_head(status_line, up_headers,
                                                 "close"))
                    await emit.send(b"".join(parts))
                    await _pipe_chunked(up_reader, emit, verbatim=False)
                    return False
                if version == "HTTP/1.1":
                    await emit.send(_client_head(status_line, up_headers,
                                                 "chunked"))
                    await _pipe_chunked(up_reader, emit, verbatim=True)
                    return True
                # Older client: chunked is not allowed for it (Perl switches on
                # `$response->request->protocol eq 'HTTP/1.1'`).
                await emit.send(_client_head(status_line, up_headers, "close"))
                await _pipe_chunked(up_reader, emit, verbatim=False)
                return False

            # No framing at all: read to EOF.
            await emit.send(_client_head(status_line, up_headers, "close"))
            while True:
                chunk = await up_reader.read(STREAM_CHUNK)
                if not chunk:
                    break
                await emit.send(chunk)
            return False
        except (ConnectionError, OSError, RuntimeError, ValueError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return False
        finally:
            try:
                up.close()
            except Exception:  # noqa: BLE001
                pass


async def _proxy_get(method: str, target: bytes, headers: dict, writer,
                     port: int, lock: asyncio.Lock | None = None) -> bool:
    """Relay a body-less request (GET/HEAD) to the internal web app.

    Thin wrapper over :func:`_relay_request` kept because the artwork GET is
    the hot path (Jive builds every artwork/static URL from the advertised
    port and pools the socket afterwards).
    """
    return await _relay_request(method, target, "HTTP/1.1", headers, None,
                                writer, port, lock=lock)


# ---------------------------------------------------------------------------
# Bayeux push
# ---------------------------------------------------------------------------

async def _push_events(manager, cid: str, writer: asyncio.StreamWriter,
                       lock: asyncio.Lock | None = None,
                       activity: list | None = None) -> None:
    """Push event batches into the open chunked stream as they arrive.

    Perl writes into a streaming /meta/connect response the MOMENT an event
    exists (``Manager::deliver_events``, Manager.pm:247-263 -> sendResponse
    Cometd.pm:661) and writes NOTHING while the client has no events: the
    streaming branch arms no timer at all (Cometd.pm:288-297).  A quiet
    streaming socket is therefore only ended by the HTTP keep-alive timeout
    (``KEEPALIVETIMEOUT => 75``, HTTP.pm:70/:2091-2099), after which the client
    re-polls (advice interval RETRY_DELAY 5000 ms, Cometd.pm:278) and so
    notices a dropped registration.  Measured on live 9.1.1: ack chunk at once,
    then no byte, EOF at 74.94 s.

    Python used to write an EMPTY batch (``[]``) every LONG_POLLING_TIMEOUT
    instead — a rate no Perl client ever sees — and left the socket open
    forever, so a client whose registration had been reaped (grace expired /
    autokill) kept a silent, open stream and never re-polled: the UI froze on
    the last title it had received, with no error and no reconnect.  Both are
    gone: silence is silence, and the socket is closed after Perl's
    KEEPALIVETIMEOUT.

    Every way out of this task is logged with the client id — a stream that
    ends must never do so silently.
    """
    try:
        while True:
            # A vanished client (meta/disconnect) makes wait_for_events
            # return [] immediately — without the existence check this
            # loop would spin at 100% CPU and freeze the whole server.
            if manager.get(cid) is None:
                logger.debug("Cometd push for %s: client dropped, stream ends",
                             cid)
                return
            started = time.monotonic()
            events = await manager.wait_for_events(cid,
                                                   timeout=KEEPALIVE_TIMEOUT)
            if manager.get(cid) is None:
                logger.debug("Cometd push for %s: client dropped, stream ends",
                             cid)
                return
            if not events:
                # No event within KEEPALIVETIMEOUT. If the socket was written
                # to in the meantime (a relayed artwork/JSON-RPC response
                # shares it) the keep-alive clock restarted, exactly like
                # Perl's timer re-arm after every send (:2091-2099).
                if activity is not None and activity[0] > started:
                    continue
                logger.info(
                    "Cometd streaming connect idle %.0fs -> closing stream for "
                    "%s (Perl HTTP.pm:70 KEEPALIVETIMEOUT=%d; client re-polls "
                    "after advice interval %d ms)",
                    KEEPALIVE_TIMEOUT, cid, int(KEEPALIVE_TIMEOUT),
                    RETRY_DELAY_MS)
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            data = json.dumps(events).encode("utf-8")
            await _send(writer, f"{len(data):x}\r\n".encode() + data + b"\r\n",
                        lock)
            if activity is not None:
                activity[0] = time.monotonic()
    except (ConnectionError, OSError, RuntimeError) as exc:
        # The peer is gone (or the transport is broken): the stream ends here,
        # and the manager only hears about it from the connection's finally —
        # so log the reason instead of vanishing in silence.
        logger.warning("Cometd push to %s failed (%r) — stream ends", cid, exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        # A bug in the push path must not leave a silently dead-but-open
        # stream: say what happened and let the response end so the client
        # re-polls (Perl closes the socket and the client reconnects).
        logger.warning("Cometd push to %s aborted: %r — stream ends",
                       cid, exc, exc_info=True)
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The connection handler
# ---------------------------------------------------------------------------

async def _handle_connection(manager, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter, internal_port: int) -> None:
    # The connection's identity. Only the connection that processed a client's
    # /meta/connect becomes that client's owner; this connection may remove a
    # client only while it still owns it. Perl does the same: Cometd.pm:286
    # calls $manager->register_connection ONLY in the /meta/(re)connect branch,
    # and webCloseHandler (Cometd.pm:1003) drops a client only when the lost
    # connection is that registered connection. A client spreads its POSTs over
    # several sockets (HTTP long-polling, Comet.lua:184 "2 pools, 1 for chunked
    # responses and 1 for requests"), so a handshake/subscribe/request POST
    # ending — the normal case — must never erase the client.
    owner = object()
    conn_cids: set[str] = set()
    # Serialises every write to this socket. While the streaming
    # /meta/connect response is open the client keeps PIPELINING requests
    # onto the same socket (Perl HTTP.pm:2065-2072: "Check for additional
    # pipelined GET or HEAD requests we need to process / We also support
    # pipelined cometd requets, even though this is against the HTTP RFC"),
    # so the push task and the request loop write into one transport.
    write_lock = asyncio.Lock()
    # "Last byte written to this socket" clock (monotonic). Every write path —
    # the connect ack, a pushed batch and a relayed response — refreshes it, so
    # the streaming keep-alive close (Perl HTTP.pm:70/:2091-2099) only fires
    # after 75 s without ANY traffic on the socket.
    activity = [time.monotonic()]
    try:
        while True:
            request = await _read_http_head(reader)
            if request is None:
                # EOF (the client closed) or an unparsable request line (logged
                # in _read_http_head). Never a silent end: say which side ended
                # it and which clients this socket carried.
                logger.debug("NativeCometd connection end: peer closed / EOF "
                             "(%d client(s) on this socket: %s)",
                             len(conn_cids), ",".join(sorted(conn_cids)) or "-")
                break
            method = request["method"]
            target = request["target"]
            version = request["version"]
            headers = request["headers"]
            # A client that announces ``Expect: 100-continue`` waits for our
            # interim response before sending the body — answer it, otherwise
            # the body never arrives and both sides wait (curl and browsers do
            # this for every POST above ~1 kB, e.g. the settings pages).
            if _expects_continue(headers):
                await _send(writer, b"HTTP/1.1 100 Continue\r\n\r\n")
            prefix, remaining = await _peek_request_body(reader, headers)

            if prefix[:1] != b"[":
                # Not a Bayeux batch. Jive builds EVERY server URL from the
                # advertised port — artwork (/music/<album>/cover_*.jpg),
                # /html/..., the SPA, /stream.mp3, POST /jsonrpc.js — and it
                # does NOT follow redirects, so the bytes must come from here:
                # stream the request through to the ASGI app and its answer
                # back. GET/HEAD have no body and stay reusable; a POST from an
                # older client is re-framed with a Content-Length so the same
                # socket can carry the next pipelined call (Jive sends its
                # JSON-RPC as HTTP/1.0 and pipelines it behind the open
                # /meta/connect body).
                reusable = await _relay_request(
                    method, target, version, headers, reader, writer,
                    internal_port, body_prefix=prefix,
                    body_remaining=remaining,
                    activity=activity,
                    self_delimit=method not in ("GET", "HEAD")
                    and version != "HTTP/1.1")
                if not reusable:
                    logger.info("NativeCometd relay says connection close "
                                "(%s %s) — closing socket",
                                method, target[:40])
                    break
                continue

            try:
                body = prefix + await _read_rest(reader, remaining)
            except _TooLarge as exc:
                logger.warning("NativeCometd: %s — closing", exc)
                break
            if not isinstance(body, bytes):
                break

            # /cometd batch
            try:
                messages = json.loads(body.decode("utf-8", errors="replace"))
                if not isinstance(messages, list):
                    messages = [messages]
            except Exception:
                messages = []
            logger.info("NativeCometd Body: %s", ",".join(
                m.get("channel", "?") for m in messages if isinstance(m, dict)))
            # DEBUG: dump the full request/subscription payloads so we
            # can see exactly what the controller apps send.
            for _m in messages:
                if not isinstance(_m, dict):
                    continue
                _ch = _m.get("channel", "")
                _data = _m.get("data")
                if isinstance(_data, dict) and (_data.get("request") or _data.get("subscription")):
                    logger.debug("Cometd PAYLOAD %s: %s", _ch, _data)

            replies = await manager.handle_messages(messages)
            # Remember every client this socket touched: handshake ids
            # arrive in the replies, explicit ones in the messages, and
            # Orange Squeeze's cid-less /slim/* in their response channel.
            # They are only TOUCHED (last_seen/autokill window) — never
            # owned — unless this batch carried their /meta/connect.
            for m in messages:
                if not isinstance(m, dict):
                    continue
                c = m.get("clientId", "")
                if not c:
                    c = _client_id_from_channel(
                        (m.get("data") or {}).get("response", ""))
                if c:
                    conn_cids.add(c)
            for r in replies:
                if isinstance(r, dict) and r.get("clientId"):
                    conn_cids.add(r["clientId"])
            for c in conn_cids:
                manager.touch(c)
            connect_msgs = [m for m in messages if isinstance(m, dict)
                            and is_connect_channel(m.get("channel"))]

            if connect_msgs:
                msg = connect_msgs[0]
                cid = msg.get("clientId", "")
                if has_invalid_client_advice(replies):
                    # Perl Cometd.pm:225-244: a message whose clientId the
                    # manager no longer knows is answered with the
                    # re-handshake advice ALONE and the message loop is
                    # abandoned (``last``) *before* the /meta/(re)connect
                    # branch — so the response carries no connect ack and
                    # no chunked stream is opened (``Transfer-Encoding:
                    # chunked`` is set at Cometd.pm:292, after the check).
                    # Appending a fabricated ``successful: true``
                    # /meta/connect told jive it was still connected while
                    # the server had no record of it, so it never
                    # re-handshaked and never re-registered its
                    # subscriptions (frozen Now-Playing after a restart /
                    # the LONG_POLLING_AUTOKILL reaper dropped it).
                    payload = json.dumps(replies).encode("utf-8")
                    writer.write(b"HTTP/1.1 200 OK\r\n"
                                 b"Content-Type: application/json\r\n"
                                 b"Cache-Control: no-cache\r\n"
                                 + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                                 + payload)
                    await writer.drain()
                    break
                # This connection processed the client's /meta/connect: it
                # now OWNS the client and is the only connection allowed to
                # drop it (Perl register_connection on (re)connect). A
                # later connect on a new socket overwrites the owner, so
                # this connection's close leaves the reconnected client
                # alone (remove_if_owner).
                if cid:
                    conn_cids.add(cid)
                    manager.register_connection(cid, owner)
                # Perl Cometd.pm:267 decides the transport of THIS connect —
                # ``connectionType eq 'streaming'`` — and stores it on the
                # connection (:295 streaming, :300 long-polling).  Everything
                # but the literal 'streaming' is long-polling (that ternary).
                streaming = msg.get("connectionType") == "streaming"
                manager.set_transport(cid, "streaming" if streaming
                                      else "long-polling")
                logger.info("NativeCometd connect: cid=%s type=%s (POST hatte "
                            "%d Nachrichten: %s)", cid,
                            "streaming" if streaming else "long-polling",
                            len(messages),
                            ",".join(m.get("channel", "?") for m in messages))
                connect_reply = connect_ack(msg, cid)
                # Per Cometd.pm:269-271 the /meta/(re)connect ack is stored in
                # the response's ``first_event`` slot — "We want the
                # /meta/(re)connect response to always be the first event sent
                # in the response".  We used to append it AFTER the batch acks
                # (``replies + [ack]``), so Perl and we ordered the SAME two
                # frames differently: live Perl 9.1.1 192.168.1.90 answers
                # SqueezeClient's ``[connect, /meta/subscribe /<cid>/**]`` with
                #   [{"advice":{"interval":5000},"channel":"/meta/connect",…},
                #    {"channel":"/meta/subscribe","id":3,…}]
                # — connect first.  Measured against our :9000 the order was
                # reversed; a Bayeux client that reads messages[0] as its
                # connect answer must not see the subscribe ack there.
                # handle_messages() deliberately does NOT answer
                # /meta/connect, so this is the one and only connect ack.
                first = [connect_reply] + list(replies)
                if not streaming:
                    # Perl Cometd.pm:298-328 — the long-polling branch. The
                    # transport is recorded (:300), the hold time is
                    # LONG_POLLING_TIMEOUT unless the client overrides it
                    # (:302-307) and a poll with events already pending answers
                    # at once (:309-313). The reply is a plain Content-Length
                    # response — ``Transfer-Encoding: chunked`` is set for
                    # STREAMING only (:288-292) — and the socket stays reusable
                    # for the client's next pipelined poll; the connection is
                    # unregistered from the manager right after the response
                    # (sendHTTPResponse, :682-696), which re-arms Perl's
                    # LONG_POLLING_AUTOKILL window (:691-695) instead of the
                    # 10 s disconnectClient grace.
                    events = await manager.wait_for_events(
                        cid, timeout=connect_timeout(msg))
                    manager.touch(cid)
                    first.extend(events)
                    payload = json.dumps(first).encode("utf-8")
                    await _send(
                        writer,
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Cache-Control: no-cache\r\n"
                        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                        + payload,
                        write_lock)
                    manager.unregister_connection(cid, owner)
                    continue
                events = await manager.wait_for_events(cid, timeout=0)
                first.extend(events)
                # Chunked transfer: the app's HttpResponseInputStream
                # requires Transfer-Encoding: chunked (or Content-Length).
                # Written in ONE locked section: the push task must not
                # splice a chunk into the response head.
                chunk = json.dumps(first).encode("utf-8")
                await _send(
                    writer,
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                    + f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n",
                    write_lock)
                # The stream stays open: mark the client as actively
                # connected so the idle reaper (Perl LONG_POLLING_AUTOKILL)
                # never drops a silently streaming client. The push task
                # dies with the connection (cancelled in the finally
                # below); the request loop keeps running.
                manager.connection_open(cid)
                push_task = asyncio.create_task(
                    _push_events(manager, cid, writer, write_lock, activity))
                stream_cid = cid
                try:
                    while True:
                        nxt = await _read_http_head(reader)
                        if nxt is None:
                            logger.debug(
                                "NativeCometd streaming loop end for %s: peer "
                                "closed / EOF", stream_cid or "-")
                            break
                        nmethod = nxt["method"]
                        ntarget = nxt["target"]
                        nversion = nxt["version"]
                        nheaders = nxt["headers"]
                        if _expects_continue(nheaders):
                            await _send(writer,
                                        b"HTTP/1.1 100 Continue\r\n\r\n",
                                        write_lock)
                        prefix, remaining = await _peek_request_body(
                            reader, nheaders)
                        if prefix[:1] != b"[":
                            # Jive builds EVERY server URL from the
                            # advertised port and pipelines the artwork/
                            # icon GET (and its HTTP/1.0 ``/jsonrpc.js``
                            # POSTs) onto the very socket that carries the
                            # open streaming response — which Perl
                            # explicitly supports (HTTP.pm:2065-2072, see
                            # write_lock above). Reading ``nxt["body"]``
                            # unconditionally raised KeyError here; it
                            # escaped the except tuple below, the handler
                            # died with its socket and the ``finally``
                            # removed the client. SqueezePlay hits this as
                            # soon as Albums or Favorites is opened (those
                            # screens fetch the menu icons/artwork): live
                            # 2026-09-14 09:46:05 logs "redirecting
                            # /html/images/artists_40x40_m.png" immediately
                            # followed by "Cometd connection close ->
                            # removing client 14ff96e66d084eb6", and the app
                            # reports "kann sich nicht mit dem Server
                            # verbinden". Relay the independent response
                            # between the chunks of the open one (Perl
                            # addHTTPResponse, HTTP.pm:1895-1960) as a
                            # self-delimiting message, so the framing is
                            # never in doubt.
                            logger.info("NativeCometd pipelined %s %s (relay)",
                                        nmethod,
                                        ntarget.decode("ascii", "replace")[:50])
                            reusable = await _relay_request(
                                nmethod, ntarget, nversion, nheaders, reader,
                                writer, internal_port, body_prefix=prefix,
                                body_remaining=remaining, lock=write_lock,
                                activity=activity,
                                self_delimit=True, atomic=True)
                            if not reusable:
                                logger.info("NativeCometd relay wants the "
                                            "socket closed (%s %s) — ending "
                                            "stream of %s",
                                            nmethod, ntarget[:40], stream_cid)
                                break
                            continue
                        nb = prefix + await _read_rest(reader, remaining)
                        # Every POST on this client's socket counts as
                        # activity: Perl re-arms the autokill timer on
                        # each new poll (Cometd.pm:693). Without this a
                        # client that keeps sending /jsonrpc.js requests
                        # but no /cometd poll looked idle and was reaped
                        # after LONG_POLLING_AUTOKILL (live 2026-09-12).
                        if stream_cid:
                            manager.touch(stream_cid)
                        try:
                            nmsgs = json.loads(
                                nb.decode("utf-8", errors="replace"))
                            if not isinstance(nmsgs, list):
                                nmsgs = [nmsgs]
                        except Exception:
                            nmsgs = []
                        logger.info("NativeCometd Folge-POST: %s",
                                    ",".join(m.get("channel", "?")
                                             for m in nmsgs))
                        nreplies = await manager.handle_messages(nmsgs)
                        nconnect = [m for m in nmsgs
                                    if isinstance(m, dict)
                                    and is_connect_channel(m.get("channel"))]
                        if nconnect:
                            # New connect while streaming — answer as
                            # a proper CHUNK (the connection body is
                            # Transfer-Encoding: chunked; a bare JSON
                            # write would corrupt the frame). Keep the
                            # acks from the same batch so a pipelined
                            # subscribe/request is not left
                            # unacknowledged. handle_messages() does
                            # not answer /meta/connect itself, so this
                            # ack is the only one; result events flow
                            # via push_task (exactly once).
                            nc = nconnect[0]
                            if has_invalid_client_advice(nreplies):
                                # Perl Cometd.pm:228-244 again: the
                                # re-handshake advice goes out alone —
                                # no connect ack, and the stream ends
                                # so the client must handshake anew.
                                nbad = json.dumps(nreplies).encode("utf-8")
                                await _send(
                                    writer,
                                    f"{len(nbad):x}\r\n".encode()
                                    + nbad + b"\r\n", write_lock)
                                break
                            new_cid = nc.get("clientId", stream_cid)
                            if new_cid and new_cid != stream_cid:
                                manager.connection_closed(stream_cid)
                                manager.connection_open(new_cid)
                                stream_cid = new_cid
                            # A connect in this batch (re)registers this
                            # connection as the client's owner — Perl's
                            # register_connection, only reached from
                            # /meta/(re)connect (Cometd.pm:286).
                            if stream_cid:
                                conn_cids.add(stream_cid)
                                manager.register_connection(stream_cid, owner)
                            # Perl stores the (re)connect answer in the
                            # response's ``first_event`` slot, so it always
                            # leads the batch (Cometd.pm:269-271); the shared
                            # helper keeps this frame byte-identical to the
                            # ASGI path's.
                            payload = [connect_ack(nc, new_cid or stream_cid)] \
                                + list(nreplies)
                            nchunk = json.dumps(payload).encode("utf-8")
                            await _send(
                                writer,
                                f"{len(nchunk):x}\r\n".encode()
                                + nchunk + b"\r\n", write_lock)
                        else:
                            # Non-connect POSTs (slim/request
                            # publishes): the acks go into the SAME
                            # open chunked body — as a chunk. Writing
                            # a complete HTTP response here (status
                            # line + Content-Length) spliced a second
                            # HTTP message into the streaming body and
                            # corrupted the framing: the client reads
                            # this body with its HttpResponseInputStream
                            # and cannot tell where that fake response
                            # ends. Result events flow via push_task.
                            nack = json.dumps(nreplies).encode("utf-8")
                            await _send(
                                writer,
                                f"{len(nack):x}\r\n".encode()
                                + nack + b"\r\n", write_lock)
                finally:
                    push_task.cancel()
                    try:
                        await push_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    manager.connection_closed(stream_cid)
                    # The streaming connection is gone: client closed it,
                    # app was killed, or the body was truncated — no
                    # /meta/disconnect ever arrives. Do NOT remove the
                    # client here: Perl's webCloseHandler only UNREGISTERS
                    # the connection and arms ``disconnectClient`` for
                    # RETRY_DELAY * 2 (Cometd.pm:1010-1014), and the next
                    # /meta/(re)connect kills that timer (:283) — which is
                    # what lets a client that merely re-pools its stream
                    # socket keep its subscriptions and the events queued
                    # for it. Removing it threw all of that away and the
                    # app re-handshaked, losing the request it had just
                    # sent (the ASGI path documents and fixes the same
                    # thing, web/app.py:282-292 "the app then re-handshaked
                    # and lost the pending request"). A client that never
                    # comes back is still reaped: the grace expires and
                    # the idle sweep (LONG_POLLING_AUTOKILL) drops it.
                    manager.release_connection(stream_cid, owner)
                break
            else:
                # no connect: reply with acks AND any queued events.
                # The result of a slim/request / a subscription's
                # initial payload is delivered EXACTLY ONCE, in this
                # reply (the manager clears the queue per client, like
                # Perl's get_pending_events()). A duplicate would make
                # Jive log "event we aren't subscribed to" and can
                # leave its subscriptions pending instead of
                # re-registering serverstatus/playerstatus after a
                # reconnect.
                events = []
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    cid2 = m.get("clientId", "")
                    if not cid2:
                        resp = (m.get("data") or {}).get("response", "")
                        cid2 = _client_id_from_channel(resp)
                    if not cid2:
                        continue
                    # NEVER drain the queue of a client that still has a
                    # live connection. Perl routes a finished request
                    # result by the transport of the connection that
                    # carried it (Cometd.pm:584-589): unless that is
                    # long-polling it calls ``$manager->deliver_events``,
                    # which writes into the client's REGISTERED connection
                    # (Manager.pm:247-263), and only a client WITHOUT a
                    # connection gets pending events appended to this
                    # reply (``sendResponse`` -> ``get_pending_events``,
                    # Cometd.pm:645). Jive keeps its streaming
                    # /meta/connect on a SECOND socket ("2 pools, 1 for
                    # chunked responses and 1 for requests",
                    # Comet.lua:184) and reads its results off the connect
                    # channel, so draining here STOLE the result out of
                    # the open stream: the request looked unanswered and
                    # the app re-handshaked (live 2026-09-14 12:40-12:41,
                    # "kann sich nicht mit dem Server verbinden"). The
                    # ASGI path guards this with the same check
                    # (web/app.py:447-450).
                    if manager.has_live_connection(cid2):
                        continue
                    events.extend(
                        await manager.wait_for_events(cid2, timeout=0))
                payload = json.dumps(replies + events).encode("utf-8")
                writer.write(b"HTTP/1.1 200 OK\r\n"
                             b"Content-Type: application/json\r\n"
                             + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                             + payload)
                await writer.drain()
    except asyncio.CancelledError:
        # Server shutdown / task cancel — not an error, but never silent.
        logger.info("NativeCometd connection cancelled by the server "
                    "(clients on this socket: %s)",
                    ",".join(sorted(conn_cids)) or "-")
        raise
    except (ConnectionError, OSError, RuntimeError,
            asyncio.IncompleteReadError, EOFError) as exc:
        # The socket died under us (reset, truncated body, ...). This used to
        # be swallowed silently — a client loss must always leave a trace that
        # names it, so a frozen UI can be correlated with the transport end.
        logger.info("NativeCometd connection lost: %r (clients on this "
                    "socket: %s)", exc, ",".join(sorted(conn_cids)) or "-")
    finally:
        # The socket is gone. ``conn_cids`` holds every client this connection
        # touched, but only a client this connection CONNECTED still has it as
        # owner — remove_if_owner is a no-op for all the rest, so a
        # handshake-/subscribe-/request-POST socket closing never erases a
        # client whose /meta/connect lives elsewhere (HTTP long-polling's
        # response/request pools, Comet.lua:184). It also leaves a client
        # alone whose reconnect on a newer socket already claimed the id
        # (Perl webCloseHandler's "is this the current connection?" check,
        # Cometd.pm:1003). Handshake-only clients are reaped by the idle
        # autokill (LONG_POLLING_AUTOKILL) / /meta/disconnect.
        for cid in conn_cids:
            manager.remove_if_owner(cid, owner)
        # Always release the socket — also for a truncated body / aborted
        # connection, which used to escape the handler (IncompleteReadError
        # is neither a ConnectionError nor an OSError) and leak the
        # transport until GC.
        try:
            writer.close()
        except Exception:
            pass


async def start_cometd_server(manager, host: str, port: int,
                              internal_port: int = 9001) -> asyncio.Server:
    """Start the native frontend: Bayeux + a streaming relay to the ASGI app.

    ``port`` is the ONE public HTTP port (announced via TLV/beacon/
    serverstatus); ``internal_port`` is where uvicorn listens for the relayed
    traffic (loopback only).
    """
    server = await asyncio.start_server(
        lambda r, w: _handle_connection(manager, r, w, internal_port),
        host, port)
    logger.info("Native HTTP frontend on %s:%d (internal ASGI relay -> 127.0.0.1:%d)",
                host, port, internal_port)
    return server
