"""MENU-Parität — fehlende Menüknoten/-aktionen nach Perl (MENU-03/04/05/12/13).

Perl-Referenz (alles read-only): Quellbaum ``/tmp/lms-ref`` und der live
laufende Perl-LMS 9.1.1 auf ``192.168.1.90:9000`` (nur lesende
``slim.request``-Aufrufe, Player ``24:0a:c4:29:77:90``).

**Live-Kommandos (wörtlich, am 2026-09-12 read-only geholt):**::

    {"id":1,"method":"slim.request","params":["24:0a:c4:29:77:90",["menu","0","60","direct:1"]]}
    {"id":1,"method":"slim.request","params":["24:0a:c4:29:77:90",["browselibrary","items","0","2","menu:1","mode:albums"]]}
    {"id":1,"method":"slim.request","params":["24:0a:c4:29:77:90",["browselibrary","items","0","2","menu:1","mode:artists"]]}
    {"id":1,"method":"slim.request","params":["24:0a:c4:29:77:90",["browselibrary","items","0","2","menu:1","mode:genres"]]}
    {"id":1,"method":"slim.request","params":["24:0a:c4:29:77:90",["browselibrary","items","0","2","menu:1","mode:years"]]}
    {"id":1,"method":"slim.request","params":["24:0a:c4:29:77:90",["yearinfo","items","0","20","menu:1","year:2026"]]}
    {"id":1,"method":"slim.request","params":["24:0a:c4:29:77:90",["contextmenu","0","20","menu:year","year:2026"]]}

Die wörtlichen Perl-Antworten (gekürzt auf die geprüften Felder)::

    # menu 0 60 direct:1 → count 53
    {"id":"myMusic","text":"Eigene Musik","node":"home","weight":11,"isANode":1}
    {"id":"playerpower","text":"Schlafzimmer ausschalten","node":"home",
     "weight":100,"actions":{"do":{"player":0,"cmd":["power",0]}}}
    {"id":"settingsAudio","text":"Audio","node":"settings","weight":35,"isANode":1}
    {"id":"settingsRepeat","text":"Wiederholen","node":"settings","weight":20,
     "choiceStrings":["Aus","Titel","Liste"],"selectedIndex":1,
     "actions":{"do":{"choices":[{"player":0,"cmd":["playlist","repeat","0"]},
                                 {"player":0,"cmd":["playlist","repeat","1"]},
                                 {"player":0,"cmd":["playlist","repeat","2"]}]}}}
    {"id":"settingsShuffle","text":"Zufall","weight":10,
     "choiceStrings":["Aus","Titel","Album"],"selectedIndex":1}
    {"id":"settingsAlarm","text":"Wecker","node":"settings","weight":29,
     "actions":{"go":{"cmd":["alarmsettings"],"player":0}}}
    {"id":"settingsSleep","text":"Schlafmodus","node":"settings","weight":65,
     "actions":{"go":{"cmd":["sleepsettings"],"player":0}}}
    {"id":"settingsSync","text":"Synchronisieren","node":"settings","weight":70,
     "actions":{"go":{"player":0,"cmd":["syncsettings"]}}}
    {"id":"settingsXfade","text":"Überblendung","node":"settingsAudio",
     "weight":30,"iconStyle":"hm_settingsAudio",
     "actions":{"go":{"cmd":["crossfadesettings"],"player":0}}}
    {"id":"settingsReplayGain","text":"Lautstärken-Normalisierung",
     "node":"settingsAudio","weight":40,"iconStyle":"hm_settingsAudio",
     "actions":{"go":{"cmd":["replaygainsettings"],"player":0}}}
    {"id":"settingsFixedVolume","text":"Festgelegte Lautstärke",
     "node":"settingsAudio","weight":100,"iconStyle":"hm_settingsAudio",
     "actions":{"go":{"cmd":["jivefixedvolumesettings"],"player":0}}}
    {"id":"settingsAlbumSettings","text":"Sortierverfahren für Alben",
     "node":"advancedSettings","weight":105,"iconStyle":"hm_advancedSettings",
     "actions":{"go":{"cmd":["jivealbumsortsettings"],"params":{"menu":"radio"}}}}
    # myMusic-Kinder (node "myMusic")
    {"id":"myMusicArtistsAllArtists","text":"Alle Interpreten","weight":11,
     "homeMenuText":"Alle Interpreten","icon":"html/images/artists.png",
     "actions":{"go":{"cmd":["browselibrary","items"],
                      "params":{"menu":1,"mode":"artists"}}}}
    {"id":"myMusicArtistsAlbumArtists","text":"Album-Interpreten","weight":9,
     "homeMenuText":"Album-Interpreten durchsuchen","icon":"html/images/artists.png",
     "actions":{"go":{"cmd":["browselibrary","items"],
                      "params":{"menu":1,"role_id":"ALBUMARTIST","mode":"artists"}}}}
    {"id":"myMusicAlbums","text":"Alben","weight":20,
     "homeMenuText":"Alben durchsuchen",
     "actions":{"go":{"cmd":["browselibrary","items"],
                      "params":{"menu":1,"mode":"albums"}}}}
    {"id":"myMusicGenres","text":"Stilrichtung","weight":30,
     "homeMenuText":"Stilrichtungen durchsuchen"}
    {"id":"myMusicYears","text":"Jahrgang","weight":40,
     "homeMenuText":"Jahre durchsuchen"}
    {"id":"myMusicNewMusic","text":"Neue Musik","weight":50,
     "homeMenuText":"Neue Musik",
     "actions":{"go":{"cmd":["browselibrary","items"],
                      "params":{"mode":"albums","sort":"new","menu":1,"wantMetadata":1}}}}
    {"id":"myMusicMusicFolder","text":"Musikordner","weight":70,
     "homeMenuText":"Musikordner"}
    {"id":"myMusicSearch","text":"Suchen","weight":90}   # kein homeMenuText

    # browselibrary items 0 2 menu:1 mode:albums → base.actions
    {"more":{"cmd":["albuminfo","items"],"player":0,"window":{"isContextMenu":1},
             "params":{"menu":1},"itemsParams":"commonParams"}}
    {"add-hold":{"params":{"menu":1,"cmd":"insert"},"itemsParams":"commonParams",
                 "cmd":["playlistcontrol"],"player":0}}
    {"set-preset-0":{"player":0,"cmd":["jivefavorites","set_preset","key:0"],
                     "itemsParams":"presetParams"}}
    # item0: {"commonParams":{"album_id":"1"},"presetParams":
    #   {"favorites_url":"db:album.title=-&contributor.name=blamstrain",
    #    "favorites_type":"playlist","favorites_title":"-"}}

    # mode:artists → add-hold includes menu_mode, artists item presetParams
    {"add-hold":{"cmd":["playlistcontrol"],"player":0,
                 "params":{"cmd":"insert","menu_mode":"artists","menu":1},
                 "itemsParams":"commonParams"}}
    # item0 presetParams:
    #   {"icon":"html/images/artists.png","favorites_url":"db:contributor.name=%3F",
    #    "favorites_title":"?","favorites_type":"playlist"}

    # mode:genres → base.actions = add, add-hold, go, more, play, playControl
    # (KEIN set-preset-*); item presetParams ist null/abwesend
    {"add-hold":{"itemsParams":"commonParams",
                 "params":{"menu":1,"role_id":"ALBUMARTIST","cmd":"insert"},
                 "player":0,"cmd":["playlistcontrol"]}}

    # mode:years → item0 presetParams
    #   {"favorites_url":"db:year.id=2026","favorites_type":"playlist",
    #    "favorites_title":2026}      (Titel ist die ZAHL 2026)

    # yearinfo items 0 20 menu:1 year:2026 → count 3, title "2026"
    {"type":"text","text":"Am Ende hinzufügen","style":"item_add","addAction":"go",
     "actions":{"go":{"player":0,"cmd":["playlistcontrol"],
                      "params":{"cmd":"add","year":"2026"},"nextWindow":"parent"}}}
    {"text":"Wiedergabe","style":"itemplay","type":"text",
     "actions":{"go":{"player":0,"cmd":["playlistcontrol"],
                      "params":{"cmd":"load","year":"2026"},
                      "nextWindow":"nowPlaying"}}}

Hinweis (ehrlich): ``trackinfo items …`` schließt auf dem live Perl die
HTTP-Verbindung (``RemoteDisconnected``) — unser Port antwortet stattdessen
über den bereits verifizierten Kontextmenü-Builder (contextmenu.py), damit
``base.actions.more`` (MENU-03) kein toter Menüpunkt ist.

Nicht abgedeckt (bewusst, siehe Modul-Docstring ``lyrion/web/menus.py``):
die Plugin- und Geräte-Kapazitäts-Einträge des Home-Menüs (opml*/MyApps/
TuneIn, randomplay, settingsRescan, settingsInformation, `name`,
Bass/Treble/StereoXL/LineOut/Brightness/Textsize) — dafür fehlt unserem Port
das Kommando bzw. das Per-Modell-Capability-Modell.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web import menus
from lyrion.web.api import JSONRPCAPI

MAC = "1C:87:2C:47:FC:36"


@pytest.fixture(autouse=True)
def german(monkeypatch):
    """Server-Pref ``language = de`` (Strings.pm:622-624)."""
    monkeypatch.setattr(
        "lyrion.config.get_prefs",
        lambda: type("P", (), {"get": staticmethod(
            lambda name, default=None: "de" if name == "language" else default)})())
    yield


def _player(**kw) -> PlayerState:
    base = dict(mac=MAC, name="Küche", ip="192.168.1.225", port=48512,
                model="squeezeplay", model_name="SB Player", connected=True,
                power=True)
    base.update(kw)
    return PlayerState(**base)


def _pm(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in (players or (_player(),))}
    return pm


# ── MENU-03/04/05: base.actions ───────────────────────────────────────────

def test_tracks_base_actions_carry_more_add_hold_and_set_preset():
    """Live tracks-Window: mehr Kacheln als vorher — ``more`` (trackinfo),
    ``add-hold`` (insert) und ``set-preset-0..9`` (XMLBrowser.pm:966-1012,
    1798-1808)."""
    actions = JSONRPCAPI._browselibrary_menu_actions(
        "tracks", {"album_id": "45"}, 0, 2, preset_fav_set=True)

    assert {"add", "add-hold", "go", "more", "play", "playControl",
            "set-preset-0", "set-preset-9"} <= set(actions)
    assert actions["more"] == {
        "player": 0, "cmd": ["trackinfo", "items"],
        "itemsParams": "commonParams", "window": {"isContextMenu": 1},
        "params": {"menu": 1, "album_id": "45"},
    }
    assert actions["add-hold"] == {
        "player": 0, "cmd": ["playlistcontrol"], "itemsParams": "commonParams",
        "params": {"cmd": "insert", "menu": 1},
    }
    assert actions["set-preset-0"] == {
        "player": 0, "cmd": ["jivefavorites", "set_preset", "key:0"],
        "itemsParams": "presetParams",
    }
    assert actions["set-preset-9"]["cmd"] == [
        "jivefavorites", "set_preset", "key:9"]
    assert len([k for k in actions if k.startswith("set-preset-")]) == 10


def test_albums_base_actions_match_live_perl():
    actions = JSONRPCAPI._browselibrary_menu_actions(
        "albums", None, 0, 2, preset_fav_set=True)
    assert actions["more"] == {
        "cmd": ["albuminfo", "items"], "player": 0,
        "window": {"isContextMenu": 1}, "params": {"menu": 1},
        "itemsParams": "commonParams",
    }
    assert actions["add-hold"]["params"] == {"cmd": "insert", "menu": 1}
    assert actions["go"]["params"] == {"mode": "tracks", "menu": 1}


def test_artists_base_actions_match_live_perl():
    """``add-hold`` trägt bei artists ``menu_mode`` (BrowseLibrary.pm:1244-1260)."""
    actions = JSONRPCAPI._browselibrary_menu_actions(
        "artists", None, 0, 2, preset_fav_set=True)
    assert actions["more"]["cmd"] == ["artistinfo", "items"]
    assert actions["add-hold"]["params"] == {
        "cmd": "insert", "menu_mode": "artists", "menu": 1}
    assert actions["go"]["params"] == {"mode": "albums", "menu": 1,
                                       "menu_mode": "artists"}


def test_genres_base_actions_have_no_set_preset():
    """Live genres: keine ``set-preset-*`` (kein Item mit presetParams)."""
    actions = JSONRPCAPI._browselibrary_menu_actions(
        "genres", None, 0, 2, preset_fav_set=False)
    assert actions["more"]["cmd"] == ["genreinfo", "items"]
    assert actions["add-hold"]["params"] == {
        "cmd": "insert", "role_id": "ALBUMARTIST", "menu": 1}
    assert not [k for k in actions if k.startswith("set-preset-")]


def test_years_base_actions_target_yearinfo():
    actions = JSONRPCAPI._browselibrary_menu_actions(
        "years", None, 0, 2, preset_fav_set=True)
    assert actions["more"]["cmd"] == ["yearinfo", "items"]
    assert actions["more"]["params"] == {"menu": 1}
    assert "set-preset-0" in actions


def test_more_is_emitted_per_mode_and_omitted_for_non_live_probed_kinds():
    """``more`` je Modus — auch für ``folder`` (Default-Base des Fensters).

    ``XMLBrowser.pm:888-941``: der **Default-Base** eines Menü-Fensters trägt
    ``more`` bereits selbst — ``cmd => [$query,'items']``,
    ``itemsParams => 'params'``, ``params = {menu => $query}
    + $feed->{query}`` und ``window {isContextMenu:1}`` (:932-938).  Ein
    ``info``-Sub-Feed überschreibt es nur, wenn er ``actions`` mit
    ``info`` definiert (``$subFeed->{'actions'}`` → ``_makeAction(...,
    'info', ...)``, :943-950).

    ``menus.more_action`` ist genau dieser ``info``-Zweig (die Feeds
    trackinfo/albuminfo/artistinfo/genreinfo/yearinfo), deshalb ist er für
    ``folder`` ``None``: das ``folderinfo`` sitzt dort **pro Item** in
    ``itemActions.info`` (``BrowseLibrary.pm:2067-2071`` →
    ``XMLBrowser.pm:1289-1290``, geprüft in ``test_musicdir_bmf``).
    Die frühere Erwartung „für folder gar kein ``more``“ war nie gegen den
    ``menu:1``-bmf-Fall gehalten — Live Perl 9.1.1 (read-only, 2026-09-14)::

        browselibrary items 0 2 menu:1 mode:bmf
        base.actions = {add, add-hold, go, more, play, playControl}
        more = {"cmd":["browselibrary","items"],"itemsParams":"params",
                "params":{"menu":"browselibrary","mode":"bmf"},
                "player":0,"window":{"isContextMenu":1}}

    und ``base.actions.more`` ist in *jedem* Modus vorhanden (live: albums,
    artists, genres, years, tracks, bmf).
    """
    expected = {"tracks": "trackinfo", "albums": "albuminfo",
                "artists": "artistinfo", "genres": "genreinfo",
                "years": "yearinfo"}
    for kind, feed in expected.items():
        assert menus.more_action(kind, {})["cmd"] == [feed, "items"], kind
    assert menus.more_action("folder") is None
    assert JSONRPCAPI._browselibrary_menu_actions("folder", None, 0, 2)["more"] == {
        "player": 0,
        "cmd": ["browselibrary", "items"],
        "itemsParams": "params",
        "params": {"mode": "bmf", "menu": "browselibrary"},
        "window": {"isContextMenu": 1},
    }


# ── MENU-05: presetParams je Item-Typ ─────────────────────────────────────

def _lib_db(tmp_path):
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT,
                             year INTEGER, url TEXT, album INTEGER, tracknum INTEGER);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             artwork INTEGER);
        CREATE TABLE albums_contributors (album INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        INSERT INTO contributors (id, name) VALUES (1, 'Artist One');
        INSERT INTO albums (id, title, year, artwork) VALUES (45, 'Kein Album', 2001, 1);
        INSERT INTO albums_contributors (album, contributor, role) VALUES (45, 1, 1);
        INSERT INTO tracks (id, title, genre, year, url, album, tracknum) VALUES
            (10, 'Molotow Soda', 'Rock', 2001, 'file:///m/a.mp3', 45, 1),
            (11, 'Zweiter', 'Rock', 2001, 'file:///m/b.mp3', 45, 2);
        INSERT INTO tracks_albums (track, album) VALUES (10, 45), (11, 45);
        INSERT INTO tracks_contributors (track, contributor, role) VALUES
            (10, 1, 1), (11, 1, 1);
        """
    )
    con.commit()
    con.close()
    return str(db)


def _browse(tmp_path, monkeypatch, mode: str, **kw):
    db = _lib_db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    args = ["browselibrary", "items", "0", "2", "menu:1", f"mode:{mode}"]
    args.extend(f"{k}:{v}" for k, v in kw.items())
    return asyncio.run(JSONRPCAPI()._json_browselibrary("browselibrary", args))


def test_albums_preset_params_and_set_preset_pipeline(tmp_path, monkeypatch):
    """Live albums-Item → ``presetParams`` inkl. ``icon`` und die daraus
    folgende ``set-preset-*``-Basis (XMLBrowser.pm:1131-1135,1427)."""
    res = _browse(tmp_path, monkeypatch, "albums")
    item = res["item_loop"][0]
    preset = item["presetParams"]
    assert preset == {
        "favorites_url": "db:album.title=Kein%20Album&contributor.name=Artist%20One",
        "favorites_type": "playlist",
        "favorites_title": "Kein Album",
        "icon": "music/45/cover",
    }
    assert res["base"]["actions"]["set-preset-0"]["itemsParams"] == "presetParams"
    assert res["base"]["actions"]["more"]["cmd"] == ["albuminfo", "items"]


def test_artists_preset_params_match_live_perl(tmp_path, monkeypatch):
    res = _browse(tmp_path, monkeypatch, "artists")
    assert res["item_loop"][0]["presetParams"] == {
        "favorites_url": "db:contributor.name=Artist%20One",
        "favorites_type": "playlist",
        "favorites_title": "Artist One",
        "icon": "html/images/artists.png",
    }


def test_years_preset_params_title_is_numeric(tmp_path, monkeypatch):
    res = _browse(tmp_path, monkeypatch, "years")
    preset = res["item_loop"][0]["presetParams"]
    assert preset["favorites_url"] == "db:year.id=2001"
    assert preset["favorites_title"] == 2001          # Zahl, kein String
    assert isinstance(preset["favorites_title"], int)


def test_genres_items_have_no_preset_params(tmp_path, monkeypatch):
    res = _browse(tmp_path, monkeypatch, "genres")
    assert "presetParams" not in res["item_loop"][0]
    assert not [k for k in res["base"]["actions"]
                if k.startswith("set-preset-")]


# ── MENU-13: My-Music-Kinder ──────────────────────────────────────────────

def test_my_music_nodes_match_perl_registry_ids_weights_and_params():
    """Die volle Registry (Konditionen an): ``BrowseLibrary.pm:495-653``."""
    nodes = {n["id"]: n for n in menus.my_music_nodes(
        has_works=True, has_playlists=True)}
    assert set(nodes) == {
        "myMusicArtistsAlbumArtists", "myMusicArtistsAllArtists",
        "myMusicAlbums", "myMusicGenres", "myMusicWorks", "myMusicYears",
        "myMusicNewMusic", "myMusicMusicFolder", "myMusicPlaylists",
        "myMusicSearch",
    }
    # unified-Variante (useUnifiedArtistsList an) ersetzt das Paar
    unified = {n["id"]: n for n in menus.my_music_nodes(unified_artists=True)}
    assert set(unified) == {
        "myMusicArtists", "myMusicAlbums", "myMusicGenres", "myMusicYears",
        "myMusicNewMusic", "myMusicMusicFolder", "myMusicSearch",
    }
    assert unified["myMusicArtists"]["weight"] == 10
    assert unified["myMusicArtists"]["actions"]["go"]["params"] == {
        "menu": 1, "mode": "artists"}
    assert unified["myMusicArtists"]["homeMenuText"] == "Interpreten durchsuchen"
    nodes.update({"myMusicArtists": unified["myMusicArtists"]})
    assert [nodes[i]["weight"] for i in (
        "myMusicArtistsAlbumArtists", "myMusicArtists", "myMusicArtistsAllArtists",
        "myMusicAlbums", "myMusicGenres", "myMusicWorks", "myMusicYears",
        "myMusicNewMusic", "myMusicMusicFolder", "myMusicPlaylists",
        "myMusicSearch")] == [9, 10, 11, 20, 30, 35, 40, 50, 70, 80, 90]
    for n in nodes.values():
        assert n["node"] == "myMusic"
        assert n["actions"]["go"]["cmd"] == ["browselibrary", "items"]
        assert n["actions"]["go"]["params"]["menu"] == 1

    assert nodes["myMusicArtistsAlbumArtists"]["actions"]["go"]["params"] == {
        "menu": 1, "mode": "artists", "role_id": "ALBUMARTIST"}
    assert nodes["myMusicNewMusic"]["actions"]["go"]["params"] == {
        "mode": "albums", "sort": "new", "wantMetadata": 1, "menu": 1}
    assert nodes["myMusicPlaylists"]["actions"]["go"]["params"] == {
        "menu": 1, "mode": "playlists"}
    # Live-Texte (DE) und homeMenuText
    assert nodes["myMusicArtistsAllArtists"]["text"] == "Alle Interpreten"
    assert nodes["myMusicArtistsAllArtists"]["homeMenuText"] == "Alle Interpreten"
    assert nodes["myMusicArtistsAlbumArtists"]["text"] == "Album-Interpreten"
    assert nodes["myMusicArtistsAlbumArtists"]["homeMenuText"] == \
        "Album-Interpreten durchsuchen"
    assert nodes["myMusicAlbums"]["text"] == "Alben"
    assert nodes["myMusicAlbums"]["homeMenuText"] == "Alben durchsuchen"
    assert nodes["myMusicGenres"]["text"] == "Stilrichtung"
    assert nodes["myMusicGenres"]["homeMenuText"] == "Stilrichtungen durchsuchen"
    assert nodes["myMusicYears"]["text"] == "Jahrgang"
    assert nodes["myMusicYears"]["homeMenuText"] == "Jahre durchsuchen"
    assert nodes["myMusicNewMusic"]["text"] == "Neue Musik"
    assert nodes["myMusicMusicFolder"]["text"] == "Musikordner"
    assert nodes["myMusicWorks"]["text"] == "Werke"
    assert nodes["myMusicPlaylists"]["text"] == "Wiedergabelisten"
    assert nodes["myMusicSearch"]["text"] == "Suchen"
    # Icon nur aus jiveIcon (BrowseLibrary.pm:522,546,618)
    assert nodes["myMusicArtistsAllArtists"]["icon"] == "html/images/artists.png"
    assert "icon" not in nodes["myMusicAlbums"]
    assert "homeMenuText" not in nodes["myMusicSearch"]


def test_my_music_conditions_default_match_the_live_perl_menu():
    """Konditionen aus (Live-Stand): kein unified myMusicArtists, keine
    Works/Playlists-Einträge — aber AlbumArtists + AllArtists."""
    ids = [n["id"] for n in menus.my_music_nodes()]
    assert ids == [
        "myMusicArtistsAlbumArtists", "myMusicArtistsAllArtists",
        "myMusicAlbums", "myMusicGenres", "myMusicYears", "myMusicNewMusic",
        "myMusicMusicFolder", "myMusicSearch",
    ]
    assert "myMusicArtists" not in ids
    assert "myMusicWorks" not in ids
    assert "myMusicPlaylists" not in ids


# ── MENU-12: Home-Menü ────────────────────────────────────────────────────

def test_home_menu_without_player_is_the_library_subset():
    items = JSONRPCAPI()._home_menu()
    ids = [i["id"] for i in items]
    assert ids[:2] == ["myMusic", "favorites"]
    assert "radios" in ids
    assert "myMusicArtistsAlbumArtists" in ids
    assert "myMusicArtistsAllArtists" in ids
    assert "playerpower" not in ids
    assert items[0]["node"] == "home"
    # Lokalisierte Titel (DE) — inkl. Radio (Perl strings.txt:505)
    texts = {i["id"]: i["text"] for i in items}
    assert texts["myMusic"] == "Eigene Musik"
    assert texts["favorites"] == "Favoriten"
    assert texts["radios"] == "Radio"


def test_home_menu_with_player_has_power_and_settings_nodes():
    items = JSONRPCAPI()._home_menu(_player(power=True, repeat=1, shuffle=0))
    by_id = {i["id"]: i for i in items}

    assert by_id["playerpower"]["text"] == "Küche ausschalten"
    assert by_id["playerpower"]["node"] == "home"
    assert by_id["playerpower"]["weight"] == 100
    assert by_id["playerpower"]["actions"] == {
        "do": {"player": 0, "cmd": ["power", 0]}}

    assert by_id["settingsAudio"] == {
        "text": "Audio", "id": "settingsAudio", "node": "settings",
        "isANode": 1, "weight": 35}
    assert by_id["settingsAlarm"]["actions"] == {
        "go": {"player": 0, "cmd": ["alarmsettings"]}}
    assert by_id["settingsAlarm"]["weight"] == 29
    assert by_id["settingsSleep"]["weight"] == 65
    assert by_id["settingsSync"]["weight"] == 70
    assert by_id["settingsXfade"]["iconStyle"] == "hm_settingsAudio"
    assert by_id["settingsXfade"]["node"] == "settingsAudio"
    assert by_id["settingsReplayGain"]["actions"]["go"]["cmd"] == \
        ["replaygainsettings"]
    assert by_id["settingsFixedVolume"]["actions"]["go"]["cmd"] == \
        ["jivefixedvolumesettings"]
    assert by_id["settingsAlbumSettings"] == {
        "text": "Sortierverfahren für Alben", "id": "settingsAlbumSettings",
        "node": "advancedSettings", "weight": 105,
        "iconStyle": "hm_advancedSettings",
        "actions": {"go": {"player": 0, "cmd": ["jivealbumsortsettings"],
                           "params": {"menu": "radio"}}}}

    # Repeat/Shuffle: choiceStrings + selectedIndex (+1 wie Perl)
    assert by_id["settingsRepeat"]["choiceStrings"] == ["Aus", "Titel", "Liste"]
    assert by_id["settingsRepeat"]["selectedIndex"] == 2      # repeat=1
    assert by_id["settingsRepeat"]["weight"] == 20
    assert by_id["settingsShuffle"]["choiceStrings"] == ["Aus", "Titel", "Album"]
    assert by_id["settingsShuffle"]["selectedIndex"] == 1     # shuffle=0
    assert by_id["settingsShuffle"]["weight"] == 10
    assert by_id["settingsRepeat"]["actions"]["do"]["choices"] == [
        {"player": 0, "cmd": ["playlist", "repeat", str(i)]} for i in range(3)]


def test_power_text_switches_with_power_state():
    off = JSONRPCAPI()._home_menu(_player(power=False))
    power = next(i for i in off if i["id"] == "playerpower")
    assert power["text"] == "Küche einschalten"
    assert power["actions"]["do"]["cmd"] == ["power", 1]


def test_every_home_item_has_string_id_and_node():
    """test_contract.test_menu_item_shape — auch für die neuen Knoten."""
    for item in JSONRPCAPI()._home_menu(_player()):
        assert isinstance(item.get("id"), str), sorted(item)
        assert isinstance(item.get("node"), str), sorted(item)
        assert isinstance(item.get("text"), str), sorted(item)


# ── Lokalisierung über lyrion.i18n ────────────────────────────────────────

def test_menu_title_localized_de_from_perl_strings():
    assert menus.menu_title("MY_MUSIC") == "Eigene Musik"       # strings.txt:530
    assert menus.menu_title("SAVED_PLAYLISTS") == "Wiedergabelisten"  # :12098
    assert menus.menu_title("REPEAT") == "Wiederholen"          # :11123
    assert menus.menu_title("SLEEP") == "Schlafmodus"           # :1376
    assert menus.menu_title("SYNCHRONIZE") == "Synchronisieren"  # :14378
    assert menus.menu_title("ALBUMS_SORT_METHOD") == "Sortierverfahren für Alben"


def test_menu_title_english_when_server_language_is_en(monkeypatch):
    monkeypatch.setattr(
        "lyrion.config.get_prefs",
        lambda: type("P", (), {"get": staticmethod(
            lambda name, default=None: "en" if name == "language" else default)})())
    assert menus.menu_title("MY_MUSIC") == "My Music"
    assert menus.menu_title("SAVED_PLAYLISTS") == "Playlists"
    assert menus.menu_title("REPEAT") == "Repeat"
    assert menus.menu_title("SLEEP") == "Sleep"


def test_existing_i18n_table_wins_over_the_local_fallback():
    """``SHUFFLE`` liegt bereits in strings_de.py — get_string gewinnt."""
    assert menus.menu_title("SHUFFLE") == "Zufall"


# ── ''more''-Ziel: die *info-Feeds ────────────────────────────────────────

def test_yearinfo_items_matches_live_perl():
    res = asyncio.run(JSONRPCAPI()._slim_request(
        MAC, ["yearinfo", "items", "0", "20", "menu:1", "year:2026"]))
    assert res["title"] == "2026"
    assert res["count"] == 3
    assert res["window"] == {"windowStyle": "text_list"}
    texts = [i["text"] for i in res["item_loop"]]
    assert texts == ["Am Ende hinzufügen", "Als nächstes wiedergeben",
                     "Wiedergabe"]
    assert [i["style"] for i in res["item_loop"]] == [
        "item_add", "item_insert", "itemplay"]
    assert res["item_loop"][0]["actions"]["go"]["params"] == {
        "cmd": "add", "year": "2026"}
    assert res["item_loop"][1]["actions"]["go"]["params"] == {
        "cmd": "insert", "year": "2026"}
    assert res["item_loop"][2]["actions"]["go"]["params"] == {
        "cmd": "load", "year": "2026"}
    assert res["item_loop"][2]["actions"]["go"]["nextWindow"] == "nowPlaying"
    assert set(res["base"]["actions"]) == {
        "go", "play", "add", "add-hold", "more", "playControl"}


def test_info_feed_without_context_is_empty():
    """Ohne Feed-Kontext: ``yearinfo`` → ``{}`` (kein ``year:``);
    ``albuminfo`` ohne ``album_id`` → leer (Perl liefert das leere
    Kontextfenster, kein Unknown-Fallback)."""
    assert asyncio.run(JSONRPCAPI()._slim_request(
        MAC, ["yearinfo", "items", "0", "20", "menu:1"])) == {}
    res = asyncio.run(JSONRPCAPI()._slim_request(
        MAC, ["albuminfo", "items", "0", "20", "menu:1"]))
    assert res in ({}, {"window": {"windowStyle": "text_list"},
                        "offset": 0, "count": 0})
    assert asyncio.run(JSONRPCAPI()._slim_request(
        MAC, ["albuminfo", "playlist", "add"])) == {}


def test_info_feed_items_uses_the_target_token():
    """``albuminfo items … album_id:<id>`` erreicht den Album-Kontextmenü-
    Builder (unser contextmenu-Pfad) — nicht mehr 'unknown command'."""
    res = asyncio.run(JSONRPCAPI()._slim_request(
        MAC, ["albuminfo", "items", "0", "20", "menu:1", "album_id:45"]))
    assert isinstance(res, dict)
    # ohne DB/Album ist die Antwort leer, aber nicht der Unknown-Fallback
    assert "unknown command" not in json.dumps(res)


# ── MENU-04: insert landet hinter dem laufenden Titel ─────────────────────

def test_playlistcontrol_insert_plays_next():
    player = _player()
    player.playlist = [10, 11, 12]
    player.playlist_position = 0
    _pm(player)
    asyncio.run(JSONRPCAPI()._slim_request(
        MAC, ["playlistcontrol", "cmd:insert", "track_id:99"]))
    assert player.playlist == [10, 99, 11, 12]


def test_playlist_insert_command_uses_position_plus_one():
    player = _player()
    player.playlist = [10, 11, 12]
    player.playlist_position = 1
    _pm(player)
    asyncio.run(JSONRPCAPI()._slim_request(
        MAC, ["playlist", "insert", "track_id:99"]))
    assert player.playlist == [10, 11, 99, 12]
