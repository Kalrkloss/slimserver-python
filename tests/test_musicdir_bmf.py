"""R0.6-B (LIVE-07) — „Musikordner“ (mode:bmf) aus der Bibliothek aufbauen.

Der Ordnerbaum wird NICHT per Dateisystem-Readwalk gebaut (SMB, 121k
Dateien), sondern aus den ``file://``-Track-URLs der Bibliothek
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

Von den 303 Perl-Einträgen haben 253 einen indexierten Track (die
restlichen 50 sind leere Ordner / Nicht-Audio-Dateien, die die
URL-Aggregation bewusst nicht kennt).  Perl identifiziert Ordner mit
numerischen ``folder_id``s; hier ist die ``folder_id`` der absolute
Verzeichnispfad, damit ``mode:bmf&folder_id:<pfad>`` direkt drillt.
"""

from __future__ import annotations

import asyncio
import sqlite3
import urllib.parse
from pathlib import Path

import pytest

from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

# ---------------------------------------------------------------------------
# R0.6-B fixtures
# ---------------------------------------------------------------------------

ROOT = "/srv/music"

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


def _make_db(path: Path, rows, with_title: bool = True) -> str:
    con = sqlite3.connect(path)
    if with_title:
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


def _db(tmp_path: Path, rows, with_title: bool = True) -> str:
    return _make_db(tmp_path / "lyrion.db", rows, with_title)


def _setup(tmp_path, monkeypatch, rows, pref: str, with_title: bool = True):
    """Temp library DB + monkeypatched musicdir runtime pref."""
    db = _db(tmp_path, rows, with_title)
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
    """Ordner-Items: type playlist, id/folder_id = virtuelle numerische ID,
    textkey, go/play/add-Aktionen (Perl-BrowseLibrary-Form).

    Perl liefert hier seine ``tracks.id`` (Verzeichnisse liegen bei Perl als
    ``content_type='dir'`` in ``tracks``); unser Port legt keine dir-Zeilen an
    und gibt deshalb ``crc32(pfad) & 0x7fffffff`` aus — stabil, numerisch, aber
    bewusst nicht identisch mit Perls ID. Der Client parst die ID numerisch,
    deshalb darf hier keine URL/kein Pfad stehen."""
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
    """Decoded Pref + percent-encoded Track-URLs (smb-share%3A…) matchen."""
    _setup(tmp_path, monkeypatch, GVFS_URLS, GVFS_ROOT)
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
    assert audio["commonParams"]["track_id"] == 1


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
    _setup(tmp_path, monkeypatch, [], ROOT)
    res = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    assert res["count"] == 0
    assert res["item_loop"] == []

    plain = _browse(["items", "0", "50", "mode:bmf"])
    assert plain["count"] == 0
    assert not (plain.get("loop_loop") or [])


def test_bmf_empty_library_without_pref(tmp_path, monkeypatch):
    """Kein Pref, keine Tracks → keine Wurzel, kein Fehler."""
    _setup(tmp_path, monkeypatch, [], "")
    res = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    assert res["count"] == 0
    assert res["item_loop"] == []


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
    """Ein einzelner Track darf keine Wurzel definieren."""
    _setup(tmp_path, monkeypatch,
           [(1, "file:///home/keiner/Music/lonely.mp3")], "")
    res = _browse(["items", "0", "50", "menu:1", "mode:bmf"])
    assert res["count"] == 0
    assert res["item_loop"] == []


def test_bmf_missing_url_column_does_not_raise(tmp_path, monkeypatch):
    """Schlanke/abweichende DBs (tracks ohne url) → leerer Ordner."""
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
    assert res["count"] == 0


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

    # minimal DB without a title column behaves the same
    db = _make_db(tmp_path / "minimal.db", [], with_title=False)
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
