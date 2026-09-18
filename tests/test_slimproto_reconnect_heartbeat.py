"""SlimProto Wiederverbindung + Heartbeat wie Perl.

Deckt die drei Lebenszyklus-Luecken ab, die einen SqueezePlay/Squeezelite als
``connected=0`` mit lebendem Jive-Kanal zuruecklassen (kein RESP/STM, Titel
friert ein, Favoritenstart kommt an aber es gibt keinen Ton):

* ``Slim/Networking/Slimproto.pm:1195`` — ein HELO fuer eine bekannte MAC
  schliesst den ALTEN Socket mit dem ``reconnect``-Flag
  (``slimproto_close( $client->tcpsock, 'reconnect' )``): dessen Close laeuft
  OHNE Disconnect-Block (:261-296). Perl haelt genau EINEN ``tcpsock`` pro
  Client; ein geleakter alter Socket blaeht den Zaehler auf, der Register
  zeigt weiter auf einen toten Writer und der naechste ``strm`` geht ins Leere.
* ``Slim/Networking/Slimproto.pm:1198`` — bei der Neuanmeldung wird der
  Forget-Timer gekillt (``Slim::Utils::Timers::killTimers($client,
  \\&forget_disconnected_client)``), :1223 setzt den Heartbeat neu.
* ``Slim/Networking/Slimproto.pm:199-241`` (``check_all_clients``, :40
  ``$check_all_clients_time = 5``) + ``:703-709`` (``_stat_handler`` frischt
  ``$heartbeat`` auf): ein Client, der 3 Poll-Intervalle lang nichts mehr
  antwortet, wird geschlossen — mit dem normalen Disconnect-Block (:265-296).

Die Frames sind echte SlimProto-Frames auf einem echten Socket (2-Byte-Laenge
inklusive Opcode in Server->Player-Richtung, 4-Byte-Opcode + 4-Byte-Laenge in
Player->Server-Richtung).
"""

from __future__ import annotations

import asyncio
import struct

import pytest

import lyrion.control.notifications as notifications
import lyrion.networking.protocol as proto
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager

MAC = "02:11:22:33:44:CC"
KEY = MAC.replace(":", "").upper()
CAPS = "Model=squeezeplay,ModelName=SB Player,Firmware=9.0.0-r1583,alc,aac,mp3"


def _helo_frame(mac: str, caps: str = CAPS) -> bytes:
    """ASCII-HELO des Squeezelite-/SB-Player-Formats (protocol.py:1202-1234)."""
    body = (
        bytes([4, 0])
        + bytes.fromhex(mac.replace(":", ""))
        + bytes(16)
        + struct.pack(">H", 0)
        + struct.pack(">I", 0)
        + struct.pack(">I", 0)
        + b"en"
        + caps.encode()
    )
    return b"HELO" + struct.pack(">I", len(body)) + body


def _client_frame(opcode: str, payload: bytes = b"") -> bytes:
    """Player->Server-Rahmen: 4-Byte-Opcode + 4-Byte-BE-Laenge + Payload."""
    return opcode.encode("ascii") + struct.pack(">I", len(payload)) + payload


async def _frame_from_server(reader: asyncio.StreamReader) -> bytes:
    """Ein Server->Player-Rahmen: 2-Byte-Laenge (inkl. Opcode) + Rahmen."""
    header = await reader.readexactly(2)
    length = int.from_bytes(header, "big")
    return await reader.readexactly(length)


async def _drain_until_eof(reader: asyncio.StreamReader, timeout: float = 2.0) -> bool:
    """Alle gepufferten Server-Frames lesen bis EOF; True bei EOF."""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not data:
                return True
    except (asyncio.TimeoutError, ConnectionError, OSError):
        return False


def _cleanup() -> None:
    PlayerManager().players.clear()
    notifications.reset()


async def _server():
    client = SlimProtoClient()
    server = await asyncio.start_server(client._handle_player, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return client, server, port


async def _connect(port: int, mac: str = MAC):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_helo_frame(mac))
    await writer.drain()
    await asyncio.sleep(0.25)
    return reader, writer


# ── Perl :1195 — der alte Socket wird beim HELO geschlossen ────────────────


def test_second_helo_closes_previous_socket_and_reregisters():
    """Eine wiederkehrende MAC darf nur EINEN lebenden Socket im Register
    haben (Perl :1195): der alte wird geschlossen, der Register zeigt auf den
    NEUEN — sonst geht der naechste strm an eine tote Adresse."""
    async def run():
        client, server, port = await _server()
        first_reader, first_writer = await _connect(port)
        second_reader, second_writer = await _connect(port)

        # Der Server hat den ersten Socket selbst geschlossen.
        assert await _drain_until_eof(first_reader), (
            "der alte Socket muss beim HELO geschlossen werden")

        assert client._player_connections.get(KEY) == 1, (
            "Perl haelt genau einen tcpsock pro Client — kein Zaehler > 1")
        assert client._player_writers.get(KEY) is not None
        assert PlayerManager().get_player(MAC).connected is True

        # Der neue Socket ist der adressierte: ein strm landet dort (die
        # HELO-Antworten vers/setd/audg/aude davor ueberspringen).
        assert await client.send_stop_to_player(MAC) is True
        frame = b""
        for _ in range(10):
            frame = await asyncio.wait_for(_frame_from_server(second_reader),
                                           timeout=2)
            if frame[:4] == b"strm":
                break
        assert frame[:4] == b"strm" and frame[4:5] == b"q"

        second_writer.close()
        await second_writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_superseded_socket_close_does_not_disconnect_the_player():
    """Perl :261-296: der reconnect-Close laeuft OHNE Disconnect-Block —
    ein ersetzter Socket darf den Client nicht abmelden und keinen
    Forget-Timer armieren."""
    async def run():
        client, server, port = await _server()
        _first_reader, first_writer = await _connect(port)
        second_reader, second_writer = await _connect(port)

        # Der alte Socket verschwindet (vom Server geschlossen): der Client
        # bleibt verbunden, kein 'client disconnect', kein Forget-Task.
        first_writer.close()
        await asyncio.sleep(0.4)

        assert PlayerManager().get_player(MAC).connected is True
        assert not client._forget_tasks, "kein Forget-Timer fuer einen ersetzten Socket"
        assert client._player_writers.get(KEY) is not None

        second_writer.close()
        await second_writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_close_of_live_socket_after_reconnect_disconnects_player():
    """Regression: der ersetzte Socket darf den Zaehler nicht aufblaehen —
    sonst erreicht der Close des LEBENDEN Sockets nie 0, der Register behaelt
    einen toten Writer und ``players`` bleibt fuer immer connected."""
    async def run():
        client, server, port = await _server()
        _r1, w1 = await _connect(port)
        r2, w2 = await _connect(port)
        w1.close()
        await asyncio.sleep(0.3)

        w2.close()                      # der lebende Socket geht
        await asyncio.sleep(0.4)

        assert PlayerManager().get_player(MAC).connected is False
        assert client._player_writers.get(KEY) is None
        assert client._forget_tasks, "der Forget-Timer muss armiert sein"
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


# ── Perl :1198 — Neuanmeldung killt den Forget-Timer ──────────────────────


def test_reconnect_cancels_pending_forget_timer():
    async def run():
        client, server, port = await _server()
        _reader, writer = await _connect(port)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.3)
        assert client._forget_tasks, "nach dem Close muss der Timer laufen"

        _r2, w2 = await _connect(port)
        assert not client._forget_tasks, (
            "Perl :1198 killTimers(forget_disconnected_client) beim HELO")
        assert PlayerManager().get_player(MAC).connected is True

        w2.close()
        await w2.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


# ── Perl :199-241 — check_all_clients schliesst stumme Clients ────────────


def test_silent_client_is_closed_after_three_missed_polls(monkeypatch):
    """Ein Client, der 3 Poll-Intervalle nichts antwortet, wird geschlossen
    (Perl :218-238) — ``connected=0`` + Disconnect-Block statt einer Ewigkeit
    ``strm``-Frames in einen toten Socket."""
    monkeypatch.setattr(proto, "KEEPALIVE_SECONDS", 0.15)
    monkeypatch.setattr(proto, "KEEPALIVE_MISS_LIMIT", 3)

    async def run():
        client, server, port = await _server()
        reader, writer = await _connect(port)
        assert PlayerManager().get_player(MAC).connected is True

        # Der Client antwortet nie → der Server schliesst (Frames kommen an,
        # aber wir lesen sie nicht und senden nichts zurueck).
        for _ in range(40):
            await asyncio.sleep(0.1)
            if PlayerManager().get_player(MAC).connected is False:
                break

        assert PlayerManager().get_player(MAC).connected is False, (
            "check_all_clients muss den stummen Client abmelden")
        assert client._player_writers.get(KEY) is None
        assert client._forget_tasks, "Forget-Timer nach check_all_clients-Close"
        writer.close()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_client_answering_the_poll_stays_connected(monkeypatch):
    """Wer antwortet, bleibt (Perl :703-709 frischt $heartbeat auf)."""
    monkeypatch.setattr(proto, "KEEPALIVE_SECONDS", 0.15)
    monkeypatch.setattr(proto, "KEEPALIVE_MISS_LIMIT", 3)

    async def run():
        client, server, port = await _server()
        reader, writer = await _connect(port)

        async def answer():
            try:
                while True:
                    await _frame_from_server(reader)   # strm 't' Poll
                    # Ein beliebiger Rahmen beweist Leben (Perl: jeder Frame
                    # laeuft durch _stat_handler, :703-709).
                    writer.write(_client_frame("setd", bytes([9, 1, 0])))
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                pass

        task = asyncio.create_task(answer())
        await asyncio.sleep(1.2)
        assert PlayerManager().get_player(MAC).connected is True, (
            "ein antwortender Client darf nicht abgemeldet werden")
        task.cancel()
        writer.close()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


# ── Perl Squeezebox.pm:551 ``return 0 unless $client->opened()`` ──────────


def test_strm_to_disconnected_player_is_refused():
    """Kein Senden an getrennte Clients: ohne registrierten Socket liefert
    der Sendepfad False (Perl ``opened()``, Squeezebox.pm:504-514/:551) und
    die Playlist bleibt unveraendert."""
    async def run():
        client, server, port = await _server()
        _reader, writer = await _connect(port)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.3)

        player = PlayerManager().get_player(MAC)
        assert player.connected is False
        assert await client.send_stop_to_player(MAC) is False
        assert await client.send_remote_stream(MAC, "http://example.invalid/x.mp3") is False

        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


# ── Perl HTTP.pm:433-439 ``canDirectStream`` / mp3StreamingMethod ────────


def test_mp3_streaming_method_one_proxies_instead_of_direct(monkeypatch):
    """Der Player-Pref ``mp3StreamingMethod 1`` verlangt Proxied Streaming
    (Perl HTTP.pm:433-439: "Not direct streaming because of mp3StreamingMethod
    pref") — der strm-Frame zeigt dann auf UNSEREN Web-Port statt auf den
    Sender, also kann der Player den Umweg nehmen, wenn sein eigener
    Direktverbindungsweg zum Sender nicht funktioniert."""
    import lyrion.player.playerprefs as pres
    from lyrion.player.manager import PlayerManager
    from lyrion.player.state import PlayerState

    class FakeWriter:
        def __init__(self):
            self.frames: list[bytes] = []

        def is_closing(self) -> bool:
            return False

        def write(self, data: bytes) -> None:
            self.frames.append(data)

        async def drain(self) -> None:
            return None

    async def run():
        client = SlimProtoClient()
        client.web_port = 9000
        player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
        player.connected = True
        pm = PlayerManager()
        pm.players[MAC] = player
        writer = FakeWriter()
        client._player_writers[KEY] = writer
        client._player_connections[KEY] = 1

        assert pres.apply_player_pref(player, "mp3StreamingMethod", 1) == "applied"
        assert player.mp3_streaming_method == 1

        assert await client.send_remote_stream(
            MAC, "http://hirschmilch.de:7000/chillout.mp3", "m") is True
        assert writer.frames, "es muss ein strm-Frame rausgehen"
        payload = writer.frames[-1]
        # Rahmen: 2-Byte-Laenge (inkl. Opcode) + Opcode + strm-Koerper.
        assert payload[2:6] == b"strm"
        assert payload[6:7] == b"s"
        # Proxied Streaming: serverIp 0 („der Steuer-Server") + unser Web-Port.
        server_port = int.from_bytes(payload[24:26], "big")
        server_ip = int.from_bytes(payload[26:30], "big")
        assert server_port == 9000, "Proxy-Stream zeigt auf unseren Web-Port"
        assert server_ip == 0
        assert b"GET /stream.mp3?remote=" in payload[30:]

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
