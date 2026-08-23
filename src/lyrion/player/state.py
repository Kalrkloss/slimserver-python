"""Player state structures for Lyrion Music Server."""
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
    current_track_id: Optional[int] = None
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

    # SlimProto STAT bookkeeping (set by the protocol handler):
    _last_stmd: Optional[float] = None        # last DECODE_COMPLETE time
    _track_started_at: Optional[float] = None  # last STMs time

    def update_activity(self) -> None:
        """Mark the last activity timestamp to now."""
        self.last_activity = time.time()

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
