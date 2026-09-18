"""Tests for ``musicfolder`` / ``folder_loop`` — Perl ``mediafolderQuery`` parity.

Regression: ``musicfolder 0 N`` answered ``count=1`` (``file:///run``, the first
path component of a track URL) because our ``mediadirs`` preference is empty and
the old handler derived the top level from ``tracks.url``.  Perl lists the
children of the media dir (``Slim/Control/Queries.pm:2169-2507`` →
``Slim/Utils/Misc.pm:1060-1160`` → ``:973-1043``) and answers ``count=303``.

Hermetic: no server, no live database, no writes.  ``folders._pref`` and the
read-only DB connection are stubbed per test.
"""

from __future__ import annotations

import locale
import os
import sqlite3
import urllib.parse
from pathlib import Path

import pytest

from lyrion.media import folders


#: ``tracks`` schema the port creates — shared fixture (``conftest``), so the
#: folder layer's INSERT has to satisfy the same NOT NULL columns as in
#: production.
@pytest.fixture
def lib_db(library_db):
    """A temp library DB with the port's ``tracks`` schema (writable)."""
    return Path(library_db)


def _use_lib_db(monkeypatch, db):
    """Point the folder layer's read *and* write handle at ``db``."""
    monkeypatch.setattr(folders, "_ro_connection",
                        lambda db_path=None: sqlite3.connect(db))
    monkeypatch.setattr(folders, "_rw_connection",
                        lambda db_path=None: sqlite3.connect(db))
    folders.reset_caches()


def _dir_rows(db) -> list[tuple[int, str]]:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT id, url FROM tracks WHERE content_type = 'dir'"
        ).fetchall()
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No live prefs, no live DB connection."""
    monkeypatch.setattr(folders, "_pref", lambda name, default="": default)
    monkeypatch.setattr(folders, "_ro_connection", lambda db_path=None: None)
    monkeypatch.setattr(folders, "_rw_connection", lambda db_path=None: None)
    folders.reset_caches()
    yield
    folders.reset_caches()


def _prefs(monkeypatch, **values):
    monkeypatch.setattr(
        folders, "_pref",
        lambda name, default="": values.get(name, default),
    )


def _library(tmp_path: Path) -> Path:
    """A small library root: two folders, one song, decoy files.

    ``Accept``/``AC+DC`` are the exact pair Perl lists first for the real
    library (``musicfolder 0 3``): under a glibc UTF-8 collation ``Accept``
    sorts before ``AC+DC``.  Assertions on order are avoided below — the sort
    is locale dependent (``Slim/Utils/OS.pm:334-353``) and the JSON-RPC
    structure diff does not compare order.
    """
    root = tmp_path / "Musik"
    (root / "Accept").mkdir(parents=True)
    (root / "AC+DC").mkdir()
    (root / "Accept" / "01 - Balls to the Wall.mp3").write_bytes(b"\0")
    (root / "infobrowser.opml").write_text("<opml/>")
    (root / "infobrowser.opml.backup").write_text("<opml/>")
    (root / "__history.m3u").write_text("#EXTM3U\n")
    (root / ".hidden").write_text("x")
    (root / "lost+found").mkdir()
    return root


# ---------------------------------------------------------------------------
# Preference reading — Perl getMediaDirs / getDirsPref
# ---------------------------------------------------------------------------


def test_get_dir_pref_accepts_list_and_comma_string(monkeypatch):
    """Prefs.pm:383-403 keeps an array; our store serialises comma separated."""
    _prefs(monkeypatch, mediadirs=["/a", "/b"])
    assert folders.get_dir_pref("mediadirs") == ["/a", "/b"]
    _prefs(monkeypatch, mediadirs="/a, /b ,")
    assert folders.get_dir_pref("mediadirs") == ["/a", "/b"]
    _prefs(monkeypatch, mediadirs="")
    assert folders.get_dir_pref("mediadirs") == []


def test_get_media_dirs_drops_ignore_in_audio_scan(monkeypatch):
    """Perl getMediaDirs('audio') removes the ignoreInAudioScan folders
    (Slim/Utils/Misc.pm:727-746)."""
    _prefs(monkeypatch, mediadirs="/a,/b", ignoreInAudioScan="/b")
    assert folders.get_media_dirs("audio") == ["/a"]
    # Without a type the ignore list is not applied (:727-732 path).
    assert folders.get_media_dirs("") == ["/a", "/b"]


# ---------------------------------------------------------------------------
# Library roots fallback (mediadirs empty)
# ---------------------------------------------------------------------------


def test_library_roots_prefers_explicit_audio_dir(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    _prefs(monkeypatch, audiodir=str(explicit))
    monkeypatch.setattr(folders, "scanner_configured_roots", lambda: ["/never"])
    assert folders.library_roots() == [str(explicit)]


def test_library_roots_prefers_scanner_roots_over_os_music(monkeypatch, tmp_path):
    """The scanner's configured root wins over the ``~/Music`` decoy."""
    scanned_root = tmp_path / "scanner-root"
    scanned_root.mkdir()
    monkeypatch.setattr(folders, "scanner_configured_roots",
                        lambda: [str(scanned_root)])
    monkeypatch.setattr(folders, "db_library_roots", lambda db_path=None: [])
    assert folders.library_roots() == [str(scanned_root)]


def test_library_roots_falls_back_to_scanned_root(monkeypatch, tmp_path):
    scanned_root = tmp_path / "from-db"
    scanned_root.mkdir()
    monkeypatch.setattr(folders, "scanner_configured_roots", lambda: [])
    monkeypatch.setattr(folders, "db_library_roots",
                        lambda db_path=None: [str(scanned_root)])
    assert folders.library_roots() == [str(scanned_root)]


def test_effective_media_dirs_uses_mediadirs_when_set(monkeypatch, tmp_path):
    root = tmp_path / "Musik"
    root.mkdir()
    _prefs(monkeypatch, mediadirs=str(root))
    assert folders.effective_media_dirs("audio") == [str(root)]


def test_effective_media_dirs_falls_back_to_roots(monkeypatch, tmp_path):
    """Empty ``mediadirs`` must NOT answer empty — the scanner's roots are used."""
    monkeypatch.setattr(folders, "scanner_configured_roots",
                        lambda: [str(tmp_path)])
    assert folders.effective_media_dirs("audio") == [str(tmp_path)]


def test_db_library_roots_derives_scanned_root(tmp_path):
    """The scanned root comes from the common ``file://`` prefix of tracks.url."""
    root = tmp_path / "Musik"
    (root / "Accept").mkdir(parents=True)
    (root / "Zappa").mkdir()
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE tracks (id INTEGER PRIMARY KEY, url TEXT);")
    for i, url in enumerate([
        folders.file_url_from_path(root / "Accept" / "a.mp3"),
        folders.file_url_from_path(root / "Zappa" / "z.mp3"),
    ]):
        con.execute("INSERT INTO tracks (id, url) VALUES (?, ?)", (i + 1, url))
    con.commit()
    con.close()

    assert folders.db_library_roots(db_path=db) == [str(root)]


# ---------------------------------------------------------------------------
# Directory listing — Perl readDirectory / fileFilter
# ---------------------------------------------------------------------------


def test_list_directory_entries_applies_perl_file_filter(tmp_path):
    """Misc.pm:835-909: dirs plus known audio/playlist files, no dotfiles, no
    ``lost+found``, no ``__*.m3u``, no unknown extensions."""
    root = _library(tmp_path)
    entries = folders.list_directory_entries(root)
    assert sorted(entries) == ["AC+DC", "Accept"]
    assert "lost+found" not in entries
    assert "infobrowser.opml" not in entries
    assert "infobrowser.opml.backup" not in entries
    assert "__history.m3u" not in entries
    assert ".hidden" not in entries


def test_list_directory_entries_keeps_audio_and_playlists(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "song.mp3").write_bytes(b"\0")
    (root / "list.m3u").write_text("#EXTM3U\n")
    (root / "notes.txt").write_text("nope")
    assert sorted(folders.list_directory_entries(root)) == ["list.m3u", "song.mp3"]


def test_list_directory_entries_honours_ignore_dir_re(monkeypatch, tmp_path):
    """Misc.pm:859-861 — ``ignoreDirRE`` removes matching entries."""
    root = tmp_path / "lib"
    root.mkdir()
    (root / "Keep").mkdir()
    (root / "@eaDir").mkdir()
    _prefs(monkeypatch, ignoreDirRE=r"^@eaDir$")
    assert folders.list_directory_entries(root) == ["Keep"]


def test_list_directory_entries_missing_dir_is_empty(tmp_path):
    """Perl readDirectory: opendir failure → empty list (Misc.pm:1010-1014)."""
    assert folders.list_directory_entries(tmp_path / "nope") == []


def test_sort_filenames_falls_back_to_case_insensitive(monkeypatch):
    def boom(*_args, **_kwargs):
        raise locale.Error("no locale")

    monkeypatch.setattr(locale, "setlocale", boom)
    assert folders.sort_filenames(["b", "A", "c"]) == ["A", "b", "c"]


# ---------------------------------------------------------------------------
# file URL encoding — Perl fileURLFromPath
# ---------------------------------------------------------------------------


def test_file_url_from_path_matches_perl_encoding():
    """Misc.pm:292-331 — ``URI::file`` escapes : = , + space and non-ASCII."""
    assert folders.file_url_from_path(
        "/run/user/1000/gvfs/smb-share:server=media.local,share=media/Musik"
    ) == ("file:///run/user/1000/gvfs/"
          "smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik")
    assert folders.file_url_from_path("/m/Ac+DC a") == "file:///m/Ac%2BDC%20a"
    assert folders.file_url_from_path("/m/Die Ärzte") == \
        "file:///m/Die%20%C3%84rzte"
    # Idempotent for values that are already URLs.
    assert folders.file_url_from_path("file:///x") == "file:///x"


def test_path_from_file_url_round_trips():
    path = "/run/user/1000/gvfs/smb-share:server=a,share=b/Musik/AC+DC"
    assert folders.path_from_file_url(folders.file_url_from_path(path)) == path
    assert folders.path_from_file_url("tmp:///x y") == "/x y"


# ---------------------------------------------------------------------------
# item types
# ---------------------------------------------------------------------------


def test_item_type_matches_perl(tmp_path):
    """Queries.pm:2472-2485 — folder / playlist / track / unknown."""
    (tmp_path / "Dir").mkdir()
    (tmp_path / "song.mp3").write_bytes(b"\0")
    (tmp_path / "list.m3u").write_text("#EXTM3U\n")
    (tmp_path / "notes.txt").write_text("x")
    assert folders.item_type(str(tmp_path / "Dir")) == "folder"
    assert folders.item_type(str(tmp_path / "song.mp3")) == "track"
    assert folders.item_type(str(tmp_path / "list.m3u")) == "playlist"
    assert folders.item_type(str(tmp_path / "notes.txt")) == "unknown"


# ---------------------------------------------------------------------------
# normalize — Perl Request::normalize
# ---------------------------------------------------------------------------


def test_normalize_paging():
    assert folders.normalize(0, 3, 10) == (True, 0, 2)
    assert folders.normalize(8, 5, 10) == (True, 8, 9)
    assert folders.normalize(10, 5, 10) == (False, 0, 0)
    assert folders.normalize(0, 0, 10) == (False, 0, 0)
    assert folders.normalize(0, 5, 0) == (False, 0, 0)


# ---------------------------------------------------------------------------
# mediafolder_result — the query
# ---------------------------------------------------------------------------


def test_musicfolder_lists_children_of_configured_media_dir(monkeypatch, tmp_path,
                                                            lib_db):
    """``mediadirs`` set → the folder's children in Perl loop shape.

    Perl hands out each directory's ``tracks`` row id (``Queries.pm:2429``,
    row created by the browse :2263-2268, ``Slim/Utils/Misc.pm:1082-1090``).
    """
    root = _library(tmp_path)
    _prefs(monkeypatch, mediadirs=str(root))
    _use_lib_db(monkeypatch, lib_db)

    result = folders.musicfolder_result(0, 100)

    assert result["count"] == 2                       # AC+DC, Accept
    by_name = {i["filename"]: i for i in result["folder_loop"]}
    assert set(by_name) == {"AC+DC", "Accept"}
    rows = _dir_rows(lib_db)
    assert len(rows) == 2                             # one dir row per folder
    ids = {url: rid for rid, url in rows}
    for name in ("AC+DC", "Accept"):
        expected = ids[folders.file_url_from_path(root / name)]
        assert isinstance(by_name[name]["id"], int)
        assert by_name[name]["id"] == expected, by_name[name]
    assert by_name["AC+DC"]["id"] != by_name["Accept"]["id"]
    # Perl sends exactly these three keys with no tags requested (:2429-2487).
    assert all(set(i) == {"id", "filename", "type"} for i in by_name.values())


def test_musicfolder_folder_rows_are_real_dir_rows(monkeypatch, tmp_path,
                                                   lib_db):
    """The row is Perl's: ``content_type='dir'``, ``filesize`` 0, not audio.

    ``Slim/Schema.pm:1774`` (``$ct eq 'dir'`` → simple create path) and
    ``Slim/Utils/Scanner/Local/AIO.pm:67`` ("size, 0 for dirs").
    """
    root = _library(tmp_path)
    _prefs(monkeypatch, mediadirs=str(root))
    _use_lib_db(monkeypatch, lib_db)

    folders.musicfolder_result(0, 100)

    con = sqlite3.connect(lib_db)
    try:
        rows = con.execute(
            "SELECT url, content_type, audio, filesize, title FROM tracks "
            "WHERE content_type = 'dir' ORDER BY url").fetchall()
    finally:
        con.close()
    assert len(rows) == 2
    assert all(r[1] == "dir" and r[2] == 0 and r[3] == 0 for r in rows)
    # ``title`` stays empty: Perl shows ``filename`` (fileName of the URL),
    # never ``tracks.title``, for a folder (Queries.pm:2429-2430).
    assert all(r[4] == "" for r in rows)


def test_musicfolder_without_writable_db_keeps_the_url_id(monkeypatch, tmp_path):
    """No writable library DB → documented fallback, browse stays usable."""
    root = _library(tmp_path)
    _prefs(monkeypatch, mediadirs=str(root))

    result = folders.musicfolder_result(0, 100)
    assert result["count"] == 2
    assert all(i["id"].startswith("file://") for i in result["folder_loop"])


def test_musicfolder_empty_mediadirs_uses_library_root(monkeypatch, tmp_path):
    """Empty ``mediadirs`` + no configured root → the scanned root is browsed,
    never a bogus top-level path component."""
    root = _library(tmp_path)
    monkeypatch.setattr(folders, "scanner_configured_roots", lambda: [str(root)])
    monkeypatch.setattr(folders, "db_library_roots", lambda db_path=None: [])
    _prefs(monkeypatch, mediadirs="")

    result = folders.musicfolder_result(0, 100)

    assert result["count"] == 2
    assert {i["filename"] for i in result["folder_loop"]} == {"AC+DC", "Accept"}
    # None of the entries is a bare path component like „run“ (the old bug),
    # and without a writable DB the id stays the folder's file URL.
    assert "file:///run" not in {i["id"] for i in result["folder_loop"]}
    assert {i["id"] for i in result["folder_loop"]} == {
        folders.file_url_from_path(root / "AC+DC"),
        folders.file_url_from_path(root / "Accept")}


def test_musicfolder_empty_mediadirs_and_no_root_is_empty(monkeypatch):
    monkeypatch.setattr(folders, "library_roots", lambda: [])
    _prefs(monkeypatch, mediadirs="")
    assert folders.musicfolder_result(0, 100) == {"count": 0}


def test_musicfolder_quantity_zero_returns_count_only(monkeypatch, tmp_path):
    """Perl adds ``count`` and skips the loop when quantity is 0 (:2385, :2507)."""
    root = _library(tmp_path)
    _prefs(monkeypatch, mediadirs=str(root))
    assert folders.musicfolder_result(0, 0) == {"count": 2}


def test_musicfolder_pages_entries(monkeypatch, tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    for name in ("A", "B", "C", "D"):
        (root / name).mkdir()
    _prefs(monkeypatch, mediadirs=str(root))
    page = folders.musicfolder_result(1, 2)
    assert page["count"] == 4
    assert [i["filename"] for i in page["folder_loop"]] == ["B", "C"]


def test_musicfolder_multiple_roots_lists_the_roots(monkeypatch, tmp_path):
    """Perl Queries.pm:2272-2278 — >1 media dir and no datum → list the roots."""
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    _prefs(monkeypatch, mediadirs=f"{one},{two}")
    result = folders.musicfolder_result(0, 100)
    assert result["count"] == 2
    assert {i["filename"] for i in result["folder_loop"]} == {"one", "two"}
    assert all(i["type"] == "folder" for i in result["folder_loop"])


def test_musicfolder_ignore_in_audio_scan_becomes_volatile_root(monkeypatch, tmp_path):
    """Perl :2208-2215 — ignored folders stay browsable, marked with brackets.

    Perl pushes them onto ``$mediaDirs`` as ``tmp://`` entries, which also turns
    a single-root browse into a root listing (``Queries.pm:2272-2278``).
    """
    active = tmp_path / "active"
    ignored = tmp_path / "ignored"
    active.mkdir()
    ignored.mkdir()
    _prefs(monkeypatch, mediadirs=f"{active},{ignored}",
           ignoreInAudioScan=str(ignored))
    result = folders.musicfolder_result(0, 100)
    assert result["count"] == 2
    assert {i["filename"] for i in result["folder_loop"]} == {"active", "[ignored]"}
    assert all(i["type"] == "folder" for i in result["folder_loop"])


# ---------------------------------------------------------------------------
# folder_id resolution
# ---------------------------------------------------------------------------


def test_resolve_folder_id_accepts_url_and_path(tmp_path):
    folder = tmp_path / "Musik" / "Accept"
    folder.mkdir(parents=True)
    assert folders.resolve_folder_id(str(folder)) == str(folder)
    assert folders.resolve_folder_id(folders.file_url_from_path(folder)) == str(folder)
    assert folders.resolve_folder_id("") is None


def test_resolve_folder_id_accepts_numeric_track_id(tmp_path, monkeypatch):
    """Perl resolves ``folder_id`` as a numeric Track id (Queries.pm:2311-2316)."""
    folder = tmp_path / "Musik" / "Accept"
    folder.mkdir(parents=True)
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE tracks (id INTEGER PRIMARY KEY, url TEXT);")
    con.execute("INSERT INTO tracks (id, url) VALUES (?, ?)",
                (204572, folders.file_url_from_path(folder)))
    con.commit()
    con.close()

    monkeypatch.setattr(folders, "_ro_connection",
                        lambda db_path=None: sqlite3.connect(db))
    assert folders.resolve_folder_id("204572") == str(folder)
    assert folders.resolve_folder_id("999999") is None


def test_musicfolder_drills_into_folder_id(monkeypatch, tmp_path, lib_db):
    root = tmp_path / "lib"
    (root / "Album").mkdir(parents=True)
    (root / "Album" / "song.mp3").write_bytes(b"\0")
    _prefs(monkeypatch, mediadirs=str(root))
    _use_lib_db(monkeypatch, lib_db)

    top = folders.musicfolder_result(0, 100)
    assert [i["filename"] for i in top["folder_loop"]] == ["Album"]

    child = folders.musicfolder_result(0, 100, folder_id=top["folder_loop"][0]["id"])
    assert [i["filename"] for i in child["folder_loop"]] == ["song.mp3"]
    assert child["folder_loop"][0]["type"] == "track"


def test_musicfolder_uses_the_dir_row_id_of_an_existing_row(tmp_path, monkeypatch,
                                                            lib_db):
    """The id is ``tracks.id`` — the row that already exists is used as is."""
    root = tmp_path / "lib"
    (root / "Album").mkdir(parents=True)
    con = sqlite3.connect(lib_db)
    con.execute(
        "INSERT INTO tracks (id, url, title, titlesort, content_type, audio)"
        " VALUES (?, ?, ?, ?, 'dir', 0)",
        (7, folders.file_url_from_path(root / "Album"), "", ""))
    con.commit()
    con.close()

    _use_lib_db(monkeypatch, lib_db)
    _prefs(monkeypatch, mediadirs=str(root))
    result = folders.musicfolder_result(0, 100)
    assert result["folder_loop"][0]["id"] == 7
    # …and no second row was created for the same directory
    assert len(_dir_rows(lib_db)) == 1


def test_musicfolder_ct_tag_reports_the_perl_content_type(monkeypatch, tmp_path,
                                                          lib_db):
    """``tags:o`` adds ``ct`` (Queries.pm:2494-2496); live Perl sends
    ``"ct":"dir"`` for a folder."""
    root = _library(tmp_path)
    _prefs(monkeypatch, mediadirs=str(root))
    _use_lib_db(monkeypatch, lib_db)
    item = folders.musicfolder_result(0, 100, tags="sto")["folder_loop"][0]
    assert item["ct"] == "dir"


def test_tags_add_the_perl_optional_fields(monkeypatch, tmp_path):
    """Queries.pm:2489-2497 — tag letters add textkey/url/title."""
    root = tmp_path / "lib"
    (root / "Album").mkdir(parents=True)
    _prefs(monkeypatch, mediadirs=str(root))
    item = folders.musicfolder_result(0, 100, tags="stu")["folder_loop"][0]
    assert item["textkey"] == "A"
    assert item["title"] == "Album"
    assert item["url"] == folders.file_url_from_path(root / "Album")


def test_media_type_audio_filters_ignore_list(monkeypatch, tmp_path):
    """Perl getMediaDirs removes the ignoreInAudioScan folders for ``audio``.

    ``mediafolderQuery`` always browses ``$type || 'audio'``
    (``Queries.pm:2205``), so the ignored folder never stays an active root —
    it comes back only as the bracketed "volatile" entry (:2208-2215).
    """
    keep = tmp_path / "keep"
    drop = tmp_path / "drop"
    keep.mkdir()
    drop.mkdir()
    (keep / "Band").mkdir()
    _prefs(monkeypatch, mediadirs=f"{keep},{drop}", ignoreInAudioScan=str(drop))

    assert folders.get_media_dirs("audio") == [str(keep)]
    assert folders.get_media_dirs("") == [str(keep), str(drop)]

    result = folders.mediafolder_result(0, 100)
    assert {i["filename"] for i in result["folder_loop"]} == {"keep", "[drop]"}
    assert result["count"] == 2


def test_scanner_configured_roots_only_returns_existing_dirs(monkeypatch, tmp_path):
    """The compiled defaults (/mnt/media*/Musik) are skipped when absent."""
    monkeypatch.setattr(folders, "_scanner_root_candidates",
                        lambda: ["/does/not/exist", "/also/not/here"])
    assert folders.scanner_configured_roots() == []


def test_scanner_configured_roots_returns_existing_roots(monkeypatch, tmp_path):
    root = tmp_path / "import-root"
    root.mkdir()
    missing = tmp_path / "nope"
    monkeypatch.setattr(folders, "_scanner_root_candidates",
                        lambda: [str(root), str(missing), str(root)])
    assert folders.scanner_configured_roots() == [str(root)]


def test_urllib_quote_is_used_for_plus(tmp_path):
    """Guards the URL encoding against a regression to ``Path.as_uri`` (which
    leaves ``+``/``,``/``:`` unescaped, unlike ``tracks.url``)."""
    url = folders.file_url_from_path("/m/AC+DC")
    assert urllib.parse.unquote(url) == "file:///m/AC+DC"
    assert "+" not in url
