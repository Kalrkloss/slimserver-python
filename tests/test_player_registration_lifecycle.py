"""Player registration + lifecycle over a real SlimProto connection.

Deckt die Registrierung/Verwaltung eines ankommenden HELO ab — nicht die
PlayerState-Feldtabelle (die liegt in ``test_player_fields.py``), sondern den
Weg „Socket rein → Player in ``players``/``serverstatus`` → Notification →
Verschwinden nach der Frist":

* ``Slim/Networking/Slimproto.pm:946-1220`` — HELO legt den Client an bzw.
  nimmt den bestehenden für dieselbe MAC (``Client::getClient($id)``, :1025);
  ein bekannter Client bekommt ``disconnected(0)`` + ``['client','reconnect']``
  (:1211-1213).
* ``Slim/Player/Client.pm:313-315`` — ``['client','new']`` beim Anlegen.
* ``Slim/Networking/Slimproto.pm:255-296`` — Socket-Close (kein
  ``reconnect``-Close): ``disconnected(1)``, ``['client','disconnect']`` (:272)
  und der Forget-Timer ``$forget_disconnected_time`` = 300 s (:38, :289-296).
* ``Slim/Control/Queries.pm:2624-2663`` — Feldliste von ``players_loop``.
"""

from __future__ import annotations

import asyncio
import struct

import lyrion.control.notifications as notifications
import lyrion.networking.protocol as proto
from lyrion.control.notifications import notify_from_array
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.web.api import JSONRPCAPI

MAC_A = "02:11:22:33:44:AA"
MAC_B = "02:11:22:33:44:BB"
# jive/SqueezePlay-HELO-Caps (SqueezePlay.pm:79-85): ModelName → modelname,
# Firmware → firmware, Model → Modellklasse.
CAPS = "Model=squeezeplay,ModelName=SB Player,Firmware=9.0.0-r1583,alc,aac,mp3"


def _helo_frame(mac: str, caps: str = CAPS) -> bytes:
    """ASCII-HELO des Squeezelite-/SB-Player-Formats (protocol.py:1202-1234).

    36-Byte-Körper (deviceid, revision, mac, uuid, wlan, bytes_received_H/L,
    lang) + Caps-Text; ``unpack('n')``-Länge wie auf der Leitung.
    """
    body = (
        bytes([4, 0])                                   # deviceid, revision
        + bytes.fromhex(mac.replace(":", ""))           # mac (6 Byte)
        + bytes(16)                                     # uuid (0 → „keine")
        + struct.pack(">H", 0)                          # wlan_channellist
        + struct.pack(">I", 0)                          # bytes_received_H
        + struct.pack(">I", 0)                          # bytes_received_L
        + b"en"                                         # lang
        + caps.encode()
    )
    return b"HELO" + struct.pack(">I", len(body)) + body


async def _start_server(client: SlimProtoClient):
    server = await asyncio.start_server(client._handle_player, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _send_helo(mac: str, port: int, caps: str = CAPS):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_helo_frame(mac, caps))
    await writer.drain()
    await asyncio.sleep(0.25)          # HELO-Verarbeitung + Registrierung
    return reader, writer


def _cleanup():
    PlayerManager().players.clear()
    notifications.reset()


# ── HELO → Player in players/serverstatus ──────────────────────────────────


def test_helo_registers_player_and_players_loop_fields():
    async def run():
        client = SlimProtoClient()
        server, port = await _start_server(client)
        _reader, writer = await _send_helo(MAC_A, port)

        player = PlayerManager().get_player(MAC_A)
        assert player is not None, "ein HELO muss den Player anlegen"
        assert player.connected is True
        assert player.model == "squeezeplay"
        assert player.model_name == "SB Player"
        assert player.firmware == "9.0.0-r1583"

        res = await JSONRPCAPI()._slim_request(MAC_A, ["players", "0", "100"])
        assert res["count"] == 1
        entry = res["players_loop"][0]
        # Feldliste + Werte wie Queries.pm:2624-2663
        assert set(entry) == {
            "playerindex", "playerid", "uuid", "ip", "name", "seq_no", "model",
            "modelname", "power", "isplaying", "isplayer", "canpoweroff",
            "connected", "firmware", "displaytype",
        }
        assert entry["playerid"] == MAC_A
        assert entry["model"] == "squeezeplay"
        assert entry["modelname"] == "SB Player"
        assert entry["connected"] == 1
        assert entry["isplaying"] == 0
        assert entry["power"] == 0
        assert entry["ip"].startswith("127.0.0.1:")
        assert entry["uuid"] is None          # Null-uuid → null (Client.pm:167)

        ss = await JSONRPCAPI()._slim_request(MAC_A, ["serverstatus", "0", "100"])
        assert ss["count"] == 1
        assert [p["playerid"] for p in ss["players_loop"]] == [MAC_A]

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()          # Singleton: Reste früherer Tests liegen lassen wir nicht
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_two_players_are_both_listed():
    async def run():
        client = SlimProtoClient()
        server, port = await _start_server(client)
        _r1, w1 = await _send_helo(MAC_A, port)
        _r2, w2 = await _send_helo(MAC_B, port, caps="Model=squeezelite,ModelName=SqueezeLite")

        res = await JSONRPCAPI()._slim_request(MAC_A, ["players", "0", "100"])
        assert res["count"] == 2
        assert [p["playerid"] for p in res["players_loop"]] == [MAC_A, MAC_B]
        assert [p["model"] for p in res["players_loop"]] == ["squeezeplay", "squeezelite"]

        for w in (w1, w2):
            w.close()
            await w.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()          # Singleton: Reste früherer Tests liegen lassen wir nicht
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_connection_without_helo_creates_no_player():
    """Kein Phantom: ein Socket ohne HELO wird kein Player (protocol.py:1207)."""
    async def run():
        client = SlimProtoClient()
        server, port = await _start_server(client)
        # a) Verbindung ohne jedes Byte, b) Verbindung mit fremdem Opcode
        _r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        _r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        w2.write(b"STAT" + struct.pack(">I", 0))
        await w2.drain()
        await asyncio.sleep(0.25)

        assert PlayerManager().get_all_players() == []

        for w in (w1, w2):
            w.close()
            await w.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()          # Singleton: Reste früherer Tests liegen lassen wir nicht
    try:
        asyncio.run(run())
    finally:
        _cleanup()


# ── Verschwindender Client (Perl-Frist) ────────────────────────────────────


def test_vanished_player_stays_offline_then_is_forgotten(monkeypatch):
    """TCP-Close: Player bleibt (connected=0) und verschwindet nach der Frist.

    Perl Slimproto.pm:269-296 — ``disconnected(1)`` sofort, Forget-Timer
    ``$forget_disconnected_time`` (300 s, :38) danach.
    """
    monkeypatch.setattr(proto, "FORGET_DISCONNECTED_TIME", 0.4)

    async def run():
        client = SlimProtoClient()
        server, port = await _start_server(client)
        _reader, writer = await _send_helo(MAC_A, port)
        pm = PlayerManager()
        assert pm.get_player(MAC_A) is not None

        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)

        player = pm.get_player(MAC_A)
        assert player is not None, "während der Frist bleibt der Client bekannt"
        assert player.connected is False        # Slimproto.pm:269
        entry = (await JSONRPCAPI()._slim_request(MAC_A, ["players", "0", "100"]))
        assert entry["count"] == 0 or entry["players_loop"][0]["connected"] == 0

        await asyncio.sleep(0.6)                # Frist abgelaufen
        assert pm.get_player(MAC_A) is None, "nach der Frist vergessen (:289-296)"

        server.close()
        await server.wait_closed()

    _cleanup()          # Singleton: Reste früherer Tests liegen lassen wir nicht
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_vanished_player_after_dsco_is_forgotten_too(monkeypatch):
    """Auch nach DSCO/BYE! darf der Forget-Timer nicht verloren gehen.

    Perl entscheidet pro Socket-Close (Slimproto.pm:255-296): nur ein
    ``reconnect``-Close überspringt den Disconnect-Block. Ein Flag aus einem
    *früheren* Frame darf den Forget-Timer deshalb nicht abschalten.
    """
    monkeypatch.setattr(proto, "FORGET_DISCONNECTED_TIME", 0.3)

    async def run():
        client = SlimProtoClient()
        server, port = await _start_server(client)
        _reader, writer = await _send_helo(MAC_A, port)

        # DSCO (Ende des Datenkanals) — Perl hält die Verbindung offen
        writer.write(b"DSCO" + struct.pack(">I", 0))
        await writer.drain()
        await asyncio.sleep(0.2)
        assert PlayerManager().get_player(MAC_A) is not None

        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.6)
        assert PlayerManager().get_player(MAC_A) is None

        server.close()
        await server.wait_closed()

    _cleanup()          # Singleton: Reste früherer Tests liegen lassen wir nicht
    try:
        asyncio.run(run())
    finally:
        _cleanup()


# ── Perl-Notifications (client new/reconnect/disconnect) ───────────────────


def test_helo_and_close_fire_perl_client_notifications():
    """Client.pm:313-315 / Slimproto.pm:272 / :1211-1213.

    Ein Listener (Perl: ``listen 1``, Plugin/CLI/Plugin.pm:969-1017) muss
    erfahren, dass ein Player gekommen/gegangen ist — sonst bleibt seiner
    Client-Liste der neue Player verborgen, obwohl ``players`` ihn führt.
    """
    async def run():
        seen: list[tuple[str, str]] = []

        def capture(notification):
            seen.append((notification.client_id, notification.request_string))

        notifications.subscribe(capture)

        client = SlimProtoClient()
        server, port = await _start_server(client)

        _reader, writer = await _send_helo(MAC_A, port)
        await asyncio.sleep(0.1)
        assert (MAC_A, "client new") in seen

        # Zweiter HELO derselben MAC — Perl nimmt den bestehenden Client
        # (Slimproto.pm:1203-1213) und meldet 'client reconnect'.
        _reader2, writer2 = await _send_helo(MAC_A, port)
        await asyncio.sleep(0.1)
        assert (MAC_A, "client reconnect") in seen

        for w in (writer, writer2):
            w.close()
            await w.wait_closed()
        await asyncio.sleep(0.2)

        server.close()
        await server.wait_closed()
        return seen

    _cleanup()          # Singleton: Reste früherer Tests liegen lassen wir nicht
    try:
        seen = asyncio.run(run())
        assert (MAC_A, "client disconnect") in seen
    finally:
        _cleanup()


def test_notify_from_array_renders_the_perl_line():
    """Die Zeile, die ein CLI-Listener bekommt: ``<mac> client new``."""
    _cleanup()
    try:
        note = notify_from_array(MAC_A, ["client", "new"])
        assert note.request_string == "client new"
        assert note.client_id == MAC_A
    finally:
        _cleanup()
