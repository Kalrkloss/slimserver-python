"""
Slimproto v8 client — player ↔ server TCP protocol on port 3483.

Frame format (binary, big-endian):
    4-byte header:  [1-byte command ID][3-byte big-endian payload length]
    N-byte payload

Command IDs (hex):
    0x00  HELO   — handshake (player → server)
    0x02  BYE    — disconnect notification
    0x03  STAT   — player status update (player → server)
    0x04  RESP   — response to CLI query (server → player)
    0x05  EVNT   — server → player event / notification
    0x06  QUER   — server → player query
    0x07  BODY   — binary body follows (chunked)
    0x08  STMU   — stream metadata update
    0x09  ANIC   — artwork / now-playing image
    0x0a  GRFB   — global radio feedback (?) — usually not needed
    0x0b  ?      — reserved
    0x0c  ?      — reserved
    0x0d  ?      — reserved
    0x0e  ?      — reserved
    0x0f  ?      — reserved
    0x10  ?      — reserved

Player capabilities bitfield (HELO payload, 4 bytes):
    bit 0  (0x0001)  — can receive PCM
    bit 1  (0x0002)  — can receive FLAC
    bit 2  (0x0004)  — needs 16-bit samples
    bit 3  (0x0008)  — supports DSD over DoP
    bit 4  (0x0010)  — has digital output
    bit 5  (0x0020)  — has IR receiver
    bit 6  (0x0040)  — can receive Ogg Vorbis
    bit 7  (0x0080)  — has cursor keys
    bit 8  (0x0100)  — has keyboard
    bit 9  (0x0200)  — has volume control
    bit 10 (0x0400)  — has infra-red
    bit 11 (0x0800)  — can decode MP3
    bit 12 (0x1000)  — can decode AAC
    bit 13 (0x2000)  — can decode ALAC
    bit 14 (0x4000)  — can decode MP4
    bit 15 (0x8000)  — supports compressed transport

HELO payload (player → server):
    0-7   device-id  (8 bytes ASCII / binary)
    8-15  revision   (8 bytes, firmware rev)
    16    MAC[0]
    17    MAC[1]
    18    MAC[2]
    19    MAC[3]
    20    MAC[4]
    21    MAC[5]
    22-25  capabilities (little-endian u32 bitfield)
    26-29  4-bytes language code (e.g. "EN")
    30-37  8-bytes UUID (unique player ID)
    38-    optional tail (extensions)

HELO response (server → player):
    0     0x00 (OK ack)
    1     num-extensions (u8)
    2-3   buffer-size (u16, in samples)
    4-7   max-output-channels (u32)
    8-11  supported-commands bitfield (u32)
    12-   extension tuples: (code, length, value)
          "J"   — JSON capabilities blob
          "Diac" — digital input adapter caps
          "TUNE" — initial server URL (e.g. http://my.squeeze.center:9000)
          "wrnm" — ?

STAT payload (player → server, sent periodically, big-endian):

    0-3    event              (4 ASCII chars)
    4      num_crlf           (u8)
    5      mas_initialized    (u8, 'm'/'p')
    6      mas_mode           (u8)
    7-10   buffer_size        (u32, rptr / decodeSize)
    11-14  buffer_fullness    (u32, wptr / decodeFull)
    15-18  bytes_received_H   (u32)
    19-22  bytes_received_L   (u32)
    23-24  signal_strength    (u16, 0xffff = "unknown")
    25-28  jiffies            (u32, ms since player boot)
    29-32  output_buffer_size (u32)
    33-36  output_buffer_fullness (u32)
    37-40  elapsed_seconds    (u32)
    41-42  voltage            (u16)
    43-46  elapsed_milliseconds (u32)
    47-50  server_timestamp   (u32)
    51-52  error_code         (u16, only present on 53/57-byte frames)

    Field order/sizes are the Perl `unpack('a4CCCNNNNnNNNNnNNn')`
    (Slim/Networking/Slimproto.pm:768, `_stat_handler`) and match
    SqueezePlay's own STAT packer byte for byte
    (~/opt/squeezeplay/share/jive/jive/net/SlimProto.lua:167-195).

    Frame length variants seen on the wire:
      51 bytes  SqueezePlay / jive (no trailing error_code)
      53 bytes  current firmware (Slimproto.pm:772 "future firmware")
      57 bytes  older firmware: same 53 fields + 4 trailing junk bytes
                (Slimproto.pm:772-777 zeroes error_code for those)

    `event` is normally an STM* code, but SqueezePlay acks *every* frame
    it has no subscription for with sendStatus(opcode)
    (SlimProto.lua:555-558), so `vers`, `setd`, `audg`, `cont`, `strm`, …
    show up as STAT events too. Those acks carry decode/output sizes but
    elapsed=0 while idle.
"""
from __future__ import annotations

import asyncio
import logging
import re
import struct
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from enum import IntFlag
from typing import Any, Callable, Literal, Optional, cast
from urllib.parse import urlparse
import time

from lyrion.formats.lms_types import (
    describe_type,
    format_byte,
    format_extension,
    pcm_samplesize_for,
)

# Install uvloop as the default event loop policy at import time.
try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ── StreamingController wiring (PROT-14) ────────────────────────────────────
# Perl drives every playback transition through one jump table
# (Slim/Player/StreamingController.pm:110-245, `_eventAction` :249-311). These
# two helpers feed the live SlimProto paths into that machine
# (lyrion.player.streaming) so the states/transitions have exactly ONE home.
# Both are failure-proof: they never raise into the caller and — for
# `_streaming_mode` — always fall back to the constant this module used before,
# so a controller that has not seen the full event sequence cannot change the
# reported `player.mode`.
PlayerMode = Literal["stop", "play", "pause", "loading"]


def _streaming_note(mac: str, event: str, **params) -> None:
    """Feed a StreamingController event (no status mode is needed)."""
    try:
        from lyrion.player import streaming
        streaming.apply_event(mac, event, **params)
    except Exception as exc:  # noqa: BLE001
        logger.debug("streaming event %s for %s failed: %s", event, mac, exc)


def _streaming_mode(mac: str, event: str, fallback: PlayerMode, **params) -> PlayerMode:
    """Feed a Perl controller event and return the status ``mode``.

    The event is one Perl reports as a play state (``Started``/``Pause``/
    ``Resume``/``Stopped``/``StreamingFailed`` → ``EVENT_MODE``); ``fallback``
    is the value this module assigned before the wiring.
    """
    try:
        from lyrion.player import streaming
        mode = streaming.apply_event(mac, event, **params)
        if mode in ("stop", "play", "pause", "loading"):
            return mode
    except Exception as exc:  # noqa: BLE001
        logger.debug("streaming event %s for %s failed: %s", event, mac, exc)
    return fallback


def _reported_playmode(mac: str) -> Optional[str]:
    """The status ``mode`` Perl derives from its controller state.

    Perl has no separate ``mode`` field: the status query adds
    ``Slim::Player::Source::playmode($client)`` (``Slim/Control/Queries.pm:4081``)
    and that is ``_returnPlayMode`` (``Slim/Player/Source.pm:55-64``)::

        return 'stop' if !$_[1]->power();
        my $returnedmode = $controller->isStopped ? 'stop'
                            : $controller->isPaused ? 'pause' : 'play';

    ``isStopped`` is ``playingState == STOPPED && streamingState == IDLE``
    (``StreamingController.pm:1681-1683``), so a stream that is only
    BUFFERING/STREAMING — the state ``_Stream`` sets with the strm frame
    (:1350-1352) — already reports ``play``; no player event is needed.
    ``None`` = no controller for this player (the caller keeps its value).
    """
    try:
        from lyrion.player import streaming
        return streaming.reported_playmode(mac)
    except Exception as exc:  # noqa: BLE001
        logger.debug("reported playmode for %s failed: %s", mac, exc)
        return None


def _after_strm_sent(mac: str, song: Any = None) -> None:
    """Perl's bookkeeping after a ``strm 's'`` frame went out.

    Three Perl rules, all of them identical for a /stream.mp3 file stream and
    a direct radio stream (``stream_s`` builds both, ``Squeezebox.pm:546-770``):

    * ``$client->streamStartTimestamp(undef)`` on EVERY strm frame
      (``Squeezebox.pm:567``; the client sets it again with its STMc,
      ``Squeezebox2.pm:141-142``). Until then ``statHandler`` ignores every
      stat code but STMc (:143-145) — which is why the player's own STMf here
      is a start ack, not a lost stream (``Squeezebox2.pm:174-176`` has no
      STMf branch at all).
    * the controller already moved to BUFFERING + STREAMING with the frame
      (``_Stream``, ``StreamingController.pm:1350-1352``; ``note_strm_sent``).
    * the status ``mode`` follows that state (``Source::playmode``,
      ``Source.pm:55-64``) — ``play`` from the first frame on, NOT only once
      the player reports STMs (a client that never sends one would otherwise
      stay ``stop`` while its audio runs).
    """
    try:
        from lyrion.player.manager import PlayerManager
        player = PlayerManager().get_player(mac)
    except Exception as exc:  # noqa: BLE001
        logger.debug("strm tail: player lookup failed for %s: %s", mac, exc)
        player = None
    if player is not None:
        # Squeezebox.pm:567 — the start handshake is re-armed per frame.
        player.strm_sent_at = time.time()
        if song is not None:
            player.strm_sent_track = song
    try:
        from lyrion.player import streaming
        streaming.note_strm_sent(mac, song)
    except Exception as exc:  # noqa: BLE001
        logger.debug("streaming note_strm_sent failed for %s: %s", mac, exc)
    mode = _reported_playmode(mac)
    if player is not None and mode in ("stop", "play", "pause"):
        player.mode = cast(PlayerMode, mode)


# Player buffer threshold, in KB of audio the player buffers before it
# starts decoding — carried in the strm frame. Perl values:
#   * default client pref `bufferThreshold` = 255 KB
#     (Slim/Player/Player.pm:65)
#   * reduced for a file smaller than the threshold: (filesize/1024)-1 KB
#     (Slim/Player/Squeezebox.pm:916-921)
#   * remote streams: 20 KB, or int(bitrate/8) * bufferSecs / 1000 capped at
#     255 (Squeezebox.pm:160-179; bufferSecs default 3 at :162)
BUFFER_THRESHOLD_KB = 255
REMOTE_BUFFER_THRESHOLD_KB = 20
REMOTE_BUFFER_SECS = 3
# Player liveness poll interval. Perl: Slim/Networking/Slimproto.pm:40
# `my $check_all_clients_time = 5` — every 5 s each client gets
# requestStatus() (= `stream 't'`), and a player that fails to answer for
# 3 intervals is disconnected (:199-241).
KEEPALIVE_SECONDS = 5.0
# Number of missed polls before Perl declares the client dead:
# `if ( $last_heard >= $check_all_clients_time * 3 ... ) { slimproto_close(
# $client->tcpsock ) }` (Slimproto.pm:218-238). The missing half of the
# check_all_clients contract — without it a client whose socket went away
# without a FIN stays "connected" and every strm frame is written into a
# black hole (no RESP, no STM, title frozen).
KEEPALIVE_MISS_LIMIT = 3

# strm `outputThreshold` per source format byte. Perl: stream_s in
# Slim/Player/Squeezebox.pm — see output_threshold() for the line numbers.
OUTPUT_THRESHOLD_BY_FORMAT: dict[str, int] = {
    "p": 0,    # pcm / wav / aif        (:602, :622)
    "f": 0,    # flac / ogf (hires: 20) (:645-648, :655)
    "m": 1,    # mp3 and the default    (:769)
    "w": 10,   # wma (lossless: 50)     (:682-686)
    "o": 20,   # ogg vorbis             (:696)
    "u": 20,   # ops / opus             (:705)
    "l": 0,    # alac                   (:714)
    "a": 0,    # mp4 / aac              (:731)
    "d": 0,    # dsf / dff              (:740)
    "s": 1,    # SqueezePlayDirect      (:750)
    "n": 0,    # test                   (:759)
}
FLAC_HIRES_SAMPLERATE = 88200   # Squeezebox.pm:646
# strm transition fields. Perl: the client prefs `transitionType` /
# `transitionDuration` (defaults 0 = TRANSITION_NONE and 10,
# Squeezebox2.pm:44-45) are sent in stream_s (Squeezebox.pm:940-941). With
# TRANSITION_NONE the duration is inert, but Perl still sends the pref value.
TRANSITION_NONE = 0             # Squeezebox2.pm:44
TRANSITION_DURATION_DEFAULT = 10  # Squeezebox2.pm:45


# ── audg volume → gain (logarithmic, Perl parity) ────────────────────────
#
# Perl sends the player's gain as a 16.16 fixed-point value derived from a
# dB curve, NOT linearly from the 0..100 volume. Linear is ~18 dB too loud
# at mid volume (vol 50: Perl 3840 vs. linear 32768).
#   Slim/Player/Squeezebox2.pm:200-211  @volume_map — old-style gain value
#   Squeezebox2.pm:229-239              getVolumeParameters: totalVolumeRange
#                                       -50 dB, stepPoint -1, stepFraction 1
#   Squeezebox2.pm:241-275              getVolume: y = m*(x - x1) + y1
#   Squeezebox2.pm:213-228              dBToFixed: 16.16 fixed point with 8
#                                       extra bits of accuracy for -30..0 dB
#   Squeezebox2.pm:283-295              volume(): oldGain from the map,
#                                       newGain from the dB curve, 0 when muted
#   Squeezebox2.pm:302-303              preamp = 255 - int(2*preampVolumeControl)
#   Slim/Player/Player.pm:38-39         defaults: digitalVolumeControl 1,
#                                       preampVolumeControl 0 (→ preamp 255)
VOLUME_MAP: tuple[int, ...] = (
    0, 1, 1, 1, 2, 2, 2, 3, 3, 4,
    5, 5, 6, 6, 7, 8, 9, 9, 10, 11,
    12, 13, 14, 15, 16, 16, 17, 18, 19, 20,
    22, 23, 24, 25, 26, 27, 28, 29, 30, 32,
    33, 34, 35, 37, 38, 39, 40, 42, 43, 44,
    46, 47, 48, 50, 51, 53, 54, 56, 57, 59,
    60, 61, 63, 65, 66, 68, 69, 71, 72, 74,
    75, 77, 79, 80, 82, 84, 85, 87, 89, 90,
    92, 94, 96, 97, 99, 101, 103, 104, 106, 108, 110,
    112, 113, 115, 117, 119, 121, 123, 125, 127, 128,
)
TOTAL_VOLUME_RANGE_DB = -50     # Squeezebox2.pm:234
STEP_POINT = -1                 # Squeezebox2.pm:235
STATIC_GAIN = 65536             # 100 % = 16.16 fixed point 1.0

# Per-class volume curves (totalVolumeRange, stepPoint, stepFraction,
# maximumVolume). Whichever curve the player class declares is the one the
# CLIENT compares the audg gain against — sending another one makes jive log
# "server sequence # out of sync" and re-send the volume forever.
#   * classic Squeezebox2/3 family: Squeezebox2.pm:229-239 (-50 dB, -1, 1)
#   * SqueezePlay + Boom:           SqueezePlay.pm:219-226 and Boom.pm:131-139
#     (-74 dB, stepPoint 25, stepFraction 0.5) — jive hardcodes this table in
#     share/jive/jive/audio/Playback.lua (_defaultVolumeToGain, "Squeezeplay.pm
#     uses Boom curve"; the SB2 curve sits commented out right below it).
VOLUME_PARAMS_SQUEEZEBOX2 = (-50.0, -1.0, 1.0, 0.0)
VOLUME_PARAMS_BOOM = (-74.0, 25.0, 0.5, 0.0)      # SqueezePlay/Boom curve


def volume_params_for(model: str):
    """Volume curve parameters of a player class (Perl getVolumeParameters)."""
    m = (model or "").lower()
    if m in ("squeezeplay", "controller", "boom", "softboom"):
        return VOLUME_PARAMS_BOOM
    return VOLUME_PARAMS_SQUEEZEBOX2


def get_volume_db(volume: float, params=VOLUME_PARAMS_SQUEEZEBOX2) -> float:
    """Volume 0..100 → dB (Perl getVolume, Squeezebox2.pm:241-275)."""
    total_range, step_point, step_fraction, maximum = params
    step_db = total_range * step_fraction
    max_volume_db = maximum
    if volume > step_point:
        slope = (max_volume_db - step_db) / (100 - step_point)
        x1, y1 = 100, max_volume_db
    else:
        slope = (step_db - total_range) / (step_point - 0)
        x1, y1 = 0, total_range
    return slope * (volume - x1) + y1


def db_to_fixed(db: float) -> int:
    """dB → 16.16 fixed point gain (Perl dBToFixed, Squeezebox2.pm:213-228)."""
    floatmult = 10 ** (db / 20)
    if -30 <= db <= 0:
        # 8 extra bits of accuracy to avoid rounding errors
        return int(floatmult * (1 << 8) + 0.5) * (1 << 8)
    return int(floatmult * (1 << 16) + 0.5)


def audg_gain(volume: int, model: str = "squeezebox2") -> int:
    """Perl newGain for a volume 0..100 (Squeezebox2.pm:283-295).

    ``model`` selects the player class's volume curve — SqueezePlay/Boom use
    the -74 dB two-ramp curve (VOLUME_PARAMS_BOOM), everything else the
    Squeezebox2 curve. See ``volume_params_for``.
    """
    volume = max(0, min(100, int(volume)))
    if volume <= 0:                 # negative/zero volume = muting
        return 0
    return db_to_fixed(get_volume_db(volume, volume_params_for(model)))


def audg_old_gain(volume: int) -> int:
    """Old-style gain for the audg `oldGain` fields (Squeezebox2.pm:285)."""
    volume = max(0, min(100, int(volume)))
    return VOLUME_MAP[volume] if volume > 0 else 0


def audg_preamp(preamp_volume_control: int = 0) -> int:
    """Perl preamp byte (Squeezebox2.pm:302; pref default 0, Player.pm:39)."""
    return 255 - int(2 * (preamp_volume_control or 0))


def output_threshold(codec: str, samplerate: int | None = None) -> int:
    """Perl ``stream_s`` ``outputThreshold`` for a source format.

    Perl sets this per format byte in ``stream_s`` (Slim/Player/Squeezebox.pm):
    pcm/wav/aif ``p`` → 0 (:602, :622); flac/ogf ``f`` → 0, but 20 for
    samplerate >= 88200 (:645-648) and 20 for Ogg-FLAC (:655); wma ``w`` →
    10, wma-lossless → 50 (:682-686); ogg ``o`` → 20 (:696); ops ``u`` →
    20 (:705); alac ``l`` → 0 (:714); mp4/aac ``a`` → 0 (:731); dsf/dff
    ``d`` → 0 (:740); SqueezePlayDirect ``s`` → 1 (:750); test ``n`` → 0
    (:759); mp3 and the default branch ``m`` → 1 (:769).
    """
    codec = (codec or "m")[:1].lower()
    if codec == "f":
        if samplerate and samplerate >= FLAC_HIRES_SAMPLERATE:
            return 20                      # Squeezebox.pm:645-648
        return 0
    return OUTPUT_THRESHOLD_BY_FORMAT.get(codec, 1)


def stream_buffer_threshold(filesize: int | None = None, *,
                            remote: bool = False,
                            bitrate_bps: int | None = None) -> int:
    """The strm ``bufferThreshold`` value (KB) for a track — Perl parity.

    See the constants above for the Perl locations. Never returns 0: the
    frame field is a byte, so the value is clamped to 1..255.
    """
    if remote:
        threshold = REMOTE_BUFFER_THRESHOLD_KB
        if bitrate_bps and bitrate_bps > 0:
            threshold = int(int(bitrate_bps / 8) * REMOTE_BUFFER_SECS / 1000)
        threshold = min(threshold, 255)
    else:
        threshold = BUFFER_THRESHOLD_KB
        if filesize and filesize < threshold * 1024:
            threshold = (int(filesize / 1024) or 2) - 1
    return max(1, min(255, int(threshold)))


def _notify_cometd_server_status() -> None:
    """Wake Cometd /slim/serverstatus subscribers (player list changed).

    Called after player register/unregister; schedules the fresh
    serverstatus push on the running event loop.
    """
    try:
        from lyrion.web.cometd import get_manager
        mgr = get_manager()
        if mgr is not None:
            asyncio.create_task(mgr.notify_server_status())
    except Exception:  # noqa: BLE001
        pass


async def _notify_metadata_display(mac: str, url: str = "", title: str = "") -> None:
    """Perl's push when an in-stream metadata title changes — ``Info.pm:513-553``.

    ``Slim/Music/Info.pm:513-553`` (``setCurrentTitle``) — Perl only acts when
    the title really changed (:516) and then walks the whole sync group::

        $client->metaTitle( $title );                           # :527
        for my $everybuddy ( $client->syncGroupActiveMembers() ) {
            $everybuddy->update();                              # :529-531
        }
        Slim::Control::Request::notifyFromArray(                # :534
            $client, [ 'playlist', 'newsong', $title ] );

    ``$client->update()`` (``Player.pm:152`` → ``Slim/Display/Display.pm:141``)
    renders the display and — with ``notifyLevel == 2``, i.e. the controller
    subscribed with something other than ``subscribe:showbriefly``
    (``Queries.pm:1758-1763``) — fires ``$display->notify('update')``
    (``Display.pm:214-217``), i.e. a ``displaynotify`` of type ``update``
    (``Display.pm:905-913``).  ``displaystatusQuery`` answers it from the
    display's own cache (``Queries.pm:1650-1651``) as
    ``{text => $screen1->{'line'}, …}`` (:1693-1700) — the two now-playing
    lines, exactly what ``PlayerManager._now_playing_lines`` renders here.

    ``displaystatusQuery_filter`` only forwards the notification when the
    screen content changed (``Queries.pm:1618``) — the changed title is what
    changed it.

    The ``playlist newsong`` notification of :534 carries **3** elements
    (verbs + ``$title``): nothing in this call path has a playlist index, the
    in-stream metadata is a *station* title change, not a new queue entry —
    ``AudioScrobbler`` tells the two forms apart by the presence of ``_p3``
    (``Slim/Plugin/AudioScrobbler/Plugin.pm:322-327``).

    ``Info.pm:536-546`` (Bug 17174) then informs every *other* player that is
    playing the same URL: masters only (a slave does not own a controller
    stream) and only while it is actually playing.
    """
    try:
        from lyrion.control.notifications import notify_from_array
        from lyrion.player.manager import PlayerManager
        from lyrion.web.cometd import get_manager

        pm = PlayerManager()
        mgr = get_manager()
        members = pm.get_sync_group(mac)          # Perl syncGroupActiveMembers
        if not members:
            player = pm.get_player(mac)
            members = [player] if player else []
        for member in members:
            if member is None or not getattr(member, "connected", True):
                continue
            # ``$everybuddy->update()`` — render the screen (Player.pm:152 →
            # Display.pm:141) …
            await pm._display_update(member)
            if mgr is None:
                continue
            # … and notify the displaystatus subscribers (Display.pm:214-217).
            lines = await pm._now_playing_lines(member)
            await mgr.notify_display(member.mac, "update", {"text": lines}, None)

        # Info.pm:534 — the station title change IS a newsong for some purposes.
        if title:
            notify_from_array(mac, ["playlist", "newsong", title])

        # Info.pm:536-546 (Bug 17174) — "Inform other players that may be
        # listening to the same station … We only do this if we have a client
        # with this setCurrentTitle(), which will be the case for in-stream
        # metadata."
        for other in list(pm.players.values()):
            if other is None or other.mac == mac:
                continue                        # :541 ignore the client above
            if not _player_is_playing(other):
                continue                        # :540 ->isPlaying()
            if getattr(other, "sync_master", None):
                continue                        # :542 isSlave() — masters only
            if str(getattr(other, "current_url", "") or "") == str(url or ""):
                notify_from_array(other.mac, ["playlist", "newsong", title])
    except Exception as exc:  # noqa: BLE001 — a display push must never break a stream
        logger.debug("metadata display notify for %s failed: %s", mac, exc)


def _player_is_playing(player) -> bool:
    """Perl ``$_->isPlaying()`` (``Info.pm:540``, ``Source::playing``).

    The port's single source of truth for the play state is the
    StreamingController; a player whose controller has not seen an event yet
    (a fresh connect with plain STAT traffic) is judged by ``mode``, the value
    the STAT path maintains and the status reports.
    """
    from lyrion.player import streaming

    ctl = streaming.controller_for(player.mac)
    if ctl is not None and ctl.events_seen:
        return ctl.is_playing()
    return getattr(player, "mode", "stop") == "play"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SLIMPROTO_PORT = 3483

# Command IDs
CMD_HELO = 0x00
CMD_BYE = 0x02
CMD_STAT = 0x03
CMD_RESP = 0x04
CMD_EVNT = 0x05
CMD_QUER = 0x06
CMD_BODY = 0x07
CMD_STMU = 0x08
CMD_ANIC = 0x09
CMD_GRFB = 0x0A
CMD_META = 0x11  # ask the player to report stream metadata (StreamTitle → STMu)

# Perl's default showBriefly duration, Display.pm:258
# ("$duration = $args->{'duration'} || 1; # duration - default to 1 second").
# The displaytexttimeout pref is 1 as well (Slim/Utils/Prefs.pm:169).
DISPLAY_DURATION_DEFAULT = 1

# ── Display frames: visu / vfdc / grfb / grfe-framebuffer (PROT-18) ───────
# Every frame here is built the Perl way: the payload is concatenated in
# Perl and handed to sendFrame, which wraps it as
# `pack('n', length($data) + 4) . $type . $data`
# (Slim/Player/Squeezebox.pm:1159 — the 2-byte BE length INCLUDES the four
# opcode bytes).
#
# Visualiser types — Slim/Display/Squeezebox2.pm:67-70 ($VISUALIZER_*).
VISU_NONE = 0                # :67
VISU_VUMETER = 1             # :68
VISU_SPECTRUM_ANALYZER = 2   # :69
VISU_WAVEFORM = 3            # :70
# Brightness maps, indexed by brightness 0..maxBrightness
# (maxBrightness = $#map, Slim/Display/Graphics.pm:511-516). The u16 value
# goes out verbatim in 'grfb'.
BRIGHTNESS_MAP_SQUEEZEBOX2 = (65535, 0, 1, 3, 4)   # Squeezebox2.pm:209-211
BRIGHTNESS_MAP_SQUEEZEBOXG = (0, 1, 4, 16, 30)     # SqueezeboxG.pm:135-137
# drawFrameBuf's default transition character — Squeezebox2.pm:234.
DISPLAY_TRANSITION_DEFAULT = "c"
# TextVFD invariants: TextVFD.pm:359-362 logdies on an odd byte count
# ("Odd vfddata") and on more than 500 bytes ("VFDData too long").
VFD_MAX_BYTES = 500
# One framebuffer for the 320x32 SB2 panel: 4 bytes/column × 320 columns
# (Squeezebox2.pm:180-182 bytesPerColumn + :188-190 displayWidth = 1-bit
# 320x32 = 1280 bytes).
DISPLAY_FRAMEBUF_BYTES_SB2 = 1280
# The bitmapped SB1 ('grfd') header: SqueezeboxG.pm:34
# ``my $GRAPHICS_FRAMEBUF_LIVE = (1 * 280 * 2);`` — 560, and that same value is
# the bitmap size (bytesPerColumn 2 × displayWidth 280, Graphics.pm:93-103).
GRAPHICS_FRAMEBUF_LIVE = 560

# ── Perl slimproto opcode table (4 ASCII bytes) ───────────────────────────
# Slim/Networking/Slimproto.pm:52-72 `%message_handlers`. The opcode is the
# RAW 4-byte name ('IR  ' is space padded), and Perl looks it up in a hash —
# so the comparison is case sensitive. Our old dispatch lower-cased the
# opcode and compared against "bye", which is why a real `BYE!` frame from a
# player fell through to the debug branch (audit B1).
PERL_MESSAGE_HANDLERS = frozenset({
    "ANIC",  # :56  _animation_complete_handler
    "BODY",  # :57  _http_body_handler
    "BUTN",  # :58  _button_handler
    "BYE!",  # :59  _bye_handler
    "DBUG",  # :60  _debug_handler
    "DSCO",  # :61  _disco_handler
    "IR  ",  # :62  _ir_handler (space padded!)
    "KNOB",  # :63  _knob_handler
    "META",  # :64  _http_metadata_handler
    "RAWI",  # :65  _raw_ir_handler
    "RESP",  # :66  _http_response_handler
    "SETD",  # :67  _settings_handler
    "STAT",  # :68  _stat_handler
    "UREQ",  # :69  _update_request_handler
    "ALSS",  # :70  _ambient_light_sensor_handler
    "SHUT",  # :71  _shut_handler (slimprox only)
})

# Perl closes the socket in exactly one handler: Slimproto.pm:939-942
# (``slimproto_close($client->tcpsock)``).
PERL_CLOSE_OPCODES = frozenset({"SHUT"})

# ... and it explicitly does NOT close for these two: BYE! only reacts to a
# firmware-upgrade payload (Slimproto.pm:921-937), DSCO reports a data-channel
# disconnect and keeps the control connection (Slimproto.pm:599-679).
PERL_KEEP_OPEN_OPCODES = frozenset({"BYE!", "DSCO"})

# DSCO disconnect reasons, verbatim from Slimproto.pm:603-609.
DISCO_REASONS = {
    0: "Connection closed normally",               # TCP_CLOSE_FIN
    1: "Connection reset by local host",           # TCP_CLOSE_LOCAL_RST
    2: "Connection reset by remote host",          # TCP_CLOSE_REMOTE_RST
    3: "Connection is no longer able to work",     # TCP_CLOSE_UNREACHABLE
    4: "Connection timed out",                     # TCP_CLOSE_LOCAL_TIMEOUT
}

# Perl handler -> human readable, for the frames whose full semantics are
# still a separate task.
PERL_HANDLER_NAMES = {
    "ANIC": "_animation_complete_handler",
    "BODY": "_http_body_handler",
    "BUTN": "_button_handler",
    "DBUG": "_debug_handler",
    "IR  ": "_ir_handler",
    "KNOB": "_knob_handler",
    "RAWI": "_raw_ir_handler",
    "UREQ": "_update_request_handler",
    "ALSS": "_ambient_light_sensor_handler",
}

# The opcodes that carry a button/IR/knob code and therefore translate into a
# player action (PROT-19). Slimproto.pm:521-546 (_ir_handler), :1270-1280
# (_button_handler) and :1282-1318 (_knob_handler) -> lyrion.player.buttons.
PERL_BUTTON_OPCODES = frozenset({"IR  ", "BUTN", "KNOB"})


def classify_opcode(op_raw: str) -> str:
    """Classify a player frame opcode exactly like Perl's handler lookup.

    ``op_raw`` is the raw 4-byte ASCII opcode (no lower-casing — that is what
    broke ``BYE!``). Returns one of:

    * ``"close"``   — Perl closes the socket here (``SHUT``)
    * ``"bye"``     — ``BYE!``: never closes, may request a firmware upgrade
    * ``"dsco"``    — data-channel disconnect notice, connection stays open
    * ``"helo"``    — (re)handshake
    * ``"handler"`` — a Perl message handler we dispatch explicitly
    * ``"unknown"`` — Perl logs ``Unknown slimproto op`` and keeps the socket
    """
    if op_raw in PERL_CLOSE_OPCODES:
        return "close"
    if op_raw == "BYE!":
        return "bye"
    if op_raw == "DSCO":
        return "dsco"
    if op_raw.upper() == "HELO":
        return "helo"
    if op_raw in PERL_MESSAGE_HANDLERS:
        return "handler"
    return "unknown"


# ---------------------------------------------------------------------------
# Enums / flags
# ---------------------------------------------------------------------------
class PlayerCapabilities(IntFlag):
    """HELO capabilities bitfield."""

    PCM = 0x0001
    FLAC = 0x0002
    NEEDS_16BIT = 0x0004
    DSD_DOP = 0x0008
    DIGITAL_OUT = 0x0010
    IR_RECEIVER = 0x0020
    VORBIS = 0x0040
    CURSOR_KEYS = 0x0080
    KEYBOARD = 0x0100
    VOLUME = 0x0200
    INFRA_RED = 0x0400
    MP3_DECODE = 0x0800
    AAC_DECODE = 0x1000
    ALAC_DECODE = 0x2000
    MP4_DECODE = 0x4000
    COMPRESSED_TRANSPORT = 0x8000


# Reasonable default caps: PCM, FLAC, 16-bit, digital-out, volume, IR,
# MP3+AAC+ALAC+MP4 decode, compressed transport
DEFAULT_CAPABILITIES: int = int(
    PlayerCapabilities.PCM
    | PlayerCapabilities.FLAC
    | PlayerCapabilities.NEEDS_16BIT
    | PlayerCapabilities.DIGITAL_OUT
    | PlayerCapabilities.VOLUME
    | PlayerCapabilities.IR_RECEIVER
    | PlayerCapabilities.MP3_DECODE
    | PlayerCapabilities.AAC_DECODE
    | PlayerCapabilities.ALAC_DECODE
    | PlayerCapabilities.MP4_DECODE
    | PlayerCapabilities.COMPRESSED_TRANSPORT
)


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------
def pack_frame(cmd: int, payload: bytes = b"") -> bytes:
    """Pack a slimproto binary frame.

    Header: 1 byte command ID + 3 bytes big-endian payload length.
    """
    length = len(payload)
    if length > 0xFFFFFF:
        raise ValueError(f"Payload too large for slimproto frame: {length}")
    header = bytes([cmd]) + length.to_bytes(3, "big")
    return header + payload


def unpack_header(data: bytes) -> tuple[int, int]:
    """Unpack a 4-byte slimproto header. Returns (command_id, payload_length)."""
    if len(data) < 4:
        raise ValueError(f"Incomplete header: {len(data)} bytes")
    cmd = data[0]
    length = int.from_bytes(data[1:4], "big")
    return cmd, length


# ---------------------------------------------------------------------------
# Dataclasses for protocol messages
# ---------------------------------------------------------------------------
@dataclass
class HelloMessage:
    """HELO handshake message sent from player to server."""

    device_id: str  # 8 bytes ASCII, padded with spaces
    revision: str  # 8 bytes ASCII firmware revision
    mac: tuple[int, int, int, int, int, int]  # 6 MAC bytes
    capabilities: int  # 4-byte little-endian bitfield
    lang: str  # 4-byte language code, space-padded
    uuid: str  # 8-byte unique ID

    def to_bytes(self) -> bytes:
        dev_id = self.device_id.encode("ascii")[:8].ljust(8)
        rev = self.revision.encode("ascii")[:8].ljust(8)
        mac_bytes = bytes(self.mac)
        # Capabilities: 4-byte little-endian
        cap = struct.pack("<I", self.capabilities)
        lang = self.lang.encode("ascii")[:4].ljust(4)
        uuid = self.uuid.encode("ascii")[:8].ljust(8)
        return dev_id + rev + mac_bytes + cap + lang + uuid


@dataclass
class HelloAck:
    """ACK response from server after HELO."""

    num_extensions: int
    buffer_size: int  # samples
    max_output_channels: int
    supported_commands: int  # bitfield u32
    extensions: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def from_bytes(cls, data: bytes) -> HelloAck:
        if len(data) < 1:
            raise ValueError("HELO response too short")
        num_ext = data[0]
        # Layout: byte(1) + ushort(2) + uint(4) + uint(4) = 11 bytes
        # Offsets: 0        1-2            3-6            7-10
        buf_size = struct.unpack_from("<H", data, 1)[0] if len(data) >= 3 else 0
        max_ch = struct.unpack_from("<I", data, 3)[0] if len(data) >= 7 else 0
        supp_cmds = struct.unpack_from("<I", data, 7)[0] if len(data) >= 11 else 0
        extensions: dict[str, bytes] = {}
        offset = 11  # byte(1) + ushort(2) + uint(4) + uint(4) = 11 bytes
        for _ in range(num_ext):
            if offset + 1 >= len(data):  # need code + length = 2 bytes
                break
            code = chr(data[offset]) if data[offset] < 128 else "?"
            length = data[offset + 1]
            offset += 2
            if offset + length > len(data):  # need 'length' more bytes for value
                break
            value = data[offset : offset + length]
            extensions[code] = value
            offset += length
        return cls(num_ext, buf_size, max_ch, supp_cmds, extensions)


@dataclass
class StatMessage:
    """STAT message — player status update sent periodically to server."""

    crlf_ref: int = 0
    wallclock: int = 0
    stream_buffers: int = 0
    decoded_buffers: int = 0
    output_buffers: int = 0
    cpu: int = 0
    dac: int = 0
    jive: int = 0
    flags: int = 0
    server_timestamp: int = 0
    elapsed_ms: int = 0
    sample_rate: int = 44100
    bit_depth: int = 16
    decoder: int = 0
    player_ip: int = 0
    wifi_mode: int = 0
    wifi_error_rate: int = 0
    wifi_noise: int = 0
    power_state: int = 0
    player_type: str = "T"
    model: str = ""

    def to_bytes(self) -> bytes:
        base = struct.pack(
            "<IIIIIIIIIII",
            self.crlf_ref,
            self.wallclock,
            self.stream_buffers,
            self.decoded_buffers,
            self.output_buffers,
            self.cpu,
            self.dac,
            self.jive,
            self.flags,
            self.server_timestamp,
            self.elapsed_ms,
        )
        extra = struct.pack(
            "<IIIII",
            self.sample_rate,
            self.bit_depth,
            self.decoder,
            self.player_ip,
            self.wifi_mode,
        )
        extra2 = struct.pack(
            "<IIH",
            self.wifi_error_rate,
            self.wifi_noise,
            self.power_state,
        )
        tail = self.player_type.encode("ascii")[:1] + self.model.encode("ascii")
        return base + extra + extra2 + tail

    def to_frame(self) -> bytes:
        return pack_frame(CMD_STAT, self.to_bytes())


@dataclass
class EventMessage:
    """EVNT message — server → player event/notification."""

    code: int  # event subtype code
    payload: bytes = b""

    def to_frame(self) -> bytes:
        return pack_frame(CMD_EVNT, bytes([self.code]) + self.payload)


# ---------------------------------------------------------------------------
# SlimProtoClient
# ---------------------------------------------------------------------------

#: A disconnected player is forgotten (removed from the registry) after this
#: many seconds unless it reconnects first (Perl Slimproto.pm
#: ``$forget_disconnected_time = 300``).
FORGET_DISCONNECTED_TIME = 300


class SlimProtoClient:
    """Async slimproto client — connects a player (or emulated player) to LMS.

    Supports both player-mode (connects to server) and server-mode
    (accepts connections from players).

    Events are dispatched via callback handlers registered per command ID.
    """

    def __init__(
        self,
        device_id: str = "Lyrion  ",
        revision: str = "9.2.0  ",
        mac: tuple[int, int, int, int, int, int] = (0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
        capabilities: int = DEFAULT_CAPABILITIES,
        lang: str = "EN  ",
        uuid: str = "LYR00001",
        web_port: int = 9000,
    ):
        self.device_id = device_id
        self.revision = revision
        self.mac = mac
        self.capabilities = capabilities
        self.lang = lang
        self.uuid = uuid
        # HTTP port of THIS server's web/stream endpoint. The strm frame
        # tells the player where to fetch /stream.mp3 — LMS sends its own
        # httpport here (Squeezebox.pm stream_s: $server_port = $prefs->get('httpport')).
        self.web_port = web_port

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

        # Callback handlers: command_id -> Callable[[bytes], None | Awaitable[None]]
        self._handlers: dict[int, Callable[[bytes], None | Coroutine[Any, Any, None]]] = {}

        # Internal response futures for synchronous-style calls
        self._resp_futures: dict[int, asyncio.Future[bytes]] = {}

        # Server-side: player MAC -> StreamWriter (for sending frames to players)
        self._player_writers: dict[str, asyncio.StreamWriter] = {}

        # Direct-stream RESP waiters: MAC -> Future resolved with icy-metaint
        # when the player reports the source's response headers (RESP frame).
        self._resp_waiters: dict[str, asyncio.Future[int]] = {}

        # Open TCP connections per player MAC. Squeezelite keeps TWO
        # connections (control + data); closing one must NOT mark the
        # player disconnected while the other is alive (that broke
        # stop/play: "Cannot send command to disconnected player").
        self._player_connections: dict[str, int] = {}

        # Perl's %heartbeat (Slim/Networking/Slimproto.pm:43/:1223): the last
        # time we HEARD from a client. `_stat_handler` refreshes it for every
        # inbound frame (:703-709) and `check_all_clients` (:199-241) closes
        # the socket of a client that stayed silent for 3 poll intervals.
        self._last_heard: dict[str, float] = {}

        # Forget tasks for disconnected players (Perl forget_disconnected_client):
        # MAC-key -> task that unregisters the player after the grace period.
        self._forget_tasks: dict[str, asyncio.Task] = {}

        # Server-side fields (populated after HELO)
        self._server_buffer_size: int = 0
        self._server_max_channels: int = 0
        self._server_supported_commands: int = 0
        self._server_extensions: dict[str, bytes] = {}

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(
        self,
        host: str,
        port: int = SLIMPROTO_PORT,
        timeout: float = 10.0,
    ) -> None:
        """Connect as a player to an LMS server."""
        logger.info("Connecting to LMS at %s:%d", host, port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        self._reader = reader
        self._writer = writer
        self._connected = True
        logger.info("TCP connection established")

        # Send HELO
        hello = HelloMessage(
            device_id=self.device_id,
            revision=self.revision,
            mac=self.mac,
            capabilities=self.capabilities,
            lang=self.lang,
            uuid=self.uuid,
        )
        await self._send_frame(CMD_HELO, hello.to_bytes())
        logger.info("HELO sent")

        # Read HELO ACK
        ack_data = await self._read_frame()
        ack_cmd, ack_payload = unpack_header(ack_data)
        if ack_cmd != 0:
            raise RuntimeError(f"Expected HELO ACK (0x00), got 0x{ack_cmd:02X}")

        ack = HelloAck.from_bytes(ack_payload)
        self._server_buffer_size = ack.buffer_size
        self._server_max_channels = ack.max_output_channels
        self._server_supported_commands = ack.supported_commands
        self._server_extensions = ack.extensions
        logger.info(
            "HELO ack: buffer=%d max_ch=%d supp_cmds=0x%08X",
            ack.buffer_size,
            ack.max_output_channels,
            ack.supported_commands,
        )

        # Start reader loop
        self._reader_task = asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        """Send BYE and close the connection gracefully."""
        if self._connected:
            try:
                await self._send_frame(CMD_BYE, b"")
            except Exception as exc:
                logger.warning("Error sending BYE: %s", exc)
        self._connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), timeout=3.0)
        self._reader = None
        self._writer = None
        logger.info("Disconnected")

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
  # Framing
    # ------------------------------------------------------------------

    async def _send_frame(self, cmd: int, payload: bytes = b"") -> None:
        """Send a binary frame. Thread-safe via lock."""
        if self._writer is None:
            raise RuntimeError("Not connected")
        frame = pack_frame(cmd, payload)
        async with self._lock:
            self._writer.write(frame)
            await self._writer.drain()

    async def _read_frame(self) -> bytes:
        """Read exactly one binary frame (header + payload)."""
        if self._reader is None:
            raise RuntimeError("Not connected")

        header = await self._reader.readexactly(4)
        cmd, length = unpack_header(header)

        if length == 0:
            return header

        payload = b""
        while len(payload) < length:
            chunk = await self._reader.readexactly(length - len(payload))
            payload += chunk

        return header + payload

    async def _read_loop(self) -> None:
        """Background task: read frames and dispatch to handlers."""
        while self._connected:
            try:
                frame = await self._read_frame()
            except asyncio.CancelledError:
                break
            except ConnectionResetError:
                logger.info("Connection reset by peer")
                break
            except Exception as exc:
                logger.error("Frame read error: %s", exc)
                break

            try:
                cmd, length = unpack_header(frame[0:4])
                payload = frame[4:]
            except Exception as exc:
                logger.error("Malformed frame: %s", exc)
                continue

            # Handle built-in responses first
            if cmd == CMD_RESP:
                # CLI response — resolve pending futures
                future_id = 0  # TODO: use request ID from RESP
                for fid, fut in list(self._resp_futures.items()):
                    if not fut.done():
                        fut.set_result(payload)
                        del self._resp_futures[fid]
                    break
                continue

            # Dispatch to registered handlers
            handler = self._handlers.get(cmd)
            if handler:
                try:
                    result = handler(payload)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.error("Handler for cmd 0x%02X raised: %s", cmd, exc)
            else:
                logger.debug("Unhandled slimproto cmd 0x%02X len=%d", cmd, length)

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def on(self, cmd: int, handler: Callable[[bytes], None | Coroutine[Any, Any, None]]) -> None:
        """Register a handler for a command ID."""
        self._handlers[cmd] = handler

    def off(self, cmd: int) -> None:
        """Remove handler for a command ID."""
        self._handlers.pop(cmd, None)

    # ------------------------------------------------------------------
    # CLI commands — send text commands over the binary channel
    # ------------------------------------------------------------------

    async def cli(
        self,
        *args: str,
        timeout: float = 5.0,
    ) -> str:
        """Send a CLI command over the slimproto channel.

        Format: "COMMAND arg1 arg2 ...\n"
        Returns the decoded text response.
        """
        if not self._connected:
            raise RuntimeError("Not connected")

        cmd_line = " ".join(str(a) for a in args) + "\r\n"
        payload = cmd_line.encode("utf-8")
        await self._send_frame(CMD_EVNT, payload)  # EVNT used for CLI in practice

        # Wait for RESP
        fut: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
        # Simple: just read one RESP frame (blocking for this call)
        # We handle this in the read loop instead
        try:
            resp_payload = await asyncio.wait_for(
                self._wait_for_resp(), timeout=timeout
            )
            return resp_payload.decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            raise TimeoutError(f"CLI command timed out: {' '.join(args)}")

    async def _wait_for_resp(self) -> bytes:
        """Wait for the next RESP frame."""
        loop = asyncio.get_event_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        fid = id(future)
        self._resp_futures[fid] = future
        try:
            return await future
        finally:
            self._resp_futures.pop(fid, None)

    # ------------------------------------------------------------------
    # Server-side (accepting player connections)
    # ------------------------------------------------------------------

    async def serve(
        self,
        host: str = "0.0.0.0",
        port: int = SLIMPROTO_PORT,
    ) -> None:
        """Server-mode: accept connections from players."""
        server = await asyncio.start_server(
            self._handle_player,
            host,
            port,
            reuse_address=True,
        )
        logger.info("Slimproto server listening on %s:%d", host, port)
        async with server:
            await server.serve_forever()

    async def _handle_player(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming player connection (server-side)."""
        peer = writer.get_extra_info("peername")
        logger.info("Player connected from %s", peer)
        buf = b""

        # Track the parsed HELO for cleanup
        hello: HelloMessage | None = None
        keepalive_task: asyncio.Task | None = None
        # Set when the player sent 'dsco' (end-of-stream): the player keeps
        # its identity/playlist across the reconnect that follows (LMS
        # behaviour). Only a real disconnect (bye/TCP-close) unregisters.
        keep_registered = False

        try:
            # Read first byte to detect protocol
            first_byte = await reader.readexactly(1)
            
            # ── Squeezelite / SB Player HELO (Ralph Irving fork format) ──
            # Wire format: "HELO" (4B) + length (u32 BE) + deviceid (u8) + revision (u8)
            # + mac (6B) + uuid (16B) + wlan_channellist (u16 BE)
            # + bytes_received_H (u32 BE) + bytes_received_L (u32 BE) + lang (2B)
            # + capabilities text (remaining bytes up to `length`)
            if first_byte[0] == 0x48:  # 'H' = "HELO" opcode
                # Read "ELO" (3B) + length (4B BE). The declared length is the
                # body size (Perl Slimproto: 36 bytes with uuid, 20 without;
                # anything beyond that is the capabilities text).
                prefix = await reader.readexactly(7)  # "ELO" + length
                if prefix[0:3] != b"ELO":
                    logger.warning("Bad HELO header from %s: expected ELO, got %s", peer, prefix[0:3].hex())
                    writer.close()
                    return
                length_be = int.from_bytes(prefix[3:7], "big")  # 4-byte big-endian length
                # Older firmware (no uuid) sends a 20-byte body; newer sends 36.
                has_uuid = length_be >= 36
                fixed_len = 36 if has_uuid else 20
                fixed = await reader.readexactly(fixed_len)
                offset = 0
                deviceid = fixed[offset]; offset += 1
                revision = fixed[offset]; offset += 1
                mac_raw = fixed[offset:offset + 6]; offset += 6
                uuid_raw = fixed[offset:offset + 16] if has_uuid else b""; offset += 16 if has_uuid else 0
                wlan = int.from_bytes(fixed[offset:offset + 2], "big"); offset += 2
                br_h = int.from_bytes(fixed[offset:offset + 4], "big"); offset += 4
                br_l = int.from_bytes(fixed[offset:offset + 4], "big"); offset += 4
                lang_raw = fixed[offset:offset + 2].decode("ascii", errors="replace"); offset += 2
                # Capabilities = remaining bytes beyond the fixed body.
                cap_bytes_len = length_be - fixed_len
                cap_text = ""
                if cap_bytes_len > 0 and cap_bytes_len < 65536:  # sanity check
                    cap_text = (await reader.readexactly(cap_bytes_len)).decode("ascii", errors="replace")
                mac_str = ":".join(f"{b:02X}" for b in mac_raw)
                # Parse capabilities: Model=<type> is the device type
                # (squeezeplay/squeezelite/...), ModelName=<display> is the
                # client-assigned display name (some clients, e.g. the
                # Taverne SqueezePlay, put their player name there).
                model = "squeezelite"
                display_name = ""
                firmware = ""
                can_https = False
                for part in cap_text.split(","):
                    part = part.strip()
                    if part.startswith("Model="):
                        model = part[6:]
                    elif part.startswith("ModelName="):
                        # Perl: SqueezePlay.pm:79-85 maps the HELO caps
                        # ModelName to _modelName (the players loop
                        # 'modelname' field) and Firmware to 'firmware'.
                        # jive sends e.g. ModelName=SB Player,
                        # Firmware=9.0.0-r1583 — NOT the player name.
                        display_name = part[10:]
                    elif part.startswith("Firmware="):
                        firmware = part[9:]
                    elif part == "CanHTTPS=1":
                        # Player can do TLS itself (SqueezeLite/ESP32 builds
                        # with OpenSSL, SqueezePlay). https radio streams may
                        # then be streamed DIRECTLY with the SSL flag; without
                        # this cap they are proxied by the server (Perl LMS:
                        # HTTP.pm canDirectStream + HTTPS.pm slimprotoFlags).
                        can_https = True

                logger.info(
                    "HELO from %s: model=%s display=%s mac=%s len=%d caps=%s",
                    peer, model, display_name, mac_str, length_be, cap_text[:80]
                )
                # Full capability string at debug level: the codec list the
                # player declares lives here (Perl parses it)
                # SqueezePlay.pm:170-200 — every comma-separated token that
                # matches /^[a-z][a-z0-9]{1,4}$/ is a format it can decode.
                logger.debug("HELO caps (full, %d chars): %s",
                             len(cap_text), cap_text)

                # ── Server response: binary 'vers' frame (NOT text!) ──
                # Server → player framing (from LMS Slim/Player/Squeezebox.pm sendFrame):
                #   pack('n', len(payload)+4) + 4-byte ASCII opcode + payload
                from lyrion import __version__
                vers_payload = __version__.encode("ascii", errors="replace")
                server_frame = struct.pack(">H", len(vers_payload) + 4) + b"vers" + vers_payload
                writer.write(server_frame)
                await writer.drain()
                logger.info("Sent 'vers' frame to %s (%s)", model, mac_str)

                # ── Ask the player for its name (SETD query, id=0) ──
                # Squeezelite/SqueezeESP32 only send their assigned name in
                # response to a SETD name query. Payload = pack('C', 0) →
                # single id byte (client sees len==5 → replies with
                # SETD(id=0, name\0)). Matches LMS getPlayerSetting:
                #   $data = pack('C', firmwareid=0); sendFrame('setd', \$data)
                try:
                    setd_payload = bytes([0])  # id=0, 1 byte
                    setd_frame = struct.pack(">H", 4 + len(setd_payload)) + b"setd" + setd_payload
                    writer.write(setd_frame)
                    await writer.drain()
                    logger.info("Sent SETD name query to %s", mac_str)
                except Exception as exc:
                    logger.warning("SETD query failed for %s: %s", mac_str, exc)

                mac_key = mac_str.replace(":", "").upper()
                # Register writer so play/stop commands can reach this player
                # (key normalized: uppercase, no colons — same as
                # PlayerManager). A (re)connect also drops a stale strm guard.
                self._register_player_writer(mac_key, writer)

                # Start keepalive: Squeezelite declares the connection dead after
                # ~35s without any server message ("No messages from server -
                # connection dead"). Real LMS sends periodic frames. A 'setd'
                # frame with display id > 0 is ignored by squeezelite (no
                # display support) but resets its receive timeout.
                keepalive_task = asyncio.create_task(
                    self._keepalive_loop(writer, mac_str)
                )

                # Track for disconnect
                class TempHello:
                    def __init__(self, m, d, r):
                        self.mac = tuple(int(b, 16) for b in m.split(":"))
                        self.device_id = d
                        self.revision = r
                hello = TempHello(mac_str, model[:8], str(revision)[:8])

                # HELO uuid: Perl unpacks it as H32 (16 bytes -> 32 hex
                # chars, Slimproto.pm:962) and drops an all-zero uuid
                # (Client.pm:167-168 "if ($uuid =~ /0000000000/) { undef }").
                uuid_str = ""
                if uuid_raw and uuid_raw != b"\x00" * 16:
                    uuid_str = uuid_raw.hex().lower()

                # Register with PlayerManager
                try:
                    from lyrion.player.manager import (
                        PlayerManager,
                        formats_from_capabilities,
                    )
                    peer_ip = peer[0] if peer else "unknown"
                    reg_name = display_name or model
                    # A ModelName that merely repeats the device type
                    # (e.g. ModelName=SqueezePlay with Model=squeezeplay) is
                    # device identity, not a real player name — rank it low
                    # so a second connection carrying the real name wins.
                    if display_name and display_name.lower() == model.lower():
                        src = "device"
                    else:
                        src = "display" if display_name else "device"
                    PlayerManager().register_player(
                        mac=mac_str, name=reg_name, ip=peer_ip,
                        port=peer[1] if peer else 0, model=model,
                        # Perl reports the caps Firmware token here
                        # (SqueezePlay.pm:85); fall back to the HELO
                        # revision byte, which is what a client without the
                        # cap sends (jive: 0).
                        firmware=firmware or str(revision),
                        name_source=src, can_https=can_https,
                        # HELO uuid: 16 bytes -> 32 lowercase hex chars.
                        # All-zero means "no uuid" (Client.pm:167-168).
                        uuid=uuid_str,
                        model_name=display_name,
                        # The player DECLARES its codecs in the HELO caps
                        # string (Perl SqueezePlay.pm:170-200); fall back to
                        # the model's static list only when it declares none.
                        supported_formats=formats_from_capabilities(
                            cap_text, model),
                    )
                    # Reconnect: cancel any pending forget-disconnected timer
                    # so the player's state (playlist/volume/position) survives.
                    self._cancel_forget(mac_str)
                    logger.info("Squeezelite player registered: %s (%s) model=%s src=%s", reg_name, mac_str, model, src)
                except Exception as exc:
                    logger.warning("Squeezelite register failed: %s", exc)

                # Wake /slim/serverstatus Cometd subscribers (player list changed)
                _notify_cometd_server_status()

                # ── Sync volume like the real LMS (audg frame) ──
                # Squeezelite zero-initialises its internal gain; until an
                # audg frame arrives all audio is multiplied by 0 → the
                # player decodes but outputs silence. The Perl LMS pushes
                # the current volume to every newly connected player.
                try:
                    from lyrion.player.manager import PlayerManager, _formats_for_model
                    pstate = PlayerManager().get_player(mac_str)
                    if pstate is not None:
                        await self.send_volume_to_player(mac_str, pstate.volume)
                        logger.info("Sent audg volume=%d to %s on connect", pstate.volume, mac_str)
                except Exception as exc:
                    logger.warning("Volume sync failed for %s: %s", mac_str, exc)

                # ── Audio outputs enable ('aude'), same Perl HELO block ──
                # Slimproto.pm:1265 audio_outputs_enable($client->power())
                try:
                    await self._send_aude_state(mac_str)
                except Exception as exc:
                    logger.warning("aude state failed for %s: %s", mac_str, exc)

                # ── Display on connect (PROT-18) ──
                # Perl Player.pm:114-124 + Squeezebox.pm:124-134: after the aude
                # state it sets brightness(powerOn/OffBrightness) and forces the
                # visualiser (Squeezebox.pm:134, the only $forceSend call). A
                # NoDisplay client returns at Player.pm:114 — before all of it.
                try:
                    from lyrion.player.manager import PlayerManager as _DisplayPM
                    await _DisplayPM().display_on_connect(mac_str)
                except Exception as exc:
                    logger.debug("display on connect failed for %s: %s", mac_str, exc)

                # ── Read loop: binary slimproto frames from player ──
                # Player → server framing (from LMS Slim/Networking/Slimproto.pm
                # client_readable): 4-byte ASCII opcode + 4-byte BE length + payload.
                # NOTE: this differs from the server → player framing (2-byte length
                # including opcode). The protocol is asymmetric.
                while True:
                    # Perl decides on the close that just happened, not on an
                    # earlier frame: a socket close is a disconnect unless it
                    # was an explicit reconnect-close (Slimproto.pm:255-296).
                    # Without this reset a player that once sent DSCO/BYE!/IR
                    # before vanishing kept the stale flag and was never
                    # forgotten (its forget timer is only armed on the
                    # disconnect path below, Slimproto.pm:289-296).
                    keep_registered = False
                    try:
                        header = await reader.readexactly(8)
                    except asyncio.IncompleteReadError:
                        logger.info("Player %s closed connection", mac_str)
                        break
                    opcode_raw = header[0:4]
                    plen = int.from_bytes(header[4:8], "big")
                    if plen > 0xFFFFFF:
                        logger.warning("Oversized frame from %s: op=%r len=%d", peer, opcode_raw, plen)
                        break
                    payload = await reader.readexactly(plen) if plen else b""
                    # Perl's _stat_handler: any frame from the player proves it
                    # is alive (`$heartbeat{$client->id} = $now`,
                    # Slimproto.pm:703-709). check_all_clients closes a client
                    # that stopped answering the `strm 't'` poll for 3
                    # intervals (:218-238).
                    self._heartbeats()[mac_key] = time.monotonic()
                    # Perl looks the opcode up in %message_handlers as the RAW
                    # 4-byte name (case sensitive, 'IR  ' space padded). Keep
                    # the raw form for that lookup; the lower-case form serves
                    # the text-HELO aliases of our own client.
                    op_raw = opcode_raw.decode("ascii", errors="replace")
                    op = op_raw.lower()
                    if op == "stat":
                        self._handle_stat_frame(mac_str, payload)
                    elif op == "resp":
                        # Direct stream: player forwards the source's HTTP
                        # response headers — extract icy-metaint and send
                        # the 'cont' frame that starts the decoder.
                        self._handle_resp_frame(mac_str, payload)
                    elif op in ("meta", "stmu"):
                        # Squeezelite pushes the source's StreamTitle over a
                        # 'meta' frame (id 0) — the metadata update. Parse it
                        # so the UI shows the real song + artist without
                        # proxying the audio. (Some builds use 'stmu'.)
                        self._handle_stmu_frame(mac_str, payload) if op == "stmu" else self._handle_meta_frame(mac_str, payload)
                    elif op == "setd":
                        # SETD frame — id 0 carries the player's assigned name
                        if payload:
                            setd_id = payload[0]
                            if setd_id == 0 and len(payload) > 1:
                                raw = payload[1:].split(b"\x00")[0]
                                try:
                                    new_name = raw.decode("utf-8").strip()
                                except UnicodeDecodeError:
                                    # SqueezePlay sends the name in latin-1
                                    # (e.g. "Küche" -> b'K\xfcche'); utf-8
                                    # would mangle it to "K�che".
                                    new_name = raw.decode("latin-1").strip()
                                if new_name:
                                    logger.info("Player %s sends name via SETD: %r", mac_str, new_name)
                                    try:
                                        from lyrion.player.manager import PlayerManager, _formats_for_model
                                        PlayerManager().rename_player(mac_str, new_name)
                                    except Exception as exc:
                                        logger.warning("SETD rename failed for %s: %s", mac_str, exc)
                    elif op in ("bye", "dsco", "quit") or op_raw in PERL_KEEP_OPEN_OPCODES \
                            or op_raw in PERL_CLOSE_OPCODES or op_raw in PERL_HANDLER_NAMES:
                        # Perl's dispatch: the opcode is looked up in
                        # %message_handlers as the RAW 4-byte name
                        # (Slimproto.pm:425-434). Behaviour per handler:
                        #   SHUT  -> slimproto_close (:939-942)  = close
                        #   BYE!  -> firmware-upgrade request, NO close (:921-937)
                        #   DSCO  -> data-channel disconnect, NO close (:599-679)
                        #   IR  /BUTN/KNOB/RAWI/ANIC/ALSS/UREQ/DBUG/BODY -> handler,
                        #           connection stays open
                        kind = classify_opcode(op_raw)
                        if kind == "dsco" or op == "dsco":
                            self._handle_dsco_frame(mac_str, payload)
                            keep_registered = True
                            continue
                        if kind == "bye":
                            self._handle_bye_frame(mac_str, payload)
                            keep_registered = True
                            continue
                        if kind == "close":
                            logger.info(
                                "Player %s sent SHUT — closing (Perl _shut_handler)",
                                mac_str)
                            break
                        if kind == "handler":
                            self._handle_player_event_frame(mac_str, op_raw, payload)
                            keep_registered = True
                            continue
                        # our text-protocol extras (no Perl counterpart:
                        # audit B3/B4 — the text path of our own client)
                        logger.info("Player %s sent '%s' — closing connection", mac_str, op)
                        break
                    elif op == "helo":
                        # Player re-sent HELO (reconnect after control drop) — reply again
                        writer.write(server_frame)
                        await writer.drain()
                    else:
                        # Perl Slimproto.pm:432 "Unknown slimproto op" — and it
                        # keeps the socket open.
                        logger.warning("Unknown slimproto op: %r from %s (%d bytes)",
                                       op_raw, mac_str, plen)
                return
            
            # ── Binary SlimProto HELO ──
            header = first_byte + await reader.readexactly(3)
            cmd, length = unpack_header(header)
            if cmd != CMD_HELO:
                logger.warning("Expected HELO from player, got 0x%02X", cmd)
                return

            payload = await reader.readexactly(length)
            hello = HelloMessage(
                device_id=payload[0:8].decode("ascii", errors="replace").strip(),
                revision=payload[8:16].decode("ascii", errors="replace").strip(),
                mac=tuple(payload[16:22]),
                capabilities=struct.unpack_from("<I", payload, 22)[0],
                lang=payload[26:30].decode("ascii", errors="replace").strip(),
                uuid=payload[30:38].decode("ascii", errors="replace").strip(),
            )
            logger.info(
                "HELO from player: device=%s rev=%s mac=%s uuid=%s caps=0x%08X",
                hello.device_id,
                hello.revision,
                ":".join(f"{b:02X}" for b in hello.mac),
                hello.uuid,
                hello.capabilities,
            )

            # Send HELO ACK — num_ext(1) + buffer_size(2) + max_channels(4)
            # + supported_commands(4) = 11 bytes. The spec-complete layout
            # lets strict players parse the u32 at offset 7 (previously the
            # field was missing entirely).
            supported_commands = 0x00000007  # strm|audg|aude (audio frames)
            ack_payload = struct.pack(
                "<BHII",
                0,  # no extensions for now
                8192,  # buffer size
                2,  # stereo
                supported_commands,
            )
            writer.write(pack_frame(0, ack_payload))
            await writer.drain()

            # Jive controllers (SqueezeControl, iPeng, SqueezePlay) expect
            # the same greeting as players: 'vers' + SETD name query —
            # mirror the text-HELO path below.
            try:
                from lyrion import __version__
                vers_payload = __version__.encode("ascii", errors="replace")
                writer.write(struct.pack(">H", len(vers_payload) + 4) + b"vers" + vers_payload)
                setd_frame = struct.pack(">H", 5) + b"setd" + bytes([0])
                writer.write(setd_frame)
                await writer.drain()
                logger.info("Sent vers + SETD to %s (binary HELO)", hello.device_id)
            except Exception as exc:
                logger.warning("vers/SETD send failed for %s: %s", hello.device_id, exc)

            # Register this player with the PlayerManager
            mac_formatted = ":".join(f"{b:02X}" for b in hello.mac)
            # The binary HELO path must populate the same writer registry as
            # the Squeezelite/ASCII-HELO path (playback commands use this map),
            # and a (re)connect drops a stale strm guard.
            mac_key = mac_formatted.replace(":", "").upper()
            self._register_player_writer(mac_key, writer)
            model_name = hello.device_id.strip() or "squeezebox"
            player_ip = peer[0] if peer else "unknown"
            player_port = peer[1] if peer else 0
            try:
                from lyrion.player.manager import PlayerManager, _formats_for_model
                PlayerManager().register_player(
                    mac=mac_formatted,
                    name=model_name,
                    ip=player_ip,
                    port=player_port,
                    model=model_name,
                    firmware=hello.revision.strip(),
                    # Legacy binary HELO (SB1-era hardware): no codec caps
                    # string in the frame, so the model's Perl list applies.
                    supported_formats=_formats_for_model(model_name),
                )
                logger.info("Player registered via SlimProto: %s (%s)", mac_formatted, player_ip)
            except Exception as exc:
                logger.warning("Could not register player %s: %s", mac_formatted, exc)
            # Wake /slim/serverstatus Cometd subscribers (player list changed)
            _notify_cometd_server_status()

            # ── Sync volume like the real LMS (audg frame) ──
            # Same as the text-HELO path above: without an audg frame the
            # player's internal gain stays 0 and everything is silent.
            try:
                pstate = PlayerManager().get_player(mac_formatted)
                if pstate is not None:
                    await self.send_volume_to_player(mac_formatted, pstate.volume)
                    logger.info("Sent audg volume=%d to %s on connect (binary HELO)",
                                pstate.volume, mac_formatted)
            except Exception as exc:
                logger.warning("Volume sync failed for %s: %s", mac_formatted, exc)

            # ── Audio outputs enable ('aude') — same Perl HELO block ──
            # Slimproto.pm:1265 audio_outputs_enable($client->power())
            try:
                await self._send_aude_state(mac_formatted)
            except Exception as exc:
                logger.warning("aude state failed for %s: %s", mac_formatted, exc)

            # ── Display on connect (PROT-18) ──
            # Same Perl block as the binary path: Player.pm:114-124 +
            # Squeezebox.pm:124-134 (brightness + forced visualiser).
            try:
                from lyrion.player.manager import PlayerManager as _DisplayPM
                await _DisplayPM().display_on_connect(mac_formatted)
            except Exception as exc:
                logger.debug("display on connect failed for %s: %s", mac_formatted, exc)

            # Read loop for this player
            while True:
                frame = await self._read_single_frame_from_reader(reader)
                if not frame:
                    break
                cmd, length = unpack_header(frame[0:4])
                payload_data = frame[4:]
                handler = self._handlers.get(cmd)
                if handler:
                    try:
                        result = handler(payload_data)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        logger.error("Server handler for cmd 0x%02X: %s", cmd, exc)
                else:
                    logger.debug("Server received cmd 0x%02X len=%d", cmd, length)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Player handler error (%s): %s", peer, exc)
        finally:
            if keepalive_task is not None:
                keepalive_task.cancel()
            # Deregister writer + decrement connection count for this player.
            # Perl keeps ONE live slimproto socket per client: a HELO for a
            # known MAC closes the previous socket with the 'reconnect' flag
            # (Slimproto.pm:1195) and that close runs NO disconnect block
            # (:261-296) — the client is alive on the new socket. Only a
            # socket that is still the registered one may mark the player
            # offline / arm the forget timer.
            try:
                if hello is not None:
                    mac_clean = ":".join(f"{b:02X}" for b in hello.mac)
                    key = mac_clean.replace(":", "").upper()
                    registered = self._player_writers.get(key)
                    if registered is not None and registered is not writer:
                        logger.debug(
                            "Superseded SlimProto socket for %s closed "
                            "(reconnect — no disconnect, Slimproto.pm:1195)",
                            mac_clean)
                    else:
                        count = self._player_connections.get(key, 1) - 1
                        if count <= 0:
                            self._player_connections.pop(key, None)
                            if registered is writer:
                                self._player_writers.pop(key, None)
                            self._heartbeats().pop(key, None)
                            # The player is gone: it cannot still hold a stream
                            # we sent (R0.5-P1), so drop the guard as well.
                            self._reset_strm_guard(key, "player disconnected")
                            from lyrion.player.manager import PlayerManager
                            if keep_registered:
                                # DSCO end-of-stream: the player reconnects
                                # immediately. Mark offline but KEEP the state
                                # (playlist/volume) so the reconnect restores it.
                                p = PlayerManager().get_player(mac_clean)
                                if p is not None:
                                    p.connected = False
                            else:
                                # Perl forget_disconnected_client semantics: keep the
                                # player's state (playlist/volume/position) across the
                                # disconnect, mark it offline, and forget it after the
                                # grace period unless it reconnects first.
                                p = PlayerManager().get_player(mac_clean)
                                if p is not None:
                                    p.connected = False
                                # Perl Slimproto.pm:272 notifies 'client disconnect'
                                # on the close (right before arming the forget
                                # timer, :289-296) — a controller that only watches
                                # the notification stream would otherwise still list
                                # a player that is gone.
                                try:
                                    from lyrion.control.notifications import (
                                        notify_from_array,
                                    )

                                    notify_from_array(mac_clean, ["client", "disconnect"])
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug("client disconnect notify failed for %s: %s",
                                                 mac_clean, exc)
                                self._schedule_forget(mac_clean)
                                logger.info("Player disconnected (forget in %ds): %s",
                                            FORGET_DISCONNECTED_TIME, mac_clean)
                                _notify_cometd_server_status()
                        else:
                            self._player_connections[key] = count
            except Exception:
                pass
            writer.close()
            await writer.wait_closed()
            logger.info("Player disconnected: %s", peer)

    def _schedule_forget(self, mac_clean: str) -> None:
        """Schedule unregistration of a disconnected player after the grace
        period (Perl ``forget_disconnected_client``). Cancelled on reconnect.
        """
        key = mac_clean.replace(":", "").upper()

        async def _forget() -> None:
            await asyncio.sleep(FORGET_DISCONNECTED_TIME)
            self._forget_tasks.pop(key, None)
            try:
                from lyrion.player.manager import PlayerManager
                p = PlayerManager().get_player(mac_clean)
                # Only forget players that are still offline — a player that
                # reconnected between scheduling and firing must survive.
                if p is not None and not p.connected:
                    PlayerManager().unregister_player(mac_clean)
                    logger.info("Forgot disconnected player: %s", mac_clean)
                    _notify_cometd_server_status()
            except Exception as exc:  # noqa: BLE001
                logger.warning("forget disconnected player failed: %s", exc)

        existing = self._forget_tasks.pop(key, None)
        if existing is not None:
            existing.cancel()
        self._forget_tasks[key] = asyncio.create_task(_forget())

    def _cancel_forget(self, mac_clean: str) -> None:
        """Cancel a pending forget task (called when a player reconnects).

        Perl does the same on every HELO of a known client:
        ``Slim::Utils::Timers::killTimers($client, \\&forget_disconnected_client)``
        (Slimproto.pm:1198) — a reconnecting client must never be forgotten,
        even if its grace period has almost run out.
        """
        key = mac_clean.replace(":", "").upper()
        tasks = getattr(self, "_forget_tasks", None)
        if tasks is None:               # unit tests build clients via __new__
            self._forget_tasks = tasks = {}
        task = tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _read_single_frame_from_reader(
        self,
        reader: asyncio.StreamReader,
    ) -> bytes:
        """Read one frame from a StreamReader."""
        header = await reader.readexactly(4)
        cmd, length = unpack_header(header)
        if length == 0:
            return header
        payload = b""
        while len(payload) < length:
            chunk = await reader.readexactly(length - len(payload))
            payload += chunk
        return header + payload

    async def _keepalive_loop(
        self,
        writer: asyncio.StreamWriter,
        mac_str: str,
    ) -> None:
        """Perl's player heartbeat: `strm 't'` every 5 s.

        Perl polls every client with ``requestStatus()`` — which is exactly
        ``stream('t')`` (Squeezebox2.pm:383-385) — from the
        ``check_all_clients`` timer, which runs every
        ``$check_all_clients_time = 5`` seconds and drops a player that has
        not answered for 3 intervals (Slimproto.pm:40, :199-241). The frame
        body is the generic ``stream($command)`` layout with replayGain 0
        (Squeezebox.pm:1089-1091, :1096-1114).

        The previous implementation wrote ``setd`` id=1 every 10 s. That is
        NOT a heartbeat in Perl: firmwareid 1 is `digitalOutputEncoding`
        (Squeezebox2.pm:916-919), i.e. we were pushing value 0 into a real
        player setting. A `strm 't'` frame cannot change any setting.

        The second half of Perl's contract is the drop: ``Slimproto.pm:218-238``
        closes the socket of a client whose ``$heartbeat`` value is
        ``check_all_clients_time * 3`` seconds old::

            my $last_heard = $now - $heartbeat{ $client->id };
            if ( $last_heard >= $check_all_clients_time * 3 ... ) {
                $log->info("Haven't heard from $client->id in $last_heard seconds, closing connection");
                slimproto_close( $client->tcpsock );
            }

        Without it a socket that dies without a FIN (WiFi drop, killed player,
        NAT timeout) keeps its register entry forever: ``players`` reports
        ``connected=1``, every ``strm`` is written into a black hole (no RESP,
        no STM, the client's title freezes) and the player never comes back
        because the server still believes it holds the stream. Closing here
        lets the normal close path in ``_handle_player`` run the disconnect
        block (``disconnected(1)``, ``['client','disconnect']``, forget timer —
        Slimproto.pm:265-296), exactly like Perl's ``slimproto_close`` call.
        """
        key = mac_str.upper().replace(":", "")
        keepalive_frame = self._build_strm_control_frame("t")
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_SECONDS)
                if writer.is_closing():
                    break
                # Perl re-reads $check_all_clients_time each pass; read the
                # module globals per iteration so a shorter poll interval
                # (tests) applies without restarting the loop.
                last_heard = self._heartbeats().get(key)
                if last_heard is not None and (
                    time.monotonic() - last_heard
                    >= KEEPALIVE_SECONDS * KEEPALIVE_MISS_LIMIT
                ):
                    logger.info(
                        "Haven't heard from %s in %d seconds, closing connection",
                        mac_str, int(time.monotonic() - last_heard))
                    writer.close()
                    break
                writer.write(keepalive_frame)
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError, OSError, RuntimeError):
            pass
        logger.debug("Keepalive loop for %s stopped", mac_str)

    # ------------------------------------------------------------------
    # Streaming control (server -> player)
    # ------------------------------------------------------------------

    #: Exact MIME → slimproto codec char (Perl Slim/Player/Squeezebox.pm).
    #: Substring matching is NOT used — e.g. "audio/x-wavpack" contains "wav"
    #: but is not raw PCM, and WMA/ALAC/Opus are not MP3.
    _CODEC_BY_MIME = {
        "audio/mpeg": "m", "audio/mp3": "m", "audio/x-mp3": "m",
        "audio/flac": "f", "audio/x-flac": "f",
        "audio/aac": "a", "audio/aacp": "a", "audio/mp4": "a",
        "audio/x-m4a": "a", "audio/m4a": "a", "audio/mp4a-latm": "a",
        "audio/ogg": "o", "application/ogg": "o", "audio/vorbis": "o",
        "audio/opus": "u",
        "audio/wav": "p", "audio/x-wav": "p", "audio/wave": "p",
        "audio/aiff": "p", "audio/x-aiff": "p", "audio/aif": "p",
        "audio/x-ms-wma": "w", "audio/wma": "w",
        "audio/x-alac": "l", "audio/alac": "l",
    }

    @staticmethod
    def _codec_char(mime: str | None) -> str:
        """Map a MIME type to the slimproto codec character."""
        if not mime:
            return "m"
        m = mime.lower().split(";")[0].strip()
        return SlimProtoClient._CODEC_BY_MIME.get(m, "m")

    @staticmethod
    def _codec_to_extension(codec: str) -> str:
        """Map a slimproto codec char back to a file extension."""
        return {
            "f": "flac", "o": "ogg", "a": "aac", "p": "wav", "m": "mp3",
            "w": "wma", "u": "opus", "l": "alac",
        }.get(codec, "mp3")

    @staticmethod
    def _guess_codec_from_url(url: str) -> str:
        """Guess the slimproto codec char from a stream/file URL.

        Radio stream URLs usually end in .mp3/.aac/.pls etc. (or carry the
        format in the path). Falls back to 'm' (mp3) when unknown — the
        historical default.
        """
        try:
            path = urlparse(url).path.lower()
        except Exception:
            return "m"
        if path.endswith((".aac", ".m4a", ".mp4", ".adts")):
            return "a"
        if path.endswith((".flac",)):
            return "f"
        if path.endswith((".ogg", ".oga", ".opus", ".vorbis")):
            return "o"
        # .mp3, .pls, .m3u, no extension → mp3 (historical default)
        return "m"

    @staticmethod
    def _player_can_decode(player, codec: str, perl_type: str | None = None) -> bool:
        """True if ``player`` natively decodes the given source.

        The decision is made on the SOURCE format (Perl type from
        types.conf), not on the strm byte: a Musepack file is streamed with
        the mp3 fallback byte but is still not decodable. Mirrors Perl's
        ``CapabilitiesHelper::supportedFormats`` (Slim/Player/Song.pm:443-470
        "Is format supported by all players?"), which compares the scanned
        format against ``$client->formats()``.
        """
        if player is None:
            return True
        formats = getattr(player, "supported_formats", None)
        if not formats:
            return True  # unknown model: assume transcode is unnecessary
        if perl_type:
            return format_extension(perl_type) in formats
        return SlimProtoClient._codec_to_extension(codec) in formats

    @staticmethod
    def _stream_rule_for_track(mac: str, perl_type: str | None, player):
        """Which convert.conf rule Perl would apply to this track (or none).

        Perl citation chain:

        * ``Slim/Player/Song.pm:417-424`` — for every format the client
          advertised, ``Slim::Player::TranscodingHelper::getConvertCommand2(
          $self, $_, \\@streamFormats, [], \\@wantOptions)`` is called; the
          first hit wins (``last if $transcoder``).
        * ``Slim/Player/TranscodingHelper.pm:355-357`` — the candidate list is
          ``CapabilitiesHelper::supportedFormats($client)``
          (``CapabilitiesHelper.pm:38-59``), i.e. ``$client->formats()``, which
          a client with HELO caps fills from those caps
          (``SqueezePlay.pm:170-200``); ``Prefs.pm:205`` defaults
          ``prioritizeNative`` to 1, so the source's own type is tried first
          (``TranscodingHelper.pm:359-366``).
        * ``Song.pm:426-428`` — no transcoder at all is an error
          (``PROBLEM_CONVERT_FILE``); ``Song.pm:476-480`` — a ``'-'`` command
          means the source is streamed as-is.

        The result is *selection only*: our port starts no external converter,
        so the caller (:meth:`_stream_track_to_player`) keeps the frame
        consistent with what it really delivers. ``None`` means the lookup
        itself failed (never a reason to convert).
        """
        formats: tuple[str, ...] = ()
        model = None
        if player is not None:
            formats = tuple(getattr(player, "supported_formats", None) or ())
            model = getattr(player, "model", None)
        try:
            from lyrion.media.transcoding import select_stream_rule
            rule = select_stream_rule(
                perl_type, formats,
                player=model,
                clientid=mac.upper().replace(":", ""),
                # A local file is offered as a stream ('I') or as a file ('F'):
                # Perl pushes 'I' (unless seeking through a transcoder) and
                # 'R'/'F' depending on whether the handler is remote
                # (Song.pm:407-410).
                stream_modes=("I", "F"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcode rule lookup failed for %s: %s", mac, exc)
            return None
        logger.info("transcode rule for %s (%s): %s",
                    mac, perl_type, rule.describe())
        return rule

    def _replay_gain_fixed(self, mac: str, track_id: int) -> int:
        """dBToFixed des anzuwendenden ReplayGain für einen lokalen Track.

        Perl-Kette: ``StreamingController.pm:1282-1284`` holt
        ``ReplayGain->fetchGainMode($client, $song)`` und reicht den Wert an
        den strm-Frame (``Squeezebox.pm:914`` → ``canDoReplayGain`` →
        ``dBToFixed``, ``Squeezebox2.pm:888-898``). Modus 0 (Default,
        ``Squeezebox2.pm:47``) heißt „aus" → 0.
        """
        try:
            from lyrion.player.manager import PlayerManager
            from lyrion.player.replaygain import fetch_gain_mode

            player = PlayerManager().get_player(mac)
            if player is None:
                return 0
            prefs = getattr(player, "playerprefs", None) or {}
            mode_raw = prefs.get("replayGainMode", 0)
            try:
                if not int(float(str(mode_raw))):
                    return 0                      # Modus 0 = ReplayGain aus
            except (TypeError, ValueError):
                return 0
            track_gain = track_peak = album_gain = album_peak = None
            neighbours_same_album = False
            album_id = None
            try:
                import sqlite3

                from lyrion.player.manager import PlayerManager as _PM

                db_path = _PM()._players_db_path().replace("players.db", "lyrion.db")
                db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    row = db.execute(
                        "SELECT replay_gain, replay_peak FROM tracks WHERE id = ?",
                        (int(track_id),)).fetchone()
                    if row:
                        track_gain, track_peak = row[0], row[1]
                    alb = db.execute(
                        "SELECT album FROM tracks_albums WHERE track = ? LIMIT 1",
                        (int(track_id),)).fetchone()
                    if alb:
                        album_id = alb[0]
                        arow = db.execute(
                            "SELECT replay_gain, replay_peak FROM albums WHERE id = ?",
                            (album_id,)).fetchone()
                        if arow:
                            album_gain, album_peak = arow[0], arow[1]
                finally:
                    db.close()
            except Exception as exc:  # noqa: BLE001 — RG ist optional
                logger.debug("ReplayGain lookup failed for %s: %s", track_id, exc)
            gain = fetch_gain_mode(
                prefs, track_gain=track_gain, track_peak=track_peak,
                album_gain=album_gain, album_peak=album_peak,
                remote=False, neighbours_same_album=neighbours_same_album,
            )
            if gain is None:
                return 0
            return db_to_fixed(gain)
        except Exception as exc:  # noqa: BLE001
            logger.debug("ReplayGain for %s failed: %s", track_id, exc)
            return 0

    @staticmethod
    def _build_stream_frame(
        *,
        request: bytes,
        codec: str = "m",
        autostart: int = 1,
        server_port: int = 9000,
        server_ip: int = 0,
        flags: int = 0,
        threshold: int = BUFFER_THRESHOLD_KB,
        samplerate: int | None = None,
        pcm_params: tuple[str, str, str, str] | None = None,
        replay_gain: int = 0,
    ) -> bytes:
        """Build the 24-byte LMS ``strm`` packet plus its HTTP request.

        The important distinction is ``autostart``:

        * 1: normal LMS proxy stream.  Squeezelite starts decoding after
          receiving the HTTP headers; no ``cont`` frame is expected.
        * 3: direct/header-managed stream.  The player waits for a later
          ``cont`` frame after sending RESP to the server.

        The Perl LMS uses ``?`` for unknown PCM fields and output threshold
        1 for MP3.  Numeric zeroes here are not equivalent: they describe
        an 8-bit/0Hz/0-channel stream to some clients.
        """
        if autostart not in (0, 1, 2, 3):
            raise ValueError(f"invalid strm autostart: {autostart}")
        # PCM parameter bytes: '?' means "unknown, container carries the
        # format" — correct for framed codecs (mp3/flac/ogg). For codec
        # 'p' (raw PCM) squeezelite reads these values DIRECTLY
        # (pcm_open: sample_size=size-'0'+1 etc.), so the server must
        # parse the WAV/AIFF header and send explicit codes — exactly
        # what the Perl LMS does.
        if pcm_params is not None:
            p_size, p_rate, p_chan, p_endian = pcm_params
        else:
            p_size = p_rate = p_chan = p_endian = "?"
        payload = b"".join([
            b"strm",
            b"s",
            str(autostart).encode("ascii"),  # LMS/squeezelite: '0'..'3'
            codec.encode("ascii"),
            p_size.encode("ascii"),   # pcm_sample_size ('0'..'3' or '?')
            p_rate.encode("ascii"),   # pcm_sample_rate (index or '?')
            p_chan.encode("ascii"),   # pcm_channels ('1'/'2' or '?')
            p_endian.encode("ascii"), # pcm_endianness ('0' big/'1' little)
            bytes([max(0, min(255, threshold))]),
            bytes([0]),               # SPDIF auto
            bytes([TRANSITION_DURATION_DEFAULT]),  # transitionDuration pref
            # transitionType is an `a` field in Perl's template, so the
            # number 0 is STRINGIFIED to ASCII '0' (0x30) — verified with
            # perl: pack(...) gives ...ff 00 0a 30 00 01...
            b"0",                     # transitionType TRANSITION_NONE
            bytes([flags & 0xFF]),
            bytes([output_threshold(codec, samplerate)]),
            bytes([0]),               # proxy slaves
            # replayGain: Perl setzt hier ``$client->canDoReplayGain(
            # $params->{replay_gain})`` = ``dBToFixed($gain)``
            # (Squeezebox.pm:914/:1093, Squeezebox2.pm:888-898); 0 = aus.
            struct.pack(">I", max(0, int(replay_gain)) & 0xFFFFFFFF),
            struct.pack(">H", server_port),
            struct.pack(">I", server_ip),
            request,
        ])
        return struct.pack(">H", len(payload)) + payload

    @staticmethod
    def _build_strm_control_frame(command: str, replay_gain: int = 0) -> bytes:
        """Build the Perl ``stream($command)`` 24-byte body (no request).

        Perl ``Slim/Player/Squeezebox.pm:1096-1116``:
        ``pack 'aaaaaaaCCCaCCCNnN', ($command, 0, 'm','?','?','?','?',
        0,0,0, TRANSITION_NONE, $flags, 0, 0, $replayGain, 0, 0)``.
        ``pack('a', 0)`` stringifies the number 0, so autostart and
        transitionType are the ASCII char ``'0'``. Verified byte for byte
        against perl:

            stream(p) -> 70 30 6d 3f 3f 3f 3f 00 00 00 30 00 00 00
                         00 00 00 00 00 00 00 00 00 00

        Used for 'f' flush, 'q' stop, 'p' pause, 'u' unpause, 'a' skip.
        (Perl: Squeezebox2.pm:1104-1126 resume/pauseForInterval/skipAhead.)
        """
        c = command.encode("ascii")
        if len(c) != 1:
            raise ValueError(f"invalid strm command: {command!r}")
        payload = b"".join([
            b"strm",
            c,                          # command
            b"0",                       # autostart (pack('a', 0) -> "0")
            b"m",                       # format byte
            b"?", b"?", b"?", b"?",     # pcm size/rate/chan/endian
            bytes([0]),                 # bufferThreshold
            bytes([0]),                 # spdif
            bytes([0]),                 # transitionDuration
            b"0",                       # transitionType TRANSITION_NONE
            bytes([0]),                 # flags
            bytes([0]),                 # outputThreshold
            bytes([0]),                 # slaves
            struct.pack(">I", max(0, int(replay_gain)) & 0xFFFFFFFF),
            struct.pack(">H", 0),       # server_port
            struct.pack(">I", 0),       # server_ip
        ])
        return struct.pack(">H", len(payload)) + payload

    @staticmethod
    def _reset_strm_guard(mac: str, reason: str = "") -> None:
        """Forget the track we streamed for ``mac`` (idempotency guard).

        Call this on EVERY path where the player demonstrably no longer holds
        the stream we sent: an incoming ``STMf``/``STMn`` that is NOT our own
        start handshake, the track-end path (``_advance_after_track``), a
        stop/pause, and a disconnect/reconnect. Otherwise the guard stays
        armed and a replay of the SAME track sends 0 frames while reporting
        success — the player stays silent and nothing but a track change can
        repair it (R0.5-P1).

        It deliberately does NOT touch ``stream_in_flight``: that flag
        belongs to the send currently in progress and is released by its own
        ``finally``.
        """
        try:
            from lyrion.player.manager import PlayerManager
            p = PlayerManager().get_player(mac)
            if p is not None and (p.strm_sent_track is not None
                                  or p.playing_track_id is not None):
                logger.debug(
                    "strm guard reset for %s (%s)", mac, reason or "stream lost",
                )
                p.forget_stream()
        except Exception as exc:  # noqa: BLE001
            logger.debug("strm guard reset failed for %s: %s", mac, exc)

    def _heartbeats(self) -> dict[str, float]:
        """Perl's ``%heartbeat`` (Slimproto.pm:43) — lazy, because unit tests
        build a SlimProtoClient via ``__new__`` without ``__init__``."""
        hb = getattr(self, "_last_heard", None)
        if hb is None:
            hb = self._last_heard = {}
        return hb

    def _register_player_writer(self, mac_key: str, writer) -> None:
        """Register (or replace) the SlimProto writer for ``mac_key``.

        Perl keeps ONE ``tcpsock`` per client (Slim/Player/Client.pm ``tcpsock``):
        when a HELO arrives for a client that still has a socket, the OLD socket
        is closed with the ``reconnect`` flag — ``slimproto_close( $client->tcpsock,
        'reconnect' )`` (Slimproto.pm:1195) — i.e. its close runs WITHOUT the
        disconnect block (:261-296), and the pending forget timer is killed
        (``Slim::Utils::Timers::killTimers($client, \\&forget_disconnected_client)``,
        :1198). Leaking the old socket instead leaves ``_player_connections``
        inflated, so a later close of the live socket no longer reaches 0 and the
        register keeps pointing at a dead StreamWriter — the next strm frame then
        goes to a socket nobody reads (no RESP, frozen title).

        A (re)connect means the player has no stream in progress: Perl
        disassociates the streaming socket on stop/play (Squeezebox.pm:
        206-216) and the fresh SlimProto connection starts with
        readyToStream(1). Clear any stale strm guard so the next play of the
        same track streams again instead of being skipped into silence.
        """
        old = self._player_writers.get(mac_key)
        if old is not None and old is not writer:
            # Perl's reconnect-close: the previous socket is dropped here, so
            # its own handler must not run the disconnect path (see the
            # superseded check in _handle_player's cleanup).
            self._player_connections.pop(mac_key, None)
            self._player_writers.pop(mac_key, None)
            self._heartbeats().pop(mac_key, None)
            try:
                old.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("closing superseded socket for %s failed: %s",
                             mac_key, exc)
            logger.info("Closed previous SlimProto socket for %s (reconnect)",
                        mac_key)
        self._player_writers[mac_key] = writer
        # Perl has exactly one live slimproto socket per client (Slimproto.pm:
        # :1195, :1215-1223) — reset the counter instead of incrementing it.
        self._player_connections[mac_key] = 1
        # Slimproto.pm:1223 — a fresh HELO (re)arms the heartbeat.
        self._heartbeats()[mac_key] = time.monotonic()
        # Slimproto.pm:1198 — a reconnecting client must not be forgotten.
        self._cancel_forget(mac_key)
        self._reset_strm_guard(mac_key, "player (re)connected")
        # PROT-14: a (re)connect starts a fresh controller state (Perl builds
        # a new controller per client, StreamingController.pm:46-87).
        try:
            from lyrion.player import streaming
            streaming.reset(mac_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("streaming reset on connect failed for %s: %s",
                         mac_key, exc)

    async def send_flush_to_player(self, mac: str) -> bool:
        """Send a 'strm' flush command ('f') to a player.

        The Perl LMS sends this before switching streams while the player
        is playing (Squeezebox2.pm flush -> stream('f'), triggered by
        StreamingController::_FlushGetNext). Squeezelite's 'f' handler
        does decode_flush + output_flush + buf_flush(streambuf) — without
        it the player keeps playing out its old buffers, so a radio
        switch takes as long as the old buffer lasts (and the old stream
        keeps being audible).
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        frame = self._build_strm_control_frame("f")
        try:
            writer.write(frame)
            await writer.drain()
            # A flush tears the player's buffers down — the next play of the
            # same track MUST send a fresh strm frame, so clear the guard.
            self._reset_strm_guard(mac, "strm 'f' (flush) sent")
            _streaming_note(mac, "Flush")
            logger.info("Sent strm 'f' (flush) to %s", mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def _flush_if_playing(self, mac: str) -> None:
        """Flush the player's buffers before a stream switch, like the
        Perl LMS does (stream('f') before the new strm when playing)."""
        try:
            from lyrion.player.manager import PlayerManager
            player = PlayerManager().get_player(mac)
            if player is not None and player.mode in ("play", "loading"):
                await self.send_flush_to_player(mac)
        except Exception:
            pass

    async def _stop_before_switch(self, mac: str) -> None:
        """Perl's switch sequence: stop the player, then start the new song.

        Perl's controller stops the player on every song change:
        ``_StopGetNext`` → ``_Stop`` → ``_stopClient``
        (StreamingController.pm:599-601, :622-627) → ``$client->stop`` =
        ``stream('q')`` (Squeezebox.pm:206-216), followed by
        ``@{$client->chunks} = ()`` and ``closeStream()``.

        The stop frame is what makes a switch IMMEDIATE. jive's ``'q'``
        handler runs ``_stopPauseAndStopTimers()`` + ``stopInternal()``
        (Playback.lua:966-970) and tears the DECODED audio pipeline down;
        a bare new ``'s'`` only flushes the network buffer
        (``_streamDisconnect(nil, true)`` → ``Stream:flush()``,
        Playback.lua:896, :777-779) and lets the already-decoded audio play
        out. Measured live before this fix: ~4.5 s of the old stream kept
        sounding (player elapsed 11.3 s → 15.4 s), then only STMs for the new
        one — user-visible as "der Puffer wird erst leergespielt".
        """
        writer = self._player_writers.get(mac.upper().replace(":", ""))
        if writer is None or writer.is_closing():
            return
        try:
            writer.write(self._build_strm_control_frame("q"))
            await writer.drain()
            logger.info("Sent strm 'q' (switch stop) to %s", mac)
        except (ConnectionError, OSError, RuntimeError):
            pass

    async def _cancel_if_switching(self, mac: str) -> None:
        """Stop + close the previous stream when a DIFFERENT song starts.

        Perl only stops when there is a streaming song to stop, so a first
        play (nothing streaming) sends no ``'q'``.
        """
        try:
            from lyrion.player.manager import PlayerManager
            player = PlayerManager().get_player(mac)
        except Exception:
            player = None
        switching = bool(
            player is not None
            and (player.mode in ("play", "pause")
                 or getattr(player, "strm_sent_track", None) is not None)
        )
        if switching:
            await self._stop_before_switch(mac)
        self.cancel_active_stream(mac)

    @staticmethod
    def cancel_active_stream(mac: str) -> bool:
        """Abort the player's running ``/stream.mp3`` response.

        Perl LMS parity: the newest streaming socket becomes
        ``$client->streamingsocket`` and ``sendStreamingResponse`` closes any
        socket that is no longer the client's current one
        (Slim/Web/HTTP.pm:2136 + 2185-2199); ``stop``/``play`` disassociate it
        outright (Squeezebox.pm:206-216, Squeezebox2.pm:398-403). The Python
        handler is paced, so without this an old stream keeps writing for
        minutes after the switch.
        """
        try:
            from lyrion.web.stream import cancel_active_stream
            cancelled = cancel_active_stream(mac)
            if cancelled:
                logger.info("Cancelled active /stream.mp3 response for %s", mac)
            return cancelled
        except Exception as exc:  # noqa: BLE001
            logger.debug("cancel_active_stream failed for %s: %s", mac, exc)
            return False

    async def send_strm_to_player(self, mac: str, track_id: int) -> bool:
        """Send a 'strm' (stream) frame to a player so it fetches the track
        over HTTP from this server's /stream.mp3 endpoint.

        Squeezelite opens its own TCP connection to the server (ip from the
        slimproto connection when server_ip=0) and issues the HTTP request
        string we embed in the frame.

        Idempotency guard (LIVE-02 / R0.5-P1): a repeat `playlistcontrol
        cmd:load` for the track that is ALREADY streaming must not flush and
        restart the player's buffers. The guard therefore compares against
        the track we actually streamed (``strm_sent_track``) — the caller
        cannot set that optimistically — and it is cleared on every path
        where the player demonstrably lost that stream (player flush `STMf`,
        decode error `STMn`, track end, stop/pause, disconnect/reconnect).
        Perl does the same by closing the streaming socket and setting
        readyToStream(1): stop -> stream('q') + streamingsocket(undef) +
        readyToStream(1) (Squeezebox.pm:206-216), flush -> stream('f') +
        readyToStream(1) (Squeezebox2.pm:387-396), STMn -> readyToStream(1) +
        playerStreamingFailed (Squeezebox2.pm:153-157). A guard that outlives
        the stream is worse than a duplicate strm: the play request reports
        success while the player stays silent.

        The claim on the track is taken synchronously, BEFORE the first
        ``await`` (``PlayerState.stream_in_flight``), so two concurrent calls
        for the same track cannot both pass the check and send (R0.5-P3); it
        is released in ``finally``.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.warning("No active connection for player %s", mac)
            return False

        # ── Idempotency guard (LIVE-02) ──
        # Perl's controller ignores a redundant _Stream for the song that is
        # already playing: a repeat `playlistcontrol cmd:load <current track>`
        # does NOT flush and restart the stream. Our handle used to send
        # `strm 'f'` + `strm 's'` on every repeat, and SqueezePlay answers
        # every `cmd:load` with a fresh load — the resulting
        # flush/connect/underrun loop tore the buffers down before any audio
        # could play (see .hermes/gap-analysis/06 LIVE-02: 6 strm frames in
        # 8 s, out_fullness flipping 0 <-> 3525496, elapsed frozen).
        #
        # Compare against the track we ACTUALLY streamed, not
        # current_track_id: the caller sets current_track_id and mode='play'
        # optimistically BEFORE calling us, so guarding on those made every
        # fresh play a silent no-op (no strm frame at all — the player stayed
        # silent with mode=play).
        existing = None
        try:
            from lyrion.player.manager import PlayerManager
            existing = PlayerManager().get_player(mac)
        except Exception as exc:  # noqa: BLE001
            logger.debug("strm idempotency check failed for %s: %s", mac, exc)

        if existing is not None:
            # "already playing → nothing to do" (Perl StreamingController:
            # state PLAYING + the same song → `_Stream` :1144 does not
            # restart anything). TWO criteria, because the player's start
            # handshake used to disarm the first one:
            #   * ``strm_sent_track`` — the track we ACTUALLY streamed last,
            #   * ``playing_track_id`` — the track the player DEMONSTRABLY
            #     runs (STMs / advancing elapsed).
            # Anything else (a different track, a stop/flush/track end, a
            # lost stream) is a genuine (re)start and must stream.
            _already_playing = (
                existing.strm_sent_track == track_id
                or existing.playing_track_id == track_id
            )
            if existing.mode == "play" and _already_playing:
                logger.info(
                    "strm for %s track=%d already playing — skipping re-stream",
                    mac, track_id,
                )
                return True
            # R0.5-P3: check-and-set must not be separated by an await.
            # Claim the track now; a concurrent call for the SAME track stops
            # here instead of flushing/streaming a second time (observed live:
            # two `Sent strm ... track=51997` in the same second for two
            # parallel cmd:load requests). A call for a DIFFERENT track is a
            # genuine switch and is not blocked.
            if existing.stream_in_flight == track_id:
                logger.info(
                    "strm for %s track=%d already in flight — skipping duplicate",
                    mac, track_id,
                )
                return True
            existing.stream_in_flight = track_id

        try:
            return await self._stream_track_to_player(mac, track_id, writer)
        finally:
            if existing is not None and existing.stream_in_flight == track_id:
                existing.stream_in_flight = None

    async def _stream_track_to_player(self, mac: str, track_id: int, writer) -> bool:
        """Do the flush + ``strm 's'`` send for ``track_id``.

        Split out of :meth:`send_strm_to_player` so that the in-flight claim
        is released in a ``finally`` on every exit path — including
        exceptions the send itself does not catch (a leaked claim would
        swallow every future play of that track, the exact failure this
        guard exists to prevent).
        """
        # Load track metadata for codec
        mime = None
        track_path = None
        track_url = ""
        try:
            from sqlalchemy import select
            from lyrion.database.schema import Track
            from lyrion.database.sqlite_helper import db_session
            async with db_session() as session:
                track = (await session.execute(
                    select(Track).where(Track.id == track_id)
                )).scalar_one_or_none()
                if track is not None:
                    mime = track.content_type
                    if track.url:
                        track_url = track.url
                        from lyrion.web.stream import _track_path_from_url
                        track_path = _track_path_from_url(track.url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load track %d for codec: %s", track_id, exc)

        # Perl decides the source format from the FILE SUFFIX first, MIME
        # second (Slim/Music/Info.pm typeFromSuffix + types.conf). Our DB's
        # content_type is unreliable (.opus stored as audio/ogg, .mpc as
        # chemical/x-mopac-input), and the suffix decides the strm format
        # byte in Slim/Player/Squeezebox.pm:546-770.
        perl_type = describe_type(track_url, mime)
        codec = format_byte(perl_type)
        pcm_params = None  # (sample_size, rate, chan, endian) ASCII codes
        transcode_requested = False

        # mp4/aac: the pcmsamplesize field carries the AAC container type
        # (Squeezebox.pm:712-717) — '5' mp4ff for .mp4/.m4a, '2' adts for
        # a raw .aac stream. Without it the player cannot demux the file.
        if perl_type in ("mp4", "aac"):
            pcm_params = (pcm_samplesize_for(perl_type), "?", "?", "?")

        # ── Transcoding rule + client decode capability ─────────────────────
        # Perl asks the convert.conf rule table which profile applies and uses
        # the profile's streamformat as the strm format byte:
        #   Song.pm:417-424  getConvertCommand2($song, $type, \@streamFormats,
        #                    [], \@wantOptions)  — per advertised format
        #   Song.pm:426-428  no transcoder → logError + PROBLEM_CONVERT_FILE
        #   Song.pm:476-480  command '-' → the source itself is streamed
        #   Song.pm:576-586  a real command starts the converter's process
        #   Song.pm:630-631/:687-688  _streamFormat($transcoder->{streamformat})
        #   StreamingController.pm:1316 'format' => $song->streamformat()
        #   Squeezebox.pm:549-781       $format → $formatbyte
        # The advertised formats come from the HELO caps
        # (CapabilitiesHelper.pm:38-59 → $client->formats(); SqueezePlay.pm:
        # 170-200), which our port keeps as ``player.supported_formats``.
        #
        # Our port starts NONE of the external converters the profiles name
        # ([flac]/[sox]/[lame]…, tokenizeConvertCommand2
        # TranscodingHelper.pm:519-673) — it can only deliver the source file
        # or raw PCM via the /stream.mp3 ffmpeg path. The frame therefore
        # never announces a format we will not send: the selected rule is
        # logged (name + reason), and the codec byte follows what is really
        # delivered.
        player = None
        try:
            from lyrion.player.manager import PlayerManager
            player = PlayerManager().get_player(mac)
        except Exception as exc:  # noqa: BLE001
            logger.debug("player lookup for %s failed: %s", mac, exc)

        rule = self._stream_rule_for_track(mac, perl_type, player)
        natively_supported = SlimProtoClient._player_can_decode(
            player, codec, perl_type=perl_type)

        if natively_supported:
            if rule is not None and rule.needs_conversion:
                # Table says "convert", the client says "I can play it".
                # Perl would only convert if the identity profile were
                # missing; keeping the playable source is the safe reading —
                # a frame for a format we do not deliver leaves the player
                # silent.
                logger.info(
                    "track %d (%s): client %s decodes natively, rule %s "
                    "(target %s) not applied — source stream kept",
                    track_id, perl_type, mac, rule.profile,
                    rule.streamformat,
                )
        else:
            reason = rule.describe() if rule is not None else "no rule table"
            try:
                from lyrion.web.stream import (
                    ffmpeg_available,
                    ffprobe_audio_info,
                    _set_server_notice,
                )
                if not ffmpeg_available():
                    _set_server_notice("ffmpeg not found - transcoding not possible")
                    logger.warning(
                        "track %d format %s (%s) not decodable by %s and no "
                        "converter (Perl rule: %s) — serving the source "
                        "directly",
                        track_id, perl_type, codec, mac, reason,
                    )
                else:
                    info = (ffprobe_audio_info(track_path)
                            if track_path is not None and track_path.is_file() else None)
                    if info is not None:
                        from lyrion.web.stream import pcm_params_for_strm
                        pcm_params = pcm_params_for_strm({
                            "bits": info["bits"], "rate": info["rate"],
                            "channels": info["channels"], "bigendian": False,
                        })
                        source_codec = codec
                        codec = "p"          # Squeezebox.pm:595-597 (pcm)
                        transcode_requested = True
                        logger.info(
                            "track %d format %s (%s) not decodable by %s — "
                            "transcoding to raw PCM via ffmpeg (%d/%d/%d); "
                            "Perl rule: %s%s",
                            track_id, perl_type, source_codec, mac, info["bits"],
                            info["rate"], info["channels"], reason,
                            "" if rule is None or rule.streamformat == "pcm"
                            else " (rule target %s not implemented — raw PCM "
                                 "delivered instead)" % rule.streamformat,
                        )
                    else:
                        logger.warning(
                            "track %d codec %s not decodable, ffprobe failed; "
                            "serving source directly (Perl rule: %s)",
                            track_id, codec, reason,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Transcode decision failed for track %d: %s",
                               track_id, exc)

        # For raw-PCM codecs the strm frame must carry the explicit format
        # (squeezelite pcm_open reads it straight from the frame); parse the
        # WAV/AIFF header server-side like the Perl LMS. This only applies
        # to a native WAV/AIFF source — when we already set pcm_params via
        # ffprobe for the format-fallback transcode, keep those.
        if codec == "p" and not transcode_requested \
                and track_path is not None and track_path.is_file():
            from lyrion.web.stream import parse_pcm_header, pcm_params_for_strm
            info = parse_pcm_header(track_path)
            if info is not None:
                pcm_params = pcm_params_for_strm(info)
                logger.info(
                    "strm PCM params for track %d (%s): bits=%d rate=%d "
                    "chan=%d bigendian=%s",
                    track_id, track_path.name, info["bits"], info["rate"],
                    info["channels"], info["bigendian"],
                )
            else:
                logger.warning(
                    "track %d is PCM but header unparsable; sending '?'",
                    track_id,
                )

        # HTTP request string Squeezelite will send to our web server.
        # LMS format (Slim/Player/Squeezebox.pm stream_s): the request is
        # exactly "GET /stream.mp3?player=<MAC> HTTP/1.0\r\n\r\n" — no track
        # id in the URL, no Host header. The /stream endpoint resolves the
        # current track from the player's playlist via the player= param.
        if transcode_requested:
            # Format fallback: ask the /stream endpoint to run ffmpeg and
            # emit raw PCM instead of the source file.
            request = (
                f"GET /stream.mp3?player={mac}&transcode=1 HTTP/1.0\r\n"
                f"\r\n"
            ).encode("ascii")
        else:
            request = (
                f"GET /stream.mp3?player={mac} HTTP/1.0\r\n"
                f"\r\n"
            ).encode("ascii")

        # Normal LMS proxy stream: autostart=1.  The player starts after
        # HTTP headers; it must not wait for a cont frame.
        threshold = stream_buffer_threshold(
            track_path.stat().st_size
            if track_path is not None and track_path.is_file() else None,
        )
        frame = self._build_stream_frame(
            request=request, codec=codec, autostart=1,
            server_port=self.web_port,
            pcm_params=pcm_params,
            threshold=threshold,
            replay_gain=self._replay_gain_fixed(mac, track_id),
        )
        # Perl's switch sequence: stop the player (``stream('q')`` —
        # _StopGetNext/_Stop/_stopClient, StreamingController.pm:599-627 →
        # Squeezebox.pm:206-216) and close the previous /stream.mp3 response
        # (``closeStream()``, Squeezebox2.pm:398-403). The stop is what cuts
        # the already-decoded audio of the OLD stream in the player; without
        # it the switch only flushed the network buffer and the old audio
        # played out (measured ~4.5 s live). NO ``strm 'f'``: Perl flushes
        # only from ``_FlushGetNext`` (song-queue flush), and a stray 'f'
        # left the decoder stopped while the source kept buffering.
        await self._cancel_if_switching(mac)
        try:
            writer.write(frame)
            await writer.drain()
            # A new stream replaces whatever ran before: the old track is no
            # longer the one the player runs.
            try:
                from lyrion.player.manager import PlayerManager
                _p = PlayerManager().get_player(mac)
                if _p is not None and _p.playing_track_id != track_id:
                    _p.playing_track_id = None
            except Exception:  # noqa: BLE001
                pass
            # PROT-14: our strm frame is out, so Perl's tail applies — the
            # stream-start handshake is re-armed (Squeezebox.pm:567), the
            # controller moves to BUFFERING/STREAMING (StreamingController.pm:
            # 1350-1352), the idempotency guard records the track we ACTUALLY
            # streamed, and the status mode follows the controller
            # (Source.pm:55-64 → Queries.pm:4081).
            _after_strm_sent(mac, track_id)
            logger.info("Sent strm to %s: track=%d codec=%s", mac, track_id, codec)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Failed to send strm to %s: %s", mac, exc)
            return False

    @staticmethod
    def _pick_playlist_url(body: bytes) -> list[str]:
        """Playable URL lines from an M3U/PLS body (http/https), skipping
        TuneIn/Radiotime ad 'bump' stub URLs — in playlist order."""
        ad_hints = ("bump", "preview", "pre_", "advert",
                    "promo", "teaser", "sample")
        return [
            ln.strip()
            for ln in body.decode("utf-8", errors="replace").splitlines()
            if ln.strip().startswith(("http://", "https://"))
            and not any(h in ln.lower() for h in ad_hints)
        ]

    async def _first_reachable(self, candidates: list[str]) -> str | None:
        """First playlist candidate that answers GET < 400, trying an
        https→http fallback per candidate. Stream ports (8000/8060/…)
        are often plain HTTP even when the playlist advertises https —
        e.g. 1Mix: TuneIn lists https://fr2.1mix.co.uk:8060/320h, the
        working stream is http://fr2.1mix.co.uk:8060/320.

        GET, not HEAD — Perl re-scans each playlist entry with a GET
        (Scanner/Remote.pm:1195-1214 → :205) and a 503 on HEAD would
        discard a perfectly fine candidate.

        All probes run in parallel (asyncio.gather) — with a dead station
        a sequential scan would block the play request for the sum of
        all timeouts (60 s+); parallel it is just one timeout.
        """
        import httpx
        headers = {"User-Agent": "PyrionMusicServer/9.2.0"}
        timeout = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=True) as client:
            variants: list[str] = []
            for cand in candidates:
                variants.append(cand)
                if cand.startswith("https://"):
                    variants.append("http://" + cand[len("https://"):])

            async def test(v: str) -> str | None:
                try:
                    async with client.stream("GET", v, headers=headers) as r:
                        if r.status_code < 400:
                            return str(r.url)
                except Exception:
                    pass
                return None

            results = await asyncio.gather(*(test(v) for v in variants))
            for r in results:
                if r:
                    return r
        return None

    # Cache for resolved stream URLs (redirects / playlist expansion).
    # Short TTL so a station's current stream URL can change.
    _url_resolve_cache: dict[str, tuple[float, str]] = {}
    _URL_RESOLVE_TTL = 600.0  # 10 min

    async def _resolve_stream_url(self, url: str) -> str:
        """Resolve a radio URL the way the Perl LMS does before sending a
        direct stream: follow HTTP redirects and expand M3U/PLS playlists
        to the first playable audio URL. Squeezelite can do neither —
        without this, redirect URLs (SWR3) or playlist URLs (1Mix/TuneIn)
        produce silence on direct streams.
        """
        import time as _time

        now = _time.time()
        cached = self._url_resolve_cache.get(url)
        if cached and now - cached[0] < self._URL_RESOLVE_TTL:
            return cached[1]

        import httpx

        result = url
        try:
            headers = {"User-Agent": "PyrionMusicServer/9.2.0"}
            timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
            async with httpx.AsyncClient(timeout=timeout,
                                         follow_redirects=True) as client:
                # GET, not HEAD: Perl's scanURL is a GET
                # (Scanner/Remote.pm:205) and some Icecast/DAS edges answer
                # HEAD with 503 while serving the audio fine on GET.
                async with client.stream("GET", url, headers=headers) as resp:
                    final = str(resp.url)
                    ctype = resp.headers.get("content-type", "").lower()
                    is_playlist = ("playlist" in ctype or "mpegurl" in ctype
                                   or final.lower().endswith((".pls", ".m3u", ".m3u8")))
                    if is_playlist:
                        # Read the playlist body (bounded, Perl: readLimit
                        # 128*1024, Remote.pm:1180-1206), then pick the first
                        # REACHABLE URL (GET test, https→http fallback) — the
                        # playlist often lists dead/mis-schemed URLs first
                        # (1Mix: TuneIn lists https://…:8000, working stream
                        # is http://…:8060/…).
                        try:
                            body = b""
                            async for chunk in resp.aiter_bytes():
                                body += chunk
                                if len(body) > 128 * 1024:
                                    break
                            candidates = self._pick_playlist_url(body)
                            if candidates:
                                reachable = await self._first_reachable(candidates)
                                result = reachable or candidates[0]
                        except Exception as exc:
                            logger.warning("Playlist resolve failed for %s: %s",
                                           url[:60], exc)
                    elif final != url and resp.status_code < 400:
                        result = final
        except Exception as exc:
            logger.warning("Stream URL resolve failed for %s (%s) — using as-is",
                           url[:60], exc)

        if result != url:
            logger.info("Resolved stream URL: %s -> %s", url[:60], result[:90])
        self._url_resolve_cache[url] = (now, result)
        return result

    async def send_remote_stream(self, mac: str, url: str, codec: str = "m",
                                 *,
                                 resolved: bool = False) -> bool:
        """Send a strm frame for an EXTERNAL stream URL (radio/favorites).

        LMS behaviour (Squeezebox.pm stream_s, $isDirect branch): the player
        streams DIRECTLY from the source. server_ip/server_port in the strm
        frame point at the source, the request string is the source request
        (Slim/Player/Protocols/HTTP.pm requestString), autostart is 3
        (direct = proxy-autostart 1 + 2). After the player connects it
        forwards the source's HTTP response headers as a RESP frame; the
        server replies with a 'cont' frame carrying the Icecast metaint so
        the player can de-interleave metadata itself. The stream keeps
        playing when this server goes away — exactly like the real LMS.

        Falls back to the server-side proxy (/stream.mp3?player=MAC) when
        the URL cannot be resolved to a direct connection or when the client's
        ``mp3StreamingMethod`` pref asks for proxied streaming (HTTP.pm:433-439).
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.warning("No active connection for player %s", mac)
            return False

        # Perl HTTP.pm:433-439 (``canDirectStream``): "Allow user pref to
        # select the method for streaming" — ``$method == 1`` refuses the
        # direct stream ("Not direct streaming because of mp3StreamingMethod
        # pref", :436), so the player is pointed at OUR /stream.mp3 relay
        # instead of at the remote URL.
        try:
            from lyrion.player.manager import PlayerManager as _PM
            _pref_player = _PM().get_player(mac)
        except Exception:  # noqa: BLE001
            _pref_player = None
        if int(getattr(_pref_player, "mp3_streaming_method", 0) or 0) == 1:
            logger.info("Player %s mp3StreamingMethod=1 — proxying %s "
                        "(HTTP.pm:433-439)", mac, url[:60])
            return await self._send_proxy_stream(mac, url, codec)

        # Stop the player and close its previous /stream.mp3 response before
        # the new direct stream — Perl's _stopClient (stream('q') +
        # closeStream(), StreamingController.pm:622-627) so the old audio is
        # cut immediately instead of playing out of the player's decoded
        # buffer. No ``strm 'f'`` (Perl flushes only in _FlushGetNext).
        await self._cancel_if_switching(mac)

        # Resolve redirects / M3U/PLS playlists server-side (Squeezelite
        # can do neither) — like the Perl LMS does before direct streams.
        # ``resolved=True`` means the caller already probed the URL the way
        # Perl's scanURL does (Slim/Utils/Scanner/Remote.pm:205-233) and
        # hands over the FINAL URL, so this HEAD/GET round-trip is skipped.
        if not resolved:
            url = await self._resolve_stream_url(url)

        # Keep the player playlist in sync: the proxy endpoint resolves
        # the track from player.playlist, so it must hold the RESOLVED
        # URL — otherwise it would re-fetch the raw M3U/redirect URL and
        # stream playlist text as audio (silence).
        try:
            from lyrion.player.manager import PlayerManager
            player = PlayerManager().get_player(mac)
            if player is not None and player.playlist:
                pos = player.playlist_position or 0
                if 0 <= pos < len(player.playlist) and isinstance(player.playlist[pos], str):
                    player.playlist[pos] = url
            # Keep state consistent regardless of the calling path
            # (play_url vs _play_playlist_item): this is a live stream.
            if player is not None:
                player.current_url = url
                player.current_track_id = None
                # A DIRECT (radio/favourite) stream replaces the file stream
                # we last sent a strm for: the idempotency guard must not
                # survive into a later play of that track — it would be
                # skipped into silence while the player runs the radio URL.
                player.forget_stream()
        except Exception:
            pass

        from urllib.parse import urlparse
        import socket as _socket

        # Player without CanHTTPS=1 cannot do TLS itself → https streams
        # must be proxied by the server (Perl LMS: canDirectStream returns
        # 0 unless CanHTTPS; HTTPS.pm slimprotoFlags only sets the SSL flag
        # for direct streams). http streams always go direct.
        try:
            from lyrion.player.manager import PlayerManager
            pstate = PlayerManager().get_player(mac)
        except Exception:
            pstate = None
        parsed_tmp = urlparse(url)
        if parsed_tmp.scheme == "https" and not getattr(pstate, "can_https", False):
            logger.info("Player %s cannot do TLS (no CanHTTPS cap) — proxying %s",
                        mac, url[:60])
            return await self._send_proxy_stream(mac, url, codec)

        # ── Format fallback: AAC radio streams ──────────────────────────
        # squeezelite's faad2 fails silently on many radio AAC flavours
        # (HE-AAC/SBR): it consumes bytes but the output never starts
        # (STMf/STMc then nothing). The Perl LMS transcodes those to MP3
        # for players without native AAC support. Route them through the
        # server proxy with an ffmpeg re-encode so the player gets plain
        # MP3 it can always decode.
        if codec == "a":
            logger.info("AAC stream %s — using transcode proxy "
                        "(faad2 unreliable on radio AAC) ", url[:60])
            return await self._send_proxy_stream(mac, url, "m",
                                                 transcode="mp3")

        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"unsupported URL scheme: {url[:40]}")
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            # Squeezelite's slimproto server_ip field is 4 bytes — IPv4 only.
            # Force AF_INET so getaddrinfo cannot return an IPv6 address
            # (inet_aton would reject it).
            infos = await asyncio.get_event_loop().getaddrinfo(
                host, port, family=_socket.AF_INET, type=_socket.SOCK_STREAM
            )
            ip = str(infos[0][4][0])
            server_ip = int.from_bytes(_socket.inet_aton(ip), "big")
        except Exception as exc:
            logger.warning("Direct stream setup failed for %s (%s) — using proxy",
                           url[:60], exc)
            return await self._send_proxy_stream(mac, url, codec)

        # Request string exactly like the Perl-LMS HTTP.pm requestString:
        #   GET <path> HTTP/1.0 + Accept + Cache-Control + User-Agent +
        #   Icy-MetaData: 1 + Connection: close + Host
        host_header = host if port in (80, 443) else f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.0\r\n"
            f"Accept: */*\r\n"
            f"Cache-Control: no-cache\r\n"
            f"User-Agent: PyrionMusicServer/9.2.0\r\n"
            f"Icy-MetaData: 1\r\n"
            f"Connection: close\r\n"
            f"Host: {host_header}\r\n"
            f"\r\n"
        ).encode("ascii")

        # Direct stream: autostart 1+2=3 (player waits for 'cont' after
        # RESP); SSL flag 0x20 for https (HTTPS.pm slimprotoFlags).
        flags = 0x20 if parsed.scheme == "https" else 0
        frame = self._build_stream_frame(
            request=request, codec=codec, autostart=3,
            server_port=port, server_ip=server_ip, flags=flags,
            threshold=stream_buffer_threshold(remote=True),
        )
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent DIRECT strm to %s (remote: %s -> %s:%d)",
                        mac, url[:60], ip, port)
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Failed to send direct strm to %s: %s", mac, exc)
            return False

        # Perl's `stream_s` tail applies to a DIRECT stream exactly like to a
        # /stream.mp3 file stream: Squeezebox.pm:567 clears the start handshake
        # on the frame, the controller already sits in BUFFERING+STREAMING
        # (StreamingController.pm:1350-1352) and the status mode is derived from
        # it (Source.pm:55-64). Without this the controller stayed STOPPED-IDLE,
        # so the status reported `mode:stop` for the whole start and the
        # player's own STMf start ack dropped it back to stop — even while the
        # stream was running (Squeezebox2.pm:174-176: no STMf branch, it is a
        # plain playerStatusHeartbeat).
        _after_strm_sent(mac)

        # Wait for the player's RESP (source headers) and send 'cont' with
        # the metaint so the player can strip Icecast metadata itself.
        asyncio.create_task(self._send_cont_after_resp(mac, url))
        return True

    async def _send_proxy_stream(self, mac: str, url: str, codec: str = "m",
                                 transcode: str = "") -> bool:
        """Fallback: server-side proxy stream (player fetches /stream.mp3
        from this server, which relays the remote URL). With ``transcode``
        set, the proxy re-encodes via ffmpeg to that target format."""
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.warning("No active connection for player %s", mac)
            return False

        from urllib.parse import quote

        query = f"remote={quote(url, safe='')}"
        if transcode:
            query += f"&transcode={quote(transcode, safe='')}"
        request = (
            f"GET /stream.mp3?{query} HTTP/1.0\r\n"
            f"\r\n"
        ).encode("ascii")

        frame = self._build_stream_frame(
            request=request, codec=codec, autostart=1,
            server_port=self.web_port,
            threshold=stream_buffer_threshold(remote=True),
        )
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent proxy strm to %s (remote: %s)", mac, url[:60])
            # Perl's stream_s tail, see _send_direct_stream / Squeezebox.pm:567.
            _after_strm_sent(mac)
            # No 'cont' frame — matches real LMS behaviour.
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Failed to send remote strm to %s: %s", mac, exc)
            return False

    async def _send_cont_after_resp(self, mac: str, url: str) -> None:
        """Wait for the player's RESP frame (source HTTP headers) for a
        direct stream, then send the 'cont' frame that starts the decoder
        and passes the Icecast metaint (Squeezebox2.pm sendContCommand).
        """
        mac_key = mac.upper().replace(":", "")
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[int] = loop.create_future()
        self._resp_waiters[mac_key] = fut
        try:
            try:
                metaint = await asyncio.wait_for(fut, timeout=20)
            except asyncio.TimeoutError:
                logger.warning("No RESP from %s for direct stream %s — cont metaint=0",
                               mac, url[:50])
                metaint = 0
                # The source never answered (dead/unreachable station) —
                # don't leave the UI showing "playing" with no audio.
                try:
                    from lyrion.player.manager import PlayerManager
                    player = PlayerManager().get_player(mac)
                    if player is not None:
                        player.mode = "stop"
                except Exception:
                    pass
            await self._send_cont(mac, metaint)
        except Exception as exc:
            logger.warning("cont after RESP failed for %s: %s", mac, exc)
        finally:
            self._resp_waiters.pop(mac_key, None)

    async def _send_cont(self, mac: str, metaint: int) -> bool:
        """Send a 'cont' frame to a player.

        Exact Perl-LMS format (Squeezebox2.pm sendContCommand,
        pack('NCnC*', metaint, loop, count, guids)) — Perl 'C' is unsigned
        char = Python 'B', Perl 'n' is unsigned short BE = Python 'H'.
        Squeezelite's cont_packet is opcode + metaint(u32 BE) + loop(u8);
        count/guids are ignored. `>IBH` = N(4 BE) + B(1) + H(2 BE).
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        payload = struct.pack(">IBH", metaint, 0, 0)  # metaint, loop, count
        frame = struct.pack(">H", 4 + len(payload)) + b"cont" + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent cont metaint=%d to %s", metaint, mac)
            # Ask the player to report the source's StreamTitle over STMu so
            # we can show the real song + artist without proxying the audio.
            # The 4-byte value is the metadata interval (matches icy-metaint).
            if metaint and metaint > 0:
                try:
                    await self.send_meta(mac, interval=metaint)
                except Exception as exc:
                    logger.warning("send_meta failed for %s: %s", mac, exc)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_stop_to_player(self, mac: str) -> bool:
        """Send a 'strm' stop command ('q') to a player."""
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        # strm command 'q' = flush/stop (Perl Squeezebox.pm:1096-1116 layout).
        frame = self._build_strm_control_frame("q")
        try:
            writer.write(frame)
            await writer.drain()
            # Perl stop(): stream('q') + streamingsocket(undef) +
            # readyToStream(1) (Squeezebox.pm:206-216). The player has no
            # stream any more, so the next play MUST re-stream (R0.5-P1).
            self._reset_strm_guard(mac, "strm 'q' (stop) sent")
            _streaming_note(mac, "Stop", suppress_notifications=True)
            logger.info("Sent strm 'q' (stop) to %s", mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_pause_to_player(self, mac: str, pause_ms: int = 0) -> bool:
        """Send a 'strm' pause command ('p') to a player.

        LMS semantics (Squeezebox.pm pause -> stream('p') without
        interval): replay_gain = 0 means the output is held paused
        INDEFINITELY (Squeezelite OUTPUT_STOPPED). A value > 0 would
        auto-resume after that many milliseconds. Resume is a separate
        command ('u'), see send_unpause_to_player.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        # replay_gain = pause interval in ms (0 = pause indefinitely).
        frame = self._build_strm_control_frame("p", pause_ms)
        try:
            writer.write(frame)
            await writer.drain()
            # Perl pause() = stream('p') → controller `Pause` → PAUSED with
            # resumeTime = playingSongElapsed (StreamingController.pm:1552-1586).
            _streaming_note(mac, "Pause")
            logger.info("Sent strm 'p' (pause=%d) to %s", pause_ms, mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_unpause_to_player(self, mac: str) -> bool:
        """Send a 'strm' unpause command ('u') — LMS resume.

        Squeezebox2.pm resume -> stream('u'); Squeezelite process_strm
        'u' with replay_gain 0 unpauses immediately.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        # replay_gain = unpause jiffies (0 = now).
        frame = self._build_strm_control_frame("u", 0)
        try:
            writer.write(frame)
            await writer.drain()
            # Perl resume() = stream('u') → controller `Resume` → PLAYING,
            # resumeTime cleared (StreamingController.pm:1620-1656).
            _streaming_note(mac, "Resume")
            logger.info("Sent strm 'u' (unpause) to %s", mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_skip_to_player(self, mac: str, seconds: int) -> bool:
        """Send a 'strm' skip-ahead command ('a').

        Perl (Slim/Player/Squeezebox.pm stream('a')) sets the replay-gain
        field to ``int(interval * 1000)`` — the skip interval in
        MILLISECONDS. Squeezelite discards the next N ms of decoded audio,
        which equals a forward seek.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        # replay_gain = skip interval in ms (Perl stream('a')).
        frame = self._build_strm_control_frame("a", int(seconds) * 1000)
        try:
            writer.write(frame)
            await writer.drain()
            # `strm 'a'` is Perl's skipAhead (Squeezebox2.pm:1120-1127) — a
            # sync correction, NOT the controller `Skip` event. It must not
            # advance the song, so the machine is only told about it.
            try:
                from lyrion.player import streaming
                streaming.note_skip_ahead_sent(mac, int(seconds) * 1000)
            except Exception as exc:  # noqa: BLE001
                logger.debug("skipAhead note failed for %s: %s", mac, exc)
            logger.info("Sent strm 'a' (skip-ahead %ds) to %s", seconds, mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_volume_to_player(self, mac: str, volume: int) -> bool:
        """Send an 'audg' volume frame to a player.

        audg_packet: opcode(4) old_gainL(4) old_gainR(4) adjust(1) preamp(1)
        gainL(4) gainR(4) sequenceNumber(4) — gains are 0..65536
        (0-100% * 655.36), BE.

        The trailing sequence number is the client's last `seq_no` param
        (Client.pm:208-210, set in Commands.pm:559-562/:2586-2589). Perl
        always sends the 7-field form because the property defaults to 0
        and is therefore always defined (Squeezebox2.pm:312-317) — SqueezePlay
        compares it against its own counter to decide whether a locally
        maintained parameter (volume/power) is in sync (Player.lua:1223-1333);
        without it the client reverts the volume and re-sends forever.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        volume = max(0, min(100, int(volume)))
        try:
            from lyrion.player.manager import PlayerManager
            pstate = PlayerManager().get_player(mac)
        except Exception:
            pstate = None
        seq_no = int(getattr(pstate, "seq_no", 0) or 0)
        # Perl volume(): newGain from the dB curve, oldGain from @volume_map,
        # dvc = digitalVolumeControl pref, preamp = 255 - 2*preampVolumeControl
        # (Squeezebox2.pm:283-303, defaults Player.pm:38-39). Balance is not
        # applied (default 0 → left/right factor 1, Squeezebox2.pm:305-307).
        # Perl liest beide Werte aus den Client-Prefs (Squeezebox2.pm:283-303):
        #   $dvc    = $prefs->client($client)->get('digitalVolumeControl')
        #   $preamp = 255 - int(2 * ($prefs->client($client)->get('preampVolumeControl') || 0))
        # playerpref schreibt genau diese Prefs — die Prefs haben Vorrang vor
        # dem State-Feld, damit ein playerpref sofort im audg-Frame wirkt.
        _prefs = getattr(pstate, "playerprefs", None) or {}
        if "digitalVolumeControl" in _prefs:
            try:
                dvc = 0 if int(float(str(_prefs["digitalVolumeControl"]))) == 0 else 1
            except (TypeError, ValueError):
                dvc = 1 if getattr(pstate, "digital_volume_control", True) else 0
        else:
            dvc = 1 if getattr(pstate, "digital_volume_control", True) else 0
        # The gain must come from THIS player's curve: jive compares it against
        # its own table and reverts/retries the volume otherwise.
        model = getattr(pstate, "model", "") or "squeezebox2"
        gain = audg_gain(volume, model)
        old_gain = audg_old_gain(volume)
        payload = b"".join([
            b"audg",
            struct.pack(">I", old_gain),  # old_gainL (Squeezebox2.pm:285/:309)
            struct.pack(">I", old_gain),  # old_gainR
            bytes([dvc]),                 # adjust: digitalVolumeControl pref
            # preamp: Player.pm:39 default 0 → 255; playerpref kann ihn ändern.
            bytes([audg_preamp(int(getattr(pstate, "preamp_volume_control", 0) or 0))]),
            struct.pack(">I", gain),      # gainL (dB curve, not linear!)
            struct.pack(">I", gain),      # gainR
            struct.pack(">I", seq_no),  # sequenceNumber (Squeezebox2.pm:313)
        ])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent audg volume=%d gain=%d old=%d preamp=%d to %s",
                        volume, gain, old_gain, audg_preamp(), mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_aude(self, mac: str, enabled: bool = True) -> bool:
        """Enable/disable the player's audio outputs ('aude' frame).

        Perl ``Squeezebox2.pm:900-906`` sets BOTH bytes from one flag::

            my $data = pack('CC', $enabled, $enabled);   # spdif + dac
            $client->sendFrame('aude', \\$data);

        Frame (server→player): 2-byte BE length (opcode included!) + the
        4 ASCII bytes 'aude' + spdif(1) + dac(1).

        Callers (Perl): power off ``Player.pm:253`` → (0), power on
        ``Player.pm:268`` → (1), and once per HELO with the client's current
        power state ``Slimproto.pm:1265``. The base class is a no-op
        (``Player.pm:358``), so only the Squeezebox2 family (incl.
        squeezelite/jive) understands it.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.debug("send_aude: no writer for player %s", mac)
            return False
        byte = 1 if enabled else 0
        payload = b"aude" + bytes([byte, byte])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent aude (enabled=%d, spdif=%d, dac=%d) to %s",
                        byte, byte, byte, mac)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("send_aude to %s failed: %s", mac, exc)
            return False

    async def send_ir_to_player(self, mac: str, button_code: int) -> bool:
        """Send an 'irm' (infra-red / button) frame to a player.

        SlimProto irm packet: opcode(4) code(2, big-endian u16).
        The code is the numeric IR button code; named buttons
        ('play','pause',...) are resolved to codes by the caller.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        payload = b"".join([b"irm", struct.pack(">H", button_code & 0xFFFF)])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent irm code=%d to %s", button_code, mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_display_to_player(
        self, mac: str, line1: str, line2: str,
        duration: int = DISPLAY_DURATION_DEFAULT
    ) -> bool:
        """Send a 'grfe' (text display) frame to a player.

        grfe packet: opcode(4) format(1, 0x01 = two lines of text)
        duration(2, big-endian seconds) line1(0-term) line2(0-term).
        Software players (squeezelite/jive) render this on their UI.

        ``duration`` defaults to 1 s: Perl ``Display.pm:258``
        ``$duration = $args->{'duration'} || 1;  # duration - default to
        1 second`` (the ``displaytexttimeout`` pref is 1 as well,
        ``Slim/Utils/Prefs.pm:169``). Was an invented 3 s.

        Note: this is the software-player text-screen form of 'grfe'.
        Perl's own ``grfe`` sender is the graphic framebuffer form — offset,
        transition, param, bits (``Squeezebox2.pm:243-248``) — which lives in
        :meth:`send_display_framebuffer`; ``sendFrame('grfe', ...)`` appears
        nowhere else in this Perl tree (``Slim/Display/*.pm`` +
        ``Slim/Player/Squeezebox.pm``), so a caller that wants the Perl bytes
        must use the framebuffer sender.

        WARNING — no Perl counterpart (PROT-18 wiring, 2026-09-12): a census of
        every ``sendFrame`` in the Perl tree shows exactly one ``'grfe'``
        sender, ``Squeezebox2.pm:248``, and it carries
        ``pack('n', $offset) . $transition . pack('c', $param) . $bits``
        (:243-248) — never a text payload. There is no ``dispc`` opcode and no
        text-grfe anywhere in ``Slim/``. Perl puts text on a graphics display by
        RENDERING it (``Graphics.pm:106-489``) and sending the bitmap, and on a
        Text display by building a TextVFD stream (``Text.pm:437-441`` ->
        ``TextVFD.pm:312-357`` -> ``Squeezebox.pm:495-502``, opcode ``vfdc``).
        This method's ``format/duration/line1/line2`` payload is therefore an
        invention: it is kept only because ``tests/test_display_frame.py`` pins
        its bytes, and the display wiring
        (:mod:`lyrion.player.display.DisplayWiring`) deliberately never calls it
        — a real client would parse those bytes as a framebuffer header.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        payload = b"".join([
            b"grfe",
            bytes([0x01]),                 # format: two text lines
            struct.pack(">H", max(0, min(65535, duration))),
            line1.encode("utf-8", "replace"), b"\x00",
            line2.encode("utf-8", "replace"), b"\x00",
        ])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent grfe to %s", mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    # ── Display frames (PROT-18): visu / vfdc / grfb / grfe-framebuffer ──

    async def send_grfd_framebuffer(self, mac: str, framebuffer: bytes) -> bool:
        """Send a bitmapped-SB1 'grfd' framebuffer frame to a player.

        Perl ``Slim/Display/SqueezeboxG.pm:149-167`` (``drawFrameBuf``):::

            my $framebuf = pack('n', $GRAPHICS_FRAMEBUF_LIVE) . $$framebufref;
            my $len = length($framebuf);
            if ($len != $display->screenBytes() + 2) {
                $framebuf = substr($framebuf . chr(0) x $display->screenBytes(),
                                   0, $display->screenBytes() + 2);
            }
            $client->sendFrame('grfd', \\$framebuf);

        The header is a single u16 BE — ``$GRAPHICS_FRAMEBUF_LIVE = 1 * 280 * 2``
        = 560 (``SqueezeboxG.pm:34``), NOT the 4-byte offset/transition/param
        header of ``grfe`` (``Squeezebox2.pm:243-246``). The bitmap is padded
        with NULs or truncated to exactly ``screenBytes()`` =
        ``bytesPerColumn() * displayWidth()`` = 2 * 280 = 560
        (``Graphics.pm:93-103``, ``SqueezeboxG.pm:128``), so the data handed to
        ``sendFrame`` is always 562 bytes (2-byte header + 560-byte bitmap).

        Recipients: the bitmapped SB1 (``SqueezeboxG``), selected in
        ``Slimproto.pm:1058-1065`` (device id ``squeezebox`` + ``$bitmapped``).
        Its ``graphicCommand`` is ``'grfd'`` (``SqueezeboxG.pm:139-141``); the
        ``Squeezebox2`` family uses ``'grfe'`` and must not receive this frame.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.debug("send_grfd_framebuffer: no writer for player %s", mac)
            return False
        bits = bytes(framebuffer)
        expected = GRAPHICS_FRAMEBUF_LIVE  # screenBytes() = 560
        if len(bits) != expected:
            bits = (bits + bytes(expected))[:expected]
        payload = b"grfd" + struct.pack(">H", GRAPHICS_FRAMEBUF_LIVE) + bits
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent grfd framebuffer (%d bitmap bytes) to %s",
                        len(bits), mac)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("send_grfd_framebuffer to %s failed: %s", mac, exc)
            return False

    async def send_visu(self, mac: str, params: Sequence[int]) -> bool:
        """Send a 'visu' visualiser frame to a player.

        Perl ``Slim/Display/Squeezebox2.pm:259-291`` (``visualizer``)::

            my @params = @{$paramsref};
            my $which = shift @params;
            my $count = scalar(@params);
            my $parambytes = pack "CC", $which, $count;
            for my $param (@params) { $parambytes .= pack "N", $param; }
            $client->sendFrame('visu', \\$parambytes);
            $display->lastVisMode($paramsref);

        Payload: ``which``(1) + ``count``(1) + ``count`` × u32 BE, i.e.
        exactly ``[which, param0, param1, …]``. ``which`` is the visualiser
        type — 0 none, 1 vumeter, 2 spectrum analyser, 3 waveform
        (``:67-70``) — and the remaining values are the mode parameters
        documented at ``:42-65`` for the mode table at ``:72-129`` (e.g.
        mode 3 = ``[1, 0, 0, 280, 18, 302, 18]`` → ``01 06`` + six u32).
        A hidden visualiser is ``[0]`` → ``00 00`` (``:266-268``,
        ``Buttons/Power.pm:52``).

        Perl builds both leading bytes with ``pack "CC"`` and each parameter
        with ``pack "N"``; neither checks its range, both truncate modulo
        2**8 / 2**32 (verified: ``pack("CC", 300, -1)`` = ``2c ff``,
        ``pack("N", -1)`` = ``ff ff ff ff``). The same truncation is applied
        here so the bytes match for any input.

        Callers (Perl): ``drawFrameBuf`` (:241), ``updateScreen`` (:225),
        ``scrollInit`` (:325), ``scrollUpdateBackground`` (:334) and once on
        connect/reconnect — ``Player/Squeezebox.pm:134``
        ``$client->display->visualizer(1) if ($client->display->isa(
        'Slim::Display::Squeezebox2'))``. It returns early when the mode is
        unchanged and ``$forceSend`` is false (:274-276), so in practice one
        frame goes out per mode switch, not per redraw.

        Recipients: only displays derived from ``Slim::Display::Squeezebox2``
        — device id ``squeezebox2``/``softsqueeze`` (``Slimproto.pm:1038-1042``,
        ``:1083-1086``), ``boom`` (:1048-1051), ``transporter`` (:1053-1056).
        ``squeezeplay``/``controller`` (squeezelite) get
        ``Slim::Display::NoDisplay`` (:1103-1106) and their emulated SB2
        display stubs ``visualizer`` out (``EmulatedSqueezebox2.pm:25-32``).
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.debug("send_visu: no writer for player %s", mac)
            return False
        values = list(params)
        if not values:
            logger.warning("send_visu: empty params for %s — Perl shifts "
                           "undef for $which; sending 0/0", mac)
            which, rest = VISU_NONE, []
        else:
            which, rest = values[0], values[1:]
        # pack "CC" → one byte each, truncated mod 256 like Perl.
        payload = b"visu" + bytes([which & 0xFF, len(rest) & 0xFF])
        payload += b"".join(struct.pack(">I", p & 0xFFFFFFFF) for p in rest)
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent visu which=%d params=%s to %s",
                        which & 0xFF, rest, mac)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("send_visu to %s failed: %s", mac, exc)
            return False

    async def send_vfdc(self, mac: str, data: bytes) -> bool:
        """Send a 'vfdc' (VFD/character display) frame to a player.

        Perl ``Slim/Player/Squeezebox.pm:495-502``::

            sub vfd {
                my $client = shift;
                my $data = shift;
                if ($client->opened()) {
                    $client->sendFrame('vfdc', \\$data);
                }
            }

        The payload is the VFD byte stream produced by the text display
        library, passed through unchanged — this sender does not encode
        anything: ``Slim/Display/Lib/TextVFD.pm:312-357`` (``vfdUpdate``)
        builds it and ends with ``$client->vfd($vfddata)`` (:357). It is a
        Noritake/Futaba command stream: alternating code/argument bytes
        (``$vfdCodeCmd`` = 0x02, ``$vfdCodeChar`` = 0x03, ``:29-31``), the
        brightness prelude (``$noritakeBrightPrelude``, :51-55), custom
        character definitions (:323-330), ``$vfdReset`` (:56) then the
        escaped text (:337-342) and the cursor commands (:344-355).

        Perl enforces two invariants on that stream before sending (:359-362):
        the byte count must be even ("Odd vfddata") and ≤ 500
        ("VFDData too long"). Both are logged here as a warning — our own
        diagnostic, Perl dies — because the bytes are forwarded verbatim.

        ``Slim/Player/Squeezebox.pm:504-514`` is Perl's ``opened`` check:
        with no live socket ``vfd`` silently does nothing, which is our
        missing-writer/``is_closing`` early return.

        Recipients: ``Slim::Display::Text`` players —
        ``squeezeslave`` (``Slimproto.pm:1098-1101``), SB1 without bitmap
        (:1058-1070) — and anything else inheriting
        ``Slim::Player::Squeezebox::vfd``. SLIMP3 does NOT use this frame:
        it overrides ``vfd`` to send a UDP datagram
        (``Slim/Player/SLIMP3.pm:289-297``,
        ``'l                 ' . $data``).
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.debug("send_vfdc: no writer for player %s", mac)
            return False
        if len(data) % 2 or len(data) > VFD_MAX_BYTES:
            logger.warning(
                "send_vfdc: %d bytes to %s violates Perl's TextVFD "
                "invariants (even, <= %d; TextVFD.pm:361-362)",
                len(data), mac, VFD_MAX_BYTES)
        payload = b"vfdc" + bytes(data)
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent vfdc (%d bytes) to %s", len(data), mac)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("send_vfdc to %s failed: %s", mac, exc)
            return False

    async def send_grfb(self, mac: str, brightness_code: int) -> bool:
        """Send a 'grfb' (display brightness) frame to a player.

        Perl ``Slim/Display/Graphics.pm:496-509``::

            my $brightness = $display->SUPER::brightness($delta);
            if (defined($delta)) {
                my @brightnessMap = $display->brightnessMap;
                my $brightnesscode = pack('n', $brightnessMap[$brightness]);
                $display->client->sendFrame('grfb', \\$brightnesscode);
            }

        Payload: one u16 BE — ``brightnessMap[brightness]``, the raw 16-bit
        code, not a percentage. The maps:

        * Squeezebox2/Boom/Transporter → ``(65535, 0, 1, 3, 4)``
          (``Squeezebox2.pm:209-211``; ``Transporter.pm`` and ``Boom.pm``
          inherit it — only ``SqueezeboxG.pm:135-137`` and
          ``Boom.pm:180-196`` override it, Boom's is sensor-derived), indexed
          0..``maxBrightness`` = ``$#map`` = 4
          (``Graphics.pm:511-516``), so full brightness on an SB2 is
          ``0xFFFF`` and "off" is ``0x0000``;
        * SqueezeboxG (bitmapped SB1) → ``(0, 1, 4, 16, 30)``.

        The frame goes out only when ``$delta`` is defined; a
        read-only/initial call does not reach the player
        (``Graphics.pm:502``; the base ``Display.pm:378-398`` does not send
        anything). ``pack('n', …)`` truncates modulo 2**16, so the same
        masking is applied here.

        Callers (Perl): ``Player/brightness`` → ``display->brightness``
        (``Player.pm:162``), invoked on connect
        (``Squeezebox.pm:127``), on power on/off (``Player.pm:119``,
        ``:257``, ``:287``), for the idle/screensaver dim
        (``Squeezebox.pm:368``, ``:409-410``) and per ``showBriefly``
        brightness (``Display.pm:307-312``, ``:336-337``).

        Recipients: the Graphics displays of ``Squeezebox2.pm:25``
        (SB2/SB3, softsqueeze), ``Boom.pm:25``, ``Transporter.pm:26``,
        ``SqueezeboxG.pm:25`` — i.e. real Logitech hardware.
        ``squeezeplay``/``controller`` (squeezelite/jive) have
        ``NoDisplay`` (``Slimproto.pm:1103-1106``), a class with no
        ``brightnessMap``, so nothing is ever sent to them.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.debug("send_grfb: no writer for player %s", mac)
            return False
        payload = b"grfb" + struct.pack(">H", int(brightness_code) & 0xFFFF)
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent grfb brightness=0x%04X to %s",
                        int(brightness_code) & 0xFFFF, mac)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("send_grfb to %s failed: %s", mac, exc)
            return False

    async def send_display_framebuffer(
        self, mac: str, framebuffer: bytes, offset: int = 0,
        transition: str = DISPLAY_TRANSITION_DEFAULT, param: int = 0
    ) -> bool:
        """Send a graphic 'grfe' framebuffer frame to a player.

        Perl ``Slim/Display/Squeezebox2.pm:230-250`` (``drawFrameBuf``)::

            my $framebuf = pack('n', $offset) .    # offset [transporter
                                                   # screen 2 = offset of 640]
                            $transition .          # transition
                            pack('c', $param) .    # param byte
                            $$framebufref;
            $client->sendFrame('grfe', \\$framebuf);

        Header: offset(u16 BE) + transition(1 ASCII char, default ``'c'``
        at ``:234``) + param(1 signed byte, default 0 at ``:235``), then the
        bitmap. ``scrollHeader`` (``:338-341``) is exactly that 4-byte
        header — ``pack('n', 0) . 'c' . pack('c', 0)`` = ``00 00 63 00`` —
        which is the fastest proof of the layout; ``Graphics.pm:542``
        prepends it for client-side scrolling and calls the same
        ``sendFrame($display->graphicCommand, …)``, with ``graphicCommand``
        = ``'grfe'`` (``Squeezebox2.pm:213-214``). SqueezeboxG's bitmapped
        SB1 uses ``'grfd'`` (``SqueezeboxG.pm:139-141``, its own
        ``drawFrameBuf`` at :149-167) and is intentionally not this method.

        Bitmap size for the SB2 panel: 4 bytes/column × 320 columns = 1280
        bytes (``Squeezebox2.pm:180-182`` + ``:188-190``;
        :meth:`DISPLAY_FRAMEBUF_BYTES_SB2`). The 320x32 SB2 has one screen,
        the Transporter two — screen 2 sits at offset 640
        (``Transporter.pm:206-209``, ``:276-286``).

        Bounds, all Perl ``pack`` semantics (no range check, truncation):
        ``offset`` mod 2**16, ``param`` mod 2**8 as a signed byte
        (verified: ``pack("c", -1)`` = ``ff``, ``pack("c", 300)`` = ``2c``).
        ``transition`` must be exactly one byte long — Perl just
        concatenates the string, so a longer one would silently produce a
        5-byte header and a corrupt frame; that ValueError is our guard, not
        Perl's (Perl has none).

        Callers (Perl): ``updateScreen`` (:218-227, screen change), the
        push/bump animations (:401-417) which pass ``'r'``/``'l'``/``'u'``/
        ``'d'``/``'L'``/``'R'``/``'U'``/``'D'`` plus the screen extent as
        ``param``, and Transporter's two-screen update. Recipients are the
        ``Squeezebox2`` family (``Squeezebox2.pm:213-214``): SB2/SB3,
        softsqueeze, Boom, Transporter. ``squeezeplay``/``controller``
        (squeezelite/jive) get ``NoDisplay`` (``Slimproto.pm:1103-1106``);
        their emulated SB2 display has ``drawFrameBuf {}``
        (``EmulatedSqueezebox2.pm:25-26``), so the framebuffer never reaches
        a soft player.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.debug("send_display_framebuffer: no writer for player %s",
                         mac)
            return False
        if isinstance(transition, bytes):
            transition_bytes = transition
        else:
            transition_bytes = transition.encode("latin-1", "replace")
        if len(transition_bytes) != 1:
            raise ValueError(
                "grfe transition must be exactly one byte "
                f"(Perl Squeezebox2.pm:234), got {transition!r}"
            )
        payload = (
            b"grfe"
            + struct.pack(">H", int(offset) & 0xFFFF)
            + transition_bytes
            + bytes([int(param) & 0xFF])
            + bytes(framebuffer)
        )
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info(
                "Sent grfe framebuffer offset=%d transition=%r param=%d "
                "(%d bitmap bytes) to %s",
                int(offset) & 0xFFFF, transition_bytes, int(param) & 0xFF,
                len(framebuffer), mac)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("send_display_framebuffer to %s failed: %s",
                           mac, exc)
            return False

    def _handle_bye_frame(self, mac_str: str, payload: bytes) -> None:
        """Perl ``_bye_handler`` — ``Slim/Networking/Slimproto.pm:921-937``.

        The comment in Perl is "THIS IS ONLY FOR THE OLD SDK4.X UPDATER": a
        payload of ``chr(1)`` asks the server to put the player into
        firmware-upgrade mode (Perl: ``sleep(2); unblock();
        upgradeFirmware()``). Perl never closes the control connection here —
        closing on ``BYE!`` was our own invention (audit B1, live: the frame
        went to the debug branch because we compared a lower-cased opcode
        against ``"bye"``). We have no firmware image, so the request is
        logged and the player keeps streaming.
        """
        logger.info("Player %s sent BYE! ('Saying goodbye')", mac_str)
        if payload == b"\x01":
            logger.info(
                "Player %s requests a firmware upgrade (BYE! chr(1)) — no "
                "firmware image available, connection stays open", mac_str)

    def _handle_dsco_frame(self, mac_str: str, payload: bytes) -> None:
        """Perl ``_disco_handler`` — ``Slim/Networking/Slimproto.pm:599-679``.

        The data channel reported a disconnect. Perl logs the reason byte
        (0..4, ``%reasons`` at :603-609), warns on a non-zero reason, resets
        ``connecting``/``readyToStream`` if the player never finished
        connecting, and KEEPS the control connection open so the buffered
        audio plays out (squeezelite sends DSCO right after the strm for fast
        local files, because the whole file is buffered instantly).
        """
        reason = payload[0] if payload else 0
        text = DISCO_REASONS.get(reason, f"unknown reason {reason}")
        logger.info("Squeezebox got disconnection on the data channel: %s (%s)",
                    text, mac_str)
        if reason:
            logger.warning("Unexpected data stream disconnect type: %s", text)
        # Perl: if ($client->connecting()) { connecting(0); readyToStream(1) }
        # Our equivalent of "the player can accept a stream again" is clearing
        # the strm idempotency guard, so a replay of the same track is sent.
        SlimProtoClient._reset_strm_guard(mac_str, "DSCO")

    def _handle_player_event_frame(self, mac_str: str, op_raw: str,
                                   payload: bytes) -> None:
        """Recognise the remaining Perl message handlers (socket stays open).

        ``Slim/Networking/Slimproto.pm:52-72``: IR/BUTN/KNOB/RAWI map buttons
        and infrared codes (PROT-19), ANIC/ALSS/UREQ/DBUG/BODY are display,
        ambient-light, firmware-update, debug and HTTP-body frames. Perl runs
        the handler and keeps the connection, so the framing/parsing of this
        loop is untouched here.

        ``IR  ``/``BUTN``/``KNOB`` now hand their payload to
        :mod:`lyrion.player.buttons` (:meth:`_dispatch_button_frame`), the
        same 8-hex-code lookup Perl does (``Slimproto.pm:539`` ``'NxxH8'`` /
        ``:1275`` ``'NH8'`` -> ``IR::enqueue`` at ``:541``/``:1277``). The
        dispatch is scheduled on the running loop so a slow action never
        stalls this read loop; the frame is still logged with its Perl
        handler name.
        """
        handler = PERL_HANDLER_NAMES.get(op_raw, "?")
        logger.debug("Player %s frame %r -> Perl %s (%d bytes)",
                     mac_str, op_raw, handler, len(payload))
        if op_raw not in PERL_BUTTON_OPCODES:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (e.g. a bare unit-style call): the frame is
            # logged above and dropped, exactly like a handler with no work.
            return
        loop.create_task(self._dispatch_button_frame(mac_str, op_raw, payload))

    async def _dispatch_button_frame(self, mac_str: str, op_raw: str,
                                     payload: bytes) -> None:
        """Translate a button/IR/knob frame into a player action.

        Perl parity: ``IR  `` (``Slimproto.pm:521-546``), ``BUTN``
        (``:1270-1280``) and ``KNOB`` (``:1282-1318``). Both ``IR  `` and
        ``BUTN`` produce the same 8-hex code and share one lookup path
        (``Slimproto.pm:541`` / ``:1277`` call the same ``IR::enqueue``).
        Any failure is logged and swallowed — Perl never closes the socket
        for a malformed IR/BUTN frame either (``:530-537``).
        """
        from lyrion.player.buttons import (
            handle_button,
            handle_knob,
            parse_butn_frame,
            parse_ir_frame,
            parse_knob_frame,
        )
        try:
            if op_raw == "IR  ":
                parsed = parse_ir_frame(payload)
                if parsed is None:
                    logger.warning("bad length %d for IR. Ignoring (Perl "
                                   "Slimproto.pm:530-537)", len(payload))
                    return
                _tick, code = parsed
                await handle_button(mac_str, code, kind="ir")
            elif op_raw == "BUTN":
                _tick, code = parse_butn_frame(payload)
                await handle_button(mac_str, code, kind="butn")
            elif op_raw == "KNOB":
                _tick, position, sync = parse_knob_frame(payload)
                await handle_knob(mac_str, position, sync)
        except Exception as exc:  # noqa: BLE001 — a bad frame must not kill the loop
            logger.warning("Button/IR dispatch failed for %s (%r): %s",
                           mac_str, op_raw, exc)

    def _handle_resp_frame(self, mac_str: str, payload: bytes) -> None:
        """Handle a RESP frame: the player forwards the source's HTTP
        response headers after connecting for a direct stream (Squeezelite
        sendRESP). Extract icy-metaint and resolve the pending cont waiter
        (or send 'cont' directly if the server restarted in between).
        """
        try:
            text = payload.decode("latin1", errors="replace")
            m = re.search(r"(?im)^icy-metaint:\s*(\d+)\s*$", text)
            metaint = int(m.group(1)) if m else 0
            mac_key = mac_str.upper().replace(":", "")
            fut = self._resp_waiters.get(mac_key)
            if fut is not None and not fut.done():
                fut.set_result(metaint)
            else:
                # No waiter (e.g. server restarted between strm and RESP) —
                # send cont directly so the decoder still starts.
                asyncio.create_task(self._send_cont(mac_str, metaint))
            logger.info("RESP from %s: metaint=%d", mac_str, metaint)
        except Exception as exc:
            logger.warning("RESP parse failed for %s: %s", mac_str, exc)

    def _handle_meta_frame(self, mac_str: str, payload: bytes) -> None:
        """Handle a 'meta' frame from the player carrying the stream title.

        Squeezelite sends the source's Icecast/Shoutcast ``StreamTitle`` over
        a ``meta`` frame (id 0) as ``StreamTitle='Artist - Title';`` (NUL
        padded). Parse it and store artist + song on the player so the Now
        Playing panel shows the actual title without proxying the audio.
        """
        self._handle_stmu_frame(mac_str, payload)

    def _handle_stmu_frame(self, mac_str: str, payload: bytes) -> None:
        """Handle an STMu (stream metadata update) frame from the player.

        On a DIRECT stream Squeezelite parses the source's Icecast/Shoutcast
        ``StreamTitle`` itself and pushes it over STMu, so the server can show
        the actual song + artist in Now Playing without proxying the audio.
        The payload is ``StreamTitle='Artist - Title'`` (NUL-terminated).

        Perl's arrival point for the same information is
        ``Slim/Player/Protocols/HTTP.pm:271-357`` (``parseMetadata``, the ICY
        ``StreamTitle``) which hands it to
        ``Slim::Music::Info::setCurrentTitle`` — see
        :meth:`_notify_metadata_display` for what that fires.
        """
        try:
            text = payload.split(b"\x00")[0].decode("utf-8", errors="replace").strip()
            logger.info("STMu from %s: %r", mac_str, text)
            if not text:
                return
            import re as _re
            m = _re.search(r"StreamTitle='([^']*)'", text)
            if not m:
                # Some builds send the bare string without the key= prefix.
                m2 = _re.search(r"([^=]+?) - ([^-]+)$", text)
                if m2:
                    artist, song = m2.group(1).strip(), m2.group(2).strip()
                else:
                    artist, song = "", text.strip()
            else:
                full = m.group(1)
                if " - " in full:
                    artist, song = full.split(" - ", 1)
                else:
                    artist, song = "", full
            from lyrion.player.manager import PlayerManager
            player = PlayerManager().get_player(mac_str)
            if player is None:
                return
            stream_title = m.group(1) if m else text.strip()
            # ``Slim/Music/Info.pm:516``: everything below happens only when the
            # title really changed (``if (getCurrentTitle($client, $url) ne
            # ($title || ''))``).
            changed = str(getattr(player, "current_title", "") or "") != stream_title
            stream_url = str(getattr(player, "current_url", "") or "")
            player.remote_meta = {
                "title": song.strip(),
                "artist": artist.strip(),
                "streamtitle": stream_title,
                "url": stream_url,
            }
            player.current_title = stream_title
            if changed:
                # ``setCurrentTitle($url, $title, $client)`` — the client form,
                # which is the ONLY one that fires the ``playlist newsong``
                # notification (``Info.pm:526-546``; the 2-argument form used
                # by getMetaData/readTags at :476 stops after the callbacks).
                self._notify_metadata_display(player, stream_url, stream_title)
        except Exception as exc:
            logger.warning("STMu parse failed for %s: %s", mac_str, exc)

    @staticmethod
    def _notify_metadata_display(player: object, url: str = "",
                                 title: str = "") -> None:
        """Queue the display + newsong push of ``setCurrentTitle`` (``Info.pm:513-553``).

        Perl runs this chain when an in-stream metadata title changes::

            $client->metaTitle( $title );                       # :527
            for my $everybuddy ( $client->syncGroupActiveMembers() ) {
                $everybuddy->update();                          # :529-531
            }
            Slim::Control::Request::notifyFromArray(            # :534
                $client, [ 'playlist', 'newsong', $title ] );

        ``url`` is ``$url`` of that call — the :539-546 loop compares it with
        the URL every other playing player is on, and ``title`` is the new
        ``$title`` (the raw ``StreamTitle``, not a formatted display text).
        """
        mac = str(getattr(player, "mac", "") or "")
        if not mac:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                     # no loop (unit tests) — nothing to push
            return
        loop.create_task(_notify_metadata_display(mac, url, title))

    # STAT field offsets — Perl `unpack('a4CCCNNNNnNNNNnNNn')`
    # (Slim/Networking/Slimproto.pm:768) == SqueezePlay SlimProto.lua:167-195.
    # (offset, size, name) in big-endian byte order.
    _STAT_FIELDS: tuple[tuple[int, int, str], ...] = (
        (4, 1, "num_crlf"),
        (5, 1, "mas_initialized"),
        (6, 1, "mas_mode"),
        (7, 4, "buffer_size"),
        (11, 4, "buffer_fullness"),
        (15, 4, "bytes_received_h"),
        (19, 4, "bytes_received_l"),
        (23, 2, "signal_strength"),
        (25, 4, "jiffies"),
        (29, 4, "output_buffer_size"),
        (33, 4, "output_buffer_fullness"),
        (37, 4, "elapsed_seconds"),
        (41, 2, "voltage"),
        (43, 4, "elapsed_milliseconds"),
        (47, 4, "server_timestamp"),
        (51, 2, "error_code"),
    )

    @staticmethod
    def parse_stat_payload(payload: bytes) -> dict:
        """Unpack a STAT payload exactly like Perl's ``_stat_handler``.

        Returns every field of the Perl unpack plus a few derived ones:

        ``event`` (str), ``num_crlf``, ``mas_initialized``, ``mas_mode``,
        ``buffer_size`` (Perl ``rptr``/decodeSize), ``buffer_fullness``
        (``wptr``/decodeFull), ``fullness`` (=buffer_fullness),
        ``bytes_received_h/l``, ``bytes_received``, ``signal_strength``,
        ``jiffies``, ``output_buffer_size``, ``output_buffer_fullness``,
        ``elapsed_seconds``, ``voltage``, ``elapsed_milliseconds``,
        ``server_timestamp``, ``error_code``, ``length``.

        Truncated/unknown-shape payloads never raise: fields that lie
        beyond the frame are reported as 0 (Perl gets ``undef`` there and
        forces ``error_code = 0`` for any length other than 53/57,
        Slimproto.pm:770-777).
        """
        n = len(payload)
        out: dict = {
            "event": payload[0:4].decode("ascii", errors="replace").strip("\x00"),
            "length": n,
        }
        for off, size, name in SlimProtoClient._STAT_FIELDS:
            out[name] = int.from_bytes(payload[off:off + size], "big") if n >= off + size else 0
        # 51-byte SqueezePlay/jive frames have no trailing error_code;
        # 53/57-byte frames do. Anything else is an older firmware whose
        # error_code Perl deliberately ignores.
        if n not in (53, 57):
            out["error_code"] = 0
        out["bytes_received"] = out["bytes_received_h"] * 2**32 + out["bytes_received_l"]
        # Perl aliases: bufferSize <- buffer_size, fullness <- buffer_fullness.
        out["fullness"] = out["buffer_fullness"]
        # Seconds of playback. Perl prefers elapsed_milliseconds
        # (Slimproto.pm:811-814 logs `elapsed_milliseconds / 1000`); some
        # firmware only fills the whole-second field.
        if out["elapsed_milliseconds"]:
            out["elapsed_seconds_precise"] = out["elapsed_milliseconds"] / 1000.0
        else:
            out["elapsed_seconds_precise"] = float(out["elapsed_seconds"])
        return out

    # An ``STMf`` arriving within this many seconds of our own ``strm 's'``
    # frame is the player's START HANDSHAKE (the ack that closes the OLD
    # stream, Perl Squeezebox2.pm:398-403 "always use a new stream"), not a
    # lost stream. Live: the STMf arrived in the SAME second as
    # ``Sent strm to 1C872C47FC36: track=9900 codec=m`` (13:45:45).
    STRM_START_HANDSHAKE_WINDOW_S = 3.0

    def _handle_stat_frame(self, mac_str: str, payload: bytes) -> None:
        """Parse a STAT frame from a player and drive the UI state machine.

        Layout documented in the module docstring (and enforced by
        ``parse_stat_payload``): Perl ``unpack('a4CCCNNNNnNNNNnNNn')``
        (Slimproto.pm:768). Live frames from the SqueezePlay on this
        network are 51 bytes (no error_code), squeezelite sends 53.
        """
        try:
            stat = self.parse_stat_payload(payload)
            event = stat["event"]
            jiffies = stat["jiffies"]
            # output buffer fullness (offset 33) — counts DOWN as the audio
            # drains. End-of-track = STMt while the output buffer has
            # fully drained after the decoder ran dry (STMd).
            out_fullness = stat["output_buffer_fullness"]
            logger.debug(
                "STAT from %s: event=%s jiffies=%d elapsed=%ss (ms=%d) "
                "out_fullness=%d/%d fullness=%d signal=%d bytes=%d "
                "srv_ts=%d err=%d len=%d hex=%s",
                mac_str, event, jiffies,
                stat["elapsed_seconds_precise"], stat["elapsed_milliseconds"],
                out_fullness, stat["output_buffer_size"],
                stat["fullness"], stat["signal_strength"],
                stat["bytes_received"], stat["server_timestamp"],
                stat["error_code"], stat["length"], payload[:60].hex())

            # event "setd" carries the player-assigned name (squeezelite/IPAD style)
            if event == "setd":
                # Try to extract a printable name from the payload tail
                try:
                    tail = payload[4:]
                    # Name is NUL-terminated ASCII somewhere after the event field
                    for sep in (b"\x00", b"\xff", b"\x00\x00"):
                        idx = tail.find(sep)
                        if idx > 0:
                            raw_name = tail[:idx]
                            try:
                                candidate = raw_name.decode("utf-8").strip()
                            except UnicodeDecodeError:
                                # SqueezePlay sends the name in latin-1
                                # (e.g. "Küche" -> b'K\xfcche'); utf-8 with
                                # errors="replace" would store "K�che".
                                candidate = raw_name.decode("latin-1").strip()
                            if candidate and candidate.isprintable() and len(candidate) < 64:
                                logger.info("STAT setd from %s: name=%r (payload %d bytes)", mac_str, candidate, len(payload))
                                from lyrion.player.manager import PlayerManager
                                PlayerManager().rename_player(mac_str, candidate)
                                break
                except Exception as exc:
                    logger.debug("STAT setd parse failed for %s: %s", mac_str, exc)

            # Update player state (mode) if possible
            try:
                from lyrion.player.manager import PlayerManager
                pm = PlayerManager()
                player = pm.get_player(mac_str)
                if player is not None:
                    # Persist the full STAT field set (PROT-15). The status
                    # 'time' uses elapsed; prefer the ms field as Perl does
                    # (`elapsed_milliseconds / 1000`, Slimproto.pm:811-814).
                    try:
                        _elapsed = stat["elapsed_seconds_precise"]
                        # A pause is NOT a stop: while paused the status must
                        # keep reporting the frozen position (Perl
                        # `playingSongElapsed` returns `resumeTime` when
                        # paused, StreamingController.pm:1719-1724; the
                        # playpoint is only extrapolated while isPlaying(1),
                        # Squeezebox2.pm:456). So while the player is paused —
                        # or is acking a pause (STMp/pause) — a STAT that
                        # reports 0 must not zero the position.
                        _pausing = event in ("STMp", "pause")
                        if _elapsed or not (player.mode == "pause" or _pausing):
                            player.elapsed = _elapsed
                    except Exception:
                        pass
                    # signal_strength follows Perl's rule (Slimproto.pm:468-478
                    # signalStrength): only 0..100 is a wireless percentage;
                    # anything else (0xffff = "unknown", >100 = wired) makes
                    # Perl return undef, i.e. the status reports 0. There is
                    # no `& 0xFF` masking in Perl.
                    _ss = stat["signal_strength"]
                    player.signal_strength = _ss if 0 < _ss <= 100 else 0
                    # Keep the whole decoded struct for diagnostics/sync
                    # (Perl keeps it in %status and exposes
                    # getPlayPointData -> jiffies/elapsed_ms/elapsed_s).
                    # `_stat` is declared on PlayerState (state.py).
                    player._stat = stat
                    if event == "STMt":
                        # TIMING heartbeat (~1/s while the output is RUNNING).
                        # Perl playerStatusHeartbeat → controller `StatusHeartbeat`
                        # (StreamingController.pm:2363-2369).
                        _streaming_note(mac_str, "StatusHeartbeat")
                        # This branch carries BOTH meanings; the former
                        # duplicate `elif event == "STMt"` further down was
                        # unreachable dead code and is merged here (R0.5-P3):
                        #
                        # 1. NATURAL TRACK END: the decoder ran dry earlier
                        #    (STMd seen after the track started) AND the output
                        #    buffer has fully drained. Perl's player reports
                        #    STMu/playerStopped then and the controller advances
                        #    (Squeezebox2.pm:162-163). The player no longer
                        #    holds this stream, so drop the strm guard BEFORE
                        #    advancing (R0.5-P1) — otherwise a replay of the
                        #    same track is swallowed.
                        # 2. RECONCILE: the tick is only sent while the output
                        #    runs, so if the UI state says otherwise (e.g. an
                        #    STMo start underrun flipped it) correct it to
                        #    play — remote streams only, they never "end".
                        stmd_at = getattr(player, "_last_stmd", None)
                        started_at = getattr(player, "_track_started_at", None)
                        # "already playing" evidence: the player's clock is
                        # MOVING. A frozen elapsed is NOT playback — the
                        # wedge repeats STMt for minutes with elapsed pinned
                        # at 49.411s (LIVE, 13:45:46+), so only an advancing
                        # clock may arm the criterion.
                        try:
                            _prog = stat["elapsed_seconds_precise"]
                            _prev = player._last_elapsed_seen
                            if (player.mode == "play" and _prog > 0
                                    and _prog > _prev + 0.25):
                                player.playing_track_id = (
                                    player.strm_sent_track
                                    if player.strm_sent_track is not None
                                    else player.current_track_id
                                )
                            if _prog > 0:
                                player._last_elapsed_seen = _prog
                        except Exception:
                            pass
                        # squeezelite sends STMd just BEFORE STMs at track
                        # start; only an STMd from after the start is "decoder
                        # is really dry".
                        stmd_after_start = bool(
                            stmd_at and (started_at is None or stmd_at > started_at)
                        )
                        if (player.mode == "play" and out_fullness == 0
                                and stmd_after_start):
                            player._last_stmd = None  # consume the signal
                            player.forget_stream()
                            asyncio.create_task(_advance_after_track(pm, mac_str))
                        # NOTE: the heartbeat must NOT set mode=play by itself.
                        # Perl's STAT dispatch (Squeezebox2.pm:150-178) has no
                        # 'STMt' branch — it falls through to the final `else`
                        # = playerStatusHeartbeat, which changes no playing
                        # state; 'STMs' (playerTrackStarted, :170-171) is what
                        # starts playback. The old blanket
                        # `elif mode != play and remote: mode = play` therefore
                        # resurrected a stream the user had just stopped
                        # (live: after `strm 'q'` the next heartbeat tick
                        # flipped the status back to play).
                    elif event == "STMd":
                        # DECODE_COMPLETE — decoder has no more data. With
                        # small local files this fires LONG before the
                        # track has finished PLAYING: squeezelite decodes
                        # the whole file into its output buffer within
                        # milliseconds. The real LMS treats STMd as
                        # "start preloading the next track", NOT as
                        # track end. Track end = underrun (STMu/STMo with
                        # empty output buffer) or an STMt after drain.
                        # NOTE: squeezelite emits STMd just BEFORE STMs at
                        # track start (decode of the first buffer chunk),
                        # so record it only when a track is already
                        # running; otherwise remember it as "pending" and
                        # let the following STMs confirm the start.
                        player._last_stmd = time.time()
                        # Track the decode format for diagnostics: a FLAC
                        # that decodes but stays silent shows STMs/STMd
                        # exactly like a healthy WAV — compare codecs.
                        player._last_stmd_codec = getattr(player, "_current_codec", "")
                    elif event == "STMs":
                        # TRACK_STARTED — a new track started playing.
                        # Perl playerTrackStarted → controller `Started`
                        # (StreamingController.pm:2250-2266); the resulting
                        # play state gives the status mode (:1676-1683).
                        player.mode = _streaming_mode(mac_str, "Started", "play")
                        player.pause_requested = False
                        player._track_started_at = time.time()
                        # The player demonstrably runs the track we streamed
                        # (Perl `playerStarted`, Squeezebox2.pm:162-163): this
                        # is the handshake-independent "already playing"
                        # criterion. Fall back to the current track when a
                        # stray stop-ack already dropped ``strm_sent_track``
                        # — otherwise the next re-play would flush+restart the
                        # very track that is running (the LIVE wedge).
                        player.playing_track_id = (
                            player.strm_sent_track
                            if player.strm_sent_track is not None
                            else player.current_track_id
                        )
                        player._last_elapsed_seen = 0.0  # elapsed restarts
                    elif event == "STMf":
                        # FLUSH/CLOSE ack — the player flushed its buffers and
                        # closed the stream. Perl: flush() = stream('f') +
                        # readyToStream(1) (Squeezebox2.pm:387-396), stop() =
                        # stream('q') + streamingsocket(undef) +
                        # readyToStream(1) (Squeezebox.pm:206-216).
                        #
                        # BUT the player ALSO sends an STMf as the FIRST frame
                        # of its start handshake for our OWN strm: Perl's
                        # play() is ``streamBytes(0); closeStream(); new strm``
                        # (Squeezebox2.pm:398-403, "always use a new stream").
                        # Treating that ack as "stream lost" disarmed the
                        # guard AND reported a stop mid-start — LIVE at
                        # 13:45:45: `Sent strm ... track=9900` → STMf → STMc →
                        # STMo → minutes of STMt with a FROZEN elapsed=49.411s
                        # and out_fullness pinned at 3520512/3528000 (99.8 %):
                        # no audio, mode=stop, old title on screen. An STMf
                        # inside the handshake window therefore keeps both the
                        # guard and the mode.
                        #
                        # The marker is ``strm_sent_at`` alone (set by
                        # ``_after_strm_sent`` on EVERY strm frame, file and
                        # direct radio alike — Perl clears the start handshake
                        # per frame, Squeezebox.pm:567). Requiring a track id
                        # here meant a radio stream — which has none — never
                        # armed the handshake: its own STMf was read as "stream
                        # lost" and the status dropped to `mode:stop` while the
                        # stream ran (LIVE 2026-09-14 18:59:41, SqueezePlay:
                        # `Sent DIRECT strm` → STMf → `Stopped ... _Invalid` →
                        # `mode: stop`, only STMs 10 s later flipped it).
                        _handshake_ack = bool(
                            player.strm_sent_at
                            and (time.time() - player.strm_sent_at)
                            <= self.STRM_START_HANDSHAKE_WINDOW_S
                        )
                        if _handshake_ack:
                            logger.debug(
                                "STMf from %s within %.1fs of our strm — start "
                                "handshake, keeping the guard (track=%s)",
                                mac_str, self.STRM_START_HANDSHAKE_WINDOW_S,
                                player.strm_sent_track,
                            )
                        else:
                            # A player-initiated flush/close: the player no
                            # longer holds the stream we sent, so drop the
                            # guard (R0.5-P1) — a replay of the SAME track
                            # must stream again.
                            player.forget_stream()
                            # Perl playerStopped → controller `Stopped`
                            # (StreamingController.pm:2223-2248).
                            _streaming_note(mac_str, "Stopped")
                            # Mark stop only if the server didn't already
                            # (natural end vs. user stop / pause-stop); the
                            # value itself follows the controller
                            # (Source.pm:55-64 → Queries.pm:4081).
                            if player.mode not in ("pause",):
                                player.mode = cast(
                                    PlayerMode,
                                    _reported_playmode(mac_str) or "stop")
                                player.pause_requested = False
                            else:
                                player.pause_requested = False  # pause-ack
                    elif event == "STMp":
                        # PAUSE ack. The player merely holds its output: the
                        # stream (and the idempotency guard) stays valid, so
                        # resume continues in place (strm 'u',
                        # Squeezebox2.pm:1104-1110). The guard must only be
                        # dropped where the player demonstrably lost the
                        # stream (stop/track end/STMf/STMn).
                        player.mode = _streaming_mode(mac_str, "Pause", "pause")
                        player.pause_requested = False
                    elif event == "STMr":
                        # RESUME ack — Perl `Resume` → PLAYING (:1620-1656).
                        player.mode = _streaming_mode(mac_str, "Resume", "play")
                        player.pause_requested = False
                    elif event == "STMn":
                        # DECODE_ERROR — the player could not decode the
                        # stream; log and fall back to stop. Perl:
                        # readyToStream(1) + playerStreamingFailed
                        # (Squeezebox2.pm:153-157) — the track is gone, so
                        # drop the strm guard too (R0.5-P1).
                        logger.warning("STAT STMn (decode error) from %s", mac_str)
                        player.forget_stream()
                        # Perl playerStreamingFailed → `StreamingFailed`
                        # (StreamingController.pm:2298-2326) → stop.
                        player.mode = _streaming_mode(mac_str, "StreamingFailed", "stop")
                        player.pause_requested = False
                    elif event in ("STMo", "STMu"):
                        # OUTPUT_UNDERRUN (STMo legacy / STMu current) —
                        # harmless mid-stream, BUT when the decoder has
                        # run dry at some point (STMd seen) an underrun
                        # with an EMPTY output buffer means the buffered
                        # audio played out completely: natural track end.
                        logger.debug("STAT %s (underrun) from %s", event, mac_str)
                        # Perl playerOutputUnderrun → `OutputUnderrun` → _Rebuffer
                        # (StreamingController.pm:2286-2296, :1659-1671).
                        _streaming_note(mac_str, "OutputUnderrun")
                        stmd_at = getattr(player, "_last_stmd", None)
                        if player.mode == "play" and out_fullness == 0 and stmd_at:
                            player._last_stmd = None  # consume the signal
                            player.forget_stream()
                            asyncio.create_task(_advance_after_track(pm, mac_str))
                    elif event == "pause":
                        # Player-initiated pause (the user pressed pause on
                        # the device): the output is held, not closed — keep
                        # mode and the guard so a resume does not re-stream.
                        # Perl `Pause` → PAUSED (:133-139 row PLAYING).
                        _streaming_note(mac_str, "Pause")
                        player.mode = "pause"
                        player.pause_requested = False
                    elif event == "stop":
                        # A pause is implemented as strm 'q' (firmware does
                        # not honour strm 'p') — the resulting STAT stop is
                        # the pause-ack and must keep mode="pause". Either
                        # way the player flushed its stream (R0.5-P1).
                        player.forget_stream()
                        # Perl playerStopped → controller `Stopped` (:2223-2248).
                        _streaming_note(mac_str, "Stopped")
                        if player.pause_requested:
                            player.pause_requested = False
                            player.mode = "pause"
                        else:
                            player.mode = "stop"
                    elif event == "play":
                        player.mode = "play"
                        player.pause_requested = False
                    elif event == "load":
                        player.mode = "loading"
                        player.pause_requested = False
                    player.last_activity = __import__("time").time()
                    # Wake CLI subscribers so they push the fresh status.
                    try:
                        from lyrion.control.cli import CLIHandler
                        CLIHandler.notify_subscribers(player.mac)
                    except Exception:
                        pass
                    # Wake Cometd status/playerstatus subscribers (Android
                    # controllers) with the fresh player status — but ONLY
                    # when this STAT actually changed the status.  Perl's
                    # STAT heartbeat is not an event: every code without its
                    # own branch in ``statHandler`` (STMf, STMp, STMr, STMt,
                    # STMz …) goes to ``playerStatusHeartbeat``
                    # (Squeezebox2.pm:174-176), whose state-table entries are
                    # ``_NoOp`` (STOPPED) / ``_CheckSync`` (PLAYING — a no-op
                    # for a single player, StreamingController.pm:485-490)
                    # / ``_CheckPaused`` (:424-452) — none of them notifies,
                    # so Perl's ``status`` subscription is NOT re-executed
                    # (``statusQuery_filter``, Queries.pm:3925-3993).  A push
                    # per STAT tick flooded the client with ~1 event/s
                    # (measured: 15 pushes in 12 s against a stopped-idle
                    # player while Perl sends one per change).
                    # status-change gate (see above) — no push while unchanged
                    _changed = True
                    try:
                        from lyrion.player.manager import PlayerManager
                        _changed = PlayerManager().status_changed(player)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("status-change gate failed: %s", exc)
                    if not _changed:
                        logger.debug("STAT %s → status unchanged, no push",
                                     mac_str)
                    else:
                        try:
                            from lyrion.web.cometd import get_manager
                            _mgr = get_manager()
                            if _mgr is not None:
                                logger.debug("STAT %s → notify_player_status (%d cometd clients)",
                                             mac_str, len(getattr(_mgr, "_clients", {})))
                                asyncio.create_task(_mgr.notify_player_status(player.mac))
                            else:
                                logger.debug("STAT %s → no cometd manager", mac_str)
                        except Exception as exc:
                            logger.debug("notify_player_status dispatch failed: %s", exc)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Failed to parse STAT from %s: %s", mac_str, exc)

    async def send_meta(self, mac: str, interval: int = 1000) -> bool:
        """Ask a player to report Icecast/Shoutcast stream metadata.

        The original LMS sends a ``meta`` frame after a DIRECT stream so the
        player pushes the source's ``StreamTitle`` back over STMu — letting
        the server show the real song + artist WITHOUT proxying the audio.
        ``interval`` is the metadata report interval in ms.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        # 4-byte BE interval (uint32)
        payload = int(interval).to_bytes(4, "big")
        try:
            await self._send_frame(CMD_META, payload)
            logger.info("Sent meta (interval=%d) to %s", interval, mac)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Failed to send meta to %s: %s", mac, exc)
            return False

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def send_stat(self, stat: StatMessage) -> None:
        """Send a STAT message to the server."""
        await self._send_frame(CMD_STAT, stat.to_bytes())

    async def _send_aude_state(self, mac_str: str) -> None:
        """Send 'aude' with the player's current power state (Perl HELO path).

        ``Slimproto.pm:1262-1266`` runs on every HELO::

            $client->audio_outputs_enable($client->power());
            $client->volume($client->volume(), defined($client->tempVolume()));
        """
        try:
            from lyrion.player.manager import PlayerManager
            pstate = PlayerManager().get_player(mac_str)
            power = bool(pstate.power) if pstate is not None else True
        except Exception:  # noqa: BLE001
            power = True
        await self.send_aude(mac_str, power)

    async def send_cli(self, mac: str, command: str) -> bool:
        """Send a CLI command to a connected player over its TCP channel."""
        mac_clean = mac.upper().replace(":", "").replace("-", "")
        writer = self._player_writers.get(mac_clean)
        if writer is None or writer.is_closing():
            logger.warning("send_cli: no writer for player %s", mac)
            return False
        try:
            # For binary SlimProto players, send as EVNT frame
            payload = command.encode("utf-8") + b"\n"
            frame = struct.pack(">H", len(payload) + 4)
            writer.write(frame + b"strm" + payload)
            await writer.drain()
            return True
        except Exception as exc:
            logger.warning("send_cli to %s failed: %s", mac, exc)
            return False

    async def send_body(self, chunk: bytes) -> None:
        """Send a BODY chunk (audio data)."""
        await self._send_frame(CMD_BODY, chunk)

    async def send_stmu(self, metadata: bytes) -> None:
        """Send a STMU (stream metadata update)."""
        await self._send_frame(CMD_STMU, metadata)

    async def send_anic(self, image_data: bytes) -> None:
        """Send ANIC (album art / now-playing image)."""
        await self._send_frame(CMD_ANIC, image_data)

    # ------------------------------------------------------------------
    # End-of-track handling
    # ------------------------------------------------------------------


async def _advance_after_track(pm, mac_str: str) -> None:
    """Advance the playlist when the player reports track end (STAT STMd).

    LMS behaviour: after the last track the player stops; otherwise the
    next playlist item gets a new strm frame. Do NOT wrap around (LMS
    default has repeat off).

    A remote (radio) stream NEVER ends — an underrun there is just a
    buffer hiccup, not track end. Never advance/stop on it.

    Called on the end-of-track path only: the player demonstrably does not
    hold the stream we sent any more, so the strm idempotency guard is
    dropped FIRST — even when there is nothing to advance to (empty
    playlist, remote stream), else a replay of the same track is swallowed
    (R0.5-P1).
    """
    try:
        player = pm.get_player(mac_str)
        if player is not None:
            player.forget_stream()
        if player is None or not player.playlist:
            return
        if getattr(player, "remote", 0):
            logger.info("Underrun on remote stream %s — not a track end, "
                        "keeping playback", mac_str)
            return
        if player.playlist_position < len(player.playlist) - 1:
            logger.info("Track finished on %s — advancing playlist", mac_str)
            await pm.playlist_next(mac_str)
        else:
            logger.info("Last track finished on %s — stopping", mac_str)
            await pm.stop_player(mac_str)
    except Exception as exc:
        logger.warning("advance after track failed for %s: %s", mac_str, exc)
