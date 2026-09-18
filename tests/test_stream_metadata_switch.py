"""Senderwechsel: die Metadaten des NEUEN Streams gewinnen.

Symptom (User, 2026-09-18): beim Wechsel von einem Radio-Stream auf einen
anderen blieben die Metadaten des vorherigen Senders stehen — Titel/Interpret
aktualisierten sich nicht; ein verspäteter Metadaten-Frame des alten Streams
konnte den neuen Titel überschreiben.

Perl-Zuordnung (alles zitiert, ``/tmp/lms-ref`` = public/9.1):

1. Ein Stream UND seine In-Stream-Metadaten hängen an EINEM Objekt:
   ``Slim::Player::SongStreamController`` entsteht beim Öffnen des
   Stream-Sockets (``Slim/Player/Song.pm:690``, ``sub open``), wird als
   aktueller Stream des Controllers erst installiert, nachdem der Player den
   ALTEN schließen konnte (``StreamingController.pm:1340-1342``, "Bug 15477:
   Delayed to here so that $player->play() has the opportunity to close any old
   stream before the new one becomes available") und in ``_Stop`` (:465-468) /
   ``SongStreamController::close`` (``SongStreamController.pm:55-65``) wieder
   fallengelassen.
2. Der ALTE Titel wird beim Öffnen des NEUEN Streams verworfen:
   ``Song.pm:700`` ``$self->setStatus(STATUS_STREAMING);`` →
   ``Song.pm:702`` ``$client->metaTitle(undef);`` — ``getCurrentTitle`` fällt
   danach auf ``standardTitle`` der neuen URL zurück, weil
   ``%currentTitles{$newUrl}`` leer ist (``Info.pm:556-583``).
3. Der Frame wird der URL des JETZT streamenden Songs zugeordnet
   (``HTTP.pm:274-276`` ``Slim::Player::Playlist::url($client,
   Slim::Player::Source::streamingSongIndex($client))``) und der Titel wird pro
   URL zwischengespeichert/verglichen: ``Info.pm:516``
   ``if (getCurrentTitle($client, $url) ne ($title || ''))``, ``:552``
   ``$currentTitles{$url} = $title;`` — der Titel eines ersetzten Streams ist
   nie die Vergleichsbasis des neuen.
4. Ein Metadaten-Frame OHNE offenen Stream fällt weg:
   ``Slim/Networking/Slimproto.pm:908-919`` (META) →
   ``StreamingController.pm:2348-2361`` → ``Squeezebox2.pm:819-823``
   ``my $controller = $client->controller()->songStreamController() || return;``.

Der Port hatte beides nicht: ``remote_meta`` überlebte den Senderwechsel (LIVE
2026-09-18 19:23: Hirschmilch → "Absolut relax" ließ ``remoteMeta`` =
{title:'Sea Surfaces', artist:'Martin Nonstatic', url:'…hirschmilch.de…'}
stehen) und ``_handle_stmu_frame`` schrieb jeden Frame auf
``player.current_url`` — ohne jede Zuordnung zu dem Stream, aus dem er kam.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from lyrion.formats import stream_probe
from lyrion.formats.stream_probe import StreamScan
from lyrion.networking import protocol as protocol_mod
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player import streaming
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"

STATION_A = "http://hirschmilch.de:7000/chillout.mp3"
NAME_A = "Hirschmilch Chillout"
TITLE_A = "Martin Nonstatic - Sea Surfaces"

STATION_B = "https://edge11.live-sm.absolutradio.de/absolut-relax/stream/mp3"
NAME_B = "Absolut relax (Easy Listening)"
TITLE_B = "Ayla - Ayla (Part 1)"


def _frame(stream_title: str) -> bytes:
    return ("StreamTitle='" + stream_title + "';").encode() + b"\x00"


@pytest.fixture(autouse=True)
def _clean_streaming():
    streaming.reset()
    yield
    streaming.reset()


class _FakeWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _new_player(**kw) -> PlayerState:
    base = dict(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234,
                connected=True, power=True, mode="play")
    base.update(kw)
    return PlayerState(**base)


def _make_client(player: PlayerState):
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {player.mac: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    client = SlimProtoClient.__new__(SlimProtoClient)
    client._player_writers = {MAC_CLEAN: _FakeWriter()}
    client._player_connections = {MAC_CLEAN: 1}
    client._resp_waiters = {}
    client.web_port = 9000
    client.server_ip = "127.0.0.1"
    return client, pm


def _playing_station(player: PlayerState, url: str, name: str,
                     title: str) -> None:
    """Put ``player`` on a running station whose stream reported ``title``."""
    player.playlist = [url]
    player.playlist_position = 0
    player.playlist_total = 1
    player.remote = 1
    player.current_url = url
    player.current_title = name
    player.strm_sent_at = time.time() - 30.0
    player.stream_source_ready = True
    player.stream_baseline_title = name      # Perl standardTitle($client, $url)
    player.stream_meta_epoch = player.stream_epoch
    player.stream_meta_url = url
    player.remote_meta = {
        "title": title.split(" - ", 1)[-1],
        "artist": title.split(" - ", 1)[0],
        "streamtitle": title,
        "url": url,
    }
    player.current_title = title


# ---------------------------------------------------------------------------
# 1. Der Senderwechsel verwirft die Metadaten des vorherigen Senders
#    (Perl Song.pm:700-702 ``$client->metaTitle(undef)``)
# ---------------------------------------------------------------------------

def test_new_stream_drops_the_previous_senders_metadata():
    player = _new_player()
    _playing_station(player, STATION_A, NAME_A, TITLE_A)
    client, _pm = _make_client(player)

    # Der Wechsel geht über das strm-Frame (jeder Streamstart passiert den
    # ``stream_s``-Tail, Squeezebox.pm:567) — hier der direkte Radio-Stream.
    assert asyncio.run(client.send_remote_stream(MAC, STATION_B)) is True

    assert player.remote_meta == {}, "der alte Sender darf nicht weiterleben"
    assert player.stream_meta_epoch == 0
    assert player.stream_meta_url == ""
    assert player.stream_source_ready is False
    assert player.current_url == STATION_B


def test_play_url_replaces_the_previous_station_metadata(monkeypatch):
    """Der Favoriten-/Senderwechsel (``PlayerManager.play_url``) räumt auf.

    LIVE 2026-09-18 19:23:47: Hirschmilch (ICY-Titel) → "Absolut relax"
    (Server-Proxy) ließ ``remoteMeta`` = {title:'Sea Surfaces',
    artist:'Martin Nonstatic', url:'http://hirschmilch.de:7000/chillout.mp3'}
    stehen — der Status meldete weiter den ALTER Sender.
    """
    async def fake_scan(url, timeout=15.0, _depth=0):
        return StreamScan(url=url, status=200, type="mp3", bitrate=128000.0,
                          headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr(stream_probe, "scan_stream_url", fake_scan)

    player = _new_player()
    _playing_station(player, STATION_A, NAME_A, TITLE_A)
    client, pm = _make_client(player)
    pm._protocol_handler = client          # der echte Slimproto-Pfad

    assert asyncio.run(pm.play_url(MAC, STATION_B, NAME_B)) is True

    assert player.remote_meta == {}
    assert player.current_url == STATION_B
    assert player.current_title == NAME_B      # Perl standardTitle-Basis
    assert player.stream_meta_epoch != player.stream_epoch or \
        player.stream_meta_epoch == 0


# ---------------------------------------------------------------------------
# 2. Ein verspätetes STMu des ALTEN Streams wird verworfen
#    (Perl: der neue Stream wird erst installiert, wenn der Player den alten
#     schließen konnte — StreamingController.pm:1340-1342)
# ---------------------------------------------------------------------------

def test_late_stmu_of_the_previous_stream_is_discarded(caplog):
    player = _new_player()
    _playing_station(player, STATION_A, NAME_A, TITLE_A)
    client, _pm = _make_client(player)

    # Der Wechsel: strm-Frame geht raus, der Player hat die Quellverbindung des
    # neuen Streams noch nicht gemeldet (kein RESP).
    assert asyncio.run(client.send_remote_stream(MAC, STATION_B)) is True
    player.current_title = NAME_B          # was ``play_url`` als Basis setzt

    with caplog.at_level(logging.INFO, logger="lyrion.networking.protocol"):
        client._handle_stmu_frame(MAC, _frame(TITLE_A))

    assert player.current_title == NAME_B, "der alte Titel darf nicht gewinnen"
    assert player.remote_meta == {}
    assert player.stream_meta_epoch == 0
    assert any("belongs to the previous stream" in str(r.getMessage())
               for r in caplog.records), caplog.text


def test_stmu_of_the_new_stream_is_applied_once_the_source_connected():
    """Nach dem RESP des neuen Streams zählt dessen Titel (Perl :516 ff.)."""
    player = _new_player()
    _playing_station(player, STATION_A, NAME_A, TITLE_A)
    client, _pm = _make_client(player)
    pushed: list[tuple] = []

    async def _fake_notify(mac, url="", title=""):
        pushed.append((mac, url, title))

    old = protocol_mod._notify_metadata_display
    protocol_mod._notify_metadata_display = _fake_notify
    try:
        assert asyncio.run(client.send_remote_stream(MAC, STATION_B)) is True
        # Der Player öffnet die Quelle und meldet ihre Header (RESP).
        client._handle_resp_frame(MAC, b"icy-metaint: 8192\r\n")

        async def main():
            client._handle_stmu_frame(MAC, _frame(TITLE_B))
            await asyncio.sleep(0)

        asyncio.run(main())
    finally:
        protocol_mod._notify_metadata_display = old

    assert player.current_title == TITLE_B
    assert player.remote_meta["title"] == "Ayla (Part 1)"
    assert player.remote_meta["artist"] == "Ayla"
    assert player.remote_meta["url"] == STATION_B
    assert player.stream_meta_url == STATION_B
    assert player.stream_meta_epoch == player.stream_epoch
    assert pushed == [(MAC, STATION_B, TITLE_B)]


def test_stmu_without_an_open_stream_is_dropped(caplog):
    """Perl ``songStreamController() || return`` (Squeezebox2.pm:819-823)."""
    player = _new_player(current_url=None, remote=0, mode="stop")
    client, _pm = _make_client(player)

    with caplog.at_level(logging.INFO, logger="lyrion.networking.protocol"):
        client._handle_stmu_frame(MAC, _frame(TITLE_A))

    assert player.remote_meta == {}
    assert player.current_title == ""
    assert any("no stream open" in str(r.message) for r in caplog.records), \
        caplog.text


# ---------------------------------------------------------------------------
# 3. Zuordnung: Titel-Basis ist der Titel DIESER URL (Info.pm:516/:552)
# ---------------------------------------------------------------------------

def test_a_title_of_the_previous_epoch_is_not_the_new_streams_baseline():
    """Ein Frame nach dem RESP ist ein Titel des NEUEN Streams.

    ``getCurrentTitle($client, $url)`` (Info.pm:556-583) liefert für die neue
    URL ``standardTitle`` — der Titel des ersetzten Streams ist dort nie die
    Basis, der Frame ist also eine Änderung und darf den neuen Sender benennen.
    """
    player = _new_player()
    _playing_station(player, STATION_A, NAME_A, TITLE_A)
    client, _pm = _make_client(player)
    pushed: list[tuple] = []

    async def _fake_notify(mac, url="", title=""):
        pushed.append((mac, url, title))

    old = protocol_mod._notify_metadata_display
    protocol_mod._notify_metadata_display = _fake_notify
    try:
        assert asyncio.run(client.send_remote_stream(MAC, STATION_B)) is True
        client._handle_resp_frame(MAC, b"icy-metaint: 8192\r\n")
        player.current_title = NAME_B          # play_url setzt die Senderbasis

        async def main():
            client._handle_stmu_frame(MAC, _frame(TITLE_A))
            await asyncio.sleep(0)

        asyncio.run(main())
    finally:
        protocol_mod._notify_metadata_display = old

    assert player.current_title == TITLE_A
    assert player.remote_meta["url"] == STATION_B     # an den NEUEN Stream gebunden
    assert player.stream_meta_epoch == player.stream_epoch
    assert pushed == [(MAC, STATION_B, TITLE_A)]


def test_a_repeated_title_of_the_same_stream_fires_no_second_newsong():
    """``Info.pm:516`` — nur ein geänderter Titel löst den Push aus."""
    player = _new_player(current_url=STATION_A, remote=1)
    player.strm_sent_at = time.time() - 30.0
    player.stream_source_ready = True
    client, _pm = _make_client(player)
    pushed: list[tuple] = []

    async def _fake_notify(mac, url="", title=""):
        pushed.append((mac, url, title))

    old = protocol_mod._notify_metadata_display
    protocol_mod._notify_metadata_display = _fake_notify
    try:
        async def main():
            client._handle_stmu_frame(MAC, _frame(TITLE_A))
            await asyncio.sleep(0)
            client._handle_stmu_frame(MAC, _frame(TITLE_A))
            await asyncio.sleep(0)

        asyncio.run(main())
    finally:
        protocol_mod._notify_metadata_display = old

    assert pushed == [(MAC, STATION_A, TITLE_A)]
    assert player.current_title == TITLE_A


def test_switching_away_drops_the_metadata_before_a_late_frame_can_win():
    """Der komplette Ablauf: Wechsel, verspätetes Frame, dann der neue Titel.

    Nach dem Wechsel ist die Basis leer (Perl ``metaTitle(undef)``), das
    verspätete Frame des alten Senders (vor dem RESP) fällt weg, und der Titel
    des neuen Senders kommt danach an — der Status zeigt NUR ihn.
    """
    player = _new_player()
    _playing_station(player, STATION_A, NAME_A, TITLE_A)
    client, _pm = _make_client(player)
    pushed: list[tuple] = []

    async def _fake_notify(mac, url="", title=""):
        pushed.append((mac, url, title))

    old = protocol_mod._notify_metadata_display
    protocol_mod._notify_metadata_display = _fake_notify
    try:
        assert asyncio.run(client.send_remote_stream(MAC, STATION_B)) is True
        player.current_title = NAME_B

        # 1) verspätetes Frame des ALTEN Streams (noch kein RESP für den neuen)
        client._handle_stmu_frame(MAC, _frame(TITLE_A))
        # 2) der neue Stream meldet seine Quelle und danach seinen Titel
        client._handle_resp_frame(MAC, b"icy-metaint: 8192\r\n")

        async def main():
            client._handle_stmu_frame(MAC, _frame(TITLE_B))
            await asyncio.sleep(0)

        asyncio.run(main())
    finally:
        protocol_mod._notify_metadata_display = old

    assert player.current_title == TITLE_B
    assert player.remote_meta == {
        "title": "Ayla (Part 1)", "artist": "Ayla",
        "streamtitle": TITLE_B, "url": STATION_B,
    }
    assert pushed == [(MAC, STATION_B, TITLE_B)]


def test_empty_stream_title_falls_back_to_the_station_name():
    """``StreamTitle=''`` = "kein Metadaten-Titel", nicht "leerer Titel".

    Perl merkt sich den leeren Titel in ``%currentTitles{$url}`` (Info.pm:552),
    gibt aber nur einen WAHREN Eintrag zurück und fällt sonst auf
    ``standardTitle($client, $url)`` zurück (:572-582) — der Status zeigt also
    den Sendernamen. LIVE sendet SUNSHINE LIVE genau dieses leere Frame
    (``StreamTitle='';``, 19:34:00) — vorher blieb unser Status danach ohne
    Titel/Interpret.
    """
    player = _new_player(current_url=STATION_A, remote=1)
    player.strm_sent_at = time.time() - 30.0
    player.stream_source_ready = True
    player.stream_baseline_title = NAME_A            # Perl standardTitle
    client, _pm = _make_client(player)
    pushed: list[tuple] = []

    async def _fake_notify(mac, url="", title=""):
        pushed.append((mac, url, title))

    old = protocol_mod._notify_metadata_display
    protocol_mod._notify_metadata_display = _fake_notify
    try:
        async def main():
            client._handle_stmu_frame(MAC, _frame(TITLE_A))
            await asyncio.sleep(0)
            client._handle_stmu_frame(MAC, _frame(""))
            await asyncio.sleep(0)

        asyncio.run(main())
    finally:
        protocol_mod._notify_metadata_display = old

    assert pushed == [(MAC, STATION_A, TITLE_A)]     # nur der echte Titel
    assert player.remote_meta == {}
    assert player.current_title == NAME_A            # standardTitle-Basis
