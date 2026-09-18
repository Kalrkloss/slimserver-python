"""Perl-Form der Alben-Antwort — Regression für den Squeeze-Client-Absturz.

Symptom (2026-09-18): Squeeze Client (de.maniac103.squeezeclient) starb mit
``java.lang.OutOfMemoryError`` (192 MB Heap, Waydroid), sobald „Eigene Musik →
Alben" geöffnet wurde. Der Client fragt dabei ``albums 0 2147483647 tags:yE``
(un-gepagte Albumliste fürs Jahres-Mapping, ``FetchAlbumInfoRequest``).

Ursache: ``_browse_response`` veröffentlichte dieselbe Liste dreifach —
``loop_loop`` + ``item_loop`` + ``albums_loop``. Für 13757 Alben ergab das
22.07 MB gegen Perls 1.01 MB (live 192.168.1.90, read-only).

Perl-Form (Queries.pm:746 ``my $loopname = 'albums_loop';`` — Request.pm
unrollt genau diesen einen Loop):

    albums 0 3   →  {"albums_loop": […], "count": 7189}

Die Feed-Ebene benennt ihren Loop dagegen modusabhängig (XMLBrowser.pm:582/846:
``$menuMode ? 'item_loop' : 'loop_loop'``).
"""

import asyncio
import json
import sqlite3

from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

ALBUM_COUNT = 5
# Die un-gepagte Crash-Anfrage des Clients.
CRASH_ARGS = ["0", "2147483647", "tags:yE"]


def _db(tmp_path):
    """Bibliothek mit Alben, einem dir-Baum und einer dir-Zeile."""
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, url TEXT,
                             audio INTEGER DEFAULT 1, content_type TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        INSERT INTO albums (id, title, year, artwork) VALUES
            (10, 'Achtung Baby', 1991, 'art10'),
            (11, 'Blue Lines',   1991, NULL),
            (12, 'Crooked Rain', 1994, 'art12'),
            (13, 'Definitely Maybe', 1994, NULL),
            (14, 'The Bends',    1995, 'art14');
        INSERT INTO tracks (id, title, url, content_type) VALUES
            (1, 'Zoo Station', 'file:///music/U2/a.mp3', 'file'),
            (2, 'Safe From Harm', 'file:///music/Massive/b.mp3', 'file'),
            -- ein Ordner: content_type='dir', kein tracks_albums-Link
            (3, 'U2', 'file:///music/U2', 'dir');
        INSERT INTO tracks_albums (track, album) VALUES (1, 10), (2, 11);
        INSERT INTO contributors (id, name) VALUES (1, 'U2');
        INSERT INTO tracks_contributors (track, contributor, role) VALUES (1, 1, 1);
        """
    )
    con.commit()
    con.close()
    return str(db)


def _ensure_db(tmp_path):
    if not (tmp_path / "lyrion.db").exists():
        _db(tmp_path)


def _albums(tmp_path, monkeypatch, args):
    _ensure_db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path",
                        lambda: str(tmp_path / "lyrion.db"))

    async def run():
        return await JSONRPCAPI()._json_browse("albums", args)

    return asyncio.run(run())


def _browselibrary(tmp_path, monkeypatch, args):
    _ensure_db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path",
                        lambda: str(tmp_path / "lyrion.db"))

    async def run():
        return await JSONRPCAPI()._json_browselibrary("browselibrary", args)

    return asyncio.run(run())


# ── 1. Top-Level: genau EIN Loop, unter Perls Namen ────────────────────────

def test_albums_query_answers_one_perl_named_loop(tmp_path, monkeypatch):
    """``albums 0 <n>`` → ``count`` + ``albums_loop``, keine Aliase.

    ``offset`` bleibt zusätzlich stehen (Perl führt es in den JSON-Queries
    nicht, live ``albums 0 3`` → ``{"albums_loop":[…], "count":7189}``); es
    ist ein 10-Byte-Rest des gemeinsamen Helpers und nicht Crash-relevant.
    """
    res = _albums(tmp_path, monkeypatch, ["0", "10"])
    assert set(res) == {"count", "albums_loop", "offset"}, sorted(res)
    assert res["count"] == ALBUM_COUNT
    assert len(res["albums_loop"]) == ALBUM_COUNT
    for alias in ("loop_loop", "item_loop"):
        assert alias not in res, f"non-Perl alias '{alias}' is back"


def test_unpaged_crash_probe_does_not_repeat_the_list(tmp_path, monkeypatch):
    """``albums 0 2147483647 tags:yE`` darf die Liste nicht verdreifachen.

    Vorher (gemessen 2026-09-18): 22.07 MB gegen Perl 1.01 MB → OOM im
    Squeeze Client. Jetzt: genau ein Loop, also ~1x.
    """
    res = _albums(tmp_path, monkeypatch, CRASH_ARGS)
    assert set(res) == {"count", "albums_loop", "offset"}
    assert res["count"] == ALBUM_COUNT
    # Die Liste steht genau einmal im Payload — kein zweiter/dritter Zwilling.
    payload = json.dumps(res)
    assert payload.count('"albums_loop"') == 1
    assert len(payload) < 2 * len(json.dumps(res["albums_loop"]))


# ── 2. Item-Form (SqueezeClient ``AlbumInfoListResponse``: id + year) ─────

def test_album_item_shape_matches_perl(tmp_path, monkeypatch):
    """Item trägt Perls Felder in Perls Typen.

    Perl ``albums_loop``-Item (ohne tags:): ``id``, ``album``, ``performance``,
    ``favorites_url``, ``favorites_title`` (+ ``year`` mit ``tags:y…``,
    Queries.pm:746-830). ``id``/``year`` müssen Zahlen sein — der Client
    deserialisiert ``id: Long`` und ``year: Int?``.
    """
    res = _albums(tmp_path, monkeypatch, ["0", "10"])
    item = next(i for i in res["albums_loop"] if i["album"] == "Achtung Baby")
    assert isinstance(item["id"], int), type(item["id"])
    assert isinstance(item["year"], int), type(item["year"])
    assert item["year"] == 1991
    assert item["performance"] == ""
    assert item["favorites_url"] == "db:album.title=Achtung%20Baby"
    assert item["favorites_title"] == "Achtung Baby"
    # Anzeige-/Zeilenfelder der Controller, konsistent zum Albumtitel.
    assert item["text"] == item["title"] == "Achtung Baby"
    assert item["type"] == "outline"
    assert item["hasitems"] == 1
    # Kein ``duration`` auf Album-Items (die frühere Falle: Zahl = Position).
    assert "duration" not in item


def test_album_year_is_never_a_string(tmp_path, monkeypatch):
    """``year`` ohne DB-Wert wird 0 (int), nicht ``""``/None/str."""
    res = _albums(tmp_path, monkeypatch, ["0", "10"])
    for item in res["albums_loop"]:
        assert isinstance(item["year"], int), (item["album"], item["year"])


# ── 3. dir-Zeilen (content_type='dir') tauchen NICHT in der Albumliste auf ─

def test_dir_rows_do_not_leak_into_the_album_list(tmp_path, monkeypatch):
    """Ein Ordner ist kein Album: keine ``dir``-Zeile in ``albums_loop``.

    Der Neuaufbau legt seit ``61ff4d860`` echte ``content_type='dir'``-Zeilen
    an. Die Albumquery liest die ``albums``-Tabelle (Queries.pm:746 — DISTINCT
    über ``albums``/``tracks_albums``), eine reine Ordner-Zeile hat dort keinen
    Link und darf weder als Item noch als ``count`` erscheinen.
    """
    res = _albums(tmp_path, monkeypatch, CRASH_ARGS)
    assert res["count"] == ALBUM_COUNT
    assert not [i for i in res["albums_loop"] if i["album"] in ("U2", "dir")]
    # ``content_type='dir'`` bleibt eine Zeile der Musikordner, nicht des Albums
    assert all(isinstance(i["id"], int) and i["id"] < 100
               for i in res["albums_loop"])


# ── 4. Feed-Layer: menuMode → item_loop, flach → loop_loop ────────────────

def test_browse_feed_names_its_loop_like_perl(tmp_path, monkeypatch):
    """XMLBrowser.pm:582/846 — ``$menuMode ? 'item_loop' : 'loop_loop'``."""
    menu = _browselibrary(tmp_path, monkeypatch,
                          ["items", "0", "10", "menu:1", "mode:albums"])
    assert "item_loop" in menu and "loop_loop" not in menu, sorted(menu)
    assert menu["item_loop"], "menu feed lost its rows"

    flat = _browselibrary(tmp_path, monkeypatch,
                          ["items", "0", "10", "mode:albums"])
    assert "loop_loop" in flat and "item_loop" not in flat, sorted(flat)
    assert flat["loop_loop"], "flat feed lost its rows"
