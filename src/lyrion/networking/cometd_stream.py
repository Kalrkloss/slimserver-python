"""Native Cometd streaming server (SqueezePlay / Orange Squeeze style).

ASGI/uvicorn cannot serve the Bayeux streaming protocol the Jive apps
use: they pipeline HTTP POSTs over ONE socket while the server keeps
the /meta/connect response open and pushes events into it. uvicorn
processes one request per connection and would block the pipelined
requests. This asyncio TCP server speaks that protocol natively:

- parses sequential HTTP POSTs on one connection
- /cometd: handles handshake/subscribe/request via CometdManager,
  keeps the response open and pushes event batches into it while
  continuing to read the next requests
- /jsonrpc.js: proxies to the main HTTP server (port 9000)

Start on a dedicated port (e.g. 9080); the SlimProto TLV discovery
advertises it as the JSON port so the apps connect here.
"""
from __future__ import annotations

import asyncio
import json
import logging


logger = logging.getLogger(__name__)

MAX_BODY = 2 * 1024 * 1024


async def _read_http_request(reader: asyncio.StreamReader) -> dict | None:
    """Read one HTTP request (head + body). Returns dict or None on EOF."""
    try:
        line = await reader.readline()
    except (ConnectionError, OSError):
        return None
    if not line:
        return None
    if not line.startswith(b"POST"):
        logger.info("NativeCometd: unerwartete Zeile: %.60r", line[:60])
        # Only POSTs expected; drain and ignore others.
        while line and line not in (b"\r\n", b"\n"):
            line = await reader.readline()
        return None
    logger.info("NativeCometd Request: %.70s", line.decode("ascii", "replace").strip())
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get(b"content-length", b"0") or 0)
    if length > MAX_BODY:
        return None
    body = await reader.readexactly(length) if length else b""
    return {"headers": headers, "body": body}


async def _push_events(manager, cid: str, writer: asyncio.StreamWriter) -> None:
    """Push event batches into the open chunked stream as they arrive."""
    try:
        while True:
            events = await manager.wait_for_events(cid, timeout=None)
            if events:
                data = json.dumps(events).encode("utf-8")
                writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                await writer.drain()
    except (ConnectionError, OSError, RuntimeError):
        pass


async def _proxy_jsonrpc(body: bytes, port: int) -> tuple[bytes, bytes]:
    """Proxy a /jsonrpc.js POST to the main web server (port 9000)."""
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = (f"POST /jsonrpc.js HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: application/json\r\n"
               f"Content-Length: {len(body)}\r\n"
               f"Connection: close\r\n\r\n").encode() + body
        writer.write(req)
        await writer.drain()
        resp = b""
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            resp += chunk
        writer.close()
        head, _, raw = resp.partition(b"\r\n\r\n")
        # uvicorn sends chunked — the app's JSON parser cannot handle it.
        if b"chunked" in head.lower():
            payload = _dechunk(raw)
        else:
            payload = raw
        # Content-Length is REQUIRED: the app reads until the declared
        # body length (the connection stays open for the next POST).
        return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\n\r\n".encode()), payload
    except Exception as exc:
        err = json.dumps({"jsonrpc": "2.0", "error": str(exc), "id": None}).encode()
        return b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\n\r\n", err


def _dechunk(data: bytes) -> bytes:
    out = b""
    rest = data
    while rest:
        line, _, rest = rest.partition(b"\r\n")
        try:
            size = int(line, 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        rest = rest[size:]
        if rest.startswith(b"\r\n"):
            rest = rest[2:]
    return out


async def _handle_connection(manager, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter, web_port: int) -> None:
    try:
        while True:
            request = await _read_http_request(reader)
            if request is None:
                break
            path = (request["headers"].get(b"host", b"") and b"")
            body = request["body"]

            if body[:1] == b"[":
                # /cometd batch
                try:
                    messages = json.loads(body.decode("utf-8", errors="replace"))
                    if not isinstance(messages, list):
                        messages = [messages]
                except Exception:
                    messages = []
                logger.info("NativeCometd Body: %s", ",".join(
                    m.get("channel", "?") for m in messages if isinstance(m, dict)))

                replies = await manager.handle_messages(messages)
                connect_msgs = [m for m in messages if isinstance(m, dict)
                                and m.get("channel") == "/meta/connect"]

                if connect_msgs:
                    msg = connect_msgs[0]
                    cid = msg.get("clientId", "")
                    logger.info("NativeCometd connect: cid=%s (POST hatte %d Nachrichten: %s)",
                                cid, len(messages),
                                ",".join(m.get("channel", "?") for m in messages))
                    connect_ack = {
                        "channel": "/meta/connect", "successful": True,
                        "clientId": cid, "id": msg.get("id", ""),
                        "advice": {"reconnect": "retry", "interval": 0,
                                   "timeout": 25},
                    }
                    first = list(replies) + [connect_ack]
                    events = await manager.wait_for_events(cid, timeout=0)
                    first.extend(events)
                    # Chunked transfer: the app's HttpResponseInputStream
                    # requires Transfer-Encoding: chunked (or Content-Length).
                    writer.write(b"HTTP/1.1 200 OK\r\n"
                                 b"Content-Type: application/json\r\n"
                                 b"Transfer-Encoding: chunked\r\n\r\n")
                    chunk = json.dumps(first).encode("utf-8")
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    await writer.drain()
                    # Keep the stream open and push events, while the
                    # loop continues reading the pipelined requests.
                    push_task = asyncio.create_task(
                        _push_events(manager, cid, writer))
                    try:
                        # continue the request loop (this function loops
                        # back to read the next POST)
                        pass
                    finally:
                        pass
                    # The request loop keeps running; the push task must
                    # survive until the connection closes.
                    try:
                        while True:
                            nxt = await _read_http_request(reader)
                            if nxt is None:
                                break
                            nb = nxt["body"]
                            if nb[:1] == b"[":
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
                                            and m.get("channel") == "/meta/connect"]
                                if nconnect:
                                    # new connect while streaming — answer
                                    # inline (events flow via push_task)
                                    nc = nconnect[0]
                                    writer.write(json.dumps([
                                        {"channel": "/meta/connect",
                                         "successful": True,
                                         "clientId": nc.get("clientId", ""),
                                         "id": nc.get("id", "")}]).encode())
                                    await writer.drain()
                                else:
                                    # Non-connect POSTs (slim/request
                                    # publishes): reply with the ACKS
                                    # inline (the OkHttp caller waits for
                                    # a Content-Length response); the
                                    # result events flow via push_task.
                                    nack = json.dumps(nreplies).encode("utf-8")
                                    writer.write(
                                        b"HTTP/1.1 200 OK\r\n"
                                        b"Content-Type: application/json\r\n"
                                        + f"Content-Length: {len(nack)}\r\n\r\n".encode()
                                        + nack)
                                    await writer.drain()
                    finally:
                        push_task.cancel()
                    break
                else:
                    # no connect: reply with acks AND any queued events.
                    # Jive clients (SqueezeCtrl) expect request results
                    # in the POST reply; SqueezeClient reads publish
                    # responses via body.string() (checks only
                    # messages[0].successful) and receives the events
                    # via the OPEN STREAM — so peek (don't clear) the
                    # queue so the push_task delivers them too.
                    events = []
                    for m in messages:
                        if not isinstance(m, dict):
                            continue
                        cid2 = m.get("clientId", "")
                        if not cid2:
                            resp = (m.get("data") or {}).get("response", "")
                            if resp.startswith("/"):
                                cid2 = resp.split("/")[1]
                        if cid2:
                            events.extend(await manager.peek_events(cid2))
                    payload = json.dumps(replies + events).encode("utf-8")
                    writer.write(b"HTTP/1.1 200 OK\r\n"
                                 b"Content-Type: application/json\r\n"
                                 + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                                 + payload)
                    await writer.drain()
            else:
                # /jsonrpc.js proxy
                head, payload = await _proxy_jsonrpc(body, web_port)
                writer.write(head)
                writer.write(payload)
                await writer.drain()
    except (ConnectionError, OSError, RuntimeError, asyncio.CancelledError):
        pass
    try:
        writer.close()
    except Exception:
        pass


async def start_cometd_server(manager, host: str, port: int,
                              web_port: int = 9000) -> asyncio.Server:
    """Start the native Cometd streaming server."""
    server = await asyncio.start_server(
        lambda r, w: _handle_connection(manager, r, w, web_port),
        host, port)
    logger.info("Native Cometd streaming server on %s:%d (web proxy %d)",
                host, port, web_port)
    return server
