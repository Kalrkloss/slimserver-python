"""LIB-10 Rest — JSON-RPC ``genres`` mit Perls ``genres_loop`` und echten IDs.

Perl-Referenz (alles read-only): Quellbaum ``/tmp/lms-ref`` und der live
laufende Perl-LMS 9.1.1 auf ``192.168.1.90:9000``.

**Perl-Fundstellen** (``Slim/Control/Queries.pm`` ``genresQuery`` :1781-1985)::

    my $sql  = 'SELECT %s FROM genres ';                       # :1809
    ... search: 'genres.namesearch LIKE ?'                     # :1814-1823
    ... genre_id: 'genres.id IN (…)'                           # :1832-1836
    $sql = sprintf($sql, 'DISTINCT(genres.id), genres.name, genres.namesort');
    $sql .= "ORDER BY genres.namesort $collate"                # :1910-1911
    my $count = COUNT(1) FROM ( $sql ) AS t1    # OHNE LIMIT   # :1919-1925
    my $loopname = 'genres_loop';                              # :1945
    $request->addResultLoop($loopname, $chunkCount, 'id', $id);            # :1971
    $request->addResultLoop($loopname, $chunkCount, 'genre', $name);       # :1972
    $request->addResultLoop($loopname, $chunkCount, 'favorites_url',
        'db:genre.name=' . URI::Escape::uri_escape_utf8( $name ));         # :1973
    $tags =~ /s/ && addResultLoop(…, 'textkey', substr($namesort,0,1));    # :1974

* ``id`` ist die **echte** ``genres``-Zeilen-Id (``DISTINCT(genres.id)``,
  :1910), nicht ein Offset in eine DISTINCT-Textliste.
* ``genre`` ist ``genres.name``; ein leerer/NULL-Name geht als JSON ``null``
  raus, ``favorites_url`` ist dann das blanke ``db:genre.name=`` (Name :1972,
  Escape :1973 — ``uri_escape_utf8(undef)`` = ``''``).
* ``count`` = Gesamtzahl der Treffer des SQL **ohne** LIMIT (:1919-1925),
  nie die Fenstergröße.
* ``tags:`` mit ``s`` ergänzt ``textkey`` (:1974); ``tags:Z`` ergänzt vorher
  ``indexList`` (:1901-1908, ``_createIndexList`` :6875-6915) und ``tags:CC``
  unterdrückt den Loop komplett (:1911, :1943) — in unserem Port (noch) nicht
  umgesetzt, siehe Code-Kommentar in ``lyrion/web/api.py``.
* Fenster/Validität: ``$request->normalize`` (``Slim/Control/Request.pm``
  :1805-1839) — fehlende ``_quantity`` bei gesetztem ``_index`` = alles ab
  Index (:1815); ``_quantity`` 0 oder Index hinter dem letzten Treffer
  ⇒ kein Loop, nur ``count`` (:1816-1822).
* Der Feed-Layer setzt danach ``name``/``type`` auf das Item
  (``Slim/Menu/BrowseLibrary.pm``:1305-1313) und drillt mit
  ``genre_id:$_->{'id'}`` (:1318) — genau deshalb bleiben unsere additiven
  Felder (``name``/``text``/``title``/``type``/``hasitems``/``actions`` +
  ``loop_loop``/``item_loop`` für die alten JSON-Clients) erhalten.

**Live-Kommandos (wörtlich, 2026-09-12 read-only geholt)** — POST an
``http://192.168.1.90:9000/jsonrpc.js`` mit
``{"id":1,"method":"slim.request","params":["",[…]]}``::

    ["genres","0","2"]
    ["genres","0","5"]
    ["genres","2","2"]
    ["genres"]
    ["genres","0","0"]
    ["genres","0"]
    ["genres","762","2"]
    ["genres","0","2","tags:s"]
    ["genres","0","2","tags:Z"]
    ["genres","0","2","tags:CC"]
    ["genres","0","2","search:roc"]
    ["genres","0","2","search:alt rock"]
    ["genres","0","2","genre_id:1728"]
    ["genres","0","2","genre_id:1728,1727"]

**Live-Antworten (wörtlich, gekürzt)**::

    genres 0 2  → {"result":{"count":762,"genres_loop":[
                    {"genre":null,"favorites_url":"db:genre.name=","id":1727},
                    {"genre":null,"favorites_url":"db:genre.name=","id":1728}]}}
    genres 0 5  → count 762, ids 1727,1728,1729,1730,1731 (alle "genre":null)
    genres 2 2  → {"result":{"count":762,"genres_loop":[
                    {"id":1729,"favorites_url":"db:genre.name=","genre":null},
                    {"id":1730,...}]}}
    genres      → {"result":{"count":762}}            # NUR count
    genres 0 0  → {"result":{"count":762}}            # NUR count
    genres 762 2→ {"result":{"count":762}}           # Index hinter dem Ende
    genres 0    → {"count":762, "genres_loop":[762 Items, ids ab 1727]}  # qty fehlt
    tags:s      → Item zusätzlich "textkey":"" (namesort leer)
    tags:Z      → zusätzlich "indexList":[["",45],[".",1],["1",2],["2",3],
                  ["A",46],…,["Р",1]] (31 Einträge) + Loop + count
    tags:CC     → {"result":{"count":762}}            # Loop unterdrückt
    search:roc  → {"result":{"count":91,"genres_loop":[
                    {"favorites_url":"db:genre.name=Alt.%20Rock",
                     "genre":"Alt. Rock","id":1876},
                    {"genre":"Alternative / Indie Rock / Dance / Pop / Rock",
                     "favorites_url":"db:genre.name=Alternative%20%2F%20Indie"
                        "%20Rock%20%2F%20Dance%20%2F%20Pop%20%2F%20Rock",
                     "id":1405}]}}
    search:alt rock → {"result":{"count":1,"genres_loop":[
                    {"id":1876,"genre":"Alt. Rock",
                     "favorites_url":"db:genre.name=Alt.%20Rock"}]}}
    genre_id:1728   → {"result":{"count":1,"genres_loop":[
                    {"genre":null,"favorites_url":"db:genre.name=","id":1728}]}}
    genre_id:1728,1727 → {"result":{"count":2,"genres_loop":[
                    {"favorites_url":"db:genre.name=","genre":null,"id":1727},
                    {"id":1728,…}]}}   # Reihenfolge = namesort, nicht die Eingabe

Anmerkung zur Feldreihenfolge: Perls JSON-Hash-Reihenfolge ist zufällig (die
Live-Antworten zeigen ``id`` mal vorn, mal hinten). Geprüft wird deshalb die
**Quelltext-Reihenfolge** ``id``, ``genre``, ``favorites_url`` (Queries.pm:1971
-1973), die unser Port deterministisch liefert.

Die Live-Daten selbst sind nicht reproduzierbar (Perl-DB vs. unsere); die Items
werden hier **strukturell** nachgebildet: Schlüsselnamen, ID-Herkunft
(``genres.id``), ``null`` bei leerem Namen, ``count`` = Gesamtzahl,
Such-/``genre_id``-Semantik und die Loop-Unterdrückung.
"""

import asyncio
import sqlite3

from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

# ---------------------------------------------------------------------------
# Fixture: ``genres``-Tabelle wie nach einem Rescan (LIB-10,
# ``media/importer.py`` schreibt sie, Spalten id/name/namespell/sortkey).
# Die Ids 1727/1728/1876/1405 und die Namen sind die live gemessenen.
# ---------------------------------------------------------------------------

_GENRES_SQL = """
CREATE TABLE genres (id INTEGER PRIMARY KEY, namespell TEXT, name TEXT,
                     parent INTEGER, sortkey TEXT);
INSERT INTO genres (id, namespell, name, parent, sortkey) VALUES
    (1727, '', '', NULL, NULL),
    (1728, '', '', NULL, ''),
    (1876, 'ALT ROCK', 'Alt. Rock', NULL, 'Alt. Rock'),
    (1405, 'ALTERNATIVE ROCK DANCE POP',
           'Alternative / Indie Rock / Dance / Pop / Rock', NULL,
           'Alternative / Indie Rock / Dance / Pop / Rock');
"""


def _genres_db(tmp_path) -> str:
    """DB mit gefüllter ``genres``-Tabelle (Zustand nach einem Rescan)."""
    db = tmp_path / "lyrion.db"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.executescript(_GENRES_SQL)
    con.commit()
    con.close()
    return str(db)


def _legacy_db(tmp_path) -> str:
    """Alte Bibliothek: keine ``genres``-Tabelle, nur Track-Genre-Text."""
    db = tmp_path / "lyrion.db"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT);
        INSERT INTO tracks (id, title, genre) VALUES
            (1, 'A', 'Rock'), (2, 'B', 'Jazz'), (3, 'C', 'Rock');
        """
    )
    con.commit()
    con.close()
    return str(db)


def _genres(tmp_path, monkeypatch, args, legacy=False):
    """``genres …`` über den JSON-RPC-Browse-Pfad (wie der Dispatcher)."""
    db = _legacy_db(tmp_path) if legacy else _genres_db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    return asyncio.run(JSONRPCAPI()._json_browse("genres", args))


# ---------------------------------------------------------------------------
# genres 0 2 — Loop-Schlüssel, Item-Felder, echte Ids, null bei leerem Namen
# ---------------------------------------------------------------------------

def test_genres_loop_key_and_item_fields_match_perl(tmp_path, monkeypatch):
    """``genres_loop`` (:1945) mit ``id``/``genre``/``favorites_url``
    (:1971-1973); ``id`` = echte ``genres``-Zeilen-Id, leerer Name = ``null``
    (live: ``{"genre":null,"favorites_url":"db:genre.name=","id":1727}``)."""
    res = _genres(tmp_path, monkeypatch, ["0", "2"])

    assert "genres_loop" in res, "Perls Loop-Name fehlt (loop_loop allein ist falsch)"
    loop = res["genres_loop"]
    assert [it["id"] for it in loop] == [1727, 1728]
    assert [it["genre"] for it in loop] == [None, None]
    assert [it["favorites_url"] for it in loop] == ["db:genre.name="] * 2
    # Quelltext-Reihenfolge Queries.pm:1971-1973
    for it in loop:
        assert list(it)[:3] == ["id", "genre", "favorites_url"], list(it)


def test_empty_genre_name_is_null_not_an_invented_placeholder(tmp_path, monkeypatch):
    """Kein ``(None)``/``""``-Platzhalter: Perls ``undef`` → JSON ``null``."""
    res = _genres(tmp_path, monkeypatch, ["0", "2"])
    for it in res["genres_loop"]:
        assert it["genre"] is None
        assert it["favorites_url"] == "db:genre.name="
        assert "(None)" not in it["favorites_url"]
        assert "%28None%29" not in it["favorites_url"]


def test_count_is_the_total_not_the_window(tmp_path, monkeypatch):
    """``COUNT(1) FROM ( $sql )`` ohne LIMIT (Queries.pm:1919-1925)."""
    res = _genres(tmp_path, monkeypatch, ["0", "2"])
    assert res["count"] == 4          # 4 Genres in der Fixture, Fenster = 2
    assert len(res["genres_loop"]) == 2


def test_paging_keeps_real_ids_and_total_count(tmp_path, monkeypatch):
    """``genres 2 2`` → ids 1876/1405, ``count`` bleibt 4 (live: 762)."""
    res = _genres(tmp_path, monkeypatch, ["2", "2"])
    assert [it["id"] for it in res["genres_loop"]] == [1876, 1405]
    assert res["count"] == 4


def test_favorites_url_uses_perls_uri_escape_set(tmp_path, monkeypatch):
    """``uri_escape_utf8`` (Queries.pm:1973): ``/`` → ``%2F``, ``.`` bleibt
    (live: ``db:genre.name=Alternative%20%2F%20Indie%20Rock%20%2F%20Dance…``)."""
    res = _genres(tmp_path, monkeypatch, ["2", "2"])
    escaped = {it["id"]: it["favorites_url"] for it in res["genres_loop"]}
    assert escaped[1876] == "db:genre.name=Alt.%20Rock"
    assert escaped[1405] == ("db:genre.name=Alternative%20%2F%20Indie%20Rock"
                             "%20%2F%20Dance%20%2F%20Pop%20%2F%20Rock")


# ---------------------------------------------------------------------------
# search: / genre_id: — Perls Filter-Semantik
# ---------------------------------------------------------------------------

def test_search_uses_perls_word_prefix_on_namespell(tmp_path, monkeypatch):
    """``genres.namesearch LIKE`` mit ``searchStringSplit`` (:1814-1823,
    Text.pm:209-236) → ``['ROC%', '% ROC%']``.

    Live ``search:roc`` → count 91 und als erste zwei Treffer die Ids 1876
    (``Alt. Rock``) und 1405 (``Alternative … / Rock``) — genau die
    ``% ROC%``-Treffer in namesort-Reihenfolge."""
    res = _genres(tmp_path, monkeypatch, ["0", "2", "search:roc"])
    assert res["count"] == 2
    assert [it["id"] for it in res["genres_loop"]] == [1876, 1405]
    assert res["genres_loop"][0]["genre"] == "Alt. Rock"


def test_search_multiple_words_are_one_and_pattern(tmp_path, monkeypatch):
    """Live ``search:alt rock`` → count 1, nur 1876 (nicht die 91 von ``roc``)."""
    res = _genres(tmp_path, monkeypatch, ["0", "2", "search:alt rock"])
    assert res["count"] == 1
    assert [it["id"] for it in res["genres_loop"]] == [1876]


def test_genre_id_filters_on_real_genres_id(tmp_path, monkeypatch):
    """``genres.id IN (…)`` (:1832-1836); live ``genre_id:1728`` → count 1."""
    res = _genres(tmp_path, monkeypatch, ["0", "2", "genre_id:1728"])
    assert res["count"] == 1
    assert [it["id"] for it in res["genres_loop"]] == [1728]


def test_genre_id_list_stays_in_namesort_order(tmp_path, monkeypatch):
    """Live ``genre_id:1728,1727`` → count 2, Reihenfolge 1727,1728."""
    res = _genres(tmp_path, monkeypatch, ["0", "2", "genre_id:1728,1727"])
    assert res["count"] == 2
    assert [it["id"] for it in res["genres_loop"]] == [1727, 1728]


# ---------------------------------------------------------------------------
# Fenster-Validität (Request.pm:1805-1839) und tags:
# ---------------------------------------------------------------------------

def test_no_index_and_quantity_returns_count_only(tmp_path, monkeypatch):
    """Live ``genres`` → ``{"result":{"count":762}}`` (kein Loop)."""
    res = _genres(tmp_path, monkeypatch, [])
    assert res == {"count": 4}


def test_quantity_zero_and_index_past_end_return_count_only(tmp_path, monkeypatch):
    """Live ``genres 0 0`` und ``genres 762 2`` → nur ``count``."""
    assert _genres(tmp_path, monkeypatch, ["0", "0"]) == {"count": 4}
    res = _genres(tmp_path, monkeypatch, ["762", "2"])
    assert res == {"count": 4}


def test_missing_quantity_after_index_means_all_remaining(tmp_path, monkeypatch):
    """``normalize``: ``$numofitems = $count if !defined $numofitems`` (:1815)
    — live ``genres 0`` liefert den kompletten Loop."""
    res = _genres(tmp_path, monkeypatch, ["0"])
    assert res["count"] == 4
    assert [it["id"] for it in res["genres_loop"]] == [1727, 1728, 1876, 1405]


def test_tags_s_adds_textkey_from_namesort(tmp_path, monkeypatch):
    """``$tags =~ /s/`` → ``textkey`` = ``substr($namesort,0,1)`` (:1974);
    live ``tags:s`` → ``"textkey":""`` bei leerem namesort."""
    res = _genres(tmp_path, monkeypatch, ["0", "4", "tags:s"])
    keys = {it["id"]: it["textkey"] for it in res["genres_loop"]}
    assert keys == {1727: "", 1728: "", 1876: "A", 1405: "A"}


def test_tags_cc_suppresses_the_loop(tmp_path, monkeypatch):
    """``$sql .= … unless $tags eq 'CC'`` (:1911) + ``if ($valid && $tags ne
    'CC')`` (:1943): live ``tags:CC`` → ``{"count":762}`` ohne Loop."""
    assert _genres(tmp_path, monkeypatch, ["0", "2", "tags:CC"]) == {"count": 4}


# ---------------------------------------------------------------------------
# Was Perl nicht liefert, unsere Clients aber lesen (BrowseLibrary.pm:1305-1313
# setzt name/type und drillt mit genre_id:<id>, :1318; die alten JSON-Clients
# lesen text/type/loop_loop/item_loop — siehe _browse_response-Docstring).
# ---------------------------------------------------------------------------

def test_compat_fields_are_additive_and_after_perls_keys(tmp_path, monkeypatch):
    res = _genres(tmp_path, monkeypatch, ["2", "2"])
    item = res["genres_loop"][0]
    # Perls drei Felder zuerst, danach die additiven Kompatibilitätsfelder.
    assert list(item)[:3] == ["id", "genre", "favorites_url"]
    assert item["name"] == "Alt. Rock"            # BrowseLibrary.pm:1306
    assert item["type"] == "outline"              # Controllers: SlimBrowseItemType
    assert item["text"] == "Alt. Rock" and item["title"] == "Alt. Rock"
    assert item["hasitems"] == 1
    assert item["actions"]["go"]["params"]["genre_id"] == 1876   # :1318
    assert item["actions"]["go"]["cmd"] == ["artists"]
    # Loop-Aliase der alten JSON-RPC-Clients bleiben identisch
    assert res["loop_loop"] is res["genres_loop"]
    assert res["item_loop"] is res["genres_loop"]


# ---------------------------------------------------------------------------
# Degradation: alte Bibliothek ohne ``genres``-Tabelle (erst ein Rescan füllt
# sie, LIB-10). Kein Rescan in diesem Test.
# ---------------------------------------------------------------------------

def test_legacy_library_without_genres_table_degrades_to_track_genre_text(
        tmp_path, monkeypatch):
    """Ohne ``genres``-Tabelle bleibt die Genre-Liste gefüllt: der Track-Text
    (DISTINCT) ist die Quelle, ``id`` ist dann der Offset (dokumentierte
    Divergenz, siehe Code-Kommentar) — ``count`` 2 (Rock/Jazz)."""
    res = _genres(tmp_path, monkeypatch, ["0", "2"], legacy=True)
    assert res["count"] == 2
    assert [it["genre"] for it in res["genres_loop"]] == ["Jazz", "Rock"]
    assert [it["id"] for it in res["genres_loop"]] == [0, 1]
    for it in res["genres_loop"]:
        assert it["favorites_url"].startswith("db:genre.name=")
        assert list(it)[:3] == ["id", "genre", "favorites_url"]


def test_legacy_genre_id_still_roundtrips_the_text_list(tmp_path, monkeypatch):
    """Im Degradationsmodus bleibt ``genre_id:<offset>`` auflösbar."""
    res = _genres(tmp_path, monkeypatch, ["0", "2", "genre_id:1"], legacy=True)
    assert res["count"] == 1
    assert res["genres_loop"][0]["genre"] == "Rock"
