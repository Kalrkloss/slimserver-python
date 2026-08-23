"""Player manager for Lyrion Music Server."""
from __future__ import annotations

import logging
import struct
import time
from datetime import datetime
from typing import Optional

from .state import PlayerState

logger = logging.getLogger(__name__)


# Formats a given squeeze-type player can decode natively. Mirrors the
# Perl-LMS model→support mapping (Slim::Utils::Misc / Types.pm): SqueezeLite
# and SqueezePlay decode the modern set; the classic Squeezebox family is
# narrower. Used to decide whether a source must be transcoded.
_COMMON_FORMATS = {"mp3", "flac", "aac", "ogg", "wav", "aiff", "pcm"}


def _formats_for_model(model: str) -> set[str]:
    """Return the set of audio extensions ``model`` plays natively."""
    m = (model or "").lower()
    if m.startswith("squeezelite") or m.startswith("squeezeplay"):
        return set(_COMMON_FORMATS)
    if m in ("squeezebox", "squeezebox2", "squeezebox3", "squeezeboxclassic"):
        # Classic hardware: MP3, FLAC, WAV, AIFF, OGG, PCM — but no AAC/ALAC.
        return {"mp3", "flac", "wav", "aiff", "ogg", "pcm"}
    if m.startswith("squeezeboxradio") or m.startswith("squeezeboxtouch"):
        return {"mp3", "flac", "aac", "wav", "aiff", "ogg", "pcm"}
    # Unknown model: assume the modern set (never blindly force a transcode).
    return set(_COMMON_FORMATS)


class PlayerManager:
    """Singleton manager for all connected Squeezebox players.

    Maintains the authoritative registry of players and provides methods to
    query and manipulate player state. Communicates with players via the
    SlimProto protocol (handled by the networking layer).
    """

    _instance: Optional[PlayerManager] = None

    def __new__(cls) -> PlayerManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self.players: dict[str, PlayerState] = {}
        self._protocol_handler = None
        logger.info("PlayerManager initialized")

    def set_protocol_handler(self, handler) -> None:
        """Inject the SlimProto server for sending commands to players."""
        self._protocol_handler = handler

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_player(
        self,
        mac: str,
        name: str,
        ip: str,
        port: int,
        model: str = "squeezebox",
        firmware: str = "unknown",
        name_source: str = "device",
        can_https: bool = False,
        supported_formats: set[str] | None = None,
    ) -> PlayerState:
        """Register a new player or update an existing one.

        Args:
            mac: Player MAC address (unique identifier).
            name: Human-readable name.
            ip: IP address of the player device.
            port: CLI port on the player.
            model: Player model string (e.g. "squeezeboxradio").
            firmware: Firmware version string.
            name_source: Where the name came from — "device" (HELO id),
                "display" (ModelName= from caps) or "setd" (client-confirmed).
                Higher-ranked sources win over lower ones on reconnect so a
                client that opens multiple connections (e.g. SqueezePlay with
                a control + compatibility session) cannot clobber the real
                name with its device identity.
            can_https: Whether the player can do TLS itself (HELO cap).
            supported_formats: Audio extensions this player can decode
                natively; used to decide whether a source must be transcoded.
                Falsy means "assume the common set" (see _formats_for_model).

        Returns:
            The PlayerState for this player.
        """
        # User-assigned name from DB has priority over the HELO name
        stored = self._stored_player_name(mac)
        if stored is not None:
            name = stored
            name_source = "setd"

        rank = {"device": 0, "display": 1, "setd": 2}

        if mac in self.players:
            player = self.players[mac]
            player.ip = ip
            player.port = port
            # Name priority: a higher-ranked source wins over the current one
            if rank.get(name_source, 0) >= rank.get(player.name_source, 0):
                player.name = name
                player.name_source = name_source
            player.model = model or player.model
            player.firmware = firmware
            player.can_https = can_https
            if supported_formats:
                player.supported_formats = set(supported_formats)
            elif not player.supported_formats:
                player.supported_formats = _formats_for_model(player.model)
            player.connected = True
            player.update_activity()
            logger.info("Player reconnected: %s (%s) src=%s", player.name, mac, player.name_source)
        else:
            player = PlayerState(
                mac=mac,
                name=name,
                name_source=name_source,
                ip=ip,
                port=port,
                model=model,
                firmware=firmware,
                connected=True,
                can_https=can_https,
                supported_formats=supported_formats or _formats_for_model(model),
            )
            self.players[mac] = player
            logger.info("Player registered: %s (%s) [%s:%d] src=%s", name, mac, ip, port, name_source)

        # Persist (INSERT OR IGNORE — keep any user-assigned name)
        self._save_player(player)

        return player

    # ------------------------------------------------------------------
    # Player name persistence (players table)
    # ------------------------------------------------------------------

    def _players_db_path(self) -> str:
        """Resolve the players.db path from the active config (test/dev runs
        use LYRION_SERVERDATA; production default stays /root/.lyrion)."""
        try:
            from lyrion.config import get_config
            return str(get_config().prefs_dir / "players.db")
        except Exception:  # noqa: BLE001
            return "/root/.lyrion/Lyrion/Prefs/players.db"

    def _stored_player_name(self, mac: str) -> str | None:
        """Return the *confirmed* name for a MAC from the players table.

        Only names stored with confirmed=1 (via SETD/rename_player) count —
        HELO placeholder rows (confirmed=0) never override a display name.
        """
        try:
            import sqlite3
            db = sqlite3.connect(
                f"file:{self._players_db_path()}?mode=ro",
                uri=True, timeout=0,
            )
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT name FROM players WHERE uuid = ? AND confirmed = 1 LIMIT 1",
                (mac,),
            ).fetchone()
            db.close()
            return row["name"] if row and row["name"] else None
        except Exception:
            return None

    def _save_player(self, player: PlayerState) -> None:
        """Persist the player row in a worker thread — never blocks the
        event loop."""
        try:
            import threading
            threading.Thread(
                target=self._save_player_sync,
                args=(player,),
                daemon=True,
            ).start()
        except Exception:
            pass

    def _save_player_sync(self, player: PlayerState) -> None:
        """Synchronous INSERT OR IGNORE (never overwrite a user-assigned
        name with a HELO name). Runs in a worker thread on players.db.
        confirmed=0: HELO placeholder — not treated as a real name."""
        try:
            import sqlite3
            db = sqlite3.connect(self._players_db_path(), timeout=2)
            db.execute("PRAGMA busy_timeout = 2000")
            db.execute(
                "CREATE TABLE IF NOT EXISTS players ("
                "uuid TEXT PRIMARY KEY, name TEXT, model TEXT, ip TEXT, "
                "port INTEGER, firmware TEXT, enabled INTEGER, lastseen INTEGER, "
                "confirmed INTEGER DEFAULT 0)"
            )
            db.execute(
                "INSERT OR IGNORE INTO players "
                "(uuid, name, model, ip, port, firmware, enabled, lastseen, confirmed) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0)",
                (player.mac, player.name, player.model or "",
                 player.ip or "", player.port or 0, player.firmware or "",
                 int(player.last_activity)),
            )
            db.commit()
            db.close()
        except Exception:
            pass

    def rename_player(self, mac: str, name: str) -> bool:
        """Rename a player (user-assigned name from SETD frame or API).

        Updates the in-memory state immediately and persists to the players
        table in a worker thread so the name survives reconnects (stored
        names have priority over HELO names in register_player).
        """
        if not name:
            return False
        if "\ufffd" in name:
            # Player sent a corrupted name (U+FFFD replacement char, e.g.
            # SqueezePlay that received the mangled name from an earlier
            # utf-8-errors=replace decode). Never let it clobber the
            # confirmed name from players.db.
            logger.warning("Rejecting player name containing U+FFFD: %r", name)
            return False
        player = self.get_player(mac)
        confirmed = False
        if player:
            player.name = name
            # A SETD name that just repeats the device model
            # (e.g. "SqueezePlay" for model "squeezeplay") is device
            # identity, not a real player name — don't rank it as confirmed.
            if name.lower() != (player.model or "").lower():
                player.name_source = "setd"
                confirmed = True
        try:
            import threading
            threading.Thread(
                target=self._rename_player_sync,
                args=(mac, name, confirmed),
                daemon=True,
            ).start()
        except Exception:
            pass
        logger.info("Player %s renamed to: %s", mac, name)
        return True

    def _rename_player_sync(self, mac: str, name: str, confirmed: bool = False) -> None:
        """Synchronous DB upsert for a renamed player (worker thread, players.db).
        confirmed=1 only when the name is a real player name (SETD), not a
        device identity that merely repeats the model."""
        try:
            import sqlite3
            import time as _t
            db = sqlite3.connect(self._players_db_path(), timeout=2)
            db.execute("PRAGMA busy_timeout = 2000")
            db.execute(
                "CREATE TABLE IF NOT EXISTS players ("
                "uuid TEXT PRIMARY KEY, name TEXT, model TEXT, ip TEXT, "
                "port INTEGER, firmware TEXT, enabled INTEGER, lastseen INTEGER, "
                "confirmed INTEGER DEFAULT 0)"
            )
            db.execute(
                "INSERT INTO players (uuid, name, enabled, lastseen, confirmed) "
                "VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, confirmed=excluded.confirmed",
                (mac, name, int(_t.time()), 1 if confirmed else 0),
            )
            db.commit()
            db.close()
        except Exception:
            pass

    def unregister_player(self, mac: str) -> None:
        """Remove a player from the registry.

        Args:
            mac: The MAC address of the player to remove.
        """
        player = self.players.pop(mac, None)
        if player:
            player.connected = False
            # Unsynchronize if needed
            if player.sync_slaves:
                for slave_mac in list(player.sync_slaves):
                    slave = self.get_player(slave_mac)
                    if slave:
                        slave.sync_master = None
                player.sync_slaves.clear()
            if player.sync_master:
                master = self.get_player(player.sync_master)
                if master and mac in master.sync_slaves:
                    master.sync_slaves.remove(mac)
                player.sync_master = None
            logger.info("Player unregistered: %s (%s)", player.name, mac)

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get_player(self, mac: str) -> Optional[PlayerState]:
        """Return the PlayerState for a MAC, or None.

        Accepts the MAC with or without colons, upper- or lowercase.
        """
        if mac in self.players:
            return self.players[mac]
        normalized = mac.replace(":", "").upper()
        for key, player in self.players.items():
            if key.replace(":", "").upper() == normalized:
                return player
        return None

    def get_all_players(self) -> list[PlayerState]:
        """Return all registered players as a list."""
        return list(self.players.values())

    def get_connected_players(self) -> list[PlayerState]:
        """Return only currently connected players."""
        return [p for p in self.players.values() if p.connected]

    def get_player_by_name(self, name: str) -> Optional[PlayerState]:
        """Find a player by name (case-insensitive partial match)."""
        name_lower = name.lower()
        for player in self.players.values():
            if player.name.lower() == name_lower:
                return player
            if name_lower in player.name.lower():
                return player
        return None

    def get_player_by_ip(self, ip: str) -> Optional[PlayerState]:
        """Find a player by IP address."""
        for player in self.players.values():
            if player.ip == ip:
                return player
        return None

    def get_sync_group(self, mac: str) -> list[PlayerState]:
        """Return all players in the same sync group as the given MAC."""
        player = self.get_player(mac)
        if not player:
            return []
        if player.sync_master:
            master = self.get_player(player.sync_master)
            if master:
                return [master] + [
                    self.players[s] for s in master.sync_slaves
                    if s in self.players and s != mac
                ]
        result = [player]
        result += [
            self.players[s] for s in player.sync_slaves
            if s in self.players
        ]
        return result

    # ------------------------------------------------------------------
    # State setters
    # ------------------------------------------------------------------

    def set_power(self, mac: str, on: bool) -> None:
        """Set power state of a player.

        Args:
            mac: Player MAC address.
            on: True for power on, False for power off.
        """
        player = self.get_player(mac)
        if not player:
            logger.warning("set_power: unknown player %s", mac)
            return
        player.power = on
        player.update_activity()
        self.send_command(mac, "power" if on else "power!")
        logger.debug("Player %s power: %s", mac, "on" if on else "off")

    async def set_volume(self, mac: str, volume: int) -> bool:
        """Set player volume (sends audg frame via the protocol handler).

        Args:
            mac: Player MAC address.
            volume: Volume level 0-100.

        Returns:
            True if the audg frame was sent to a connected player.
        """
        player = self.get_player(mac)
        if not player:
            return False
        volume = max(0, min(100, volume))
        player.volume = volume
        player.update_activity()
        handler = self._protocol_handler
        if handler is not None:
            return await handler.send_volume_to_player(player.mac, volume)
        return False

    def set_mode(self, mac: str, mode: str) -> None:
        """Set playback mode for a player.

        Args:
            mac: Player MAC address.
            mode: One of "stop", "play", "pause", "loading".
        """
        player = self.get_player(mac)
        if not player:
            return
        player.mode = mode
        player.update_activity()
        logger.debug("Player %s mode: %s", mac, mode)

    def set_current_track(self, mac: str, track_id: int) -> None:
        """Update the current track for a player.

        Args:
            mac: Player MAC address.
            track_id: Database track ID.
        """
        player = self.get_player(mac)
        if not player:
            return
        player.current_track_id = track_id
        player.update_activity()

    def set_playlist_info(
        self, mac: str, position: int, total: int
    ) -> None:
        """Update playlist position info.

        Args:
            mac: Player MAC address.
            position: Current track index (0-based).
            total: Total number of tracks in playlist.
        """
        player = self.get_player(mac)
        if not player:
            return
        player.playlist_position = position
        player.playlist_total = total

    # ------------------------------------------------------------------
    # Synchronisation
    # ------------------------------------------------------------------

    def sync_players(self, master_mac: str, slave_macs: list[str]) -> None:
        """Group players into a sync group with a master.

        Args:
            master_mac: MAC address of the sync master.
            slave_macs: List of MAC addresses to become slaves.
        """
        master = self.get_player(master_mac)
        if not master:
            logger.error("Sync master %s not found", master_mac)
            return

        # First unsync any existing relationships
        self.unsync_player(master_mac)

        for slave_mac in slave_macs:
            if slave_mac == master_mac:
                continue
            slave = self.get_player(slave_mac)
            if not slave:
                continue
            # Remove from any existing sync group
            if slave.sync_master:
                old_master = self.get_player(slave.sync_master)
                if old_master and master_mac in old_master.sync_slaves:
                    old_master.sync_slaves.remove(slave_mac)
            slave.sync_master = master_mac
            master.sync_slaves.append(slave_mac)
            self.send_command(slave_mac, f"sync {master_mac}")

        logger.info(
            "Synced %s as master with slaves: %s", master_mac, slave_macs
        )

    def unsync_player(self, mac: str) -> None:
        """Remove a player from its sync group.

        Args:
            mac: MAC address of the player to unsync.
        """
        player = self.get_player(mac)
        if not player:
            return

        if player.sync_master:
            # This player is a slave — remove from master list
            master = self.get_player(player.sync_master)
            if master and mac in master.sync_slaves:
                master.sync_slaves.remove(mac)
            player.sync_master = None
            self.send_command(mac, "sync -")
        elif player.sync_slaves:
            # This player is a master — unsync all slaves
            for slave_mac in list(player.sync_slaves):
                slave = self.get_player(slave_mac)
                if slave:
                    slave.sync_master = None
                    self.send_command(slave_mac, "sync -")
            player.sync_slaves.clear()

        logger.info("Unsynced player: %s", mac)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def send_command(self, mac: str, command: str, *args) -> None:
        """Send a CLI command to a player via SlimProto.

        Args:
            mac: Target player MAC address.
            command: CLI command string.
            *args: Additional arguments appended to the command.
        """
        if self._protocol_handler is None:
            logger.warning("No protocol handler set, cannot send command")
            return

        player = self.get_player(mac)
        if not player or not player.connected:
            logger.warning("Cannot send command to disconnected player %s", mac)
            return

        full_command = command
        if args:
            full_command = f"{command} {' '.join(str(a) for a in args)}"

        # The slimproto server has no text-CLI channel to players; this is
        # only meaningful for emulated/legacy clients. Keep it best-effort.
        send = getattr(self._protocol_handler, "send_cli", None)
        if send is None:
            logger.debug("send_cli not implemented — ignoring '%s' for %s",
                         full_command, mac)
            return
        try:
            send(mac, full_command)
        except Exception as e:
            logger.error("Failed to send command to %s: %s", mac, e)

    def broadcast_command(self, command: str, *args) -> None:
        """Send a CLI command to all connected players.

        Args:
            command: CLI command string.
            *args: Additional arguments.
        """
        for player in self.get_connected_players():
            self.send_command(player.mac, command, *args)

    # ------------------------------------------------------------------
    # Playback control (via SlimProto protocol handler)
    # ------------------------------------------------------------------

    async def play_track(self, player_id: str, track_id: int) -> bool:
        """Start playback of a track on a player (sends strm frame)."""
        player = self.get_player(player_id)
        if player is None:
            logger.warning("play_track: player not found: %s", player_id)
            return False
        handler = self._protocol_handler
        if handler is None:
            logger.warning("play_track: no protocol handler wired")
            return False
        # The HTTP proxy endpoint resolves a request without ?id= through the
        # player's playlist, exactly like LMS. Populate state before sending:
        # Squeezelite can connect immediately after receiving the strm frame.
        if not player.playlist:
            player.playlist = [track_id]
            player.playlist_position = 0
            player.playlist_total = 1
        elif track_id in player.playlist:
            player.playlist_position = player.playlist.index(track_id)
        else:
            # Track not yet in playlist: append it (LMS 'playlist play'
            # semantics = clear + load + play of that track, but keeping
            # an existing playlist and jumping to it is friendlier for
            # multi-item queues built with 'playlist add').
            player.playlist.append(track_id)
            player.playlist_position = len(player.playlist) - 1
            player.playlist_total = len(player.playlist)

        ok = await handler.send_strm_to_player(player.mac, track_id)
        if ok:
            player.power = True  # playing implies power-on
            player.mode = "play"
            player.current_track_id = track_id
            player.elapsed = 0.0
            # Track duration for status 'duration'/'time' queries — the
            # real LMS serves it from the DB as soon as the track loads.
            try:
                from sqlalchemy import select
                from lyrion.database.schema import Track
                from lyrion.database.sqlite_helper import db_session
                async with db_session() as session:
                    t = (await session.execute(
                        select(Track).where(Track.id == track_id)
                    )).scalar_one_or_none()
                    if t is not None:
                        player.duration = float(t.duration or 0)
            except Exception:
                pass
            player.last_activity = time.time()
        return ok

    async def play_url(self, player_id: str, url: str, title: str = "") -> bool:
        """Play an external stream URL on a player (radio/favorites).

        The player's playlist becomes the single stream URL; no DB track
        involved. Sends a strm frame pointing Squeezelite at the remote
        server (Squeezelite connects there directly).
        """
        player = self.get_player(player_id)
        if player is None:
            logger.warning("play_url: player not found: %s", player_id)
            return False
        handler = self._protocol_handler
        if handler is None:
            logger.warning("play_url: no protocol handler wired")
            return False
        # Set the URL before sending strm: the player may open the HTTP
        # connection before this coroutine gets another scheduling point.
        old_playlist = player.playlist
        old_position = player.playlist_position
        player.playlist = [url]
        player.playlist_position = 0
        player.playlist_total = 1
        ok = await handler.send_remote_stream(player.mac, url, "m")
        if ok:
            player.power = True  # playing implies power-on
            player.current_title = title or url
            player.current_url = url
            player.current_track_id = None
            player.mode = "play"
            player.last_activity = time.time()
            logger.info("play_url %s: %s (%s)", player_id, title or url, url[:60])
        else:
            player.playlist = old_playlist
            player.playlist_position = old_position
        return ok

    async def stop_player(self, player_id: str) -> bool:
        """Stop playback on a player (sends strm 'q')."""
        player = self.get_player(player_id)
        if player is None:
            return False
        handler = self._protocol_handler
        if handler is None:
            return False
        ok = await handler.send_stop_to_player(player.mac)
        if ok:
            player.mode = "stop"
            player.last_activity = time.time()
        return ok

    @staticmethod
    def _current_is_stream(player) -> bool:
        """True if the player is (or was last) playing a live stream URL
        rather than a local track — live streams cannot be paused in
        place (LMS stops them on pause and restarts on resume)."""
        if player.current_track_id is None:
            return True
        pl = getattr(player, "playlist", [])
        pos = getattr(player, "playlist_position", 0)
        return bool(pl and 0 <= pos < len(pl) and isinstance(pl[pos], str))

    async def pause_player(self, player_id: str, pause: bool) -> bool:
        """Pause (or resume) playback on a player.

        Squeezelite (this build, 2.0.0-1584) does NOT honour the strm 'p'
        pause frame (interval 0) — the output keeps playing. So pause =
        STOP (strm 'q'); resume restarts the current item (stream URL or
        track). This matches the LMS behaviour for live streams
        ('Stopping remote stream upon full buffer when paused') and is
        the only reliable pause for this player firmware.
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        handler = self._protocol_handler
        if handler is None:
            return False
        if pause:
            ok = await handler.send_stop_to_player(player.mac)
            if ok:
                player.mode = "pause"
                player.last_activity = time.time()
            return ok
        # resume — restart the current item
        is_stream = self._current_is_stream(player)
        if is_stream:
            url = getattr(player, "current_url", None) or ""
            if not url:
                pl = getattr(player, "playlist", [])
                pos = getattr(player, "playlist_position", 0)
                if pl and 0 <= pos < len(pl) and isinstance(pl[pos], str):
                    url = pl[pos]
            title = getattr(player, "current_title", "") or ""
            ok = await self.play_url(player_id, url, title)
        elif player.current_track_id is not None:
            ok = await self.play_track(player_id, player.current_track_id)
        else:
            ok = False
        if ok:
            player.mode = "play"
            player.last_activity = time.time()
        return ok

    # ------------------------------------------------------------------
    # Playlist management (per-player in-memory track-id list)
    # ------------------------------------------------------------------

    def playlist_add(self, player_id: str, track_id: int) -> bool:
        """Append a track id to the player's playlist."""
        player = self.get_player(player_id)
        if player is None:
            return False
        if track_id not in player.playlist:
            player.playlist.append(track_id)
        player.playlist_total = len(player.playlist)
        player.last_activity = time.time()
        return True

    def playlist_clear(self, player_id: str) -> bool:
        """Clear the player's playlist."""
        player = self.get_player(player_id)
        if player is None:
            return False
        player.playlist.clear()
        player.playlist_position = 0
        player.playlist_total = 0
        player.last_activity = time.time()
        return True

    def playlist_remove(self, player_id: str, index: int) -> bool:
        """Remove a track at a playlist index (0-based)."""
        player = self.get_player(player_id)
        if player is None or index < 0 or index >= len(player.playlist):
            return False
        player.playlist.pop(index)
        player.playlist_total = len(player.playlist)
        if player.playlist_position > index:
            player.playlist_position -= 1
        player.last_activity = time.time()
        return True

    # ------------------------------------------------------------------
    # IR / Display (player-facing commands)
    # ------------------------------------------------------------------

    async def send_ir(self, player_id: str, button_code: int) -> bool:
        """Send an IR/button code to a player (slimproto 'irm' frame).

        Args:
            player_id: Player MAC address.
            button_code: Numeric IR button code (e.g. 0x7689xx = play).

        Returns:
            True if the frame was sent to a connected player.
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        handler = self._protocol_handler
        if handler is None:
            return False
        return await handler.send_ir_to_player(player.mac, button_code)

    async def show_display(
        self, player_id: str, line1: str, line2: str, duration: int = 3
    ) -> bool:
        """Show a two-line text message on a player (slimproto 'grfe' frame).

        Args:
            player_id: Player MAC address.
            line1: First display line.
            line2: Second display line.
            duration: How many seconds to show the message.

        Returns:
            True if the frame was sent to a connected player.
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        handler = self._protocol_handler
        if handler is None:
            return False
        return await handler.send_display_to_player(
            player.mac, line1, line2, duration
        )

    async def playlist_play(self, player_id: str, index: int) -> bool:
        """Play the track at a playlist index (0-based)."""
        player = self.get_player(player_id)
        if player is None:
            return False
        if not player.playlist:
            return False
        if index < 0 or index >= len(player.playlist):
            return False
        player.playlist_position = index
        track_id = player.playlist[index]
        ok = await self.play_track(player_id, track_id)
        if ok:
            player.playlist_position = index
            player.playlist_total = len(player.playlist)
        return ok

    async def seek_to(self, player_id: str, seconds: int) -> bool:
        """Seek within the current stream (SlimProto strm 'a' skip-ahead).

        Sends a skip-ahead command for <seconds> * sample_rate jiffies —
        squeezelite discards the next N decoded samples, which equals a
        forward seek. Backwards seeks restart the current stream first
        (the real LMS re-streams the track too).
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        handler = self._protocol_handler
        if handler is None:
            return False

        async def _send_skip(mac_clean: str, secs: int, rate: int = 44100) -> bool:
            mac = mac_clean.upper().replace(":", "")
            writer = handler._player_writers.get(mac)
            if writer is None or writer.is_closing():
                return False
            samples = int(max(0, secs)) * rate
            payload = b"".join([
                b"strm", b"a",
                struct.pack(">I", samples),      # jiffies to skip
                struct.pack(">I", 0),            # reserved (data offset hi)
                struct.pack(">I", 0),            # reserved
            ])
            frame = struct.pack(">H", len(payload)) + payload
            try:
                writer.write(frame)
                await writer.drain()
                logger.info("Sent strm 'a' (skip-ahead %ds) to %s", secs, mac)
                return True
            except (ConnectionError, OSError, RuntimeError):
                return False

        if seconds <= getattr(player, "elapsed", 0) or seconds > 100000:
            # Backwards / out-of-range: restart the current item instead.
            pos = player.playlist_position or 0
            items = player.playlist or []
            if 0 <= pos < len(items):
                item = items[pos]
                if isinstance(item, int):
                    await self.play_track(player_id, item)
                    return True
                return await self.play_url(player_id, str(item))
            return await _send_skip(player.mac, max(0, seconds))
        return await _send_skip(player.mac, seconds - int(getattr(player, "elapsed", 0) or 0))

    async def playlist_next(self, player_id: str) -> bool:
        """Skip to the next track in the playlist (wraps to start)."""
        player = self.get_player(player_id)
        if player is None or not player.playlist:
            return False
        nxt = (player.playlist_position + 1) % len(player.playlist)
        return await self.playlist_play(player_id, nxt)

    async def playlist_prev(self, player_id: str) -> bool:
        """Go back to the previous track in the playlist (wraps to end)."""
        player = self.get_player(player_id)
        if player is None or not player.playlist:
            return False
        prev = (player.playlist_position - 1) % len(player.playlist)
        return await self.playlist_play(player_id, prev)

    async def save_playlist(self, player_id: str, name: str) -> bool:
        """Persist the player's current playlist to the DB under a name."""
        player = self.get_player(player_id)
        if player is None or not player.playlist:
            return False
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from lyrion.database.schema import Playlist, PlaylistItem, Track
        from lyrion.database.sqlite_helper import db_session

        # Track URLs for the snapshot
        urls: dict[int, str] = {}
        async with db_session() as session:
            tracks = (await session.execute(
                select(Track).where(Track.id.in_(player.playlist))
            )).scalars().all()
            for t in tracks:
                urls[t.id] = t.url or ""

            pl = (await session.execute(
                select(Playlist)
                .options(selectinload(Playlist.items))
                .where(Playlist.playlist == name)
            )).scalar_one_or_none()
            if pl is None:
                pl = Playlist(playlist=name, name=name, pl_type=0,
                              changed=datetime.utcnow())
                session.add(pl)
                await session.flush()
            else:
                pl.changed = datetime.utcnow()
                for item in list(pl.items):
                    await session.delete(item)
                await session.flush()

            for pos, tid in enumerate(player.playlist):
                session.add(PlaylistItem(
                    playlist=pl.id, track=tid, position=pos,
                    url=urls.get(tid, ""),
                ))
            await session.commit()
        logger.info("Saved playlist '%s' (%d tracks) for %s",
                    name, len(player.playlist), player_id)
        return True

    async def load_playlist(self, player_id: str, name: str) -> bool:
        """Load a saved playlist into the player and start playing."""
        player = self.get_player(player_id)
        if player is None:
            return False
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from lyrion.database.schema import Playlist
        from lyrion.database.sqlite_helper import db_session

        async with db_session() as session:
            pl = (await session.execute(
                select(Playlist)
                .options(selectinload(Playlist.items))
                .where(Playlist.playlist == name)
            )).scalar_one_or_none()
            if pl is None:
                return False
            items = sorted(pl.items, key=lambda i: i.position)
            track_ids = [i.track for i in items if i.track is not None]

        if not track_ids:
            return False
        player.playlist = track_ids
        player.playlist_total = len(track_ids)
        player.playlist_position = 0
        logger.info("Loaded playlist '%s' (%d tracks) for %s",
                    name, len(track_ids), player_id)
        return await self.playlist_play(player_id, 0)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_player_count(self) -> int:
        """Return the total number of registered players."""
        return len(self.players)

    def get_connected_count(self) -> int:
        """Return the number of currently connected players."""
        return sum(1 for p in self.players.values() if p.connected)

    def __repr__(self) -> str:
        return f"<PlayerManager {len(self.players)} players>"
