"""
HTTP audio streaming endpoint for Lyrion Music Server.

Squeezelite/Squeezebox players fetch track audio from the server over
HTTP: the slimproto `strm` frame points the player at
`http://<server>:9000/stream.mp3?id=<track_id>`, and this module serves
the actual file bytes with range support (needed for resume/seek).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024

# MIME types by extension (fallback if track.content_type is not set)
_EXT_MIME = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".wav": "audio/wav",
    ".wma": "audio/x-ms-wma",
    ".ape": "audio/x-ape",
    ".mpc": "audio/x-musepack",
    ".wv": "audio/x-wavpack",
}

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _track_path_from_url(url: str) -> Path | None:
    """Convert an LMS file:// URL (or plain path) to a local Path."""
    if url.startswith("file://"):
        return Path(unquote(urlparse(url).path))
    p = Path(url)
    return p if p.exists() else None


async def _load_track(track_id: int):
    """Load a Track row by id, return (path, mime) or (None, None)."""
    from sqlalchemy import select

    from lyrion.database.schema import Track
    from lyrion.database.sqlite_helper import db_session

    async with db_session() as session:
        track = (
            await session.execute(select(Track).where(Track.id == track_id))
        ).scalar_one_or_none()
        if track is None:
            return None, None
        path = _track_path_from_url(track.url) if track.url else None
        mime = track.content_type
        return path, mime


async def stream_track(scope: dict, receive, send) -> None:
    """Serve a track file with HTTP range support (ASGI)."""
    query = parse_qs(urlparse(scope.get("raw_path", b"").decode("latin1")).query)
    query = query or parse_qs(scope.get("query_string", b"").decode("latin1"))

    # Remote stream proxy: ?remote=<url> relays an external stream (radio
    # favorite) through the server. Squeezelite cannot do TLS, so https://
    # streams are fetched here and re-served as plain http.
    remote_url = query.get("remote", [None])[0]
    if remote_url:
        await _proxy_remote(send, remote_url)
        return

    # Test tone (like original LMS "Test tone" in player settings):
    # /stream.mp3?testtone=1 → 5s 440 Hz sine as 16-bit WAV.
    if query.get("testtone"):
        await _send_testtone(send)
        return

    track_id = query.get("id", [None])[0]
    player_mac = query.get("player", [None])[0]

    # LMS format: request is "GET /stream.mp3?player=MAC" (no track id) —
    # resolve the current track from the player's playlist.
    if not track_id and player_mac:
        try:
            from lyrion.player.manager import PlayerManager
            pm = PlayerManager()
            player = pm.get_player(player_mac.replace("%3A", ":"))
            if player is None:
                # normalized MAC (no colons) may also be used
                mac_clean = player_mac.upper().replace("%3A", "").replace(":", "")
                for p in pm.get_all_players():
                    if p.mac.upper().replace(":", "") == mac_clean:
                        player = p
                        break
            if player is not None:
                items = getattr(player, "playlist", []) or []
                idx = player.playlist_position or 0
                if 0 <= idx < len(items):
                    item = items[idx]
                    if isinstance(item, int):
                        track_id = str(item)
                    elif isinstance(item, str) and item.startswith(("http://", "https://")):
                        # Remote radio stream — proxy it through this server
                        # (LMS behaviour: player always fetches from the server).
                        logger.info("Stream: proxying remote %s for %s", item[:60], player_mac)
                        await _proxy_remote(send, item)
                        return
                else:
                    logger.warning("Stream: no track for player %s (playlist %s)", player_mac, items)
        except Exception as exc:
            logger.warning("Stream: resolve player track failed: %s", exc)

    if not track_id:
        await _send_simple(send, 400, "Missing track id", "text/plain")
        return

    try:
        track_id = int(track_id)
    except ValueError:
        await _send_simple(send, 400, "Invalid track id", "text/plain")
        return

    path, mime = await _load_track(track_id)
    if path is None or not path.is_file():
        logger.warning("Stream: track %d not found on disk (%s)", track_id, path)
        await _send_simple(send, 404, "Track not found", "text/plain")
        return

    if not mime:
        mime = _EXT_MIME.get(path.suffix.lower(), "audio/mpeg")

    # Headers from the ASGI scope (uvicorn lowercases them)
    headers = {k.decode("latin1").lower(): v.decode("latin1")
               for k, v in scope.get("headers", [])}
    range_header = headers.get("range", "")

    file_size = path.stat().st_size
    start = 0
    end = file_size - 1
    status = 200

    m = _RANGE_RE.search(range_header)
    if m:
        status = 206
        if m.group(1):
            start = int(m.group(1))
        if m.group(2):
            end = min(int(m.group(2)), file_size - 1)
        if start >= file_size:
            await _send_simple(
                send, 416,
                f"Requested range not satisfiable: {start}-{end}",
                "text/plain",
                extra={"Content-Range": f"bytes */{file_size}"},
            )
            return

    length = end - start + 1
    response_headers = [
        (b"content-type", mime.encode()),
        (b"accept-ranges", b"bytes"),
        (b"content-length", str(length).encode()),
        (b"cache-control", b"no-cache"),
    ]
    if status == 206:
        response_headers.append(
            (b"content-range", f"bytes {start}-{end}/{file_size}".encode())
        )

    await send({
        "type": "http.response.start",
        "status": status,
        "headers": response_headers,
    })

    # Stream the file in chunks (async via to_thread — no aiofiles needed)
    import asyncio as _asyncio

    fully_streamed = False
    try:
        remaining = length
        with open(path, "rb") as f:
            if start > 0:
                f.seek(start)
            while remaining > 0:
                chunk = await _asyncio.to_thread(f.read, min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                await send({"type": "http.response.body", "body": chunk,
                            "more_body": remaining > 0})
        fully_streamed = remaining <= 0
    except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
        # Player disconnected (stop/next) — this is normal.
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stream error for track %d: %s", track_id, exc)

    # Auto-next: when the file was streamed to the end and the request
    # carried a player MAC, advance that player's playlist. Manual stop
    # closes the connection early (fully_streamed stays False).
    if fully_streamed:
        player_mac = (query.get("player", [None]) or [None])[0]
        if player_mac:
            try:
                from lyrion.player.manager import PlayerManager
                _asyncio.create_task(PlayerManager().playlist_next(player_mac))
                logger.debug("Track %d finished for %s — auto-next scheduled",
                             track_id, player_mac)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Auto-next failed: %s", exc)


async def _send_testtone(send) -> None:
    """Send a 5s 440 Hz sine test tone as 16-bit PCM WAV (like original LMS)."""
    import math
    import struct as _struct

    rate = 44100
    freq = 440.0
    seconds = 5
    amplitude = 0.5  # -6 dBFS, clearly audible but not clipping
    n_samples = rate * seconds

    data_size = n_samples * 2  # mono, 16-bit
    header = b"RIFF" + _struct.pack("<I", 36 + data_size) + b"WAVE"
    header += b"fmt " + _struct.pack("<I", 16)
    header += _struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)  # PCM, mono
    header += b"data" + _struct.pack("<I", data_size)

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"audio/wav"),
            (b"content-length", str(44 + data_size).encode()),
            (b"cache-control", b"no-cache"),
        ],
    })

    samples_per_chunk = 8192 // 2
    buf = bytearray(header)
    idx = 0
    while idx < n_samples:
        end = min(idx + samples_per_chunk, n_samples)
        samples = b"".join(
            _struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(idx, end)
        )
        buf += samples
        idx = end
        if len(buf) >= 65536:
            await send({"type": "http.response.body", "body": bytes(buf), "more_body": True})
            buf = bytearray()
    await send({"type": "http.response.body", "body": bytes(buf), "more_body": False})
    logger.info("Test tone sent: %d Hz, %.0fs, %d bytes", freq, seconds, 44 + data_size)


async def _send_simple(send, status: int, body: str, content_type: str,
                       extra: dict | None = None) -> None:
    """Send a small non-streaming response."""
    headers = [(b"content-type", content_type.encode()),
               (b"content-length", str(len(body.encode())).encode())]
    for k, v in (extra or {}).items():
        headers.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body.encode()})


async def _proxy_remote(send, remote_url: str) -> None:
    """Relay an external stream URL (radio favorite) to the player.

    Squeezelite cannot handle https/TLS, so the server fetches the remote
    stream (httpx supports TLS) and re-serves it over plain http on
    /stream.mp3?remote=... The stream may run forever (radio), so no read
    timeout is applied.
    """
    import httpx

    try:
        timeout = httpx.Timeout(connect=15.0, read=None, write=None, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream(
                "GET", remote_url,
                headers={
                    "User-Agent": "LyrionMusicServer/9.2.0",
                    # Ask for Icecast metadata — Squeezelite sends this to us
                    # and NEEDS the icy-metaint interval in the response to
                    # interleave metadata bytes correctly.
                    "Icy-MetaData": "1",
                },
            ) as resp:
                if resp.status_code >= 400:
                    logger.warning("Remote stream %s -> HTTP %d",
                                   remote_url[:80], resp.status_code)
                    await _send_simple(send, 502, "Upstream error", "text/plain")
                    return
                ctype = resp.headers.get("content-type", "audio/mpeg")
                proxy_headers = [
                    (b"content-type", ctype.encode()),
                    (b"cache-control", b"no-cache"),
                ]
                # Forward Icecast metadata interval — Squeezelite sends
                # "Icy-MetaData: 1" and NEEDS icy-metaint to interleave
                # the metadata bytes correctly. Without it the decoder
                # chokes on metadata as audio → silence.
                metaint = resp.headers.get("icy-metaint")
                if metaint:
                    proxy_headers.append((b"icy-metaint", metaint.encode()))
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": proxy_headers,
                })
                async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                    await send({"type": "http.response.body", "body": chunk,
                                "more_body": True})
                await send({"type": "http.response.body", "body": b"",
                            "more_body": False})
    except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
        # Player disconnected (stop/next) — normal.
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Remote stream proxy error for %s: %s",
                       remote_url[:80], exc)


import asyncio  # noqa: E402  (used in stream_track exception handling)
