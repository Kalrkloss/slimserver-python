"""Zweiter ``displaynotify`` beim Eintreffen der Stream-Metadaten (= STMu/ICY).

Perl-Kette (alles zitiert, ``/tmp/lms-full`` = public/9.1):

1. Eintreffen: ``Slim/Player/Protocols/HTTP.pm:271-357`` (``parseMetadata``,
   ICY ``StreamTitle``) ruft ``Slim::Music::Info::setCurrentTitle($url,
   $newTitle, $client)`` (:333-334; bei schon vorhandenem ``metaTitle``
   verzögert über ``setDelayedCallback``, :350-356).  Der Port bekommt dieselbe
   Information über den ``meta``/STMu-Frame des Players
   (``networking/protocol.py:_handle_stmu_frame``).
2. ``Slim/Music/Info.pm:513-553`` (``setCurrentTitle``) handelt **nur bei
   geändertem Titel** (:516) und dann: ``$client->metaTitle($title)`` (:527),
   ``$everybuddy->update()`` für jedes Mitglied der Sync-Gruppe (:529-531),
   ``notifyFromArray($client, ['playlist','newsong',$title])`` (:534).
3. ``$client->update()`` = ``Player.pm:152`` → ``Slim/Display/Display.pm:141``
   → bei ``notifyLevel == 2`` (Abonnent mit etwas anderem als
   ``subscribe:showbriefly``, ``Queries.pm:1758-1763``) ``$display->notify
   ('update')`` (``Display.pm:214-217``) → ``displaynotify`` (:905-913).
4. ``displaystatusQuery_filter`` (``Queries.pm:1592-1629``) reicht nur an
   passende Abos weiter (:1610) und verlangt für ``update`` eine geänderte
   Anzeige (:1618); ``displaystatusQuery`` antwortet aus dem
   Display-Cache (``:1650-1651``) mit ``{text => $screen1->{'line'}}``
   (:1693-1700).

Live Perl 9.1.1 (192.168.1.90:9000) wurde nur lesend benutzt; am 2026-09-14
spielte kein Client einen Stream mit wechselnden ICY-Titeln, ein Live-Mitschnitt
des ``update``-Events war deshalb nicht möglich — die Zeilen-/Typ-Form stammt
wörtlich aus ``Queries.pm``/``Display.pm``.
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.networking import protocol as protocol_mod
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI
from lyrion.web.cometd import CometdManager

MAC = "00:04:20:2B:88:C8"
STREAM = "http://strm112.1.fm/ambientpsy_mobile_mp3"
ICY = "Dj Fada 2 - Life Breath (oct12)"

FRAME = ("StreamTitle='" + ICY + "'").encode() + b"\x00"


def _run(coro):
    return asyncio.run(coro)


def _player(**kw) -> PlayerState:
    base = dict(
        mac=MAC, name="Squeezebox Radio", ip="192.168.1.127", port=47413,
        model="baby", model_name="Squeezebox Radio", connected=True,
        power=True, mode="play", playlist=[STREAM], playlist_position=0,
        current_url=STREAM, current_title=ICY, remote=1,
    )
    base.update(kw)
    return PlayerState(**base)


def _pm(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in (players or (_player(),))}
    return pm


class _StubRPC:
    """The JSON-RPC API the cometd manager stores/dispatches through."""

    def __init__(self) -> None:
        self.api = JSONRPCAPI()

    async def handle_request(self, body: bytes) -> bytes:
        return await self.api.handle_request(body)

    def set_pending_display(self, jive, kind="showbriefly", duration=None,
                            ttl=15.0):
        self.api.set_pending_display(jive, kind, duration, ttl)


def _manager_with_sub(subscribe: str):
    rpc = _StubRPC()
    mgr = CometdManager(rpc)
    client = mgr.handshake()
    channel = f"/{client.client_id}/slim/displaystatus/{MAC}"
    client.subscriptions[channel] = {
        "request": [MAC, ["displaystatus", f"subscribe:{subscribe}"]],
        "response": channel,
    }
    return rpc, mgr, client


# ---------------------------------------------------------------------------
# 1. STMu/ICY-Metadaten lösen den Push aus (Info.pm:516)
# ---------------------------------------------------------------------------

def test_stmu_frame_pushes_the_display_only_on_a_changed_title(monkeypatch):
    """``Slim/Music/Info.pm:516`` — nur ein **neuer** Titel löst den Push aus."""
    _pm(_player(current_title=""))
    calls: list[tuple] = []

    # ``_notify_metadata_display(mac, url, title)`` — die zwei Werte sind
    # ``$url``/``$title`` des ``setCurrentTitle($url,$title,$client)``-Aufrufs
    # (``Info.pm:513-534``).
    async def _fake(mac: str, url: str = "", title: str = "") -> None:
        calls.append((mac, url, title))

    monkeypatch.setattr(protocol_mod, "_notify_metadata_display", _fake)
    client = SlimProtoClient()

    async def main():
        client._handle_stmu_frame(MAC, FRAME)
        await asyncio.sleep(0)          # die geplante Task ausführen
        client._handle_stmu_frame(MAC, FRAME)   # gleicher Titel → nichts
        await asyncio.sleep(0)

    _run(main())

    player = PlayerManager().get_player(MAC)
    assert player is not None
    assert player.current_title == ICY                 # wie bisher gesetzt
    assert player.remote_meta["artist"] == "Dj Fada 2"
    assert player.remote_meta["title"] == "Life Breath (oct12)"
    # Genau EIN Push, und zwar mit dem Stream-URL und dem neuen ICY-Titel.
    assert calls == [(MAC, STREAM, ICY)], calls


def test_stmu_without_a_known_player_pushes_nothing(monkeypatch):
    PlayerManager().players = {}
    calls: list[tuple] = []

    async def _fake(mac: str, url: str = "", title: str = "") -> None:
        calls.append((mac, url, title))

    monkeypatch.setattr(protocol_mod, "_notify_metadata_display", _fake)
    _run(_async_handle(SlimProtoClient(), MAC, FRAME))
    assert calls == []


async def _async_handle(client: SlimProtoClient, mac: str, payload: bytes):
    client._handle_stmu_frame(mac, payload)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 2. Der Push selbst: displaynotify `update` auf dem displaystatus-Kanal
# ---------------------------------------------------------------------------

def test_metadata_push_answers_perls_update_record(monkeypatch):
    """``Display.pm:214-217`` → ``Queries.pm:1610/1650-1651/1693-1700``."""
    player = _player()
    _pm(player)
    rpc, mgr, client = _manager_with_sub("all")
    monkeypatch.setattr("lyrion.web.cometd.get_manager", lambda: mgr)

    _run(protocol_mod._notify_metadata_display(MAC))

    assert len(client.events) == 1, client.events
    event = client.events[0]
    assert event["channel"] == f"/{client.client_id}/slim/displaystatus/{MAC}"
    assert event["data"]["type"] == "update"
    # ``displaystatusQuery`` gibt beim Typ != 'showbriefly' die Zeilen des
    # Displays aus (:1650-1651 + :1693-1700) — die beiden Now-Playing-Zeilen.
    assert event["data"]["display"]["text"] == ["Now Playing", ICY]
    assert "duration" not in event["data"]["display"]

    # Ein Poll derselben Subskription antwortet denselben Record (privateData
    # bis zur nächsten displaynotify, :1616-1620).
    polled = _run(rpc.api._slim_request(MAC, ["displaystatus", "0", "2"]))
    assert polled == event["data"]


def test_metadata_push_skips_showbriefly_subscribers(monkeypatch):
    """``Queries.pm:1610`` — ``subscribe:showbriefly`` bekommt kein ``update``.

    Der Typ steht als ``notifyLevel``: ``showbriefly`` → 1 (nur showBriefly),
    alles andere → 2 (auch ``update``), ``Queries.pm:1758-1763``.
    """
    _pm(_player())
    _rpc, mgr, client = _manager_with_sub("showbriefly")
    monkeypatch.setattr("lyrion.web.cometd.get_manager", lambda: mgr)

    _run(protocol_mod._notify_metadata_display(MAC))

    assert client.events == []


def test_metadata_push_covers_the_sync_group(monkeypatch):
    """``Slim/Music/Info.pm:529-531`` — ``syncGroupActiveMembers``.

    Perl ruft ``update()`` für **jedes** Mitglied der Sync-Gruppe (der Master
    UND seine Slaves), also feuert jeder von ihnen seine eigene
    ``displaynotify`` auf seinem ``displaystatus``-Kanal.
    """
    master = _player()
    slave = _player(mac="00:04:20:2B:88:C9", name="Kitchen",
                    sync_master=MAC)
    master.sync_slaves = [slave.mac]
    _pm(master, slave)

    sent: list[tuple] = []

    class _Mgr:
        async def notify_display(self, player_id, kind, jive, duration=None):
            sent.append((player_id, kind, jive, duration))

    monkeypatch.setattr("lyrion.web.cometd.get_manager", lambda: _Mgr())

    _run(protocol_mod._notify_metadata_display(MAC))

    assert [s[0] for s in sent] == [MAC, "00:04:20:2B:88:C9"], sent
    for _pid, kind, jive, duration in sent:
        assert kind == "update" and duration is None
        assert jive == {"text": ["Now Playing", ICY]}


def test_metadata_push_is_skipped_without_a_running_loop(monkeypatch):
    """Der Frame-Loop ist synchron — ohne Loop (Unit-Test) passiert nichts."""
    _pm(_player())
    protocol_mod.SlimProtoClient._notify_metadata_display(_player())
