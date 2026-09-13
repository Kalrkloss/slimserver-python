"""JSON-RPC ``info total <entity> ?`` — Perl ``infoTotalQuery`` parity.

Der JSON-Pfad beantwortete ``slim.request`` ``["info","total","songs","?"]``
mit der leeren Browse-Standardantwort (``count``/``offset``/``loop_loop``/
``item_loop``) statt mit der Zahl, obwohl der CLI-Pfad korrekt war
(``info total songs 80441``). Perls JSON-RPC liefert ``{"result":{"_songs":…}}``.

Live gegengeprüft (nur lesend, Perl-LMS 9.x, 192.168.1.90:9000, 2026-09-13)::

    curl -s http://192.168.1.90:9000/jsonrpc.js -H 'Content-Type: application/json' \\
      -d '{"method":"slim.request","params":["",["info","total","songs","?"]]}'
    {"params":["",["info","total","songs","?"]],"method":"slim.request",
     "result":{"_songs":80218}}

    songs     -> {"result":{"_songs":80218}}
    albums    -> {"result":{"_albums":7189}}
    artists   -> {"result":{"_artists":11170}}
    genres    -> {"result":{"_genres":762}}
    duration  -> {"result":{"_duration":22851708.851}}

und die nicht-registrierten Formen (Perl schließt den Socket ohne Body —
"Empty reply from server")::

    ["info","total","songs"]      (ohne ?)  -> kein Result
    ["info","total","?"]                    -> kein Result
    ["info","total"]                        -> kein Result
    ["info","total","songs","?","extra"]    -> kein Result
    ["info","total","contributors","?"]     -> kein Result
    ["info","total","years","?"]            -> kein Result
    ["info","total","foo","?"]              -> kein Result

Perl-Fundstellen:

* Dispatch — nur diese fünf Entitäten, jeweils ``['info','total','<e>','?']``
  mit ``[0,1,0]`` (kein Client, kein Tag): ``Slim/Control/Request.pm:501-505``.
* Handler ``infoTotalQuery`` — ``Slim/Control/Queries.pm:2019-2055``;
  ``isNotQuery([['info'],['total'],['genres','artists','albums','songs',
  'duration']])`` (:2023) → alles andere ist Bad Dispatch. Der Ergebnisschlüssel
  ist IMMER ``_<entity>`` (:2039-2051), der WERT kommt aus einem anderen
  ``totals``-Schlüssel: albums→album, artists→contributor, genres→genre,
  songs→track (:2038-2049, ``Slim/Schema.pm:3305-3320``); duration ist
  ``Slim::Schema->totalTime`` (:2051, ``Schema.pm:2173-2199``).
* Serialisierung: ``Slim/Web/JSONRPC.pm:566`` reicht ``$request->{'_results'}``
  unverändert an ``generateJSONResponse`` (:373-396) — ``_songs`` bleibt
  ``_songs``.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lyrion.database.schema import Base
from lyrion.web.api import JSONRPCAPI

#: Live-Antworten des Perl-LMS (read-only, 2026-09-13) — die Form, die der
#: JSON-Pfad liefern muss: genau EIN Schlüssel ``_<entity>``, Zahl als Wert.
PERL_LIVE_INFO_TOTAL = {
    "songs": {"_songs": 80218},          # Queries.pm:2047-2048
    "albums": {"_albums": 7189},         # Queries.pm:2038-2039
    "artists": {"_artists": 11170},      # Queries.pm:2041-2042
    "genres": {"_genres": 762},          # Queries.pm:2044-2045
    "duration": {"_duration": 22851708.851},  # Queries.pm:2050-2051
}

#: Die fünf Entitäten der Dispatch-Tabelle (Request.pm:501-505).
ENTITIES = ("albums", "artists", "genres", "songs", "duration")


@pytest.fixture
def lib_db(tmp_path, monkeypatch):
    """Temporäre Bibliotheks-DB mit dem ECHTEN Schema (kein Prod-Zugriff)."""
    db_path = tmp_path / "lyrion.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    asyncio.run(engine.dispose())

    monkeypatch.setattr("lyrion.web.api._library_db_path", lambda: str(db_path))
    monkeypatch.setattr("lyrion.control.cli_commands._library_db_path",
                        lambda: str(db_path))

    # Core-Inserts über die ORM-Tabellen: die Python-Defaults der NOT-NULL-
    # Spalten (playcount, weight, …) werden dabei gesetzt.
    from sqlalchemy import create_engine, insert

    from lyrion.database.schema import (Album, Contributor, Track,
                                        tracks_contributors)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(insert(Track), [
            {"id": 1, "titlesort": "creep", "title": "Creep",
             "url": "file:///m/1.flac", "duration": 100.5, "genre": "Rock"},
            {"id": 2, "titlesort": "airbag", "title": "Airbag",
             "url": "file:///m/2.flac", "duration": 200.0, "genre": "Jazz"},
            {"id": 3, "titlesort": "lucky", "title": "Lucky",
             "url": "file:///m/3.flac", "duration": 0.0, "genre": ""},
        ])
        conn.execute(insert(Album), [
            {"id": 1, "titlesort": "pablo honey", "title": "Pablo Honey"},
            {"id": 2, "titlesort": "ok computer", "title": "OK Computer"},
        ])
        conn.execute(insert(Contributor), [
            {"id": 1, "namespell": "radiohead", "name": "Radiohead"},
            {"id": 2, "namespell": "nigel", "name": "Nigel Godrich"},
            {"id": 3, "namespell": "thom", "name": "Thom Yorke"},
        ])
        # role 1 = artist (zählt), role 2 = Komponist (zählt nicht)
        conn.execute(insert(tracks_contributors), [
            {"track": 1, "contributor": 1, "role": 1},
            {"track": 2, "contributor": 2, "role": 1},
            {"track": 3, "contributor": 3, "role": 2},
        ])
    engine.dispose()
    return db_path


def _request(command: list[str], player: str = "") -> dict:
    return asyncio.run(JSONRPCAPI()._slim_request(player, command))  # type: ignore[return-value]


# ── Die fünf Entitäten (Request.pm:501-505) ───────────────────────────────


def test_info_total_songs_returns_the_songs_key(lib_db):
    """``_songs`` aus ``COUNT(*) FROM tracks`` (Queries.pm:2047-2048)."""
    assert _request(["info", "total", "songs", "?"]) == {"_songs": 3}


def test_info_total_albums_returns_the_albums_key(lib_db):
    """``_albums`` aus ``totals->{album}`` (Queries.pm:2038-2039, Schema.pm:3314)."""
    assert _request(["info", "total", "albums", "?"]) == {"_albums": 2}


def test_info_total_artists_returns_the_artists_key(lib_db):
    """``_artists`` aus ``totals->{contributor}`` — nur role 1
    (Queries.pm:2041-2042, Schema.pm:3315)."""
    assert _request(["info", "total", "artists", "?"]) == {"_artists": 2}


def test_info_total_genres_returns_the_genres_key(lib_db):
    """``_genres`` aus ``totals->{genre}`` (Queries.pm:2044-2045)."""
    assert _request(["info", "total", "genres", "?"]) == {"_genres": 2}


def test_info_total_duration_returns_the_duration_key(lib_db):
    """``_duration`` aus ``totalTime`` — eine ZAHL (Queries.pm:2050-2051)."""
    assert _request(["info", "total", "duration", "?"]) == {"_duration": 300.5}


def test_every_entity_answers_with_exactly_its_underscore_key(lib_db):
    """Formtreue: genau ein Schlüssel ``_<entity>``, keine Browse-Felder."""
    for entity in ENTITIES:
        res = _request(["info", "total", entity, "?"])
        assert list(res.keys()) == [f"_{entity}"], entity
        assert isinstance(res[f"_{entity}"], (int, float)), entity
        # Die alte, falsche Antwort der Browse-Standardform:
        assert "loop_loop" not in res and "item_loop" not in res, entity


# ── Nicht-registrierte Formen: Perl liefert gar kein Result ───────────────


def test_info_total_without_question_mark_is_not_dispatchable(lib_db):
    """Ohne ``?`` greift kein Dispatch-Eintrag (Request.pm:501-505)."""
    assert _request(["info", "total", "songs"]) == {}


def test_info_total_without_entity_is_not_dispatchable(lib_db):
    """``info total ?`` — Live-Perl: leerer Reply, kein Dispatch."""
    assert _request(["info", "total", "?"]) == {}
    assert _request(["info", "total"]) == {}


def test_info_total_with_unknown_entity_is_not_dispatchable(lib_db):
    """``contributors``/``years``/``foo`` stehen NICHT in der Dispatch-Tabelle
    (Request.pm:501-505, ``isNotQuery`` Queries.pm:2023)."""
    for entity in ("contributors", "years", "foo", "artist", "album"):
        assert _request(["info", "total", entity, "?"]) == {}, entity


def test_info_total_with_extra_arguments_is_not_dispatchable(lib_db):
    """Ein 5. Argument verlässt die registrierte Form (Request.pm:501-505)."""
    assert _request(["info", "total", "songs", "?", "extra"]) == {}


def test_info_total_is_case_sensitive_like_perl(lib_db):
    """Perls Dispatch vergleicht wörtlich — ``SONGS`` matcht nicht."""
    assert _request(["info", "total", "SONGS", "?"]) == {}
