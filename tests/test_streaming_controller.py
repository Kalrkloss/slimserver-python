"""StreamingController-Zustandsmaschine (Perl ``Slim/Player/StreamingController.pm``).

Jeder Test nennt die Perl-Fundstelle, die den erwarteten Übergang belegt.
Perl-Referenz ist das einzige Vorbild — nichts ist geraten; wo die Mechanik
einen echten Client/Codec bräuchte, steht das im Modul als UNKLAR
(``streaming.py``, Modul-Docstring).

Die Zustandsnamen und der Sprungtabelle stammen aus
``StreamingController.pm``:

    :26-32   streamingState  IDLE/STREAMING/STREAMOUT/TRACKWAIT
    :35-42   playingState    STOPPED/BUFFERING/WAITING_TO_SYNC/PLAYING/PAUSED
    :101-108 @ValidStates (Gültigkeitsmatrix)
    :110-245 %stateTable[event][playingState][streamingState] → Aktion
    :249-311 _eventAction (Aufruf + Invalid-Erkennung)
"""

from __future__ import annotations

import struct

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player import streaming
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.player.streaming import (
    FADEVOLUME,
    STATE_TABLE,
    TRANSITION_CROSSFADE_IMMEDIATE,
    TRANSITION_FADEIN,
    TRANSITION_NONE,
    VALID_STATES,
    PlayingState,
    StreamingController,
    StreamingHooks,
    StreamingState,
    apply_event,
    autostart_value,
    get_controller,
    note_strm_sent,
    select_transition,
)

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"
TRACK = 47111
TRACK2 = 47112


@pytest.fixture(autouse=True)
def _fresh_controllers():
    """Kein Controller-Zustand darf zwischen Tests überleben."""
    streaming.reset()
    yield
    streaming.reset()


# ---------------------------------------------------------------------------
# Test-Doubles: nur die Hooks, die ein Übergang wirklich abfragt
# ---------------------------------------------------------------------------

class _Harness:
    """Controller + Recorder für die Perl-Hooks (kein I/O, kein Codec)."""

    def __init__(self, **hooks):
        self.calls = []
        self.ready = None
        self.seek_time = None
        self.seek_enabled = hooks.get("seek", True)
        recorded = {
            name: (lambda _n=name, **kw: self.calls.append((_n, kw)))
            for name in (
                "stop_client", "close_stream", "flush_player", "pause_player",
                "rebuffer_player", "resume_player", "start_playback",
                "playback_started", "notify_stopped", "sync_start",
                "error_opening", "refresh_song_status", "mark_song_finished",
                "mark_song_ready", "show_trackwait", "load_song",
            )
        }
        recorded["playlist_count"] = lambda: hooks.get("playlist_count", 3)
        recorded["next_song_index"] = lambda error_index=None: hooks.get(
            "next_index", 0)
        recorded["make_song"] = lambda index: {"index": index}
        recorded["get_next_song"] = self._get_next_song
        recorded["playing_elapsed"] = lambda: hooks.get("elapsed", 30.0)
        recorded["can_do_action"] = lambda action="": hooks.get("allowed", True)
        recorded["all_buffer_ready"] = lambda: hooks.get("buffer_ready", True)
        recorded["all_ready_to_stream"] = lambda: hooks.get("ready", True)
        recorded["can_seek"] = lambda: hooks.get("can_seek", True)
        recorded["song_duration"] = lambda: hooks.get("duration", 180.0)
        recorded["open_stream"] = lambda song, seekdata=None: song
        recorded["reports_track_start"] = lambda: True
        recorded["get_seekdata"] = self._get_seekdata
        # Der Controller muss im Modul-Register stehen: protocol.py adressiert
        # ihn über die MAC (get_controller/apply_event).
        self.ctl = get_controller("AA:BB", StreamingHooks(**recorded))

    def _get_seekdata(self, newtime):
        self.seek_time = newtime
        return {"newtime": newtime} if self.seek_enabled else None

    def _get_next_song(self, song, on_ready, on_error):
        self.ready = on_ready

    def fire_ready(self):
        assert callable(self.ready), "Perl hätte getNextSong-Callback registriert"
        self.ready()

    def saw(self, name: str) -> bool:
        return any(n == name for n, _ in self.calls)


def _playing(ctl: StreamingController, state=StreamingState.STREAMING,
             song=TRACK) -> StreamingController:
    """Bringt den Controller in den Perl-Startzustand PLAYING-STREAMING."""
    ctl.songqueue = [song]
    ctl._set_playing_state(PlayingState.PLAYING)
    ctl._set_streaming_state(state)
    return ctl


# ---------------------------------------------------------------------------
# Tabellen-Parität
# ---------------------------------------------------------------------------

def test_states_match_perl_names_and_values():
    """``@StreamingStateName``/``@PlayingStateName`` (StreamingController.pm:31, :41)."""
    assert [int(s) for s in StreamingState] == [0, 1, 2, 3]
    assert [s.name for s in StreamingState] == [
        "IDLE", "STREAMING", "STREAMOUT", "TRACKWAIT"]
    assert [int(s) for s in PlayingState] == [0, 1, 2, 3, 4]
    assert [s.name for s in PlayingState] == [
        "STOPPED", "BUFFERING", "WAITING_TO_SYNC", "PLAYING", "PAUSED"]


def test_valid_states_matrix_is_perl_verbatim():
    """``@ValidStates`` (StreamingController.pm:101-108) — Zeilen STOPPED…PAUSED."""
    assert VALID_STATES == (
        (1, 0, 0, 1),   # STOPPED          :103
        (0, 1, 1, 0),   # BUFFERING        :104
        (0, 1, 1, 0),   # WAITING_TO_SYNC  :105
        (1, 1, 1, 1),   # PLAYING          :106
        (1, 1, 1, 1),   # PAUSED           :107
    )


@pytest.mark.parametrize("event,playing,streaming,action", [
    # Stop: PLAYING/STREAMING -> _Stop (:116); STOPPED/IDLE -> _NoOp (:113)
    ("Stop", PlayingState.PLAYING, StreamingState.STREAMING, "_Stop"),
    ("Stop", PlayingState.STOPPED, StreamingState.IDLE, "_NoOp"),
    # Resume: PLAYING ist ungültig (:144), PAUSED/STREAMING -> _Resume (:145)
    ("Resume", PlayingState.PLAYING, StreamingState.STREAMING, "_Invalid"),
    ("Resume", PlayingState.PAUSED, StreamingState.STREAMING, "_Resume"),
    # Skip: PLAYING/STREAMING -> _Skip (:158)
    ("Skip", PlayingState.PLAYING, StreamingState.STREAMING, "_Skip"),
    # OutputUnderrun: PLAYING/STREAMING -> _Rebuffer (:235)
    ("OutputUnderrun", PlayingState.PLAYING, StreamingState.STREAMING, "_Rebuffer"),
    # Started: BUFFERING/STREAMING -> _Playing (:198)
    ("Started", PlayingState.BUFFERING, StreamingState.STREAMING, "_Playing"),
    # NextTrackReady: STOPPED/TRACKWAIT -> _Stream (:169)
    ("NextTrackReady", PlayingState.STOPPED, StreamingState.TRACKWAIT, "_Stream"),
    # BufferReady: BUFFERING/STREAMING -> _WaitToSync (:191)
    ("BufferReady", PlayingState.BUFFERING, StreamingState.STREAMING, "_WaitToSync"),
])
def test_state_table_cells_match_perl(event, playing, streaming, action):
    """``%stateTable`` (StreamingController.pm:110-245) — Stichproben je Zeile."""
    assert STATE_TABLE[event][int(playing)][int(streaming)] == action


# ---------------------------------------------------------------------------
# Frischer Start / Wechsel / Stop / Skip
# ---------------------------------------------------------------------------

def test_fresh_start_play_trackwait_streaming_playing():
    """Frischer Start: ``Play`` in STOPPED-IDLE → ``_StopGetNext``
    (StreamingController.pm:120) → ``_Stop`` + ``_getNextTrack`` (``:969-973``),
    das den Streaming-State auf TRACKWAIT setzt (:681). Sobald der Song-Track
    bereit ist, läuft ``NextTrackReady`` → ``_Stream`` (:169) → playing
    BUFFERING + streaming STREAMING (:1351-1352); der Player-Start ``Started``
    führt mit ``_Playing`` nach PLAYING (:198)."""
    h = _Harness(playlist_count=3, next_index=0)
    ctl = h.ctl
    assert ctl.state() == "STOPPED-IDLE"

    assert ctl.play() is None
    assert ctl.streaming_state == StreamingState.TRACKWAIT
    assert ctl.playing_state == PlayingState.STOPPED

    h.fire_ready()                       # $song->getNextSong callback (:704)
    assert ctl.streaming_state == StreamingState.STREAMING
    assert ctl.playing_state == PlayingState.BUFFERING   # :1351

    assert ctl.player_track_started() == "play"          # EVENT_MODE
    assert ctl.playing_state == PlayingState.PLAYING      # :198 _Playing
    assert ctl.streaming_state == StreamingState.STREAMING
    assert ctl.buffering() == 0                           # :1698-1702


def test_switch_stream_keeps_streaming_and_waits_for_start():
    """Wechsel: während STREAMING setzt ``_Stream`` den playing-State nicht
    erneut (Perl setzt BUFFERING nur aus STOPPED, StreamingController.pm:1351)
    und der State bleibt STREAMING (:1352)."""
    h = _Harness()
    ctl = _playing(h.ctl)
    note_strm_sent("AA:BB", TRACK2)          # der zweite strm-Frame

    assert ctl.streaming_state == StreamingState.STREAMING
    assert ctl.playing_state == PlayingState.PLAYING     # nicht erneut BUFFERING
    assert ctl.streaming_song() == TRACK2


def test_stop_reaches_stopped_idle_and_notifies():
    """``Stop`` in PLAYING-STREAMING → ``_Stop`` (StreamingController.pm:116):
    playing STOPPED + streaming IDLE (:617-618), Player-Stop über ``_stopClient``
    (:599-601/:622-627) und ``_notifyStopped`` (:406-413)."""
    h = _Harness()
    ctl = _playing(h.ctl)
    assert ctl.stop() is None            # 'Stop' ist kein Status-Modus-Event
    assert ctl.state() == "STOPPED-IDLE"
    assert ctl.is_stopped() is True
    assert h.saw("stop_client") and h.saw("notify_stopped")


def test_skip_from_playing_stops_then_waits_for_the_next_track():
    """``Skip`` in PLAYING-STREAMING → ``_Skip`` (:158) prüft zuerst den
    Protokoll-Handler (:982) und geht dann über ``_StopGetNext`` (:969-973) →
    ``_Stop`` + ``_getNextTrack`` → TRACKWAIT (:681)."""
    h = _Harness(playlist_count=3, next_index=1)
    ctl = _playing(h.ctl)
    assert ctl.skip() is None
    assert ctl.playing_state == PlayingState.STOPPED
    assert ctl.streaming_state == StreamingState.TRACKWAIT
    assert h.saw("stop_client")


def test_skip_is_refused_when_the_protocol_handler_vetoes():
    """``_Skip`` bricht ab, wenn der Handler ``canDoAction(..., 'stop')``
    verneint (StreamingController.pm:982-985) — kein Zustandswechsel."""
    h = _Harness(allowed=False)
    ctl = _playing(h.ctl)
    assert ctl.skip() is None
    assert ctl.state() == "PLAYING-STREAMING"


def test_pause_event_in_stopped_idle_is_invalid():
    """``Pause`` in STOPPED-IDLE → ``_Invalid`` (StreamingController.pm:134);
    der Zustand bleibt unverändert (``_Invalid`` :322-327)."""
    ctl = StreamingController("AA:BB")
    ctl.pause()
    assert ctl.state() == "STOPPED-IDLE"


# ---------------------------------------------------------------------------
# Rebuffer
# ---------------------------------------------------------------------------

def test_output_underrun_sets_rebuffering_and_buffering_returns_2():
    """``OutputUnderrun`` in PLAYING-STREAMING → ``_Rebuffer`` (:235):
    playing BUFFERING + ``rebuffering = 1`` (:1662-1663); ``buffering()``
    liefert deshalb 2 statt 1 (:1698-1702)."""
    h = _Harness()
    ctl = _playing(h.ctl)
    ctl.player_output_underrun()
    assert ctl.playing_state == PlayingState.BUFFERING
    assert ctl.rebuffering == 1
    assert ctl.buffering() == 2


def test_track_start_while_rebuffering_keeps_the_buffering_state():
    """Bug 10681 (StreamingController.pm:342-348): ein Track-Start während
    eines Rebuffer-Rennens darf den playing-State NICHT umschalten — ``_Playing``
    lässt BUFFERING stehen, wenn ``rebuffering`` gesetzt ist."""
    h = _Harness()
    ctl = _playing(h.ctl)
    ctl.player_output_underrun()
    assert ctl.player_track_started() == "play"      # STATUS meldet play
    assert ctl.playing_state == PlayingState.BUFFERING
    assert ctl.rebuffering == 1


def test_buffer_ready_when_rebuffering_resumes_instead_of_starting():
    """``BufferReady`` → ``_WaitToSync`` (:191) → ``_StartIfReady`` (:192,
    :1519-1534); ``_Start`` sieht ``rebuffering`` und ruft ``_Resume`` statt
    eines Neustarts (:1406/:1424-1425) → PLAYING (:1629)."""
    h = _Harness(buffer_ready=True)
    ctl = _playing(h.ctl)
    ctl.player_output_underrun()
    ctl.player_buffer_ready()
    assert ctl.playing_state == PlayingState.PLAYING
    assert ctl.rebuffering == 0          # _setPlayingState :2432
    assert h.saw("resume_player")


# ---------------------------------------------------------------------------
# TRACKWAIT
# ---------------------------------------------------------------------------

def test_trackwait_is_the_state_while_the_next_song_is_built():
    """``_getNextTrack`` setzt TRACKWAIT VOR dem asynchronen Song-Aufbau
    (StreamingController.pm:681) — solange ``getNextSong`` noch läuft, ist das
    der Zustand; ``show_trackwait`` läuft in STOPPED-TRACKWAIT (:715-719)."""
    h = _Harness(playlist_count=3, next_index=0)
    h.ctl.play()
    assert h.ctl.streaming_state == StreamingState.TRACKWAIT
    assert callable(h.ready)                     # getNextSong-Callback offen
    assert h.saw("show_trackwait")


def test_next_track_error_goes_through_nextifmore():
    """``_getNextTrack`` kann den nächsten Song nicht bauen → TRACKWAIT kurz
    gesetzt (:675), ``_nextTrackError`` (:755-771) → ``NextTrackError``; in
    STOPPED-TRACKWAIT ist die Aktion ``_NextIfMore`` (:176) → streaming IDLE
    (:1005) und der Folgeversuch steigt am Playlist-Ende aus (:663-664)."""
    hooks = StreamingHooks(make_song=lambda index: None,
                           playlist_count=lambda: 0)
    ctl = StreamingController("AA:BB", hooks)
    ctl.get_next_track(index=0)
    assert ctl.consecutive_errors == 1
    assert ctl.streaming_state == StreamingState.IDLE


def test_trackwait_status_is_only_shown_while_stopped():
    """``_showTrackwaitStatus`` greift nur in STOPPED-TRACKWAIT
    (StreamingController.pm:715-737) — unser Hook wird also beim frischen
    ``play`` gerufen, in PLAYING aber nicht."""
    h = _Harness(playlist_count=3, next_index=0)
    h.ctl.play()
    assert h.saw("show_trackwait")
    h.calls.clear()
    _playing(h.ctl)
    h.ctl.get_next_track(index=0)
    assert not h.saw("show_trackwait")


# ---------------------------------------------------------------------------
# Pause / Resume / Jump (skipAhead/jumpToTime)
# ---------------------------------------------------------------------------

def test_pause_saves_resume_time_and_resume_clears_it():
    """``Pause`` → ``_Pause``: resumeTime = playingSongElapsed (:1555),
    playing PAUSED (:1556). ``Resume`` → ``_Resume``: PLAYING (:1629),
    resumeTime = undef (:1654). ``playingSongElapsed`` liefert pausiert diesen
    Wert zurück (:1719-1724)."""
    h = _Harness(elapsed=73.5)
    ctl = _playing(h.ctl)
    assert ctl.pause() == "pause"
    assert ctl.playing_state == PlayingState.PAUSED
    assert ctl.resume_time == pytest.approx(73.5)

    assert ctl.resume() == "play"
    assert ctl.playing_state == PlayingState.PLAYING
    assert ctl.resume_time is None


def test_jump_to_time_zero_restarts_the_track():
    """``JumpToTime`` mit ``newtime == 0`` = Nutzer-Restart:
    ``_JumpToTime`` prüft ``canDoAction(..., 'rew')`` (:1106), stoppt ohne
    Notification (:1111) und streamt neu (:1113) → BUFFERING/STREAMING (:1351)."""
    h = _Harness(duration=180.0)
    ctl = _playing(h.ctl)
    ctl.jump_to_time(0)
    assert ctl.playing_state == PlayingState.BUFFERING
    assert ctl.streaming_state == StreamingState.STREAMING
    assert ctl.fade_in is None


def test_jump_to_time_relative_uses_playing_song_elapsed():
    """``$newtime =~ /^[\\+\\-]/`` → ``newtime += playingSongElapsed``
    (StreamingController.pm:1117-1125); die absolute Zeit geht an den
    Seek-Pfad (:1132-1141)."""
    h = _Harness(elapsed=30.0, duration=180.0)
    ctl = _playing(h.ctl)
    ctl.jump_to_time("+10")
    assert h.seek_time == pytest.approx(40.0)      # 30 + 10 (…:1120)
    # nach dem Seek wird gestoppt (:1139) und neu gestreamt (:1141)
    assert ctl.streaming_state == StreamingState.STREAMING
    assert ctl.playing_state == PlayingState.BUFFERING


def test_jump_paused_only_moves_the_resume_point():
    """``JumpToTime`` im Zustand PAUSED → ``_JumpPaused`` (:161-167, :1051-1090):
    ``resumeTime`` wird gesetzt und das Streaming verworfen
    (``_pauseStreaming`` → IDLE, :470)."""
    h = _Harness(duration=180.0, can_seek=True)
    ctl = _playing(h.ctl)
    ctl.pause()
    ctl.jump_to_time(42)
    assert ctl.playing_state == PlayingState.PAUSED
    assert ctl.streaming_state == StreamingState.IDLE
    assert ctl.resume_time == pytest.approx(42)


def test_jump_paused_restart_without_seek_stops():
    """``_JumpPaused``: kann der Song nicht seeken und ist ``restartIfNoSeek``
    gesetzt, wird gestoppt (StreamingController.pm:1060-1066)."""
    h = _Harness(can_seek=False)
    ctl = _playing(h.ctl)
    ctl.pause()
    ctl.jump_to_time(42, restart_if_no_seek=1)
    assert ctl.state() == "STOPPED-IDLE"


# ---------------------------------------------------------------------------
# Gapless-Grundlagen (ohne echten Übergang — der ist UNKLAR)
# ---------------------------------------------------------------------------

def test_gapless_groundwork_transition_constants_match_perl():
    """``TRANSITION_*`` (Squeezebox.pm:36-41) und die Pref-Defaults
    ``transitionType => 0`` / ``transitionDuration => 10`` (Squeezebox2.pm:44-45)."""
    assert TRANSITION_NONE == 0
    assert TRANSITION_FADEIN == 2
    assert TRANSITION_CROSSFADE_IMMEDIATE == 5
    # fadeIn hat Vorrang vor crossfade (Squeezebox.pm:930-941)
    assert select_transition({"fadeIn": 3}, {}) == (TRANSITION_FADEIN, 3)
    assert select_transition({}, {}) == (0, 10)
    assert select_transition({}, {"transitionType": 5,
                                  "transitionDuration": 7}) == (5, 7)


def test_autostart_byte_matches_perl_stream_s():
    """``my $autostart = $params->{'paused'} ? 0 : 1`` (Squeezebox.pm:570);
    Direktstreams addieren 2 (Squeezebox.pm:796)."""
    assert autostart_value(paused=True) == 0
    assert autostart_value(paused=False) == 1
    assert streaming.AUTOSTART_DIRECT_FLAG == 2


def test_skip_ahead_is_not_the_controller_skip_event():
    """``strm 'a'`` ist ``skipAhead`` (Squeezebox2.pm:1120-1127) — eine
    Sync-Korrektur, KEIN ``Skip``-Event. Der Controller-Zustand bleibt."""
    ctl = _playing(get_controller(MAC))
    streaming.note_skip_ahead_sent(MAC, 250)
    assert ctl.state() == "PLAYING-STREAMING"


# ---------------------------------------------------------------------------
# Verdrahtung in networking/protocol.py (echter SlimProto-Pfad)
# ---------------------------------------------------------------------------

class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass

    def is_closing(self):
        return False


def _stat_client(monkeypatch, player):
    """PlayerManager-Singleton + Client wie in test_pause_resume."""
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    try:
        import lyrion.web.cometd as cometd_mod
        monkeypatch.setattr(cometd_mod, "get_manager", lambda: None)
    except Exception:  # pragma: no cover
        pass
    client = SlimProtoClient.__new__(SlimProtoClient)
    client._player_writers = {MAC_CLEAN: _FakeWriter()}
    return client


def _stat_frame(event: str, elapsed_seconds: int = 0) -> bytes:
    """53-Byte-STAT; elapsed auf Offset 37 (Perl-Layout)."""
    buf = bytearray(53)
    buf[0:4] = event.encode("ascii")
    buf[37:41] = struct.pack(">I", elapsed_seconds)
    return bytes(buf)


def test_protocol_stat_STMs_drives_the_machine_to_playing(monkeypatch):
    """Verdrahtung: ``_handle_stat_frame`` speist ``Started`` in die Maschine
    (Perl ``playerTrackStarted`` → ``Started``, StreamingController.pm:2250-2266).
    Nach unserem strm-Frame (``_Stream``-Tail :1350-1352) muss der Controller
    PLAYING erreichen und der Status-Modus 'play' bleiben."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    player.mode = "stop"
    client = _stat_client(monkeypatch, player)

    note_strm_sent(MAC, TRACK)                 # was _stream_track_to_player tut
    assert get_controller(MAC).state() == "BUFFERING-STREAMING"

    client._handle_stat_frame(MAC, _stat_frame("STMs"))

    assert player.mode == "play"
    assert get_controller(MAC).playing_state == PlayingState.PLAYING
    assert get_controller(MAC).streaming_state == StreamingState.STREAMING


def test_protocol_stat_STMp_feeds_pause_and_keeps_the_guard(monkeypatch):
    """``STMp`` → Perl ``Pause`` (:133-139 Zeile PLAYING) und der Status bleibt
    'pause' — der strm-Guard bleibt (Pause verliert den Stream nicht,
    Squeezebox2.pm:1104-1110)."""
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    player.mode = "play"
    player.strm_sent_track = TRACK
    client = _stat_client(monkeypatch, player)
    note_strm_sent(MAC, TRACK)

    # echte Reihenfolge: erst der Track-Start, dann der Pause-Ack
    client._handle_stat_frame(MAC, _stat_frame("STMs"))
    assert get_controller(MAC).playing_state == PlayingState.PLAYING

    client._handle_stat_frame(MAC, _stat_frame("STMp"))

    assert player.mode == "pause"
    assert player.strm_sent_track == TRACK
    assert get_controller(MAC).playing_state == PlayingState.PAUSED


def test_protocol_stop_frame_feeds_the_stop_event(monkeypatch):
    """``send_stop_to_player`` (strm 'q') speist ``Stop`` → STOPPED-IDLE
    (StreamingController.pm:116/:585-620). Der Status-Modus bleibt der
    bisherige Rückfallwert."""
    import asyncio

    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    client = SlimProtoClient.__new__(SlimProtoClient)
    client._player_writers = {MAC_CLEAN: _FakeWriter()}
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm

    note_strm_sent(MAC, TRACK)
    assert get_controller(MAC).state() == "BUFFERING-STREAMING"

    assert asyncio.run(client.send_stop_to_player(MAC)) is True
    assert get_controller(MAC).is_stopped() is True


def test_networking_helpers_never_raise_on_a_broken_controller(monkeypatch):
    """Die Verdrahtung darf den Server nie stören: ein Fehler in der Maschine
    wird geloggt und der bisherige Modus zurückgegeben."""
    from lyrion.networking import protocol as protocol_mod

    def _boom(*a, **k):
        raise RuntimeError("controller exploded")

    # direkter Aufruf mit einem mac, dessen apply_event absichtlich scheitert
    monkeypatch.setattr(streaming, "apply_event", _boom)
    assert protocol_mod._streaming_mode(MAC, "Started", "play") == "play"
    protocol_mod._streaming_note(MAC, "Stopped")         # darf nicht werfen


# ---------------------------------------------------------------------------
# UNKLAR-Marker
# ---------------------------------------------------------------------------

def test_unklar_markers_are_documented():
    """Was einen echten Client/Codec bräuchte, ist im Modul als UNKLAR belegt:
    Buffer-Füllstände, ``_CheckPaused``/``_CheckSync`` und der echte
    Gapless-Übergang; ``STREAMOUTPUT_*`` und ``setDrainType`` existieren in der
    Perl-Referenz gar nicht (grep-Läufe, siehe Modul-Docstring)."""
    doc = streaming.__doc__ or ""
    assert "UNKLAR" in doc
    assert "STREAMOUTPUT" in doc and "setDrainType" in doc
    assert "_CheckPaused" in doc and "_CheckSync" in doc
    assert "trackSampleRateMatch" in doc


def test_unknown_event_is_rejected():
    """Ein Event außerhalb ``%stateTable`` ist ein Programmierfehler, kein
    stiller No-Op."""
    with pytest.raises(KeyError):
        StreamingController("AA:BB").resolve("NotARealEvent")


def test_fadevolume_constant_matches_perl():
    """``use constant FADEVOLUME => 0.3125`` (StreamingController.pm:44)."""
    assert FADEVOLUME == pytest.approx(0.3125)


def test_apply_event_registry_normalizes_mac():
    """``apply_event``/``get_controller`` normalisieren die MAC wie der restliche
    Server (Kleinschreibung + Doppelpunkte dürfen keine zweite Instanz bauen)."""
    a = apply_event("aa:bb:cc:dd:ee:ff", "Stop")
    b = apply_event("AABBCCDDEEFF", "Stop")
    assert a is None and b is None
    assert get_controller("AA:BB:CC:DD:EE:FF") is get_controller("AABBCCDDEEFF")
