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
    it never waits for a timeout. Perl arms NO hold timer for the streaming
    branch at all: it only KILLS one (:283 ``killTimers($clid,
    \\&disconnectClient)``) and otherwise just sets the chunked framing and
    the transport marker (:288-297); only the long-polling branch arms a
    timer (:302-325). The socket simply stays in the read set
    (``processHTTP``, Slim/Web/HTTP.pm:277) and every response is written on
    its own (``addHTTPResponse``, HTTP.pm:1895-1960, from Cometd.pm:734), so
    a pipelined POST behind an open stream is answered while the stream
    stays open (live measurement against LMS 9.1.1: 0.8 s).

    So this response is held open for as long as the peer is there — exactly
    like Perl — and is ended ONLY by facts Perl also reacts to: the peer is
    gone (``http.disconnect``, Perl's webCloseHandler Cometd.pm:1002-1015)
    or the client was dropped (``/meta/disconnect``, idle autokill). No
    Python-side timer writes an empty batch or closes the stream. This used
    to be different (a 5 s silence window closed the response) because
    uvicorn defers HTTP pipelined requests until the current response
    completes (h11_impl.py:191-197, :278; httptools_impl.py:291-297) — but
    since the one-port rework (commit 54a201363) the PUBLIC port is served
    by the native frontend, which reads pipelined requests itself and relays
    every one of them on its OWN upstream connection
    (networking/cometd_stream.py); the ASGI stream is reached only over that
    internal relay, so the uvicorn limitation no longer reaches a client.
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
        # uvicorn path (internal :9001) serves clients relayed by the native
        # frontend on the public port (:9000). Orange Squeeze (pipelined
        # POSTs over one socket) is served by the native frontend itself
        # (`public_http_port`, 9000; the separate 9080 native port was
        # retired in commit 54a201363).
        #
        # The wait for events races the disconnect watcher: a push wakes the
        # wait (Perl Manager::deliver_events -> sendResponse), a vanished peer
        # ends it at once (http.disconnect), and a dropped client wakes it too
        # (CometdManager.remove sets the client's notify). Perl holds this
        # response for as long as the socket lives: its streaming branch arms
        # no hold timer at all (Cometd.pm:288-297 only sets chunked + the
        # transport marker, the sole timer there is the one :283 kills) and
        # only ends it when the connection goes away (webCloseHandler,
        # Cometd.pm:1002-1015). Nothing here writes an empty batch or
        # terminates the body on a timeout — there is no timeout.
        while not gone.done():
            # A vanished client (meta/disconnect) wakes the wait below with an
            # empty queue; without the existence check the loop would spin at
            # 100% CPU and freeze the whole server.
            if cometd.get(cid) is None:
                break
            events_task = _asyncio.ensure_future(
                cometd.wait_for_events(cid, timeout=None))
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
                # A spurious wake (a removal that raced the existence check
                # above): keep holding, exactly as Perl keeps the response
                # open and silent.
                continue
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
        # The handler is leaving for exactly two reasons: the peer is gone
        # (``http.disconnect``) or the client was dropped. Both are Perl's
        # webCloseHandler (Cometd.pm:1002-1015): it unregisters the connection
        # and only arms ``disconnectClient`` for RETRY_DELAY * 2 (10 s), which
        # the client's next /meta/connect kills (:283) — a client between two
        # connections keeps its subscriptions and its queued events. Removing
        # the client here threw away its subscriptions and everything queued
        # for it whenever a stream ended — the app then re-handshook and lost
        # the pending request.
        cometd.connection_closed(cid)
        if delivered:
            cometd.release_connection(cid, owner)
        else:
            # The connect ack never reached the client: it has no live
            # connection and has to handshake again, so there is nothing to
            # keep for a reconnect (pinned by
            # tests/test_cometd_push.py::test_asgi_streaming_connect_removes_client_on_abort).
            cometd.remove_if_owner(cid, owner)
        # Terminate the (still open) chunked body. A vanished peer makes this
        # a no-op; it is what ends the response for a client that was dropped
        # while the stream was open.
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


#: Wurzel der ausgelieferten html/-Dateien (setzt create_app); enthaelt den
#: generischen Cover-Platzhalter, den Perl fuer Alben ohne Bild ausliefert.
_STATIC_ROOT: Path | None = None

#: Jive/LMS size-encoded skin images (``genres_40x40_m.png``).
_STATIC_SIZE_RE = _re.compile(
    r"^(?P<base>.+?)_(?P<w>\d+)x(?P<h>\d+)(?:_[a-z])?(?P<ext>\.(?:png|jpe?g|gif))$")


def _static_path_variants(path: str) -> list[str]:
    """Candidate file paths (relative to the static root) for one URL.

    Perl serves ``html/`` as the web root and resolves every URL against the
    *skin* directory: ``Slim/Web/HTTP.pm:466-490`` takes ``$uri->path()`` and
    the SkinManager prepends the skin (``Slim/Web/Template/SkinManager.pm``),
    so a request for the skin-relative path ``/html/images/radio.png`` is
    answered from ``HTML/EN/html/images/radio.png``.  Live Perl 9.1.1 proves
    exactly that pairing (curl, 2026-09-14):

    ==========================================  ======  ====================
    URL                                         status  served from
    ==========================================  ======  ====================
    ``/html/images/radio.png``                  200     ``HTML/EN/html/…``
    ``/html/EN/html/images/radio.png``          404     (skin doubled)
    ``/html/images/favorites.png``              200     14815 B
    ``/html/EN/html/images/favorites.png``      404     (skin doubled)
    ==========================================  ======  ====================

    Our static root *is* Perl's ``HTML/`` (``html/``), and this port keeps a
    second copy of some defaults under the bare ``images/`` directory, so the
    Jive clients' two spellings (server root and skin-prefixed — the live
    SqueezePlay log asks for both, ``/html/images/radio.png`` **and**
    ``/html/EN/html/images/radio.png``) must resolve to one file.  Order
    mirrors Perl: the path as sent, then without a leading ``html/`` (this
    port's historical layout), then Perl's ``EN/`` skin fallback.

    A stray ``EN/`` segment is also dropped (``/html/EN/html/images/x`` →
    ``html/images/x``): Perl 404s that doubled spelling, but SqueezePlay
    builds it from a *skin-relative* item field, and a 404 there is exactly
    the "no default image" symptom this module fixes.  Being more tolerant
    than Perl is the deliberate choice — it cannot hide a real file.
    """
    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.lstrip("/")
        if candidate and candidate not in out:
            out.append(candidate)

    raw = path.lstrip("/")
    add(raw)
    stripped = raw[len("html/"):] if raw.startswith("html/") else raw
    add(stripped)
    if stripped.startswith("EN/"):
        inner = stripped[len("EN/"):]        # /html/EN/html/images/x → html/images/x
        add(inner)
        if inner.startswith("html/"):
            # …and the layout this port keeps its defaults in (images/x).
            add(inner[len("html/"):])
    if raw.startswith("EN/"):
        add(raw[len("EN/"):])
    add("EN/" + raw)                        # Perl's HTML/EN/<path> skin fallback
    if stripped != raw:
        add("EN/" + stripped)
    return out


def _resize_skin_image(data: bytes, size: tuple[int, int]) -> bytes | None:
    """Shrink a skin image to ``size`` as PNG — Perl's sized skin answer.

    Jive adds its ``artworkspec`` size when it fetches a list icon, so it asks
    for ``/html/images/radio_40x40_m.png``; live Perl answers 40x40 RGBA PNG
    (1961 B for radio, curl 2026-09-14) while we used to ship the 512x512
    original.  ``None`` means "keep the original bytes" (Pillow missing or a
    broken file) — never a 404, the image data is what matters.
    """
    try:
        import io as _io

        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a project dependency
        return None
    try:
        with Image.open(_io.BytesIO(data)) as im:
            im = im.convert("RGBA")
            resample = getattr(
                getattr(Image, "Resampling", Image), "LANCZOS", None
            ) or getattr(Image, "LANCZOS", 1)
            im.thumbnail(size, resample)
            out = _io.BytesIO()
            im.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - a bad image must not break the route
        return None


# ---------------------------------------------------------------------------
# ``/imageproxy/…`` — Perl's Slim::Web::ImageProxy (parity deviation D5)
#
# Perl serves radio/TuneIn logos over this route.  Every rule below is the
# Perl rule with its source:
#   * routing      ``Slim/Web/HTTP.pm:1208-1212`` → ``Slim/Web/Graphics.pm:150-153``
#   * spec         ``Slim/Web/Graphics.pm:128-133`` (crop it out of the basename)
#                  + ``:534-539`` (``parseSpec``)
#   * proxy        ``Slim/Web/ImageProxy.pm:111-236`` (``getImage``),
#                  ``:238-261`` (``_gotArtwork``), ``:263-295``
#                  (``_gotArtworkError``), ``:297-360`` (``_resizeFromFile``),
#                  ``:362-371`` (``_setHeaders``), ``:373-397`` (``_artworkError``),
#                  ``:474-493`` (``getRightSize``), ``:531-533`` (cloud WebP
#                  resizer, 9.1)
#   * TuneIn logos ``Slim/Plugin/InternetRadio/TuneIn/Metadata.pm:44-62``
#                  (``registerHandler``) + ``:507-559`` (``artworkUrl``)
# ---------------------------------------------------------------------------

#: ``ImageProxy.pm:136`` — ``my ($url) = $path =~ m|imageproxy/(.*)/[^/]*|``.
#: Greedy, because the proxied URL contains slashes itself.
_IMAGEPROXY_URL_RE = _re.compile(r"imageproxy/(.*)/[^/]*")

#: ``Graphics.pm:131`` — the resize spec carved out of the basename:
#: ``WxH[_mode][_bgcolor][.ext]``, every part optional.
_IMAGEPROXY_SPEC_RE = _re.compile(
    r"_?((?:[0-9X]+x[0-9X]+)?(?:_\w)?(?:_[\da-fA-F]+)?(?:\.\w+)?)$", _re.ASCII)

#: ``Graphics.pm:536`` — ``parseSpec``.
_IMAGEPROXY_PARSE_RE = _re.compile(
    r"^(?:([0-9X]+)x([0-9X]+))?(?:_(\w))?(?:_([\da-fA-F]+))?(?:\.(\w+))?$",
    _re.ASCII)

#: ``ImageProxy.pm:145`` — ``$spec =~ /^\.(?:png|jpe?g)/i`` = "no resizing asked".
_IMAGEPROXY_NO_RESIZE_RE = _re.compile(r"^\.(?:png|jpe?g)", _re.I)
_IMAGEPROXY_SVG_RE = _re.compile(r"\.svg$", _re.I)
_IMAGEPROXY_WEBP_RE = _re.compile(r"^https?:.*\.webp(?:$|\?)", _re.I)

#: ``ImageProxy.pm:94`` — ``ONE_YEAR``.
_IMAGEPROXY_ONE_YEAR = 86400 * 365

#: ``ImageProxy.pm:96`` (9.1) — ``REDIRECT_IMAGE_TO_COMPATIBLE``.
_IMAGEPROXY_CLOUD_RESIZER = "https://api.lms-community.org/img/compatible/"

#: ``ImageProxy.pm:94`` — the proxied fetch asks for these formats.
_IMAGEPROXY_ACCEPT = "image/jpeg,image/png;q=0.9,image/gif;q=0.1"

#: ``Slim/Utils/Misc.pm:1199-1229`` — ``userAgentString('legacy')`` starts with
#: ``iTunes/4.7.1`` so the CDNs hand out PNG/JPEG instead of WebP.
_IMAGEPROXY_LEGACY_UA = (
    "iTunes/4.7.1 (Linux; N; Linux; x86_64-linux; de; de_DE; "
    "SqueezeCenter, Squeezebox Server, Lyrion Music Server) 9.2.0"
)

#: TuneIn logo sizes — ``Slim/Plugin/InternetRadio/TuneIn/Metadata.pm:507-517``
#: (``t`` = 75x75, ``q`` = 145x145, ``d`` = 300x300, ``g`` = 600x600).
_IMAGEPROXY_TUNEIN_SIZES = {75: "t", 145: "q", 300: "d", 600: "g"}

#: ``Metadata.pm:49-62`` — the artwork URL patterns TuneIn registers.
_IMAGEPROXY_HANDLERS = (
    (_re.compile(
        r"cloudfront\.net/(?:[ps]?\d+|gn/[A-Z0-9]+)[tqgd]?\.(?:jpe?g|png|gif)$"),
     "tunein"),
    (_re.compile(
        r"cdn-profiles\.tunein\.com/.*/logo[tqgd]\.(?:jpe?g|png|gif)"),
     "tunein"),
    (_re.compile(
        r"cdn-radiotime-logos\.tunein\.com/s\d+[tqdg]\.(?:jpe?g|png|gif)"),
     "tunein"),
)

#: Artwork cache of ``ImageProxy.pm:499-523`` (Perl: 30 days on disk).  The
#: observable behaviour is the byte-identical replay with ``_setHeaders``
#: (``:126-134``), so an in-process LRU carries it.
_IMAGEPROXY_CACHE_MAX = 256
_imageproxy_cache: "OrderedDict[str, tuple[str, bytes]]" = OrderedDict()
#: Perl's request queue (``ImageProxy.pm:194-214``) downloads a URL once and
#: hands the same bytes to every queued caller — one lock per URL.
_imageproxy_locks: dict = {}


def _imageproxy_cache_get(key: str) -> tuple[str, bytes] | None:
    hit = _imageproxy_cache.get(key)
    if hit is None:
        return None
    _imageproxy_cache.move_to_end(key)
    return hit


def _imageproxy_cache_put(key: str, fmt: str, data: bytes) -> None:
    _imageproxy_cache[key] = (fmt, data)
    _imageproxy_cache.move_to_end(key)
    while len(_imageproxy_cache) > _IMAGEPROXY_CACHE_MAX:
        _imageproxy_cache.popitem(last=False)


def _imageproxy_parse_spec(spec: str) -> tuple[str, str, str, str, str]:
    """Perl ``Slim::Web::Graphics->parseSpec`` (``Graphics.pm:534-539``).

    Returns ``(width, height, mode, bgcolor, ext)``; missing parts are ``""``
    (Perl's ``undef``).
    """
    m = _IMAGEPROXY_PARSE_RE.match(spec or "")
    if not m:
        return ("", "", "", "", "")
    return tuple(g or "" for g in m.groups())  # type: ignore[return-value]


def _imageproxy_spec(path: str) -> str:
    """Resize spec of an ``/imageproxy/…`` path (``Graphics.pm:128-133``)."""
    m = _IMAGEPROXY_SPEC_RE.search(Path(path).name)
    return m.group(1) if m else ""


def _imageproxy_get_right_size(spec: str, sizes: dict) -> str | None:
    """Perl ``Slim::Web::ImageProxy->getRightSize`` (``ImageProxy.pm:474-493``)."""
    width, height, _mode, _bg, _ext = _imageproxy_parse_spec(spec)
    if width or height:
        # ``$width ||= $height; $height ||= $width;``
        width = width or height
        height = height or width

        def _num(value: str) -> int:
            # Perl compares numerically; 'X' (the auto axis) is 0.
            try:
                return int(value)
            except ValueError:
                return 0

        minimum = max(_num(width), _num(height))
        # smallest size larger than what we need
        for size in sorted(sizes):
            if size >= minimum:
                return sizes[size]
    return None


def _imageproxy_tunein_artwork(url: str, spec: str) -> str:
    """Perl ``artworkUrl`` — ``TuneIn/Metadata.pm:521-559``.

    Picks the smallest TuneIn file that fits the requested spec.
    """
    # "shortcut for station logo" (:526-531)
    m = _re.search(r"(/images/logo)(?:[tgqd])", url)
    if m:
        return f"{url[:m.start()]}{m.group(1)}g{url[m.end():]}"
    m = _re.search(r"(cdn-radiotime-logos\.tunein\.com/s\d+)[tqdg](\.png)", url)
    if m:
        return f"{url[:m.start()]}{m.group(1)}g{m.group(2)}{url[m.end():]}"

    # (:533-554)
    m = _re.search(r"/([ps]?)(\d+)([tqgd]?)\.(jpg|jpeg|png|gif)$", url, _re.I)
    logo = m.group(1) if m else ""
    size = (m.group(3) if m else "").lower()
    if not (logo and size):
        size = "g"
    ext = _imageproxy_parse_spec(spec)[4]
    minimum = _imageproxy_get_right_size(spec, _IMAGEPROXY_TUNEIN_SIZES)
    for key in sorted(_IMAGEPROXY_TUNEIN_SIZES):
        if _IMAGEPROXY_TUNEIN_SIZES[key] == minimum:
            size = minimum
            break
        if _IMAGEPROXY_TUNEIN_SIZES[key] == size:
            break
    if size:
        url = _re.sub(rf"[tqgd]?\.{_re.escape(ext)}$", f"{size}.{ext}", url)
    return url


def _imageproxy_handler_for(url: str):
    """Perl ``ImageProxy->getHandlerFor`` (``ImageProxy.pm:457-460``)."""
    for pattern, kind in _IMAGEPROXY_HANDLERS:
        if pattern.search(url):
            return _imageproxy_tunein_artwork
    return None


def _imageproxy_is_http(url: str) -> bool:
    return bool(_re.match(r"^https?:", url, _re.I))


def _imageproxy_magic_type(data: bytes) -> str | None:
    """``Slim/Utils/GDResizer.pm`` ``_content_type`` — type from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _imageproxy_headers(fmt: str) -> dict[str, str]:
    """Perl ``_setHeaders`` — ``ImageProxy.pm:362-371``."""
    ct = fmt if "image" in fmt else f"image/{fmt}"
    ct = ct.replace("jpg", "jpeg")
    import time as _time
    from email.utils import formatdate
    return {
        "Content-Type": ct,
        "Cache-Control": f"max-age={_IMAGEPROXY_ONE_YEAR}",
        "Expires": formatdate(_time.time() + _IMAGEPROXY_ONE_YEAR, usegmt=True),
    }


def _imageproxy_error_headers(fmt: str = "png") -> dict[str, str]:
    """Perl ``_artworkError`` header block — ``ImageProxy.pm:388-394``."""
    ct = fmt if "image" in fmt else f"image/{fmt}"
    ct = ct.replace("jpg", "jpeg")
    import time as _time
    from email.utils import formatdate
    return {
        "Content-Type": ct,
        # ``$response->expires( time() - 1 )`` + ``Cache-Control: no-cache``
        "Cache-Control": "no-cache",
        "Expires": formatdate(_time.time() - 1, usegmt=True),
    }


def _imageproxy_resize(data: bytes, spec: str, source_format: str,
                       use_spec_ext: bool = True) -> tuple[bytes, str] | None:
    """Perl ``Slim::Utils::ImageResizer``/``GDResizer::resize``.

    ``ImageResizer.pm`` hands the spec to ``GDResizer`` (``GDResizer.pm:33-250``):
    ``$mode`` defaults to ``'m'`` (:108-111, "default mode is always max"),
    ``'m'``/``'p'`` keep the aspect ratio and never upscale (:140-171), the
    output format is the requested extension and falls back to the source
    format (:124-131) — except "Bug 17140" (:142-149) which switches to PNG
    whenever the result would be padded.  ``_artworkError`` passes no format
    (``use_spec_ext=False``), so the placeholder keeps the source format.
    """
    width, height, mode, _bg, ext = _imageproxy_parse_spec(spec)
    want = (ext if use_spec_ext else "") or source_format or ""
    want = want.split("/")[-1].lower()
    fmt = "jpg" if want in ("jpg", "jpeg") else (want or "png")
    try:
        import io as _io

        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a project dependency
        return None
    try:
        with Image.open(_io.BytesIO(data)) as im:
            ow, oh = im.size
            if not ow or not oh:
                return None
            # ``$width = undef if $width eq 'X'`` (:87-89)
            req_w = None if (not width or width == "X") else int(width)
            req_h = None if (not height or height == "X") else int(height)
            if req_w and not req_h:
                req_h = max(1, int(round(req_w * oh / ow)))
            elif req_h and not req_w:
                req_w = max(1, int(round(req_h * ow / oh)))
            if not req_w and not req_h:
                return None
            box_w = int(req_w or ow)
            box_h = int(req_h or oh)
            resample = getattr(
                getattr(Image, "Resampling", Image), "LANCZOS", None
            ) or getattr(Image, "LANCZOS", 1)
            out_im = im.copy()
            if str(mode).lower() in ("c", "f") or (str(mode) == "F"
                                                   and (box_w < ow or box_h < oh)):
                # crop/fill to the exact box (Image::Scale keep_aspect => 0)
                scale = max(box_w / ow, box_h / oh)
                box = (max(1, int(round(ow * scale))),
                       max(1, int(round(oh * scale))))
                out_im = out_im.resize(box, resample)
                left = (box[0] - box_w) // 2
                top = (box[1] - box_h) // 2
                out_im = out_im.crop((left, top, left + box_w, top + box_h))
            else:
                # mode 'm' (default) / 'p': fit, never upscale (:151-161)
                out_im.thumbnail((box_w, box_h), resample)
                # Bug 17140 (:140-149): a padded result must be PNG
                if fmt != "png":
                    if (oh / ow) != (box_h / box_w):
                        fmt = "png"
            out = _io.BytesIO()
            if fmt == "jpg":
                out_im.convert("RGB").save(out, format="JPEG", quality=85)
            elif fmt == "gif":
                out_im.convert("RGB").save(out, format="GIF")
            else:
                fmt = "png"
                out_im.convert("RGBA").save(out, format="PNG")
            return out.getvalue(), fmt
    except Exception:  # noqa: BLE001 - a bad image must never break the route
        return None


def _imageproxy_placeholder_path() -> Path | None:
    """Perl ``_artworkError`` source file — ``ImageProxy.pm:382``.

    ``Slim::Web::HTTP::fixHttpPath($prefs->get('skin'), 'html/images/radio.png')``
    — the skin's generic radio image.
    """
    root = _static_root()
    if root is None:
        return None
    for rel in _static_path_variants("/html/images/radio.png"):
        cand = root / rel
        if cand.is_file():
            return cand
    return None


async def _imageproxy_placeholder(spec: str) -> tuple[int, dict[str, str], bytes]:
    """Perl ``_artworkError`` — ``ImageProxy.pm:373-397``.

    It serves the skin's ``html/images/radio.png`` (resized to the spec) with
    ``Cache-Control: no-cache``.  Perl deliberately does NOT set the error code
    (``# $response->code($code);``, ``:391``), so a failed fetch answers **200**
    with the placeholder — live Perl 9.1.1, 2026-09-14:
    ``/imageproxy/test.jpg`` → ``200 image/png 16749 B`` and
    ``/imageproxy/<defunct tunein id>/image.png`` → ``200 image/png`` (radio.png).
    """
    p = _imageproxy_placeholder_path()
    data = None
    if p is not None:
        try:
            data = p.read_bytes()
        except OSError:
            data = None
    if data is None:
        return 404, {"Content-Type": "text/plain"}, b"no artwork"
    fmt = "png"
    width, height = _imageproxy_parse_spec(spec)[:2]
    if width or height:
        res = _imageproxy_resize(data, spec, fmt, use_spec_ext=False)
        if res is not None:
            data, fmt = res
    return 200, _imageproxy_error_headers(fmt), data


def _imageproxy_read_file_url(url: str) -> tuple[bytes, str] | None:
    """Perl ``Slim::Utils::Misc::pathFromFileURL`` (``ImageProxy.pm:208-211``)."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        path = f"//{parsed.netloc}{path}"
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    ctype = _MIME_BY_EXT.get(Path(path).suffix.lower(), "")
    mime = _imageproxy_magic_type(data)
    return data, (mime or ctype)


async def _imageproxy_proxied(url: str, spec: str, cache_path: str,
                             original_url: str | None = None
                             ) -> tuple[int, dict[str, str], bytes]:
    """Perl ``$handleProxiedUrl`` + ``_gotArtwork`` + ``_resizeFromFile``.

    ``ImageProxy.pm:157-236``, ``:238-261``, ``:297-360``.
    """
    if not url or not (_imageproxy_is_http(url) or url.lower().startswith("file:")):
        # :160-165 — "No artwork found, returning 404"
        return await _imageproxy_placeholder(spec)

    headers = {
        "Accept": _IMAGEPROXY_ACCEPT,
        "User-Agent": _IMAGEPROXY_LEGACY_UA,
    }
    # :199-205 (9.1) — WebP sources go through the LMS cloud resizer, because
    # Image::Scale/ImageResize here cannot read WebP.
    if original_url is None and _IMAGEPROXY_WEBP_RE.match(url):
        from urllib.parse import quote
        url = _IMAGEPROXY_CLOUD_RESIZER + quote(url, safe="~")
        headers["X-LMS-Plugin-ID"] = "Slim::Web::ImageProxy"

    if url.lower().startswith("file:"):
        got = _imageproxy_read_file_url(url)
        if got is None:
            return await _imageproxy_placeholder(spec)   # → _gotArtworkError
        data, ctype = got
    else:
        import httpx

        try:
            async with httpx.AsyncClient(follow_redirects=True,
                                         timeout=30.0) as client:
                resp = await client.get(url, headers=headers)
        except Exception:  # noqa: BLE001 - network failures end in the placeholder
            return await _imageproxy_placeholder(spec)
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        data = resp.content
        if resp.status_code >= 400 or not data:
            return await _imageproxy_placeholder(spec)

        if "text" in ctype:
            # :246-258 — many servers lie about playlists/images; guess from
            # the magic bytes and error out when that fails too.
            ctype = _imageproxy_magic_type(data) or ""
            if not ctype:
                return await _imageproxy_placeholder(spec)
        elif "webp" in ctype:
            # :269-288 — one conversion attempt via the cloud resizer, then 500.
            if original_url is not None:
                return await _imageproxy_placeholder(spec)
            from urllib.parse import quote
            return await _imageproxy_proxied(
                _IMAGEPROXY_CLOUD_RESIZER + quote(url, safe="~"), spec,
                cache_path, original_url=url)

    ctype = (ctype or _imageproxy_magic_type(data) or "").lower()
    # ``_resizeFromFile`` (:311-326): no resizing when the spec asks for the
    # original extension and the source already is PNG/JPEG.
    if (_IMAGEPROXY_NO_RESIZE_RE.match(spec)
            and ctype in ("image/png", "image/jpeg", "image/jpg")):
        fmt = ctype.split("/", 1)[1].replace("jpeg", "jpg")
        _imageproxy_cache_put(cache_path, fmt, data)
        return 200, _imageproxy_headers(fmt), data

    # :328-356 — resize, cache, then answer with the resized bytes.
    source_format = ctype.split("/", 1)[1] if ctype else ""
    res = _imageproxy_resize(data, spec, source_format)
    if res is None:
        # :341-348 — "resize command failed, return 500" (again: the code is
        # not sent, the placeholder goes out).
        return await _imageproxy_placeholder(spec)
    data, fmt = res
    _imageproxy_cache_put(cache_path, fmt, data)
    return 200, _imageproxy_headers(fmt), data


async def _serve_imageproxy(path: str) -> tuple[int, dict[str, str], bytes]:
    """``/imageproxy/<uri_escaped url>/<spec>`` — ``ImageProxy.pm:111-236``.

    Perl's image proxy: it serves the artwork behind a URL locally, resizing
    on the way — the route SqueezePlay/Squeezer use for radio and TuneIn logos
    (``proxiedImage`` builds it, ``ImageProxy.pm:407-425``).
    """
    spec = _imageproxy_spec(path)

    # Perl unescapes the request path before routing it (``Slim/Utils/Misc.pm
    # :345-353`` ``unescape``, called from ``Slim/Web/HTTP.pm``), so the
    # proxied URL arrives decoded — live 9.1.1, 2026-09-14: the lowercase
    # ``%3a%2f%2f`` spelling answers from the *same* cache entry as the
    # uppercase one.  ``proxiedImage`` escapes with ``uri_escape_utf8``
    # (``ImageProxy.pm:424``), i.e. UTF-8 percent escapes.
    from urllib.parse import unquote

    path = unquote(path, encoding="utf-8", errors="replace")

    # Cache lookup (:119-134): some clients ask with a trailing ".png" that is
    # not part of the cache key, so both spellings are tried.
    keys = [path]
    if path.endswith(".png"):
        keys.append(path[:-4])
    for key in keys:
        hit = _imageproxy_cache_get(key)
        if hit is not None:
            fmt, data = hit
            return 200, _imageproxy_headers(fmt), data

    m = _IMAGEPROXY_URL_RE.search(path)
    url = m.group(1) if m else ""
    if not url:
        # :138-143 — "Artwork ID not found" → _artworkError
        return await _imageproxy_placeholder(spec)

    handler = _imageproxy_handler_for(url)
    if handler is None and (
            _IMAGEPROXY_SVG_RE.search(url)
            or (_IMAGEPROXY_NO_RESIZE_RE.match(spec) and _imageproxy_is_http(url))):
        # :145-155 — a ``.png``/``.jpg`` spec asks for the untouched original:
        # 301 to the source URL (live 9.1.1: 301, Location, Content-Length 0).
        return 301, {"Location": url,
                     "Content-Type": "application/octet-stream",
                     "Content-Length": "0"}, b""

    if handler is not None:
        # :229-233 — a registered handler (TuneIn) may rewrite the URL.
        rewritten = handler(url, spec)
        if rewritten is None:
            return await _imageproxy_placeholder(spec)
        url = rewritten

    lock = _imageproxy_locks.get(url)
    if lock is None:
        import asyncio as _asyncio
        lock = _imageproxy_locks[url] = _asyncio.Lock()
    async with lock:
        # Perl queued identical requests (:194-214) so the file is downloaded
        # once; the queued callbacks all get the same bytes.
        hit = _imageproxy_cache_get(path)
        if hit is not None:
            fmt, data = hit
            return 200, _imageproxy_headers(fmt), data
        return await _imageproxy_proxied(url, spec, path)


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

        # Perl's ImageProxy (deviation D5): /imageproxy/<uri-escaped url>/<spec>.
        # Perl routes it in Slim/Web/HTTP.pm:1208-1212 to
        # Slim::Web::Graphics::artworkRequest → Slim/Web/Graphics.pm:150-153 →
        # Slim::Web::ImageProxy->getImage (ImageProxy.pm:111-236).
        if path.startswith("/imageproxy/") and method in ("GET", "HEAD"):
            status, headers, body = await _serve_imageproxy(path)
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            })
            await send({"type": "http.response.body",
                        "body": b"" if method == "HEAD" else body})
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
