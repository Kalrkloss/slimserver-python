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


def jump_target(player: PlayerState, index) -> int | None:
    """``playlistJumpCommand`` target index — ``Slim/Control/Commands.pm:937-1014``.

    Perl's handler serves BOTH ``playlist jump`` and ``playlist index``
    (``Commands.pm:925``) and accepts an absolute index as well as a relative
    ``+n``/``-n`` offset (``Commands.pm:970``):

    * no playlist at all -> the command returns immediately (``:937``)
    * playing and ``+0`` — or playing with a single-song playlist and ``-1`` —
      restarts the CURRENT track (``:974-979``, ``jumpToTime(0)``); the index
      does not move
    * playing and ``+1`` skips to the next song (``:980-986``): the controller's
      ``nextsong`` wraps at the end and REPLAYS the current song while
      ``playlist repeat == 1`` (``StreamingController.pm:848-899``)
    * everything else is ``playingSongIndex + offset`` / the absolute index,
      wrapped into the playlist (``:990``, ``:1010-1013``)

    Returns ``None`` when there is nothing to jump to (empty playlist, or a
    non-numeric absolute index).
    """
    playlist = list(getattr(player, "playlist", None) or [])
    count = len(playlist)
    if not count:
        return None                                     # :937 (|| return)
    text = str(index)
    cur = int(getattr(player, "playlist_position", 0) or 0)
    if cur < 0 or cur >= count:
        cur = 0
    if "+" in text or "-" in text:                      # :970 relative
        stopped = getattr(player, "mode", "stop") == "stop"   # StreamCtrl:1681
        if not stopped:
            if text == "+0" or (count == 1 and text == "-1"):
                return cur                              # :974-979 restart
            if text == "+1":                            # :980-986 skip()
                if int(getattr(player, "repeat", 0) or 0) == 1:
                    return cur             # StreamingController.pm:876-878
                return (cur + 1) % count
        return (cur + int(text)) % count                # :990, :1010-1013
    stripped = text.lstrip("+")
    if not stripped.isdigit():
        return None
    return int(stripped) % count                        # :1005, :1010-1013


def nextsong(player: PlayerState, currsong: int | None = None) -> int | None:
    """Perl ``nextsong`` — the index the AUTO-ADVANCE plays next.

    ``Slim/Player/StreamingController.pm:847-899``; ``_getNextTrack`` asks for
    it whenever no explicit index was given (:655-660):

    * ``repeat == 1`` (repeat-song) → the CURRENT index (:871-873);
    * otherwise ``currsong + 1``, wrapped to 0 at the end of the playlist
      (:881-893) — with a reshuffle when shuffle is on, ``repeat == 2`` and
      the ``reshuffleOnRepeat`` pref is set (:885-891);
    * ``undef`` when the playlist wrapped and repeat is off (:897) — that is
      what ENDS the playlist: ``_getNextTrack(..., $ifMoreTracks=1)`` returns
      without streaming (:662-664), so the controller stays stopped
      (``Stopped`` :224-230) and the status mode becomes ``stop``.

    ``None`` is therefore NOT "nothing to do" for a caller that wants the
    track to repeat — it is Perl's explicit end-of-playlist answer.

    The shuffle ORDER is Perl's per-player ``shufflelist``
    (``Playlist.pm:172-178``; ``reshuffle`` :788-…) — not ported, so a
    shuffled playlist advances in playlist order here (same limitation as
    :func:`jump_target`). ``consecutiveErrors`` (:862-879) belongs to the
    song-queue error path and is not part of this port.
    """
    playlist = list(getattr(player, "playlist", None) or [])
    count = len(playlist)
    if not count:
        return None                                     # :858
    if currsong is None:
        currsong = int(getattr(player, "playlist_position", 0) or 0)
    if currsong < 0 or currsong >= count:
        currsong = 0
    repeat = int(getattr(player, "repeat", 0) or 0)
    if repeat == 1:
        return currsong                                 # :871-873
    nxt = currsong + 1
    if nxt >= count:
        # :883-893 — start over at the end of the playlist. The reshuffle of
        # a shuffle+repeat-all playlist (:885-891) needs Perl's shufflelist.
        nxt = 0
    if not repeat and nxt == 0:
        return None                                     # :897
    return nxt


#: Perl ``$defaultPrefs`` ``powerOnResume`` (``Slim/Player/Player.pm:66``) —
#: the "Power On Resume" player setting. Its TWO halves are read with a regex
#: each: ``(.*)Off`` for the power-OFF behaviour (Player.pm:210) and
#: ``-(.*)On`` for the power-ON/(re)connect behaviour (Player.pm:305). The six
#: values of the setting page (``Slim/Web/Settings/Player/Audio.pm:33``) are
#: PauseOff-NoneOn, PauseOff-PlayOn (default), StopOff-PlayOn, StopOff-NoneOn,
#: StopOff-ResetPlayOn, StopOff-ResetOn.
POWER_ON_RESUME_DEFAULT = "PauseOff-PlayOn"


def power_on_resume_pref(player: PlayerState) -> str:
    """The player's ``powerOnResume`` value, Perl's default when unset.

    ``Slim/Player/Player.pm:66`` (``'powerOnResume' => 'PauseOff-PlayOn'``);
    ``playerpref powerOnResume <value>`` stores it in ``player.playerprefs``
    (``Slim/Control/Commands.pm:2631-2677``).
    """
    prefs = getattr(player, "playerprefs", None) or {}
    value = str(prefs.get("powerOnResume") or "").strip()
    return value or POWER_ON_RESUME_DEFAULT


def _hashable(value):
    """A hashable, comparable view of a status field value.

    ``PlayerState`` keeps a few dicts/lists (``playlist``,
    ``remote_meta``, ``playerprefs``, ``sync_slaves``); they must take part
    in the status signature without making it unhashable.
    """
    if isinstance(value, dict):
        return tuple(sorted((str(k), _hashable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(_hashable(v) for v in value)
    return value


def status_signature(player: PlayerState) -> tuple:
    """The player state a pushed ``status`` is built from, minus the clock.

    Every field here is one the status response reports (``Queries.pm:3996+``
    ``statusQuery``).  Deliberately excluded are the fields the ~1/s STAT tick
    moves without changing what the client displays — ``elapsed``,
    ``signal_strength``, ``_stat``, ``_last_stmd*``, ``_track_started_at``,
    ``last_activity``, ``strm_sent_*``, ``playing_track_id``: Perl's STAT
    heartbeat is not an event (``Squeezebox2.pm:174-176`` ->
    ``playerStatusHeartbeat`` -> ``_NoOp``/``_CheckSync``,
    ``StreamingController.pm:238-244``) and must not produce a push.
    """
    return (
        player.mode,
        player.power,
        player.volume,
        player.mute,
        player.name,
        player.connected,
        player.current_track_id,
        player.current_title,
        player.current_url,
        _hashable(player.playlist),
        player.playlist_position,
        player.playlist_total,
        player.playlist_timestamp,
        player.playlist_mode,
        player.shuffle,
        player.repeat,
        player.randomplay,
        player.remote,
        player.sleep_remaining,
        player.sync_master,
        _hashable(player.sync_slaves),
        player.use_volume_control,
        player.digital_volume_control,
        player.stream_bitrate,
        _hashable(player.remote_meta),
        _hashable(player.playerprefs),
    )


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
        # Last pushed status per player (the STAT heartbeat gate, see
        # ``status_changed``): mac -> status_signature().
        self._status_signatures: dict[str, tuple] = {}
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

    # ------------------------------------------------------------------
    # Status-change gate (Perl's notification rule for a `status` sub)
    # ------------------------------------------------------------------

    def status_changed(self, player: PlayerState) -> bool:
        """True when the player's status differs from the last pushed one.

        Perl re-executes a ``status`` subscription only when the server
        *notifies* a change — never once per STAT tick:

        * ``Slim/Player/Squeezebox2.pm:138-178`` ``statHandler`` has a branch
          for STMc/d/n/l/u/a/s/o and EoS only; **every other STAT code
          (STMf, STMp, STMr, STMt, STMz …) is dispatched as
          ``$client->controller->playerStatusHeartbeat($client)``
          (:174-176)** — a heartbeat, not an event.
        * ``Slim/Player/StreamingController.pm:238-244`` maps
          ``StatusHeartbeat`` to ``_NoOp`` (STOPPED), ``_CheckSync``
          (PLAYING — :485-490 returns at once unless more than one player
          shares a sync group) and ``_CheckPaused`` (:424-452 acts only when
          a remote stream's buffer is full).  None of them notifies.
        * The subscription itself is registered as an auto-execute whose
          filter decides relevance (``Queries.pm:4588-4597`` +
          ``Request.pm:2065-2102``); a notification that does not concern the
          player is filtered out (``statusQuery_filter``,
          ``Queries.pm:3925-3993``).

        Our port's only server-side clock is the STAT frame the player sends
        ~1/s, so the faithful equivalent is: compare the status the client
        would be sent and stay silent while it is unchanged.  The volatile
        STAT fields (``elapsed``/``jiffies``/``bytes_received``/
        ``buffer_fullness``/``output_buffer_fullness``/``signal_strength``/
        ``_stat``) are deliberately NOT part of the signature: they move on
        every tick while the displayed status does not — exactly the tick
        Perl's heartbeat never turns into a notification.

        Returns True (and records the new signature) when a push is due.
        """
        signature = status_signature(player)
        key = player.mac
        # getattr: some tests build the singleton with object.__new__.
        store = getattr(self, "_status_signatures", None)
        if store is None:
            store = self._status_signatures = {}
        if store.get(key) == signature:
            return False
        store[key] = signature
        return True

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
        # Perl's client lifecycle verb for this HELO: a MAC Perl does not know
        # yet creates a client (Client.pm:313-315 notifies 'client new'), a MAC
        # it already has takes the reconnect branch (Slimproto.pm:1203-1213
        # "$client->disconnected(0); notifyFromArray($client,
        # ['client','reconnect'])"). Both are fired below.
        lifecycle = "new"

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
            lifecycle = "reconnect"
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

        # Perl's client lifecycle notification, fired where Perl fires it:
        #   * a new client:       Client.pm:313-315  ['client', 'new']
        #   * a known client's HELO: Slimproto.pm:1211-1213 ['client','reconnect']
        # Subscribers are Perl's CLI listeners (``listen 1``, Plugin/CLI/
        # Plugin.pm:969-1017 — a controller only learns about a player that
        # appeared from this line) and the controller auto-execute
        # (Request.pm:2055-2102). Without it the client list never changed for
        # any listener, even though ``players``/``serverstatus`` were right.
        try:
            from lyrion.control.notifications import notify_from_array

            notify_from_array(mac, ["client", lifecycle])
        except Exception as exc:  # noqa: BLE001 — a notification must not
            logger.debug("client %s notification failed for %s: %s",  # kill a HELO
                         lifecycle, mac, exc)

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
            # Perl records whether the player was REALLY playing before it
            # stops/pauses it (``isPlaying(1)`` = playingState PLAYING, i.e.
            # not paused/buffering): ``$prefs->client($client)->
            # set('playingAtPowerOff', $playing)`` — Player.pm:230-231. The
            # flag is what ``resumeOnPower`` later consumes to decide whether
            # a power-on may start playing again (Player.pm:312-329).
            player.playing_at_power_off = player.mode == "play"
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
        else:
            # Perl: power-on ends with ``$client->resumeOnPower() unless
            # $noplay`` (Player.pm:296-297) — a player that was playing when
            # it was switched off starts again (Player.pm:301-332). Nothing to
            # resume on a first-ever power-on (``playingAtPowerOff`` unset).
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.resume_on_power(mac))
            except RuntimeError:
                pass  # no running loop — state-only fallback
        logger.debug("Player %s power: %s", mac, "on" if on else "off")

    async def persist_playback_state_for_power_off(self, mac: str) -> None:
        """Perl ``persistPlaybackStateForPowerOff`` — ``Slimproto.pm:305-321``.

        Called on every close of a player's live slimproto socket
        (``slimproto_close`` → ``persistPlaybackStateForPowerOff($client)``,
        ``Slim/Networking/Slimproto.pm:283-286``): "treat a vanishing player
        that's still playing the same way as a player that's turned off while
        still playing, so we can make it start playing again if it reappears
        and the user told us to resume playing when powering on" (:309-312).

        Two client prefs are written, exactly like Perl:
          * ``playingAtPowerOff = $client->isPlaying(1)`` (:313) — our
            ``mode == "play"`` (``isPlaying(1)`` is ``playingState ==
            PLAYING``, ``StreamingController.pm:1676-1678``: a PAUSED player
            is NOT playing, so it must not resume);
          * ``positionAtDisconnect``, but ONLY while playing (:315-318):
            ``$client->playingSong()->canSeek() ? playingSongElapsed() : 0``.
            Our remote (radio) streams cannot seek → 0, like Perl.
        """
        player = self.get_player(mac)
        if player is None:
            return
        playing = player.mode == "play"                      # :313
        player.playing_at_power_off = playing
        if playing:                                          # :315-318
            player.position_at_disconnect = (
                0.0 if getattr(player, "remote", 0)
                else float(getattr(player, "elapsed", 0.0) or 0.0)
            )
        logger.debug(
            "persisted playback state for %s: playingAtPowerOff=%s "
            "positionAtDisconnect=%.3f",
            mac, playing, player.position_at_disconnect,
        )

    @staticmethod
    def _playing_index(player: PlayerState) -> int:
        """Perl ``Slim::Player::Source::playingSongIndex($client)``.

        The index of the song the player is on — Player.pm:317 reads it for
        the resume jump. Our ``playlist_position`` tracks it; if it points
        past the playlist we fall back to the current track, then to 0.
        """
        items = list(getattr(player, "playlist", None) or [])
        if not items:
            return 0
        index = int(getattr(player, "playlist_position", 0) or 0)
        if 0 <= index < len(items):
            return index
        track_id = getattr(player, "current_track_id", None)
        if track_id is not None and track_id in items:
            return items.index(track_id)
        return 0

    async def resume_on_power(self, mac: str, connect: bool = False,
                              was_online: bool = False) -> bool:
        """Perl ``Slim::Player::Player::resumeOnPower`` — ``Player.pm:301-332``.

        Called from the two places Perl calls it:

        * the HELO handler of a (re)connecting client
          (``Squeezebox.pm:89`` ``$client->resumeOnPower(1)`` for a client
          that comes back after being gone, ``:96`` ``resumeOnPower()`` for a
          reconnect with an active data connection),
        * the power-on path (``Player.pm:297``).

        Perl's logic:

            if (!$client->controller->isPlaying()) {                    :304
                my ($resumeOn) = ... =~ /-(.*)On/;                      :305
                if ($resumeOn =~ /Reset/) {                             :307
                    $client->execute(["playlist","jump", 0, 1, 1]);     :309
                }
                if ($resumeOn =~ /Play/ && track($client)               :312
                        && playingAtPowerOff) {                         :313
                    if ($connect) {                                     :316
                        my $index    = playingSongIndex($client);       :317
                        my $position = positionAtDisconnect;            :318
                        $client->execute(["playlist","jump", $index, 1, 0,
                                          { timeOffset => $position }]); :319
                    } else {
                        $client->execute(["play"]);  # resume if paused  :321
                    }
                    $prefs->client($client)->set('playingAtPowerOff', 0); :329
                }
            }

        ``was_online`` is our reading of ``$controller->isPlaying()``: a client
        that was ALREADY connected keeps its stream (its old socket was closed
        with the ``reconnect`` flag only, ``Slimproto.pm:1193-1195``), so a
        second HELO of a live player must not start a second stream.
        """
        player = self.get_player(mac)
        if player is None:
            return False
        # Player.pm:304 — nothing to do while the player is really playing.
        if player.mode == "play" and was_online:
            return False
        # Squeezebox.pm:85/:95 — the whole resume is gated on the power pref:
        # a player that is switched OFF must stay silent when it reappears.
        if not player.power:
            return False
        # Squeezebox.pm:86-90 — "Don't try to resume if we are synced, we
        # might confuse others who have moved on. I think playerActive is not
        # need should Sync::restoreSync be removed from Client::startup." The
        # check sits in the HELO path only (that is ``connect`` here); the
        # power-on path (Player.pm:297) has no such gate.
        if connect and (player.sync_master is not None or player.sync_slaves):
            return False

        resume_on = power_on_resume_pref(player).split("-")[-1]   # :305
        if "Reset" in resume_on:
            # :307-310 — "reset playlist to start, but don't start the
            # playback yet" (``playlist jump 0 1 1``: noplay=1).
            if player.playlist:
                player.playlist_position = 0
                player.current_track_id = None
        if "Play" not in resume_on:
            return False
        if not player.playlist:                                   # :312 track()
            return False
        if not player.playing_at_power_off:                        # :313
            return False

        index = self._playing_index(player)                        # :317
        if connect:
            # :316-319 — no stream exists yet (restarted player), so the
            # track is streamed again and started at the saved position:
            # ``playlist jump $index 1 0 { timeOffset => $position }``.
            # The offset travels as the source byte range of the strm frame
            # (Perl: ``$song->startOffset`` → File.pm:196-223 / HTTP.pm:963-971)
            # and as the song clock base (HTTP.pm:976-979).
            position = float(getattr(player, "position_at_disconnect", 0.0) or 0.0)
            ok = await self.playlist_play(mac, index, start_seconds=position)
        elif player.mode == "pause":
            # :321 — ``play`` on a paused player is a RESUME (playcontrolCommand
            # maps pause+play to 'resume', Commands.pm:747-748).
            ok = await self.pause_player(mac, False)
        else:
            # :321 — ``play`` from stop: playcontrolCommand goes through
            # ``playlist jump <playingSongIndex>`` (Commands.pm:756-763).
            ok = await self.playlist_play(mac, index)

        # :324-329 — the persisted flag is consumed. Leaving it set would let
        # a later reconnect resume playback a second time.
        player.playing_at_power_off = False
        logger.info("resumed playback for %s (index=%s connect=%s)",
                    mac, index, connect)
        return bool(ok)

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

    async def play_track(self, player_id: str, track_id: int,
                         start_seconds: float = 0.0) -> bool:
        """Start playback of a track on a player (sends strm frame).

        ``start_seconds`` (Perl ``timeOffset``, Player.pm:319) starts the
        stream at that position — used when a player resumes after being gone
        (``resumeOnPower``); 0 for a normal play.
        """
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

        ok = await handler.send_strm_to_player(player.mac, track_id,
                                               start_seconds)
        if ok:
            # Playing implies power-on (Perl enables the audio outputs then).
            await self.power_on_for_playback(player)
            player.mode = "play"
            player.current_track_id = track_id
            player.remote = 0  # local track: never a "live stream" flag
            # Perl counts the song clock from the offset the stream starts at
            # (`$song->startOffset($seekdata->{timeOffset})`, File.pm:196-223);
            # the protocol layer adds the SAME value to every STAT elapsed.
            player.elapsed = float(start_seconds or 0.0)
            # A local track must not inherit the radio's StreamTitle/meta
            # (else now-playing shows the old station name over the track):
            # Perl clears the client's metadata title when the new song opens
            # (``Song.pm:700-702``) — here together with the stream epoch, so a
            # late STMu of the radio stream cannot attach to the local track.
            player.forget_metadata()
            player.current_title = ""
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
        # Determine the source codec the way Perl does: ONE GET scan of the
        # URL maps the response content type (Slim/Utils/Scanner/Remote.pm:
        # 205-233 request, :333-390 type rules) — and the scan's OUTCOME
        # decides whether there is anything to play at all. A failed scan
        # (connection error, or a status outside 2xx/3xx like 1.FM's
        # "503 Service Unavailable") makes Async::HTTP call onError
        # (Slim/Networking/Async/HTTP.pm:434-435) → Remote.pm:228-238 →
        # Song.pm:302-312 notifies `playlist cant_open` and fails the play.
        # Perl sends NO strm in that case and has no proxy fallback — the old
        # port ignored the 503, kept the URL-suffix guess 'm' and pointed the
        # player at a dead source, which is why the stream stayed silent
        # (live 2026-09-14).
        from lyrion.networking.protocol import SlimProtoClient
        from lyrion.formats.lms_types import FORMAT_TO_BYTE
        from lyrion.formats.stream_probe import scan_stream_url

        suffix_codec = SlimProtoClient._guess_codec_from_url(url)
        scan = await scan_stream_url(url)                 # Perl scanURL
        if scan.failed:
            logger.warning(
                "play_url %s: Stream-Scan für %s fehlgeschlagen: %s "
                "(Song.pm:302-312 'playlist cant_open' — kein strm, kein Proxy)",
                player_id, url[:70], scan.error)
            player.playlist = old_playlist
            player.playlist_position = old_position
            return False
        # Perl's format byte comes from the scanned content type
        # (`$song->wantFormat`); the URL suffix ("getFormatForURL") and 'mp3'
        # are only the fallbacks of Squeezebox.pm:585-593.
        url = scan.url                                     # redirect/playlist target
        byte = FORMAT_TO_BYTE.get((scan.type or "").lower())
        codec = byte or suffix_codec
        stream_bitrate = scan.bitrate                      # Remote.pm:530-545
        if codec != suffix_codec:
            logger.info("Stream codec from headers: %s (suffix said '%s')",
                        codec, suffix_codec)
        ok = await handler.send_remote_stream(player.mac, url, codec,
                                              resolved=True)
        if ok:
            logger.info("play_url codec guess: %s -> '%s'", url[:60], codec)
        if ok:
            # Playing implies power-on (Perl enables the audio outputs then).
            await self.power_on_for_playback(player)
            # New stream, new metadata — Perl's ``Song::open`` drops the
            # client's metadata title on EVERY stream start
            # (``$client->metaTitle(undef)``, ``Slim/Player/Song.pm:700-702``)
            # and the metadata is filed under the URL of the song that IS
            # streaming (``%currentTitles{$url}``, Info.pm:552). The replaced
            # sender's ``remoteMeta``/title must not survive the switch (LIVE
            # 2026-09-18: switching onto a proxied station left remoteMeta =
            # {title:'Sea Surfaces', artist:'Martin Nonstatic',
            #  url:'http://hirschmilch.de:7000/chillout.mp3'}). The strm tail
            # did the same a moment ago (``_after_strm_sent``); here the station
            # name becomes the baseline the status shows until the stream
            # reports its own ``StreamTitle`` (Perl ``standardTitle``,
            # Info.pm:556-583) — unless a frame of THIS stream already arrived.
            if int(getattr(player, "stream_meta_epoch", 0) or 0) != int(
                    getattr(player, "stream_epoch", 0) or 0):
                player.forget_metadata()
                player.current_title = title or url
            # Perl's ``standardTitle($client, $url)`` baseline for THIS stream
            # (Info.pm:556-583): what ``getCurrentTitle`` answers while no
            # (truthy) in-stream title is cached — e.g. after an empty
            # ``StreamTitle``.
            player.stream_baseline_title = title or url
            player.current_url = url
            player.current_track_id = None
            player.remote = 1  # radio stream: never "track end"
            # A remote stream starts at 0 (Perl's ``canSeek()`` is false, so
            # ``positionAtDisconnect`` is 0 and there is no timeOffset).
            player.stream_start_offset = 0.0
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

    async def playlist_play(self, player_id: str, index: int,
                            start_seconds: float = 0.0) -> bool:
        """Play the track at a playlist index (0-based).

        A playlist entry is either a DB track id (``int``) or a remote stream
        URL (``str``, radio/favorites); Perl streams both through one path — a
        ``Song`` whose ``url`` is local or remote (``Slim/Player/Song.pm``, the
        remote scan ``Slim/Utils/Scanner/Remote.pm``). The stream branch mirrors
        the web API's ``_play_playlist_item`` so ``playlist jump``/``next`` work
        on a queue of radio streams too.
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        if not player.playlist:
            return False
        if index < 0 or index >= len(player.playlist):
            return False
        player.playlist_position = index
        item = player.playlist[index]
        if not isinstance(item, int):
            # Stream URL entry — send the strm frame for the remote source.
            handler = self._protocol_handler
            if handler is None:
                return False
            from lyrion.networking.protocol import SlimProtoClient

            url = str(item)
            codec = SlimProtoClient._guess_codec_from_url(url)
            ok = await handler.send_remote_stream(player.mac, url, codec)
            if not ok:
                return False
            await self.power_on_for_playback(player)
            player.playlist_position = index
            player.playlist_total = len(player.playlist)
            player.mode = "play"
            player.remote = 1
            player.current_track_id = None
            player.current_url = url
            player.elapsed = 0.0
            # A remote stream cannot seek: Perl stores position 0 for it
            # (Slimproto.pm:317 ``canSeek()``) and starts at 0 as well.
            player.stream_start_offset = 0.0
            player.last_activity = time.time()
            return True
        ok = await self.play_track(player_id, item, start_seconds)
        if ok:
            player.playlist_position = index
            player.playlist_total = len(player.playlist)
        return ok

    async def playlist_jump(self, player_id: str, index) -> bool:
        """``playlist jump|index`` — ``Slim/Control/Commands.pm:921-1036``.

        Absolute index or relative ``+n``/``-n`` offset; the target index comes
        from :func:`jump_target` and is then played (``:1016-1021``).
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        target = jump_target(player, index)
        if target is None:
            return False
        return await self.playlist_play(player_id, target)

    async def seek_to(self, player_id: str, seconds: int) -> bool:
        """``time <n>`` — Perl's ``gototime`` → ``jumpToTime`` → ``_JumpToTime``.

        Perl does NOT skip within the player's buffers: it re-opens the
        source at the requested position.

          * ``timeCommand`` → ``Slim::Player::Source::gototime``
            (``Slim/Control/Commands.pm:3069-3086``; ``Source.pm:216-221``)
            → ``controller->jumpToTime`` (``StreamingController.pm:2214-2217``)
            → ``_JumpToTime`` (``StreamingController.pm:1092-1141``).
          * ``newtime == 0`` (absolute) or an unknown duration → restart the
            current item (``_Stop`` + ``resetSeekdata`` + ``_Stream`` without
            seekdata, :1097-1111);
          * ``newtime > duration`` → ``_Skip`` (:1127-1130);
          * otherwise ``getSeekData(newtime)`` → ``{timeOffset => newtime}``
            (``File.pm:372-380``) and ``_Stop`` + ``_Stream(seekdata)``
            (:1132-1141). ``File.pm:194-202`` seeks the file to the byte
            offset of that position and sets ``$song->startOffset(timeOffset)``,
            so the song clock counts on from there: ``playingSongElapsed`` =
            ``startOffset + player elapsed`` (``StreamingController.pm:1719-1743``)
            → ``Source::songTime`` (``Source.pm:51-53``) → the status ``time``
            field (``Queries.pm:4093-4094``).

        ``strm 'a'`` is a DIFFERENT command — Perl's ``skipAhead``
        (``Squeezebox2.pm:1120-1127``), sent only by the sync correction
        (``_CheckSync``, ``StreamingController.pm:566-568``). The player
        applies it to its OUTPUT buffer, so the position moves by at most the
        buffered few seconds and the song clock keeps its old base — the
        requested position is lost and the status ``time`` never reaches it
        (live 2026-09-21: click at 324 s of a 519 s track → status ``time``
        only went 49 s → 132 s, then counted on from there).
        """
        player = self.get_player(player_id)
        if player is None:
            return False
        handler = self._protocol_handler
        if handler is None:
            return False

        pos = player.playlist_position or 0
        items = player.playlist or []
        if not (0 <= pos < len(items)):
            return False
        item = items[pos]
        is_track = isinstance(item, int)
        duration = float(getattr(player, "duration", 0) or 0)

        # Perl _JumpToTime :1097-1111 — restart the current item from 0. A
        # remote (radio) stream has no duration either (``canSeek`` false,
        # File.pm:403-415 / HTTP.pm:1186-1188), so it takes this branch too.
        if seconds <= 0 or not duration or not is_track:
            # Perl ``_Stop`` closes the stream before the restart, so the
            # re-stream of the SAME track must not be swallowed by the
            # idempotency guard (protocol.py ``send_strm_to_player``).
            player.forget_stream()
            if is_track:
                return await self.play_track(player_id, item, 0.0)
            return await self.play_url(player_id, str(item))

        # Perl :1127-1130 — past the end of the song: ``_Skip`` advances.
        if seconds > duration:
            return await self.playlist_next(player_id)

        # Perl :1132-1141 — ``_Stop`` + ``_Stream`` with the seekdata of the
        # position. The player's own clock restarts at 0 with the new stream
        # and ``stream_start_offset`` (the protocol layer's ``startOffset``)
        # carries the position, so the status ``time`` jumps to ``seconds``
        # and counts on from there.
        player.forget_stream()
        return await self.play_track(player_id, item, float(seconds))

    async def playlist_next(self, player_id: str) -> bool:
        """Skip to the next track in the playlist (wraps to start).

        Perl's ``skip`` is what ``playlist jump +1`` runs
        (``Commands.pm:980-986``) — routed through :func:`jump_target` so the
        wrap and the ``repeat == 1`` (repeat-song) rule match
        (``StreamingController.pm:848-899``).
        """
        return await self.playlist_jump(player_id, "+1")

    async def playlist_prev(self, player_id: str) -> bool:
        """Go back to the previous track in the playlist (wraps to end)."""
        return await self.playlist_jump(player_id, "-1")

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
