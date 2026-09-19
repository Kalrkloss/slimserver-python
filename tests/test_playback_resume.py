"""Wiedergabe-Fortsetzung beim (Wieder-)Verbinden — Perls ``resumeOnPower``.

Ein Player, der beim Beenden/Verbindungsverlust im Modus PLAY war, muss nach
dem Neustart denselben Titel an derselben Stelle weiterspielen. Perl:

* ``Slim/Networking/Slimproto.pm:283-286`` — beim Close des lebenden Sockets
  laeuft ``persistPlaybackStateForPowerOff($client)`` (:305-321):
  ``playingAtPowerOff = $client->isPlaying(1)`` (:313) und, nur wenn wirklich
  gespielt wurde, ``positionAtDisconnect``
  (``$client->playingSong()->canSeek() ? $client->playingSongElapsed() : 0``,
  :315-318).
* ``Slim/Player/Squeezebox.pm:81-98`` — der HELO-Handler eines zurueckkehrenden
  Clients ruft ``$client->resumeOnPower(1)`` (:89, "Reconnection of a forgotten
  client, need to take resume position from the preferences", :83-84), der
  HELO mit gesetztem ``reconnect``-Bit (Kontrollverbindung weg, kein Reboot,
  :70-77) dagegen ``resumeOnPower()`` ohne Positions-Jump (:96).
  Das Bit steht in der ``wlan_channellist`` des HELO
  (``Slimproto.pm:972-974``: ``& 0x8000`` bitmapped, ``& 0x4000`` reconnect).
* ``Slim/Player/Player.pm:301-332`` — ``resumeOnPower`` selbst: nicht
  abspielen, wenn schon PLAYING (:304); ``powerOnResume`` wird als
  ``/-(.*)On/`` gelesen (:305, Default ``PauseOff-PlayOn``, :66); ``Reset``
  setzt die Playlist auf 0 ohne zu starten (:307-310); ``Play`` startet nur,
  wenn ein Titel in der Playlist liegt **und** ``playingAtPowerOff`` (:312-313)
  — mit ``$connect`` per ``playlist jump <index> 1 0 {timeOffset => $position}``
  (:316-319), sonst per ``play`` (:321). Danach wird das Flag geloescht (:329).

STOP und PAUSE starten NICHT wieder: ``isPlaying(1)`` ist
``playingState == PLAYING`` (``StreamingController.pm:1676-1678``), ein
pausierter/gestoppter Player setzt ``playingAtPowerOff`` also auf 0.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

import lyrion.control.notifications as notifications
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import (
    POWER_ON_RESUME_DEFAULT,
    PlayerManager,
    power_on_resume_pref,
)
from lyrion.player.state import PlayerState

MAC = "02:11:22:33:44:EE"
CAPS = "Model=squeezeplay,ModelName=SB Player,Firmware=9.0.0-r1583,alc,aac,mp3"


class _FakeHandler:
    """Records the frames the resume path would write to a real player."""

    def __init__(self):
        self.strm: list[tuple[str, object]] = []
        self.skip: list[tuple[str, int]] = []
        self.paused: list[str] = []
        self.unpaused: list[str] = []
        self.stopped: list[str] = []

    async def send_strm_to_player(self, mac, track_id, start_seconds=0.0):
        self.strm.append((mac, track_id, float(start_seconds or 0.0)))
        return True

    async def send_skip_to_player(self, mac, seconds):
        self.skip.append((mac, seconds))
        return True

    async def send_pause_to_player(self, mac, pause_ms=0):
        self.paused.append(mac)
        return True

    async def send_unpause_to_player(self, mac):
        self.unpaused.append(mac)
        return True

    async def send_stop_to_player(self, mac):
        self.stopped.append(mac)
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        self.strm.append((mac, url, 0.0))
        return True


def _h(pm: PlayerManager) -> _FakeHandler:
    """The fake transport installed by :func:`_fresh_pm`."""
    handler = pm._protocol_handler
    assert isinstance(handler, _FakeHandler)
    return handler


def _streams(handler: _FakeHandler) -> list:
    """The (mac, track) pairs the resume path streamed."""
    return [(mac, item) for mac, item, _offset in handler.strm]


def _fresh_pm(handler=None) -> PlayerManager:
    pm = object.__new__(PlayerManager)
    pm.players = {}
    pm._protocol_handler = handler or _FakeHandler()
    return pm


def _player(pm, **kw) -> PlayerState:
    p = PlayerState(mac=MAC, name="Test", ip="127.0.0.1", port=0)
    p.power = True
    p.mode = "play"
    p.playlist = [11, 22, 33]
    p.playlist_position = 1
    p.current_track_id = 22
    p.elapsed = 42.5
    for k, v in kw.items():
        setattr(p, k, v)
    pm.players[MAC] = p
    return p


# ── Pref (Player.pm:66 / :305) ─────────────────────────────────────────────


def test_power_on_resume_default_is_perls_pauseoff_playon():
    pm = _fresh_pm()
    p = _player(pm)
    assert POWER_ON_RESUME_DEFAULT == "PauseOff-PlayOn"   # Player.pm:66
    assert power_on_resume_pref(p) == "PauseOff-PlayOn"
    p.playerprefs["powerOnResume"] = "StopOff-NoneOn"
    assert power_on_resume_pref(p) == "StopOff-NoneOn"


# ── Zustand beim Verbindungsverlust (Slimproto.pm:305-321) ─────────────────


def test_persist_records_playing_and_position():
    pm = _fresh_pm()
    p = _player(pm, mode="play", elapsed=42.5, remote=0)
    asyncio.run(pm.persist_playback_state_for_power_off(MAC))
    assert p.playing_at_power_off is True                 # :313
    assert p.position_at_disconnect == pytest.approx(42.5)  # :315-318


def test_persist_treats_paused_and_stopped_as_not_playing():
    for mode in ("pause", "stop"):
        pm = _fresh_pm()
        p = _player(pm, mode=mode, elapsed=42.5)
        asyncio.run(pm.persist_playback_state_for_power_off(MAC))
        assert p.playing_at_power_off is False, mode      # isPlaying(1) = PLAYING
        # positionAtDisconnect bleibt in Perl unangetastet (:315 nur bei Wiedergabe)
        assert p.position_at_disconnect == 0.0


def test_persist_uses_zero_position_for_unseekable_remote_stream():
    pm = _fresh_pm()
    p = _player(pm, current_url="http://radio.example/x", remote=1, elapsed=99.0)
    asyncio.run(pm.persist_playback_state_for_power_off(MAC))
    assert p.playing_at_power_off is True
    assert p.position_at_disconnect == 0.0                # canSeek() ? … : 0


# ── resumeOnPower (Player.pm:301-332) ──────────────────────────────────────


def test_connect_resumes_same_track_at_saved_position():
    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, playing_at_power_off=True,
                    position_at_disconnect=42.5)
        ok = await pm.resume_on_power(MAC, connect=True)
        return ok, p, _h(pm)

    ok, p, h = asyncio.run(run())
    assert ok
    assert _streams(h) == [(MAC, 22)]           # selber Titel (:317)
    # selbe Stelle (:318/:319) — Perl ``timeOffset``, wird als Byte-Range der
    # Quelle gestartet (File.pm:196-223, HTTP.pm:963-971)
    assert h.strm[0][2] == pytest.approx(42.5)
    assert p.mode == "play"
    assert p.playing_at_power_off is False                  # :329 verbraucht


def test_connect_after_stop_does_not_start_playback():
    """Gegenprobe (a): nach STOP bleibt es still (isPlaying(1) == 0, :313)."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, mode="stop",
                    playing_at_power_off=False, position_at_disconnect=42.5)
        ok = await pm.resume_on_power(MAC, connect=True)
        return ok, _h(pm)

    ok, h = asyncio.run(run())
    assert ok is False
    assert h.strm == [] and h.skip == []


def test_connect_after_pause_does_not_start_playback():
    """Gegenprobe (b): PAUSE verhaelt sich wie Perl — kein Stream, kein Resume."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, mode="pause", pause_requested=True,
                    pause_time=42.5, playing_at_power_off=False)
        ok = await pm.resume_on_power(MAC, connect=True)
        return ok, p, _h(pm)

    ok, p, h = asyncio.run(run())
    assert ok is False
    assert h.strm == [] and h.skip == []
    assert p.mode == "pause", "PAUSE darf nicht in PLAY kippen"


def test_connect_without_previous_playback_stays_unchanged():
    """Gegenprobe (c): normales Verbinden ohne vorherige Wiedergabe."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, mode="stop", playlist=[],
                    playlist_position=0, current_track_id=None,
                    playing_at_power_off=False)
        ok = await pm.resume_on_power(MAC, connect=True)
        return ok, _h(pm)

    ok, h = asyncio.run(run())
    assert ok is False
    assert h.strm == []


def test_power_off_player_is_not_resumed():
    """Squeezebox.pm:85/:95 — ein ausgeschalteter Player bleibt still."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, power=False,
                    playing_at_power_off=True, position_at_disconnect=42.5)
        return await pm.resume_on_power(MAC, connect=True)

    assert asyncio.run(run()) is False


def test_already_streaming_player_is_not_restarted():
    """Player.pm:304 — ein noch laufender Player bekommt keinen zweiten Stream."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=True, mode="play",
                    playing_at_power_off=True, position_at_disconnect=42.5)
        ok = await pm.resume_on_power(
            MAC, connect=False, was_online=True)
        return ok, _h(pm)

    ok, h = asyncio.run(run())
    assert ok is False
    assert h.strm == []


def test_none_on_pref_never_plays():
    """'PauseOff-NoneOn' — (:305) resumeOn enthaelt kein 'Play'."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, playing_at_power_off=True,
                    position_at_disconnect=42.5,
                    playerprefs={"powerOnResume": "PauseOff-NoneOn"})
        return await pm.resume_on_power(MAC, connect=True), _h(pm)

    ok, h = asyncio.run(run())
    assert ok is False
    assert h.strm == []


def test_reset_on_resets_playlist_without_playing():
    """'StopOff-ResetOn' — ``playlist jump 0 1 1`` (noplay), Player.pm:307-310."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, mode="stop",
                    playing_at_power_off=True, position_at_disconnect=42.5,
                    playerprefs={"powerOnResume": "StopOff-ResetOn"})
        return await pm.resume_on_power(MAC, connect=True), p, _h(pm)

    ok, p, h = asyncio.run(run())
    assert ok is False
    assert p.playlist_position == 0
    assert h.strm == [], "ResetOn startet nicht"


def test_reset_play_on_resets_then_plays_first_track():
    """'StopOff-ResetPlayOn' — erst Reset, dann Play (Player.pm:307-319)."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, mode="stop",
                    playing_at_power_off=True, position_at_disconnect=0.0,
                    playerprefs={"powerOnResume": "StopOff-ResetPlayOn"})
        return await pm.resume_on_power(MAC, connect=True), p, _h(pm)

    ok, p, h = asyncio.run(run())
    assert ok
    assert p.playlist_position == 0
    assert _streams(h) == [(MAC, 11)]


def test_second_connect_does_not_resume_again():
    """Player.pm:324-329 — das Flag wird verbraucht."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, playing_at_power_off=True,
                    position_at_disconnect=42.5)
        first = await pm.resume_on_power(MAC, connect=True)
        p.mode = "stop"                    # Player ist danach wieder weg
        second = await pm.resume_on_power(MAC, connect=True)
        return first, second, _h(pm)

    first, second, h = asyncio.run(run())
    assert first is True
    assert second is False
    assert len(h.strm) == 1


def test_reconnect_with_active_data_connection_resumes_paused_player():
    """Squeezebox.pm:96 → ``resumeOnPower()`` → ``play`` (Player.pm:321).

    Ohne ``$connect`` gibt es keinen Positions-Jump: ein pausierter Player
    wird an Ort und Stelle fortgesetzt (playcontrolCommand 'resume',
    Commands.pm:747-748).
    """

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, mode="pause", pause_time=42.5,
                    playing_at_power_off=True, position_at_disconnect=42.5)
        ok = await pm.resume_on_power(MAC, connect=False)
        return ok, p, _h(pm)

    ok, p, h = asyncio.run(run())
    assert ok
    assert h.unpaused == [MAC], "Resume in place statt neuem Stream"
    assert h.strm == [] and h.skip == []
    assert p.mode == "play"


# ── Power-Pfad (Player.pm:230-231 / :296-297) ──────────────────────────────


def test_power_off_records_playing_at_power_off():
    pm = _fresh_pm()
    p = _player(pm, mode="play")
    pm.set_power(MAC, False)
    assert p.playing_at_power_off is True          # Player.pm:230-231
    assert p.mode == "stop"


def test_power_off_while_paused_records_not_playing():
    pm = _fresh_pm()
    p = _player(pm, mode="pause")
    pm.set_power(MAC, False)
    assert p.playing_at_power_off is False         # isPlaying(1) == PLAYING


def test_power_on_resumes_a_player_that_was_playing():
    """Player.pm:296-297 → ``resumeOnPower()`` ohne ``$connect`` (:321)."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, mode="play")
        pm.set_power(MAC, False)                   # merkt sich das Abspielen
        p.connected = True
        pm.set_power(MAC, True)
        await asyncio.sleep(0.05)                  # geplante Resume-Task
        return p, _h(pm)

    p, h = asyncio.run(run())
    assert p.power is True
    assert _streams(h) == [(MAC, 22)], "selber Titel, von vorn"
    assert h.strm[0][2] == 0.0, "Power-on startet ohne Positions-Offset"


# ── Verdrahtung am echten HELO-Socket (Squeezebox.pm:79-98) ────────────────


def _helo_frame(mac: str, caps: str = CAPS, reconnect: bool = False) -> bytes:
    """HELO-Körper wie ``tests/test_player_registration_lifecycle.py``.

    ``reconnect`` setzt Bit 14 der wlan_channellist (Slimproto.pm:973).
    """
    wlan = 0x4000 if reconnect else 0x0000
    body = (
        bytes([4, 0])
        + bytes.fromhex(mac.replace(":", ""))
        + bytes(16)
        + struct.pack(">H", wlan)
        + struct.pack(">I", 0)
        + struct.pack(">I", 0)
        + b"en"
        + caps.encode()
    )
    return b"HELO" + struct.pack(">I", len(body)) + body


def _cleanup() -> None:
    PlayerManager().players.clear()
    notifications.reset()


def _install_fake_transport() -> _FakeHandler:
    """Real HELO/Socket wiring, fake audio frames.

    ``playlist_play``/``seek_to`` laufen echt (playlist/index/Positions-Logik
    aus Player.pm:316-319); nur die Schreibzugriffe auf den Player sind
    aufgezeichnet, damit der Test keine Library/keinen Decoder braucht.
    """
    handler = _FakeHandler()
    pm = PlayerManager()
    pm.set_protocol_handler(handler)
    return handler


async def _serve(client: SlimProtoClient):
    server = await asyncio.start_server(client._handle_player, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _connect(port: int, reconnect: bool = False):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_helo_frame(MAC, reconnect=reconnect))
    await writer.drain()
    await asyncio.sleep(0.15)
    return reader, writer


def test_helo_after_disconnect_resumes_via_the_saved_position():
    """Ein beim Beenden abspielender Player nimmt nach dem Neustart wieder auf."""

    async def run():
        client = SlimProtoClient()
        handler = _install_fake_transport()
        server, port = await _serve(client)
        try:
            reader, writer = await _connect(port)
            pm = PlayerManager()
            player = pm.get_player(MAC)
            assert player is not None
            # Der Player laeuft: Titel 22 an Index 1, 42.5 s gespielt.
            player.power = True
            player.mode = "play"
            player.playlist = [11, 22, 33]
            player.playlist_position = 1
            player.current_track_id = 22
            player.elapsed = 51.767
            handler.strm.clear()

            # Player-Prozess beendet: Socket-Close = echter Disconnect.
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.2)
            assert player.connected is False
            assert player.playing_at_power_off is True          # Slimproto.pm:313
            assert player.position_at_disconnect == pytest.approx(51.767)

            # Player-Prozess neu gestartet: frischer HELO ohne reconnect-Bit.
            reader2, writer2 = await _connect(port)
            await asyncio.sleep(0.2)
            try:
                assert _streams(handler) == [(MAC, 22)], "selber Titel"
                assert handler.strm[0][2] == pytest.approx(51.767), "selbe Stelle"
            finally:
                writer2.close()
        finally:
            server.close()
            await server.wait_closed()
            _cleanup()

    asyncio.run(run())


def test_helo_without_previous_playback_sends_nothing():
    """Gegenprobe (c) am echten Socket: nur verbinden heisst nicht abspielen."""

    async def run():
        client = SlimProtoClient()
        handler = _install_fake_transport()
        server, port = await _serve(client)
        try:
            reader, writer = await _connect(port)
            try:
                assert handler.strm == []
                assert handler.skip == []
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()
            _cleanup()

    asyncio.run(run())


def test_helo_after_stop_sends_nothing():
    """Gegenprobe (a) am echten Socket."""

    async def run():
        client = SlimProtoClient()
        handler = _install_fake_transport()
        server, port = await _serve(client)
        try:
            reader, writer = await _connect(port)
            pm = PlayerManager()
            player = pm.get_player(MAC)
            player.power = True
            player.playlist = [11, 22, 33]
            player.playlist_position = 1
            player.mode = "stop"
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.2)
            assert player.playing_at_power_off is False

            reader2, writer2 = await _connect(port)
            await asyncio.sleep(0.2)
            try:
                assert handler.strm == []
                assert handler.skip == []
            finally:
                writer2.close()
        finally:
            server.close()
            await server.wait_closed()
            _cleanup()

    asyncio.run(run())


def test_helo_after_pause_sends_nothing():
    """Gegenprobe (b) am echten Socket."""

    async def run():
        client = SlimProtoClient()
        handler = _install_fake_transport()
        server, port = await _serve(client)
        try:
            reader, writer = await _connect(port)
            pm = PlayerManager()
            player = pm.get_player(MAC)
            player.power = True
            player.playlist = [11, 22, 33]
            player.playlist_position = 1
            player.mode = "pause"
            player.pause_time = 42.5
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.2)
            assert player.playing_at_power_off is False

            reader2, writer2 = await _connect(port)
            await asyncio.sleep(0.2)
            try:
                assert handler.strm == []
                assert handler.skip == []
                assert player.mode == "pause"
            finally:
                writer2.close()
        finally:
            server.close()
            await server.wait_closed()
            _cleanup()

    asyncio.run(run())


def test_helo_with_reconnect_bit_uses_the_non_connect_path():
    """Squeezebox.pm:93-98 — reconnect-Bit (kein Reboot): ``resumeOnPower()``.

    Ein pausierter Player wird dabei an Ort und Stelle fortgesetzt, ohne
    erneuten Stream (Player.pm:321).
    """

    async def run():
        client = SlimProtoClient()
        handler = _install_fake_transport()
        server, port = await _serve(client)
        try:
            reader, writer = await _connect(port)
            pm = PlayerManager()
            player = pm.get_player(MAC)
            player.power = True
            player.playlist = [11, 22, 33]
            player.playlist_position = 1
            player.mode = "pause"
            player.pause_time = 42.5
            player.playing_at_power_off = False

            # Kontrollverbindung brach ab, Player lief weiter (Bit 0x4000),
            # der Stream ist weg (Strmschutz faellt beim Disconnect).
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.2)
            player.playing_at_power_off = True

            reader2, writer2 = await _connect(port, reconnect=True)
            await asyncio.sleep(0.2)
            try:
                # Ohne player-_connect_ bleibt es bei Perls Resume-Zweig:
                # der pausierte Player wird fortgesetzt (unpause), nicht neu
                # gestreamt.
                assert handler.strm == []
            finally:
                writer2.close()
        finally:
            server.close()
            await server.wait_closed()
            _cleanup()

    asyncio.run(run())


KEY = MAC.replace(":", "").upper()


def test_synced_player_is_not_resumed():
    """Squeezebox.pm:86-90 — "Don't try to resume if we are synced"."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=False, playing_at_power_off=True,
                    position_at_disconnect=42.5, sync_master="02:00:00:00:00:01",
                    sync_slaves=["02:00:00:00:00:02"])
        return await pm.resume_on_power(MAC, connect=True), _h(pm)

    ok, h = asyncio.run(run())
    assert ok is False
    assert h.strm == []


def test_power_on_of_a_synced_player_has_no_sync_gate():
    """Der Gate steht nur im HELO-Pfad (Squeezebox.pm:89), nicht in :297."""

    async def run():
        pm = _fresh_pm()
        p = _player(pm, connected=True, mode="stop", playing_at_power_off=True,
                    position_at_disconnect=42.5,
                    sync_master="02:00:00:00:00:01")
        ok = await pm.resume_on_power(MAC, connect=False)
        return ok, _h(pm)

    ok, h = asyncio.run(run())
    assert ok
    assert _streams(h) == [(MAC, 22)]


# ── Frame-Ebene: Perl startet die Quelle am Offset (File.pm:196-223) ───────


def test_resume_request_carries_perls_range_header():
    from lyrion.networking.protocol import stream_request_bytes

    plain = stream_request_bytes(KEY)
    assert plain == (b"GET /stream.mp3?player=" + KEY.encode()
                     + b" HTTP/1.0\r\n\r\n")
    ranged = stream_request_bytes(KEY, range_offset=1234567)
    assert ranged == (b"GET /stream.mp3?player=" + KEY.encode()
                      + b" HTTP/1.0\r\nRange: bytes=1234567-\r\n\r\n")
    assert stream_request_bytes(KEY, transcode=True) == (
        b"GET /stream.mp3?player=" + KEY.encode() + b"&transcode=1 HTTP/1.0\r\n\r\n")


def test_resume_byte_offset_is_proportional_and_guarded():
    from lyrion.networking.protocol import resume_byte_offset

    assert resume_byte_offset(51.767, 1494.164, 38912000) == int(
        38912000 * 51.767 / 1494.164)
    assert resume_byte_offset(0, 100, 1000) == 0
    assert resume_byte_offset(10, 0, 1000) == 0     # keine Dauer → kein Offset
    assert resume_byte_offset(10, 100, 0) == 0      # kein Quellbyte bekannt
    assert resume_byte_offset(200, 100, 1000) == 1000   # nie über das Ende
