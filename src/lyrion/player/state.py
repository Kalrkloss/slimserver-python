"""Player state structures for Pyrion Music Server."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional


class PlaybackStatus(Enum):
    """Playback state enumeration."""
    STOPPED = "stop"
    PLAYING = "play"
    PAUSED = "pause"
    LOADING = "loading"


@dataclass
class PlayerState:
    """Represents the complete state of a single Squeezebox player.

    This dataclass mirrors the state that the LMS server maintains per-player,
    including hardware info, playback status, volume, sync relationships,
    and display state.
    """

    mac: str
    name: str
    ip: str
    port: int
    model: str = "squeezebox"
    firmware: str = "unknown"
    connected: bool = False
    power: bool = False
    volume: int = 50
    mode: Literal["stop", "play", "pause", "loading"] = "stop"
    # True between a pause command and a STAT stop-ack: the ack must NOT
    # flip mode back to "stop". A real pause is `strm 'p'`
    # (Squeezebox.pm:197-204) and normally has no stop-ack; the flag still
    # protects against a stray one.
    pause_requested: bool = False
    # Playback position (seconds) frozen at the moment of the pause. Perl
    # keeps it as the controller's ``resumeTime``
    # (Slim/Player/StreamingController.pm:64, set in ``_Pause`` :1555,
    # returned by ``playingSongElapsed`` :1719-1724 while paused) — the
    # status ``time`` reports THIS while paused and resume continues here.
    # A zero STAT (or a flush) must never overwrite it.
    pause_time: float = 0.0
    current_track_id: Optional[int] = None
    # Track id of the last strm frame actually written to the player (used
    # by the send-idempotency guard; NOT current_track_id, which the caller
    # sets optimistically before the frame goes out). Cleared on EVERY path
    # where the player no longer holds that stream (incoming STMf/STMn,
    # track end, stop/pause, disconnect/reconnect) — a stale value makes a
    # replay of the same track a silent no-op.
    strm_sent_track: Optional[int] = None
    # Wall-clock time the last strm 's' frame was written. The player
    # answers our OWN strm with a start handshake whose first frame is an
    # ``STMf`` (it closes the OLD stream — Squeezebox2.pm:398-403 "always
    # use a new stream"). An STMf inside this window is that ack, NOT
    # "stream lost": it must keep the guard and must not report a stop
    # (live: `Sent strm track=9900` → STMf → mode=stop, frozen elapsed,
    # 99.8 % full output buffer = the wedge).
    strm_sent_at: float = 0.0
    # Track id the player DEMONSTRABLY plays: set by ``STMs`` (track started,
    # Squeezebox2.pm:162-163) or by an ``STMt`` whose ``elapsed`` advanced —
    # a frozen elapsed is not playback (the wedge repeats STMt forever with
    # a frozen clock). Second, handshake-independent criterion for "already
    # playing → send nothing" (Perl StreamingController: state PLAYING +
    # same song → ``_Stream`` :1144 does nothing), so a stray stop-ack that
    # drops ``strm_sent_track`` cannot re-arm a redundant flush+strm.
    # Cleared with ``forget_stream()``.
    playing_track_id: Optional[int] = None
    # Last STAT ``elapsed`` seen for this player (progress detection).
    _last_elapsed_seen: float = 0.0
    # Track id a send_strm_to_player call is CURRENTLY sending. Claimed
    # synchronously before the first await and released in `finally`, so two
    # concurrent calls for the same track cannot both flush+stream.
    stream_in_flight: Optional[int] = None
    # P6-1: fields used by the status handlers were set via setattr —
    # declare them so tooling/linters see them and typos fail early.
    elapsed: float = 0.0          # seconds into the current track (STAT)
    duration: float = 0.0         # duration of the current track in seconds
    current_title: str = ""       # station name / override title
    current_url: Optional[str] = None  # currently streaming URL
    shuffle: int = 0              # playlist shuffle mode (0/1/2)
    repeat: int = 0               # playlist repeat mode (0/1/2)
    playlist_position: int = 0
    playlist_total: int = 0
    # SqueezePlay/controller parity fields (Perl status emits these):
    playlist_mode: str = "none"   # "none" | "repeat_one" | "repeat_all" | ...
    playlist_timestamp: float = 0.0
    seq_no: int = 0               # monotonically increasing status seq
    remote: int = 0               # 1 when playing a remote (stream) URL
    randomplay: int = 0           # "random play" mode active
    sleep_remaining: int = 0      # sleep-timer seconds remaining (0 = off)
    use_volume_control: bool = True
    remote_meta: dict = field(default_factory=dict)  # ICY/HTTP stream metadata
    playerprefs: dict = field(default_factory=dict)  # per-player prefs (playerpref)
    stream_titles: dict = field(default_factory=dict)  # stream URL -> display title
    stream_images: dict = field(default_factory=dict)  # stream URL -> logo/artwork path
    # P6-2: playlist holds track ids AND stream URLs (radio/favorites).
    playlist: list[int | str] = field(default_factory=list)
    sync_master: Optional[str] = None
    sync_slaves: list[str] = field(default_factory=list)
    display_state: Optional[dict] = None
    last_activity: float = field(default_factory=time.time)
    name_source: str = "device"  # "device" | "display" | "setd" (highest)

    # Extended fields for player capabilities
    is_player: bool = True
    can_power_off: bool = True
    can_sync: bool = True
    can_multi_sync: bool = True
    digital_volume_control: bool = True
    max_volume: int = 100
    signal_strength: int = 0
    display_width: int = 320
    display_height: int = 32
    display_lines: int = 4
    # Whether the player can do TLS itself (HELO cap "CanHTTPS=1").
    # False → https radio streams must be proxied by the server
    # (like the Perl LMS: canDirectStream honours CanHTTPS).
    can_https: bool = False
    # Audio formats this player can decode natively (filled from the HELO
    # 'Model' capability, like Perl LMS). Empty/falsy means "assume the
    # common set". Used to decide whether a source must be transcoded.
    supported_formats: set[str] = field(default_factory=set)
    # Mixer control values (Perl keeps them as client prefs):
    # bass/treble are tone controls, pitch is a speed/preamp setting, and
    # mute is a TEMPORARY gain of 0 that keeps the volume pref
    # (Commands.pm:559-640 ``fade_volume`` + 'tempVolume').
    # Defaults reproduce the live Perl LMS for our player types
    # (read-only ``mixer <entity> ?`` probe 2026-09-12: bass 0, treble 0,
    # pitch 100; ``mixer volume ?`` -> the volume pref).
    bass: int = 0
    treble: int = 0
    pitch: int = 100
    mute: bool = False

    # The player's own UUID from its HELO frame. Perl stores it on the client
    # and reports it as the ``uuid`` field of the players loop
    # (Queries.pm:2627 ``$eachclient->uuid()``) — it is NOT the MAC address.
    uuid: str = ""
    # Client's self-declared model name (HELO caps ModelName=, Perl
    # SqueezePlay.pm:82 _modelName) — the players loop 'modelname' field.
    # Empty means "fall back to the per-class modelName() table".
    model_name: str = ""

    # SlimProto STAT bookkeeping (set by the protocol handler):
    _last_stmd: Optional[float] = None        # last DECODE_COMPLETE time
    _track_started_at: Optional[float] = None  # last STMs time
    _last_stmd_codec: str = ""                # codec of the last STMd
    _stat: Optional[dict] = None              # last decoded STAT struct

    def update_activity(self) -> None:
        """Mark the last activity timestamp to now."""
        self.last_activity = time.time()

    def forget_stream(self) -> None:
        """The player demonstrably no longer holds the stream we sent.

        Drops BOTH halves of the strm idempotency guard
        (``strm_sent_track`` + ``playing_track_id``). Call it on every path
        where the stream is gone — an STMf that is NOT our own start
        handshake, STMn, a stop/track end, a disconnect/reconnect — but
        NEVER while the player merely holds its output (STMp/pause): a
        resume continues the same stream (strm 'u', Squeezebox2.pm:1104-1110).

        Keeping the guard armed too long is the worse failure: the play
        request reports success while the player stays silent.
        """
        self.strm_sent_track = None
        self.playing_track_id = None

    def to_dict(self) -> dict:
        """Return a plain dict representation."""
        return {
            "mac": self.mac,
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "model": self.model,
            "firmware": self.firmware,
            "connected": self.connected,
            "power": self.power,
            "volume": self.volume,
            "mode": self.mode,
            "current_track_id": self.current_track_id,
            "playlist_position": self.playlist_position,
            "playlist_total": self.playlist_total,
            "sync_master": self.sync_master,
            "sync_slaves": self.sync_slaves,
            "last_activity": self.last_activity,
        }

    @property
    def is_playing(self) -> bool:
        return self.mode == "play"

    @property
    def is_synced(self) -> bool:
        return self.sync_master is not None or bool(self.sync_slaves)

    @property
    def playback_status(self) -> PlaybackStatus:
        """Return PlaybackStatus enum matching current mode."""
        mapping = {
            "play": PlaybackStatus.PLAYING,
            "pause": PlaybackStatus.PAUSED,
            "loading": PlaybackStatus.LOADING,
        }
        return mapping.get(self.mode, PlaybackStatus.STOPPED)

    def __repr__(self) -> str:
        return (
            f"<PlayerState {self.mac} ({self.name}) "
            f"mode={self.mode} vol={self.volume} power={self.power}>"
        )
