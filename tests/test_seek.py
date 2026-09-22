"""Seek (``time <n>``) — Perls ``gototime``/``jumpToTime`` statt ``strm 'a'``.

Perl behandelt ``time <n>`` als **Neustart der Quelle an der Position**:

* ``timeCommand`` → ``Slim::Player::Source::gototime``
  (``Slim/Control/Commands.pm:3069-3086``, ``Source.pm:216-221``)
  → ``controller->jumpToTime`` (``StreamingController.pm:2214-2217``)
  → ``_JumpToTime`` (``StreamingController.pm:1092-1141``);
* ``_Stop`` + ``_Stream`` mit ``seekdata`` = ``{timeOffset => newtime}``
  (``File.pm:372-380``) → ``File.pm:194-202`` sucht die Datei an dem
  Byte-Offset und setzt ``$song->startOffset(timeOffset)``;
* die Songuhr zaehlt von dort weiter: ``playingSongElapsed`` =
  ``startOffset + player elapsed`` (``StreamingController.pm:1719-1743``)
  → ``Source::songTime`` (``Source.pm:51-53``) → Status ``time``
  (``Queries.pm:4093-4094``).

``strm 'a'`` ist Perls ``skipAhead`` (``Squeezebox2.pm:1120-1127``) und wird
nur von der Sync-Korrektur gesendet (``_CheckSync``,
``StreamingController.pm:566-568``) — der Player wirft damit ein paar
Sekunden aus seinem Ausgabepuffer weg, die Position springt nicht.

Die Pause-/Resume-Strecke selbst ist ``strm 'p'`` / ``strm 'u'``
(Squeezebox.pm:197-204, Squeezebox2.pm:1104-1110) — siehe
``tests/test_pause_resume.py``.
"""

import asyncio
import struct

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "02:11:22:33:44:55"


class _FakeWriter:
    def __init__(self):
        self.written = b""

    def is_closing(self):
        return False

    def write(self, data):
        self.written += data

    async def drain(self):
        pass


class _FakeHandler:
    def __init__(self):
        self.started = []   # (track_id, start_seconds) via send_strm_to_player
        self.skips = []     # seconds sent via send_skip_to_player
        self.stopped = []
        self.paused = []
        self.unpaused = []
        self.streams = []   # URLs via send_remote_stream

    async def send_strm_to_player(self, mac, track_id, start_seconds=0.0):
        self.started.append((track_id, float(start_seconds or 0.0)))
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        self.streams.append(url)
        return True

    async def send_stop_to_player(self, mac):
        self.stopped.append(mac)
        return True

    async def send_pause_to_player(self, mac, pause_ms=0):
        self.paused.append(mac)
        return True

    async def send_unpause_to_player(self, mac):
        self.unpaused.append(mac)
        return True

    async def send_skip_to_player(self, mac, seconds):
        self.skips.append(seconds)
        return True


def _fresh_pm():
    pm = object.__new__(PlayerManager)
    pm.players = {}
    pm._protocol_handler = _FakeHandler()
    return pm


def _player(pm, **kw):
    p = PlayerState(mac=MAC, name="Test", ip="127.0.0.1", port=0)
    p.power = True
    p.mode = "play"
    p.playlist = [1]
    p.playlist_position = 0
    p.current_track_id = 1
    p.elapsed = 10.0
    p.duration = 200.0
    for k, v in kw.items():
        setattr(p, k, v)
    pm.players[p.mac] = p
    return p


# ── Dauer der laufenden Nummer: Perls ``$track->secs`` aus der DB ─────────
#
# ``seek_to`` holt die Dauer NICHT mehr aus dem Player-Zustand, sondern aus
# der DB-Zeile des laufenden Eintrags (``$song->duration()``, Song.pm:809-815
# → ``Info::getDuration($song->currentTrack()->url)``, Info.pm:382-390).
# Diese Tests modellieren die Zeile, statt die echte Bibliothek zu lesen:
# ``_FAKE_DB['duration']`` ist das LENGTH-Feld des Titels, ``present=False``
# eine fehlende Zeile.
_FAKE_DB = {"duration": 200.0, "present": True}


@pytest.fixture(autouse=True)
def _fake_track_row(monkeypatch):
    import lyrion.database.sqlite_helper as helper

    class _Row:
        def __init__(self, duration):
            self.duration = duration

    class _Result:
        def scalar_one_or_none(self):
            if not _FAKE_DB["present"]:
                return None
            return _Row(_FAKE_DB["duration"])

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    _FAKE_DB.update(duration=200.0, present=True)
    monkeypatch.setattr(helper, "db_session", lambda: _Session())
    yield
    _FAKE_DB.update(duration=200.0, present=True)


# ── Protokoll-Ebene: ``strm 'a'`` bleibt der Sync-Skip (Squeezebox2.pm:1120) ──

def test_send_skip_builds_24_byte_strm_a_frame_with_ms_interval():
    client = SlimProtoClient()
    writer = _FakeWriter()
    client._player_writers = {"021122334455": writer}

    assert asyncio.run(client.send_skip_to_player(MAC, 30)) is True

    data = writer.written
    length = struct.unpack(">H", data[:2])[0]
    assert length == 4 + 24  # 'strm' opcode + 24-byte body
    assert data[2:6] == b"strm"

    body = data[6:]
    assert len(body) == 24
    assert body[0:1] == b"a"  # command = skip-ahead
    # replay-gain field carries the interval in MILLISECONDS (offset 14..18)
    assert struct.unpack(">I", body[14:18])[0] == 30000


# ── Manager-Ebene: Perl ``_JumpToTime`` (StreamingController.pm:1092-1141) ───

def test_seek_forward_restreams_the_same_track_at_the_position():
    """:1132-1141 — ``getSeekData(newtime)`` → ``_Stop`` + ``_Stream`` mit
    ``timeOffset``: derselbe Titel wird an der Position neu gestreamt."""
    pm = _fresh_pm()
    p = _player(pm)

    assert asyncio.run(pm.seek_to(p.mac, 120)) is True
    # KEIN skip-ahead — der Player bekommt einen neuen Stream mit Offset.
    assert pm._protocol_handler.skips == []
    assert pm._protocol_handler.started == [(1, 120.0)]
    # Die Songuhr steht sofort auf der Position (startOffset + player elapsed,
    # StreamingController.pm:1719-1743 → Source.pm:51-53 → Queries.pm:4093).
    assert p.elapsed == 120.0


def test_seek_backwards_also_restreams_at_the_position():
    """Perl unterscheidet die Richtung nicht — nur ``newtime == 0`` startet
    ohne ``seekdata`` neu (:1097-1111)."""
    pm = _fresh_pm()
    p = _player(pm, elapsed=150.0)

    assert asyncio.run(pm.seek_to(p.mac, 30)) is True
    assert pm._protocol_handler.started == [(1, 30.0)]
    assert p.elapsed == 30.0


def test_seek_to_zero_restarts_the_track_without_offset():
    """:1097-1111 — ``$newtime == 0`` → ``_Stop`` + ``resetSeekdata`` +
    ``_Stream`` ohne ``seekdata`` (startOffset 0)."""
    pm = _fresh_pm()
    p = _player(pm, elapsed=150.0)

    assert asyncio.run(pm.seek_to(p.mac, 0)) is True
    assert pm._protocol_handler.started == [(1, 0.0)]
    assert p.elapsed == 0.0


def test_seek_past_the_end_skips_to_the_next_track():
    """:1127-1130 — ``$newtime > $song->duration()`` → ``_Skip``."""
    pm = _fresh_pm()
    p = _player(pm, playlist=[1, 2], duration=200.0)

    assert asyncio.run(pm.seek_to(p.mac, 500)) is True
    assert p.playlist_position == 1
    assert pm._protocol_handler.started == [(2, 0.0)]


def test_seek_without_duration_restarts_the_track():
    """``!$song->duration()`` (:1099-1100) — ohne Dauer kein Offset."""
    _FAKE_DB["duration"] = 0.0        # LENGTH des Titels unbekannt/0
    pm = _fresh_pm()
    p = _player(pm, duration=0.0, elapsed=42.0)

    assert asyncio.run(pm.seek_to(p.mac, 120)) is True
    assert pm._protocol_handler.started == [(1, 0.0)]
    assert p.elapsed == 0.0


def test_first_seek_right_after_a_start_does_not_fall_back_to_zero():
    """Der Web-UI-Start (``playlist.play`` → ``api._play_playlist_item``)
    setzt ``player.duration`` nicht: beim ERSTEN Klick steht dort 0, obwohl
    die Nummer 265 s lang ist (live gemessen 2026-09-22, 0,05 s nach dem
    Start). Perl liest die Dauer der laufenden Nummer aus der DB
    (``Song.pm:809-815`` → ``Info.pm:382-390`` → ``$track->secs``) — der
    erste Klick springt an die Position, statt von vorn zu starten."""
    _FAKE_DB["duration"] = 265.217
    pm = _fresh_pm()
    p = _player(pm, duration=0.0, elapsed=0.2)

    assert asyncio.run(pm.seek_to(p.mac, 120)) is True
    assert pm._protocol_handler.started == [(1, 120.0)]   # KEIN (1, 0.0)
    assert p.elapsed == 120.0


def test_seek_uses_the_playing_entrys_length_not_a_stale_one():
    """Zweite Live-Messung: ``player.duration`` trug noch die 265 s der
    VORIGEN Nummer, die laufende ist 519 s lang. Mit dem Zustandswert war
    ``$newtime > $song->duration()`` wahr → Perl-``_Skip`` (:1127-1130)
    schaltete weiter statt an die Position zu springen."""
    _FAKE_DB["duration"] = 519.3
    pm = _fresh_pm()
    p = _player(pm, playlist=[1, 111], duration=265.217)   # stale
    p.playlist_position = 1
    p.current_track_id = 111

    assert asyncio.run(pm.seek_to(p.mac, 300)) is True
    assert pm._protocol_handler.started == [(111, 300.0)]  # kein Weiter-Schalten
    assert p.playlist_position == 1


def test_seek_keeps_the_open_time_length_when_the_row_is_gone():
    """Perls Kette ist ``$self->_duration() || Info::getDuration(url)``
    (Song.pm:815): die beim Oeffnen gesetzte Dauer bleibt gueltig, wenn die
    DB-Zeile nicht mehr da ist (Titel geloescht)."""
    _FAKE_DB["present"] = False
    pm = _fresh_pm()
    p = _player(pm, duration=200.0)

    assert asyncio.run(pm.seek_to(p.mac, 120)) is True
    assert pm._protocol_handler.started == [(1, 120.0)]


def test_seek_refreshes_the_state_duration_from_the_db_row():
    """Die gelesene Dauer geht in den Zustand zurueck (Perls Song traegt sie,
    ``Song.pm:809-815``) — der status-``duration``-Wert kann damit nicht auf
    der vorigen Nummer stehenbleiben (``cli_commands.py:1724-1735``)."""
    _FAKE_DB["duration"] = 265.217
    pm = _fresh_pm()
    p = _player(pm, duration=0.0)

    assert asyncio.run(pm.seek_to(p.mac, 120)) is True
    assert p.duration == pytest.approx(265.217)


def test_seek_on_a_remote_stream_restarts_it(monkeypatch):
    """Ein Radio-Stream hat keine Dauer und ist nicht seekbar
    (``canSeek`` File.pm:403-415 / HTTP.pm:1186-1188) → Restart."""
    import types

    import lyrion.formats.stream_probe as probe

    monkeypatch.setattr(probe, "scan_stream_url", lambda url: _async_result(
        types.SimpleNamespace(failed=False, error="", url=url, type="mp3",
                              bitrate=128000)))
    pm = _fresh_pm()
    p = _player(pm, playlist=["http://radio.example/x"], duration=0.0)

    assert asyncio.run(pm.seek_to(p.mac, 120)) is True
    assert pm._protocol_handler.streams == ["http://radio.example/x"]
    assert pm._protocol_handler.started == []
    assert pm._protocol_handler.skips == []


def _async_result(value):
    async def _coro():
        return value
    return _coro()


def test_seek_drops_the_strm_guard_before_the_restream():
    """Perl ``_Stop`` schliesst den Stream vor dem neuen (StreamingController
    .pm:1139-1141). Ohne das faengt unser Idempotenz-Waechter in
    ``protocol.send_strm_to_player`` den Re-Stream desselben Titels ab."""
    pm = _fresh_pm()
    p = _player(pm)
    p.strm_sent_track = 1
    p.playing_track_id = 1

    assert asyncio.run(pm.seek_to(p.mac, 120)) is True
    assert p.strm_sent_track is None
    assert p.playing_track_id is None
    assert pm._protocol_handler.started == [(1, 120.0)]


def test_seek_on_empty_playlist_is_a_no_op():
    pm = _fresh_pm()
    p = _player(pm, playlist=[])
    assert asyncio.run(pm.seek_to(p.mac, 120)) is False
    assert pm._protocol_handler.started == []


def test_resume_continues_at_pause_position_without_restart():
    """Perl resume() = ``strm 'u'`` (Squeezebox2.pm:1104-1110): der Ausgang
    laeuft weiter, die Datei wird NICHT neu gestreamt
    (StreamingController.pm:1605-1614)."""
    pm = _fresh_pm()
    p = _player(pm, elapsed=60.0)
    h = pm._protocol_handler

    async def run():
        assert await pm.pause_player(p.mac, True) is True
        assert p.mode == "pause"
        assert p.pause_time == 60.0
        assert await pm.pause_player(p.mac, False) is True

    asyncio.run(run())
    assert p.mode == "play"
    assert p.elapsed == 60.0          # continues at the pause point
    assert h.paused == [p.mac]
    assert h.unpaused == [p.mac]
    assert h.started == []            # no restart → no second stream GET
    assert h.skips == []              # no seek needed
    assert h.stopped == []            # pause is not a stop


# ── Zeitbasis: der Status-``time`` folgt startOffset + Player-Uhr ────────────

class _FakeManager:
    def __init__(self, player):
        self._player = player

    def get_player(self, mac):
        return self._player


def test_status_time_is_start_offset_plus_player_clock(monkeypatch):
    """``StreamingController.pm:1719-1743`` — ``playingSongElapsed`` addiert
    ``$song->startOffset`` zur Player-Uhr; unser STAT-Handler macht dasselbe
    mit ``PlayerState.stream_start_offset``. Nach einem Seek auf 120 s und
    3,4 s Player-Uhr meldet der Status also 123,4 s."""
    import lyrion.player.manager as manager_mod

    p = _player(_fresh_pm(), stream_start_offset=120.0, elapsed=120.0)
    monkeypatch.setattr(manager_mod, "PlayerManager", lambda: _FakeManager(p))
    try:
        import lyrion.web.cometd as cometd_mod
        monkeypatch.setattr(cometd_mod, "get_manager", lambda: None)
    except Exception:  # pragma: no cover
        pass
    client = object.__new__(SlimProtoClient)
    client._handle_stat_frame(p.mac, _stat_payload("STMt", elapsed_ms=3400))
    assert p.elapsed == pytest.approx(123.4)


def test_new_track_starts_the_clock_at_zero():
    """Ein frischer Titel beginnt bei 0: ``play_track`` setzt die Uhr auf
    ``start_seconds`` und der Re-Stream setzt ``stream_start_offset`` neu."""
    pm = _fresh_pm()
    p = _player(pm, elapsed=180.0, stream_start_offset=180.0,
                playlist=[1, 2])

    assert asyncio.run(pm.playlist_play(p.mac, 1)) is True
    assert pm._protocol_handler.started == [(2, 0.0)]
    assert p.elapsed == 0.0


def _stat_payload(event: str, elapsed_ms: int, *, out_fullness: int = 3528000,
                  jiffies: int = 1234) -> bytes:
    """Ein STAT-Frame wie ihn Squeezelite schickt (51 Byte, keine error_code).

    Feldreihenfolge: ``Slim/Networking/Slimproto.pm:768-814`` — event(4),
    jiffies(4), elapsed_milliseconds(4) ab Offset 39, output buffer
    fullness ab Offset 29.
    """
    body = bytearray(51)
    body[0:4] = event.encode()
    struct.pack_into(">I", body, 4, jiffies)
    struct.pack_into(">I", body, 33, out_fullness)
    struct.pack_into(">I", body, 43, elapsed_ms)
    return bytes(body)
