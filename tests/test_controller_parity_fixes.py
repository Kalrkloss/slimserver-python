"""Controller-Parität Runde 2 + 3 — fehlende JSON-Kommandos, Loop-Schlüssel,
``status``-Felder, ``playlist <entity> ?``, ``can ?`` und ``pref``-Keys.

Referenz ist Perl allein. Dispatch-Einträge in ``Slim/Control/Request.pm``,
Handler in ``Slim/Control/Queries.pm``; die OPML-basierten Menü-Queries
(``apps``/``radios``) entstehen pro Plugin in ``Slim/Plugin/OPMLBased.pm``.

Live gegengeprüft (nur lesend) gegen das Perl-LMS 9.1.1,
192.168.1.90:9000, Player ``ca:c8:c7:26:6d:38``, 2026-09-13 — wörtliche
``result``-Werte des JSON-RPC::

    player count ?  -> {"_count":3}
    player ip ?     -> {"_ip":"192.168.1.130:45614"}
    player model ?  -> {"_model":"squeezelite"}
    rescanprogress  -> {"rescan":0}
    years 0 2       -> {"count":65,"years_loop":[{"year":0,
                        "favorites_url":"db:year.id=0"},
                        {"favorites_url":"db:year.id=2026","year":2026}]}
    tracks 0 2      -> {"count":80218,"titles_loop":[{"id":129497,
                        "title":"!!!!!!!","genre":"Alternative",
                        "artist":"Billie Eilish","album":"WHEN WE ALL FALL
                        ASLEEP, WHERE DO WE GO?","duration":13.609}, …]}
    roles           -> {"count":6}
    apps 0 5        -> {"count":1,"appss_loop":[{"type":"xmlbrowser",
                        "cmd":"sounds","weight":90,"name":"Sounds & Effekte",
                        "icon":"plugins/Sounds/html/images/icon.png"}]}
    radios 0 10     -> {"count":10,"radioss_loop":[{"cmd":"presets",
                        "type":"xmlbrowser",
                        "icon":"/plugins/TuneIn/html/images/radiopresets.png",
                        "weight":5,"name":"Eigene Voreinstellungen"}, …]}
    status 0 100    -> Top-Level ohne album/artist/count/current_album/
                        current_artist/current_url, aber mit ``can_seek``
                        (Remote-Stream mit bekannter Bitrate UND Dauer)

Live-Probe 2026-09-13 (Runde 3, nur lesend, Perl 9.1.1, Player
``00:00:00:00:00:00``, Queue = ein 1.FM-Stream) — wörtliche Antworten::

    playlist repeat ?   -> {"_repeat":"0"}
    playlist shuffle ?  -> {"_shuffle":"0"}
    playlist tracks ?   -> {"_tracks":1}
    playlist index ?    -> {"_index":"0"}
    playlist modified ? -> {"_modified":1}
    playlist name ?     -> {"_name":"1.FM - Ambient Psychill"}
    playlist url ?      -> {"_url":null}
    playlist duration ? -> {"_duration":"0"}
    playlist artist ?   -> {"_artist":"Goabert"}
    playlist title ?    -> {"_title":"pulchra somnium"}
    playlist path ?     -> {"_path":"http://strm112.1.fm/ambientpsy_mobile_mp3"}
    playlist remote ?   -> {"_remote":1}
    playlist album ?    -> {}
    playlist genre ?    -> {}
    can ?               -> {"_can":0}
    can play ?          -> {"_can":1}
    pref ?              -> {"_p2":null}
    pref audiodir ?     -> {"_p2":null}
    playerpref ?        -> {"_p2":null}

Perl-Fundstellen je Kommando:

* ``player <entity> ?`` — ``playerXQuery`` ``Queries.pm:2514-2576``
  (Dispatch ``Request.pm:534-543``; ``_count`` :2529-2531, ``_<entity>``
  :2555-2573; unbekannter Client → kein Result :2553-2556).
* ``rescanprogress`` — ``rescanprogressQuery`` ``Queries.pm:3231-3357``
  (Dispatch ``Request.pm:608``; ``rescan 0`` im Idle :3355-3357).
* ``roles`` — ``rolesQuery`` ``Queries.pm:3302-3473`` (Dispatch
  ``Request.pm:634``; SQL :3393-3400, ``roles_loop`` :3433-3460, ``count``
  zuletzt :3471).
* ``years`` — ``yearsQuery`` ``Queries.pm:4949-5056`` (Dispatch
  ``Request.pm:632``; ``years_loop`` :5051-5054, ``count`` :5055).
* ``tracks`` — derselbe Handler wie ``songs``/``titles`` (``titlesQuery``),
  Dispatch ``Request.pm:629``.
* ``apps``/``radios`` — OPML-Basismenü: Dispatch pro Plugin
  ``Slim/Plugin/OPMLBased.pm:26-28`` + :125-132, Item-Form :252-258,
  Loop-Name ``<query>s_loop`` und ``count`` zuletzt über
  ``dynamicAutoQuery`` ``Queries.pm:5384``/``:5443``.
* ``musicfolder`` — ``mediafolderQuery`` ``Queries.pm:2169-2507`` (Alias
  ``musicfolderQuery`` :2161-2167), media dirs ``Misc.pm:727-756``,
  ``folder_loop`` :2384-2470, ``count`` :2507.
* ``status`` — ``statusQuery`` ``Queries.pm:3996-4500``: ``current_title``
  nur beim Remote-Stream (:4084-4090), ``can_seek`` :4104-4107 über
  ``Song.pm:849-870`` → ``Protocols/HTTP.pm:1150-1165`` (Bitrate UND Dauer)
  bzw. Formatklassen; ``count``/``offset`` nur im ``menuMode``
  (:4325-4332, :4401).
* ``playlist <entity> ?`` — ``playlistXQuery`` ``Queries.pm:2708-2772``
  (Dispatch ``Request.pm:548-591``): ``repeat``/``shuffle``
  :2724-2725/:2727-2728, ``index``/``jump`` :2730-2731, ``name``
  :2733-2734 + ``remote_title`` :2766-2767, ``url`` :2736-2738
  (``Client.pm:1212-1228``), ``modified`` :2740-2741, ``tracks``
  :2743-2744, ``path`` :2746-2748, ``remote`` :2750-2753,
  ``title``/``duration``/``artist``/``album``/``genre`` :2755-2768 über
  ``_songData`` (Tags ``dalgN``); der Index kommt aus ``_index`` bzw. dem
  laufenden Titel (``Playlist.pm:62-73``).
* ``can ?`` — ``canQuery`` ``Slim/Plugin/CLI/Plugin.pm:751-797``
  (Dispatch ``Request.pm:400-401``): ``_can`` 0/1 als Zahl (:783, :791).
* ``pref``/``playerpref`` — ``prefQuery`` ``Queries.pm:3009-3051``
  (Dispatch ``Request.pm:544``/``:602``): Ergebnisschlüssel immer ``_p2``
  (:3045-3048), unbekannte Pref = undef → JSON null.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from lyrion.control import cli_commands as cli_mod
from lyrion.media import folders as folders_mod
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

MAC = "1C:87:2C:47:FC:36"
STREAM_URL = "http://stream.example.org:8000/live.mp3"

PLAYER: dict = dict(
    mac=MAC,
    name="Küche",
    ip="192.168.1.130",
    port=45614,
    model="squeezelite",
    model_name="SqueezeLite",
    connected=True,
    power=True,
)


def _player(**kw) -> PlayerState:
    base = dict(PLAYER)
    base.update(kw)
    return PlayerState(**base)


def _pm(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in (players or (_player(),))}
    return pm


def _req(command: list, player: str = MAC) -> object:
    _pm()
    return asyncio.run(JSONRPCAPI()._slim_request(player, list(command)))


@pytest.fixture(autouse=True)
def _idle_scan_state(monkeypatch):
    """``SCAN_STATE`` ist ein Prozess-Singleton: eine vorherige Suite kann
    ``scanning=True`` hinterlassen, dann hängen ``rescanprogress``/``years``
    ein ``rescan``-Feld an (Queries.pm:4972-4974, :3355). Für die Formtests
    wird der Idle-Zustand fixiert."""
    from lyrion.media import scan_state

    monkeypatch.setattr(scan_state.SCAN_STATE, "snapshot",
                        lambda: {"scanning": False, "progress": 0, "total": 0})


# ----------------------------------------------------------------------
# Bibliotheks-DB (echtes Schema, kein Prod-Zugriff) — years/roles/tracks
# ----------------------------------------------------------------------

def _lib_db(tmp_path, monkeypatch) -> str:
    path = tmp_path / "lyrion.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY, titlesort TEXT, title TEXT, url TEXT,
            duration REAL, year INTEGER, genre TEXT, content_type TEXT,
            remote INTEGER, audio INTEGER DEFAULT 1
        );
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO tracks (id, titlesort, title, url, duration, year, genre,
                            content_type, remote, audio)
        VALUES
            (1, 'creep', 'Creep', 'file:///m/1.flac', 100.5, 1993, 'Rock',
             'flac', 0, 1),
            (2, 'airbag', 'Airbag', 'file:///m/2.flac', 200.0, 1997, 'Rock',
             'flac', 0, 1),
            (3, 'lucky', 'Lucky', 'file:///m/3.flac', 150.0, 0, '',
             'flac', 0, 1);
        INSERT INTO contributors (id, name) VALUES (1, 'Radiohead'),
                                                   (2, 'Nigel Godrich');
        INSERT INTO tracks_contributors (track, contributor, role)
        VALUES (1, 1, 1), (2, 1, 1), (3, 2, 2);
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(path))
    monkeypatch.setattr(cli_mod, "_library_db_path", lambda: str(path))
    return str(path)


# ----------------------------------------------------------------------
# player <entity> ?   (playerXQuery, Queries.pm:2514-2576)
# ----------------------------------------------------------------------

def test_player_count_is_an_integer_result():
    """Perl ``clientCount()`` → ``{"_count":N}`` (Queries.pm:2529-2531).

    Live: ``player count ?`` → ``{"result":{"_count":3}}``.
    """
    pm = _pm(_player(mac="aa:00:00:00:00:01"), _player(mac="aa:00:00:00:00:02"))
    api = JSONRPCAPI()
    res = asyncio.run(api._slim_request("", ["player", "count", "?"]))
    assert res == {"_count": 2}
    assert pm.get_all_players()


@pytest.mark.parametrize("entity,expected", [
    ("id", MAC),
    ("address", MAC),
    ("name", "Küche"),
    ("model", "squeezelite"),
    ("ip", "192.168.1.130:45614"),
])
def test_player_entity_queries(entity, expected):
    """``player <entity> ?`` → ``{"_<entity>":value}`` (Queries.pm:2555-2573).

    ``ip`` ist ``ipport()`` (Queries.pm:2563) — live
    ``{"_ip":"192.168.1.130:45614"}``; ``model`` live
    ``{"_model":"squeezelite"}``.
    """
    res = _req(["player", entity, "?"])
    assert res == {f"_{entity}": expected}, res


def test_player_entity_accepts_an_explicit_mac():
    """``_IDorIndex`` direkt nach der Entität (Queries.pm:2533-2545)."""
    _pm(_player())
    res = asyncio.run(JSONRPCAPI()._slim_request(
        "", ["player", "name", MAC, "?"]))
    assert res == {"_name": "Küche"}


def test_player_entity_without_a_client_has_no_result():
    """Perl bleibt ohne ``$client`` ohne Result → ``{}`` (:2553-2556)."""
    _pm(_player())
    res = asyncio.run(JSONRPCAPI()._slim_request(
        "", ["player", "name", "aa:bb:cc:dd:ee:ff", "?"]))
    assert res == {}


def test_player_isplayer_and_canpoweroff_are_ints():
    """``isPlayer()``/``canPowerOff()`` als 0/1 (:2565-2568)."""
    res = _req(["player", "isplayer", "?"])
    assert res == {"_isplayer": 1}
    res = _req(["player", "canpoweroff", "?"])
    assert res == {"_canpoweroff": 1}


# ----------------------------------------------------------------------
# rescanprogress   (rescanprogressQuery, Queries.pm:3231-3357)
# ----------------------------------------------------------------------

def test_rescanprogress_idle_is_exactly_rescan_zero():
    """Idle: ``addResult('rescan', 0)`` — genau ein Schlüssel (:3355-3357).

    Live: ``rescanprogress`` → ``{"result":{"rescan":0}}``.
    """
    assert _req(["rescanprogress"]) == {"rescan": 0}


# ----------------------------------------------------------------------
# roles   (rolesQuery, Queries.pm:3302-3473)
# ----------------------------------------------------------------------

def test_roles_without_window_is_count_only(tmp_path, monkeypatch):
    """Ohne Index/Quantity bleibt nur ``count`` (normalize, :3471).

    Live: ``roles`` → ``{"result":{"count":6}}``.
    """
    _lib_db(tmp_path, monkeypatch)
    assert _req(["roles"]) == {"count": 2}


def test_roles_window_emits_role_ids(tmp_path, monkeypatch):
    """``roles_loop``-Items tragen ``role_id`` (num, :3447-3450)."""
    _lib_db(tmp_path, monkeypatch)
    res = _req(["roles", "0", "10"])
    assert res["count"] == 2
    assert res["roles_loop"] == [{"role_id": 1}, {"role_id": 2}]


def test_roles_tags_t_adds_role_name(tmp_path, monkeypatch):
    """``tags:t`` → zusätzlich ``role_name`` (:3447-3452)."""
    _lib_db(tmp_path, monkeypatch)
    res = _req(["roles", "0", "10", "tags:t"])
    assert res["roles_loop"][0] == {"role_id": 1, "role_name": "ARTIST"}


# ----------------------------------------------------------------------
# years   (yearsQuery, Queries.pm:4949-5056)
# ----------------------------------------------------------------------

def test_years_perl_shape(tmp_path, monkeypatch):
    """``{"count":N,"years_loop":[{"year":…,"favorites_url":"db:year.id=…"}]}``.

    Perl: DISTINCT ``year`` != '0' (Queries.pm:4974-4979), Loop
    ``years_loop`` mit ``year`` (num) und ``favorites_url`` (:5051-5054),
    ``count`` zuletzt (:5055). Live: ``years 0 2`` → ``{"count":65,
    "years_loop":[…]}``. Dieselbe Datenquelle wie der CLI-Pfad
    (``cmd_years``: ``WHERE year > 0 ORDER BY year DESC``).
    """
    _lib_db(tmp_path, monkeypatch)
    res = _req(["years", "0", "10"])
    assert res["count"] == 2
    assert res["years_loop"] == [
        {"year": 1997, "favorites_url": "db:year.id=1997"},
        {"year": 1993, "favorites_url": "db:year.id=1993"},
    ]


def test_years_sort_tag_keeps_the_loop(tmp_path, monkeypatch):
    """``sort:year`` ist ein Tagged-Parameter ohne Dispatch-Wirkung (:4971)."""
    _lib_db(tmp_path, monkeypatch)
    res = _req(["years", "0", "10", "sort:year"])
    assert set(res) == {"count", "years_loop"}
    assert len(res["years_loop"]) == 2


# ----------------------------------------------------------------------
# tracks   (titlesQuery, Request.pm:629)
# ----------------------------------------------------------------------

def test_tracks_answers_like_titles(tmp_path, monkeypatch):
    """``tracks`` ist ein Dispatch-Alias von ``songs``/``titles``.

    Live Perl: ``tracks 0 2`` → ``{"count":80218,"titles_loop":[{"id":…,
    "title":…,"genre":…,"artist":…,"album":…,"duration":…}]}``.
    """
    _lib_db(tmp_path, monkeypatch)
    res = _req(["tracks", "0", "10"])
    assert set(res) >= {"count", "titles_loop"}
    assert res["count"] == 3
    assert res["titles_loop"], "tracks must return items like titles"
    for item in res["titles_loop"]:
        assert set(item) >= {"id", "title", "duration"}


# ----------------------------------------------------------------------
# apps / radios   (OPMLBased.pm:125-132, 181-280; Queries.pm:5384/5443)
# ----------------------------------------------------------------------

def test_apps_is_count_plus_appss_loop():
    """Perl-Form ``{"count":N,"appss_loop":[…]}`` (Loop ``$query . 's_loop'``).

    Item-Form ``{cmd,name,type,icon,weight}`` (OPMLBased.pm:252-258, weight
    default 1000 :34). Live: ``apps 0 5`` → ``{"count":1,"appss_loop":[…]}}``.
    Dieser Port hat keine is_app-Plugins → count 0, aber der Loop-Schlüssel
    muss vorhanden sein.
    """
    res = _req(["apps", "0", "50"])
    assert set(res) == {"count", "appss_loop"}
    assert res["count"] == 0
    assert res["appss_loop"] == []


def test_radios_uses_the_radioss_loop_key():
    """``radios 0 10`` → ``{"count":10,"radioss_loop":[…]}}`` (Perl's form).

    Live Perl 9.1.1, read-only 2026-09-14: the plain query answers exactly the
    two keys ``count`` and ``radioss_loop`` with TuneIn's directory items
    ``{cmd,name,type,icon,weight}`` (``OPMLBased.pm:249-265``); the ``menu:``
    form answers ``count``/``item_loop``/``offset`` (``:200-247``).  The
    structure parity is covered in detail by ``tests/test_radio_structure.py``
    against ``tests/fixtures/perl_radio_structure.json``.
    """
    res = _req(["radios", "0", "10"])
    assert set(res) == {"count", "radioss_loop"}, res
    assert res["count"] == 10
    assert [i["cmd"] for i in res["radioss_loop"]] == [
        "presets", "local", "music", "sports", "news", "talk", "location",
        "language", "podcast", "search"]

    menu = _req(["radios", "0", "10", "menu:radio"])
    assert set(menu) == {"count", "item_loop", "offset"}, menu
    assert menu["count"] == 10
    assert menu["item_loop"][0]["actions"]["go"]["cmd"] == ["presets", "items"]


def test_radios_without_quantity_returns_the_full_list():
    """Without index/quantity the whole directory is answered.

    Perl's ``dynamicAutoQuery`` needs both to render a result; the Jive home
    action calls ``radios``/``radios menu:radio`` with neither and needs the
    sub-nodes (the port answered the favourites streams here before).
    """
    api = JSONRPCAPI()
    res = asyncio.run(api._json_radios("radios", ["menu:radio"]))
    assert res["count"] == 10
    assert len(res["item_loop"]) == 10
    plain = asyncio.run(api._json_radios("radios", []))
    assert len(plain["radioss_loop"]) == 10


def test_radio_items_carry_the_perl_item_keys():
    """OPMLBased item forms — plain ``cmd/name/type/icon/weight`` and the
    jive ``text/weight/icon-id/actions/window`` (live 2026-09-14)."""
    api = JSONRPCAPI()
    plain = asyncio.run(api._json_radios("radios", ["0", "10"]))
    first = plain["radioss_loop"][0]
    assert set(first) == {"cmd", "name", "type", "icon", "weight"}, first
    assert first["cmd"] == "presets" and first["weight"] == 5
    assert first["name"] == "Eigene Voreinstellungen"
    assert first["icon"] == "/plugins/TuneIn/html/images/radiopresets.png"
    assert first["type"] == "xmlbrowser"

    menu = asyncio.run(api._json_radios("radios", ["9", "1", "menu:radio"]))
    row = menu["item_loop"][0]
    assert set(row) == {"text", "weight", "icon-id", "actions", "input",
                        "window"}, row
    assert row["window"] == {"titleStyle": "album"}
    assert row["input"]["len"] == 3


def test_browse_radios_no_longer_errors():
    """``browse radios`` answers the Perl radio directory, no exception path."""
    res = _req(["browse", "radios", "0", "10"])
    assert "radioss_loop" in res
    assert res["count"] == 10


# ----------------------------------------------------------------------
# musicfolder   (mediafolderQuery, Queries.pm:2161-2507)
# ----------------------------------------------------------------------

def test_json_musicfolder_uses_the_shared_primitive(monkeypatch):
    """JSON-Pfad und CLI benutzen ``lyrion.media.folders.musicfolder_result``.

    Perl-Form: ``count`` + ``folder_loop`` mit ``id``/``filename``/``type``
    (:2384-2470, count :2507).
    """
    calls: dict = {}

    def _fake(index=0, quantity=0, **kw):
        calls.update({"index": index, "quantity": quantity, **kw})
        return {"count": 303,
                "folder_loop": [{"id": 81408, "filename": "Accept",
                                 "type": "folder"}]}

    monkeypatch.setattr(folders_mod, "musicfolder_result", _fake)
    res = asyncio.run(JSONRPCAPI()._json_musicfolder(["0", "2"]))
    assert calls["index"] == "0" and calls["quantity"] == "2"
    assert res["count"] == 303
    assert res["folder_loop"] == [{"id": 81408, "filename": "Accept",
                                   "type": "folder"}]
    # additive aliases (Jive/Material read item_loop)
    assert res["item_loop"] == res["folder_loop"]
    assert res["loop_loop"] == res["folder_loop"]


def test_musicfolder_via_jsonrpc_command_uses_primitive(monkeypatch):
    monkeypatch.setattr(
        folders_mod, "musicfolder_result",
        lambda *a, **kw: {"count": 7,
                          "folder_loop": [{"id": "file:///m/X",
                                           "filename": "X",
                                           "type": "folder"}]})
    res = _req(["musicfolder", "0", "2"])
    assert res["count"] == 7
    assert res["folder_loop"][0]["filename"] == "X"


def test_cli_musicfolder_uses_the_shared_primitive(monkeypatch):
    """``control/cli_commands.py`` ``cmd_musicfolder`` nutzt dieselbe Quelle."""
    monkeypatch.setattr(
        folders_mod, "musicfolder_result",
        lambda index=0, quantity=0, **kw: {
            "count": 303,
            "folder_loop": [{"id": 81408, "filename": "Accept",
                             "type": "folder"}]})
    lines = asyncio.run(cli_mod.cmd_musicfolder(None, None, ["0", "1"]))
    assert isinstance(lines, list) and lines
    joined = " ".join(lines)
    assert "folder_loop" in joined or "filename" in joined


def test_musicfolder_primitive_lists_a_real_directory(tmp_path, monkeypatch):
    """Primitive gegen ein echtes Verzeichnis (media dirs, Misc.pm:727-756)."""
    (tmp_path / "A-Album").mkdir()
    (tmp_path / "b.mp3").write_bytes(b"\0" * 10)
    (tmp_path / "ignore.txt").write_text("x")
    monkeypatch.setattr(folders_mod, "effective_media_dirs",
                        lambda media_type="": [str(tmp_path)])
    monkeypatch.setattr(folders_mod, "get_inactive_audio_dirs", lambda: [])
    monkeypatch.setattr(folders_mod, "_ro_connection", lambda db_path=None: None)
    folders_mod.reset_caches()
    res = folders_mod.musicfolder_result(0, 10)
    assert res["count"] == 2, res  # dir + mp3, no .txt
    names = {item["filename"] for item in res["folder_loop"]}
    assert names == {"A-Album", "b.mp3"}
    assert all(set(item) >= {"id", "filename", "type"}
               for item in res["folder_loop"])


# ----------------------------------------------------------------------
# status   (statusQuery, Queries.pm:3996-4500)
# ----------------------------------------------------------------------

class _PM:
    def __init__(self, player):
        self._player = player

    def get_all_players(self):
        return [self._player]

    def get_player(self, mac):
        return self._player if mac in (self._player.mac, None) else None


def _status_player(tmp_path, monkeypatch, playlist, position, **kw) -> PlayerState:
    import sqlite3 as _sq

    path = tmp_path / "lyrion.db"
    con = _sq.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, url TEXT,
            duration REAL, year INTEGER, tracknum INTEGER, bitrate INTEGER,
            samplerate INTEGER, bitspersample INTEGER, genre TEXT, cover TEXT,
            remote INTEGER, disc INTEGER, filesize INTEGER, comment TEXT,
            lyrics TEXT, content_type TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO tracks (id, title, url, duration, genre, content_type,
                            remote)
        VALUES (11, 'First Song', 'file:///music/first.mp3', 180.0, 'Rock',
                'mp3', 0),
               (12, 'Second Song', 'file:///music/second.mp3', 245.5, 'Rock',
                'mp3', 0);
        INSERT INTO contributors (id, name) VALUES (7, 'The Artist');
        INSERT INTO tracks_contributors (track, contributor, role)
        VALUES (11, 7, 1), (12, 7, 1);
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(path))
    p = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=58044)
    p.power = True
    p.playlist = list(playlist)
    p.playlist_position = position
    p.playlist_total = len(p.playlist)
    for key, value in kw.items():
        setattr(p, key, value)
    return p


def _status(player, args=None):
    args = args if args is not None else ["0", "100"]
    return asyncio.run(JSONRPCAPI()._json_player_status(_PM(player), MAC, args))


def test_status_has_no_top_level_song_fields(tmp_path, monkeypatch):
    """album/artist/count/current_album/current_artist/current_url gehören
    NICHT auf die Antwort-Ebene (Perl Queries.pm:4084-4090 fügt nur
    ``current_title`` bei Remote-Streams hinzu; die Songfelder hängen über die
    tags am ``playlist_loop``-Item :4425-4470)."""
    player = _status_player(tmp_path, monkeypatch, [11, 12], 0, mode="play",
                            elapsed=10.0)
    res = _status(player, ["0", "100"])
    for key in ("album", "artist", "count", "current_album",
                "current_artist", "current_url"):
        assert key not in res, f"{key} leaks to the top level: {sorted(res)}"
    # the data itself stays available on the current playlist item
    item = res["playlist_loop"][0]
    assert item["artist"] == "The Artist"
    assert "album" in item


def test_status_remote_stream_keeps_current_title_on_the_item(tmp_path,
                                                              monkeypatch):
    """Remote-Stream: ``current_title`` (Perl :4084-4090) + die current_*
    Felder am Item statt oben."""
    player = _status_player(tmp_path, monkeypatch, [STREAM_URL], 0, mode="play",
                            elapsed=5.0, current_url=STREAM_URL,
                            current_title="Radio Eins")
    res = _status(player, ["0", "100"])
    assert res["current_title"] == "Radio Eins"
    item = res["playlist_loop"][0]
    assert item["current_url"] == STREAM_URL
    assert "current_artist" in item


def test_status_count_and_offset_only_in_menu_mode(tmp_path, monkeypatch):
    """Perl: ``count``/``offset`` nur im menuMode (:4325-4332, :4401).

    Squeezer/Material fragen das Menü mit ``menu:menu`` ab — dort bleiben
    beide Felder erhalten.
    """
    player = _status_player(tmp_path, monkeypatch, [11, 12], 0, mode="play",
                            elapsed=10.0)
    plain = _status(player, ["0", "100"])
    assert "count" not in plain and "offset" not in plain
    menu = _status(player, ["-", "10", "menu:menu"])
    assert menu["count"] == len(menu["item_loop"]) == 2
    assert menu["offset"] == "0"


def test_status_can_seek_for_a_local_mp3(tmp_path, monkeypatch):
    """Lokale Datei + Formatklasse mit ``canSeek`` (MP3.pm:476,
    Song.pm:849-870) → ``can_seek`` 1 (:4104-4107)."""
    player = _status_player(tmp_path, monkeypatch, [11], 0, mode="play",
                            elapsed=10.0)
    assert _status(player, ["0", "100"])["can_seek"] == 1


def test_status_can_seek_remote_stream_with_bitrate_and_duration(tmp_path,
                                                                 monkeypatch):
    """Remote-Stream: seekbar nur mit Bitrate UND Dauer
    (Protocols/HTTP.pm:1150-1165). Die Bitrate kommt aus derselben Quelle wie
    die Radio-Liste (``remote_media.bitrate``)."""
    path = tmp_path / "lyrion.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, url TEXT,
            duration REAL, genre TEXT, content_type TEXT, remote INTEGER);
        CREATE TABLE remote_media (id INTEGER PRIMARY KEY, url TEXT,
            name TEXT, bitrate INTEGER);
        INSERT INTO remote_media (id, url, name, bitrate)
        VALUES (1, '""" + STREAM_URL + """', 'Radio Eins', 128000);
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(path))
    p = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=58044)
    p.power = True
    p.playlist = [STREAM_URL]
    p.playlist_position = 0
    p.mode = "play"
    p.elapsed = 5.0
    p.duration = 30.0           # Dauer bekannt
    p.current_url = STREAM_URL
    res = _status(p, ["0", "100"])
    assert res.get("can_seek") == 1, sorted(res)


def test_status_no_can_seek_for_a_remote_stream_without_bitrate(tmp_path,
                                                                monkeypatch):
    """Ohne Bitrate bleibt ``can_seek`` weg (HTTP.pm:1159-1163)."""
    p = _status_player(tmp_path, monkeypatch, [STREAM_URL], 0, mode="play",
                       elapsed=5.0, duration=30.0, current_url=STREAM_URL)
    res = _status(p, ["0", "100"])
    assert "can_seek" not in res, sorted(res)


def test_status_no_can_seek_without_a_playing_song(tmp_path, monkeypatch):
    """Stop → kein ``can_seek`` (Perl nur im ``playingSong()``-Zweig :4086)."""
    player = _status_player(tmp_path, monkeypatch, [11, 12], 0, mode="stop")
    assert "can_seek" not in _status(player, ["0", "100"])


def test_status_plain_has_no_count_or_offset_even_with_a_queue(tmp_path,
                                                               monkeypatch):
    """Runde-3-Entscheid: ``count`` NUR im Menü-Pfad (Perl-Beleg).

    Perl fügt ``count`` ausschließlich innerhalb ``if ($menuMode)`` hinzu
    (``Queries.pm:4318-4320``: ``my $menuCount = $songCount?$songCount+2:0;
    $request->addResult("count", $menuCount);``); ``offset`` ebenso
    (:4433, nur ``if $menuMode``). Squeezers ``count``-Feld ohne
    Default ist deshalb KEIN Grund, die Perl-Form zu verlassen — live
    Perl 9.1.1: ``status`` → ``{…, "playlist_tracks":1, …}`` ohne
    ``count``/``offset``.

    Der Menü-Pfad behält beide Felder (siehe
    ``test_status_count_and_offset_only_in_menu_mode``).
    """
    player = _status_player(tmp_path, monkeypatch, [11, 12], 0, mode="play",
                            elapsed=10.0)
    plain = _status(player, [])
    assert "count" not in plain and "offset" not in plain
    assert plain["playlist_tracks"] == 2


# ----------------------------------------------------------------------
# playlist <entity> ?   (playlistXQuery, Queries.pm:2708-2772)
# ----------------------------------------------------------------------

def _playlist_db(tmp_path, monkeypatch) -> str:
    """Eigene Bibliotheks-DB mit Artist UND Album (für ``_songData``-Felder)."""
    path = tmp_path / "lyrion.db"
    if path.exists():
        path.unlink()          # mehrere Player pro Test → DB neu aufsetzen
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, url TEXT,
            duration REAL, year INTEGER, tracknum INTEGER, bitrate INTEGER,
            samplerate INTEGER, bitspersample INTEGER, genre TEXT, cover TEXT,
            remote INTEGER, disc INTEGER, filesize INTEGER, comment TEXT,
            lyrics TEXT, content_type TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        INSERT INTO tracks (id, title, url, duration, genre, content_type,
                            remote)
        VALUES (11, 'First Song', 'file:///music/first.mp3', 180.0, 'Rock',
                'mp3', 0),
               (12, 'Second Song', 'file:///music/second.mp3', 245.5, 'Metal',
                'mp3', 0);
        INSERT INTO contributors (id, name) VALUES (7, 'Goabert');
        INSERT INTO tracks_contributors (track, contributor, role)
        VALUES (11, 7, 1), (12, 7, 1);
        INSERT INTO albums (id, title) VALUES (5, 'First Album');
        INSERT INTO tracks_albums (track, album) VALUES (11, 5);
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(path))
    return str(path)


def _playlist_player(tmp_path, monkeypatch, playlist, position, **kw):
    _playlist_db(tmp_path, monkeypatch)
    p = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=58044)
    p.power = True
    p.playlist = list(playlist)
    p.playlist_position = position
    p.playlist_total = len(p.playlist)
    for key, value in kw.items():
        setattr(p, key, value)
    return p


def _pl_req(player, command):
    """``_slim_request`` gegen DIESEN Player (der Singleton-PlayerManager
    wird auf ihn gesetzt, wie in ``_status``)."""
    _pm(player)
    return asyncio.run(JSONRPCAPI()._slim_request(player.mac, list(command)))


@pytest.mark.parametrize("entity,expected", [
    ("repeat", {"_repeat": 0}),
    ("shuffle", {"_shuffle": 0}),
    ("tracks", {"_tracks": 2}),
    ("index", {"_index": 1}),
    ("jump", {"_jump": 1}),
])
def test_playlist_entity_queue_state_queries(entity, expected, tmp_path,
                                             monkeypatch):
    """``repeat``/``shuffle``/``tracks``/``index``/``jump`` sind IMMER
    vorhanden (Perl ``Playlist::repeat``/``shuffle`` :2724-2725/:2727-2728,
    ``playingSongIndex`` :2730-2731, ``Playlist::count`` :2743-2744).

    Live Perl 9.1.1 2026-09-13:: ``playlist repeat ?`` → ``{"_repeat":"0"}``,
    ``playlist shuffle ?`` → ``{"_shuffle":"0"}``, ``playlist tracks ?`` →
    ``{"_tracks":1}``, ``playlist index ?`` → ``{"_index":"0"}``. Der Harness
    setzt numerische Strings mit Zahlen gleich, wir senden echte Zahlen.
    """
    player = _playlist_player(tmp_path, monkeypatch, [11, 12], 1,
                              mode="play", elapsed=10.0)
    assert _pl_req(player, ["playlist", entity, "?"]) == expected


def test_playlist_entity_name_is_the_remote_stream_title(tmp_path, monkeypatch):
    """``playlist name ?`` — Remote-Stream: ``remote_title`` (:2766-2767).

    Live Perl 9.1.1 (read-only 2026-09-19, Player 00:04:20:2B:88:C8, Sender
    Hirschmilch Chillout): bei ``current_title: "Vibrasphere - Tierra Azul
    (Nordlight Remix)"`` (ICY) antwortet ``playlist name ?`` mit
    ``Hirschmilch Chillout`` — dem NAMEN des Eintrags (``$track->title``, per
    ``setRemoteMetadata`` aus der Feed-Zeile, ``Slim/Control/XMLBrowser.pm:
    693-700``), NICHT dem ICY-Titel des laufenden Streams.  ``playlist
    title ?`` liefert dagegen den ICY-Anteil (``_songData``:5972).

    Für einen LOKALEN Track setzt Perl kein ``name`` (Tag ``N`` =
    ``remote_title`` greift nur bei Streams) → kein Result.
    """
    stream = "http://stream.example.org:8000/live.mp3"
    player = _playlist_player(
        tmp_path, monkeypatch, [stream], 0, mode="play", elapsed=5.0,
        current_url=stream,
        # ICY-Titel des Streams und der Name des Eintrags sind VERSCHIEDEN —
        # genau darum ging es im Live-Fall.
        current_title="Vibrasphere - Tierra Azul (Nordlight Remix)",
        stream_baseline_title="Hirschmilch Chillout",
        stream_meta_url=stream,
        remote_meta={"streamtitle": "Vibrasphere - Tierra Azul (Nordlight Remix)",
                     "title": "Tierra Azul (Nordlight Remix)",
                     "artist": "Vibrasphere", "url": stream},
        stream_titles={stream: "Hirschmilch Chillout"})
    assert _pl_req(player, ["playlist", "name", "?"]) == {
        "_name": "Hirschmilch Chillout"}
    assert _pl_req(player, ["playlist", "title", "?"]) == {
        "_title": "Tierra Azul (Nordlight Remix)"}

    local = _playlist_player(tmp_path, monkeypatch, [11, 12], 0, mode="play",
                             elapsed=5.0)
    assert _pl_req(local, ["playlist", "name", "?"]) == {}


def test_playlist_entity_url_is_null_without_a_saved_playlist(tmp_path,
                                                              monkeypatch):
    """``playlist url ?`` → ``$client->currentPlaylist()`` (:2736-2738).

    ``currentPlaylist`` liefert nur ein gespeichertes Playlist-Objekt und
    sonst undef (``Client.pm:1212-1228``) → live ``{"_url":null}``.
    """
    player = _playlist_player(tmp_path, monkeypatch, [11, 12], 0, mode="play")
    assert _pl_req(player, ["playlist", "url", "?"]) == {"_url": None}


def test_playlist_entity_modified_follows_queue_mutations(tmp_path,
                                                          monkeypatch):
    """``playlist modified ?`` → ``currentPlaylistModified`` (:2740-2741).

    Perl setzt das Flag in den Queue-Kommandos auf 1 (``playlistXitemCommand``
    ``Commands.pm:831``, ``playlistJumpCommand`` :1506) und auf 0 beim Laden
    einer gespeicherten Playlist (:1162). Live Perl (Queue per add gebaut):
    ``{"_modified":1}``. Ohne je veränderte Queue ist es undef → null.
    """
    player = _playlist_player(tmp_path, monkeypatch, [11], 0, mode="stop")
    assert _pl_req(player, ["playlist", "modified", "?"]) == {"_modified": None}
    _pl_req(player, ["playlist", "add", "12"])
    assert _pl_req(player, ["playlist", "modified", "?"]) == {"_modified": 1}


def test_playlist_entity_current_track_fields(tmp_path, monkeypatch):
    """``title``/``duration``/``artist``/``album``/``genre`` kommen aus
    ``_songData`` des Tracks am ``_index`` (:2755-2768).

    Live Perl (Remote-Stream): ``{"_artist":"Goabert"}``,
    ``{"_duration":"0"}``, ``{"_title":"pulchra somnium"}`` — und
    ``playlist album ?`` → ``{}``, ``playlist genre ?`` → ``{}``, weil der
    Stream keine Werte hat (ein fehlendes Feld ergibt KEINEN Schlüssel).
    """
    player = _playlist_player(tmp_path, monkeypatch, [11, 12], 0, mode="play",
                              elapsed=10.0)
    assert _pl_req(player, ["playlist", "title", "?"]) == {"_title": "First Song"}
    assert _pl_req(player, ["playlist", "artist", "?"]) == {"_artist": "Goabert"}
    assert _pl_req(player, ["playlist", "album", "?"]) == {"_album": "First Album"}
    assert _pl_req(player, ["playlist", "genre", "?"]) == {"_genre": "Rock"}
    assert _pl_req(player, ["playlist", "duration", "?"]) == {"_duration": 180.0}
    # Ohne Dauer-Feld bleibt der Schlüssel trotzdem stehen (definiert, :2755).
    assert _pl_req(player, ["playlist", "duration", "?"])["_duration"] == 180.0
    # Track 12 hat kein Album → kein Result (:2755-2768).
    assert _pl_req(player, ["playlist", "album", "1", "?"]) == {}


def test_playlist_entity_index_argument_selects_the_track(tmp_path,
                                                          monkeypatch):
    """``playlist title <_index> ?`` (Dispatch ``Request.pm:588``).

    Ohne ``_index`` arbeitet Perl auf dem laufenden Titel
    (``Playlist.pm:72-73``).
    """
    player = _playlist_player(tmp_path, monkeypatch, [11, 12], 0, mode="play")
    assert _pl_req(player, ["playlist", "title", "1", "?"]) == {
        "_title": "Second Song"}


def test_playlist_entity_path_and_remote(tmp_path, monkeypatch):
    """``path`` → ``Playlist::url`` bzw. 0 (:2746-2748), ``remote`` nur bei
    definierter URL (:2750-2753).

    Live Perl (Stream): ``{"_path":"http://strm112.1.fm/ambientpsy_mobile_mp3"}``
    und ``{"_remote":1}``.
    """
    stream = "http://stream.example.org:8000/live.mp3"
    remote = _playlist_player(tmp_path, monkeypatch, [stream], 0, mode="play",
                              elapsed=5.0, current_url=stream,
                              current_title="Radio Eins")
    assert _pl_req(remote, ["playlist", "path", "?"]) == {"_path": stream}
    assert _pl_req(remote, ["playlist", "remote", "?"]) == {"_remote": 1}

    local = _playlist_player(tmp_path, monkeypatch, [11], 0, mode="play")
    assert _pl_req(local, ["playlist", "path", "?"]) == {
        "_path": "file:///music/first.mp3"}
    assert _pl_req(local, ["playlist", "remote", "?"]) == {"_remote": 0}

    empty = _playlist_player(tmp_path, monkeypatch, [], 0, mode="stop")
    assert _pl_req(empty, ["playlist", "path", "?"]) == {"_path": 0}
    assert _pl_req(empty, ["playlist", "remote", "?"]) == {}
    assert _pl_req(empty, ["playlist", "title", "?"]) == {}


# ----------------------------------------------------------------------
# can ?   (canQuery, Slim/Plugin/CLI/Plugin.pm:751-790)
# ----------------------------------------------------------------------

def test_can_query_is_a_number_and_zero_without_params():
    """``can ?`` → ``{"_can":0}`` als ZAHL (Perl :783/:791).

    Live Perl 9.1.1: ``can ?`` → ``{"_can":0}``, ``can play ?`` →
    ``{"_can":1}`` (ein Dispatch-Eintrag existiert). Vorher antworteten wir
    ``{"_can":""}`` (String) — der Harness meldete das als Typabweichung.
    """
    assert _req(["can", "?"]) == {"_can": 0}
    assert _req(["can", "play", "?"]) == {"_can": 1}
    assert _req(["can", "gibtsnicht", "?"]) == {"_can": 0}


# ----------------------------------------------------------------------
# pref / playerpref ohne Namespace   (prefQuery, Queries.pm:3009-3044)
# ----------------------------------------------------------------------

def test_pref_query_uses_the_p2_key():
    """``pref <name> ?`` → IMMER ``_p2`` (:3045-3048).

    Live Perl 9.1.1: ``pref ?`` → ``{"_p2":null}``,
    ``pref audiodir ?`` → ``{"_p2":null}`` (unbekannte Pref = undef → null).
    Vorher antworteten wir mit dem Pref-Namen als Schlüssel
    (``{"audiodir":""}``) — eine erfundene Form.
    """
    assert _req(["pref", "?"]) == {"_p2": None}
    assert _req(["pref", "audiodir", "?"]) == {"_p2": None}


def test_playerpref_without_a_name_is_p2_null():
    """``playerpref ?`` (ohne Pref-Namen) → ``{"_p2":null}`` (live Perl).

    ``playerpref <pref> ?`` bleibt unverändert (``tests/test_playerpref.py``).
    """
    assert _req(["playerpref", "?"]) == {"_p2": None}
    assert _req(["playerpref", "replayGainMode", "?"]) == {"_p2": "0"}
