"""
ASGI/anyio web application for Pyrion Music Server.

Uses uvicorn as the HTTP server library, wrapped in anyio for consistency
with the project's async/await model.
"""
from __future__ import annotations

from pathlib import Path

import logging
from typing import Callable, Optional

import uvicorn

from .api import JSONRPCAPI, WebAPIHandler
from .cometd import (
    LONG_POLL_TIMEOUT,
    STREAMING_HOLD_WINDOW,
    CometdManager,
    _client_id_from_channel,
    connect_ack,
    connect_timeout,
    has_invalid_client_advice,
)

logger = logging.getLogger(__name__)


def _authorize(scope: dict, authorize: bool, username: str, password: str) -> bool:
    """Return True if the request is authorized.

    When ``authorize`` is False (the default), every request passes.
    Otherwise the request must carry a Basic-Auth header matching
    ``username``/``password`` (constant-time comparison). Mirrors the
    Perl LMS ``Slim::Web::HTTP`` ``authorize``/``checkAuthorization`` path.
    """
    import base64
    import hmac

    if not authorize:
        return True
    headers = {k.decode("latin1").lower(): v.decode("latin1")
               for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
    except Exception:
        return False
    user, _, pwd = decoded.partition(":")
    return hmac.compare_digest(user, username) and hmac.compare_digest(pwd, password)


def _auth_config() -> tuple[bool, str, str]:
    """Read (authorize, username, password) from the active config.

    Defensive boolean coercion: the preference store may return the raw
    string ``"0"``/``"1"`` for a bool pref, and ``"0"`` is truthy.
    """
    try:
        from lyrion.config import get_config
        cfg = get_config()
        raw = str(cfg.get("authorize", 0) or 0)
        authorize = raw.lower() in ("1", "true", "yes", "on")
        username = str(cfg.get("username", "") or "")
        password = str(cfg.get("password", "") or "")
        return authorize, username, password
    except Exception:
        return False, "", ""


async def _respond_401(send) -> None:
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"text/plain"),
            (b"content-length", b"12"),
            (b"www-authenticate", b'Basic realm="Pyrion Music Server"'),
        ],
    })
    await send({"type": "http.response.body", "body": b"Unauthorized"})


async def _send_cometd_reply(send, replies: list[dict]) -> None:
    """One complete JSON reply (Content-Length framing, no chunked body)."""
    import json as _json

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"Content-Type", b"application/json"),
                    (b"Cache-Control", b"no-cache")],
    })
    await send({"type": "http.response.body",
                "body": _json.dumps(replies).encode("utf-8")})


async def _watch_disconnect(receive) -> None:
    """Resolve once uvicorn reports the peer gone (``http.disconnect``).

    uvicorn does NOT cancel a held-open ASGI task when the socket dies — it
    signals the death on the ``receive()`` channel instead. Nothing else in
    the streaming handler reads that channel after the request body, so
    without this watcher a vanished client kept its ``CometdManager`` entry
    (and the idle reaper spared it, ``connections > 0``) forever. Perl learns
    the same fact from the socket close handler (``webCloseHandler``,
    ``Slim/Web/Cometd.pm:1003`` -> ``disconnectClient``).
    """
    while True:
        event = await receive()
        if event.get("type") == "http.disconnect":
            return


async def _handle_streaming_connect(
    cometd: CometdManager,
    cid: str,
    msg: dict,
    replies: list[dict],
    send,
    receive,
) -> None:
    """Streaming /meta/connect: reply immediately (acks + any queued
    events), then hold the response open and push events as chunks.

    SqueezeClient expects the connect + subscribe acks within 5 seconds
    and then reads the body as a stream of JSON arrays.

    Perl reference: Cometd.pm:264-297 keeps the response chunked
    (``Transfer-Encoding: chunked`` at :292) and ``Manager::deliver_events``
    (Manager.pm:247-263) writes every event into the client's registered
    connection the moment it is produced — the stream is woken BY the event,
    it never waits for a timeout. ``CometdManager.push`` does exactly that
    here (append + ``notify.set()``). ``advice.timeout`` is only honoured by
    the *long-polling* branch (Cometd.pm:302-306); a streaming connect keeps
    ``LONG_POLLING_TIMEOUT`` as its advertised advice and is held for
    ``STREAMING_HOLD_WINDOW`` of silence (see that constant: Perl holds it
    forever, uvicorn cannot — it defers pipelined requests until the response
    completes, so the client's queued POSTs would never be served).
    """
    import asyncio as _asyncio
    import json as _json

    # Perl Cometd.pm:271-280 stores this frame as ``first_event`` with
    # id/channel/clientId/successful/timestamp and
    # ``advice => { interval => $streaming ? RETRY_DELAY : 0 }``. Built by the
    # shared helper so both transports (this one and cometd_stream.py) emit
    # byte-identical acks — the ASGI path used to advertise
    # ``advice.timeout: 60`` (60 ms!) and ``interval: 0`` for a streaming
    # connect, while the native stream already carried 60000/5000.
    ack = connect_ack(msg, cid)
    # Perl puts the (re)connect reply FIRST in the response (Cometd.pm:279-292,
    # "first_event"). We keep the shipped order — batch acks, then the connect
    # ack — because the Android/libcometd clients could not be exercised in
    # this suite; the deviation is documented, not silently changed (P3-4).
    first = list(replies) + [ack]
    events = await cometd.wait_for_events(cid, timeout=0)
    first.extend(events)

    owner = object()
    cometd.register_connection(cid, owner)
    cometd.set_transport(cid, "streaming")
    cometd.connection_open(cid)
    # The ASGI transport never sees a /meta/disconnect: a client that dies
    # is reported as ``http.disconnect`` (Perl's webCloseHandler ->
    # disconnectClient, Cometd.pm:1003).
    gone = _asyncio.ensure_future(_watch_disconnect(receive))
    # True once the connect ack reached the client (see the first send below).
    delivered = False
    # True once the connection was unregistered (window closed cleanly).
    released = False
    try:
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"Content-Type", b"application/json"),
                        (b"Cache-Control", b"no-cache")],
        })
        await send({"type": "http.response.body",
                    "body": _json.dumps(first).encode("utf-8"),
                    "more_body": True})
        # The connect ack reached the client: from here on the connection is
        # a real one and its loss is handled like Perl's webCloseHandler (grace
        # period, see below). If the very first send fails, the client never
        # saw the ack and must re-handshake anyway — it is dropped instead.
        delivered = True

        # Keep the stream open and push event batches as they arrive. The
        # uvicorn path (port 9000) serves clients that connect directly to
        # :9000 (Squeezer manual address) — closing after 0.6 s broke their
        # connection. Orange Squeeze (pipelined POSTs over one socket) is
        # served by the native server on 9080.
        #
        # The wait for events races the disconnect watcher: a push wakes the
        # wait (Perl Manager::deliver_events -> sendResponse), a vanished peer
        # ends it at once (http.disconnect).
        #
        # The response is NOT held open forever: uvicorn queues HTTP pipelined
        # requests until the CURRENT response completes (h11_impl.py:191-197
        # pauses the read flow on h11.PAUSED, on_response_complete starts the
        # queued cycle at :278; httptools_impl.py:291-297 does the same), while
        # Perl keeps reading the socket and answers such a POST at once
        # (measured: 0.8 s, stream stays open — Slim/Web/HTTP.pm:277 +
        # addHTTPResponse :1895-1960 via Cometd.pm:734). A POST the client sent
        # behind this stream therefore stayed unanswered and its own network
        # deadline fired: libcometd gives a NON-connect message exactly
        # maxNetworkDelay = 10000 ms (html/material/html/lib/libcometd.js:1268,
        # :380-389 adds advice.timeout only for metaConnect) — the 10 s after
        # which the app re-handshaked and lost the request. So: after
        # STREAMING_HOLD_WINDOW of silence (RETRY_DELAY, Cometd.pm:45/:278 —
        # the interval the ack above tells the client to wait before
        # reconnecting) the empty batch is written as the LAST body event and
        # the response completes, which lets uvicorn serve the queued POSTs.
        # Every pushed event restarts the window, so a busy stream stays open.
        closed_cleanly = False
        while not gone.done():
            # A vanished client (meta/disconnect) makes wait_for_events
            # return [] immediately — without the existence check the
            # loop would spin at 100% CPU and freeze the whole server.
            if cometd.get(cid) is None:
                break
            events_task = _asyncio.ensure_future(
                cometd.wait_for_events(cid, timeout=STREAMING_HOLD_WINDOW))
            try:
                done, _pending = await _asyncio.wait(
                    {events_task, gone},
                    return_when=_asyncio.FIRST_COMPLETED)
            finally:
                if not events_task.done():
                    # Cancelling is lossless: the queued events stay in
                    # ``client.events`` for the next call.
                    events_task.cancel()
                    try:
                        await events_task
                    except (_asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            if gone.done() or cometd.get(cid) is None:
                break
            events = events_task.result() if events_task in done else []
            if not events:
                # A whole hold window passed with nothing to say: emit the
                # empty batch the native stream emits (Perl answers an empty
                # sendResponse the same way) and terminate the response. The
                # write is also a liveness probe — a half-dead socket raises
                # here — and the poll re-arms the client's autokill window
                # (``sendHTTPResponse``, Cometd.pm:687-695).
                cometd.touch(cid)
                # Release the connection BEFORE the terminating body event:
                # uvicorn starts the request it queued behind this response as
                # soon as the response is complete, and that POST's own result
                # must therefore find no live connection — Perl unregisters the
                # connection with the finished response as well
                # (sendHTTPResponse, Cometd.pm:682-696) and then answers the
                # queued POST from ``get_pending_events`` (Cometd.pm:645-648).
                cometd.connection_closed(cid)
                if delivered:
                    cometd.release_connection(cid, owner)
                else:
                    cometd.remove_if_owner(cid, owner)
                released = True
                await send({"type": "http.response.body",
                            "body": _json.dumps(events).encode("utf-8"),
                            "more_body": False})
                closed_cleanly = True
                break
            await send({"type": "http.response.body",
                        "body": _json.dumps(events).encode("utf-8"),
                        "more_body": True})
    except Exception:
        pass
    finally:
        # ``gone.cancel()`` first: the watcher is parked in ``receive()`` and
        # would otherwise keep the task alive after this handler returns.
        gone.cancel()
        try:
            await gone
        except (_asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if not released:
            cometd.connection_closed(cid)
            if delivered:
                # The connection is gone (peer vanished): Perl's webCloseHandler
                # (Cometd.pm:1002-1015) unregisters the connection and only arms
                # ``disconnectClient`` for RETRY_DELAY * 2 (10 s), which the
                # client's next /meta/connect kills (:283). Removing the client
                # here threw away its subscriptions and everything queued for it
                # whenever a stream ended — the app then re-handshook and lost
                # the pending request.
                cometd.release_connection(cid, owner)
            else:
                # The connect ack never reached the client: it has no live
                # connection and has to handshake again, so there is nothing to
                # keep for a reconnect (pinned by
                # tests/test_cometd_push.py::test_asgi_streaming_connect_removes_client_on_abort).
                cometd.remove_if_owner(cid, owner)
        if not closed_cleanly:
            try:
                await send({"type": "http.response.body", "body": b"",
                            "more_body": False})
            except Exception:
                pass


async def _handle_cometd(cometd: CometdManager, path: str, receive, send) -> None:
    """Handle a POST /cometd batch (Jive controller protocol).

    Replies to handshake/subscribe/request immediately; /meta/connect
    long-polls (held open until events arrive or the timeout expires).
    Streaming /meta/connect keeps the response open until the client is gone
    and drops it (cometd_stream.py does the same on its socket). The
    handshake-only, non-connect and dead long-polling clients the ASGI path
    never sees close for are reaped by CometdManager's idle autokill
    (Perl LONG_POLLING_AUTOKILL).
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
    # cid -> owner of every /meta/connect this POST holds. ONLY these clients
    # may be dropped when the response cannot be delivered: a handshake/
    # subscribe/request POST that aborts must not erase a client whose connect
    # is served by another request/socket (HTTP long-polling, Comet.lua:184).
    poll_owners: dict[str, object] = {}

    try:
        # Perl Cometd.pm:228-244: a message whose clientId the manager does not
        # know is answered with the re-handshake advice ALONE and the message
        # loop is abandoned (``last``) BEFORE the /meta/(re)connect branch — so
        # the reply carries no connect ack and no chunked stream is opened
        # (``Transfer-Encoding: chunked`` is set at Cometd.pm:292, after the
        # check). Appending a fabricated ``successful: true`` /meta/connect next
        # to the advice told jive/libcometd it was still connected while the
        # server had no record of it, so it never re-handshaked and never
        # re-registered its subscriptions — and, on this path, the client also
        # got the meaningless ``advice.timeout: 60`` (60 ms) frame. Mirrors
        # cometd_stream.py:331-352.
        if connect_msgs and has_invalid_client_advice(replies):
            await _send_cometd_reply(send, replies)
            return

        if connect_msgs:
            # /meta/connect: streaming clients (SqueezeClient, Material) need
            # the reply IMMEDIATELY (5s timeout) and then a held-open stream
            # that pushes events as chunks.
            for msg in connect_msgs:
                cid = msg.get("clientId", "")
                # Perl Cometd.pm:200-212: "No clientId found" — a message
                # without a clientId is not processed at all (only the first
                # clientId of a packet is used, :164-180). Never fabricate a
                # connect ack for one: its clientId would be empty.
                if not cid:
                    continue
                # Perl Cometd.pm:267 decides the transport of THIS connect —
                # ``connectionType eq 'streaming'`` — and stores it on the
                # connection (:295 streaming, :300 long-polling). A later
                # /meta/(re)connect overwrites it, so a client that switches
                # transport (SqueezeClient: long-polling after a stream died)
                # is routed by its newest connect. Everything but the literal
                # 'streaming' is long-polling (Perl's ternary), which is what
                # ``set_transport`` records for the routing below.
                if msg.get("connectionType") == "streaming":
                    await _handle_streaming_connect(cometd, cid, msg, replies,
                                                    send, receive)
                    return
                # long-polling: this POST IS the client's connect connection —
                # it owns the client until a newer connect takes over (Perl
                # registers the connection with the manager only in the
                # /meta/(re)connect branch, Cometd.pm:286).
                owner = object()
                cometd.register_connection(cid, owner)
                # Perl Cometd.pm:300 records the transport as soon as the
                # long-polling connect is accepted; it decides where a later
                # request result goes (Cometd.pm:584-589 — its OWN POST
                # response, see CometdManager.deliver_result).
                cometd.set_transport(cid, "long-polling")
                poll_owners[cid] = owner
                # Hold until events arrive or the timeout expires. Perl lets
                # the CLIENT pick the hold time (Cometd.pm:302-306,
                # ``advice.timeout`` in ms; 0 = answer now) and answers
                # immediately when events are already pending (:309-312, which
                # wait_for_events does by returning the queue first).
                events = await cometd.wait_for_events(
                    cid, timeout=connect_timeout(msg))
                # Perl Cometd.pm:271-280 (first_event): the same shared ack the
                # native stream writes — interval 0 for long-polling,
                # timeout 60000 ms, RFC1123 timestamp.
                replies.append(connect_ack(msg, cid))
                replies.extend(events)
                # A completed poll restarts Perl's autokill window.
                cometd.touch(cid)
        else:
            # No connect in this batch. The result of a /slim/request or
            # /slim/subscribe was already routed by ``handle_messages`` —
            # ``CometdManager.deliver_result`` — according to the client's
            # transport (Perl Cometd.pm:584-589 / :466-475):
            #   * long-polling -> it is IN ``replies`` and rides in this POST's
            #     response, exactly like Perl's ``push @{$events}, $result``
            #     (:587/:468), so a long-polling client (SqueezeClient) sees
            #     its result the moment it reads the reply and never waits for
            #     a poll cycle;
            #   * streaming -> it was handed to the client's registered
            #     connection (``$manager->deliver_events``, :589/:475) and the
            #     open stream frames it (the app reads its results off the
            #     connect channel).
            # What is left to drain here are events that were queued while no
            # connection was registered — Perl's ``get_pending_events`` is
            # appended to every response in ``sendResponse`` (Cometd.pm:645).
            # Only a client with NO registered connection gets them in this
            # reply; with one, ``deliver_events`` already wrote them there.
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                cid = msg.get("clientId", "")
                if not cid:
                    # Orange Squeeze's /slim/* carry no clientId — the id is
                    # the first segment of the response channel (P3-2 rule).
                    cid = _client_id_from_channel(
                        (msg.get("data") or {}).get("response", ""))
                if not cid:
                    continue
                if cometd.has_live_connection(cid):
                    continue
                events = await cometd.wait_for_events(cid, timeout=0)
                replies.extend(events)

        await _send_cometd_reply(send, replies)
    except Exception as exc:  # noqa: BLE001
        # The response could not be written — the client is gone from THIS
        # request. Only a connection that processed the client's /meta/connect
        # may remove it (Perl webCloseHandler, Cometd.pm:1003); a failed
        # handshake/subscribe/request POST leaves the client alone, because its
        # connect is normally served by another request/socket. (Streaming
        # connects clean up in their own finally.)
        logger.info("Cometd response aborted (%s) — dropping connect client(s) %s",
                    exc, sorted(poll_owners))
        for cid, owner in poll_owners.items():
            cometd.remove_if_owner(cid, owner)


_MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}


import re as _re
from collections import OrderedDict

_COVER_CACHE_MAX = 512
_cover_cache: "OrderedDict[tuple, tuple[bytes, str]]" = OrderedDict()


def _cover_cache_get(key):
    """LRU read for served covers (returns (bytes, mime) or None)."""
    hit = _cover_cache.get(key)
    if hit is None:
        return None
    _cover_cache.move_to_end(key)
    return hit


def _cover_cache_put(key, data: bytes, mime) -> None:
    """LRU write; evicts the oldest entry beyond ``_COVER_CACHE_MAX``."""
    _cover_cache[key] = (data, mime or "image/jpeg")
    _cover_cache.move_to_end(key)
    while len(_cover_cache) > _COVER_CACHE_MAX:
        _cover_cache.popitem(last=False)


_COVER_PATH_RE = _re.compile(
    r"^/music/(\d+|current)/(?:"
    r"cover\.(?:jpg|png)"                          # plain: cover.jpg
    # LMS sized form. Jive omits the extension when it fetches a browser
    # thumbnail (fetchArtwork without imgFormat) — SqueezePlay asked for
    # '/music/2441/cover_40x40_m' and our strict '.jpg' rule answered 404.
    r"|cover_(\d+)x(\d+)(?:_[a-z])?(?:\.(?:jpg|png))?"
    r")$"
)


def _parse_cover_path(path: str) -> tuple[int | str, tuple[int, int] | None] | None:
    """Album id (or the literal ``"current"``) + optional (w, h) from an URL.

    ``/music/current/cover.jpg?player=<mac>`` is the form Material Skin's
    Now-Playing widget uses (``html/material/html/js/currentcover.js:102``:
    "Use players current cover ... Need to add extra params so that the URL is
    different between tracks"). Perl special-cases it in
    ``Slim/Web/Graphics.pm:155-172``: the id becomes the currently playing
    track's coverid. We answered 404 before, so the browser kept the previous
    cover image and never showed a placeholder for cover-less tracks.

    Perl's ImageProxy accepts ``/music/<albumid>/cover.jpg`` and the
    size-encoded form Jive/SqueezePlay use from their ``artworkspec``
    (``cover_40x40_m.jpg``; the trailing ``_m``/``_f`` is the
    crop flag). Accepting only the plain form made every cover request
    from SqueezePlay 404 — the album list then showed endless spinners.
    """
    m = _COVER_PATH_RE.match(path)
    if not m:
        return None
    raw_id = m.group(1)
    album_id: int | str = raw_id if raw_id == "current" else int(raw_id)
    if m.group(2) and m.group(3):
        return album_id, (int(m.group(2)), int(m.group(3)))
    return album_id, None


def _resize_cover(data: bytes, size: tuple[int, int]) -> bytes:
    """Downscale a cover with Pillow (Lanczos); returns the original data
    unchanged when Pillow is unavailable or the image is already small."""
    try:
        import io as _io

        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a project dependency
        return data
    try:
        with Image.open(_io.BytesIO(data)) as im:
            if im.width <= size[0] and im.height <= size[1]:
                return data
            im = im.convert("RGB")
            resample = getattr(
                getattr(Image, "Resampling", Image), "LANCZOS", None
            ) or getattr(Image, "LANCZOS", 1)
            im.thumbnail(size, resample)
            out = _io.BytesIO()
            im.save(out, format="JPEG", quality=85)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - never break artwork on a bad file
        return data


# Wurzel der ausgelieferten html/-Dateien (setzt create_app); enthaelt den
# generischen Cover-Platzhalter, den Perl fuer Alben ohne Bild ausliefert.
_STATIC_ROOT: Path | None = None


def _set_static_root(path: str | Path | None) -> None:
    global _STATIC_ROOT
    _STATIC_ROOT = Path(path) if path else None


def _static_root() -> Path | None:
    if _STATIC_ROOT is not None:
        return _STATIC_ROOT
    # Wie __main__: Paket-, Checkout- oder CWD-Layout.
    try:
        import lyrion as _pkg

        pkg = Path(_pkg.__file__).resolve().parent
        for cand in (pkg / "html", pkg.parent.parent / "html", Path.cwd() / "html"):
            if cand.is_dir():
                return cand
    except Exception:  # noqa: BLE001
        pass
    return None


def _placeholder_cover(size: tuple[int, int] | None = None) -> tuple[bytes, str] | None:
    """Perl's generic cover — ``html/images/cover.png``.

    Perl reports this image wherever a track/album has no artwork
    (``Slim/Control/XMLBrowser.pm:1603``, ``Commands.pm:2151``) and its
    artwork endpoint answers it with HTTP 200 instead of 404 (live probe
    2026-09-12: ``/music/2/cover.jpg`` -> 200 image/png 13113 B). Material
    Skin then shows the generic symbol instead of keeping the old cover.
    """
    root = _static_root()
    if root is None:
        return None
    # Zwei Konventionen im Projekt: static_dir IST das html-Verzeichnis
    # (__main__.py:233-238) bzw. ist sein Elternverzeichnis
    # (server.py:205-209 haengt selbst "html/" an). Beide abdecken.
    candidates = (root / "images" / "cover.png", root / "html" / "images" / "cover.png")
    p = next((c for c in candidates if c.is_file()), None)
    if p is None:
        return None
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if size is not None:
        # The sized form is re-encoded as JPEG, matching Perl's answer for
        # /music/current/cover_40x40_m.jpg (live: 200 image/jpeg).
        data = _resize_cover(data, size)
        return data, "image/jpeg"
    return data, "image/png"      # unsized: Perl serves cover.png as-is


async def _send_placeholder_cover(send, size: tuple[int, int] | None = None) -> None:
    """Send the generic cover (200) — or 404 if even it is missing."""
    res = _placeholder_cover(size)
    if res is None:
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"Content-Type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"no artwork"})
        return
    data, mime = res
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"Content-Type", mime.encode()),
            (b"Cache-Control", b"max-age=86400"),
        ],
    })
    await send({"type": "http.response.body", "body": data})


def _current_album_id(scope: dict) -> int | None:
    """Album id for ``/music/current/...`` (Perl Graphics.pm:155-172).

    Resolves the player from the ``player`` query param (Material sends the
    MAC) and returns the album of its current track. Material also appends
    ``album_id``/``album``/``artist``/``year`` as hints; ``album_id`` is used
    when the server has no current track.
    """
    from urllib.parse import parse_qs

    qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", "replace"))
    mac = (qs.get("player") or [""])[0]
    try:
        from lyrion.player.manager import PlayerManager

        pm = PlayerManager()
        player = pm.get_player(mac) if mac else None
        if player is None:
            players = pm.get_all_players()
            player = players[0] if players else None
        track_id = getattr(player, "current_track_id", None) if player else None
        if track_id:
            album_id = _album_id_for_track(int(track_id))
            if album_id:
                return album_id
    except Exception:  # noqa: BLE001 — nie die Cover-Auslieferung sprengen
        pass
    hint = (qs.get("album_id") or [""])[0]
    if hint.isdigit():
        return int(hint)
    return None


def _album_id_for_track(track_id: int) -> int | None:
    """Album id of a track (read-only; eigener Loop wie _load_sync_factory)."""
    def _run() -> int | None:
        import asyncio as _aio

        async def _q() -> int | None:
            from lyrion.database.sqlite_helper import db_session
            from lyrion.database.schema import Track

            async with db_session() as session:
                track = await session.get(Track, track_id)
                return int(track.album_id) if track and track.album_id else None

        return _aio.run(_q())

    try:
        return _run()
    except Exception:  # noqa: BLE001
        return None


async def _serve_album_cover(album_id: int, send, size: tuple[int, int] | None = None) -> None:
    """Serve the cover image stored in Album.artwork for /music/<id>/cover.

    Reads the image file in a thread (SMB reads block) and streams it with
    long-lived cache headers — covers never change for an album id. Falls
    back to 404 when the album has no artwork or the file vanished.

    Results are memoised in-process: SqueezePlay requests a whole album
    list worth of thumbnails at once, and without the cache every one
    re-read the file over SMB and re-ran Pillow — slow enough that the
    client logged ``_getArtworkThumbSink(...) error: keep-alive timeout``
    and showed only the one cover that won the race.
    """
    import asyncio as _asyncio

    cache_key = (album_id, size)
    cached = _cover_cache_get(cache_key)
    if cached is not None:
        data, mime = cached
    else:
        try:
            loop = _asyncio.get_running_loop()
            data, mime = await loop.run_in_executor(None, _load_sync_factory(album_id))
        except Exception:
            data, mime = None, None
        if data and size is not None:
            data = await _asyncio.get_running_loop().run_in_executor(
                None, _resize_cover, data, size
            )
            mime = "image/jpeg"
        if data:
            _cover_cache_put(cache_key, data, mime)
    if not data:
        # Perl answers the generic cover with 200 here (live: /music/2/cover.jpg
        # -> 200 image/png 13113 B), not a 404 — Material Skin relies on it to
        # switch away from the previous album's cover.
        await _send_placeholder_cover(send, size)
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
        _set_static_root(static_dir)

    async def app(scope: dict, receive, send) -> None:
        """ASGI application entry point."""
        method = scope.get("method", "GET")
        path = scope.get("path", "/")

        # Authorization gate (Perl 'authorize' pref → Basic-auth required).
        authorize, username, password = _auth_config()
        if not _authorize(scope, authorize, username, password):
            await _respond_401(send)
            return

        # Web-Settings-Seiten: /settings/<bereich>/<seite>.html.
        # Perl: Dispatch über den pageFunction-Regexp-Hash
        # (Slim/Web/HTTP.pm:1160-1176) in den Basis-Handler
        # Slim/Web/Settings.pm:135-287 (pref_<name> → set()).
        from .settings import handle_settings_request, is_settings_path
        if is_settings_path(path):
            await handle_settings_request(scope, receive, send)
            return

        # Audio streaming gets a dedicated path (needs chunked body sends).
        if path.startswith("/stream") and method == "GET":
            from .stream import stream_track
            await stream_track(scope, receive, send)
            return

        # Album cover art (LMS convention): /music/<album_id>/cover.jpg and
        # the size-encoded form Jive asks for from its artworkspec
        # (/music/<album_id>/cover_40x40_m.jpg).
        if path.startswith("/music/") and method == "GET":
            parsed = _parse_cover_path(path)
            if parsed is not None:
                cover_id, cover_size = parsed
                if cover_id == "current":
                    # /music/current/cover.jpg?player=<mac> — cover of the
                    # current track, else the generic image (Perl Graphics.pm
                    # :155-172 plus the /html/images/cover.png fallback).
                    cover_id = _current_album_id(scope)
                    if cover_id is None:
                        await _send_placeholder_cover(send, cover_size)
                        return
                await _serve_album_cover(cover_id, send, cover_size)
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
