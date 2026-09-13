"""LIB-Block: Perl-parity for transcoding rules, search ranking and genre_id.

Perl reference (READ-ONLY): ``/tmp/lms-ref`` = Lyrion Music Server
``public/9.2`` @ ``d1d0a683d8301c04e64be0425e0aec51fc4e8397``.

Cited sources
-------------
Transcoding (LIB-35)
    ``convert.conf`` / ``types.conf`` (shipped verbatim in
    ``src/lyrion/media/``), ``Slim/Player/TranscodingHelper.pm:51-135``
    (``loadConversionTables``), ``:193-225`` (``_getCapabilities``),
    ``:309-500`` (``getConvertCommand2``), ``:519-673``
    (``tokenizeConvertCommand2``), ``Slim/Music/Info.pm:92-159``
    (``loadTypesConfig``).
Search ranking (LIB-16/17)
    ``Slim/Control/Queries.pm:3472-3659`` (``searchQuery``),
    ``Slim/Plugin/FullTextSearch/Plugin.pm:345-481`` (``parseSearchTerm``),
    ``:486-503`` (``_getWeight`` = the relevance formula), ``:31-47``
    (what feeds w10/w5/w3/w1 per track),
    ``Slim/Utils/Text.pm:209-236`` (``searchStringSplit``),
    ``:72-96`` (``matchCase``), ``:45-60`` (``ignorePunct``),
    ``Slim/Utils/Prefs.pm:176`` (``searchSubString`` default 0).
genre_id (LIB-10)
    ``Slim/Schema/Genre.pm:20-33`` (columns) and ``:91-132`` (``add``:
    ``namesort``/``namesearch`` + ``REPLACE INTO genre_track``).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from lyrion.control import cli_commands
from lyrion.control import queries as q
from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.database.schema import (Album, Base, Genre, tracks_albums,
                                    tracks_genres)
from lyrion.media import transcoding
from lyrion.media.importer import MusicImporter


def _dispatch(cmd: str, args: list[str]) -> list[str]:
    handler = CLIHandler()
    ctx = CLIContext(player_id=None)
    ctx.command = cmd
    return asyncio.run(handler.dispatch(ctx, (cmd, args)))


# ===========================================================================
# LIB-35 — transcoding rules
# ===========================================================================

def test_convert_conf_all_rules_match_perl_table():
    """80 profiles exactly like ``loadConversionTables`` (Perl parse of the
    shipped ``convert.conf``; TranscodingHelper.pm:93-130)."""
    t = transcoding.get_conversion_tables()
    assert len(t.command_table) == 80
    # spot checks against the raw file (convert.conf line refs in the comment)
    assert t.command_table["mp4-mp3-*-*"] == (
        "[faad] -q -w -f 1 $START$ $END$ $FILE$ | "
        "[lame] --silent -q $QUALITY$ $BITRATE$ - -")            # convert.conf:101-103
    assert t.command_table["mp3-mp3-*-*"] == "-"                 # convert.conf:169-170
    assert t.command_table["flc-pcm-*-*"] == (
        "[flac] -dcs --force-raw-format --endian=little --sign=signed "
        "$START$ $END$ -- $FILE$")                                # convert.conf:185-187
    assert t.command_table["dsf-dsf-*-*"] == "-"                 # convert.conf:400-402
    assert t.command_table["wvp-pcm-*-*"].startswith("[wvunpack]")  # convert.conf:261-263


def test_capabilities_come_from_the_comment_line():
    """``_getCapabilities`` (TranscodingHelper.pm:193-225): the ``# FBTU:{}``
    comment right above the command line is the capability declaration."""
    t = transcoding.get_conversion_tables()
    caps = t.capabilities["mp4-mp3-*-*"]
    assert set("FBTUE") <= set(caps)                 # convert.conf:102
    assert caps["T"] == "START=-j %s"
    assert caps["U"] == "END=-e %u"
    assert caps["B"] == "BITRATE=--abr %B"
    # no comment → the default {I,F} (TranscodingHelper.pm:112) — Perl adds no
    # 'E' extension hash for a defaulted profile
    default = t.capabilities["mp3-mp3-*-*"]
    assert set(default) == {"I", "F"}
    # flc-pcm carries I, F, T, U (convert.conf:186)
    flc_pcm = t.capabilities["flc-pcm-*-*"]
    assert {"I", "F", "T", "U"} <= set(flc_pcm)
    # wma-pcm has no 'I' — only F/R (convert.conf:229-230)
    assert "I" not in t.capabilities["wma-pcm-*-*"]


def test_types_conf_matches_perl_hashes():
    """``loadTypesConfig`` (Info.pm:92-159) builds four hashes."""
    tc = transcoding.get_types_config()
    assert tc.suffixes["flac"] == "flc"          # types.conf:24
    assert tc.suffixes["m4a"] == "mp4"           # types.conf:40
    assert tc.mime_types["audio/mpeg"] == "mp3"  # types.conf:42
    assert tc.mime_types["audio/x-flac"] == "flc"  # types.conf:24
    assert tc.slim_types["flc"] == "audio"       # types.conf:24
    assert tc.types["mp3"] == "audio/mpeg"       # first mime column
    assert tc.types["flc"] == "audio/x-flac"
    assert len(tc.types) == 62
    assert len(tc.suffixes) == 67
    assert len(tc.mime_types) == 88
    assert len(tc.slim_types) == 41


def test_convert_command_prefers_native_passthrough():
    """``getConvertCommand2`` (TranscodingHelper.pm:371-386): ``<type>-<type>-*-*``
    is tried first; a ``-`` command is a passthrough."""
    t = transcoding.get_conversion_tables()
    tr = transcoding.get_convert_command(
        "mp3", ["mp3"], player="squeezebox", clientid="00:04:20:00:00:01",
        command_table=t.command_table, capabilities=t.capabilities,
        find_bin=lambda n: "/usr/bin/" + n)
    assert tr is not None
    assert tr["profile"] == "mp3-mp3-*-*"
    assert tr["command"] == "-"
    assert tr["streamformat"] == "mp3"


def test_convert_command_transcodes_mp4_to_mp3():
    # mp4 is a file protocol: the rule's stream mode is F (convert.conf:102)
    t = transcoding.get_conversion_tables()
    tr = transcoding.get_convert_command(
        "mp4", ["mp3"], player="squeezebox", clientid="00:04:20:00:00:01",
        stream_modes=["F", "R"],
        command_table=t.command_table, capabilities=t.capabilities,
        find_bin=lambda n: "/usr/bin/" + n)
    assert tr["profile"] == "mp4-mp3-*-*"
    assert "[faad]" in tr["command"] and "[lame]" in tr["command"]


def test_convert_command_rejects_missing_stream_mode_and_capability():
    """A profile must satisfy one of ``@$streamModes`` (:403-414) and every
    mandatory capability in ``@$need`` (:417-426)."""
    t = transcoding.get_conversion_tables()
    kw = dict(command_table=t.command_table, capabilities=t.capabilities,
              find_bin=lambda n: "/usr/bin/" + n, player="x", clientid="y")
    # wma-pcm caps are F/R only (convert.conf:229-231) → no 'I' stream mode
    assert transcoding.get_convert_command("wma", ["pcm"],
                                           stream_modes=["I"], **kw) is None
    # mp4-mp3 has no 'D' (downsample) capability (convert.conf:102)
    assert transcoding.get_convert_command("mp4", ["mp3"], need=["D"],
                                           stream_modes=["F"], **kw) is None
    # but the same rule works with no extra need
    assert transcoding.get_convert_command("mp4", ["mp3"],
                                           stream_modes=["F"], **kw) is not None


def test_tokenize_conversion_command_substitutes_perl_placeholders():
    """``tokenizeConvertCommand2`` (TranscodingHelper.pm:519-673):
    ``[bin]`` → absolute path, ``$FILE$`` → quoted path, ``%q/$QUALITY$`` →
    the quality, ``$START$``/``$END$`` from the capability args."""
    caps = {"F": "noArgs", "B": "BITRATE=--abr %B", "T": "START=-j %s",
            "U": "END=-e %u", "E": {}}
    tr = {
        "command": "[faad] -q -w -f 1 $START$ $END$ $FILE$ | "
                   "[lame] --silent -q $QUALITY$ $BITRATE$ - -",
        "profile": "mp4-mp3-*-*", "streamMode": "F",
        "usedCapabilities": ["F", "B"],
        "extensions": {}, "channels": 2, "sampleSize": 16,
        "sampleRate": 44100, "outputChannels": 2,
        "clientid": "00:04:20:00:00:01", "player": "squeezebox",
        "name": "Living Room", "groupid": 0,
    }
    cmd = transcoding.tokenize_convert_command(
        tr, "/music/a b.mp4", quality="5",
        binaries={"faad": "/usr/bin/faad", "lame": "/usr/bin/lame"},
        capabilities=caps)
    assert "/usr/bin/faad" in cmd and "/usr/bin/lame" in cmd
    assert '"/music/a b.mp4"' in cmd
    assert "--silent -q 5" in cmd
    assert "$START$" not in cmd and "$FILE$" not in cmd


def test_tokenize_passthrough_rule_stays_a_dash():
    tr = {"command": "-", "profile": "mp3-mp3-*-*", "streamMode": "I",
          "usedCapabilities": [], "extensions": {}, "channels": 2,
          "sampleSize": 16, "sampleRate": 44100, "outputChannels": 2,
          "clientid": "x", "player": "x", "name": "x", "groupid": 0}
    assert transcoding.tokenize_convert_command(tr, "/m/a.mp3") == "-"


# ===========================================================================
# LIB-16/17 — search tokenizing + relevance ordering
# ===========================================================================

def test_search_string_split_matches_perl_word_prefix():
    """``searchStringSplit`` (Text.pm:209-236) with the default
    ``searchSubString=0`` (Prefs.pm:176): a word-prefix, NOT a substring."""
    assert q.search_string_split("night") == ["NIGHT%", "% NIGHT%"]
    assert q.search_string_split("the night", substring=True) == ["%THE NIGHT%"]
    # ignoreCaseArticles upper-cases and folds punctuation (Text.pm:134-176)
    assert q.search_string_split("Rock & Roll") == ["ROCK ROLL%", "% ROCK ROLL%"]


def test_search_tokens_split_terms_and_keep_negation():
    """``parseSearchTerm`` (Plugin.pm:345-457): lower-case, split on
    punctuation/whitespace, a leading '-' is an exclusion."""
    assert q.search_tokens("Love Me") == ["love", "me"]
    assert q.search_tokens("paul simon -garfunkel") == [
        "paul", "simon", "-garfunkel"]
    assert q.search_tokens("   ") == []


def test_fulltext_weight_matches_getweight_formula():
    """``_getWeight`` (Plugin.pm:486-503): +10000 if the token hits the w10
    column (title), else +5 per w5 hit, +3 per w3 hit, +1 per w1 hit."""
    assert q.fulltext_weight(["love"], w10="Endless Love") == 10_000
    assert q.fulltext_weight(["love"], w10="Other", w5=["Love Album"]) == 5
    assert q.fulltext_weight(["love"], w10="Other",
                             w3=["Love", "love"]) == 6
    assert q.fulltext_weight(["love", "love"], w10="Love") == 20_000
    assert q.fulltext_weight(["love"], w10="Other", w1=["Love"]) == 1


def _capture_query_db(monkeypatch, rows):
    """Patch ``cli_commands._query_db`` with a stub returning ``rows``."""
    seen: list[tuple[str, tuple]] = []

    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        seen.append((sql, params))
        if "COUNT(" in sql.upper():
            return [{"n": len(rows)}]
        return list(rows)

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    return seen


def test_songs_search_orders_by_relevance_not_by_title(monkeypatch):
    """LIB-16 evidence: Perl ``["songs",0,3,"search:love"]`` returns a
    relevance order (Queries.pm:3597 ``ORDER BY quickSearch.fulltextweight
    DESC``).  A title hit (w10) must beat an album hit (w5) even when the
    album hit's title sorts first."""
    rows = [
        # title hit would sort last alphabetically but must rank first
        {"id": 2, "title": "Love Song", "genre": "", "year": 0,
         "tracknum": 1, "duration": 10, "album_title": "Zzz", "artist_name": "A"},
        {"id": 1, "title": "Sunshine", "genre": "", "year": 0,
         "tracknum": 2, "duration": 10, "album_title": "Love Album",
         "artist_name": "B"},
    ]
    seen = _capture_query_db(monkeypatch, rows)
    line = _dispatch("songs", ["0", "5", "search:love"])[0]
    assert line.index("id%3A2") < line.index("id%3A1")
    # the SQL is tokenized (one LIKE per token / per searched column),
    # not a single '%term%' on the title (the LIB-16 bug).
    sql = " ".join(s for s, _ in seen)
    assert "LIKE" in sql


def test_search_command_ranks_tracks_by_title_weight(monkeypatch):
    """``searchQuery`` (Queries.pm:3649-3657) runs contributor, album, genre
    and track and sums the counts.  The track loop is weight-ordered."""
    rows = [
        {"id": 11, "title": "Ordinary", "album_title": "Love", "artist_name": "A"},
        {"id": 12, "title": "Love", "album_title": "X", "artist_name": "B"},
    ]

    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        if "COUNT(" in sql.upper():
            return [{"n": 1}]
        return list(rows)

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    line = _dispatch("search", ["0", "5", "term:love"])[0]
    assert "tracks_count%3A" in line
    assert line.index("track_id%3A12") < line.index("track_id%3A11")


# ===========================================================================
# LIB-10 — genre_id from the genres table
# ===========================================================================

def test_importer_populates_genres_and_genre_track_links():
    """Perl ``Slim::Schema::Genre::add`` (Genre.pm:91-132): split the tag
    (``splitList`` default ';', Prefs.pm:178), upsert one row per genre and
    ``REPLACE INTO genre_track``."""
    asyncio.run(_run_genre_import())


async def _run_genre_import():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    importer = MusicImporter()
    info = SimpleNamespace(title="T", artist="A", album="Al", year=2000,
                           duration=1000, genre="Rock;Pop")
    async with Session() as session:
        await importer._import_batch(session, [(Path("/m/a.mp3"), info)])
        await session.commit()
        genres = (await session.execute(select(Genre))).scalars().all()
        names = sorted(g.name for g in genres)
        links = (await session.execute(select(tracks_genres))).all()
    await engine.dispose()
    assert names == ["Pop", "Rock"], names
    assert len(links) == 2
    # namespell is the unique, case-folded key (Genre.pm:31 namesearch)
    assert sorted(g.namespell for g in genres) == ["POP", "ROCK"]


def test_genres_cli_reports_real_genre_ids(monkeypatch):
    """``genresQuery`` (Queries.pm:1910) selects ``DISTINCT(genres.id),
    genres.name, genres.namesort`` — real ids, not the offset into a
    DISTINCT track-text list (the LIB-10 bug)."""
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        if "FROM genres" not in sql:
            return []
        if "COUNT(" in sql.upper():
            return [{"n": 762}]
        return [{"id": 497, "name": "", "namesort": ""},
                {"id": 498, "name": "Rock", "namesort": "ROCK"}]

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    line = _dispatch("genres", ["0", "2"])[0]
    assert "id%3A497" in line
    assert "id%3A498" in line
    assert "count%3A762" in line


def test_genres_search_uses_perls_word_prefix(monkeypatch):
    """``genresQuery`` has no FTS path (Plugin.pm:3516) and searches
    ``genres.namesearch LIKE`` with ``searchStringSplit`` (Queries.pm:1814-1823)."""
    seen: list[tuple] = []

    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        seen.append((sql, params))
        if "COUNT(" in sql.upper():
            return [{"n": 0}]
        return []

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    _dispatch("genres", ["0", "5", "search:roc"])
    sql, params = seen[0]
    assert "ROCK" in params or "ROC%" in params
    assert any(p in ("ROC%", "% ROC%") for p in params)


def test_genres_search_patterns_are_joined_with_or(monkeypatch):
    """Perl verknüpft die Muster EINES Suchbegriffs mit OR (Queries.pm:1817).

    Regression: die Muster von ``searchStringSplit`` wurden mit AND verknüpft,
    dadurch lieferte jede Mehrwortsuche (und in der Praxis auch
    ``genres 0 5 search:roc``) immer 0 Treffer.
    """
    seen = _capture_query_db(monkeypatch, [
        {"id": 1727, "name": "Rock", "namesort": "rock"},
    ])
    _dispatch("genres", ["0", "5", "search:alt rock"])
    sql, params = seen[-1]
    # Ein Suchbegriff mit zwei Mustern → EINE OR-verknüpfte Bedingung
    assert " OR " in sql.upper()
    assert sql.upper().count(" LIKE ") >= 2
    assert len([p for p in (params or []) if isinstance(p, str) and "%" in p]) >= 2


def test_import_adopts_a_legacy_album_row_without_albumartist_sort():
    """Regression (Live-Scan 2026-09-13): Alben aus der Zeit vor der Spalte
    ``albumartist_sort`` haben dort NULL; die Migration füllt nicht nach. Der
    Import suchte über (titlesort, artist_sort), legte eine zweite Zeile an und
    lief in ``UNIQUE constraint failed: albums.titlesort, albums.year`` — die
    gesamte Import-Transaktion rollte zurück, der Scan brach ab.

    Erwartung: die Altzeile wird adoptiert, ``albumartist_sort`` nachgetragen,
    es entsteht KEINE zweite Zeile.
    """
    asyncio.run(_run_legacy_album_import())


async def _run_legacy_album_import():
    from sqlalchemy import text as sqltext
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # den alten UNIQUE-Index nachstellen (er stammt aus einer früheren
        # Schema-Version und existiert in Bestands-DBs als autoindex)
        await conn.execute(sqltext(
            "CREATE UNIQUE INDEX uq_legacy_album ON albums (titlesort, year)"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        # Zeile wie vor der Spalte: albumartist_sort bleibt leer
        legacy = Album(title="Al", titlesort="al", year=2000,
                       albumartist_sort=None)
        session.add(legacy)
        await session.commit()
    importer = MusicImporter()
    info = SimpleNamespace(title="Neu", artist="A", album="Al", year=2000,
                           duration=1000, genre="Rock")
    async with Session() as session:
        await importer._import_batch(session, [(Path("/m/neu.mp3"), info)])
        await session.commit()
        rows = (await session.execute(
            select(Album.id, Album.albumartist_sort))).all()
        links = (await session.execute(select(tracks_albums))).all()
    await engine.dispose()
    assert len(rows) == 1, rows                      # adoptiert, nicht neu
    assert rows[0][1] == "a", rows                   # nachgetragen
    assert len(links) == 1 and links[0][1] == 1, links
