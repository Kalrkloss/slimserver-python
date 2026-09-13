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

from lyrion.web.cometd import (
    LONG_POLL_TIMEOUT,
    _client_id_from_channel,
    _http_timestamp,
    connect_advice,
    has_invalid_client_advice,
)


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
        # Jive builds EVERY server URL from the advertised (Cometd) port:
        # artwork (/music/<album>/cover_*.jpg), /html/... and /stream.mp3.
        # Only the /cometd POSTs belong here, so answer any other request
        # with a 302 redirect to the real web port — otherwise SqueezePlay
        # gets nothing for its cover requests and the lists spin forever.
        parts = line.split()
        method = parts[0] if parts else b""
        target = parts[1] if len(parts) > 1 else b"/"
        headers: dict[bytes, bytes] = {}
        while True:
            hline = await reader.readline()
            if hline in (b"\r\n", b"\n", b""):
                break
            if b":" in hline:
                k, _, v = hline.partition(b":")
                headers[k.strip().lower()] = v.strip()
        if method in (b"GET", b"HEAD"):
            logger.info("NativeCometd: redirecting %s to the web port",
                        target.decode("ascii", "replace")[:60])
            return {"method": method.decode(), "target": target,
                    "headers": headers}
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
    if not length:
        return {"headers": headers, "body": b""}
    try:
        body = await reader.readexactly(length)
    except (asyncio.IncompleteReadError, EOFError, ConnectionError, OSError):
        # Truncated/aborted body: the peer vanished mid-POST. Treat it as
        # EOF so the caller unwinds and closes the socket. Without this
        # asyncio.IncompleteReadError escaped the handler (it is not a
        # ConnectionError) and the transport stayed open until GC.
        logger.info("NativeCometd: abgebrochener Body (Content-Length %d)", length)
        return None
    return {"headers": headers, "body": body}


async def _push_events(manager, cid: str, writer: asyncio.StreamWriter) -> None:
    """Push event batches into the open chunked stream as they arrive.

    Perl answers a /meta/connect after at most LONG_POLLING_TIMEOUT
    (Cometd.pm:48 = 60 s, Cometd.pm:318-322 "Waiting N seconds on
    long-poll connection"). We mirror that: with nothing to send we emit an
    empty batch so the poll completes and the client re-polls, which also
    refreshes its autokill timer (Cometd.pm:693). Waiting forever left the
    client's request unanswered — its session was reaped after
    LONG_POLLING_AUTOKILL while the socket stayed open, and Now-Playing
    stopped updating (live 2026-09-12).
    """
    try:
        while True:
            # A vanished client (meta/disconnect) makes wait_for_events
            # return [] immediately — without the existence check this
            # loop would spin at 100% CPU and freeze the whole server.
            if manager.get(cid) is None:
                break
            events = await manager.wait_for_events(cid, timeout=LONG_POLL_TIMEOUT)
            if not events and manager.get(cid) is None:
                break
            data = json.dumps(events).encode("utf-8")
            writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            await writer.drain()
    except (ConnectionError, OSError, RuntimeError):
        pass


async def _proxy_get(method: str, target: bytes, headers: dict,
                     writer, port: int) -> None:
    """Serve a non-Cometd request by proxying it to the web app (port 9000).

    Jive builds artwork/image/static URLs from the advertised (Cometd) port
    and does NOT follow redirects — the 302 we tried produced no follow-up
    request, so the cover slots stayed empty. Forward the raw request and
    pipe the upstream response (status line, headers, body) back verbatim,
    then close the client connection.
    """
    try:
        reader, up = await asyncio.open_connection("127.0.0.1", port)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NativeCometd GET proxy failed (%s): %s", target[:40], exc)
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        return
    try:
        req = (
            f"{method} {target.decode('ascii', 'replace')} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Connection: close\r\n"
        )
        for key in (b"accept", b"accept-encoding", b"if-modified-since",
                    b"user-agent", b"range"):
            if key in headers:
                req += f"{key.decode()}: {headers[key].decode('latin-1')}\r\n"
        up.write(req.encode("latin-1") + b"\r\n")
        await up.drain()
        # Forward the UPSTREAM header block, but make the close explicit:
        # Jive pools the thumbnail connection and, when we close it without
        # saying so, the next request on that socket dies as
        # '_getArtworkThumbSink(...) error: keep-alive timeout' (live).
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(4096)
            if not chunk:
                break
            head += chunk
            if len(head) > 65536:      # runaway header block — give up
                break
        block, _, rest = head.partition(b"\r\n\r\n")
        lines = block.split(b"\r\n")
        status_line = lines[0] if lines else b"HTTP/1.1 502 Bad Gateway"
        # Jive POOLS the thumbnail connection and drops every queued
        # request when we close it (live symptom: '_getArtworkThumbSink(
        # /music/3079/cover_40x40_m) error: keep-alive timeout' and only
        # placeholder icons). Keep the client side reusable: drop the
        # upstream keep-alive/close headers, keep Content-Length or
        # Transfer-Encoding so the client can frame the body itself.
        keep = [ln for ln in lines[1:]
                if b":" in ln and ln.split(b":", 1)[0].strip().lower()
                not in (b"connection", b"keep-alive")]
        out = b"\r\n".join([status_line, *keep, b"", b""])
        writer.write(out)
        if rest:
            writer.write(rest)
        await writer.drain()
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError, RuntimeError,
            asyncio.IncompleteReadError):
        pass
    finally:
        try:
            up.close()
        except Exception:  # noqa: BLE001
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
    try:
        while True:
            request = await _read_http_request(reader)
            if request is None:
                break
            if request.get("method") in ("GET", "HEAD"):
                # Jive resolves every non-Cometd URL against the advertised
                # port — artwork (/music/<album>/cover_*.jpg), /html/... —
                # and it does NOT follow redirects, so the bytes must come
                # from here: proxy the request to the real web app.
                await _proxy_get(
                    request["method"], request["target"],
                    request["headers"], writer, web_port,
                )
                # Keep the connection: Jive pools it and reuses it for the
                # next thumbnail. Only the upstream side closes per request.
                continue
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
                                and m.get("channel") == "/meta/connect"]

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
                    logger.info("NativeCometd connect: cid=%s (POST hatte %d Nachrichten: %s)",
                                cid, len(messages),
                                ",".join(m.get("channel", "?") for m in messages))
                    connect_ack = {
                        "channel": "/meta/connect", "successful": True,
                        "clientId": cid, "id": msg.get("id", ""),
                        # Perl Cometd.pm:274-280: the ack is stamped and its
                        # advice carries RETRY_DELAY (5000 ms) for a streaming
                        # connect, 0 for long-polling (Cometd.pm:45, :278).
                        "timestamp": _http_timestamp(),
                        "advice": connect_advice(msg.get("connectionType", "")),
                    }
                    # handle_messages() deliberately does NOT answer
                    # /meta/connect, so this is the one and only connect ack.
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
                    # The stream stays open: mark the client as actively
                    # connected so the idle reaper (Perl LONG_POLLING_AUTOKILL)
                    # never drops a silently streaming client. The push task
                    # dies with the connection (cancelled in the finally
                    # below); the request loop keeps running.
                    manager.connection_open(cid)
                    push_task = asyncio.create_task(
                        _push_events(manager, cid, writer))
                    stream_cid = cid
                    try:
                        while True:
                            nxt = await _read_http_request(reader)
                            if nxt is None:
                                break
                            nb = nxt["body"]
                            # Every POST on this client's socket counts as
                            # activity: Perl re-arms the autokill timer on
                            # each new poll (Cometd.pm:693). Without this a
                            # client that keeps sending /jsonrpc.js requests
                            # but no /cometd poll looked idle and was reaped
                            # after LONG_POLLING_AUTOKILL (live 2026-09-12).
                            if stream_cid:
                                manager.touch(stream_cid)
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
                                        writer.write(f"{len(nbad):x}\r\n".encode()
                                                     + nbad + b"\r\n")
                                        await writer.drain()
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
                                    payload = list(nreplies) + [{
                                        "channel": "/meta/connect",
                                        "successful": True,
                                        "clientId": nc.get("clientId", ""),
                                        "id": nc.get("id", ""),
                                        "timestamp": _http_timestamp(),
                                        "advice": connect_advice(
                                            nc.get("connectionType", ""))}]
                                    nchunk = json.dumps(payload).encode("utf-8")
                                    writer.write(f"{len(nchunk):x}\r\n".encode()
                                                 + nchunk + b"\r\n")
                                    await writer.drain()
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
                                    writer.write(f"{len(nack):x}\r\n".encode()
                                                 + nack + b"\r\n")
                                    await writer.drain()
                    finally:
                        push_task.cancel()
                        try:
                            await push_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        manager.connection_closed(stream_cid)
                        # The streaming connection is gone: client closed it,
                        # app was killed, or the body was truncated — no
                        # /meta/disconnect ever arrives. Drop the client here,
                        # but only while this connection is still its owner
                        # (a newer connect on another socket may have taken
                        # over). Perl does the same from webCloseHandler ->
                        # disconnectClient (Cometd.pm:1003). A client that
                        # never connected on this socket is only reaped by its
                        # own connect connection, /meta/disconnect, or the idle
                        # autokill (LONG_POLLING_AUTOKILL).
                        manager.remove_if_owner(stream_cid, owner)
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
                        if cid2:
                            events.extend(
                                await manager.wait_for_events(cid2, timeout=0))
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
    except (ConnectionError, OSError, RuntimeError, asyncio.CancelledError,
            asyncio.IncompleteReadError, EOFError):
        pass
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
                              web_port: int = 9000) -> asyncio.Server:
    """Start the native Cometd streaming server."""
    server = await asyncio.start_server(
        lambda r, w: _handle_connection(manager, r, w, web_port),
        host, port)
    logger.info("Native Cometd streaming server on %s:%d (web proxy %d)",
                host, port, web_port)
    return server
