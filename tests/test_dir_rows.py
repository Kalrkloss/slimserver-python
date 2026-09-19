"""D4 — Ordner-IDs als echte ``dir``-Zeilen in ``tracks`` (Perl-Verhalten).

Perl kennt kein ``crc32(path)``-Konstrukt für Ordner: jedes Verzeichnis, das
der Server anfasst, liegt als Zeile in ``tracks`` mit
``content_type = 'dir'``, und die ``id``, die ein Client für einen Ordner
bekommt, ist genau diese ``tracks.id``.

Perl-Belegstellen (read-only ``/tmp/lms-ref``, Live-9.1.1-Proben read-only auf
192.168.1.90:9000):

* ``Slim/Music/Info.pm:1482-1484`` — ``-d $filepath`` → ``$type = 'dir'``
* ``Slim/Schema.pm:764-856`` ``objectForUrl`` — ``create => 1`` →
  ``updateOrCreate``; ``:1774`` lässt ein Verzeichnis den einfachen
  ``_createTrack``-Pfad nehmen (kein Album/Contributor-Link,
  ``!$columnValueHash{'audio'}``)
* ``Slim/Control/Queries.pm:2263-2268`` — der Browse legt die Zeile jedes
  angezeigten Kindes an; ``:2249-2257`` prüft im ersten Durchlauf nur die
  Existenz ("don't create the dir objects in the first pass")
* ``:2429-2430`` — ``id``/``filename`` des ``folder_loop``-Items;
  ``:2494-2496`` — ``ct``
* ``:2311-2316`` — ``folder_id`` wird über ``Slim::Schema->find('Track', $id)``
  aufgelöst (``Slim/Utils/Misc.pm:1067-1074``)
* ``Slim/Utils/Scanner/Local/AIO.pm:62-67``/``:135-142`` — der Scan vermerkt
  jedes Verzeichnis (``filesize`` 0)
* ``Slim/Utils/Scanner/Local.pm:186`` — Rescan-Mengen mit
  ``content_type != 'dir'``; ``Slim/Schema.pm:2346-2357`` ``wipeAllData``
  löscht alle Zeilen
* ``Slim/Control/Queries.pm:4843`` — die Songs-Query filtert
  ``tracks.audio = 1 AND tracks.content_type NOT IN ("cpl","src","ssp","dir")``
* ``Slim/Schema.pm:2186-2190`` — ``totalTime`` über ``tracks.audio = 1``
* Live-Proben: ``musicfolder 0 5 tags:stuo`` → ``{"id":204572,
  "filename":"Accept","type":"folder","ct":"dir"}``;
  ``folder_id:99999999`` und ``folder_id:1`` → ``{"count":0}``

Die Tests sind hermetic: jede Bibliotheks-DB liegt unter ``tmp_path``; die
``conftest``-Fixture ``_no_library_db_writes`` verhindert Schreibzugriffe auf
die echte DB, wo ein Test sie nicht ausdrücklich umlenkt.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lyrion.database import migrate_dir_rows
from lyrion.database.schema import Base
from lyrion.media import dir_rows, folders
from lyrion.media.importer import ImportConfig, MusicImporter
from lyrion.web import api as api_mod
from lyrion.web import menus as menus_mod
from lyrion.web.api import JSONRPCAPI

ROOT = "/srv/music"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(path: Path, schema: str, rows=()) -> str:
    con = sqlite3.connect(path)
    con.executescript(schema)
    if rows:
        con.executemany(
            "INSERT INTO tracks (id, url, title, titlesort, audio, video, "
            "remote, disabled, compilation, artflow_flag, duration, playcount)"
            " VALUES (?, ?, ?, '', 1, 0, 0, 0, 0, 0, 0, 0)",
            [(r[0], r[1], r[2] if len(r) > 2 else "") for r in rows])
    con.commit()
    con.close()
    return str(path)


def _browse(db: str, monkeypatch, root: str, args: list[str]) -> dict:
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    monkeypatch.setattr(api_mod, "_bmf_musicdir_pref", lambda: root)

    async def run():
        return await JSONRPCAPI()._json_browselibrary("browselibrary",
                                                      [str(a) for a in args])

    return asyncio.run(run())


def _folders(db: str, monkeypatch, root: str, args: list[str]) -> list[dict]:
    res = _browse(db, monkeypatch, root, args)
    return res.get("item_loop") or []


def _dir_rows(db: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT id, url, content_type, title, titlesort, audio, filesize,"
            " modtime FROM tracks WHERE content_type = 'dir' ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _all_tracks(db: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT id, url, content_type FROM tracks"
                           " ORDER BY id").fetchall()
    finally:
        con.close()


TREE = [
    (1, "file:///srv/music/Metal/Accept/01.mp3"),
    (2, "file:///srv/music/Metal/Accept/02.mp3"),
    (3, "file:///srv/music/Metal/Iron%20Maiden/03.flac"),
    (4, "file:///srv/music/Ambient/Boards%20of%20Canada/04.flac"),
]


# ---------------------------------------------------------------------------
# 1. Die Zeile selbst — Perl's Werte
# ---------------------------------------------------------------------------

def test_ensure_dir_row_stores_the_perl_row(tmp_path, tracks_schema_sql):
    """``content_type='dir'``, ``audio`` 0, ``filesize`` 0, title leer.

    ``Slim/Schema.pm:1774`` (dir → einfacher Create-Pfad, ``audio`` 0),
    ``Slim/Utils/Scanner/Local/AIO.pm:67`` ("size, 0 for dirs"),
    ``Slim/Control/Queries.pm:2429-2430`` (der Name kommt aus ``fileName``,
    nicht aus ``tracks.title``).
    """
    folder = tmp_path / "Musik" / "Accept"
    folder.mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql)

    rid = dir_rows.ensure_dir_row(folder, db_path=db)
    assert isinstance(rid, int) and rid > 0
    row = _dir_rows(db)[0]
    assert row[0] == rid
    assert row[1] == folders.file_url_from_path(folder)
    assert row[2] == "dir"
    assert row[3] == "" and row[4] == ""
    assert row[5] == 0                     # audio
    assert row[6] == 0                     # filesize
    assert row[7] == int(folder.stat().st_mtime)
    # no album/contributor links, no tracks_persistent row (Schema.pm:1563)
    assert _all_tracks(db) == [(rid, row[1], "dir")]


def test_ensure_dir_row_is_idempotent_and_refreshes_modtime(tmp_path,
                                                            tracks_schema_sql):
    """Zweiter Aufruf liefert dieselbe id, kein zweiter Datensatz.

    ``url`` ist UNIQUE (Perl: ``_retrieveTrack`` vor ``updateOrCreate``),
    ``modtime`` wird wie ``$topLevelObj->timestamp($fsMTime)``
    (``Slim/Utils/Misc.pm:1131-1136``) nachgezogen.
    """
    folder = tmp_path / "Musik" / "Accept"
    folder.mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql)

    first = dir_rows.ensure_dir_row(folder, db_path=db)
    assert dir_rows.ensure_dir_row(folder, db_path=db) == first
    assert len(_dir_rows(db)) == 1

    os.utime(folder, (1_600_000_000, 1_600_000_000))
    assert dir_rows.ensure_dir_row(folder, db_path=db) == first
    assert len(_dir_rows(db)) == 1
    assert _dir_rows(db)[0][7] == 1_600_000_000


def test_ensure_dir_row_without_library_db_is_none(tmp_path):
    """Ohne DB (oder ohne ``content_type``) keine Zeile, kein Fehler."""
    folder = tmp_path / "Musik"
    folder.mkdir()
    assert dir_rows.ensure_dir_row(folder, db_path=str(tmp_path / "nope.db")) \
        is None
    lean = _db(tmp_path / "lean.db", "CREATE TABLE tracks (id INTEGER"
               " PRIMARY KEY, url TEXT);")
    assert dir_rows.ensure_dir_row(folder, db_path=lean) is None


def test_dir_url_uses_the_importer_encoding(tmp_path, tracks_schema_sql):
    """gvfs/SMB-Pfade: ``smb-share%3Aserver%3Dx%2Cshare%3Dy`` wie ``tracks.url``.

    Perl ``fileURLFromPath`` (``Slim/Utils/Misc.pm:307-351``), das der
    Importer über ``Path.as_uri()`` in identischer Kodierung schreibt.
    """
    gvfs = ("/run/user/1000/gvfs/smb-share:server=media.local,share=media/"
            "Musik/AC+DC")
    folder = Path(gvfs)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql)
    rid = dir_rows.ensure_dir_row(gvfs, db_path=db)
    assert rid is not None
    url = _dir_rows(db)[0][1]
    assert url == ("file:///run/user/1000/gvfs/"
                   "smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/"
                   "AC%2BDC")
    assert url == Path(gvfs).as_uri()
    # Der Pfad kommt unverändert zurück — der Drill vergleicht dekodiert.
    assert dir_rows.dir_path_by_id(rid, db_path=db) == str(folder)
    # …und dieselbe Zeile wird auch über die URL wiedergefunden.
    assert dir_rows.ensure_dir_row(url, db_path=db) == rid


def test_dir_row_lookup_is_case_folded_on_case_insensitive_fs(tmp_path,
                                                              tracks_schema_sql,
                                                              monkeypatch):
    """macOS/Windows: derselbe Ordner in anderer Schreibweise = dieselbe Zeile.

    Perl löst das mit ``noCaseFilename`` (``Slim/Utils/OS.pm:388-390``).
    """
    folder = tmp_path / "Musik" / "Accept"
    folder.mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql)
    rid = dir_rows.ensure_dir_row(folder, db_path=db)

    monkeypatch.setattr(
        "lyrion.platform.paths.fs_is_case_insensitive", lambda: True)
    other_case = str(folder).upper() if os.name != "nt" else str(folder)
    assert dir_rows.ensure_dir_row(other_case, db_path=db) == rid
    assert len(_dir_rows(db)) == 1


# ---------------------------------------------------------------------------
# 2. Auflösung / Drilldown über die Zeile
# ---------------------------------------------------------------------------

def test_dir_row_by_id_resolves_any_row(tmp_path, tracks_schema_sql):
    """``folder_id`` → ``find('Track', $id)`` (Queries.pm:2311-2316)."""
    folder = tmp_path / "Musik" / "Accept"
    folder.mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    rid = dir_rows.ensure_dir_row(folder, db_path=db)

    row = dir_rows.dir_row_by_id(rid, db_path=db)
    assert row and row["path"] == str(folder)
    assert row["content_type"] == "dir"
    # eine Track-Zeile wird ebenfalls aufgelöst (Perl prüft den Typ nicht)
    track = dir_rows.dir_row_by_id(1, db_path=db)
    assert track and track["content_type"] == ""
    assert track["path"] == f"{ROOT}/Metal/Accept/01.mp3"
    # unbekannt / negativ → nichts
    assert dir_rows.dir_row_by_id(999999, db_path=db) is None
    assert dir_rows.dir_row_by_id(-3, db_path=db) is None
    assert dir_rows.dir_row_by_id("abc", db_path=db) is None


def test_bmf_folder_items_carry_the_dir_row_id(tmp_path, monkeypatch,
                                               tracks_schema_sql):
    """Der Browse legt die Zeile an und liefert ihre ``id`` (Queries.pm:2429)."""
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    items = _folders(db, monkeypatch, ROOT,
                     ["items", "0", "50", "menu:1", "mode:bmf"])
    assert [i["text"] for i in items] == ["Ambient", "Metal"]
    rows = {url: rid for rid, url, *_ in _dir_rows(db)}
    for item in items:
        expected = rows[folders.file_url_from_path(f"{ROOT}/{item['text']}")]
        assert item["id"] == expected
        assert item["commonParams"]["folder_id"] == str(expected)
        # Der Pfad bleibt als Token erhalten, ist aber nicht mehr die id.
        assert item["commonParams"]["url"] == f"{ROOT}/{item['text']}"


def test_bmf_folder_id_roundtrip_uses_the_stored_row(tmp_path, monkeypatch,
                                                     tracks_schema_sql):
    """``folder_id:<id>`` drillt über die Zeile, nicht über einen Pfad-Abgleich."""
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    metal = next(i for i in _folders(db, monkeypatch, ROOT,
                                     ["items", "0", "50", "menu:1", "mode:bmf"])
                 if i["text"] == "Metal")
    assert [i["text"] for i in _folders(
        db, monkeypatch, ROOT,
        ["items", "0", "50", "menu:1", "mode:bmf", f"folder_id:{metal['id']}"])
    ] == ["Accept", "Iron Maiden"]
    # Der stabile Wert: zweiter Aufruf, dieselbe id (Client-Cache).
    assert metal["id"] == next(
        i for i in _folders(db, monkeypatch, ROOT,
                            ["items", "0", "50", "menu:1", "mode:bmf"])
        if i["text"] == "Metal")["id"]


def test_bmf_lists_directories_without_any_track(tmp_path, monkeypatch,
                                                 tracks_schema_sql):
    """Ein Ordner ohne Track erscheint, weil seine Scanz-Zeile existiert.

    Perl listet ihn immer (``readDirectory``, ``Slim/Utils/Misc.pm:973-1043``);
    vorher fehlte er in der URL-Aggregation des Ports.
    """
    folder = tmp_path / "Musik" / "Leer"
    folder.mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    dir_rows.ensure_dir_row(folder, db_path=db)
    items = _folders(db, monkeypatch, str(tmp_path / "Musik"),
                     ["items", "0", "50", "menu:1", "mode:bmf"])
    assert "Leer" in [i["text"] for i in items]


def test_bmf_unknown_and_non_dir_ids_answer_empty(tmp_path, monkeypatch,
                                                  tracks_schema_sql):
    """Unbekannte/nicht-Verzeichnis-ids: leere Liste — Perls ``Leer``-Zeile.

    Live Perl 9.1.1 (read-only): die CLI-Query ``musicfolder 0 5
    folder_id:99999999`` antwortet ``count 0``, die *Menue*-Form derselben
    Query (``browselibrary items 0 1 menu:1 mode:bmf folder_id:99999999`` —
    so fragt die App) dagegen ``count 1`` mit der ``Empty``-Zeile
    (``XMLBrowser.pm:841-846``).
    """
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    for token in ("99999999", "1"):
        res = _browse(db, monkeypatch, ROOT,
                      ["items", "0", "50", "menu:1", "mode:bmf",
                       f"folder_id:{token}"])
        assert res["count"] == 1, token
        assert [it["text"] for it in res["item_loop"]] == [
            menus_mod.menu_title("EMPTY")], token


def test_folder_tracks_expansion_ignores_dir_rows(tmp_path, monkeypatch,
                                                  tracks_schema_sql):
    """``playlistcontrol folder_id`` lädt nur Tracks, keine Verzeichnisse.

    Ohne Filter käme die ``dir``-Zeile jedes Unterordners als "Track" mit
    (``Slim/Control/Queries.pm:4843`` filtert dieselbe Menge).
    """
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)
    _folders(db, monkeypatch, ROOT, ["items", "0", "50", "menu:1", "mode:bmf"])
    assert _dir_rows(db), "die Zeilen müssen existieren"
    ids = api_mod._expand_track_ids({"folder_id": f"{ROOT}/Metal"})
    assert ids == [1, 2, 3], ids


def test_mediafolder_folder_id_drills_through_the_row(tmp_path, monkeypatch,
                                                      tracks_schema_sql):
    """Auch ``musicfolder`` gibt Zeilen-ids (Queries.pm:2429) und drillt damit."""
    root = tmp_path / "Musik"
    (root / "Accept").mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql)
    monkeypatch.setattr(folders, "_pref",
                        lambda name, default="": str(root)
                        if name == "mediadirs" else default)
    monkeypatch.setattr(folders, "_ro_connection",
                        lambda db_path=None: sqlite3.connect(db))
    monkeypatch.setattr(folders, "_rw_connection",
                        lambda db_path=None: sqlite3.connect(db))
    folders.reset_caches()
    try:
        top = folders.musicfolder_result(0, 100)
        assert [i["filename"] for i in top["folder_loop"]] == ["Accept"]
        fid = top["folder_loop"][0]["id"]
        assert fid == _dir_rows(db)[0][0]
        assert folders.musicfolder_result(0, 100, folder_id=fid)["count"] == 0
    finally:
        folders.reset_caches()


# ---------------------------------------------------------------------------
# 3. Album-/Titel-/Gesamt-Queries ignorieren dir-Zeilen (Perl-Filter)
# ---------------------------------------------------------------------------

def test_info_total_songs_and_duration_ignore_dir_rows(tmp_path, monkeypatch,
                                                      tracks_schema_sql):
    """``info total songs`` = Zählung der titles-Query, ``duration`` ohne dirs.

    ``Slim/Control/Queries.pm:4843`` / ``Slim/Schema.pm:2186-2190``.
    """
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    asyncio.run(dir_rows_sync(db, tmp_path / "Musik"))
    rows = _dir_rows(db)
    assert rows, "dir-Zeilen müssen existieren"

    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)

    async def run():
        api = JSONRPCAPI()
        return (await api._json_info_total(["total", "songs", "?"]),
                await api._json_info_total(["total", "duration", "?"]))

    songs, duration = asyncio.run(run())
    assert songs == {"_songs": 4}
    assert duration == {"_duration": 0.0}


async def dir_rows_sync(db: str, root: Path) -> None:
    """Zeilen über den (async) Scan-Weg schreiben — oder direkt per sqlite."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await dir_rows.sync_dir_rows(session, [str(root)], root=str(root))
        await session.commit()
    await engine.dispose()


def test_track_list_and_search_ignore_dir_rows(tmp_path, monkeypatch,
                                               tracks_schema_sql):
    """Die Songs-Liste (``mode:tracks``) filtert wie Perl ``Queries.pm:4843``."""
    folder = tmp_path / "Musik" / "Leer"
    folder.mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    dir_rows.ensure_dir_row(folder, db_path=db)
    dir_rows.ensure_dir_row(tmp_path / "Musik" / "Accept", db_path=db)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: db)

    rows, total, plural, kind = asyncio.run(
        JSONRPCAPI()._library_rows("tracks", 0, 50))
    assert kind == "tracks"
    assert total == 4
    assert {r["id"] for r in rows} == {1, 2, 3, 4}
    assert all(str(r["id"]) != str(_dir_rows(db)[0][0]) for r in rows)


def test_songs_clause_matches_the_perl_filter():
    """Wörtliche Gegenprobe zum Perl-String ``Queries.pm:4843``."""
    assert dir_rows.songs_clause("tracks") == (
        'tracks.audio = 1 AND (tracks.content_type IS NULL OR '
        'tracks.content_type NOT IN ("cpl", "src", "ssp", "dir"))')
    assert dir_rows.not_dir_clause("t") == (
        "(t.content_type IS NULL OR t.content_type != 'dir')")
    assert dir_rows.duration_clause("tracks") == "tracks.audio = 1"


# ---------------------------------------------------------------------------
# 4. Scan und Rescan
# ---------------------------------------------------------------------------

@pytest.fixture
def scan_db(tmp_path, monkeypatch):
    """Wegwerf-Bibliotheks-DB mit dem echten Schema + gepatchtem ``db_session``."""
    db_path = tmp_path / "lyrion.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False,
                                 autoflush=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    @asynccontextmanager
    async def _session_ctx():
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr("lyrion.database.sqlite_helper.db_session",
                        _session_ctx)

    async def _fake_scan(self, file_path):
        return SimpleNamespace(title=file_path.stem, artist="A",
                               album=file_path.parent.name, compilation=False,
                               mimetype="audio/mpeg", duration=1000)

    monkeypatch.setattr("lyrion.media.scanner.MediaScanner.scan_single_file",
                        _fake_scan)
    yield db_path
    asyncio.run(engine.dispose())


def _music(tmp_path) -> Path:
    music = tmp_path / "music"
    for album in ("AlbumA", "AlbumB"):
        folder = music / album
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "one.mp3").write_bytes(b"x")
    (music / "Leer").mkdir()
    return music


def test_scan_stores_one_dir_row_per_walked_directory(scan_db, tmp_path):
    """Der Scan legt je Verzeichnis eine Zeile an (AIO.pm:62-67/:135-142)."""
    music = _music(tmp_path)

    async def run():
        await MusicImporter(ImportConfig(source_path=music)).import_music()

    asyncio.run(run())
    stored = {dir_rows.dir_path(r[1]) for r in _dir_rows(str(scan_db))}
    assert stored == {str(music), str(music / "AlbumA"),
                      str(music / "AlbumB"), str(music / "Leer")}
    # Die Zeilen sind keine Tracks: audio 0, filesize 0 (Schema.pm:1774).
    assert all(r[6] == 0 for r in _dir_rows(str(scan_db)))


def test_full_rescan_removes_orphaned_dir_rows(scan_db, tmp_path):
    """Ein voller Rescan räumt Zeilen verschwundener Ordner auf.

    Perl löscht sie mit dem ``wipe`` (``Slim/Schema.pm:2346-2357``) und
    überspringt sie sonst bewusst (``Slim/Utils/Scanner/Local.pm:186``); die
    Port-Variante entfernt genau die Zeilen, deren Ordner weg ist.
    """
    music = _music(tmp_path)

    async def run():
        imp = MusicImporter(ImportConfig(source_path=music))
        await imp.import_music()
        assert len(_dir_rows(str(scan_db))) == 4
        import shutil
        shutil.rmtree(music / "AlbumB")
        await imp.import_music()

    asyncio.run(run())
    stored = {dir_rows.dir_path(r[1]) for r in _dir_rows(str(scan_db))}
    assert stored == {str(music), str(music / "AlbumA"), str(music / "Leer")}
    # Die Track-Reconciliation hat die dir-Zeilen nicht angefasst
    # (``content_type != 'dir'``, Local.pm:186) und die Tracks selbst auch
    # nicht (AlbumB/one.mp3 ist weg, AlbumA/one.mp3 bleibt).
    con = sqlite3.connect(str(scan_db))
    try:
        urls = [r[0] for r in con.execute(
            "SELECT url FROM tracks WHERE content_type IS NULL OR "
            "content_type != 'dir'").fetchall()]
    finally:
        con.close()
    assert len(urls) == 1 and urls[0].endswith("AlbumA/one.mp3")


def test_additive_scan_never_prunes_dir_rows(scan_db, tmp_path):
    """Ein additiver Scan legt neue Zeilen an, löscht aber keine."""
    music = _music(tmp_path)

    async def run():
        await MusicImporter(ImportConfig(source_path=music)).import_music()
        import shutil
        shutil.rmtree(music / "AlbumB")
        await MusicImporter(ImportConfig(source_path=music,
                                         mode="playlists")).import_music()

    asyncio.run(run())
    stored = {dir_rows.dir_path(r[1]) for r in _dir_rows(str(scan_db))}
    assert str(music / "AlbumB") in stored


# ---------------------------------------------------------------------------
# 5. Migration (implementiert, im Test gegen eine Kopie/eigene DB)
# ---------------------------------------------------------------------------

def test_migration_dry_run_writes_nothing(tmp_path, monkeypatch,
                                          tracks_schema_sql):
    music = tmp_path / "Musik"
    (music / "Accept").mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    before = _all_tracks(db)

    report = migrate_dir_rows.plan_migration(db, roots=[str(tmp_path / "srv")])
    # Der Fixture-Pfad /srv/music existiert hier nicht → Wurzel explizit:
    report = migrate_dir_rows.plan_migration(db, roots=["/srv/music"])
    assert report["to_create"] > 0
    assert report["track_urls"] == 4
    assert "stored" not in report
    assert _all_tracks(db) == before, "Dry run darf nichts schreiben"


def test_migration_apply_creates_then_is_idempotent(tmp_path, tracks_schema_sql):
    root = tmp_path / "Musik"
    (root / "Metal" / "Accept").mkdir(parents=True)
    (root / "Metal" / "Accept" / "01.mp3").write_bytes(b"x")
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, [
        (1, folders.file_url_from_path(root / "Metal" / "Accept" / "01.mp3")),
    ])

    first = migrate_dir_rows.apply_migration(db, roots=[str(root)])
    assert first["stored"] == 3                     # root + Metal + Accept
    assert len(_dir_rows(db)) == 3
    paths = {dir_rows.dir_path(r[1]) for r in _dir_rows(db)}
    assert paths == {str(root), str(root / "Metal"), str(root / "Metal" / "Accept")}
    second = migrate_dir_rows.apply_migration(db, roots=[str(root)])
    assert second["stored"] == 0, "zweiter Lauf legt nichts Neues an"
    assert len(_dir_rows(db)) == 3
    # Die ids der Tracks bleiben unangetastet.
    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM tracks WHERE content_type IS NULL OR "
            "content_type != 'dir'").fetchone()[0] == 1
    finally:
        con.close()


def test_migration_can_prune_orphans(tmp_path, monkeypatch, tracks_schema_sql):
    root = tmp_path / "Musik"
    (root / "Gone").mkdir(parents=True)
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql)
    dir_rows.ensure_dir_row(root / "Gone", db_path=db)
    monkeypatch.setattr(
        "lyrion.platform.paths.fs_is_case_insensitive", lambda: False)
    import shutil
    shutil.rmtree(root / "Gone")

    plan = migrate_dir_rows.plan_migration(db, roots=[str(root)], walk=True)
    assert plan["orphan_rows"] == 1
    # ohne --walk wird nie gepruned (unmountbare Shares!)
    assert migrate_dir_rows.apply_migration(
        db, roots=[str(root)], prune=True)["error"]
    report = migrate_dir_rows.apply_migration(db, roots=[str(root)],
                                              walk=True, prune=True)
    assert report["pruned"] == 1
    # Nur die Wurzel bleibt — sie existiert (Perl legt sie beim Browsen an,
    # Slim/Utils/Misc.pm:1082-1090).
    assert {dir_rows.dir_path(r[1]) for r in _dir_rows(db)} == {str(root)}


def test_migration_against_a_copy_of_a_scan_db(scan_db, tmp_path):
    """Gegen eine **Kopie** der Scan-DB (kein Live-Zugriff, kein Rescan)."""
    music = _music(tmp_path)

    async def run():
        await MusicImporter(ImportConfig(source_path=music)).import_music()

    asyncio.run(run())
    copy_path = tmp_path / "copy.db"
    source = sqlite3.connect(str(scan_db))
    target = sqlite3.connect(str(copy_path))
    with target:
        source.backup(target)
    source.close()
    target.close()

    # Die Kopie auf den Zustand „vor D4" bringen: dir-Zeilen entfernen.
    con = sqlite3.connect(str(copy_path))
    con.execute("DELETE FROM tracks WHERE content_type = 'dir'")
    con.commit()
    con.close()
    assert _dir_rows(str(copy_path)) == []

    report = migrate_dir_rows.plan_migration(str(copy_path), roots=[str(music)])
    # Ohne --walk deckt die Migration die Vorfahren der Track-URLs ab: root,
    # AlbumA, AlbumB — der leere Ordner „Leer" braucht den Dateisystem-Walk.
    assert report["to_create"] == 3
    assert migrate_dir_rows.plan_migration(
        str(copy_path), roots=[str(music)], walk=True)["to_create"] == 4
    applied = migrate_dir_rows.apply_migration(str(copy_path),
                                               roots=[str(music)])
    assert applied["stored"] == 3
    stored = {dir_rows.dir_path(r[1]) for r in _dir_rows(str(copy_path))}
    assert stored == {str(music), str(music / "AlbumA"), str(music / "AlbumB")}
    # Mit --walk kommt der leere Ordner dazu (Perl listet ihn via
    # readDirectory, Slim/Utils/Misc.pm:973-1043).
    assert migrate_dir_rows.apply_migration(
        str(copy_path), roots=[str(music)], walk=True)["stored"] == 1
    assert str(music / "Leer") in {
        dir_rows.dir_path(r[1]) for r in _dir_rows(str(copy_path))}
    # idempotent gegen die Kopie
    assert migrate_dir_rows.apply_migration(
        str(copy_path), roots=[str(music)], walk=True)["stored"] == 0


def test_migration_without_a_root_stops_instead_of_guessing(tmp_path):
    report = migrate_dir_rows.plan_migration(str(tmp_path / "x.db"), roots=[])
    assert report["error"]


def test_migration_cli_defaults_to_dry_run(tmp_path, capsys,
                                           tracks_schema_sql):
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, TREE)
    rc = migrate_dir_rows.main(["--db", db, "--root", "/srv/music"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Dry run" in out
    assert _dir_rows(db) == [], "CLI ohne --apply schreibt nichts"


def test_bmf_drill_lists_a_child_without_a_track_row(tmp_path, monkeypatch,
                                                     tracks_schema_sql):
    """``mode:bmf`` lists ``readDirectory``'s children — also the row-less ones.

    Perl's bmf feed is a wrapper around the ``musicfolder`` query
    (``Slim/Menu/BrowseLibrary.pm:2044-2046`` ``_generic(…, 'musicfolder',
    ['tags:cdus'…])`` → ``Queries.pm:2169-2507`` → ``Slim/Utils/Misc.pm:1150``
    ``readDirectory`` → :973-1043), and that query creates a row for every
    child it displays (``Queries.pm:2263-2268`` ``objectForUrl({url,
    create => 1, playlist => isPlaylist($url)})``).  A list derived from the
    ``tracks`` rows alone drops them: live 192.168.1.90 (read-only 2026-09-18)
    ``folder_id:<Bollywood>`` → Perl 13 children, ours 12 — the
    ``…-Songs Mar 18 [2008].m3u`` was missing.  Both paths must agree.
    """
    root = tmp_path / "Musik"
    (root / "Bollywood").mkdir(parents=True)
    (root / "Bollywood" / "Songs.m3u").write_text("#EXTM3U\n")
    (root / "Bollywood" / "01.mp3").write_bytes(b"\0")
    db = _db(tmp_path / "lyrion.db", tracks_schema_sql, [])
    monkeypatch.setattr(folders, "_ro_connection",
                        lambda db_path=None: sqlite3.connect(db))
    monkeypatch.setattr(folders, "_rw_connection",
                        lambda db_path=None: sqlite3.connect(db))
    folders.reset_caches()
    try:
        top = _folders(db, monkeypatch, str(root),
                       ["items", "0", "50", "menu:1", "mode:bmf"])
        assert [i["text"] for i in top] == ["Bollywood"]
        drill = _folders(db, monkeypatch, str(root),
                         ["items", "0", "50", "menu:1", "mode:bmf",
                          f"folder_id:{top[0]['id']}"])
        assert {i["text"] for i in drill} == {"Songs.m3u", "01.mp3"}
        # Der musicfolder-Loop (Perl: dieselbe Query) sieht dieselben Kinder.
        mf = folders.musicfolder_result(0, 50, folder_id=top[0]["id"])
        assert mf["count"] == len(drill) == 2
        assert {i["filename"] for i in mf["folder_loop"]} == {
            "Songs.m3u", "01.mp3"}
    finally:
        folders.reset_caches()
