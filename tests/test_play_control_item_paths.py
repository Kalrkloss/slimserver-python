"""Der Play-Control-Tap folgt in JEDEM Item-Pfad derselben Perl-Bedingung.

Perl entscheidet **einmal pro Request** —

``Slim/Control/XMLBrowser.pm:800``::

    my $defeatDestructiveTouchToPlay = _defeatDestructiveTouchToPlay($request, $client);

— und liest dieses eine Boolean in *jedem* Zeilen-Pfad
(``:1259-1272``, der touch-to-play-Zweig: ``goAction``/``style``/
``playControlParams``) und einmal für das Fenster-``go`` (``:1429-1430``).
Der Feed-Loop ist für Albumtitel, Titel-Drill, Suchtreffer und die
„Musikordner“-Dateien derselbe, deshalb darf kein Item-Pfad eine eigene
Kopie der Regel haben.

Live-Beleg (read-only, 2026-09-22, gegen 192.168.1.90): derselbe Request
``browselibrary items 0 … menu:1`` antwortet mit einem *idle* Client
(``38:54:39:c9:d2:57``) in ``mode:tracks``, ``mode:tracks search:`` und
``mode:bmf`` mit ``goAction "play"`` + ``style "itemplay"`` und base
``go`` = Feed-Play-Action; mit ``defeatDestructiveTouchToPlay:1`` (bzw. ohne
Client) mit ``goAction "playControl"`` + ``playControlParams`` und base
``go`` = Feed-``playControl``-Action — in allen drei Pfaden gleich.

Diese Datei prüft das je Pfad gegen dieselbe Entscheidung
(:func:`lyrion.web.api._defeat_destructive_touch_to_play`), mit einem
installierten ``PlayerState`` für die vier Zustände spielend-lokal /
spielend-Stream / pausiert / gestoppt.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

MAC = "1c:87:2c:47:fc:36"
MAC_CLEAN = "1C872C47FC36"
TRACK_ID = 10
STREAM_URL = "http://hirschmilch.de:7000/chillout.mp3"


# ── Temp-Library (kein DB-Schreiben, kein Serverstart) ────────────────────

def _build_db(path) -> str:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT,
                             year INTEGER, url TEXT, tracknum INTEGER,
                             duration REAL, audio INTEGER DEFAULT 1,
                             content_type TEXT);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        INSERT INTO tracks (id, title, genre, year, url, tracknum, duration)
        VALUES
            (10, 'Track One', 'Rock', 2000, 'file:///m/A/one.mp3', 1, 200.0),
            (11, 'Track Two', 'Rock', 2000, 'file:///m/A/two.mp3', 2, 210.0),
            (12, 'Other Hey', 'Jazz', 2001, 'file:///m/B/other.mp3', 1, 99.0);
        INSERT INTO albums (id, title, year, artwork) VALUES
            (45, 'Album A', 2000, 'art1');
        INSERT INTO contributors (id, name) VALUES (1, 'Artist One');
        INSERT INTO tracks_albums (track, album) VALUES (10, 45), (11, 45);
        INSERT INTO tracks_contributors (track, contributor, role) VALUES
            (10, 1, 1), (11, 1, 1);
        """
    )
    con.commit()
    con.close()
    return str(path)


@pytest.fixture()
def lib_db(tmp_path, monkeypatch):
    path = _build_db(tmp_path / "lib.db")
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: path)
    return path


@pytest.fixture()
def bmf_seams(tmp_path, monkeypatch):
    """Die „Musikordner“-Listing-Nähte (Dateibaum + Kinder), ohne Dateisystem.

    Die Datei-Zeilen selbst kommen aus ``_bmf_children`` — dieselbe Funktion,
    die der echte Pfad benutzt (``media/folders``), hier auf zwei feste
    Zeilen gezogen: ein Unterordner und eine Audiodatei.
    """
    root = str(tmp_path / "Musik")
    sub = str(tmp_path / "Musik" / "Accept")
    monkeypatch.setattr(api_mod, "_bmf_music_root", lambda: root)
    monkeypatch.setattr(api_mod, "_bmf_resolve_dir",
                        lambda token, r: token if str(token).startswith(root)
                        else None)

    def children(directory: str, start: int = 0, count: int = 200):
        rows = ([{"type": "folder", "id": 90, "name": "Accept",
                  "path": sub},
                 {"type": "audio", "id": TRACK_ID, "name": "one.mp3"}]
                if directory == root else
                [{"type": "audio", "id": TRACK_ID, "name": "one.mp3"},
                 {"type": "audio", "id": 11, "name": "two.mp3"}])
        return rows[start:start + count], len(rows)

    monkeypatch.setattr(api_mod, "_bmf_children", children)
    monkeypatch.setattr(api_mod, "_bmf_window_folder_id", lambda d: "999")
    return root, sub


# ── Spielerzustände ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_players():
    """Kein Player leckt aus einem anderen Test (die Verzweigung hängt am
    Zustand des anfragenden Players, ``XMLBrowser.pm:1951-1983``)."""
    prev = getattr(PlayerManager, "_instance", None)
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    yield
    PlayerManager._instance = prev


def _install(mode: str, playlist: list, position: int = 0,
             playlist_total: int = 1) -> PlayerState:
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856)
    player.power = True
    player.mode = mode
    player.playlist = list(playlist)
    player.playlist_position = position
    player.playlist_total = playlist_total
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    return player


@pytest.fixture()
def idle_player():
    """Bekannter, gestoppter Client — Perl ``:1977`` ist falsch."""
    prev = getattr(PlayerManager, "_instance", None)
    _install("stop", [], 0, 0)
    yield
    PlayerManager._instance = prev


@pytest.fixture()
def local_playing():
    """Bekannter Client mit laufendem **lokalen** Titel (``duration()`` > 0,
    ``isPlaylist()`` falsch) — die Verzweigung, die defeated wird."""
    prev = getattr(PlayerManager, "_instance", None)
    _install("play", [TRACK_ID], 0, 1)
    yield
    PlayerManager._instance = prev


@pytest.fixture()
def stream_playing():
    """Laufender Stream: ``RemoteTrack::duration`` ist undef
    (``Slim/Schema/RemoteTrack.pm:483-489``) ⇒ nicht defeated."""
    prev = getattr(PlayerManager, "_instance", None)
    _install("play", [STREAM_URL], 0, 1)
    yield
    PlayerManager._instance = prev


@pytest.fixture()
def paused_player():
    """Pausiert ⇒ ``isPlaying()`` falsch (``StreamingController.pm:1676-1679``)."""
    prev = getattr(PlayerManager, "_instance", None)
    _install("pause", [TRACK_ID], 0, 1)
    yield
    PlayerManager._instance = prev


# ── Request-Helfer ────────────────────────────────────────────────────────

def browse(args: list, pid: str | None = MAC) -> dict:
    async def run():
        return await JSONRPCAPI()._json_browselibrary(
            "browselibrary", [str(a) for a in args], pid)
    return asyncio.run(run())


ALBUM_ARGS = ["items", "0", "10", "menu:1", "mode:tracks", "album_id:45"]
SEARCH_ARGS = ["items", "0", "10", "menu:1", "mode:tracks", "search:hey"]
BMF_ARGS = ["items", "0", "10", "menu:1", "mode:bmf"]


def row(res: dict, i: int = 0) -> dict:
    return res["item_loop"][i]


def assert_defeated(res: dict, i: int = 0) -> dict:
    """Perls defeated Zeile (``:1268-1272``) — Popup statt Direktstart."""
    it = row(res, i)
    assert it["goAction"] == "playControl", it
    assert it["playControlParams"] == {
        "xmlbrowserPlayControl": str(res["offset"] + i)}
    assert "style" not in it
    assert "touchToPlay" not in (it.get("params") or {})
    assert res["base"]["actions"]["go"] == \
        res["base"]["actions"]["playControl"]
    return it


def assert_live(res: dict, i: int = 0) -> dict:
    """Perls touch-to-play-Zeile (``:1259-1267``) — Tap spielt direkt."""
    it = row(res, i)
    assert it["goAction"] == "play", it
    assert it["style"] == "itemplay"
    assert "playControlParams" not in it
    assert res["base"]["actions"]["go"] == res["base"]["actions"]["play"]
    return it


# ── 1. Albumtitel (mode:tracks) ───────────────────────────────────────────

def test_album_track_row_uses_the_shared_decision(lib_db, idle_player,
                                                  local_playing,
                                                  stream_playing,
                                                  paused_player):
    """idle/Stream/pausiert → ``play``; spielender lokaler Titel → Popup."""
    idle = browse(ALBUM_ARGS)
    assert_live(idle)
    assert row(idle)["playallParams"] == {"play_index": 0}

    # derselbe Request, jetzt läuft ein lokaler Titel
    _install("play", [TRACK_ID], 0, 1)
    assert_defeated(browse(ALBUM_ARGS))
    assert_defeated(browse(ALBUM_ARGS), 1)      # paging offset → row index


def test_album_track_row_is_play_for_playing_stream_and_paused(
        lib_db, stream_playing):
    _install("play", [STREAM_URL], 0, 1)
    assert_live(browse(ALBUM_ARGS))
    _install("pause", [TRACK_ID], 0, 1)
    assert_live(browse(ALBUM_ARGS))


def test_album_track_row_paging_reports_the_absolute_index(lib_db,
                                                           local_playing):
    _install("play", [TRACK_ID], 0, 1)
    res = browse(["items", "1", "1", "menu:1", "mode:tracks",
                  "album_id:45"])
    it = row(res)
    assert it["playControlParams"] == {"xmlbrowserPlayControl": "1"}
    assert it["playallParams"] == {"play_index": 1}


def test_album_drill_window_play_is_perls_playall(lib_db, idle_player):
    """Das Album-Drill-Fenster trägt Perls ``playallParams``/``album_id``.

    Live Perl 9.1.1 (read-only 2026-09-22, ``browselibrary items 0 100
    menu:1 mode:tracks album_id:16591``)::

        base.go = base.play = {cmd:["playlistcontrol"],
                               itemsParams:"playallParams", player:0,
                               nextWindow:"nowPlaying",
                               params:{album_id:"16591", cmd:"load",
                                       menu:1, sort:"albumtrack"}}
        base.add = {… itemsParams:"addallParams", params:{album_id, cmd:"add",
                                                          menu:1,
                                                          sort:"albumtrack"}}
        item[0]  = {goAction:"play", style:"itemplay",
                    playallParams:{play_index:0}, commonParams:{track_id:…}}

    (``Slim/Control/XMLBrowser.pm:954`` wählt die ``playall``-Aktion für
    ``play``, ``:1682-1686`` benennt ``itemsParams`` nach ihr,
    ``Slim/Menu/BrowseLibrary.pm:1871`` liefert den Zeilenindex.)
    """
    res = browse(ALBUM_ARGS)
    acts = res["base"]["actions"]
    assert acts["play"] == {
        "player": 0, "cmd": ["playlistcontrol"],
        "itemsParams": "playallParams", "nextWindow": "nowPlaying",
        "params": {"cmd": "load", "album_id": "45", "sort": "albumtrack",
                   "menu": 1}}
    assert acts["add"] == {
        "player": 0, "cmd": ["playlistcontrol"],
        "itemsParams": "addallParams",
        "params": {"cmd": "add", "album_id": "45", "sort": "albumtrack",
                   "menu": 1}}
    # solange der Tap nicht defeated ist, ist ``go`` diese ``play``-Aktion
    assert acts["go"] == acts["play"]
    assert row(res)["playallParams"] == {"play_index": 0}
    assert row(res, 1)["playallParams"] == {"play_index": 1}


# ── 2. Suchtreffer (mode:tracks search:) ──────────────────────────────────

def test_search_list_window_has_no_playall_params(lib_db, idle_player):
    """Suchtrefferliste: ``play`` liest ``commonParams``, keine ``playallParams``.

    ``BrowseLibrary.pm:1964`` ``if ($search) { $actions{'playall'} =
    $actions{'play'} }`` — live Perl ``… mode:tracks search:Reprise`` →
    ``play {cmd:load, commonParams}`` und Zeilen mit den Keys
    ``{commonParams, goAction, style}`` (kein ``playallParams``).
    """
    res = browse(SEARCH_ARGS)
    acts = res["base"]["actions"]
    assert acts["play"] == {
        "player": 0, "cmd": ["playlistcontrol"],
        "itemsParams": "commonParams", "nextWindow": "nowPlaying",
        "params": {"cmd": "load", "menu": 1}}
    assert "playallParams" not in row(res)


def test_search_hit_row_uses_the_same_decision(lib_db, idle_player):
    assert_live(browse(SEARCH_ARGS))
    _install("play", [TRACK_ID], 0, 1)
    assert_defeated(browse(SEARCH_ARGS))


# ── 3. Musikordner-Datei (mode:bmf) ───────────────────────────────────────

def test_musicfolder_file_row_uses_the_same_decision(lib_db, bmf_seams,
                                                     idle_player):
    root, sub = bmf_seams
    idle = browse(BMF_ARGS + [f"folder_id:{sub}"])
    it = assert_live(idle)
    assert it["params"]["touchToPlay"] == it["params"]["item_id"]
    assert "touchToPlaySingle" not in it["params"]   # Perl: playall ⇒ kein Single

    _install("play", [TRACK_ID], 0, 1)
    defeated = browse(BMF_ARGS + [f"folder_id:{sub}"])
    it = assert_defeated(defeated)
    assert it["params"]["item_id"] == "0"
    assert "track_id" in it["params"]            # port-eigener Auflösetoken


def test_musicfolder_defeated_tap_answers_the_play_control_menu(
        lib_db, bmf_seams, local_playing):
    """Perl ``_playlistControlContextMenu`` für eine bmf-Dateizeile.

    Live Perl 2026-09-22 (``items 0 5 menu:1 mode:bmf folder_id:204573
    xmlbrowserPlayControl:0``): vier Einträge, das bmf-Feed-Vokabular
    ``browselibrary playlist add|insert|play|playall``.
    """
    root, sub = bmf_seams
    _install("play", [TRACK_ID], 0, 1)
    res = browse(BMF_ARGS + [f"folder_id:{sub}",
                             "xmlbrowserPlayControl:0",
                             "xmlBrowseInterimCM:1"])
    texts = [e["text"] for e in res["item_loop"]]
    assert texts == ["Am Ende hinzufügen", "Als nächstes wiedergeben",
                     "Diesen Titel wiedergeben", "Alle Titel wiedergeben"]
    add = res["item_loop"][0]["actions"]["go"]
    assert add["cmd"] == ["browselibrary", "playlist", "add"]
    assert add["params"]["mode"] == "bmf"
    assert add["params"]["item_id"] == "0"
    assert add["nextWindow"] == "parentNoRefresh"
    playall = res["item_loop"][3]["actions"]["go"]
    assert playall["cmd"] == ["browselibrary", "playlist", "playall"]
    assert playall["params"]["playIndex"] == "0"


def test_musicfolder_window_with_a_folder_row_keeps_the_drill_go(
        lib_db, bmf_seams, local_playing):
    """Perl ``$allTouchToPlay`` (:801/1275): gemischtes Fenster → Drill-go."""
    _install("play", [TRACK_ID], 0, 1)
    res = browse(BMF_ARGS)                       # Wurzel: Ordner + Datei
    folder_row, file_row = res["item_loop"][0], res["item_loop"][1]
    assert "goAction" not in folder_row          # Ordnerzeilen bleiben Drill
    assert file_row["goAction"] == "playControl"
    assert res["base"]["actions"]["go"]["cmd"] == ["browselibrary", "items"]


def test_musicfolder_play_control_window_keeps_its_folder_id(
        lib_db, bmf_seams, local_playing, monkeypatch):
    """Der Popup-Request bleibt im angetippten Fenster (Perls ``getParamsCopy``).

    Perl beantwortet ``base.actions.playControl`` mit der **Param-Kopie des
    Requests** (``Slim/Control/XMLBrowser.pm:805-812``), also inklusive des
    Drill-Tokens des Fensters.  Live Perl 9.1.1 (read-only 2026-09-22)::

        browselibrary items 0 3 menu:1 mode:bmf folder_id:204573
          → playControl.params {_index:"0", _quantity:"3", menu:"1",
                               mode:"bmf", folder_id:"204573"}
        browselibrary items 0 6 menu:1 mode:bmf            (Wurzel)
          → playControl.params {_index:"0", _quantity:"6", menu:"1",
                               mode:"bmf"}                 (kein folder_id)

    Ohne das Token nennt der Popup-Request kein Fenster mehr: der Server
    beantwortet ihn für die **Wurzel**, dort ist Zeile N eine Ordnerzeile, und
    „Diesen Titel wiedergeben“ startete ein Verzeichnis statt der angetippten
    Datei (Stream bricht ab, Player bleibt leer/gestoppt) — der gemeldete
    Fehler „Musikordner-Dateien spielen nicht“.
    """
    root, sub = bmf_seams
    # ``_bmf_window_folder_id`` liefert die ``tracks.id`` des Verzeichnisses
    # (Perls ``folder_id``); ``_bmf_resolve_dir`` löst sie wie der echte Pfad
    # über die Verzeichniszeile wieder auf (``_folder_dir_by_id``).
    monkeypatch.setattr(
        api_mod, "_bmf_resolve_dir",
        lambda token, r: (str(token) if str(token).startswith(root)
                          else sub if str(token) == "999" else None))

    _install("play", [TRACK_ID], 0, 1)           # spielender lokaler Titel

    # 1. das Fenster trägt den Drill-Token in seinen playControl-Params
    window = browse(BMF_ARGS + [f"folder_id:{sub}"])
    assert window["base"]["actions"]["playControl"]["params"]["folder_id"] == "999"
    # die Wurzel trägt keinen (Perl-Parität)
    assert "folder_id" not in browse(BMF_ARGS)["base"]["actions"]["playControl"]["params"]

    # 2. der Popup-Request MIT Token adressiert die angetippte Datei …
    popup = browse(["items", "0", "200", "_index:0", "_quantity:200", "menu:1",
                    "xmlBrowseInterimCM:1", "xmlbrowserPlayControl:0",
                    "mode:bmf", "folder_id:999"])
    play_entry = popup["item_loop"][2]["actions"]["go"]["params"]
    assert play_entry["track_id"] == TRACK_ID          # die Datei, nicht 90
    # … und der Tap löst genau diesen Titel auf: ``_bmf_playlist`` holt die
    # Queue über ``ids = _bmf_tap_tracks(tagged)`` (api.py:9913) — derselbe
    # Aufruf, der hier geprüft wird.  Dass diese Params dann wirklich starten
    # (Queue = [102], ``mode=play``, ``strm``), pinnt
    # ``test_musicdir_bmf.test_bmf_file_tap_plays_exactly_the_tapped_track``.
    assert api_mod._bmf_tap_tracks(play_entry) == [TRACK_ID]


def test_musicfolder_folder_row_never_plays_a_directory(
        lib_db, bmf_seams, local_playing):
    """Eine Ordnerzeile gibt NIE ihre Verzeichnis-``tracks.id`` als Ziel aus.

    Perls Live-Antwort auf den Tap einer Ordnerzeile (read-only 2026-09-22,
    Wurzel-Fenster) trägt kein ``track_id`` — sie spielt über
    ``playlistcontrol {cmd:load, folder_id:…}``.  Die ``id`` einer bmf-
    Ordnerzeile ist die ``tracks``-Zeile des Verzeichnisses
    (``_bmf_dir_row_ids``); stand sie als ``track_id`` im Popup, startete
    „Diesen Titel wiedergeben“ ein Verzeichnis (Stream-Abbruch).
    """
    root, sub = bmf_seams
    _install("play", [TRACK_ID], 0, 1)
    # Wurzel-Fenster: Zeile 0 ist die Ordnerzeile "Accept" (id 90)
    popup = browse(["items", "0", "200", "menu:1", "xmlBrowseInterimCM:1",
                    "xmlbrowserPlayControl:0", "mode:bmf"])
    folder_entry = popup["item_loop"][2]["actions"]["go"]["params"]
    assert "track_id" not in folder_entry, folder_entry
    assert folder_entry["item_id"] == "0"


# ── 4. Favoritenzeile ─────────────────────────────────────────────────────

class _Favs:
    """Minimaler Favoriten-Manager: ein Ordner, ein Sender."""

    def __init__(self) -> None:
        self.items: dict[Any, list] = {
            None: [{"id": 1, "type": "folder", "title": "Chill",
                    "icon": "html/images/favorites.png"}],
            1: [{"id": 2, "type": "stream", "title": "Station",
                 "url": STREAM_URL, "icon": "html/images/radio.png"}],
        }

    async def list_items(self, parent=None):
        return list(self.items.get(parent, []))

    async def resolve_path(self, path):
        """Perl-Session-Pfad ``<sid>.<idx>[.<idx>]`` → DB-Id (wie der Server)."""
        crumbs = [c for c in str(path).split(".") if c]
        if crumbs and not crumbs[0].isdigit():
            crumbs = crumbs[1:]
        parent: Any = None
        for crumb in crumbs:
            if not str(crumb).isdigit():
                return None
            items = await self.list_items(parent)
            idx = int(crumb)
            if idx >= len(items):
                return None
            parent = int(items[idx]["id"])
        return parent


@pytest.fixture()
def favs(monkeypatch):
    fake = _Favs()
    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: fake)
    return fake


def fav_rows(args: list, pid: str | None = MAC) -> dict:
    async def run():
        return await JSONRPCAPI()._json_favorites_items(
            pid, [str(a) for a in args])
    return asyncio.run(run())


def _station_args(favs) -> list:
    root = fav_rows(["items", "0", "20", "menu:favorites"])
    sid = root["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    return ["items", "0", "20", "menu:favorites", "item_id:" + sid]


def test_favorites_station_row_uses_the_same_decision(favs, lib_db,
                                                      idle_player):
    # ``lib_db`` ist nötig: ``_playing_item`` liest die Dauer/URL des
    # laufenden Eintrags aus der Bibliothek (sonst fällt es auf die
    # geladenen Track-Felder zurück, die kein ``play_track`` geschrieben hat).
    res = fav_rows(_station_args(favs))
    assert_live(res)
    _install("play", [TRACK_ID], 0, 1)
    assert_defeated(fav_rows(_station_args(favs)))


# ── 5. Radiozeile ─────────────────────────────────────────────────────────

class _StubPlayer:
    """Nur die Felder, die ``_defeat_destructive_touch_to_play`` liest."""

    def __init__(self, mode: str, playlist: list) -> None:
        self.mode = mode
        self.playlist = playlist
        self.playlist_position = 0
        self.playlist_total = len(playlist)
        self.playerprefs: dict = {}


@pytest.fixture()
def local_track_library(monkeypatch):
    """``_playing_item`` liest die Dauer des laufenden Eintrags aus der DB."""
    monkeypatch.setattr(api_mod, "_db_query",
                        lambda sql, params=(): (
                            [{"duration": 200.0, "url": "file:///m/A/one.mp3"}]
                            if params == (TRACK_ID,) else []))


def test_radio_station_row_uses_the_same_decision(monkeypatch,
                                                 local_track_library):
    """Auch der Radio-Feed fragt dieselbe Funktion — Ergebnis gleich."""
    from lyrion.web import radiobrowser

    monkeypatch.setattr(radiobrowser, "_cache", {})
    monkeypatch.setattr(radiobrowser, "_sids", {})
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: None)

    ours = JSONRPCAPI()
    playing_local = _StubPlayer("play", [TRACK_ID])
    idle = _StubPlayer("stop", [])

    # dieselbe Entscheidung wie im Bibliotheks-Pfad (eine Funktion, kein
    # zweiter Zweig): lokaler Titel ⇒ defeated, idle ⇒ nicht defeated
    assert api_mod._defeat_destructive_touch_to_play(
        ["0", "4", "menu:local"], playing_local, client_named=True) is True
    assert api_mod._defeat_destructive_touch_to_play(
        ["0", "4", "menu:local"], idle, client_named=True) is False
    assert asyncio.run(ours._radio_play_control([], None)) is True

    # der Feed selbst: ein spielender lokaler Titel macht die Senderzeile zur
    # Popup-Zeile, ein idle Client zur Direktstart-Zeile
    monkeypatch.setattr(PlayerManager, "get_player",
                        lambda self, pid, *a, **k: (
                            playing_local if pid else None))

    def feed(pid):
        return asyncio.run(JSONRPCAPI()._json_radio_feed(
            "local", ["0", "6", "menu:local"], pid))

    sid = feed(MAC)["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    stations = asyncio.run(JSONRPCAPI()._json_radio_feed(
        "local", ["0", "4", "menu:local", f"item_id:{sid}"], MAC))
    assert stations["item_loop"][0]["goAction"] == "playControl"
    monkeypatch.setattr(PlayerManager, "get_player",
                        lambda self, pid, *a, **k: idle if pid else None)
    stations = asyncio.run(JSONRPCAPI()._json_radio_feed(
        "local", ["0", "4", "menu:local", f"item_id:{sid}"], MAC))
    assert stations["item_loop"][0]["goAction"] == "play"


# ── 6. Gegenprobe: Kategorien/Ordner bleiben navigierbar ──────────────────

def test_category_and_folder_rows_keep_their_drill(lib_db, bmf_seams,
                                                   local_playing):
    _install("play", [TRACK_ID], 0, 1)
    for args in (["items", "0", "3", "menu:1", "mode:albums"],
                 ["items", "0", "3", "menu:1", "mode:artists"]):
        res = browse(args)
        it = row(res)
        assert "goAction" not in it
        assert it["type"] == "playlist"
        assert res["base"]["actions"]["go"]["cmd"] == ["browselibrary", "items"]
    bmf_root = browse(BMF_ARGS)
    assert "goAction" not in row(bmf_root)
    assert bmf_root["base"]["actions"]["go"]["cmd"] == \
        ["browselibrary", "items"]
