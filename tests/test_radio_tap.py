"""Radio-Tap: ein Sender-Tap startet den Sender — er zeigt nicht die Liste.

Symptom (User): „Beim Anklicken eines Radios im Radiobrowser kommt ein
Popupmenue, das die GLEICHEN Sender wie in der Liste zeigt, und es ist
beliebig rekursiv."

Gemessene Client-Form (SqueezePlay vom 2026-09-19, cometd-Log
``/tmp/lyrion-live.log``, Tap auf die Senderzeile der Liste
``item_id:0277ed87.1``) — dieselbe Liste, 161-mal in 200er-Schritten
nachgeladen::

    ['local','items',<start>,200,'menu:local','useContextMenu:1',
     'item_id:0277ed87.1','xmlbrowserPlayControl:3','xmlBrowseInterimCM:1']

Der Tap benennt die Zeile **nicht** mit ihrer eigenen ``item_id``: er schickt
die Liste erneut und dazu den absoluten Zeilenindex aus
``playControlParams``.  Perl wertet das in
``Slim/Control/XMLBrowser.pm:805-836`` aus: ``$i = $xmlbrowserPlayControl -
$subFeed->{'offset'}``, ``$items->[$i]`` und daraus das Play-Control-Menue
dieser Zeile (``_playlistControlContextMenu``, :1811-1844) — ``noFavorites
=> 1`` (:823) und ``playalbum => 1`` (:826), also die drei
Playlist-Zeilen.  Ausserhalb des Bereichs antwortet Perl mit dem nackten
Envelope (live gegen Perl 9.1.1, read-only 2026-09-19:
``{"offset":0,"count":0,"window":{"windowStyle":"text_list"}}``).

Die Senderzeilen selbst sind fuer einen *spielenden* Client Perl's schlichte
``play``-Zeile (``goAction: "play"`` + ``style: "itemplay"`` +
``touchToPlay``, :1259-1267): ``_defeatDestructiveTouchToPlay`` liefert 0,
weil ``playingSong()->duration()`` eines Remote-Streams undef/0 ist
(``Slim/Schema/RemoteTrack.pm:483-489``).  Der Tap des Clients ist dann
``[<feed>,'playlist','play', …]`` (gemessen: ``favorites playlist play
touchToPlay:…``) und startet den Stream.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lyrion.web import radiobrowser
from lyrion.web.api import JSONRPCAPI

PLAYER = "1c:87:2c:47:fc:36"

#: Vier Sender — genug fuer ``xmlbrowserPlayControl:3`` und einen
#: Out-of-range-Fall.
STATIONS = [
    {
        "stationuuid": f"tap-{n}", "name": f"Sender {n}",
        "url": f"http://example.invalid/s{n}.m3u",
        "url_resolved": f"http://example.invalid/s{n}",
        "favicon": "", "country": "Germany", "countrycode": "DE",
        "language": "german", "tags": "pop", "codec": "MP3", "bitrate": 128,
    }
    for n in range(4)
]

COUNTRIES = [{"name": "Germany", "iso_3166_1": "DE", "stationcount": 4}]


async def _fake_get_json(path: str, params: dict | None = None) -> Any:
    if path.startswith("/json/stations/"):
        offset = int((params or {}).get("offset") or 0)
        limit = int((params or {}).get("limit") or len(STATIONS))
        return [dict(s) for s in STATIONS[offset:offset + limit]]
    if path == "/json/countries":
        return [dict(c) for c in COUNTRIES]
    return []


class _PlayingRadio:
    """Ein Client, der gerade einen Radio-Stream spielt.

    ``duration`` traegt bewusst den Wert des ZULETZT geladenen lokalen Titels
    (``manager.load_track`` setzt ihn, ``manager.play_url`` raeumt ihn nicht
    auf): genau diese Zahl liess die Portierung jeden Sender-Tap als
    „destruktiv" gelten, waehrend Perl fuer den Stream 0 liest.
    """

    mac = PLAYER
    playerprefs: dict = {}
    mode = "play"
    remote = 1
    current_url = "http://example.invalid/now"
    duration = 312.0
    playlist_total = 1
    name = "Test"


@pytest.fixture(autouse=True)
def mock_radio_browser(monkeypatch):
    monkeypatch.setattr(radiobrowser, "_get_json", _fake_get_json)
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: None)
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.delenv("LANG", raising=False)
    radiobrowser._cache.clear()
    radiobrowser._sids.clear()


@pytest.fixture()
def playing_client(monkeypatch):
    """Der Request kommt von einem bekannten, spielenden Radio-Client."""
    from lyrion.player import manager as player_manager

    monkeypatch.setattr(player_manager.PlayerManager, "get_player",
                        lambda self, pid, *a, **k: _PlayingRadio())
    return _PlayingRadio()


@pytest.fixture()
def started_streams(monkeypatch):
    """Merkt sich, welche Streams an den Player gehen (statt strm-Frame)."""
    from lyrion.player import manager as player_manager

    seen: list[tuple[str, str]] = []

    async def fake_play_url(self, player_id, url, title=""):
        seen.append((url, title))
        return True

    monkeypatch.setattr(player_manager.PlayerManager, "play_url",
                        fake_play_url)
    return seen


@pytest.fixture()
def unknown_client(monkeypatch):
    """Ein Request, den kein bekannter Client zeichnet (Perl ``!$client``)."""
    from lyrion.player import manager as player_manager

    monkeypatch.setattr(player_manager.PlayerManager, "get_player",
                        lambda self, pid, *a, **k: None)


def feed(rest: list, *, player: str | None = PLAYER) -> dict:
    return asyncio.run(JSONRPCAPI()._json_radio_feed(
        "local", [str(a) for a in rest], player))


def request(command: list, *, player: str | None = PLAYER) -> dict:
    """Der echte Dispatch-Weg (wie der Client ihn faehrt)."""
    import json

    async def run():
        return await JSONRPCAPI().handle_request(json.dumps({
            "id": 1, "method": "slim.request",
            "params": [player, [str(a) for a in command]],
        }).encode())

    return json.loads(asyncio.run(run()))["result"]


def station_list() -> tuple[str, dict]:
    """``(item_id der Liste, Senderliste)`` — der Knoten „Sender"."""
    idx = feed(["0", "6", "menu:local"])
    link = idx["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    return link, feed(["0", "4", "menu:local", f"item_id:{link}"])


# ── 1. Die Zeilenform, aus der der Client seinen Tap baut ─────────────────


def test_station_rows_stay_plain_play_rows_for_a_playing_radio_client(
        playing_client):
    """Perl :1259-1267 — ``play`` + ``itemplay`` + ``touchToPlay``.

    ``_defeatDestructiveTouchToPlay`` (:1951-1983) prueft ``:1977``
    ``isPlaying() && playingSong()->duration() && !isPlaylist()``; ein
    Remote-Stream hat keine Dauer (``RemoteTrack.pm:483-489``) ⇒ 0 ⇒ die
    schlichte Zeile.
    """
    _, stations = station_list()
    row = stations["item_loop"][0]
    assert row["goAction"] == "play"
    assert row["style"] == "itemplay"
    assert "playControlParams" not in row
    assert row["params"]["touchToPlay"] == row["params"]["item_id"]
    assert stations["base"]["actions"]["go"] == \
        stations["base"]["actions"]["play"]


def test_station_rows_are_defeated_for_an_unattributed_request(
        unknown_client):
    """Ohne Client bleibt Perl's Aussage ``|| !$client`` (:1976) gueltig."""
    _, stations = station_list()
    assert stations["item_loop"][0]["goAction"] == "playControl"


# ── 2. Der Tap des Clients: Menue der Zeile, nicht die Liste ──────────────


def tap_args(list_id: str, index: int) -> list:
    """Die gemessene Client-Form, 1:1."""
    return ["0", "200", "menu:local", "useContextMenu:1",
            f"item_id:{list_id}", f"xmlbrowserPlayControl:{index}",
            "xmlBrowseInterimCM:1"]


def test_station_tap_answers_the_rows_play_control_menu(playing_client):
    """``XMLBrowser.pm:805-836`` — drei Playlist-Zeilen fuer *diese* Zeile."""
    list_id, stations = station_list()
    row_id = stations["item_loop"][3]["params"]["item_id"]
    cm = feed(tap_args(list_id, 3))

    assert cm["count"] == 3 and cm["offset"] == 0
    assert cm["window"] == {"windowStyle": "text_list"}
    assert [r["text"] for r in cm["item_loop"]] == [
        "Am Ende hinzufügen", "Als nächstes wiedergeben", "Wiedergabe"]
    assert [r["actions"]["go"]["cmd"] for r in cm["item_loop"]] == [
        ["local", "playlist", "add"], ["local", "playlist", "insert"],
        ["local", "playlist", "play"]]
    # Kein Selbstbezug: die Zeilen tragen die id der ZEILE, nicht die der
    # Liste — und das Menue enthaelt keine Senderzeilen mehr.
    assert {r["actions"]["go"]["params"]["item_id"]
            for r in cm["item_loop"]} == {row_id}
    assert cm["item_loop"][-1]["actions"]["go"]["nextWindow"] == "nowPlaying"
    assert not any("playControlParams" in r for r in cm["item_loop"])


def test_tap_addresses_the_row_it_was_meant_for(playing_client):
    """``xmlbrowserPlayControl`` ist der absolute Zeilenindex (:808/:1271)."""
    list_id, stations = station_list()
    for index in (1, 3):
        cm = feed(tap_args(list_id, index))
        want = stations["item_loop"][index]["params"]["item_id"]
        assert cm["item_loop"][-1]["actions"]["go"]["params"]["item_id"] == want


def test_tap_out_of_range_is_perls_bare_envelope(playing_client):
    """Jenseits des Bereichs: nur ``offset``/``count``/``window`` (:813-830).

    Live-Perl-Form (read-only 2026-09-19):
    ``{"offset":0,"count":0,"window":{"windowStyle":"text_list"}}``.
    """
    list_id, _ = station_list()
    for token in ("9", "-1"):
        cm = feed(tap_args(list_id, 0)[:-1] + [f"xmlbrowserPlayControl:{token}",
                                               "xmlBrowseInterimCM:1"])
        assert cm == {"offset": 0, "count": 0,
                      "window": {"windowStyle": "text_list"}}, token


def test_tap_on_a_drill_down_row_is_not_playable(playing_client):
    """``hasAudio`` (:1823): eine Link-Zeile hat kein Play-Control-Menue.

    ``item_id`` ohne Index loest auf die *Index*-Ebene auf — dort ist die
    Zeile ``0`` die Link-Zeile „Sender" (kein ``url``/``type: audio``), also
    antwortet Perl mit dem nackten Envelope statt mit einem Menue.
    """
    idx = feed(["0", "6", "menu:local"])
    root_sid = idx["item_loop"][0]["actions"]["go"]["params"][
        "item_id"].split(".")[0]
    cm = feed(["0", "6", "menu:local", f"item_id:{root_sid}",
               "xmlbrowserPlayControl:0", "xmlBrowseInterimCM:1"])
    assert cm == {"offset": 0, "count": 0,
                  "window": {"windowStyle": "text_list"}}


# ── 3. Der Tap spielt — ueber zwei Sender ─────────────────────────────────


@pytest.mark.parametrize("index,name", [(1, "Sender 1"), (3, "Sender 3")])
def test_play_row_of_the_tap_menu_starts_that_station(
        monkeypatch, playing_client, started_streams, index, name):
    """Die „Wiedergabe"-Zeile schickt ``<feed> playlist play item_id:…``."""
    list_id, stations = station_list()
    cm = feed(tap_args(list_id, index))
    play = cm["item_loop"][-1]["actions"]["go"]
    assert play["cmd"] == ["local", "playlist", "play"]

    params = play["params"]
    command = list(play["cmd"]) + [f"{k}:{v}" for k, v in params.items()]
    assert request(command) == {}
    assert started_streams == [(STATIONS[index]["url_resolved"], name)], \
        started_streams


def test_touch_to_play_tap_starts_the_station(
        monkeypatch, playing_client, started_streams):
    """Perl :295-299: ``touchToPlay`` auf einer ``items``-Query = Playlist-Befehl.

    Ein Client, der ueber ``base.actions.go`` tappt (dort sind die Zeilen-
    ``params`` die ``itemsParams``, :960-990), schickt ``item_id`` *und*
    ``touchToPlay`` mit der ``items``-Query — Perl spielt dann (:649-675)
    statt die Info-Zeilen zu rendern.
    """
    _, stations = station_list()
    row = stations["item_loop"][2]
    station_id = row["params"]["item_id"]
    command = ["local", "items", "0", "4", "menu:local",
               f"item_id:{station_id}", f"touchToPlay:{station_id}",
               "isContextMenu:1"]
    assert request(command) == {}
    assert started_streams == [(STATIONS[2]["url_resolved"], "Sender 2")]


# ── 4. Gegenproben (kein Regress) ─────────────────────────────────────────


def test_parent_node_still_navigates_into_the_station_list(playing_client):
    """Der uebergeordnete Knoten bleibt navigierbar (zwei Link-Zeilen)."""
    idx = feed(["0", "6", "menu:local"])
    assert [r["text"] for r in idx["item_loop"]] == ["Sender", "Alle Germany"]
    for row in idx["item_loop"]:
        assert row["type"] == "link"
        assert row["actions"]["go"]["cmd"] == ["local", "items"]
        assert row["actions"]["go"]["params"]["menu"] == "local"

    list_id, stations = station_list()
    assert stations["title"] == "Sender"
    assert stations["count"] == 4
    assert len(stations["item_loop"]) == 4
    assert stations["item_loop"][0]["text"] == "Sender 0"
    assert stations["offset"] == 0


def test_long_press_on_a_station_keeps_the_interim_context_menu(playing_client):
    """``xmlBrowseInterimCM:1`` ohne ``xmlbrowserPlayControl`` bleibt gleich."""
    _, stations = station_list()
    station_id = stations["item_loop"][1]["params"]["item_id"]
    cm = feed(["0", "4", "menu:local", f"item_id:{station_id}",
               "isContextMenu:1", "xmlBrowseInterimCM:1"])
    assert [r["actions"]["go"]["cmd"] for r in cm["item_loop"][:3]] == [
        ["local", "playlist", "add"], ["local", "playlist", "insert"],
        ["local", "playlist", "play"]]
    assert cm["item_loop"][3]["actions"]["go"]["cmd"] == ["jivefavorites",
                                                          "add"]


def test_plain_leaf_query_keeps_perls_info_rows(playing_client):
    """Ohne Tap-Marker bleibt die Zeilen-Info (Titel/URL/Bitrate)."""
    _, stations = station_list()
    station_id = stations["item_loop"][0]["params"]["item_id"]
    info = feed(["0", "4", "menu:local", f"item_id:{station_id}"])
    assert info["count"] == 3
    assert info["item_loop"][0]["style"] == "itemNoAction"
    assert info["item_loop"][0]["action"] == "none"
