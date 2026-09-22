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


def test_sort_filenames_ignores_case():
    """Perl ``sortFilename``/``noCaseFilename``: ``lc`` + native collation
    (``Slim/Utils/OS.pm:334-356``) — Gross-/Kleinschreibung entscheidet nicht.
    ``Zebra`` nach ``apple``, obwohl ``Z`` vor ``a`` liegt (Byte-Ordnung)."""
    assert folders.sort_filenames(["Zebra", "apple"]) == ["apple", "Zebra"]
    assert folders.sort_filenames(["b", "A", "c"]) == ["A", "b", "c"]
    assert folders.sort_filenames(["abba", "ABBA"]) == ["abba", "ABBA"]


def test_sort_filenames_tie_break_is_readdir_order():
    """Perl sortiert stabil: bei gleichem ``lc``-Schlüssel bleibt die Reihenfolge
    der Eingabe (``readdir``) erhalten —
    ``perl -e 'print join " ", sort {lc($a) cmp lc($b)} qw(ABBA abba)'`` →
    ``ABBA abba``, umgekehrte Eingabe → ``abba ABBA``."""
    assert folders.sort_filenames(["ABBA", "abba"]) == ["ABBA", "abba"]
    assert folders.sort_filenames(["abba", "ABBA"]) == ["abba", "ABBA"]


def test_sort_filenames_does_not_consult_process_locale(monkeypatch):
    """Die Reihenfolge darf nicht von der Prozess-Locale abhängen.

    Der Perl-Server läuft unter einer UTF-8-Locale, unser Prozess unter ``C``
    — ``locale.strxfrm``/``setlocale`` dürfen darum nicht befragt werden.
    """
    names = ["Zebra", "apple", "AC+DC", "Accept", "abba", "ABBA"]
    expected = folders.sort_filenames(names)

    def boom(*_args, **_kwargs):
        raise AssertionError("sort_filenames must not use the process locale")

    monkeypatch.setattr(locale, "setlocale", boom)
    monkeypatch.setattr(locale, "strxfrm", boom)
    for value in ("C", "C.UTF-8", "en_US.UTF-8"):
        monkeypatch.setenv("LC_ALL", value)
        monkeypatch.setenv("LANG", value)
        monkeypatch.setenv("LC_COLLATE", value)
        assert folders.sort_filenames(names) == expected
    assert expected == ["abba", "ABBA", "Accept", "AC+DC", "apple", "Zebra"]


def test_sort_filenames_matches_perl_on_real_media_folder():
    """Golden: echte Namen aus ``/mnt/media/Musik`` in der Reihenfolge, die der
    Live-Perl-LMS (192.168.1.90) für ``readdirectory folder:/mnt/media/Musik``
    liefert (gruppenweise: erst alle Ordner, dann Dateien, ``sortFilename`` je
    Gruppe). Perl ignoriert in der nativen Kollation Satzzeichen und Akzente und
    faltet die Gross-/Kleinschreibung — ``Accept`` vor ``AC+DC``,
    ``Boy_Harsher_-_Careful…`` vor ``Boy_Harsher-Country_Girl…``,
    ``L’Âme Immortelle`` zwischen ``Lamb of God`` und ``Lars Leonhard``.
    """
    as_perl_sorted = [
        "Accept",
        "AC+DC",
        "Boy Harsher-Burn It Down-VINYL-2023-FWYH",
        "Boy_Harsher_-_Careful_(2019)_MP3",
        "Boy_Harsher-Country_Girl_(Extended_Version)-EP-WEB-2018-ENTiTLED",
        "Boy_Harsher-Lesser_Man_(Extended_Version)-WEB-2017-POWPOW",
        "Boy_Harsher-Pain_II-WEB-2018-WV",
        "Boy_Harsher-Yr_Body_Is_Nothing-Reissue-CD-2018-FWYH",
        "Fetenhits_-_25_Years_(2021)-NoGroup",
        "Fetenhits_Compilation_-_70s_(2020)",
        "Fetenhits_Compilation_-_90s_(2020)",
        "Fetenhits_Compilation_-_Discofox_(2020)",
        "Fetenhits_Compilation_-_Sommerparty_(2020)",
        "Fetenhits_Compilation_-_The_Real_Classics_(2020)",
        "Fetenhits_-_Disco_-",
        "Fetenhits_NDW_Maxi_Classics_Best_Of_-2020-NoGroup part01 rar",
        "Fetenhits_NDW_Maxi_Classics_Best_Of_-2020-NoGroup.part01.rar",
        "Laibach",
        "Lamb of God",
        "L’Âme Immortelle",
        "Lars Leonhard",
        "Levellers_-_Greatest_Hits_(2014)_MP3",
    ]
    # Eingabe in Perls Reihenfolge (das ist zugleich die ``readdir``-Reihenfolge
    # der Tie-Breaks) — die Sortierung darf nichts umstellen.
    assert folders.sort_filenames(list(as_perl_sorted)) == as_perl_sorted
    # Umgekehrte Eingabe: gleiche Schlüssel bleiben in Eingabe-Reihenfolge,
    # verschiedene Schlüssel landen in Perls Reihenfolge.
    assert folders.sort_filenames(list(reversed(as_perl_sorted))) == as_perl_sorted
    # Gegenprobe: reine Byte-/``lower()``-Ordnung wäre falsch.
    assert folders.sort_filenames(["Accept", "AC+DC"]) == ["Accept", "AC+DC"]
    assert sorted(["Accept", "AC+DC"]) == ["AC+DC", "Accept"]


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


# ---------------------------------------------------------------------------
# Perl ``validTypeExtensions('list|audio')`` — the drill's child filter
# ---------------------------------------------------------------------------

#: Every suffix ``types.conf`` defines with slim-type ``list``/``audio`` — the
#: set Perl's ``validTypeExtensions()`` (``Slim/Music/Info.pm:1345-1375``,
#: ``next unless $type =~ /$findTypes/`` with ``$findTypes || 'list|audio'``)
#: turns into the regex ``readDirectory``'s ``fileFilter`` uses
#: (``Slim/Utils/Misc.pm:973-975`` → ``:900-903``).  ``lnk`` is excluded: Perl
#: adds it only on Windows (``Info.pm:1371-1374``).
PERL_LIST_AUDIO_SUFFIXES: frozenset[str] = frozenset({
    "aac", "aif", "aiff", "ape", "asx", "cue", "dff", "dsf", "fla", "flac",
    "flc", "l16", "l24", "lpcm", "m3u", "m3u8", "m4a", "m4b", "mp+", "mp2",
    "mp3", "mp4", "mpc", "oga", "ogf", "ogg", "opus", "pcm", "pls", "wav",
    "wave", "wax", "wma", "wpl", "wv", "xspf",
})


def test_every_perl_valid_type_extension_is_listable():
    """The drill must not drop children Perl lists.

    Regression (live 192.168.1.90, read-only 2026-09-18):
    ``musicfolder 0 400 folder_id:<Video>`` → Perl 6 children (two of them
    ``.mp4``), ours 4 — ``LISTABLE_EXTENSIONS`` was derived from
    ``SUPPORTED_EXTENSIONS`` and missed the 14 suffixes Perl's
    ``validTypeExtensions()`` adds (``types.conf:20/22/24/30/40/42/45/47/57/62``).
    """
    missing = PERL_LIST_AUDIO_SUFFIXES - folders.LISTABLE_EXTENSIONS
    assert not missing, f"Perl lists these, we dropped them: {sorted(missing)}"


def test_listable_set_is_exactly_perls():
    """**Equality**, not containment: no file Perl does not list may appear.

    ``fileFilter`` (``Slim/Utils/Misc.pm:900-903``) drops every file that does
    not match ``validTypeExtensions('list|audio')`` — a superset would show
    files Perl hides.  The one we had was ``spx``/``tak``: both are in
    ``media/scanner.SUPPORTED_EXTENSIONS`` (decodable) but ``types.conf`` has
    no entry for either, so Perl's ``fileFilter`` never lists them.
    """
    assert folders.LISTABLE_EXTENSIONS == PERL_LIST_AUDIO_SUFFIXES
    assert "spx" not in folders.LISTABLE_EXTENSIONS
    assert "tak" not in folders.LISTABLE_EXTENSIONS


def test_listable_set_is_derived_from_types_conf():
    """The sets are computed from ``types.conf``, not hand-maintained.

    ``validTypeExtensions`` (``Slim/Music/Info.pm:1345-1388``) walks
    ``%slimTypes`` (column 4) and collects the suffixes of the matching types
    (columns 1-3 live in ``formats/lms_types.TYPES_CONF``).  Recomputing that
    here catches a typo in either table.
    """
    from lyrion.formats.lms_types import TYPES_CONF

    def suffixes(slim_type: str) -> set[str]:
        return {suffix
                for type_id, (suffixes, _mimes) in TYPES_CONF.items()
                if folders.PERL_SLIM_TYPES.get(type_id) == slim_type
                for suffix in suffixes if ":" not in suffix}

    assert folders.PERL_SONG_SUFFIXES == suffixes("audio")
    assert folders.PLAYLIST_EXTENSIONS == suffixes("playlist")
    # ``list|audio`` — the default ``$findTypes`` of ``validTypeExtensions``.
    assert folders.LISTABLE_EXTENSIONS == (
        suffixes("audio") | suffixes("playlist") | suffixes("list")
    ) - {"lnk"}            # ``lnk`` only on Windows (``Info.pm:1371-1374``)
    # ``dir`` (slim-type ``list``) has no suffix of its own; if that changes,
    # the set above grows silently — pin the count.
    assert len(folders.LISTABLE_EXTENSIONS) == 36


def test_non_music_files_are_not_listable(tmp_path):
    """``.nfo``/``.sfv``/``.par2``/``.jpg``/``.txt`` never reach the listing.

    Live Perl 9.1.1 (read-only 2026-09-22), ``musicfolder 0 50
    url:file:///mnt/media/Musik/Mittelalter/Sava-Metamorphosis-2008`` (holds
    ``.m3u`` + ``.nfo`` + ``.sfv`` + 11 ``.mp3``) → ``count 12`` with the
    ``.m3u`` as ``type 'playlist'``; ``.nfo``/``.sfv`` are absent.  The same
    folder with a release-named ``.nfo`` DIRECTORY (the real library has those
    at the top level, e.g. ``6MzM6F.…-2020-NoGroup.nfo``) still lists the
    directory — Perl lists directories unconditionally (``Misc.pm:895-899``).
    """
    root = tmp_path / "lib"
    root.mkdir()
    for name in ("00-release.nfo", "00-release.sfv", "release.par2",
                 "cover.jpg", "read me.txt", "cuesheet.cue", "list.m3u",
                 "list.pls", "01-song.mp3", "06MzM6F.release.nfo"):
        (root / name).write_bytes(b"\0")
    (root / "06MzM6F.release.nfo").unlink()
    (root / "06MzM6F.release.nfo").mkdir()          # a *folder* named *.nfo

    entries = folders.list_directory_entries(root)
    assert sorted(entries) == sorted(["06MzM6F.release.nfo", "01-song.mp3",
                                      "cuesheet.cue", "list.m3u", "list.pls"])
    assert {n: folders.item_type(str(root / n)) for n in entries} == {
        "06MzM6F.release.nfo": "folder",
        "01-song.mp3": "track",
        "cuesheet.cue": "playlist",
        "list.m3u": "playlist",
        "list.pls": "playlist",
    }


def test_playlist_suffixes_are_perls_playlist_types():
    """``wax`` is a playlist too — it is the second suffix of the ``asx`` type.

    ``types.conf``: ``asx  asx,wax  …  playlist``; ``isPlaylist`` is
    ``$slimTypes{$type} eq 'playlist'`` (``Slim/Music/Info.pm:1313-1320``), so
    ``folder_loop`` answers ``type 'playlist'`` for a ``.wax``
    (``Slim/Control/Queries.pm:2441-2443``) — ours said ``unknown``.
    """
    assert "wax" in folders.PLAYLIST_EXTENSIONS
    assert folders.item_type("x.wax") == "playlist"
    assert folders.item_type("x.m3u") == "playlist"
    assert folders.item_type("x.pls") == "playlist"
    assert folders.item_type("x.cue") == "playlist"


def test_drill_lists_the_perl_type_set(monkeypatch, tmp_path, lib_db):
    """``.mp4``/``.dsf``/``.mp2`` children appear and are ``type 'track'``.

    Perl's ``folder_loop`` type comes from ``isSong`` — slim-type ``audio``
    (``Slim/Control/Queries.pm:2483-2484``, ``Slim/Music/Info.pm:1262-1276``),
    so an ``.mp4`` (``types.conf:40`` ``mp4 … audio``) is a ``track``, not
    ``unknown``.
    """
    root = tmp_path / "lib"
    (root / "Video").mkdir(parents=True)
    for name in ("a.mp4", "b.dsf", "c.mp2", "d.flac"):
        (root / "Video" / name).write_bytes(b"\0")
    _prefs(monkeypatch, mediadirs=str(root))
    _use_lib_db(monkeypatch, lib_db)

    top = folders.musicfolder_result(0, 100)
    folder_id = top["folder_loop"][0]["id"]
    child = folders.musicfolder_result(0, 100, folder_id=folder_id)

    assert child["count"] == 4
    assert {i["filename"] for i in child["folder_loop"]} == {
        "a.mp4", "b.dsf", "c.mp2", "d.flac"}
    assert {i["type"] for i in child["folder_loop"]} == {"track"}
