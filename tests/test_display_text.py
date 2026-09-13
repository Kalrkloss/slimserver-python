"""Now-Playing-Displaytext beim Zustandswechsel (PROT-18, Teil 2).

Commit ``60d2812e6`` lieferte die Renderer (``player/fonts.py``), meldete aber
offen, dass der Aufrufer den Text noch nicht übergibt — die Textzeile wird im
``player/manager.py`` gesetzt. Diese Datei belegt den Text in Perl und prüft,
dass beim Zustandswechsel real ein ``grfe``-Frame mit Bits rausgeht
(Fake-Writer, wie ``tests/test_display_wiring.py``).

WER den Text setzt (Perl)
-------------------------
* Screen-Zusammenstellung: ``Slim/Buttons/Playlist.pm:398-483`` — ``lines()``
  der Button-Mode "Playlist". Ist ``showingNowPlaying($client)`` wahr
  (:404, definiert :494-504: Mode ``screensaver`` ODER Mode ``playlist`` mit
  ``browseplaylistindex == playingSongIndex``), holt es ``currentSongLines``
  (:408) — also der Now-Playing-Screen. Sonst zeigt es den Playlist-Browser
  (:420-453, "CURRENT_PLAYLIST").
* Die zwei Zeilen: ``Slim/Player/Player.pm:488-706`` ``currentSongLines``.
  - Leere Playlist (:509-513): ``line[0] = string('NOW_PLAYING')``,
    ``line[1] = string('NOTHING')``.
  - ``playmode eq 'pause'`` (:521-535): ``line[0] = string('PAUSED')``.
  - ``playmode eq 'stop'`` (:541-553): ``line[0] = string('STOPPED')``.
  - sonst (play/play) (:571-585): ``line[0] = string('PLAYING')``.
    Bei ``playlistlen > 1`` hängt jedes der drei ``sprintf($status." (%d %s %d) ",
    playingSongIndex+1, string('OUT_OF'), playlistlen)`` an (:531-534,
    :549-552, :580-583) — inkl. Leerzeichen am Ende.
  - ``line[1] = currentTitle`` (:633) = ``Slim::Music::Info::getCurrentTitle``
    (``Music/Info.pm:556-583``) mit dem Default-``titleFormat`` ``'TITLE'``
    (``Utils/Prefs.pm:249-250`` + ``Player/Client.pm:52-53`` → ``titleFormat[0]``).
* Englische Default-Strings (``strings.txt``): PLAYING→"Now Playing" (:761),
  PAUSED→"Paused" (:801), STOPPED→"Stopped" (:11107), NOW_PLAYING→"Now Playing"
  (:703), NOTHING→"Nothing" (:469), OUT_OF→"of" (:781).

Welcher Modus beeinflusst was — belegt, nicht UNKLAR
---------------------------------------------------
``Player.pm:685-688`` setzt ``$parts->{line}`` und ruft danach
``nowPlayingModeLines`` (:688/:710-799). Dieses liest den Modus
``playingDisplayModes[playingDisplayMode]`` (:721-723), holt ``bar``/``secs``/
``fullness``/``clock`` aus der Modustabelle (:727-732) und schreibt
ausschließlich ``$parts->{overlay}[0]`` (:798) — **die beiden Textzeilen bleiben
unverändert**. Der Modus steuert also den Fortschritts-Overlay (Balken/Zeit/
Prozent), nicht den Now-Playing-Text. Die Zeilen längen sich nur über
``displayWidth`` (``Squeezebox2.pm:195-203``), was der Renderer schon umsetzt.
Der zweite modusabhängige Teil (Bild/Interpret) ist ``screen2``:
``hasScreen2`` ist laut ``Display.pm:859`` für alle Klassen 0, nur
``Transporter.pm:150`` meldet 1, und ``showExtendedText`` hängt dort am
Visualizer-Modus (``Transporter.pm:319-325``). ``screen2`` (``Player.pm:638-665``
mit ``displayText(...,'ALBUM')``/``('ARTIST')``) ist im Port nicht verdrahtet —
deshalb enthält der gesendete Screen1-Text Status+Titel, kein Interpret.
"""

from __future__ import annotations

import asyncio
import sqlite3
import struct

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player import fonts
from lyrion.player.display import SQUEEZEBOX2
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"

#: Perl-Strings, die ``currentSongLines`` auf die Zeilen schreibt.
NOW_PLAYING = "Now Playing"   # strings.txt:761 (PLAYING) / :703 (NOW_PLAYING)
PAUSED = "Paused"             # strings.txt:801
STOPPED = "Stopped"           # strings.txt:11107
NOTHING = "Nothing"           # strings.txt:469


class _FakeWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


class _Handler:
    """Minimaler SlimProtoClient: nur die Display-Sender + Writer-Tabelle."""

    def __init__(self) -> None:
        self.writer = _FakeWriter()
        self._player_writers = {MAC_CLEAN: self.writer}

    send_visu = SlimProtoClient.send_visu
    send_grfb = SlimProtoClient.send_grfb
    send_display_framebuffer = SlimProtoClient.send_display_framebuffer
    send_grfd_framebuffer = SlimProtoClient.send_grfd_framebuffer
    send_vfdc = SlimProtoClient.send_vfdc


def _payload(frame: bytes) -> bytes:
    (length,) = struct.unpack(">H", frame[:2])
    payload = frame[2:]
    assert length == len(payload)
    return payload


def _grfe_bits(writer: _FakeWriter) -> bytes:
    """Bits des letzten ``grfe``-Frames (Squeezebox2.pm:243-248)."""
    frames = [f for f in writer.frames if f[2:6] == b"grfe"]
    assert frames, "kein grfe-Frame gesendet"
    payload = _payload(frames[-1])
    # payload = 'grfe' + offset(2) + transition(1) + param(1) + bits
    assert payload[4:8] == b"\x00\x00c\x00"
    return payload[8:]


@pytest.fixture
def lib_db(tmp_path, monkeypatch):
    """Mini-``tracks``-Tabelle für den Titel-Lookup (Perl ``getCurrentTitle``)."""
    db_path = tmp_path / "lyrion.db"
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT)")
        con.executemany(
            "INSERT INTO tracks (id, title) VALUES (?, ?)",
            [(1, "Creep"), (2, "Airbag")],
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr("lyrion.player.manager._library_db_path",
                        lambda: str(db_path))
    return db_path


def _manager(handler: _Handler) -> PlayerManager:
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {}
    pm._protocol_handler = handler
    pm._display_wiring = None
    PlayerManager._instance = pm
    return pm


def _player(model: str = "squeezebox2", **kwargs) -> PlayerState:
    return PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=3483,
                       model=model, **kwargs)


def _mode(pm: PlayerManager, player: PlayerState) -> int:
    """``displayWidth``-Modus des Spielers (``Squeezebox2.pm:195-203``)."""
    wiring = pm.display_wiring()
    assert wiring is not None
    return wiring.display_mode(player)


def _setup(model: str = "squeezebox2", **kwargs):
    handler = _Handler()
    pm = _manager(handler)
    player = _player(model, **kwargs)
    pm.players[MAC_CLEAN] = player
    return pm, player, handler


# ── Zustandswechsel -> echter grfe-Frame mit den Now-Playing-Zeilen ───────


def test_play_renders_status_and_title_into_the_framebuffer(lib_db):
    """Play: ``line[0] = "Now Playing (1 of 2) "`` (Player.pm:571-585),
    ``line[1] = Titel`` (:633) — gerendert als ``grfe``-Bits."""
    async def run():
        pm, player, handler = _setup(
            "squeezebox2", mode="play", playlist=[1, 2], playlist_position=0,
            playlist_total=2, current_track_id=1,
        )
        sent = await pm._display_update(player)
        assert sent == ["visu", "grfe"]
        lines = ["Now Playing (1 of 2) ", "Creep"]
        mode = _mode(pm, player)   # playingDisplayMode 5
        assert mode == 5
        expected = fonts.render_display_text(SQUEEZEBOX2, lines, mode=mode)
        assert len(expected) == 278 * 4                  # displayWidth Modus 5
        assert any(expected), "Renderer muss echte Bits liefern"
        assert _grfe_bits(handler.writer) == expected

    asyncio.run(run())


def test_single_track_playlist_has_no_out_of_counter(lib_db):
    """``playlistlen > 1`` gated den ``(i of n)``-Zusatz (Player.pm:579-584)."""
    async def run():
        pm, player, handler = _setup(
            "squeezebox2", mode="play", playlist=[2], playlist_position=0,
            playlist_total=1, current_track_id=2,
        )
        await pm._display_update(player)
        bits = _grfe_bits(handler.writer)
        assert bits == fonts.render_display_text(
            SQUEEZEBOX2, [NOW_PLAYING, "Airbag"],
            mode=_mode(pm, player))

    asyncio.run(run())


def test_pause_and_stop_use_their_own_status_lines(lib_db):
    """``PAUSED`` (:523) / ``STOPPED`` (:543) statt ``PLAYING`` (:573)."""
    async def run():
        for mode, status in (("pause", PAUSED), ("stop", STOPPED),
                             ("play", NOW_PLAYING)):
            pm, player, handler = _setup(
                "squeezebox2", mode=mode, playlist=[1, 2], playlist_position=1,
                playlist_total=2, current_track_id=1,
            )
            await pm._display_update(player)
            bits = _grfe_bits(handler.writer)
            assert bits == fonts.render_display_text(
                SQUEEZEBOX2, [f"{status} (2 of 2) ", "Creep"],
                mode=_mode(pm, player)), mode

    asyncio.run(run())


def test_empty_playlist_shows_nothing_currently_playing(lib_db):
    """Player.pm:509-513 — ``NOW_PLAYING`` / ``NOTHING`` ohne Playlist."""
    async def run():
        pm, player, handler = _setup("squeezebox2", mode="stop")
        await pm._display_update(player)
        bits = _grfe_bits(handler.writer)
        assert bits == fonts.render_display_text(
            SQUEEZEBOX2, [NOW_PLAYING, NOTHING],
            mode=_mode(pm, player))

    asyncio.run(run())


def test_remote_stream_uses_current_title_instead_of_the_track_row(lib_db):
    """Radio: ``getCurrentTitle`` liefert den Stations-/Streamtitel
    (``Info.pm:556-570`` Handler-Override); kein Track-Row-Lookup."""
    async def run():
        pm, player, handler = _setup(
            "squeezebox2", mode="play", playlist=["http://radio.example/live"],
            playlist_position=0, playlist_total=1, current_track_id=None,
            current_title="Radio Eins", remote=1,
        )
        await pm._display_update(player)
        bits = _grfe_bits(handler.writer)
        assert bits == fonts.render_display_text(
            SQUEEZEBOX2, [NOW_PLAYING, "Radio Eins"],
            mode=_mode(pm, player))

    asyncio.run(run())


def test_boom_and_transporter_send_their_own_geometry(lib_db):
    """Boom (160er-Breite, Boom.pm:115-121) und Transporter
    (``Transporter.pm:192-193`` → 320, Visualizer nur bei Power,
    ``Transporter.pm:308-317``)."""
    async def run():
        pm, player, handler = _setup(
            "boom", mode="play", playlist=[1], playlist_position=0,
            playlist_total=1, current_track_id=1,
        )
        assert await pm._display_update(player) == ["visu", "grfe"]
        assert _grfe_bits(handler.writer) == fonts.render_display_text(
            "Boom", [NOW_PLAYING, "Creep"],
            mode=_mode(pm, player))

        pm, player, handler = _setup(
            "transporter", mode="play", playlist=[1], playlist_position=0,
            playlist_total=1, current_track_id=1, power=True,
        )
        assert await pm._display_update(player) == ["visu", "grfe"]
        assert _grfe_bits(handler.writer) == fonts.render_display_text(
            "Transporter", [NOW_PLAYING, "Creep"],
            mode=_mode(pm, player))

    asyncio.run(run())


def test_the_display_mode_only_changes_the_geometry_not_the_lines(lib_db):
    """``nowPlayingModeLines`` schreibt nur ``overlay[0]`` (Player.pm:798) —
    die Zeilen bleiben gleich, nur ``displayWidth`` folgt dem Modus."""
    async def run():
        seen = []
        for index, width in ((0, 320), (3, 278)):
            pm, player, handler = _setup(
                "squeezebox2", mode="play", playlist=[1, 2],
                playlist_position=0, playlist_total=2, current_track_id=1,
            )
            player.playerprefs["playingDisplayMode"] = index
            await pm._display_update(player)
            bits = _grfe_bits(handler.writer)
            assert len(bits) == width * 4, index
            assert bits == fonts.render_display_text(
                SQUEEZEBOX2, ["Now Playing (1 of 2) ", "Creep"], mode=index)
            seen.append(bits)
        assert seen[0] != seen[1]

    asyncio.run(run())


def test_no_display_models_still_get_nothing(lib_db):
    """NoDisplay.pm:32 ``sub update {}`` — auch mit Text."""
    async def run():
        for model in ("squeezeplay", "controller", "receiver", "http", "web",
                      "quatsch"):
            pm, player, handler = _setup(
                model, mode="play", playlist=[1], playlist_position=0,
                playlist_total=1, current_track_id=1,
            )
            assert await pm._display_update(player) == []
            assert handler.writer.frames == [], model

    asyncio.run(run())


# ── show_display / show_briefly mit Text ─────────────────────────────────


def test_show_display_sends_real_bytes_for_the_given_lines(lib_db):
    """Perl ``display`` (``Commands.pm:444-472``) -> ``showBriefly``
    (``Display.pm:221-327``) — der Text ist die Nutzlast."""
    async def run():
        pm, player, handler = _setup(
            "squeezebox2", mode="play", playlist=[1], playlist_position=0,
            playlist_total=1, current_track_id=1,
        )
        assert await pm.show_display(MAC, "Hello", "World") is True
        assert _grfe_bits(handler.writer) == fonts.render_display_text(
            SQUEEZEBOX2, ["Hello", "World"],
            mode=_mode(pm, player))

    asyncio.run(run())


def test_text_display_gets_a_vfd_stream_for_the_lines(lib_db):
    """Text-Display: ``Text.pm:437-441`` -> TextVFD -> ``vfdc``."""
    async def run():
        pm, player, handler = _setup(
            "squeezeslave", mode="play", playlist=[1], playlist_position=0,
            playlist_total=1, current_track_id=1,
        )
        await pm._display_update(player)
        frames = [f for f in handler.writer.frames if f[2:6] == b"vfdc"]
        assert frames, "kein vfdc-Frame gesendet"
        vfd = _payload(frames[-1])[4:]
        assert vfd == fonts.vfd_update(NOW_PLAYING, "Creep",
                                       model="squeezeslave")

    asyncio.run(run())
