"""Player manager for Pyrion Music Server."""
from __future__ import annotations

import asyncio
import logging
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

# ── Perl's modelName() / vfdmodel() per player class ──────────────────────
# modelName: Client.pm:936 returns nothing by default, each class overrides it
#   SqueezePlay.pm:58 'SqueezePlay', Boom.pm:209 'Squeezebox Boom',
#   Receiver.pm:43 'Squeezebox Receiver', HTTP.pm:70 'Web Client',
#   Disconnected.pm:59 'Dummy Client'.
MODEL_NAMES: dict[str, str] = {
    "squeezeplay": "SqueezePlay",
    "controller": "SqueezePlay",
    "boom": "Squeezebox Boom",
    "softboom": "Squeezebox Boom",
    "receiver": "Squeezebox Receiver",
    "http": "Web Client",
    "web": "Web Client",
    "disconnected": "Dummy Client",
}

# displaytype = $client->display->vfdmodel(), and the display class is chosen
# from the HELO device id in Slim/Networking/Slimproto.pm:1027-1120:
#   squeezebox2/softsqueeze -> Slim::Display::Squeezebox2 -> 'graphic-320x32'
#   boom/softboom           -> Slim::Display::Boom       -> 'graphic-160x32'
#   transporter/softsq3     -> Slim::Display::Transporter-> 'graphic-320x32'
#   receiver                -> Slim::Display::NoDisplay  -> 'none'
#   squeezeplay/controller  -> Slim::Display::NoDisplay  -> 'none'
#   squeezebox (SB1)        -> SqueezeboxG 'graphic-280x16' (bitmapped) or Text
# The values are NoDisplay.pm:59, Boom.pm:165, Squeezebox2.pm:206,
# SqueezeboxG.pm:132, Transporter.pm:197.
DISPLAY_TYPES: dict[str, str] = {
    "squeezebox2": "graphic-320x32",
    "squeezebox3": "graphic-320x32",
    "softsqueeze": "graphic-320x32",
    "transporter": "graphic-320x32",
    "softsqueeze3": "graphic-320x32",
    "boom": "graphic-160x32",
    "softboom": "graphic-160x32",
    "squeezebox": "graphic-280x16",     # SB1 bitmapped (SqueezeboxG)
    "receiver": "none",
    "squeezeplay": "none",
    "controller": "none",
}


def model_name_for(model: str) -> str:
    """Perl ``modelName()`` — empty when the class does not override it."""
    return MODEL_NAMES.get((model or "").lower(), "")


# ── Now-Playing-Displayzeilen (Perl ``Player.pm:488-706``) ─────────────────
# ``currentSongLines`` schreibt bei jedem Zustandswechsel die zwei Zeilen des
# Now-Playing-Screens; die englischen Defaults stehen in LMS' ``strings.txt``.
NOW_PLAYING_TEXT = "Now Playing"   # strings.txt:761 (Token PLAYING) / :703
PAUSED_TEXT = "Paused"             # strings.txt:801
STOPPED_TEXT = "Stopped"           # strings.txt:11107
NOTHING_TEXT = "Nothing"           # strings.txt:469
OUT_OF_TEXT = "of"                 # strings.txt:781


# ── Stream-Bitrate (Perl ``HTTP::parseDirectHeaders``) ─────────────────────
#: ICY-Bitrate je Stream-URL — Perl cached sie in ``Slim::Music::Info``
#: (``setBitrate``/``getBitrate``), liest den Header also einmal je URL.
_stream_bitrate_cache: dict[str, float] = {}
_stream_bitrate_failed: set[str] = set()


def _icy_bitrate_from_headers(headers: dict) -> float:
    """Perl ``HTTP::parseDirectHeaders`` (``HTTP.pm:714-805``)::

        elsif ($header =~ /^(?:icy-br|x-audiocast-bitrate):\\s*(.+)/i) {
            if ($song && !$song->bitrate) {
                $bitrate = $1;
                $bitrate *= 1000 if $bitrate < 8000;
            }
        }

    Returns bits/s; ``0`` when the stream announces none (``HTTP.pm:870-872``
    then falls back to the cached ``Slim::Music::Info::getBitrate``).
    """
    for name in ("icy-br", "x-audiocast-bitrate"):
        value = (headers or {}).get(name)
        if not value:
            continue
        try:
            bitrate = float(str(value).strip())
        except (TypeError, ValueError):
            continue
        if bitrate < 8000:
            bitrate *= 1000                       # HTTP.pm:734
        return bitrate
    return 0.0


async def probe_stream_bitrate(url: str, timeout: float = 5.0) -> float:
    """ICY/audiocast bitrate of ``url`` in bits/s (Perl ``HTTP.pm``).

    Perl reads the header while opening the stream (``parseDirectHeaders``,
    ``HTTP.pm:714-805``); this port never gets the player's stream, so the
    headers are read separately — HEAD first, then a header-only GET, the same
    order :mod:`lyrion.formats.stream_probe` uses for the content type.
    Failures are never fatal: the caller keeps 0 and Perl's own fallback
    (``$track->prettyBitRate``, ``Track.pm:353-363``) answers 0 too.
    """
    if not url:
        return 0.0
    if url in _stream_bitrate_cache:
        return _stream_bitrate_cache[url]
    if url in _stream_bitrate_failed:
        return 0.0
    try:
        import httpx

        headers: dict = {}

        async def _fetch(method: str) -> dict:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=timeout, read=timeout,
                                      write=timeout, pool=timeout),
                follow_redirects=True,
            ) as client:
                if method == "HEAD":
                    resp = await client.head(url)
                    return {k.lower(): v for k, v in resp.headers.items()}
                async with client.stream("GET", url) as resp:
                    return {k.lower(): v for k, v in resp.headers.items()}

        try:
            headers = await _fetch("HEAD")
        except Exception:  # noqa: BLE001 — HEAD nicht unterstützt
            headers = {}
        # The ICY headers ride on the AUDIO response; a server that answers
        # HEAD without them (or rejects HEAD outright) still announces the
        # bitrate on the GET Perl itself performs (HTTP.pm:824
        # ``parseDirectHeaders`` runs on the stream response).
        if not _icy_bitrate_from_headers(headers):
            try:
                get_headers = await _fetch("GET")
                headers = get_headers or headers
            except Exception:  # noqa: BLE001
                pass
        bitrate = _icy_bitrate_from_headers(headers)
        if bitrate:
            _stream_bitrate_cache[url] = bitrate
            logger.info("Stream bitrate for %s: %.0f bps (%s)",
                        url[:60], bitrate,
                        headers.get("icy-br") or headers.get("x-audiocast-bitrate"))
            return bitrate
        logger.debug("Stream %s announces no bitrate (headers: %s)",
                     url[:60], ",".join(sorted(headers))[:200])
    except Exception as exc:  # noqa: BLE001 — Netzfehler nie fatal
        logger.debug("Bitrate-Probe für %s fehlgeschlagen: %s", url[:70], exc)
    _stream_bitrate_failed.add(url)
    return 0.0


def _library_db_path() -> str:
    """Pfad der Bibliotheks-DB (Test-/Dev-Läufe nutzen LYRION_SERVERDATA)."""
    try:
        from lyrion.config import get_config
        return str(get_config().db_path)
    except Exception:  # noqa: BLE001
        return "/root/.lyrion/Lyrion/Prefs/lyrion.db"


def display_type_for(model: str) -> str | None:
    """Perl ``vfdmodel()`` for a HELO device id.

    ``None`` means "stay silent": Perl only omits the field for the ``http``
    model (Queries.pm:2647-2649), and an unknown device id is one we cannot
    name honestly.
    """
    m = (model or "").lower()
    if m in ("http", "web"):
        return None
    return DISPLAY_TYPES.get(m, "none")


def _perl_model_formats(model: str) -> set[str] | None:
    """Perl's static ``formats()`` list per player class.

    Citations (/tmp/lms-ref): SqueezePlay.pm:59 ``ogg flc aif pcm mp3``,
    Squeezebox2.pm:137 (SB2/SB3/Boom/Transporter) ``wma ogg flc aif pcm
    mp3``, Squeezebox1.pm:282 ``aif pcm mp3``, SoftSqueeze.pm:43-52,
    SLIMP3.pm:285-287 ``mp3``, HTTP.pm:68 ``mp3``, Disconnected.pm:57 none.

    These are only the FALLBACK: a modern player declares its codecs as
    capability tokens and Perl then uses exactly those
    (SqueezePlay.pm:170-200 "if we have capabilities then all CODECs must be
    declared that way") — see ``formats_from_capabilities``.
    """
    from lyrion.formats.lms_types import format_extension

    m = (model or "").lower()
    if m.startswith("squeezeplay") or m.startswith("core"):
        perl = ("ogg", "flc", "aif", "pcm", "mp3")
    elif m.startswith("squeezebox1") or m.startswith("squeezeboxclassic"):
        perl = ("aif", "pcm", "mp3")
    elif m.startswith("slimp3") or m.startswith("http") or m == "web":
        perl = ("mp3",)
    elif m.startswith("softsqueeze"):
        perl = ("ogg", "flc", "aif", "pcm", "mp3")
    elif (m.startswith("squeezebox2") or m.startswith("squeezebox3")
          or m.startswith("squeezebox") or m.startswith("boom")
          or m.startswith("transporter") or m.startswith("receiver")):
        perl = ("wma", "ogg", "flc", "aif", "pcm", "mp3")
    else:
        # Unknown / squeezelite-class client WITHOUT capability tokens: use
        # the Squeezebox2 base list (Squeezebox2.pm:137). Anything outside it
        # is transcoded, so this can only cost CPU, never correctness.
        perl = ("wma", "ogg", "flc", "aif", "pcm", "mp3")
    return {format_extension(p) for p in perl}


def formats_from_capabilities(capabilities: str | None,
                              model: str = "") -> set[str]:
    """Codec set a player declares in its HELO capability string.

    Perl SqueezePlay.pm:170-200 (``updateCapabilities``): *"if we have
    capabilities then all CODECs must be declared that way"* — every comma
    separated token matching ``/^[a-z][a-z0-9]{1,4}$/`` is a format, e.g.
    the live SqueezePlay sends
    ``alc,aac,ogg,ogf,flc,aif,pcm,mp3,MaxSampleRate=384000,...``.
    Tokens that map to no known format (``test``, ``tone``, ...) are kept
    as-is so nothing is silently invented.

    Without codec tokens the model's static Perl list is the fallback.
    """
    from lyrion.formats.lms_types import FORMAT_TO_EXTENSION

    codecs: set[str] = set()
    if capabilities:
        for token in capabilities.split(","):
            token = token.strip()
            if not token or len(token) < 2 or len(token) > 5:
                continue
            if not (token[0].islower() and token[0].isalpha()):
                continue
            if not all(c.islower() or c.isdigit() for c in token):
                continue
            codecs.add(FORMAT_TO_EXTENSION.get(token, token))
    if codecs:
        return codecs
    return _perl_model_formats(model) or set(_COMMON_FORMATS)


def _formats_for_model(model: str) -> set[str]:
    """Return the set of audio extensions ``model`` plays natively."""
    return _perl_model_formats(model) or set(_COMMON_FORMATS)


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
        # PROT-18 display wiring (lazily bound to _protocol_handler).
        self._display_wiring = None
        logger.info("PlayerManager initialized")

    def set_protocol_handler(self, handler) -> None:
        """Inject the SlimProto server for sending commands to players."""
        self._protocol_handler = handler
        self._display_wiring = None  # rebind the display wiring to the new handler

    # ------------------------------------------------------------------
    # Display wiring (PROT-18)
    # ------------------------------------------------------------------

    def display_wiring(self):
        """The :class:`~lyrion.player.display.DisplayWiring` for this handler."""
        from .display import DisplayWiring

        handler = self._protocol_handler
        if handler is None:
            return None
        # getattr/setattr: some tests build the singleton with object.__new__.
        wiring = getattr(self, "_display_wiring", None)
        if wiring is None:
            wiring = DisplayWiring(handler)
            self._display_wiring = wiring
        return wiring

    async def notify_now_playing_display(self, player: PlayerState,
                                         kind: str = "showbriefly",
                                         duration: int | None = None) -> None:
        """Perl ``Display::notify`` for the now-playing display (jive block).

        Perl notifies the ``displaystatus`` subscriptions on every playmode
        change: ``playcontrolCommand`` ends with
        ``$client->showBriefly($client->currentSongLines(), …)``
        (``Commands.pm:758-765`` for pause/stop, ``:958-965`` via playlist jump
        for play), and ``Display::showBriefly`` fires
        ``notify('showbriefly', $parts, $duration)`` (``Display.pm:285-288``) →
        ``displaystatusQuery`` publishes ``$parts->{'jive'}`` — the icon block
        of ``Player.pm:651-673`` — on the controller's displaystatus channel.

        ``kind`` is the notification type (``showbriefly`` for a ``showBriefly``
        call, ``update`` for a plain ``$client->update()``, ``Display.pm:214-217``).
        """
        try:
            from lyrion.web.api import jive_now_playing_display
            from lyrion.web.cometd import get_manager

            mgr = get_manager()
            if mgr is None:
                return
            block = await jive_now_playing_display(player)
            await mgr.notify_display(player.mac, kind, block, duration)
        except Exception as exc:  # noqa: BLE001 — Anzeige darf nie stören
            logger.debug("display notify for %s failed: %s", player.mac, exc)

    async def _display_update(self, player: PlayerState) -> list[str]:
        """Perl ``$client->update()`` — ``Player.pm:152`` -> ``Display.pm:141``.

        Zustandswechsel rufen es direkt (Track-Start ``Player.pm:1115-1116``,
        ``:1250-1252``). Der Screen-Text kommt aus :meth:`_now_playing_lines`
        (Perl ``Player.pm:488-706`` ``currentSongLines``), damit die Renderer
        (``player/fonts.py``) echte ``grfe``/``grfd``/``vfdc``-Bits bauen.
        """
        wiring = self.display_wiring()
        if wiring is None:
            return []
        try:
            text = await self._now_playing_lines(player)
            return await wiring.update(player, text=text)
        except Exception as exc:  # noqa: BLE001 — Display darf nie stören
            logger.debug("display update for %s failed: %s", player.mac, exc)
            return []

    # ------------------------------------------------------------------
    # Now-Playing-Text (Perl currentSongLines, Player.pm:488-706)
    # ------------------------------------------------------------------

    async def _now_playing_lines(self, player: PlayerState) -> list[str]:
        """Die zwei Now-Playing-Zeilen — Perl ``currentSongLines``.

        ``Player.pm:509-513`` (leere Playlist) -> ``NOW_PLAYING``/``NOTHING``,
        ``:521-535`` (pause) -> ``PAUSED``, ``:541-553`` (stop) -> ``STOPPED``,
        ``:571-585`` (play) -> ``PLAYING``; bei ``playlistlen > 1`` folgt
        ``" (i OUT_OF n) "`` mit ``playingSongIndex+1`` (``Source.pm:229-233``).
        ``line[1]`` ist der aktuelle Titel (``:633``).

        Der Modus (``playingDisplayModes[playingDisplayMode]``) bestimmt NUR den
        Fortschritts-Overlay (``Player.pm:721-733``, geschrieben wird allein
        ``overlay[0]``, ``:798``) und für den Transporter, ob ``screen2``
        (Album/Interpret) existiert (``Transporter.pm:319-325``; ``Display.pm:
        859`` ``hasScreen2`` = 0 für alle anderen Klassen) — NICHT diese Zeilen.
        """
        playlist = getattr(player, "playlist", None) or []
        total = len(playlist)
        if total < 1:
            # Player.pm:509-513 — NOTHING / NOTHING
            return [NOW_PLAYING_TEXT, NOTHING_TEXT]
        mode = getattr(player, "mode", "stop")
        if mode == "pause":
            line0 = PAUSED_TEXT                      # Player.pm:521-535
        elif mode == "stop":
            line0 = STOPPED_TEXT                     # Player.pm:541-553
        else:
            line0 = NOW_PLAYING_TEXT                 # Player.pm:571-585
        if total > 1:
            index = int(getattr(player, "playlist_position", 0) or 0) + 1
            line0 += f" ({index} {OUT_OF_TEXT} {total}) "   # :531-534/:580-583
        return [line0, await self._current_song_title(player)]

    @staticmethod
    def _playing_track_id(player: PlayerState) -> Optional[int]:
        """Track-ID des laufenden Songs (Perl ``Playlist::track($client)``)."""
        track_id = getattr(player, "current_track_id", None)
        if isinstance(track_id, int):
            return track_id
        playlist = getattr(player, "playlist", None) or []
        position = getattr(player, "playlist_position", 0) or 0
        if 0 <= position < len(playlist) and isinstance(playlist[position], int):
            return int(playlist[position])
        return None

    async def _current_song_title(self, player: PlayerState) -> str:
        """Perl ``Slim::Music::Info::getCurrentTitle`` (``Music/Info.pm:556-583``).

        Der Default-``titleFormat`` ist ``'TITLE'`` (``Utils/Prefs.pm:249-250``,
        ``Player/Client.pm:52-53`` → ``titleFormat[0]``), also der Track-Titel.
        Für Radio-Streams setzt der Port den Stations-/ICY-Titel in
        ``player.current_title`` (``ProtocolHandlers``-Override, Info.pm:562-570).
        """
        track_id = self._playing_track_id(player)
        if track_id is not None:
            title = await self._track_title(track_id)
            if title:
                return title
        return str(getattr(player, "current_title", "") or "")

    async def _track_title(self, track_id: int) -> str:
        """Titel eines Bibliothekstracks (lesend, blockiert den Loop nicht)."""
        def _run() -> str:
            import sqlite3

            con = sqlite3.connect(
                f"file:{_library_db_path()}?mode=ro", uri=True, timeout=30)
            try:
                row = con.execute(
                    "SELECT title FROM tracks WHERE id = ?", (track_id,)
                ).fetchone()
                return str(row[0] or "") if row else ""
            finally:
                con.close()

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001 — kein Titelfehler nach außen
            logger.debug("display title lookup for %s failed: %s", track_id, exc)
            return ""

    async def _display_power(self, player: PlayerState, on: bool) -> list[str]:
        """Perl ``Player::power`` — ``Player.pm:255-290`` (Helligkeit + Update)."""
        wiring = self.display_wiring()
        if wiring is None:
            return []
        try:
            return await wiring.power(player, on)
        except Exception as exc:  # noqa: BLE001
            logger.debug("display power for %s failed: %s", player.mac, exc)
            return []

    async def display_on_connect(self, mac: str) -> list[str]:
        """Perl-Connect-Displaypfad — ``Player.pm:114-124`` + ``Squeezebox.pm:124-134``.

        Setzt die Helligkeit (``powerOn/OffBrightness``) und erzwingt den
        Visualizer (``Squeezebox.pm:134``). NoDisplay bricht vorher ab
        (``Player.pm:114``).
        """
        player = self.get_player(mac)
        if player is None:
            return []
        wiring = self.display_wiring()
        if wiring is None:
            return []
        try:
            return await wiring.on_connect(player)
        except Exception as exc:  # noqa: BLE001
            logger.debug("display on connect for %s failed: %s", mac, exc)
            return []

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
        uuid: str = "",
        model_name: str = "",
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
            uuid: The player's own UUID as sent in its HELO frame. Perl keeps
                it on the client and reports it in the players loop
                (Queries.pm:2627) — not the MAC address.
            model_name: The client's self-declared model name from the HELO
                caps (ModelName=, SqueezePlay.pm:82) — e.g. "SB Player",
                "SqueezeLite". Reported as 'modelname' in the players loop.

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
            if uuid:
                player.uuid = uuid
            if model_name:
                player.model_name = model_name
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
                uuid=uuid,
                model_name=model_name,
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
        # Perl switches the player's audio outputs together with power:
        # Player.pm:253 ``$client->audio_outputs_enable(0)`` on power-off and
        # Player.pm:268 ``audio_outputs_enable(1)`` on power-on (the frame is
        # 'aude' with pack('CC', $enabled, $enabled), Squeezebox2.pm:900-906).
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.send_audio_outputs(mac, on))
        except RuntimeError:
            pass  # no running loop — state-only fallback
        # PROT-18: Perl's power path also drives the display
        # (Player.pm:255-259 power-off brightness, :277-287 power-on).
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._display_power(player, on))
        except RuntimeError:
            pass  # no running loop — state-only fallback
        if not on:
            # Power off = stop playback (SlimProto strm 'q') + standby. The
            # stop-frame send is async; schedule it on the running loop (all
            # callers are async: JSON-RPC/CLI/alarm wake).
            player.mode = "stop"
            player.pause_requested = False  # real stop supersedes a pause
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.stop_player(mac))
            except RuntimeError:
                pass  # no running loop — state-only fallback
        logger.debug("Player %s power: %s", mac, "on" if on else "off")

    async def power_on_for_playback(self, player: PlayerState) -> None:
        """Power a player on because playback starts (Perl powers it on first).

        Perl's power-on path switches the audio outputs on as well
        (``Player.pm:268`` → ``audio_outputs_enable(1)`` → 'aude' with
        ``pack('CC', 1, 1)``, Squeezebox2.pm:900-906). Sending it only on an
        actual transition keeps a plain play on a powered player silent.
        """
        if player.power:
            return
        player.power = True
        await self.send_audio_outputs(player.mac, True)
        # PROT-18: Perl's power-on also re-initialises the display
        # (Player.pm:265-290: update + brightness(powerOnBrightness) >= 1).
        await self._display_power(player, True)

    async def send_audio_outputs(self, mac: str, enabled: bool) -> bool:
        """Perl ``audio_outputs_enable`` — 'aude' frame (spdif + dac).

        Perl: Squeezebox2.pm:900-906, called from Player.pm:253 (power off)
        and Player.pm:268 (power on).
        """
        handler = self._protocol_handler
        send = getattr(handler, "send_aude", None) if handler is not None else None
        if send is None:
            return False
        try:
            return bool(await send(mac, enabled))
        except Exception as exc:  # noqa: BLE001
            logger.debug("send_audio_outputs(%s, %s) failed: %s", mac, enabled, exc)
            return False

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
        # A fresh play supersedes any pending pause state.
        player.pause_requested = False
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
            # Playing implies power-on (Perl enables the audio outputs then).
            await self.power_on_for_playback(player)
            player.mode = "play"
            player.current_track_id = track_id
            player.remote = 0  # local track: never a "live stream" flag
            player.elapsed = 0.0
            # A local track must not inherit the radio's StreamTitle/meta
            # (else now-playing shows the old station name over the track).
            player.current_title = ""
            player.remote_meta = {}
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
            # PROT-18: a new track is a screen change — Perl re-renders the
            # display when buffering ends / the track starts
            # (Player.pm:1115-1116, :1250-1252).
            await self._display_update(player)
            # A track change notifies the displaystatus subscribers with the
            # jive icon block (Commands.pm:758-765 / :958-965 -> Display.pm:287).
            await self.notify_now_playing_display(player, "showbriefly")
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
        # A fresh play supersedes any pending pause state.
        player.pause_requested = False
        # Set the URL before sending strm: the player may open the HTTP
        # connection before this coroutine gets another scheduling point.
        old_playlist = player.playlist
        old_position = player.playlist_position
        player.playlist = [url]
        player.playlist_position = 0
        player.playlist_total = 1
        # Determine the source codec the way Perl does: read the stream's
        # HTTP headers and map the content type (Slim/Utils/Scanner/Remote.pm:
        # 333-390). The URL suffix alone lies — an AAC station without ".aac"
        # in its URL was announced as 'm', so the client decoded AAC with the
        # MP3 decoder ("AAC radio stream geht nicht", live 2026-09-12). The
        # suffix guess stays as the fallback when the probe fails.
        from lyrion.networking.protocol import SlimProtoClient
        from lyrion.formats.stream_probe import codec_for_stream_url

        suffix_codec = SlimProtoClient._guess_codec_from_url(url)
        # Perl reads the stream's headers once (Scanner/Remote content type,
        # HTTP.pm ICY bitrate); both probes run together so a stream start
        # pays for one round-trip, not two.
        codec, stream_bitrate = await asyncio.gather(
            codec_for_stream_url(url, fallback=suffix_codec),
            probe_stream_bitrate(url),
        )
        if codec != suffix_codec:
            logger.info("Stream codec from headers: %s (suffix said '%s')",
                        codec, suffix_codec)
        ok = await handler.send_remote_stream(player.mac, url, codec)
        if ok:
            logger.info("play_url codec guess: %s -> '%s'", url[:60], codec)
        if ok:
            # Playing implies power-on (Perl enables the audio outputs then).
            await self.power_on_for_playback(player)
            player.current_title = title or url
            player.current_url = url
            player.current_track_id = None
            player.remote = 1  # radio stream: never "track end"
            player.stream_bitrate = float(stream_bitrate or 0)
            player.mode = "play"
            player.last_activity = time.time()
            # PROT-18: screen change on a new stream (Player.pm:1115-1116).
            await self._display_update(player)
            # Perl's play path ends with
            # `$client->showBriefly($client->currentSongLines(), {duration => 2})`
            # (playcontrolCommand Commands.pm:758-765 → playlist jump :958-965);
            # that fires the displaynotify carrying the jive icon block
            # (Display.pm:285-288 → Player.pm:651-673).
            await self.notify_now_playing_display(player, "showbriefly")
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
        # An explicit stop ends any pending pause.
        player.pause_requested = False
        ok = await handler.send_stop_to_player(player.mac)
        # The guard is dropped even when the command could NOT be delivered
        # (writer gone): the local "we streamed track X" belief is not
        # authoritative in that case, and a stale guard would make the play
        # after the next reconnect a silent no-op (R0.5-P1, g4). Re-streaming
        # an unchanged track is harmless, staying silent is not.
        player.forget_stream()
        if ok:
            player.mode = "stop"
            player.last_activity = time.time()
            # PROT-18: playmode change -> the always-on visualizer is hidden
            # (Squeezebox2.pm:252-257 showVisualizer, :301-305 -> visu [0]).
            await self._display_update(player)
            # playcontrolCommand's stop branch shows the status (Commands.pm:758-765).
            await self.notify_now_playing_display(player, "showbriefly")
        return ok

    async def pause_player(self, player_id: str, pause: bool) -> bool:
        """Pause (True) / resume (False) playback — NOT a stop+restart.

        Perl parity (read from the pinned clone, public/9.2):
          * pause  = ``strm 'p'``: ``sub pause { $client->stream('p');
            $client->playPoint(undef); $client->SUPER::pause(); }``
            — Slim/Player/Squeezebox.pm:197-204. The player holds its
            output buffers; nothing is flushed, the stream socket stays
            open, so the position is preserved.
          * resume = ``strm 'u'``: ``sub resume { $client->stream('u', ...);
            $client->SUPER::resume(); }`` — Slim/Player/Squeezebox2.pm:1104-1110.
            The output continues; the file is NOT re-streamed.
          * stop   = ``strm 'q'`` — Squeezebox.pm:206-216 (the OTHER cmd).
          * replay-gain field: ``'p'`` carries ``int(interval*1000)`` (ms),
            ``'u'`` the interval directly — Squeezebox.pm:1080-1094.

        Position handling: on pause the position is frozen in the state
        (``PlayerState.pause_time``, Perl's ``resumeTime`` —
        StreamingController.pm:64/1555/1719-1724) and the strm idempotency
        guard is deliberately NOT reset: the player still holds the stream,
        so a resume must not trigger a second ``/stream.mp3`` GET.
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        handler = self._protocol_handler
        if handler is None:
            return False
        if pause:
            # Freeze the position in the state machine (Perl _Pause:
            # resumeTime = playingSongElapsed, StreamingController.pm:1555).
            player.pause_time = float(getattr(player, "elapsed", 0) or 0)
            ok = await handler.send_pause_to_player(player.mac)
            if ok:
                player.mode = "pause"
                # Kept so a stray STAT stop-ack cannot flip mode to "stop".
                player.pause_requested = True
                player.last_activity = time.time()
                # PROT-18: playmode change (Squeezebox2.pm:252-257/:301-305).
                await self._display_update(player)
                # Pause notifies the display (Commands.pm:758-765).
                await self.notify_now_playing_display(player, "showbriefly")
            return ok
        # resume — continue the paused output in place, never re-stream
        ok = await handler.send_unpause_to_player(player.mac)
        if ok:
            player.mode = "play"
            player.pause_requested = False
            # The displayed position continues at the pause point
            # (Perl _JumpOrResume jumps to resumeTime, StreamingController.pm:1605-1614).
            player.elapsed = float(getattr(player, "pause_time", 0.0) or 0.0)
            player.last_activity = time.time()
            # PROT-18: playmode change -> the always-on visualizer returns
            # (Squeezebox2.pm:252-257).
            await self._display_update(player)
            # Resume notifies the display (Commands.pm:758-765).
            await self.notify_now_playing_display(player, "showbriefly")
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
        self, player_id: str, line1: str, line2: str, duration: int = 1
    ) -> bool:
        """Show a two-line message on a player's display.

        Perl: the ``display`` command (``Commands.pm:444-472``) wakes the
        screensaver and calls ``$client->showBriefly({line => [$line1, $line2]},
        $duration, $p4)`` — ``Display.pm:221-327`` renders the screen (:298) and
        restores the old one after ``duration`` seconds (``endShowBriefly`` via
        timer, :325). ``duration`` defaults to 1 s (``Display.pm:258``), the same
        value as the ``displaytexttimeout`` pref (``Utils/Prefs.pm:169``).

        PROT-18: the rendered payload of a graphics display is a Bitmap
        (``grfe``, Squeezebox2.pm:243-248) and of a Text display a TextVFD
        stream (``vfdc``, Text.pm:437-441). ``line1``/``line2`` sind Perls
        ``line[0]``/``line[1]`` aus ``showBriefly`` (``Display.pm:221-327``) und
        werden dem Renderer (:mod:`lyrion.player.fonts`) übergeben.

        Returns:
            True if at least one display frame was sent.
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        wiring = self.display_wiring()
        if wiring is None:
            return False
        try:
            # No ``sleep``: the wait for Perl's endShowBriefly timer must not
            # block the CLI/JSON request that called us.
            sent = await wiring.show_briefly(
                player, text=[line1, line2], duration=duration)
        except Exception as exc:  # noqa: BLE001 — Display darf nie stören
            logger.debug("show_display for %s failed: %s", player_id, exc)
            return False
        if not sent:
            logger.info(
                "show_display %s: %r/%r — kein Frame (NoDisplay oder "
                "nicht renderbarer Text)",
                player_id, line1, line2,
            )
        return bool(sent)

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
        """Seek within the current stream.

        A forward seek is a SlimProto ``strm 'a'`` skip-ahead (the replay-gain
        field carries the interval in milliseconds). A backwards or
        out-of-range seek restarts the current item instead (the real LMS
        re-streams the track too).
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        handler = self._protocol_handler
        if handler is None:
            return False

        if seconds <= getattr(player, "elapsed", 0) or seconds > 100000:
            # Backwards / out-of-range: restart the current item instead.
            pos = player.playlist_position or 0
            items = player.playlist or []
            if 0 <= pos < len(items):
                item = items[pos]
                if isinstance(item, int):
                    return await self.play_track(player_id, item)
                return await self.play_url(player_id, str(item))
            return False
        return await handler.send_skip_to_player(
            player.mac, seconds - int(getattr(player, "elapsed", 0) or 0)
        )

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
