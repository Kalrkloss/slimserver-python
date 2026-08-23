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


def parse_pcm_header(path: Path) -> dict | None:
    """Parse WAV/AIFF headers server-side, like the Perl LMS does
    (Slim/Player/Source.pm -> Squeezebox2 stream('s') fills the strm PCM
    fields from the parsed header and serves *headerless* raw PCM).

    Returns None for non-PCM files or unparsable headers; otherwise:
      {"bits": 16, "rate": 44100, "channels": 2, "bigendian": False,
       "data_offset": 44}
    data_offset is the byte offset of the first sample frame — the
    /stream endpoint skips everything before it.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(128)
        if len(head) < 44:
            return None
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            # walk chunks to 'fmt ' and 'data'
            pos = 12
            fmt = None
            data_offset = None
            while pos + 8 <= len(head):
                cid = head[pos:pos + 4]
                clen = int.from_bytes(head[pos + 4:pos + 8], "little")
                if cid == b"fmt " and pos + 8 + 16 <= len(head):
                    body = head[pos + 8:pos + 8 + 16]
                    audio_fmt = int.from_bytes(body[0:2], "little")
                    channels = int.from_bytes(body[2:4], "little")
                    rate = int.from_bytes(body[4:8], "little")
                    bits = int.from_bytes(body[14:16], "little")
                    fmt = (audio_fmt, channels, rate, bits)
                elif cid == b"data":
                    data_offset = pos + 8
                    break
                pos += 8 + clen + (clen & 1)  # chunks are word-aligned
            if fmt is None or data_offset is None:
                return None
            audio_fmt, channels, rate, bits = fmt
            if audio_fmt not in (1, 3) or not (1 <= channels <= 2) \
                    or bits not in (8, 16, 24, 32) or rate == 0:
                return None
            return {"bits": bits, "rate": rate, "channels": channels,
                    "bigendian": False, "data_offset": data_offset}
        if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
            pos = 12
            comm = None
            ssnd = None
            while pos + 8 <= len(head):
                cid = head[pos:pos + 4]
                clen = int.from_bytes(head[pos + 4:pos + 8], "big")
                if cid == b"COMM" and pos + 8 + 18 <= len(head):
                    body = head[pos + 8:pos + 8 + 18]
                    channels = int.from_bytes(body[0:2], "big")
                    bits = int.from_bytes(body[6:8], "big")
                    # 80-bit IEEE extended sample rate: only the common
                    # integer rates are produced by real encoders.
                    exponent = ((body[8] & 0x7F) << 8 | body[9]) - 16383 - 31
                    mantissa = int.from_bytes(body[10:14], "big")
                    rate = mantissa
                    while exponent < 0:
                        rate >>= 1
                        exponent += 1
                    while exponent > 0:
                        rate <<= 1
                        exponent -= 1
                    comm = (channels, bits, rate)
                elif cid == b"SSND":
                    offset = int.from_bytes(head[pos + 8:pos + 12], "big")
                    ssnd = pos + 16 + offset
                    break
                pos += 8 + clen + (clen & 1)
            if comm is None or ssnd is None:
                return None
            channels, bits, rate = comm
            if not (1 <= channels <= 2) or bits not in (8, 16, 24, 32) \
                    or rate == 0:
                return None
            return {"bits": bits, "rate": rate, "channels": channels,
                    "bigendian": True, "data_offset": ssnd}
    except OSError:
        return None
    return None


# squeezelite pcm.c tables (strm bytes are ASCII digits):
#   sample_size = size-'0'+1  → '0'=8bit '1'=16bit '2'=24bit '3'=32bit
#   sample_rate = sample_rates[rate-'0']
_SL_SAMPLE_SIZE = {8: "0", 16: "1", 24: "2", 32: "3"}
_SL_SAMPLE_RATE = {
    11025: "0", 22050: "1", 32000: "2", 44100: "3", 48000: "4",
    8000: "5", 12000: "6", 16000: "7", 24000: "8", 96000: "9",
    88200: ":", 176400: ";", 192000: "<", 352800: "=", 384000: ">",
}


def pcm_params_for_strm(info: dict) -> tuple[str, str, str, str]:
    """(sample_size, sample_rate, channels, endianness) ASCII codes for
    the strm frame from a parse_pcm_header() result."""
    size = _SL_SAMPLE_SIZE.get(info["bits"], "?")
    rate = _SL_SAMPLE_RATE.get(info["rate"], "?")
    chan = str(info["channels"])
    endian = "0" if info["bigendian"] else "1"
    return size, rate, chan, endian



def _track_path_from_url(url: str) -> Path | None:
    """Convert an LMS file:// URL (or plain path) to a local Path."""
    if url.startswith("file://"):
        return Path(unquote(urlparse(url).path))
    p = Path(url)
    return p if p.exists() else None


# ── ffmpeg availability + PCM transcode (format fallback) ────────────────
_FFMPEG_CACHE: dict[str, bool] = {"checked": False, "available": False}

def _set_server_notice(text: str) -> None:
    """Thin wrapper so this module can surface a Status-bar notice without
    importing the API module at import time (avoids a circular import)."""
    try:
        from lyrion.web.api import set_server_notice as _s
        _s(text)
    except Exception:
        pass


def ffmpeg_available() -> bool:
    """Return True if ffmpeg is on PATH (cached across calls)."""
    if _FFMPEG_CACHE["checked"]:
        return _FFMPEG_CACHE["available"]
    import shutil
    _FFMPEG_CACHE["available"] = shutil.which("ffmpeg") is not None
    _FFMPEG_CACHE["checked"] = True
    return _FFMPEG_CACHE["available"]


def ffprobe_audio_info(path: Path) -> dict | None:
    """Return {'codec','bits','rate','channels'} via ffprobe, or None."""
    import asyncio
    import subprocess
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_name,sample_rate,channels,bits_per_raw_sample",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        line = (proc.stdout or "").strip().splitlines()
        if not line:
            return None
        c, rate, ch, bits = (line[0].split(",") + ["", "", ""])[:4]
        return {
            "codec": c.strip(),
            "rate": int(rate) if rate.isdigit() else 44100,
            "channels": int(ch) if ch.isdigit() else 2,
            "bits": int(bits) if bits.isdigit() else 16,
        }
    except Exception:
        return None


async def _transcode_to_pcm_pipe(send, path: Path, source_mime: str) -> None:
    """Stream ``path`` through ffmpeg as raw little-endian PCM (s16le).

    Used as the format-fallback when a player cannot decode the source
    codec natively (e.g. FLAC/ALAC/OGG on a classic Squeezebox). Emits the
    ASGI response directly: http.response.start + chunked body.

    The source's own sample rate / channel count / bit depth are read by
    ffprobe so the emitted PCM matches the strm-frame params the server
    already advertised to the player (no resampling).
    """
    import asyncio
    info = ffprobe_audio_info(path) or {}
    rate = int(info.get("rate") or 44100)
    channels = int(info.get("channels") or 2)
    bits = int(info.get("bits") or 16)
    rate_arg = str(rate)
    if bits == 24:
        codec_arg, fmt = "pcm_s24le", "s24le"
    else:
        codec_arg, fmt = "pcm_s16le", "s16le"
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-acodec", codec_arg, "-ac", str(channels), "-ar", rate_arg,
        "-f", fmt, "pipe:1",
    ]
    headers = [
        (b"content-type", b"audio/L16"),
        (b"cache-control", b"no-cache"),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await proc.wait()
    except Exception as exc:
        logger.warning("ffmpeg transcode failed for %s: %s", path.name, exc)
    await send({"type": "http.response.body", "body": b"", "more_body": False})


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


async def _revert_player_mode(mac: str) -> None:
    """Set a player's mode back to stop after a failed stream attempt.

    The strm frame was already sent and mode was set to 'play' (the UI shows
    Pause optimistically). If the track turns out to be unplayable (missing
    on disk, bad source), revert to 'stop' so the status poll flips the icon
    back to Play — exactly like the real LMS when a track fails.
    """
    try:
        from lyrion.player.manager import PlayerManager
        pm = PlayerManager()
        # Stream requests carry the MAC possibly URL-encoded (player=02%3A…)
        mac = mac.replace("%3A", ":")
        player = pm.get_player(mac)
        if player is None:
            cleaned = mac.upper().replace(":", "")
            for p in pm.get_all_players():
                if p.mac.upper().replace(":", "") == cleaned:
                    player = p
                    break
        if player is not None and player.mode in ("play", "loading"):
            player.mode = "stop"
            pm.set_mode(player.mac, "stop")
    except Exception:
        pass


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
        # The strm frame was already sent and the player set mode=play (the
        # UI shows Pause optimistically). Tell the player to stop so the
        # status poll flips the icon back to Play — the track simply doesn't
        # exist / is unplayable.
        if player_mac:
            await _revert_player_mode(player_mac)
        await _send_simple(send, 404, "Track not found", "text/plain")
        return

    if not mime:
        mime = _EXT_MIME.get(path.suffix.lower(), "audio/mpeg")

    # ── Format fallback: if the request asked for transcoding (strm frame
    # set codec 'p' + &transcode=1) and ffmpeg is present, emit raw PCM via
    # ffmpeg instead of the source file. If ffmpeg is missing the request
    # should never have set transcode=1, but guard anyway: surface a
    # Status-bar notice and serve the source directly (LMS must keep going).
    if query.get("transcode", [None])[0]:
        if ffmpeg_available():
            await _transcode_to_pcm_pipe(send, path, mime)
            return
        _set_server_notice("ffmpeg not found - transcoding not possible")
        logger.warning("transcode requested for %s but no ffmpeg; serving source", path.name)

    # PCM (wav/aiff) is served as HEADERLESS raw samples: the strm frame
    # carries the parsed format and squeezelite's pcm decoder expects the
    # byte stream to start at the first sample frame (Perl LMS behaviour).
    pcm_info = None
    if mime in ("audio/wav", "audio/x-wav", "audio/wave", "audio/aiff",
                "audio/x-aiff", "audio/aif"):
        pcm_info = parse_pcm_header(path)

    # Headers from the ASGI scope (uvicorn lowercases them)
    headers = {k.decode("latin1").lower(): v.decode("latin1")
               for k, v in scope.get("headers", [])}
    range_header = headers.get("range", "")

    file_size = path.stat().st_size
    data_start = pcm_info["data_offset"] if pcm_info else 0
    start = data_start
    end = file_size - 1
    status = 200

    m = _RANGE_RE.search(range_header)
    if m:
        status = 206
        if m.group(1):
            start = max(int(m.group(1)), data_start)
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
    except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
        # Player disconnected (stop/next) — this is normal.
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stream error for track %d: %s", track_id, exc)
    # NOTE: no auto-next here. Advancing the playlist on HTTP-send
    # completion is WRONG: the server can push the whole file into the
    # player's buffer long before it finished playing, which restarts the
    # track in a loop (observed: a new strm every ~1s). The real LMS
    # advances when the PLAYER reports end-of-track (STAT "STMd" — decoder
    # has no more data) — handled in networking/protocol.py
    # `_handle_stat_frame`.


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


async def _strip_icy_meta(chunks, metaint: int, send) -> None:
    """Forward only audio bytes from an Icecast/Shoutcast stream, stripping
    the metadata chunks (1 length byte + N*16 bytes after every `metaint`
    audio bytes).

    `audio_left` is reset after EVERY metadata block — also for real title
    metadata (length byte > 0). Resetting only for empty blocks (n==0) makes
    the parser read the first audio byte after a block as the next length
    byte → the stream drifts → `mad_frame_decode error: lost
    synchronization / bad main_data_begin pointer` → silence on stations
    with real title metadata (1Mix, SWR3) while empty-block stations
    (Hirschmilch) keep playing.
    """
    audio_left = int(metaint)
    meta_left = 0
    async for chunk in chunks:
        buf = chunk
        while buf:
            if meta_left:
                take = min(len(buf), meta_left)
                meta_left -= take
                buf = buf[take:]
            elif audio_left == 0:
                n = buf[0]
                buf = buf[1:]
                meta_left = n * 16
                audio_left = int(metaint)
            else:
                take = min(len(buf), audio_left)
                await send({"type": "http.response.body", "body": buf[:take],
                            "more_body": True})
                buf = buf[take:]
                audio_left -= take


async def _proxy_remote(send, remote_url: str) -> None:
    """Relay an external stream URL (radio favorite) to the player.

    Squeezelite cannot handle https/TLS, so the server fetches the remote
    stream (httpx supports TLS) and re-serves it over plain http on
    /stream.mp3?remote=... The stream may run forever (radio), so no read
    timeout is applied.

    Icecast/Shoutcast metadata: the upstream request sends Icy-MetaData: 1
    (like the Perl LMS, Slim/Player/Protocols/HTTP.pm requestString) and the
    metadata chunks are STRIPPED here before forwarding — Squeezelite does
    NOT parse icy-metaint from the response header (verified in
    stream.c: meta_interval is only set via a cont frame), so any metadata
    left in the stream would be decoded as audio → "lost synchronization".
    """
    import httpx

    try:
        timeout = httpx.Timeout(connect=15.0, read=None, write=None, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream(
                "GET", remote_url,
                headers={
                    "User-Agent": "LyrionMusicServer/9.2.0",
                    # Ask for Icecast metadata — the Perl LMS does the same;
                    # the metadata bytes are stripped below, only the audio
                    # reaches the player.
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
                metaint = resp.headers.get("icy-metaint")
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": proxy_headers,
                })
                if metaint:
                    # Strip Icecast metadata chunks (1 length byte + N*16
                    # bytes after every metaint audio bytes).
                    logger.info("Stream %s: stripping icy metadata (metaint=%s)",
                                remote_url[:60], metaint)
                    await _strip_icy_meta(
                        resp.aiter_bytes(CHUNK_SIZE), int(metaint), send
                    )
                else:
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
