"""StreamingController state machine — Perl ``Slim/Player/StreamingController.pm``.

Perl keeps TWO orthogonal state variables per player group and drives them
from one jump table (``%stateTable``):

* ``streamingState``  — ``IDLE(0) / STREAMING(1) / STREAMOUT(2) / TRACKWAIT(3)``
  (``StreamingController.pm:26-32``; names in ``@StreamingStateName`` :31).
* ``playingState``    — ``STOPPED(0) / BUFFERING(1) / WAITING_TO_SYNC(2) /
  PLAYING(3) / PAUSED(4)`` (``StreamingController.pm:35-42``).

Every inbound event (a user command like ``stop``/``play``/``skip`` or a
player notification like ``playerTrackStarted`` / ``playerOutputUnderrun``)
is mapped through ``%stateTable[$event][$playingState][$streamingState]`` to a
named *action* (``StreamingController.pm:110-245``). The action is what
actually changes the state and performs the side effects
(``_eventAction``, ``:249-311``).

This module ports that table and the state-only half of the actions. The
side-effect half (``$player->play``, ``$player->stop``, opening the source
stream, buffer fullness) needs a real player socket + codec and is expressed
as optional **hooks** — see :class:`StreamingHooks`. ``networking/protocol.py``
drives :func:`apply_event` from the live SlimProto paths it used to handle with
ad-hoc ``player.mode = "…"`` assignments, and keeps ``player.mode`` as the
status field (Perl derives its status ``mode`` from
``isPlaying()/isPaused()/isStopped()``, ``:1676-1695``).

Gapless groundwork
------------------
Perl's strm frame carries the transition parameters (``Squeezebox.pm:930-1012``)
and the ``autostart`` byte (``Squeezebox.pm:519``, ``:570``). The constants and
the selection helper are ported here; a *real* gapless/crossfade transition is
UNKLAR (needs player firmware + matching sample rates), see below.

UNKLAR / deliberately NOT implemented (needs a real client or codec)
--------------------------------------------------------------------
* ``STREAMOUTPUT_*`` — **no such symbol exists** in the Perl reference
  (``grep -rn STREAMOUTPUT /tmp/lms-ref`` → nothing). The task's guess is not
  a Perl fact; the real output-buffer handling lives in ``Player.pm``
  (``rebuffer`` :1035-1066, ``_buffering`` :1094-1190) and uses
  ``outputBufferFullness()``/``bufferFullness()`` from the STAT frames.
* ``Source.pm setDrainType`` — **also not present** in this reference
  (``grep -rn setDrainType`` → nothing). There is no drain-type concept.
* Real buffer-fill decisions (``_CheckPaused`` ``StreamingController.pm:424-452``,
  ``_CheckSync`` ``:485-583``) need live ``usage()``/``bufferFullness()``/
  ``playPoint()`` values from a connected player. Hooked, never guessed.
* A gap-free crossfade/gapless handoff: ``Squeezebox.pm:995-997`` refuses the
  transition unless both tracks share the sample rate
  (``ReplayGain->trackSampleRateMatch``) — that comparison needs decoded audio.
* ``frameData``/``initialStreamBuffer`` (SB1/SliMP3 sync rate calculation,
  ``:1866-1887``) — needs the byte-by-byte stream pump.
"""

from __future__ import annotations

import asyncio
import logging
from enum import IntEnum
from typing import Any, Callable, Optional

logger = logging.getLogger("lyrion.player.streaming")


# ---------------------------------------------------------------------------
# States (Perl StreamingController.pm:26-42)
# ---------------------------------------------------------------------------

class StreamingState(IntEnum):
    """``@StreamingStateName`` (StreamingController.pm:31)."""

    IDLE = 0        # StreamingController.pm:26
    STREAMING = 1   # StreamingController.pm:27
    STREAMOUT = 2   # StreamingController.pm:28
    TRACKWAIT = 3   # StreamingController.pm:29 — next track not ready yet


class PlayingState(IntEnum):
    """``@PlayingStateName`` (StreamingController.pm:41)."""

    STOPPED = 0         # StreamingController.pm:35
    BUFFERING = 1       # StreamingController.pm:36
    WAITING_TO_SYNC = 2  # StreamingController.pm:37
    PLAYING = 3         # StreamingController.pm:38
    PAUSED = 4          # StreamingController.pm:39


STREAMING_STATE_NAME = {
    StreamingState.IDLE: "IDLE",          # StreamingController.pm:31
    StreamingState.STREAMING: "STREAMING",
    StreamingState.STREAMOUT: "STREAMOUT",
    StreamingState.TRACKWAIT: "TRACKWAIT",
}

PLAYING_STATE_NAME = {
    PlayingState.STOPPED: "STOPPED",      # StreamingController.pm:41
    PlayingState.BUFFERING: "BUFFERING",
    PlayingState.WAITING_TO_SYNC: "WAITING_TO_SYNC",
    PlayingState.PLAYING: "PLAYING",
    PlayingState.PAUSED: "PAUSED",
}

#: ``use constant FADEVOLUME => 0.3125`` (StreamingController.pm:44).
FADEVOLUME = 0.3125


# ---------------------------------------------------------------------------
# Validity matrix — Perl StreamingController.pm:101-108
# ---------------------------------------------------------------------------

#: ``@ValidStates`` indexed ``[playingState][streamingState]``. ``0`` marks a
#: combination Perl considers invalid and logs (``_eventAction`` :288-295).
VALID_STATES: tuple[tuple[int, ...], ...] = (
    #  IDLE  STREAMING  STREAMOUT  TRACKWAIT     playingState
    (1, 0, 0, 1),   # STOPPED         StreamingController.pm:103
    (0, 1, 1, 0),   # BUFFERING       StreamingController.pm:104
    (0, 1, 1, 0),   # WAITING_TO_SYNC StreamingController.pm:105
    (1, 1, 1, 1),   # PLAYING         StreamingController.pm:106
    (1, 1, 1, 1),   # PAUSED          StreamingController.pm:107
)


def _rows(*rows: tuple[str, str, str, str]) -> tuple[tuple[str, ...], ...]:
    return tuple(rows)


#: ``%stateTable`` (StreamingController.pm:110-245) — verbatim.
#: ``STATE_TABLE[event][playingState][streamingState]`` → action name.
STATE_TABLE: dict[str, tuple[tuple[str, ...], ...]] = {
    "Stop": _rows(      # StreamingController.pm:112-118
        ("_NoOp", "_BadState", "_BadState", "_Stop"),
        ("_BadState", "_Stop", "_Stop", "_BadState"),
        ("_BadState", "_Stop", "_Stop", "_BadState"),
        ("_Stop", "_Stop", "_Stop", "_Stop"),
        ("_Stop", "_Stop", "_Stop", "_Stop"),
    ),
    "Play": _rows(      # StreamingController.pm:119-125
        ("_StopGetNext", "_BadState", "_BadState", "_StopGetNext"),
        ("_BadState", "_StopGetNext", "_StopGetNext", "_BadState"),
        ("_BadState", "_StopGetNext", "_StopGetNext", "_BadState"),
        ("_StopGetNext", "_StopGetNext", "_StopGetNext", "_StopGetNext"),
        ("_StopGetNext", "_StopGetNext", "_StopGetNext", "_StopGetNext"),
    ),
    "ContinuePlay": _rows(  # StreamingController.pm:126-132
        ("_Stop", "_BadState", "_BadState", "_StopGetNext"),
        ("_BadState", "_StopGetNext", "_StopGetNext", "_BadState"),
        ("_BadState", "_StopGetNext", "_StopGetNext", "_BadState"),
        ("_Continue", "_Continue", "_Continue", "_PlayIfReady"),
        ("_Stop", "_Stop", "_Stop", "_Stop"),
    ),
    "Pause": _rows(     # StreamingController.pm:133-139
        ("_Invalid", "_BadState", "_BadState", "_NoOp"),
        ("_BadState", "_NoOp", "_NoOp", "_BadState"),
        ("_BadState", "_NoOp", "_NoOp", "_BadState"),
        ("_Pause", "_Pause", "_Pause", "_Pause"),
        ("_JumpOrResume", "_Resume", "_Resume", "_Resume"),
    ),
    "Resume": _rows(    # StreamingController.pm:140-146
        ("_Invalid", "_BadState", "_BadState", "_Invalid"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_Invalid", "_Invalid", "_Invalid", "_Invalid"),
        ("_JumpOrResume", "_Resume", "_Resume", "_Resume"),
    ),
    "Flush": _rows(     # StreamingController.pm:147-153
        ("_Invalid", "_BadState", "_BadState", "_Invalid"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_FlushGetNext", "_FlushGetNext", "_FlushGetNext", "_FlushGetNext"),
        ("_FlushGetNext", "_FlushGetNext", "_FlushGetNext", "_FlushGetNext"),
    ),
    "Skip": _rows(      # StreamingController.pm:154-160
        ("_StopGetNext", "_BadState", "_BadState", "_NoOp"),
        ("_BadState", "_Skip", "_Skip", "_BadState"),
        ("_BadState", "_Skip", "_Skip", "_BadState"),
        ("_StopGetNext", "_Skip", "_Skip", "_Skip"),
        ("_StopGetNext", "_Skip", "_Skip", "_Skip"),
    ),
    "JumpToTime": _rows(  # StreamingController.pm:161-167
        ("_Invalid", "_BadState", "_BadState", "_Invalid"),
        ("_BadState", "_JumpToTime", "_JumpToTime", "_BadState"),
        ("_BadState", "_JumpToTime", "_JumpToTime", "_BadState"),
        ("_JumpToTime", "_JumpToTime", "_JumpToTime", "_JumpToTime"),
        ("_JumpPaused", "_JumpPaused", "_JumpPaused", "_JumpPaused"),
    ),
    "NextTrackReady": _rows(  # StreamingController.pm:168-174
        ("_NoOp", "_BadState", "_BadState", "_Stream"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_Invalid", "_Invalid", "_Invalid", "_StreamIfReady"),
        ("_Invalid", "_Invalid", "_Invalid", "_StreamIfReady"),
    ),
    "NextTrackError": _rows(  # StreamingController.pm:175-181
        ("_Invalid", "_BadState", "_BadState", "_NextIfMore"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_Invalid", "_Invalid", "_Invalid", "_NextIfMore"),
        ("_Invalid", "_Invalid", "_Invalid", "_NextIfMore"),
    ),
    "LocalEndOfStream": _rows(  # StreamingController.pm:182-188
        ("_Invalid", "_BadState", "_BadState", "_Invalid"),
        ("_BadState", "_Streamout", "_Invalid", "_BadState"),
        ("_BadState", "_Streamout", "_Invalid", "_BadState"),
        ("_Invalid", "_Streamout", "_Invalid", "_Invalid"),
        ("_Invalid", "_Streamout", "_Invalid", "_Invalid"),
    ),
    "BufferReady": _rows(  # StreamingController.pm:189-195
        ("_Invalid", "_BadState", "_BadState", "_Invalid"),
        ("_BadState", "_WaitToSync", "_WaitToSync", "_BadState"),
        ("_BadState", "_StartIfReady", "_StartIfReady", "_BadState"),
        ("_Invalid", "_Invalid", "_Invalid", "_Invalid"),
        ("_Invalid", "_Invalid", "_Invalid", "_Invalid"),
    ),
    "Started": _rows(   # StreamingController.pm:196-202
        ("_Invalid", "_BadState", "_BadState", "_Invalid"),
        ("_BadState", "_Playing", "_Playing", "_BadState"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_PlayAndNext", "_Playing", "_Playing", "_PlayAndStream"),
        ("_PlayAndNext", "_Playing", "_Playing", "_PlayAndStream"),
    ),
    "StreamingFailed": _rows(  # StreamingController.pm:203-209
        ("_Invalid", "_BadState", "_BadState", "_Invalid"),
        ("_BadState", "_StopNextIfMore", "_StopNextIfMore", "_BadState"),
        ("_BadState", "_StopNextIfMore", "_StopNextIfMore", "_BadState"),
        ("_Invalid", "_SyncStopNext", "_SyncStopNext", "_Invalid"),
        ("_Invalid", "_Stop", "_Stop", "_Invalid"),
    ),
    "EndOfStream": _rows(  # StreamingController.pm:210-216
        ("_NoOp", "_BadState", "_BadState", "_NoOp"),
        ("_BadState", "_StartStreamout", "_Start", "_BadState"),
        ("_BadState", "_StartStreamout", "_Start", "_BadState"),
        ("_Invalid", "_AutoStart", "_AutoStart", "_Invalid"),
        ("_Invalid", "_Streamout", "_NoOp", "_Invalid"),
    ),
    "ReadyToStream": _rows(  # StreamingController.pm:217-223
        ("_Invalid", "_BadState", "_BadState", "_Invalid"),
        ("_BadState", "_NoOp", "_Invalid", "_BadState"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_NoOp", "_NextIfMore", "_RetryOrNext", "_StreamIfReady"),
        ("_NoOp", "_NextIfMore", "_NextIfMore", "_StreamIfReady"),
    ),
    "Stopped": _rows(   # StreamingController.pm:224-230
        ("_Invalid", "_BadState", "_BadState", "_NoOp"),
        ("_BadState", "_NoOp", "_NoOp", "_BadState"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_Stopped", "_Buffering", "_Buffering", "_PlayIfReady"),
        ("_Stopped", "_Buffering", "_Buffering", "_Stopped"),
    ),
    "OutputUnderrun": _rows(  # StreamingController.pm:231-237
        ("_NoOp", "_BadState", "_BadState", "_NoOp"),
        ("_BadState", "_NoOp", "_NoOp", "_BadState"),
        ("_BadState", "_Invalid", "_Invalid", "_BadState"),
        ("_Invalid", "_Rebuffer", "_Rebuffer", "_Invalid"),
        ("_NoOp", "_NoOp", "_NoOp", "_NoOp"),
    ),
    "StatusHeartbeat": _rows(  # StreamingController.pm:238-244
        ("_NoOp", "_BadState", "_BadState", "_NoOp"),
        ("_BadState", "_NoOp", "_NoOp", "_BadState"),
        ("_BadState", "_StartIfReady", "_StartIfReady", "_BadState"),
        ("_CheckSync", "_CheckSync", "_CheckSync", "_CheckSync"),
        ("_NoOp", "_CheckPaused", "_CheckPaused", "_NoOp"),
    ),
}

#: Events whose outcome is reported to the UI as a SlimProto/status ``mode``.
#: Perl derives ``mode`` from the state accessors
#: (``isPlaying``/``isPaused``/``isStopped``, StreamingController.pm:1676-1695).
EVENT_MODE: dict[str, str] = {
    "Started": "play",
    "Pause": "pause",
    "Resume": "play",
    "Stopped": "stop",
    "StreamingFailed": "stop",
}

#: ``@retryIntervals`` + retry limits (StreamingController.pm:1429-1431).
RETRY_INTERVALS = (5, 10, 15, 30)
RETRY_LIMIT = 5 * 60           # StreamingController.pm:1430
RETRY_LIMIT_PLAYLIST = 30      # StreamingController.pm:1431


# ---------------------------------------------------------------------------
# Gapless groundwork (Squeezebox.pm / Squeezebox2.pm)
# ---------------------------------------------------------------------------

#: ``use constant TRANSITION_*`` (Squeezebox.pm:36-41).
TRANSITION_NONE = 0                 # Squeezebox.pm:36
TRANSITION_CROSSFADE = 1            # Squeezebox.pm:37
TRANSITION_FADEIN = 2               # Squeezebox.pm:38
TRANSITION_FADEOUT = 3              # Squeezebox.pm:39
TRANSITION_FADEINOUT = 4            # Squeezebox.pm:40
TRANSITION_CROSSFADE_IMMEDIATE = 5  # Squeezebox.pm:41

#: ``transitionType``/``transitionDuration`` player pref defaults
#: (Squeezebox2.pm:44-45).
DEFAULT_TRANSITION_TYPE = 0        # TRANSITION_NONE
DEFAULT_TRANSITION_DURATION = 10   # seconds


def autostart_value(paused: bool) -> int:
    """The strm ``autostart`` byte: ``$params->{paused} ? 0 : 1``.

    ``Squeezebox.pm:570``. Direct streams add 2
    (``$autostart += 2``, Squeezebox.pm:796/:804/:841/:884) — handled by the
    caller, see :data:`AUTOSTART_DIRECT_FLAG`.
    """
    return 0 if paused else 1


#: ``$autostart += 2`` for direct streaming (Squeezebox.pm:796).
AUTOSTART_DIRECT_FLAG = 2


def select_transition(params: dict, prefs: dict | None = None) -> tuple[int, int]:
    """Pick ``(transitionType, transitionDuration)`` like Perl does.

    ``StreamingController.pm:1268`` sets ``fadeIn`` from the play command;
    ``Squeezebox.pm:930-941`` turns that into the frame fields:

    * ``fadeIn`` set    → ``TRANSITION_FADEIN`` + its duration (:934-935),
    * ``crossFade`` set → ``TRANSITION_CROSSFADE_IMMEDIATE`` + duration (:937-938),
    * otherwise the player prefs ``transitionType``/``transitionDuration``
      (:940-941), whose defaults are 0/10 (Squeezebox2.pm:44-45).

    The result is only the *requested* transition. ``Squeezebox.pm:945-1012``
    then downgrades it when both tracks do not share a sample rate (:995-997)
    or either is shorter than ``2 × duration`` (:1011-1012) — that needs the
    decoded tracks, so it is UNKLAR here.
    """
    prefs = prefs or {}
    if params.get("fadeIn"):
        return TRANSITION_FADEIN, int(params["fadeIn"])
    if params.get("crossFade"):
        return TRANSITION_CROSSFADE_IMMEDIATE, int(params["crossFade"])
    return (
        int(prefs.get("transitionType") or DEFAULT_TRANSITION_TYPE),
        int(prefs.get("transitionDuration") or DEFAULT_TRANSITION_DURATION),
    )


# ---------------------------------------------------------------------------
# Hooks — the side-effect half of Perl's actions
# ---------------------------------------------------------------------------

class StreamingHooks:
    """Optional callables for the side effects Perl performs in its actions.

    Every hook is optional; a missing hook is a no-op. This is what makes the
    state machine testable without a player, source stream or codec — and it
    keeps the *decisions* (states) here while the I/O stays in the caller.

    Hook                 Perl source
    -------------------  --------------------------------------------------
    stop_client          ``_stopClient`` (:622-627) → ``$client->stop``
    close_stream         ``closeStream`` / ``$songStreamController->close``
    flush_player         ``_FlushGetNext`` (:996-998) → ``$player->flush``
    pause_player         ``_Pause`` (:1561-1583) / ``_Rebuffer`` (:1664-1667)
    rebuffer_player      ``_Rebuffer`` (:1666) → ``$player->rebuffer``
    resume_player        ``_Resume`` (:1630-1648) / ``_Start`` (:1420)
    start_playback       ``_Start`` → ``_syncStart`` (:1417-1421)
    playback_started     ``_Playing`` (:331-399) — newsong notification
    notify_stopped       ``_notifyStopped`` (:406-420)
    playing_elapsed      ``playingSongElapsed`` (:1719-1791)
    can_do_action        ``_Skip`` (:982) / ``_JumpToTime`` (:1106) handler veto
    will_retry           ``_willRetry`` (:1434-1485)
    active_players       ``activePlayers`` (:2136-2138)
    playlist_count       ``Slim::Player::Playlist::count`` (:632, :856)
    next_song_index      ``nextsong`` (:848-900)
    make_song            ``Slim::Player::Song->new`` (:672, :1825)
    get_next_song        ``$song->getNextSong`` (:704-711)
    current_track_url    ``$song->currentTrack()->url``
    can_seek             ``$song->canSeek()`` (:1060, :1101)
    song_duration        ``$song->duration()`` (:1101, :1127)
    can_do_action        handler veto for 'stop'/'rew'/'pause'
    all_buffer_ready     ``$player->isBufferReady`` (:1527)
    all_ready_to_stream  ``$player->isReadyToStream`` (:1394)
    check_sync           ``_CheckSync`` (:485-583) — needs playPoints
    check_paused         ``_CheckPaused`` (:424-452) — needs buffer fullness
    open_stream          ``$song->open`` (:1250)
    """

    def __init__(self, **callables: Callable):
        self._callables = {k: v for k, v in callables.items() if callable(v)}

    def get(self, name: str) -> Optional[Callable]:
        return self._callables.get(name)


# ---------------------------------------------------------------------------
# Perl's own side effects — the default hooks of the live server
# ---------------------------------------------------------------------------

def default_hooks() -> StreamingHooks:
    """The hooks a live controller carries: Perl's own notification path.

    Only ``playback_started`` has an implementation here, because it is the
    one side effect of the state machine that needs no player socket and no
    codec: it is a ``notifyFromArray`` call. Every other hook stays optional
    (``$player->play``/``stop``/``flush`` need the player), see
    :class:`StreamingHooks`.
    """
    return StreamingHooks(playback_started=_on_playback_started)


def _on_playback_started(*, master: str = "", song: Any = None, **_: Any) -> None:
    """``_Playing`` tail — ``StreamingController.pm:381-393``.

    ``if ( $last_song ) { Slim::Control::Request::notifyFromArray(
    $self->master(), ['playlist','newsong', ...,$last_song->index()] ) }``

    ``notifyFromArray`` only *queues* the request (``Request.pm:852``; the
    idle loop sends it, :856-862), so the port schedules the task instead of
    doing a blocking title lookup inside the event dispatch.
    """
    if not master or song is None:          # ``if ( $last_song )``
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return               # no loop → no idle loop → nothing to deliver to
    loop.create_task(send_newsong_playing(master, song))


async def send_newsong_playing(master: str, song: Any = None) -> None:
    """Send ``['playlist','newsong', title, index]`` — 4 elements (``:381-393``).

    Perl builds the array as ``('playlist', 'newsong',
    Slim::Music::Info::standardTitle($self->master(), $last_song->currentTrack()),
    $last_song->index())``: the verb pair, then the display title of the song
    that just started, then its *playlist* index. ``newsong`` declares no
    parameters (``Request.pm:645`` → ``[1,0,0,undef]``), so the two values land
    as ``_p2``/``_p3`` and render as bare elements (``Request.pm:1049-1061``).

    The notification's client id is ``$self->master()->id()``
    (``Request.pm:846-848``) — the player's own MAC, not the controller's
    normalised registry key.
    """
    from lyrion.control.notifications import notify_from_array
    from lyrion.player.manager import PlayerManager

    song = song if song is not None else _controller_song(master)
    if song is None:                        # ``if ( $last_song )``
        return
    player = PlayerManager().get_player(master)
    client_id = str(getattr(player, "mac", "") or master) if player else master
    index = _song_index(song, player)
    title = await standard_title(player)
    notify_from_array(client_id, ["playlist", "newsong", title, index])


def _controller_song(master: str) -> Any:
    ctl = controller_for(master)
    return ctl.playing_song() if ctl is not None else None


def _song_index(song: Any, player: Any) -> Optional[int]:
    """``$last_song->index()`` — the song's index in the play list.

    ``Slim/Player/Song.pm:81-121``: ``Slim::Player::Song->new($owner, $index,
    $seekdata)`` looks the track up as ``Slim::Player::Playlist::track($client,
    $index)`` and stores ``index => $index``, so it is the playlist position of
    the song. Our song queue holds the *track id* on the live stream paths
    (``networking/protocol.py:2440`` ``note_strm_sent(mac, track_id)``), so the
    position comes from the player; a song object that carries its own
    ``index`` (Perl-shaped, and what the unit tests use) wins.
    """
    value: Any = None
    if isinstance(song, dict):
        value = song.get("index")
    elif song is not None:
        attr = getattr(song, "index", None)
        value = attr() if callable(attr) else attr
    if value is not None:
        return int(value)
    if player is None:
        return None
    return int(getattr(player, "playlist_position", 0) or 0)


async def standard_title(player: Any) -> str:
    """``Slim::Music::Info::standardTitle($master, $track)`` — ``Info.pm:701-739``.

    ``standardTitle`` formats the track with the client's ``titleFormat``,
    default ``'TITLE'`` (``Utils/Prefs.pm:249-250`` + ``Client.pm:52-53``),
    which ``TitleFormatter.pm:44-51`` resolves to the track's own title. The
    port's title formatter is ``PlayerManager._current_song_title``
    (``getCurrentTitle``, ``Info.pm:556-583``) — the same text the status and
    the display show, so the notification and the status can never disagree.
    """
    if player is None:
        return ""
    from lyrion.player.manager import PlayerManager

    try:
        return await PlayerManager()._current_song_title(player)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 — a notification must never raise
        logger.debug("standardTitle for %s failed: %s",
                     getattr(player, "mac", "?"), exc)
        return str(getattr(player, "current_title", "") or "")


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------

class StreamingController:
    """Port of ``Slim::Player::StreamingController`` (state machine only).

    ``StreamingController.pm:46-87`` (``new``) initialises the fields; the
    fields whose comments name a Perl line below mirror that constructor.
    """

    def __init__(self, master_id: str = "", hooks: StreamingHooks | None = None):
        self.master_id = master_id                    # StreamingController.pm:50
        self.all_players: list[str] = []              # StreamingController.pm:52
        self.players: list[str] = []                  # StreamingController.pm:51

        # State
        self.streaming_state = StreamingState.IDLE    # StreamingController.pm:55
        self.playing_state = PlayingState.STOPPED     # StreamingController.pm:56
        self.rebuffering = 0                          # StreamingController.pm:57
        self.last_state_change = 0.0                  # StreamingController.pm:58

        # Streaming control
        self.songqueue: list[Any] = []                # StreamingController.pm:61
        self.song_stream_controller = None            # StreamingController.pm:62
        self.next_check_sync_time = 0.0               # StreamingController.pm:63
        self.resume_time: Optional[float] = None      # StreamingController.pm:64

        # Sync management (ported where it needs no player)
        self.syncgroupid = None                       # StreamingController.pm:67
        self.frame_data = None                        # StreamingController.pm:68
        self.initial_stream_buffer = None             # StreamingController.pm:69

        # Track management
        self.next_track = None                        # StreamingController.pm:72
        self.next_track_callback_id = 0               # StreamingController.pm:73
        self.consecutive_errors = 0                   # StreamingController.pm:74
        self.fade_in = None                           # StreamingController.pm:1268
        self.fade_active = None                       # StreamingController.pm:1290

        self.hooks = hooks or StreamingHooks()
        self.events_seen: list[str] = []

    # -- hook plumbing ---------------------------------------------------
    def _hook(self, name: str, default: Any = None, **kw: Any) -> Any:
        fn = self.hooks.get(name)
        if fn is None:
            return default
        try:
            result = fn(**kw)
        except Exception as exc:  # noqa: BLE001 — a hook must never kill the machine
            logger.warning("streaming hook %s failed for %s: %s",
                           name, self.master_id, exc)
            return default
        return default if result is None and default is not None else result

    # -- state mutators (Perl :2426-2446) --------------------------------
    def _set_playing_state(self, state: PlayingState) -> None:
        self.playing_state = state
        # StreamingController.pm:2432 — only BUFFERING/WAITING_TO_SYNC keep
        # the rebuffering flag; every other state clears it.
        if state not in (PlayingState.BUFFERING, PlayingState.WAITING_TO_SYNC):
            self.rebuffering = 0

    def _set_streaming_state(self, state: StreamingState) -> None:
        self.streaming_state = state
        # StreamingController.pm:2442-2445 — leaving to IDLE discards any
        # outstanding getNextTrack callback.
        if state == StreamingState.IDLE:
            self.next_track_callback_id += 1
            self.next_track = None

    # -- queries (Perl :1676-1712) ---------------------------------------
    def is_playing(self, really: bool = False) -> bool:
        if really:
            return self.playing_state == PlayingState.PLAYING
        return not self.is_stopped() and not self.is_paused()

    def is_stopped(self) -> bool:
        return (self.playing_state == PlayingState.STOPPED
                and self.streaming_state == StreamingState.IDLE)

    def is_streaming(self) -> bool:
        return self.streaming_state == StreamingState.STREAMING

    def is_streamout(self) -> bool:
        return self.streaming_state == StreamingState.STREAMOUT

    def is_paused(self) -> bool:
        return self.playing_state == PlayingState.PAUSED

    def is_waiting_to_sync(self) -> bool:
        return self.playing_state == PlayingState.WAITING_TO_SYNC

    def buffering(self) -> int:
        """1 normal buffering, 2 rebuffering, 0 otherwise (Perl :1698-1702)."""
        if self.playing_state in (PlayingState.BUFFERING,
                                  PlayingState.WAITING_TO_SYNC):
            return 2 if self.rebuffering else 1
        return 0

    def is_retrying(self) -> bool:
        return bool(self._hook("is_retrying", default=False))

    def playing_song(self):
        """``$songqueue->[-1]`` (Perl :1805-1807)."""
        return self.songqueue[-1] if self.songqueue else None

    def streaming_song(self):
        """``$songqueue->[0]`` (Perl :1809-1811)."""
        return self.songqueue[0] if self.songqueue else None

    def reported_mode(self) -> str:
        """The status ``mode`` this state maps to (Perl :1676-1695)."""
        if self.is_paused():
            return "pause"
        if self.playing_state == PlayingState.PLAYING or self.is_playing():
            return "play"
        return "stop"

    def state(self) -> str:
        """``"PLAYING-STREAMING"`` — Perl's ``%s-%s`` log form (:270-274)."""
        return (f"{PLAYING_STATE_NAME[self.playing_state]}-"
                f"{STREAMING_STATE_NAME[self.streaming_state]}")

    # -- dispatcher (Perl _eventAction :249-311) -------------------------
    def resolve(self, event: str, **params: Any) -> Optional[str]:
        """Apply ``event`` through ``%stateTable``; return the status mode.

        Mirrors ``_eventAction`` (StreamingController.pm:249-311): look the
        action up, run it, and log when the resulting combination is invalid
        (:288-295). Returns the UI mode via :data:`EVENT_MODE` when the event
        is one Perl reports as a play state, else ``None``.
        """
        row = STATE_TABLE.get(event)
        if row is None:
            raise KeyError(f"unknown StreamingController event: {event!r}")
        action = row[int(self.playing_state)][int(self.streaming_state)]
        if action is None:  # pragma: no cover — table is fully populated
            logger.error("%s: %s in state %s -> undefined",
                         self.master_id, event, self.state())
            return EVENT_MODE.get(event)
        logger.debug("%s: %s in %s -> %s",
                     self.master_id, event, self.state(), action)
        self.events_seen.append(event)
        getattr(self, "_act_" + action[1:].lower())(event, **params)
        if not VALID_STATES[int(self.playing_state)][int(self.streaming_state)]:
            logger.error("%s: %s with action %s resulted in invalid state %s",
                         self.master_id, event, action, self.state())
        return EVENT_MODE.get(event)

    # -- actions (Perl names in comment) ---------------------------------
    def _act_noop(self, event, **p):
        """``_NoOp`` (StreamingController.pm:313)."""

    def _act_badstate(self, event, **p):
        """``_BadState`` (:315-320)."""
        logger.error("%s: event %s received while in invalid state %s",
                     self.master_id, event, self.state())

    def _act_invalid(self, event, **p):
        """``_Invalid`` (:322-327)."""
        logger.warning("%s: event %s received while in invalid state %s",
                       self.master_id, event, self.state())

    def _act_buffering(self, event, **p):
        """``_Buffering`` (:329)."""
        self._set_playing_state(PlayingState.BUFFERING)

    def _act_stopped(self, event, **p):
        """``_Stopped`` (:401-404)."""
        self._set_playing_state(PlayingState.STOPPED)
        self._hook("notify_stopped")

    def _act_stop(self, event, **p):
        """``_Stop`` — stop -> Stopped, Idle (:585-620)."""
        for player in list(self.players):
            self._hook("stop_client", player=player)
        while len(self.songqueue) > 1:      # :603-604
            self.songqueue.pop(0)
        if self.songqueue:                  # :606
            self._hook("mark_song_finished", song=self.songqueue[0])
        if self.song_stream_controller is not None:   # :612-615
            self._hook("close_stream", controller=self.song_stream_controller)
            self.song_stream_controller = None
        self._set_playing_state(PlayingState.STOPPED)  # :617
        self._set_streaming_state(StreamingState.IDLE)  # :618
        self._hook("notify_stopped", suppress=p.get("suppress_notifications"))
        self._set_song_status()

    def _set_song_status(self) -> None:
        """Queue bookkeeping done by Perl's Song objects (:606, :745)."""
        self._hook("refresh_song_status", queue=list(self.songqueue))

    def _act_stopgetnext(self, event, **p):
        """``_StopGetNext`` — stop, getNextTrack -> Stopped, TrackWait (:969-973)."""
        self._act_stop(event, **p)
        self.get_next_track(**p)

    def _act_skip(self, event, **p):
        """``_Skip`` (:975-988)."""
        if not self._hook("can_do_action", default=True, action="stop"):
            logger.info("%s: skip disallowed by protocol handler", self.master_id)
            return
        self._act_stopgetnext(event, **p)

    def _act_streamout(self, event, **p):
        """``_Streamout`` (:422)."""
        self._set_streaming_state(StreamingState.STREAMOUT)

    def _act_startstreamout(self, event, **p):
        """``_StartStreamout`` (:1536-1540)."""
        self._set_streaming_state(StreamingState.STREAMOUT)
        self._act_start(event, **p)

    def _act_start(self, event, **p):
        """``_Start`` — start -> Playing (:1403-1427)."""
        if not self.rebuffering:
            if self.fade_in:
                for player in list(self.players):
                    self._hook("fade_volume", player=player,
                               duration=self.fade_in)
            self.fade_in = None
            if len(self.players) > 1:
                self._hook("sync_start")        # _syncStart :1417-1418
            else:
                self._hook("start_playback")    # $self->master()->resume() :1420
            self._act_playing(event, **p)
        else:
            self._act_resume(event, **p)

    def _act_playing(self, event, **p):
        """``_Playing`` (:331-399)."""
        self.fade_active = None                  # :340
        # Bug 10681 (:342-348): while rebuffering the state must NOT change —
        # an output underrun can race the track-start event.
        if not self.rebuffering:
            self._set_playing_state(PlayingState.PLAYING)
        self.consecutive_errors = 0              # :350
        # :381-393 — the 4-element ``playlist newsong`` notification, fired
        # only while ``$last_song`` exists.
        self._hook("playback_started", master=self.master_id,
                   song=self.playing_song())

    def _act_resume(self, event, **p):
        """``_Resume`` — resume -> Playing (:1620-1656)."""
        self._set_playing_state(PlayingState.PLAYING)   # :1629
        self._hook("resume_player")                     # :1630-1648
        self.resume_time = None                         # :1654
        self.fade_in = None                             # :1655

    def _act_pause(self, event, **p):
        """``_Pause`` — pause -> Paused (:1552-1586)."""
        self.resume_time = self._hook(
            "playing_elapsed", default=self.resume_time or 0.0)  # :1555
        self._set_playing_state(PlayingState.PAUSED)             # :1556
        self._hook("pause_player")                               # :1561-1583

    def _act_rebuffer(self, event, **p):
        """``_Rebuffer`` — pause(noFadeOut) -> Buffering(rebuffering) (:1659-1671)."""
        self._set_playing_state(PlayingState.BUFFERING)  # :1662
        self.rebuffering = 1                             # :1663
        for player in list(self.players):                # :1664-1667
            self._hook("pause_player", player=player)
            self._hook("rebuffer_player", player=player)
        # Bug 17877 (:1669-1670): keep the position for a synced resume.
        self.resume_time = self._hook(
            "playing_elapsed", default=self.resume_time or 0.0)

    def _act_startifready(self, event, **p):
        """``_StartIfReady`` (:1519-1534)."""
        if self._hook("all_buffer_ready", default=False):
            self._act_start(event, **p)

    def _act_waittosync(self, event, **p):
        """``_WaitToSync`` (:1542-1549)."""
        self._set_playing_state(PlayingState.WAITING_TO_SYNC)
        self._act_startifready(event, **p)

    def _act_stream(self, event, **p):
        """``_Stream`` — play -> Buffering, Streaming (:1144-1357)."""
        song = p.get("song") or self.next_track
        if song is None:                       # :1164-1170
            logger.error("No song to stream: try next")
            self._act_nextifmore(event, **p)
            return
        # :1200-1219 — drop stale songs, keep at most 2 (Bug 5103).
        while len(self.songqueue) > 2:
            logger.error("aborting streaming because songqueue too long: %d",
                         len(self.songqueue))
            self.songqueue.pop(0)
        if not self.songqueue or self.songqueue[0] is not song:
            self.songqueue.insert(0, song)
        if self.song_stream_controller is not None:      # :1244-1248
            self._hook("close_stream", controller=self.song_stream_controller)
            self.song_stream_controller = None
        controller = self._hook("open_stream", default=song,
                                song=song, seekdata=p.get("seekdata"))  # :1250
        if controller is None:                 # :1252-1260
            self._hook("error_opening", song=song)
            if self._hook("will_retry", default=False):
                return
            self._act_nextifmore(event, **p, error_song=song)
            return
        self.song_stream_controller = controller        # :1342
        self.next_track = None                          # :1350
        if self.playing_state == PlayingState.STOPPED:  # :1351
            self._set_playing_state(PlayingState.BUFFERING)
        self._set_streaming_state(StreamingState.STREAMING)  # :1352
        # :1354-1356 — players that do not report a track-start event are
        # advanced immediately.
        if not self._hook("reports_track_start", default=True):
            self._act_playing(event, **p)

    def _act_streamifready(self, event, **p):
        """``_StreamIfReady`` (:1386-1401)."""
        if self.next_track is None and not p.get("song"):
            return
        if self._hook("all_ready_to_stream", default=False):
            self._act_stream(event, **p)

    def _act_playifready(self, event, **p):
        """``_PlayIfReady`` (:1359-1370)."""
        self._set_playing_state(PlayingState.STOPPED)
        if self.next_track:
            self._act_stream(event, **p)
        else:
            self._hook("notify_stopped")

    def _act_playandstream(self, event, **p):
        """``_PlayAndStream`` (:1372-1375)."""
        self._act_playing(event, **p)
        self._act_streamifready(event, **p)

    def _act_playandnext(self, event, **p):
        """``_PlayAndNext`` (:1377-1384)."""
        self._act_playing(event, **p)
        self.get_next_track(if_more_tracks=True)

    def _act_continue(self, event, **p):
        """``_Continue`` (:941-967)."""
        seekdata = p.get("seekdata")
        if seekdata and seekdata.get("streamComplete"):
            self._act_streamout(event, **p)                     # :954
        elif seekdata:
            self._act_stream(event, **p, reconnect=True)        # :957
        else:
            self._act_jumptotime(event, newtime=self._hook(
                "playing_elapsed", default=0.0), restart_if_no_seek=1)  # :965

    def _act_flushgetnext(self, event, **p):
        """``_FlushGetNext`` — flush -> Idle; then getNextTrack (:990-1001)."""
        if self.songqueue:
            self.songqueue.pop(0)               # :994
        for player in list(self.players):
            self._hook("flush_player", player=player)   # :996-998
        self._set_streaming_state(StreamingState.IDLE)
        self.get_next_track(if_more_tracks=True, **{k: v for k, v in p.items()
                                                    if k != "if_more_tracks"})

    def _act_nextifmore(self, event, **p):
        """``_NextIfMore`` (:1003-1014)."""
        self._set_streaming_state(StreamingState.IDLE)
        # :1009 — at most one streaming + one playing track at a time.
        if len(self.songqueue) < 2:
            self.get_next_track(if_more_tracks=True)
        else:
            logger.info("streaming track not started yet, will wait until "
                        "then to try next track")

    def _act_stopnextifmore(self, event, **p):
        """``_StopNextIfMore`` (:1017-1026)."""
        self._act_stop(event, **p)
        if self._hook("will_retry", default=False):
            return
        self.get_next_track(if_more_tracks=True)

    def _act_syncstopnext(self, event, **p):
        """``_SyncStopNext`` (:1029-1049)."""
        if self._hook("active_players", default=len(self.players)) > 1:
            self._act_stop(event, **p)
        elif p.get("error_disconnect"):
            self._set_streaming_state(StreamingState.STREAMOUT)
        else:
            self._set_streaming_state(StreamingState.IDLE)
        if self._hook("will_retry", default=False):
            return
        self.get_next_track(if_more_tracks=True)

    def _act_jumptotime(self, event, **p):
        """``_JumpToTime`` (:1092-1142)."""
        raw = p.get("newtime", 0)
        restart_if_no_seek = p.get("restart_if_no_seek")
        duration = self._hook("song_duration", default=0) or 0
        # :1100-1102 — restart when the (non-relative) time is 0 or the song
        # has no duration.
        if (not _is_relative(raw) and _num(raw) == 0) or not duration:
            if not self._hook("can_do_action", default=True, action="rew"):
                return
            self._act_stop(event, suppress_notifications=True)
            self._hook("reset_seekdata")
            self._act_stream(event, song=self.playing_song())
            return
        if _is_relative(raw):
            newtime = self._relative_time(raw)          # :1117-1125
        else:
            newtime = _num(raw)
        if duration and newtime > duration:             # :1127-1130
            self._act_skip(event)
            return
        seekdata = self._hook("get_seekdata", newtime=newtime)  # :1135
        if not seekdata and not restart_if_no_seek:     # :1137
            return
        self._act_stop(event, suppress_notifications=True)  # :1139
        self._act_stream(event, song=self.playing_song(), seekdata=seekdata)

    def _act_jumppaused(self, event, **p):
        """``_JumpPaused`` (:1051-1090)."""
        raw = p.get("newtime", 0)
        restart_if_no_seek = p.get("restart_if_no_seek")
        if not self._hook("can_seek", default=True):     # :1060-1067
            if restart_if_no_seek:
                self._act_stop(event, **p)
            return
        if _is_relative(raw):                            # :1078-1086
            newtime = self._relative_time(raw)
        else:
            newtime = _num(raw)
        if not _is_relative(raw) and newtime == 0:       # :1069-1077
            if not self._hook("can_do_action", default=True, action="rew"):
                return
        self.resume_time = newtime                       # :1088
        self._pause_streaming()                          # :1089

    def _act_jumporresume(self, event, **p):
        """``_JumpOrResume`` (:1605-1618)."""
        if self.resume_time is not None:
            self.fade_in = FADEVOLUME
            self._act_jumptotime(event, newtime=self.resume_time,
                                 restart_if_no_seek=1)
            self.resume_time = None
            self.fade_in = None
        else:
            self._act_resume(event, **p)

    def _act_autostart(self, event, **p):
        """``_AutoStart`` (:1591-1603) — Bug 8861 short-track force start."""
        self._set_streaming_state(StreamingState.STREAMOUT)
        if self.streaming_song() is not None and not self._hook(
                "streaming_song_playing", default=True):
            logger.info("autostart possibly short track")
            for player in list(self.players):
                self._hook("resume_player", player=player)

    def _act_checksync(self, event, **p):
        """``_CheckSync`` (:485-583) — resync needs live playPoints (UNKLAR)."""
        self._hook("check_sync")

    def _act_checkpaused(self, event, **p):
        """``_CheckPaused`` (:424-452) — needs live buffer fullness (UNKLAR)."""
        self._hook("check_paused")

    def _act_retryornext(self, event, **p):
        """``_RetryOrNext`` (:912-939)."""
        self._set_streaming_state(StreamingState.IDLE)
        if self._hook("should_retry", default=False):
            self._act_stream(event, song=self.streaming_song())
            return
        if len(self.songqueue) < 2:
            self.get_next_track(if_more_tracks=True)
        else:
            logger.info("streaming track not started yet, will wait until "
                        "then to try next track")

    # -- helpers ---------------------------------------------------------
    def _relative_time(self, newtime: Any) -> float:
        old = self.resume_time if self.is_paused() else self._hook(
            "playing_elapsed", default=0.0)
        # Perl `$newtime += $oldtime` numifies the string ("+10" → 10) and
        # clamps at 0 (:1083-1085, :1122-1124).
        result = (_num(old) or 0.0) + _num(newtime)
        return max(0.0, result)

    def _pause_streaming(self) -> None:
        """``_pauseStreaming`` (:454-478) — stop clients, drop the stream."""
        if self.streaming_state == StreamingState.IDLE:
            return
        for player in list(self.players):
            self._hook("stop_client", player=player)
        if self.song_stream_controller is not None:      # :465-468
            self._hook("close_stream", controller=self.song_stream_controller)
            self.song_stream_controller = None
        self._set_streaming_state(StreamingState.IDLE)
        self._hook("mark_song_ready", song=self.playing_song())

    def get_next_track(self, index: Optional[int] = None, *,
                       if_more_tracks: bool = False, error_song=None,
                       error_index: Optional[int] = None, **p) -> None:
        """``_getNextTrack`` — getNextTrack -> TrackWait (:629-713)."""
        playlist_count = self._hook("playlist_count", default=0) or 0
        if self.consecutive_errors > playlist_count:      # :632-635
            logger.warning("Giving up because of too many consecutive "
                           "errors: %d", self.consecutive_errors)
            return
        if index is None and error_song is None:
            index = self._hook("next_song_index", default=None,
                               error_index=error_index)
        if index is None:
            if if_more_tracks:
                return                                     # :663-664
            index = 0
        callback_id = self.next_track_callback_id + 1      # :640
        self.next_track_callback_id = callback_id
        self.next_track = None                             # :641
        song = self._hook("make_song", default=None, index=index)  # :672
        if song is None:
            self._set_streaming_state(StreamingState.TRACKWAIT)  # :675
            self.next_track_error(callback_id, error=error_song)
            return
        self._set_streaming_state(StreamingState.TRACKWAIT)  # :681
        # Perl `_showTrackwaitStatus` only acts in STOPPED-TRACKWAIT (:715-719).
        if self.playing_state == PlayingState.STOPPED:
            self._hook("show_trackwait", song=song)          # :702
        self._hook("load_song", song=song)                   # :696-700
        self._hook("get_next_song", song=song,
                   on_ready=lambda _s=song: self.next_track_ready(callback_id, _s),
                   on_error=lambda *a: self.next_track_error(
                       callback_id, song=song, error=a))

    def next_track_ready(self, callback_id: int, song=None, params=None) -> None:
        """``_nextTrackReady`` (:739-753)."""
        if self.next_track_callback_id != callback_id:      # :742-747
            logger.info("%s: discarding unexpected nextTrackCallbackId %s "
                        "(expected %s)", self.master_id, callback_id,
                        self.next_track_callback_id)
            return
        self.next_track = song                              # :749
        self.resolve("NextTrackReady", **(params or {}))

    def next_track_error(self, callback_id: int, song=None, error=None) -> None:
        """``_nextTrackError`` (:755-771)."""
        if self.next_track_callback_id != callback_id:
            return
        self.consecutive_errors += 1                        # :776
        self._hook("error_opening", song=song)              # :768
        self.resolve("NextTrackError", error_song=song, error=error)

    def error_opening(self, song=None, error=None) -> None:
        """``_errorOpening`` (:773-782)."""
        self.consecutive_errors += 1
        self._hook("error_opening", song=song, error=error)

    # -- PlayControl interface (Perl :2166-2217) -------------------------
    def stop(self, **p) -> Optional[str]:
        """``sub stop`` (:2166)."""
        return self.resolve("Stop", **p)

    def play(self, index: Optional[int] = None, seekdata=None,
             fade_in: Optional[float] = None, **p) -> Optional[str]:
        """``sub play`` (:2168-2173)."""
        self.consecutive_errors = 0
        if fade_in and fade_in > 0:
            self.fade_in = fade_in
        return self.resolve("Play", index=index, seekdata=seekdata, **p)

    def skip(self, **p) -> Optional[str]:
        """``sub skip`` (:2175-2179)."""
        self.consecutive_errors = 0
        return self.resolve("Skip", **p)

    def pause(self, **p) -> Optional[str]:
        """``sub pause`` (:2182-2200)."""
        return self.resolve("Pause", **p)

    def resume(self, fade_in: Optional[float] = None, **p) -> Optional[str]:
        """``sub resume`` (:2203-2207)."""
        if fade_in and fade_in > 0:
            self.fade_in = fade_in
        return self.resolve("Resume", **p)

    def flush(self, **p) -> Optional[str]:
        """``sub flush`` (:2209-2212)."""
        return self.resolve("Flush", **p)

    def jump_to_time(self, newtime, restart_if_no_seek=None, **p):
        """``sub jumpToTime`` (:2214-2217)."""
        return self.resolve("JumpToTime", newtime=newtime,
                            restart_if_no_seek=restart_if_no_seek, **p)

    # -- PlayerNotificationHandler interface (Perl :2223-2369) -----------
    def player_stopped(self, **p):
        """``playerStopped`` (:2223-2248) — track end → ``Stopped``."""
        return self.resolve("Stopped", **p)

    def player_track_started(self, **p) -> Optional[str]:
        """``playerTrackStarted`` (:2250-2266) — ``Started`` (→ mode play)."""
        return self.resolve("Started", **p)

    def player_ready_to_stream(self, **p):
        """``playerReadyToStream`` (:2268-2284)."""
        return self.resolve("ReadyToStream", **p)

    def player_output_underrun(self, **p):
        """``playerOutputUnderrun`` (:2286-2296) — ``OutputUnderrun`` → rebuffer."""
        return self.resolve("OutputUnderrun", **p)

    def player_streaming_failed(self, **p) -> Optional[str]:
        """``playerStreamingFailed`` (:2298-2326) — ``StreamingFailed``."""
        return self.resolve("StreamingFailed", **p)

    def player_buffer_ready(self, **p):
        """``playerBufferReady`` (:2328-2334)."""
        return self.resolve("BufferReady", **p)

    def player_end_of_stream(self, **p):
        """``playerEndOfStream`` (:2336-2346)."""
        return self.resolve("EndOfStream", **p)

    def local_end_of_stream(self, **p):
        """``localEndOfStream`` (:1893-1898)."""
        return self.resolve("LocalEndOfStream", **p)

    def player_status_heartbeat(self, **p):
        """``playerStatusHeartbeat`` (:2363-2369)."""
        return self.resolve("StatusHeartbeat", **p)

    def player_reconnect(self, bytes_received=None, **p):
        """``playerReconnect`` (:2124-2134) — ``ContinuePlay``."""
        return self.resolve("ContinuePlay", bytes_received=bytes_received, **p)

    def __repr__(self) -> str:
        return f"<StreamingController {self.master_id} {self.state()}>"


def _is_relative(newtime: Any) -> bool:
    """``$newtime =~ /^[\\+\\-]/`` (StreamingController.pm:1078, :1117)."""
    return isinstance(newtime, str) and newtime[:1] in "+-"


def _num(value: Any) -> float:
    """Perl numeric context (``$newtime += $oldtime``, :1081, :1120).

    ``"+10"`` → 10, ``"-5"`` → -5, anything not numeric → 0 (Perl warns and
    uses 0). Perl does NOT strip whitespace-plus-garbage here differently, so a
    plain ``float()`` with a 0-fallback is the faithful port.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Per-player registry + thin helpers used by networking/protocol.py
# ---------------------------------------------------------------------------

_controllers: dict[str, StreamingController] = {}


def _normalize(mac: str) -> str:
    return (mac or "").upper().replace(":", "")


def get_controller(mac: str, hooks: StreamingHooks | None = None
                   ) -> StreamingController:
    """Return (creating if needed) the controller for ``mac``.

    A fresh controller gets :func:`default_hooks` — Perl's own side effects,
    of which ``playback_started`` (the ``playlist newsong`` notification) is
    the only one that needs no player socket. An explicit ``hooks`` argument
    replaces them (that is how the unit tests observe the transitions).
    """
    key = _normalize(mac)
    ctl = _controllers.get(key)
    if ctl is None:
        ctl = StreamingController(master_id=key,
                                  hooks=hooks if hooks is not None else default_hooks())
        ctl.all_players = [key]
        ctl.players = [key]
        _controllers[key] = ctl
    elif hooks is not None:
        ctl.hooks = hooks
    return ctl


def controller_for(mac: str) -> Optional[StreamingController]:
    """The controller already registered for ``mac`` — creates nothing."""
    return _controllers.get(_normalize(mac))


def reported_playmode(mac: str) -> Optional[str]:
    """Perl ``Slim::Player::Source::playmode($client)`` — the status ``mode``.

    ``Queries.pm:4081`` reports ``mode`` as
    ``Slim::Player::Source::playmode($client)``; called without a new mode
    that is ``_returnPlayMode`` (``Slim/Player/Source.pm:55-64``)::

        return 'stop' if !$_[1]->power();
        my $returnedmode = $controller->isStopped ? 'stop'
                            : $controller->isPaused ? 'pause' : 'play';

    with ``isStopped`` = ``playingState == STOPPED && streamingState == IDLE``
    (``StreamingController.pm:1681-1683``). A stream that is only
    BUFFERING/STREAMING — the state ``_Stream`` sets together with the strm
    frame (:1350-1352) — therefore already reads ``play``; the player's STMs
    is not needed (:1676-1695).

    ``None`` when no controller is registered for ``mac`` yet (the caller then
    keeps its own value).
    """
    ctl = controller_for(mac)
    return ctl.reported_mode() if ctl is not None else None


def reset(mac: Optional[str] = None) -> None:
    """Drop the controller for ``mac`` (or all) — for tests and disconnects."""
    if mac is None:
        _controllers.clear()
    else:
        _controllers.pop(_normalize(mac), None)


def apply_event(mac: str, event: str, **params: Any) -> Optional[str]:
    """Feed ``event`` to ``mac``'s controller and return the status mode.

    ``networking/protocol.py`` calls this from the SlimProto paths it used to
    handle with ad-hoc ``player.mode = "…"`` assignments, so the Perl state
    table is the single source of the transition. The returned value is the
    ``EVENT_MODE`` for player-status events (``None`` elsewhere); callers keep
    their previous constant as a fallback so behaviour is unchanged.
    """
    return get_controller(mac).resolve(event, **params)


def note_strm_sent(mac: str, track_id: Any = None) -> None:
    """Our ``strm 's'`` frame went out → Perl ``_Stream`` tail (:1350-1352).

    ``_Stream`` is an *action*, not an event: Perl reaches it via
    ``NextTrackReady``/``EndOfStream``/``JumpToTime``. We send the frame
    ourselves, so we apply exactly the state tail Perl applies once
    ``$player->play`` succeeded — ``nextTrack`` cleared, playing state
    BUFFERING (only from STOPPED), streaming state STREAMING. The song goes on
    the head of the queue (``unshift``, :1219) so ``streamingSong()`` is right
    for the following ``Started``/``Stopped`` events.
    """
    ctl = get_controller(mac)
    song = track_id
    if song is None:
        song = ctl.next_track if ctl.next_track is not None else ctl.playing_song()
    ctl.next_track = None                                   # :1350
    if song is not None and (not ctl.songqueue or ctl.songqueue[0] is not song):
        ctl.songqueue.insert(0, song)                       # :1219
    if ctl.playing_state == PlayingState.STOPPED:           # :1351
        ctl._set_playing_state(PlayingState.BUFFERING)      # noqa: SLF001
    ctl._set_streaming_state(StreamingState.STREAMING)      # :1352  # noqa: SLF001
    ctl._hook("playback_prepared", song=song)               # :1326 $player->play


def note_stop_sent(mac: str) -> None:
    """Our ``strm 'q'`` frame went out → Perl ``_Stop`` (:585-620)."""
    get_controller(mac).resolve("Stop", suppress_notifications=True)


def note_flush_sent(mac: str) -> None:
    """Our ``strm 'f'`` frame went out → Perl ``_FlushGetNext`` (:990-1001)."""
    get_controller(mac).resolve("Flush", if_more_tracks=True)


def note_pause_sent(mac: str) -> None:
    """Our ``strm 'p'`` frame went out → Perl ``_Pause`` (:1552-1586)."""
    get_controller(mac).resolve("Pause")


def note_unpause_sent(mac: str) -> None:
    """Our ``strm 'u'`` frame went out → Perl ``_Resume`` (:1620-1656)."""
    get_controller(mac).resolve("Resume")


def note_skip_ahead_sent(mac: str, interval_ms: int = 0) -> None:
    """``strm 'a'`` is Perl's ``skipAhead`` (Squeezebox2.pm:1120-1127).

    It is a *sync* correction (drop N ms of decoded audio), NOT the ``Skip``
    controller event (which advances the song). The controller state is
    deliberately untouched — documented so nobody wires it to ``Skip``.
    """
    logger.debug("strm 'a' skipAhead %dms for %s (no state change)",
                 interval_ms, mac)
