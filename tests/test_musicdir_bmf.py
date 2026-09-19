"""R0.6-B (LIVE-07) — „Musikordner“ (mode:bmf) aus der Bibliothek aufbauen.

Der Ordnerbaum kommt aus **Perls ``readDirectory``**: jede Ebene wird wie bei
Perl gelesen (``Slim/Utils/Misc.pm:973-1043``, der ``mode:bmf``-Feed ist ein
Wrapper um die ``musicfolder``-Query, ``Slim/Menu/BrowseLibrary.pm:2044-2046``)
— genau EIN Verzeichnis pro Anfrage, nie ein Walk über den SMB-Baum.  Die
``file://``-Track-URLs der Bibliothek liefern nur noch die IDs; nur wenn die
Liste leer ist (Share nicht gemountet), wird der Baum aus den Zeilen
aggregiert.  Wurzel ist das Runtime-Pref ``musicdir``; ohne Pref die
längste gemeinsame Verzeichnis-Wurzel der Track-URLs — und nur, wenn sie
mehr als einen Track umfasst (drei Testdateien außerhalb der Bibliothek
dürfen die Wurzel nicht auf ``/`` drücken; genau daraus entstand das
falsche ``home``/``run``-Top-Level).

Perl-Gegenprobe (read-only, 192.168.1.90:9000/jsonrpc.js, Player
1c:87:2c:47:fc:36, ``["browselibrary","items",0,400,"menu:1","mode:bmf"]``)::

    count 303, window {"windowStyle": "text_list"}
    item   {"type": "playlist", "text": "Accept", "textkey": "A",
            "params": {"item_id": "1", "isContextMenu": 1},
            "actions": {"add":      {"cmd": ["playlistcontrol"],
                                     "params": {"cmd": "add", "menu": 1,
                                                "folder_id": "81409"}},
                        "add-hold": {... "cmd": "insert" ...},
                        "play":     {... "cmd": "load" ...,
                                     "nextWindow": "nowPlaying"},
                        "more":     {"cmd": ["folderinfo", "items"],
                                     "params": {"menu": 1,
                                                "folder_id": "81409"},
                                     "window": {"isContextMenu": 1}}}}
    file   {"type": "audio", "text": "01-accept-….mp3",
            "params": {"item_id": "06801e44.0", "isContextMenu": 1},
            "actions": {"more": {... "folderinfo" ...}}}
    base   {"actions": {"go": {"cmd": ["browselibrary", "items"],
                               "itemsParams": "params",
                               "params": {"mode": "bmf",
                                          "menu": "browselibrary"}}, …}}

Perl identifiziert Ordner mit den
``tracks.id``-Werten seiner ``content_type='dir'``-Zeilen; der Port führt
dieselben Zeilen (``lyrion.media.dir_rows``: Scan + Browse legen sie an) und
gibt deren id aus — jeder Drill ``folder_id:<id>`` läuft wie bei Perl über
diese Zeile.  Die Liste selbst ist Perls ``readDirectory``, also auch die
Kinder, für die Perl erst beim Auflisten eine Zeile anlegt
(``Slim/Control/Queries.pm:2263-2268``: Playlist-/Nicht-Scan-Dateien) —
vorher fehlten sie, weil nur ``tracks``-Zeilen gezählt wurden.
Zusätzlich akzeptiert der Drill Pfade/``file://``-Tokens, damit
eine Bibliothek ohne geschriebene dir-Zeilen weiter browst.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import urllib.parse
from pathlib import Path

import pytest

from lyrion.media import folders
from lyrion.web import api as api_mod
from lyrion.web import menus as menus_mod
from lyrion.web.api import JSONRPCAPI

#: Perls ``Empty``-Platzhalterzeile (``menu_title('EMPTY')``): "Leer" auf dem
#: deutschen Live-Perl, "Empty" im Testlauf ohne Sprach-Tabelle.
EMPTY_TEXT = menus_mod.menu_title("EMPTY")

# ---------------------------------------------------------------------------
# R0.6-B fixtures
# ---------------------------------------------------------------------------

ROOT = "/srv/music"

#: Player-MAC der Tap-Tests (dieselbe Form wie im Live-Log der App).
MAC = "1C:87:2C:47:FC:36"

#: Library URLs with percent-encoding like the importer stores them
#: (``Path.as_uri()``), plus three strays under ~/Music that must never
#: define the browse root.
LIB_URLS = [
    (1, "file:///srv/music/Metal/Accept/01-hard_attack.mp3"),
    (2, "file:///srv/music/Metal/Accept/02-objection.mp3"),
    (3, "file:///srv/music/Metal/Iron%20Maiden/03-tv_war.flac"),
    (4, "file:///srv/music/Ambient/Boards%20of%20Canada/04-oktaf.flac"),
    (5, "file:///srv/music/Heavy%20Metal/Album1/05-track.mp3"),
    (6, "file:///srv/music/Accept%26DC/06-track.mp3"),
    (7, "file:///home/keiner/Music/testlib/Test%20Song%201.wav"),
    (8, "file:///home/keiner/Music/testlib/Test%20Song%202.wav"),
    (9, "file:///home/keiner/Music/testlib/Test%20Song%203.flac"),
]

#: gvfs/SMB library root: the pref is decoded, the URLs are percent-encoded
#: (``smb-share%3Aserver%3D…``) — the encoding mismatch the old
#: first-component guess tripped over.
GVFS_ROOT = ("/run/user/1000/gvfs/smb-share:server=media.local,"
             "share=media/Musik")
GVFS_URLS = [
    (1, "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2C"
        "share%3Dmedia/Musik/Metal/Accept/01.mp3"),
    (2, "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2C"
        "share%3Dmedia/Musik/Ambient/Boards%20of%20Canada/02.flac"),
]

#: The gvfs root shape as a *real* tree under ``tmp_path`` (the drill lists the
#: directory, so the fixture needs the files on disk — see
#: ``test_bmf_percent_encoded_gvfs_root``).
GVFS_DIR_NAME = "smb-share:server=media.local,share=media"


#: ``tracks`` DDL used for every DB in this module — set by the autouse
#: fixture below from the shared conftest schema, so the folder layer writes
#: real Perl ``content_type='dir'`` rows into the test library.
_TEST_SCHEMA: str | None = None


@pytest.fixture(autouse=True)
def _use_the_port_schema(tracks_schema_sql):
    global _TEST_SCHEMA
    _TEST_SCHEMA = tracks_schema_sql
    yield
    _TEST_SCHEMA = None


def _make_db(path: Path, rows, with_title: bool = True,
             schema: str | None = None) -> str:
    """Temp library DB.

    ``schema`` is the port's real ``tracks`` DDL (conftest fixture) — the
    folder layer writes Perl's ``content_type='dir'`` rows into it, so a
    hand-trimmed table would not exercise the id path.
    """
    con = sqlite3.connect(path)
    if schema is not None:
        con.executescript(schema)
        if rows:
            con.executemany(
                "INSERT INTO tracks (id, url, title, titlesort, audio, video, "
                "remote, disabled, compilation, artflow_flag, duration, playcount)"
                " VALUES (?, ?, ?, ?, 1, 0, 0, 0, 0, 0, 0, 0)",
                [(i, u, urllib.parse.unquote(
                    Path(u.rsplit("/", 1)[-1]).stem), "") for i, u in rows])
    elif with_title:
        con.executescript(
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY, url TEXT, "
            "title TEXT, tracknum INTEGER);")
        con.executemany(
            "INSERT INTO tracks (id, url, title, tracknum) VALUES (?, ?, ?, ?)",
            [(i, u, urllib.parse.unquote(
                Path(u.rsplit("/", 1)[-1]).stem), 0) for i, u in rows])
    else:
        con.executescript("CREATE TABLE tracks (id INTEGER PRIMARY KEY, url TEXT);")
        con.executemany("INSERT INTO tracks (id, url) VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return str(path)


def _db(tmp_path: Path, rows, with_title: bool = True, schema: str | None = None) -> str:
    return _make_db(tmp_path / "lyrion.db", rows, with_title,
                    _TEST_SCHEMA if schema is None else schema)


def _setup(tmp_path, monkeypatch, rows, pref: str, with_title: bool = True,
           schema: str | None = None):
    """Temp library DB + monkeypatched musicdir runtime pref."""
    db = _db(tmp_path, rows, with_title, schema)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    monkeypatch.setattr(api_mod, "_bmf_musicdir_pref", lambda: pref)
    return db


def _browse(args: list[str]) -> dict:
    """Run ``_json_browselibrary`` in-process (SqueezePlay wire form)."""
    async def run():
        api = JSONRPCAPI()
        return await api._json_browselibrary("browselibrary", args)

    return asyncio.run(run())


def _items(args: list[str]) -> list[dict]:
    return _browse(args).get("item_loop") or []


def _texts(args: list[str]) -> set[str]:
    return {str(it.get("text")) for it in _items(args)
            if it.get("text") is not None}


# ---------------------------------------------------------------------------
# (a) Wurzel = musicdir-Pref, erste Ebene unter der Wurzel
# ---------------------------------------------------------------------------

def test_bmf_top_level_uses_musicdir_root(tmp_path, monkeypatch):
    """Top-Level = erste Ebene UNTER dem musicdir-Pref — kein home/run."""
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)

    res = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    texts = {it["text"] for it in res["item_loop"]}
    assert texts == {"Accept&DC", "Ambient", "Heavy Metal", "Metal"}, texts
    assert res["count"] == 4
    # the old bug: first path component of the URL ("srv"/"home"/"run")
    assert not ({"home", "run", "srv", "keiner"} & texts)


def test_bmf_top_level_items_are_perl_like(tmp_path, monkeypatch):
    """Ordner-Items: type playlist, id/folder_id = die ``tracks``-Zeile des
    Verzeichnisses, textkey, go/play/add-Aktionen (Perl-BrowseLibrary-Form).

    Perl liefert seine ``tracks.id`` (Verzeichnisse liegen als
    ``content_type='dir'`` in ``tracks``, ``Slim/Control/Queries.pm:2429``);
    der Port legt diese Zeile beim Browsen an (:2263-2268) und gibt ihre id
    aus. Der Client parst die ID numerisch, deshalb darf hier keine URL/kein
    Pfad stehen."""
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    items = _items(["items", "0", "50", "menu:1", "mode:bmf"])
    metal = next(it for it in items if it["text"] == "Metal")

    assert metal["type"] == "playlist"
    assert isinstance(metal["id"], int)
    # In den Aktions-Params sind die IDs bei Perl Strings (live: "204572"),
    # das id-Feld selbst numerisch — wir spiegeln beides.
    assert metal["commonParams"]["folder_id"] == str(metal["id"])
    assert metal["textkey"] == "M"
    assert {"add", "add-hold", "play"} <= set(metal["actions"])
    assert metal["actions"]["play"]["params"]["folder_id"] == str(metal["id"])
    assert metal["actions"]["play"]["params"]["cmd"] == "load"
    assert metal["actions"]["add"]["params"]["cmd"] == "add"
    # the base action that drives a plain tap (Perl base.actions.go)
    base = _browse(["items", "0", "50", "menu:1", "mode:bmf"])["base"]["actions"]
    assert base["go"]["cmd"] == ["browselibrary", "items"]
    assert base["go"]["params"]["mode"] == "bmf"


def test_bmf_percent_encoded_gvfs_root(tmp_path, monkeypatch):
    """Decoded Pref + percent-encoded Track-URLs (smb-share%3A…) matchen.

    Der gvfs-Fall: der ``musicdir``-Pref ist der dekodierte Pfad
    (``…/smb-share:server=…,share=…/Musik``), die ``file://``-URLs der
    Bibliothek tragen die Prozentkodierung.  Der Lesepfad arbeitet auf dem
    dekodierten Pfad (``readDirectory``, ``Slim/Utils/Misc.pm:973-1043``), die
    Kinder müssen also trotz der Kodierung gefunden und die ``dir``-Zeilen
    unter der kodierten URL angelegt werden.
    """
    root = tmp_path / GVFS_DIR_NAME / "Musik"
    (root / "Metal" / "Accept").mkdir(parents=True)
    (root / "Ambient" / "Boards of Canada").mkdir(parents=True)
    (root / "Metal" / "Accept" / "01-hard_attack.mp3").write_bytes(b"\0")
    (root / "Ambient" / "Boards of Canada" / "02-oktaf.flac").write_bytes(b"\0")
    rows = [
        (1, folders.file_url_from_path(
            root / "Metal" / "Accept" / "01-hard_attack.mp3")),
        (2, folders.file_url_from_path(
            root / "Ambient" / "Boards of Canada" / "02-oktaf.flac")),
    ]
    assert "%3A" in rows[0][1], "the fixture must keep the URL encoding"
    _setup(tmp_path, monkeypatch, rows, str(root))
    assert _texts(["items", "0", "50", "menu:1", "mode:bmf"]) == {
        "Ambient", "Metal"}


# ---------------------------------------------------------------------------
# (b) Unterordner: search:/folder_id: liefern die KINDER des Verzeichnisses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", [
    f"search:{ROOT}/Metal",
    f"folder_id:{ROOT}/Metal",
    f"folder_id:file://{ROOT}/Metal",
    "search:file:///srv/music/Metal",
    "folder_id:Metal",                      # relative to the root
])
def test_bmf_subfolder_children(tmp_path, monkeypatch, token):
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    assert _texts(["items", "0", "50", "menu:1", "mode:bmf", token]) == {
        "Accept", "Iron Maiden"}


def test_bmf_subfolder_with_space_via_encoded_uri(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    assert _texts(["items", "0", "50", "menu:1", "mode:bmf",
                   "folder_id:file:///srv/music/Heavy%20Metal"]) == {"Album1"}


def test_bmf_loose_tracks_listed_as_audio(tmp_path, monkeypatch):
    """Dateien direkt im Ordner werden (wie bei Perl) als audio-Items
    geführt, in Dateinamen-Reihenfolge — Ordner zuerst."""
    rows = [
        (1, "file:///srv/music/Album1/track-a.mp3"),
        (2, "file:///srv/music/Album1/track-b.flac"),
        (3, "file:///srv/music/Album1/Sub/track-c.mp3"),
    ]
    _setup(tmp_path, monkeypatch, rows, ROOT)
    items = _items(["items", "0", "50", "menu:1", "mode:bmf",
                    f"search:{ROOT}/Album1"])
    assert [it["text"] for it in items] == ["Sub", "track-a.mp3",
                                            "track-b.flac"]
    # track rows carry a track id (Perl: params.item_id) for play
    audio = next(it for it in items if it["text"] == "track-a.mp3")
    assert audio["type"] == "audio"
    # Perls Tap-Form: ``params`` (nicht ``commonParams``) — die
    # Basis-``play``-Aktion liest ``itemsParams: params``
    # (XMLBrowser.pm:1139-1149/:1259-1267, siehe (h)).
    assert audio["params"]["track_id"] == 1
    assert audio["goAction"] == "play"


# ---------------------------------------------------------------------------
# (c) Tracks außerhalb der Wurzel
# ---------------------------------------------------------------------------

def test_bmf_strays_outside_root_are_not_folders(tmp_path, monkeypatch):
    """Nur der Pref-Baum zählt; ~/Music-Testdateien tauchen nicht auf."""
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    top = _texts(["items", "0", "50", "menu:1", "mode:bmf"])
    assert "testlib" not in top
    metal = _texts(["items", "0", "50", "menu:1", "mode:bmf",
                    f"search:{ROOT}/Metal"])
    assert metal == {"Accept", "Iron Maiden"}


def test_bmf_non_audio_only_dirs_are_absent(tmp_path, monkeypatch):
    """Ordner ohne indexierte Tracks liefert die URL-Aggregation nicht
    (Perl listet sie, weil es das Dateisystem liest) — dokumentierte,
    gewollte Abweichung."""
    rows = [
        (1, "file:///srv/music/Metal/Accept/01.mp3"),
        (2, "file:///srv/music/Metal/Accept/02.mp3"),
    ]
    _setup(tmp_path, monkeypatch, rows, ROOT)
    assert _texts(["items", "0", "50", "menu:1", "mode:bmf"]) == {"Metal"}


# ---------------------------------------------------------------------------
# (d) Leere DB → leerer Ordner, kein Fehler
# ---------------------------------------------------------------------------

def test_bmf_empty_library_empty_folder(tmp_path, monkeypatch):
    """Leere Bibliothek: die Menue-Form antwortet Perls ``Empty``-Zeile.

    Live Perl 9.1.1 (read-only) ``browselibrary items 0 1 menu:1 mode:bmf
    folder_id:99999999`` → ``{count: 1, offset: 0, window {windowStyle:
    text_list}, item_loop: [{'text': 'Leer', 'style': 'itemNoAction',
    'action': 'none', 'type': 'text'}]}`` — Bug 7024,
    ``Slim/Control/XMLBrowser.pm:841-846``: ``if ($menuMode && !$count &&
    !$xmlBrowseInterimCM) { $items = [ { type => 'text', name =>
    $request->string('EMPTY') } ]; $totalCount = $count = 1; }``.  Die
    *modellose* (flache) Form bleibt ``count 0`` mit leerer Liste.
    """
    _setup(tmp_path, monkeypatch, [], ROOT)
    res = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    assert res["count"] == 1
    assert [it["text"] for it in res["item_loop"]] == [EMPTY_TEXT]

    plain = _browse(["items", "0", "50", "mode:bmf"])
    assert plain["count"] == 0
    assert not (plain.get("loop_loop") or [])


def test_bmf_empty_library_without_pref(tmp_path, monkeypatch):
    """Kein Pref, keine Tracks → keine Wurzel, Perls ``Leer``-Zeile."""
    _setup(tmp_path, monkeypatch, [], "")
    res = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    assert res["count"] == 1
    assert [it["text"] for it in res["item_loop"]] == [EMPTY_TEXT]


# ---------------------------------------------------------------------------
# (e) folder_id-Roundtrip: gelieferte id direkt als nächstes search:
# ---------------------------------------------------------------------------

def test_bmf_folder_id_roundtrip(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    items = _items(["items", "0", "50", "menu:1", "mode:bmf"])
    metal = next(it for it in items if it["text"] == "Metal")
    drilled = _texts(["items", "0", "50", "menu:1", "mode:bmf",
                      f"folder_id:{metal['id']}"])
    assert drilled == {"Accept", "Iron Maiden"}
    # second level → third level (folder_id of the drilled level)
    accept = _items(["items", "0", "50", "menu:1", "mode:bmf",
                     f"folder_id:{metal['id']}"])
    first = next(it for it in accept if it["text"] == "Accept")
    assert isinstance(first["id"], int) and first["id"] != metal["id"]
    assert _browse(["items", "0", "50", "mode:bmf",
                    f"search:{first['id']}"])["count"] == 2


# ---------------------------------------------------------------------------
# (f) Fallback-Wurzel ohne Pref: Mehrheit statt "/"
# ---------------------------------------------------------------------------

def test_bmf_fallback_root_ignores_stray_tracks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, LIB_URLS, "")
    top = _texts(["items", "0", "50", "menu:1", "mode:bmf"])
    assert top == {"Accept&DC", "Ambient", "Heavy Metal", "Metal"}
    assert not ({"home", "run", "srv", "keiner"} & top)


def test_bmf_fallback_root_single_tree(tmp_path, monkeypatch):
    """Nur die drei Testdateien: deren gemeinsamer Ordner ist die Wurzel;
    die Dateien erscheinen als audio-Items, kein 'home'-Ordner."""
    rows = LIB_URLS[6:]
    _setup(tmp_path, monkeypatch, rows, "")
    items = _items(["items", "0", "50", "menu:1", "mode:bmf"])
    assert {it["type"] for it in items} == {"audio"}
    assert _texts(["items", "0", "50", "menu:1", "mode:bmf"]) == {
        "Test Song 1.wav", "Test Song 2.wav", "Test Song 3.flac"}


def test_bmf_fallback_requires_more_than_one_track(tmp_path, monkeypatch):
    """Ein einzelner Track darf keine Wurzel definieren → Perls ``Leer``."""
    _setup(tmp_path, monkeypatch,
           [(1, "file:///home/keiner/Music/lonely.mp3")], "")
    res = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    assert res["count"] == 1
    assert [it["text"] for it in res["item_loop"]] == [EMPTY_TEXT]


def test_bmf_missing_url_column_does_not_raise(tmp_path, monkeypatch):
    """Schlanke/abweichende DBs (tracks ohne url) → Perls ``Leer``-Zeile."""
    db = _db(tmp_path, LIB_URLS, with_title=False)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    monkeypatch.setattr(api_mod, "_bmf_musicdir_pref", lambda: "")
    # no url column at all
    con = sqlite3.connect(db)
    con.execute("DROP TABLE tracks")
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    res = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    assert res["count"] == 1
    assert [it["text"] for it in res["item_loop"]] == [EMPTY_TEXT]


# ---------------------------------------------------------------------------
# Weitere Abdeckung: Encoding der Namen, Paging, Ordner-Play
# ---------------------------------------------------------------------------

def test_bmf_child_folder_name_is_decoded(tmp_path, monkeypatch):
    """Kindordner kommen dekodiert zurück ('Boards of Canada', nicht %20)."""
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    items = _items(["items", "0", "50", "menu:1", "mode:bmf",
                    f"folder_id:{ROOT}/Ambient"])
    assert [it["text"] for it in items] == ["Boards of Canada"]
    # Kindordner-ID ist ebenfalls numerisch (siehe Top-Level-Test)
    assert isinstance(items[0]["id"], int)


def test_bmf_paging_and_total(tmp_path, monkeypatch):
    """count = Gesamtzahl, start/count schneiden das Fenster."""
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    page1 = _browse(["items", "0", "2", "menu:1", "mode:bmf"])
    assert page1["count"] == 4
    assert [it["text"] for it in page1["item_loop"]] == [
        "Accept&DC", "Ambient"]
    page2 = _browse(["items", "2", "2", "menu:1", "mode:bmf"])
    assert [it["text"] for it in page2["item_loop"]] == [
        "Heavy Metal", "Metal"]


def test_bmf_loose_track_display_uses_filename_not_tag(tmp_path, monkeypatch):
    """Wie Perl (bmf zeigt Dateinamen): die Dateien heißen im Ordner wie die
    dekodierte Datei, sortiert in Dateinamen-/Disc-Reihenfolge — auch wenn
    die DB einen abweichenden Tag-Titel hat, und ohne title-Spalte."""
    rows = [
        (1, "file:///srv/music/X/zz%20second.mp3"),
        (2, "file:///srv/music/X/aa%20first.mp3"),
    ]
    _setup(tmp_path, monkeypatch, rows, ROOT)
    items = _items(["items", "0", "50", "menu:1", "mode:bmf",
                    f"folder_id:{ROOT}/X"])
    assert [it["text"] for it in items] == ["aa first.mp3", "zz second.mp3"]
    assert {it["type"] for it in items} == {"audio"}

    # minimal DB without a title column behaves the same (``content_type`` is
    # required: it is what marks a directory row, so a folder whose row cannot
    # be stored keeps the path token and the *files* must still be listed)
    db = _make_db(
        tmp_path / "minimal.db", [], schema=(
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "url TEXT, content_type TEXT);"))
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO tracks (id, url) VALUES (?, ?)", rows)
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    items = _items(["items", "0", "50", "menu:1", "mode:bmf",
                    f"folder_id:{ROOT}/X"])
    assert [it["text"] for it in items] == ["aa first.mp3", "zz second.mp3"]


def test_bmf_folder_id_expands_to_folder_tracks(tmp_path, monkeypatch):
    """play/add auf einem Ordner-Item (Perl playlistControl folder_id)
    expandiert zu allen Tracks unter dem Ordner."""
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    assert api_mod._expand_track_ids({"folder_id": f"{ROOT}/Metal"}) == [1, 2, 3]
    assert api_mod._expand_track_ids({"folder_id": f"{ROOT}/Ambient"}) == [4]


# ---------------------------------------------------------------------------
# (g) Der Tap der App: ``menu:browselibrary`` + ``folder_id``/``url``/``item_id``
#
# Squeeze Client sendet den Tap als ``browselibrary items <start> <count>
# useContextMenu:1 mode:bmf menu:browselibrary item_id:<n> isContextMenu:1
# folder_id:<id> url:<pfad>`` — ``menu:browselibrary`` kommt aus unserer
# eigenen ``base.actions.go`` (Perls ``params {mode: bmf, menu:
# browselibrary}``), ``folder_id``/``url``/``item_id`` aus den
# Item-``params``.  Perl schaltet bei JEDEM ``menu``-Parameter in den
# Jive-Modus (``Slim/Control/XMLBrowser.pm:313``), liefert also
# ``item_loop``/``offset``/``count``/``window``/``base``.
# ---------------------------------------------------------------------------

#: Die Tap-Form aus dem Live-Log der App (MAC 1C:87:2C:47:FC:36), für den
#: Ordner ``Metal`` der Test-Bibliothek.
def _app_tap(metal: dict, start: int, count: int) -> list[str]:
    return ["items", str(start), str(count), "useContextMenu:1", "mode:bmf",
            "menu:browselibrary", "item_id:0", "isContextMenu:1",
            f"folder_id:{metal['id']}", f"url:{ROOT}/Metal"]


def test_bmf_app_tap_answers_the_jive_window(tmp_path, monkeypatch):
    """``menu:browselibrary`` ist Menue-Modus — kein flaches ``loop_loop``.

    Die Extra-Parameter (item_id/isContextMenu/folder_id/url) dürfen den
    Zweig nicht verdrängen: der Tap liefert die Kinder des Ordners im
    Jive-Fenster (Perl-Form).
    """
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    metal = next(it for it in _items(["items", "0", "50", "menu:1", "mode:bmf"])
                 if it["text"] == "Metal")
    res = _browse(_app_tap(metal, 0, 300))
    assert "loop_loop" not in res, res
    assert res["count"] == 2                    # direkte Kinder, nicht rekursiv
    assert res["offset"] == 0
    assert [it["text"] for it in res["item_loop"]] == ["Accept", "Iron Maiden"]
    assert res["window"] == {"windowStyle": "text_list"}
    # die Basis-Aktion der Antwort ist wieder der eigene Feed (Perl) — inklusive
    # des Drill-Tokens des FENSTERS (live Perl: params {mode, folder_id, menu})
    go = res["base"]["actions"]["go"]
    assert go["params"] == {"mode": "bmf", "folder_id": str(metal["id"]),
                            "menu": "browselibrary"}
    assert go["itemsParams"] == "params"
    # Live Perl 9.1.1, gleiche Anfrageform gegen den Aerosmith-Ordner:
    # ``{count: 10, offset: 0, item_loop: [… 10 Dateien …], window, base,
    # title}`` (10 = ``readDirectory``-Kinder, nicht die rekursive Gesamtzahl)


def test_bmf_window_past_the_end_has_no_item_loop(tmp_path, monkeypatch):
    """``start`` jenseits der Kinderzahl: Perls ungültiges Fenster.

    Perl ``normalize`` (``Slim/Control/Request.pm:1805-1839``) verwirft das
    Fenster (``if ($from > $lastidx) { return ($valid, 0, 0) }``), der
    Builder fügt dann nur ``count``/``offset``/``window`` an
    (``XMLBrowser.pm:851,1420,1450``).  Live Perl 9.1.1 ``browselibrary items
    303 100 menu:1 mode:bmf`` → keys ``count``/``offset``/``window``.
    """
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    metal = next(it for it in _items(["items", "0", "50", "menu:1", "mode:bmf"])
                 if it["text"] == "Metal")
    res = _browse(_app_tap(metal, 119100, 100))
    assert res["count"] == 2                    # die Kinderzahl bleibt stehen
    assert res["offset"] == 119100
    assert "item_loop" not in res, res          # kein (leerer) Loop
    assert "loop_loop" not in res, res
    assert "base" not in res
    assert res["window"] == {"windowStyle": "text_list"}


def test_bmf_app_pager_terminates(tmp_path, monkeypatch):
    """Der Pager der App läuft nicht mehr endlos (Regression Endlos-Blättern).

    Squeeze Client lädt ``PagingConfig(100)`` mit ``initialLoadSize`` 300 und
    hängt die nächste Seite an, solange ``items.size + offset < count``
    (``ui/common/BasePagingListFragment.kt:124-133`` →
    ``model/ListResponse.kt:27``; ``offset`` ist dort 0, wenn die Antwort
    keines schickt).  Vor dem Fix hatte der Tap-Antwort kein ``item_loop``
    und kein ``offset`` → ``0 + 0 < 2`` blieb wahr, der Client fragte
    ``start = 100, 200, 300, …`` ohne Ende (Live-Log: bis 318300).
    """
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    metal = next(it for it in _items(["items", "0", "50", "menu:1", "mode:bmf"])
                 if it["text"] == "Metal")

    page_size, initial = 100, 300
    page, requested, loaded = 0, [], []
    for _ in range(50):                        # der Client-Loop
        start = page * (initial if page == 0 else page_size)
        res = _browse(_app_tap(metal, start, initial if page == 0 else page_size))
        items = res.get("item_loop") or []
        offset = res.get("offset", 0)
        requested.append(start)
        loaded += [it["text"] for it in items]
        if not (items and len(items) + offset < res.get("count", 0)):
            break                              # serverHasMoreData == false
        page += 1
    assert requested == [0], requested         # genau EINE Seite, dann Ende
    assert loaded == ["Accept", "Iron Maiden"]


def test_bmf_flat_form_keeps_the_opensqueeze_shape(tmp_path, monkeypatch):
    """Ohne ``menu:``-Parameter bleibt die modelose Form (kein Umbau)."""
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    metal = next(it for it in _items(["items", "0", "50", "menu:1", "mode:bmf"])
                 if it["text"] == "Metal")
    res = _browse(["items", "0", "300", "mode:bmf", f"folder_id:{metal['id']}"])
    assert "item_loop" not in res
    assert res["count"] == 2
    assert [it["text"] for it in res["loop_loop"]] == ["Accept", "Iron Maiden"]


# ---------------------------------------------------------------------------
# (h) Datei-Tap im Musikordner: Perls Item-Form + der daraus gebaute Aufruf
#
# Symptom: „Im Musikordner erscheinen jetzt Unterordner und Audiodateien, aber
# Anklicken spielt sie nicht ab" — im Live-Log der App kam beim Tap auf eine
# Datei KEIN Abspiel-Kommando an.  Ursache war die Item-Form: Perls bmf-Zeile
# für eine Audiodatei trägt ``goAction: 'play'``, ``style: 'itemplay'`` und
# ``params`` (``touchToPlay``/``item_id``/``isContextMenu``), unsere nur
# ``commonParams`` + Item-Aktionen — und Jive bricht den Tap bei einer
# Basis-Aktion ohne passenden ``itemsParams``-Eintrag still ab
# (``SlimBrowserApplet.lua:2003-2026`` „No params entry in item, no action
# taken").
# ---------------------------------------------------------------------------

#: Musikordner ohne Unterordner — nur Dateien (Reihenfolge = URL-Sortierung).
ONLY_FILES_DIR = "Only/Accept"
ONLY_FILES_ROWS = [
    (101, f"file://{ROOT}/{ONLY_FILES_DIR}/01-hard_attack.mp3"),
    (102, f"file://{ROOT}/{ONLY_FILES_DIR}/02-objection.mp3"),
]


def _only_files_browse():
    return _browse(["items", "0", "50", "menu:1", "mode:bmf",
                    f"folder_id:{ROOT}/{ONLY_FILES_DIR}"])


def test_bmf_audio_file_item_is_perl_shaped(tmp_path, monkeypatch):
    """Die Dateizeile trägt Perls Tap-Felder (live Perl: ``goAction: 'play'``,
    ``style: 'itemplay'``, ``params {item_id, touchToPlay, isContextMenu}``,
    ``actions.more → trackinfo``, ``presetParams``)."""
    _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)

    item = _only_files_browse()["item_loop"][0]
    assert item["type"] == "audio"
    assert item["text"] == "01-hard_attack.mp3"
    # XMLBrowser.pm:1265/:1267 — goAction + style der touch-to-play-Zeile
    assert item["goAction"] == "play"
    assert item["style"] == "itemplay"
    # XMLBrowser.pm:1142/:1146/:1261 — params der Zeile
    assert item["params"]["item_id"] == "0"
    assert item["params"]["touchToPlay"] == "0"
    assert item["params"]["isContextMenu"] == 1
    # dieses Ports auflösbarer Token für dieselbe Zeile (_bmf_tap_tracks)
    assert item["params"]["track_id"] == 101
    # BrowseLibrary.pm:2091-2096 — itemActions info → trackinfo
    assert item["actions"]["more"]["cmd"] == ["trackinfo", "items"]
    assert item["actions"]["more"]["params"]["track_id"] == 101
    assert item["actions"]["more"]["window"] == {"isContextMenu": 1}
    # XMLBrowser.pm:1131-1136 — isPlayable-Zeilen tragen presetParams
    assert item["presetParams"] == {
        "favorites_type": "audio",
        "favorites_title": "01-hard_attack.mp3",
        "favorites_url": f"file://{ROOT}/{ONLY_FILES_DIR}/01-hard_attack.mp3",
    }
    # keine Item-Aktion, an der der Tap hängen bleibt: ``play``/``add`` kommen
    # aus ``base.actions`` (Perl) und werden über ``itemsParams: params``
    # ergänzt.
    assert "commonParams" not in item
    assert "play" not in item["actions"]


def test_bmf_all_file_window_go_is_the_feed_play(tmp_path, monkeypatch):
    """Sind ALLE Zeilen des Fensters touch-to-play, ersetzt Perl
    ``base.actions.go`` durch ``base.actions.play`` (XMLBrowser.pm:1429-1430)
    — live Perl 9.1.1 (Accept) antwortet genau so; ``count``/``offset``/
    ``window`` bleiben unverändert."""
    _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)

    res = _only_files_browse()
    actions = res["base"]["actions"]
    assert actions["go"] == actions["play"]
    assert actions["play"]["cmd"] == ["browselibrary", "playlist", "play"]
    assert actions["play"]["itemsParams"] == "params"
    assert actions["play"]["nextWindow"] == "nowPlaying"
    # Perl-Fixture desselben Fensters (live, Accept): dieselbe Ersetzung,
    # dieselben params-Schlüssel {mode, folder_id, menu}
    perl_actions = json.loads(PERL_FILE_FIXTURE.read_text())["result"]["base"]["actions"]
    assert perl_actions["go"] == perl_actions["play"]
    assert set(perl_actions["play"]["params"]) == {"menu", "folder_id", "mode"}
    assert set(actions["play"]["params"]) <= {"mode", "menu", "folder_id"}
    # presetParams einer Zeile → Perls $presetFavSet (XMLBrowser.pm:1427)
    assert "set-preset-0" in actions
    assert res["count"] == 2 and res["offset"] == 0
    assert res["window"] == {"windowStyle": "text_list"}


def test_bmf_root_window_base_has_no_folder_id(tmp_path, monkeypatch):
    """Das WURZEL-Fenster trägt kein ``folder_id`` — Perl hat dort keinen
    Drill-Token (live ``browselibrary items 0 3 menu:1 mode:bmf`` →
    ``params {mode: menu}``); ein gedrilltes Fenster schon (Fixture oben)."""
    perl = json.loads(PERL_FOLDER_FIXTURE.read_text())["result"]
    assert set(perl["base"]["actions"]["go"]["params"]) == {"mode", "menu"}

    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    ours = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    assert set(ours["base"]["actions"]["go"]["params"]) == {"mode", "menu"}


def test_bmf_mixed_window_keeps_the_drill_go(tmp_path, monkeypatch):
    """Enthält das Fenster einen Ordner, bleibt ``go`` der Drill
    (XMLBrowser.pm:1275 löscht ``$allTouchToPlay``) — sonst würde jeder
    Ordner-Tap den Elternordner abspielen."""
    rows = ONLY_FILES_ROWS + [
        (103, f"file://{ROOT}/{ONLY_FILES_DIR}/Deep/03-deep.mp3"),
    ]
    _setup(tmp_path, monkeypatch, rows, ROOT)

    folder_item = next(it for it in _browse(
        ["items", "0", "50", "menu:1", "mode:bmf",
         f"folder_id:{ROOT}/{ONLY_FILES_DIR}"])["item_loop"]
        if it["type"] == "playlist")
    res = _browse(["items", "0", "50", "menu:1", "mode:bmf",
                   f"folder_id:{ROOT}/{ONLY_FILES_DIR}"])
    go = res["base"]["actions"]["go"]
    assert go["cmd"] == ["browselibrary", "items"]
    assert go["params"]["mode"] == "bmf"
    assert go["params"]["menu"] == "browselibrary"
    # der Ordner selbst bleibt ohne ``goAction`` (Basis-``go`` = Drill)
    assert "goAction" not in folder_item
    # ``folder_id`` ist die ``tracks``-Zeile des Ordners (hier die beim
    # Auflisten angelegte dir-Zeile) — Perls Drill-Token; ``id`` bleibt die
    # Ganzzahl, die die Controller parsen.
    assert str(folder_item["params"]["folder_id"]).isdigit()
    assert isinstance(folder_item["id"], int)


class _TapHandler:
    def __init__(self):
        self.strm = []

    async def send_strm_to_player(self, mac, track_id):
        self.strm.append(track_id)
        return True

    async def send_remote_stream(self, mac, url, codec="m", **kw):
        self.strm.append(url)
        return True


class _TapPlayer:
    def __init__(self):
        self.mac = MAC
        self.power = False
        self.mode = "stop"
        self.playlist = []
        self.playlist_position = 0
        self.playlist_total = 0
        self.playlist_modified = 0
        self.remote = 0
        self.last_activity = 0.0


class _TapPM:
    def __init__(self):
        self.player = _TapPlayer()
        self._protocol_handler = _TapHandler()
        self.modes = []

    def get_player(self, mac):
        return self.player if mac == MAC else None

    def get_all_players(self):
        return [self.player]

    async def power_on_for_playback(self, player):
        player.power = True

    def set_mode(self, mac, mode):
        self.modes.append(mode)
        self.player.mode = mode


#: Die Form, die SqueezePlay aus ``base.actions.play`` + den Zeilen-``params``
#: baut: ``cmd`` + ``from`` + ``qty`` + Parameter
#: (SlimBrowserApplet.lua:756-767).
def _file_tap_args(item: dict, folder: str) -> list[str]:
    return ["playlist", "play", "0", "50", "useContextMenu:1", "mode:bmf",
            f"folder_id:{folder}", "menu:browselibrary",
            f"item_id:{item['params']['item_id']}",
            f"touchToPlay:{item['params']['touchToPlay']}",
            "isContextMenu:1",
            f"track_id:{item['params']['track_id']}"]


def test_bmf_file_tap_plays_exactly_the_tapped_track(tmp_path, monkeypatch):
    """Der Tap startet GENAU den angetippten Titel (nicht den Ordner, nicht
    den Fenster-``from``-Index): Queue = [102], ``mode=play``, ``strm`` an den
    Player."""
    _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)
    item = _only_files_browse()["item_loop"][1]        # 02-objection (id 102)
    assert item["params"]["track_id"] == 102

    pm = _TapPM()
    args = _file_tap_args(item, f"{ROOT}/{ONLY_FILES_DIR}")
    asyncio.run(api_mod.JSONRPCAPI()._bmf_playlist(
        MAC, "play", args[2:], pm=pm))

    assert pm.player.playlist == [102]
    assert pm.player.playlist_total == 1
    assert pm.player.playlist_position == 0
    assert pm.player.mode == "play"
    assert pm._protocol_handler.strm == [102]


def test_bmf_file_tap_resolves_the_row_without_track_id(tmp_path, monkeypatch):
    """Ohne ``track_id`` (Perls eigene Form: nur ``item_id``/``touchToPlay``)
    findet der Server die Zeile über Fenster-``folder_id`` + Index."""
    _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)
    res = _only_files_browse()
    item = res["item_loop"][1]
    args = ["playlist", "play", "0", "50", "useContextMenu:1", "mode:bmf",
            f"folder_id:{ROOT}/{ONLY_FILES_DIR}", "menu:browselibrary",
            f"item_id:{item['params']['item_id']}",
            f"touchToPlay:{item['params']['touchToPlay']}",
            "isContextMenu:1"]
    pm = _TapPM()
    asyncio.run(api_mod.JSONRPCAPI()._bmf_playlist(
        MAC, "play", args[2:], pm=pm))
    assert pm.player.playlist == [102]


def test_bmf_folder_row_tap_loads_the_whole_folder(tmp_path, monkeypatch):
    """Perls ``playall`` für eine Ordnerzeile: der ganze Ordner wandert in die
    Queue (BrowseLibrary.pm:2084)."""
    rows = ONLY_FILES_ROWS + [(103, f"file://{ROOT}/{ONLY_FILES_DIR}/x.mp3")]
    _setup(tmp_path, monkeypatch, rows, ROOT)
    res = _only_files_browse()
    args = ["playlist", "play", "0", "50", "useContextMenu:1", "mode:bmf",
            f"folder_id:{ROOT}", "menu:browselibrary",
            f"item_id:{res['item_loop'][0]['params']['item_id']}",
            f"touchToPlay:{res['item_loop'][0]['params']['touchToPlay']}",
            "isContextMenu:1"]
    # Fenster = Wurzel, Zeile 0 = Ordner "Only" → alle Tracks darunter
    pm = _TapPM()
    asyncio.run(api_mod.JSONRPCAPI()._bmf_playlist(
        MAC, "play", args[2:], pm=pm))
    assert pm.player.playlist == [101, 102, 103]
    assert pm.player.mode == "play"


def test_bmf_file_tap_add_and_insert_do_not_start(tmp_path, monkeypatch):
    """``add``/``insert`` der Zeile ändern nur die Queue (Perl startet dabei
    nicht; ``insert`` landet direkt hinter dem laufenden Titel)."""
    _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)
    item = _only_files_browse()["item_loop"][1]

    pm = _TapPM()
    pm.player.playlist = [999]
    pm.player.playlist_position = 0
    asyncio.run(api_mod.JSONRPCAPI()._bmf_playlist(
        MAC, "add", _file_tap_args(item, f"{ROOT}/{ONLY_FILES_DIR}")[2:],
        pm=pm))
    assert pm.player.playlist == [999, 102]
    assert pm.player.mode == "stop"
    assert pm._protocol_handler.strm == []

    pm = _TapPM()
    pm.player.playlist = [999, 888]
    pm.player.playlist_position = 0
    asyncio.run(api_mod.JSONRPCAPI()._bmf_playlist(
        MAC, "insert", _file_tap_args(item, f"{ROOT}/{ONLY_FILES_DIR}")[2:],
        pm=pm))
    assert pm.player.playlist == [999, 102, 888]
    assert pm.player.mode == "stop"


def test_bmf_tap_reaches_the_feed_playlist_action(tmp_path, monkeypatch):
    """``browselibrary playlist …`` mit ``mode:bmf`` läuft über den
    Feed-Zweig — nur ein Tap eines ANDEREN Feeds geht an das generische
    ``playlist``-Kommando."""
    _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)
    item = _only_files_browse()["item_loop"][0]

    seen = {"bmf": [], "generic": []}

    async def fake_bmf(self, pid, sub, rest, pm=None):
        seen["bmf"].append((sub, list(rest)))

    async def fake_control(self, pm, pid, cmd, args):
        seen["generic"].append((cmd, list(args)))

    monkeypatch.setattr(api_mod.JSONRPCAPI, "_bmf_playlist", fake_bmf)
    monkeypatch.setattr(api_mod.JSONRPCAPI, "_json_control", fake_control)
    api = api_mod.JSONRPCAPI()

    asyncio.run(api._json_browselibrary(
        "browselibrary", _file_tap_args(item, f"{ROOT}/{ONLY_FILES_DIR}"), MAC))
    assert seen["bmf"] and seen["bmf"][0][0] == "play"
    assert seen["generic"] == []

    asyncio.run(api._json_browselibrary(
        "browselibrary", ["playlist", "play", "0", "50", "mode:albums",
                          "menu:browselibrary", "album_id:7"], MAC))
    assert seen["generic"] == [("playlist", ["play", "0", "50",
                                             "mode:albums",
                                             "menu:browselibrary",
                                             "album_id:7"])]
    assert len(seen["bmf"]) == 1


# ---------------------------------------------------------------------------
# (i) Rohvergleich gegen die live Perl-Zeilen (fixtures/) — Datei und Ordner
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: live Perl 9.1.1, 192.168.1.90, Player ``24:0a:c4:29:77:90`` (gestoppt),
#: ``browselibrary items 0 3 menu:1 mode:bmf folder_id:204573`` (Accept).
PERL_FILE_FIXTURE = _FIXTURES / "perl_bmf_folder_files_menu.json"
#: dieselbe Query ohne ``folder_id`` (Wurzel) — erste Zeile ist ein Ordner.
PERL_FOLDER_FIXTURE = _FIXTURES / "perl_bmf_top_folders_menu.json"
#: dieselbe Datei-Query OHNE Player-Parameter: ``_defeatDestructiveTouchToPlay``
#: liefert ohne Client immer 1 (``XMLBrowser.pm:1976``) → ``playControl``-Form.
PERL_FILE_PLAYING_FIXTURE = _FIXTURES / "perl_bmf_file_item_playing.json"


def _fixture_item(path: Path, n: int = 0) -> dict:
    return json.loads(path.read_text())["result"]["item_loop"][n]


def test_bmf_file_item_matches_the_live_perl_row(tmp_path, monkeypatch):
    """Feld-für-Feld gegen die echte Perl-Zeile: gleiche Tap-Felder.

    Die Artwork-Keys fehlen hier nur, weil dieses Fixture-``tracks``-Schema
    keine ``albums``/``tracks_albums``-Zeilen hat — mit Album-Artwork trägt
    unsere Zeile dieselben Keys wie Perl
    (``test_bmf_file_rows_carry_perls_artwork_fields``).
    """
    perl = _fixture_item(PERL_FILE_FIXTURE)
    _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)
    ours = _only_files_browse()["item_loop"][0]

    assert perl["type"] == ours["type"] == "audio"
    assert perl["goAction"] == ours["goAction"] == "play"
    assert perl["style"] == ours["style"] == "itemplay"
    assert set(perl["params"]) == {"item_id", "touchToPlay", "isContextMenu"}
    assert set(perl["params"]) <= set(ours["params"])
    # Perls ``item_id`` ist der Index-PFAD in seinen Feed-Cache
    # (``<feed-sid>.<index>``, XMLBrowser.pm:334-384); die letzte Komponente
    # ist der Zeilenindex, den dieses Port direkt führt.
    assert perl["params"]["item_id"].split(".")[-1] == "0"
    assert perl["params"]["touchToPlay"] == perl["params"]["item_id"]
    assert ours["params"]["item_id"] == "0"
    assert ours["params"]["touchToPlay"] == "0"
    assert ours["params"]["isContextMenu"] == 1
    assert perl["actions"]["more"]["cmd"] == ours["actions"]["more"]["cmd"]
    assert set(perl["presetParams"]) <= set(ours["presetParams"]) | {
        "icon"}          # unser row ohne coverid: kein icon (siehe window_style)
    assert set(ours["presetParams"]) == {"favorites_type", "favorites_title",
                                         "favorites_url"}
    # Kein Album-Artwork in diesem Fixture-Schema ⇒ kein ``icon``; Perl hat
    # hier ``coverid`` (``BrowseLibrary.pm:2098-2102``).  Mit Artwork liefert
    # unser Feed dieselben Keys (test_bmf_file_rows_carry_perls_artwork_fields).
    assert set(perl) - set(ours) == {"icon", "icon-id"}
    assert not (set(ours) - set(perl))
    # unser zusätzlicher, auflösbarer Token (Perl liest seinen Feed-Cache)
    assert set(ours["params"]) - set(perl["params"]) == {"track_id"}


def test_bmf_folder_item_matches_the_live_perl_row(tmp_path, monkeypatch):
    """Die Ordnerzeile trägt dieselben Felder wie Perl — ohne ``goAction``
    (der Tap läuft über ``base.actions.go`` = Drill)."""
    perl = _fixture_item(PERL_FOLDER_FIXTURE)
    _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    ours = next(it for it in _items(["items", "0", "50", "menu:1", "mode:bmf"])
                if it["type"] == "playlist")

    assert perl["type"] == ours["type"] == "playlist"
    assert "goAction" not in perl and "goAction" not in ours
    assert set(perl["params"]) == {"item_id", "isContextMenu"}
    assert {"item_id", "isContextMenu"} <= set(ours["params"])
    assert set(perl["actions"]) == {"add", "add-hold", "play", "more"}
    # unser ``add``/``add-hold``/``play`` sind Perls ``playlistcontrol``-Form;
    # ``more`` fehlt uns, weil ``folderinfo`` (Perl :2067-2070) hier noch nicht
    # implementiert ist — dokumentierte Abweichung.
    assert {"add", "add-hold", "play"} <= set(ours["actions"])
    for key, cmd in (("add", ["playlistcontrol"]),
                     ("add-hold", ["playlistcontrol"]),
                     ("play", ["playlistcontrol"])):
        assert ours["actions"][key]["cmd"] == perl["actions"][key]["cmd"] == cmd
        assert ours["actions"][key]["params"]["cmd"] == \
            perl["actions"][key]["params"]["cmd"]
    # unser Drill-Vokabular (folder_id/url) zusätzlich zu Perls ``item_id``
    assert {"folder_id", "url"} <= set(ours["params"])


def test_bmf_file_row_is_always_the_touch_to_play_form(tmp_path, monkeypatch):
    """Dokumentierte Abweichung: Perl schaltet je nach Player-Zustand um.

    ``_defeatDestructiveTouchToPlay`` (XMLBrowser.pm:1951-1983) gibt für den
    Vorgabe-Pref 4 ``isPlaying``-abhängig 1 zurück — und ohne Client SOGAR
    immer (``:1976`` ``return 1 if $pref == 1 || !$client``); dann trägt die
    Zeile ``goAction: 'playControl'`` + ``playControlParams``
    (:1268-1272) und der Tap öffnet Perls Play-Control-Kontextmenü.  Dieses
    Port liefert immer die touch-to-play-Form (der Fall der Live-App:
    gestoppt), d.h. ein Tap spielt direkt — ohne den CM-Zwischenschritt.
    """
    playing = _fixture_item(PERL_FILE_PLAYING_FIXTURE)
    assert playing["goAction"] == "playControl"
    assert playing["playControlParams"] == {"xmlbrowserPlayControl": "0"}
    assert "touchToPlay" not in playing["params"]

    _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)
    ours = _only_files_browse()["item_loop"][0]
    assert ours["goAction"] == "play"
    assert "playControlParams" not in ours
    assert ours["params"]["touchToPlay"] == "0"




# ---------------------------------------------------------------------------
# (j) Artwork der Dateizeilen — Perls ``_bmf``-Regel (das Cover in SqueezePlay)
# ---------------------------------------------------------------------------

def _add_album_artwork(db: str, albums, links) -> None:
    """Legt ``albums``/``tracks_albums`` an, wie der Importer sie füllt."""
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS albums (id INTEGER PRIMARY KEY, "
        "titlesort VARCHAR(255) NOT NULL, title VARCHAR(255) NOT NULL, "
        "artwork VARCHAR(1000));"
        "CREATE TABLE IF NOT EXISTS tracks_albums (track INTEGER NOT NULL, "
        "album INTEGER NOT NULL, position INTEGER NOT NULL, "
        "PRIMARY KEY (track, album));")
    con.executemany(
        "INSERT OR REPLACE INTO albums (id, titlesort, title, artwork)"
        " VALUES (?, '', ?, ?)", [(aid, f"Album {aid}", art)
                                  for aid, art in albums])
    con.executemany(
        "INSERT OR REPLACE INTO tracks_albums (track, album, position)"
        " VALUES (?, ?, 0)", links)
    con.commit()
    con.close()


def test_bmf_file_rows_carry_perls_artwork_fields(tmp_path, monkeypatch):
    """``icon``/``icon-id``/``presetParams.icon`` an der Dateizeile.

    Perl ``Slim/Menu/BrowseLibrary.pm:2097-2100`` (``_bmf``)::

        if ( $_->{'coverid'} ) {
            $_->{'image'} = 'music/' . $_->{'coverid'} . '/cover';
            $_->{'artwork_track_id'} = $_->{'coverid'};
        }

    ``Slim/Control/XMLBrowser.pm:1160-1169`` macht daraus ``icon`` (aus
    ``image``, weil der Pfad nicht ``https?:`` ist, ``proxiedImage`` reicht
    relative Pfade unverändert durch, ``ImageProxy.pm:443-447``) und
    ``icon-id`` (aus ``artwork_track_id``); beides setzt ``$hasImage`` und
    damit ``windowStyle`` (``:1434-1441``).  Live Perl 9.1.1, 2026-09-19,
    ``browselibrary items 0 4 menu:1 mode:bmf folder_id:204776``::

        {"type": "audio", "text": "01. Queen - Somebody To Love.mp3",
         "icon": "music/42a671d3/cover", "icon-id": "42a671d3",
         "presetParams": {…, "icon": "music/42a671d3/cover"}}

    Dieses Port veröffentlicht als Id die *Album*-Id — genau die Id, die seine
    ``/music/<id>/cover(_<w>x<h>_<m|f>).jpg``-Route auflöst (``_artwork_id``,
    dieselbe Regel wie die ``albums``-/``status``-Zeilen).  Ein Album ohne
    ``artwork`` bekommt kein ``icon`` (Perls ``if $_->{coverid}``).
    """
    db = _setup(tmp_path, monkeypatch, ONLY_FILES_ROWS, ROOT)
    _add_album_artwork(db, [(7, "/srv/covers/front.jpg"), (8, "")],
                       [(101, 7), (102, 8)])
    browse = _only_files_browse()
    items = browse["item_loop"]
    first = next(it for it in items if "01-hard_attack" in it["text"])
    assert first["icon"] == "music/7/cover"
    assert first["icon-id"] == "7"
    assert first["presetParams"]["icon"] == "music/7/cover"
    # Album ohne ``artwork``: Perls ``if $_->{coverid}`` ist falsch
    second = next(it for it in items if "02-objection" in it["text"])
    assert "icon" not in second and "icon-id" not in second
    assert "icon" not in second["presetParams"]
    # ``$hasImage`` ⇒ windowStyle home_menu (XMLBrowser.pm:1160-1169, :1434-1441)
    # — live Perl antwortet genau das für dieselbe Datei-Ebene.
    assert browse["window"] == {"windowStyle": "home_menu"}


def test_bmf_folder_rows_never_carry_artwork(tmp_path, monkeypatch):
    """Ordnerzeilen ohne Bild — Perl setzt dort nichts (:2051-2083)."""
    db = _setup(tmp_path, monkeypatch, LIB_URLS, ROOT)
    _add_album_artwork(db, [(7, "/srv/covers/front.jpg")],
                       [(1, 7), (2, 7), (3, 7)])
    items = _items(["items", "0", "50", "menu:1", "mode:bmf"])
    folders = [it for it in items if it["type"] == "playlist"]
    assert folders
    assert not any("icon" in it or "icon-id" in it for it in folders)


def test_bmf_file_artwork_from_the_readdirectory_path(tmp_path, monkeypatch):
    """Dieselben Felder, wenn die Datei-Ids aus dem Verzeichnis-Listing kommen."""
    root = tmp_path / "Musik"
    (root / "Album").mkdir(parents=True)
    track = root / "Album" / "01-track.mp3"
    track.write_bytes(b"\0")
    db = _setup(tmp_path, monkeypatch,
                [(1, folders.file_url_from_path(track))], str(root))
    _add_album_artwork(db, [(9, "/srv/covers/x.jpg")], [(1, 9)])
    items = _items(["items", "0", "50", "menu:1", "mode:bmf",
                    f"search:{root}/Album"])
    assert len(items) == 1
    assert items[0]["icon"] == "music/9/cover"
    assert items[0]["icon-id"] == "9"
