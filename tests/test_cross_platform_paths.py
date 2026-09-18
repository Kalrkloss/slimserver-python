"""Cross-platform path/URL rules and the gvfs diagnostics.

Covers :mod:`lyrion.platform.paths` — the single place that decides where the
server's directories live per OS (Perl ``Slim/Utils/OS*``) and how a filesystem
path becomes a ``file://`` URL and back (Perl ``Slim/Utils/Misc.pm:223-351``) —
plus the start-up warning for unusable music folders and a static guard that no
unguarded POSIX-only call survives in the tree (the Windows startability
requirement).

Hermetic: no server, no live database, no system changes.  Everything that
would need a real Windows/macOS host is driven through the explicit
``os_name=``/``home=``/``env=`` parameters of the platform module — the honest
limit is stated in ``references/cross-platform.md``.
"""

from __future__ import annotations

import ast
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from lyrion.platform import paths as platform_paths

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "lyrion"

# ---------------------------------------------------------------------------
# fixtu
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# directory rules per OS (Perl dirsFor)
# ---------------------------------------------------------------------------


def test_windows_dirs_match_perl_writable_path():
    """Win32.pm:153-187 / :485-560 — %ProgramData%\\Lyrion\\{prefs,Cache,Logs}."""
    env = {"PROGRAMDATA": "C:\\ProgramData", "USERPROFILE": "C:\\Users\\k"}
    assert platform_paths.dirs_for("prefs", os_name="win", env=env)[0] == \
        Path("C:/ProgramData/Lyrion/prefs")
    assert platform_paths.dirs_for("cache", os_name="win", env=env)[0] == \
        Path("C:/ProgramData/Lyrion/Cache")
    assert platform_paths.dirs_for("log", os_name="win", env=env)[0] == \
        Path("C:/ProgramData/Lyrion/Logs")
    # CSIDL_MYMUSIC → %USERPROFILE%\Music, playlists → <music>\Playlists
    assert platform_paths.dirs_for("music", os_name="win", env=env)[0] == \
        Path("C:/Users/k/Music")
    assert platform_paths.dirs_for("playlists", os_name="win", env=env)[0] == \
        Path("C:/Users/k/Music/Playlists")


def test_windows_falls_back_to_c_programdata_without_env():
    """The env vars are not guaranteed — Perl falls back to the shell folders."""
    dirs = platform_paths.dirs_for("prefs", os_name="win", env={})
    assert dirs[0] == Path("C:/ProgramData/Lyrion/prefs")


def test_macos_dirs_match_perl_osx():
    """OSX.pm:169-205 — Library/{Application Support,Caches,Logs} and ~/Music."""
    home = "/Users/k"
    assert platform_paths.dirs_for("prefs", os_name="mac", home=home)[0] == \
        Path("/Users/k/Library/Application Support/Squeezebox")
    assert platform_paths.dirs_for("cache", os_name="mac", home=home)[0] == \
        Path("/Users/k/Library/Caches/Squeezebox")
    assert platform_paths.dirs_for("log", os_name="mac", home=home)[0] == \
        Path("/Users/k/Library/Logs/Squeezebox")
    assert platform_paths.dirs_for("music", os_name="mac", home=home)[0] == \
        Path("/Users/k/Music")
    assert platform_paths.dirs_for("playlists", os_name="mac", home=home)[0] == \
        Path("/Users/k/Music/Playlists")


def test_unix_has_no_default_music_folder():
    """Unix.pm:68-71 — Perl returns '' for music/playlists on Unix."""
    assert platform_paths.dirs_for("music", os_name="linux") == []
    assert platform_paths.dirs_for("playlists", os_name="linux") == []
    assert platform_paths.default_music_dir(os_name="linux") is None
    # …but Windows/macOS do have one.
    assert platform_paths.default_music_dir(
        os_name="win", env={"USERPROFILE": "C:/Users/k"}) == Path("C:/Users/k/Music")
    assert platform_paths.default_music_dir(
        os_name="mac", home="/Users/k") == Path("/Users/k/Music")


def test_packaged_linux_layout_matches_debian_pm():
    """Debian.pm:77-91 — the packaged layout under /var/{lib,log}."""
    assert platform_paths.dirs_for("prefs", os_name="linux", packaged=True)[0] == \
        Path("/var/lib/squeezeboxserver/prefs")
    assert platform_paths.dirs_for("cache", os_name="linux", packaged=True)[0] == \
        Path("/var/lib/squeezeboxserver/cache")
    assert platform_paths.dirs_for("log", os_name="linux", packaged=True)[0] == \
        Path("/var/log/squeezeboxserver")


def test_port_dir_per_os():
    """The port's own layout: explicit root wins, else the per-OS default."""
    env = {"PROGRAMDATA": "C:\\ProgramData", "USERPROFILE": "C:\\Users\\k"}
    assert platform_paths.port_dir("prefs", serverdata="/tmp/sd") == Path("/tmp/sd/Prefs")
    assert platform_paths.port_dir("log", serverdata="/tmp/sd") == Path("/tmp/sd/Logs")
    assert platform_paths.port_dir("cache", serverdata="/tmp/sd") == Path("/tmp/sd/Cache")
    assert platform_paths.port_dir("prefs", os_name="win", env=env) == \
        Path("C:/ProgramData/Lyrion/prefs")
    assert platform_paths.port_dir("log", os_name="mac", home="/Users/k") == \
        Path("/Users/k/Library/Logs/Squeezebox")
    assert platform_paths.port_dir("log", os_name="linux", home="/home/k") == \
        Path("/home/k/.lyrion/Lyrion/Logs")


def test_serverdata_resolution_order(tmp_path):
    """cli > env > migration (existing data) > OS default."""
    home = tmp_path / "home"
    legacy = home / ".lyrion" / "Lyrion" / "Prefs"
    legacy.mkdir(parents=True)
    (legacy / "lyrion.db").write_bytes(b"")

    assert platform_paths.resolve_serverdata_dir(
        cli_value="/tmp/from-cli", env_value="/tmp/from-env", home=home) == \
        (Path("/tmp/from-cli"), "cli")
    assert platform_paths.resolve_serverdata_dir(
        env_value="/tmp/from-env", home=home) == (Path("/tmp/from-env"), "env")
    # Existing data wins over the OS default: an upgrade must not lose the library.
    assert platform_paths.resolve_serverdata_dir(home=home) == \
        (home / ".lyrion" / "Lyrion", "migration")


def test_serverdata_falls_back_to_os_default(tmp_path):
    """No data anywhere → the per-OS default (never a hardcoded Linux path)."""
    home = tmp_path / "empty-home"
    home.mkdir()
    assert platform_paths.resolve_serverdata_dir(home=home) == \
        (home / ".lyrion" / "Lyrion", "os-default")
    assert platform_paths.resolve_serverdata_dir(os_name="mac", home=home) == \
        (home / "Library" / "Application Support" / "Squeezebox", "os-default")
    assert platform_paths.resolve_serverdata_dir(
        os_name="win", home=home, environ={"PROGRAMDATA": "C:\\ProgramData"}) == \
        (Path("C:/ProgramData/Lyrion"), "os-default")


# ---------------------------------------------------------------------------
# path <-> file URL
# ---------------------------------------------------------------------------


def test_gvfs_smb_url_round_trip():
    """The live music URL shape: /run/user/<uid>/gvfs/smb-share:server=…,share=…."""
    path = ("/run/user/1000/gvfs/smb-share:server=192.168.1.90,share=musik"
            "/Musik/Album/01 - Title.mp3")
    url = platform_paths.file_url_from_path(path)
    assert url == ("file:///run/user/1000/gvfs/smb-share%3Aserver%3D192.168.1.90"
                   "%2Cshare%3Dmusik/Musik/Album/01%20-%20Title.mp3")
    assert platform_paths.path_from_file_url(url) == path


def test_gvfs_url_accepts_encoded_and_plain_mount_ids():
    """Both spellings occur: our stored URLs are percent-encoded, clients may not."""
    want = "/run/user/1000/gvfs/smb-share:server=192.168.1.90,share=musik/Musik"
    encoded = ("file:///run/user/1000/gvfs/smb-share%3Aserver%3D192.168.1.90"
               "%2Cshare%3Dmusik/Musik")
    plain = "file:///run/user/1000/gvfs/smb-share:server=192.168.1.90,share=musik/Musik"
    assert platform_paths.path_from_file_url(encoded) == want
    assert platform_paths.path_from_file_url(plain) == want


def test_plus_is_a_literal_plus_not_a_space():
    """URI::file escapes '+' as %2B; unquote_plus would corrupt the name."""
    path = "/m/AC+DC a+b"
    url = platform_paths.file_url_from_path(path)
    assert url == "file:///m/AC%2BDC%20a%2Bb"
    assert platform_paths.path_from_file_url(url) == path
    # A hand-written URL with a raw '+' must stay a '+' as well.
    assert platform_paths.path_from_file_url("file:///m/Ac+DC") == "/m/Ac+DC"


def test_umlauts_and_special_characters_round_trip():
    """Non-ASCII and the characters URI::file escapes."""
    for path in ("/m/Die Ärzte/Übermäßig.mp3", "/m/a:b,c=d;e@f.mp3", "/m/Café & Bar"):
        url = platform_paths.file_url_from_path(path)
        assert url.startswith("file:///m/")
        assert platform_paths.path_from_file_url(url) == path


def test_trailing_space_is_preserved():
    """Misc.pm:331-342, Bug 15511 — URI::file strips trailing space."""
    path = "/m/trailing "
    url = platform_paths.file_url_from_path(path)
    assert url.endswith("%20")
    assert platform_paths.path_from_file_url(url) == path


def test_windows_drive_letter_round_trip():
    """file:///C:/… ↔ C:\\… (URI::file keeps the drive colon literal)."""
    path = "C:\\Users\\k\\Musik\\Album\\01.mp3"
    url = platform_paths.file_url_from_path(path, os_name="win")
    assert url == "file:///C:/Users/k/Musik/Album/01.mp3"
    assert platform_paths.path_from_file_url(url, os_name="win") == path


def test_unc_share_round_trip():
    """file://server/share/… ↔ \\\\server\\share\\… (network drive)."""
    path = "\\\\server\\share\\Musik\\01.mp3"
    url = platform_paths.file_url_from_path(path, os_name="win")
    assert url == "file://server/share/Musik/01.mp3"
    assert platform_paths.path_from_file_url(url, os_name="win") == path


def test_windows_localhost_authority_is_dropped():
    """Perl Misc.pm:260-263 — file://localhost/… behaves like file:///…."""
    assert platform_paths.path_from_file_url(
        "file://localhost/C:/x.mp3", os_name="win") == "C:\\x.mp3"
    assert platform_paths.path_from_file_url(
        "file://localhost/m/x", os_name="linux") == "/m/x"


def test_macos_volume_paths_round_trip():
    """External volumes are plain absolute paths (OSX.pm:183-199)."""
    path = "/Volumes/Ext SSD/Music/01.mp3"
    url = platform_paths.file_url_from_path(path)
    assert url == "file:///Volumes/Ext%20SSD/Music/01.mp3"
    assert platform_paths.path_from_file_url(url) == path


def test_windows_backslashes_inside_urls_are_accepted():
    """Misc.pm:255, Bug 3589 — file://C:\\foo\\bar is a valid spelling."""
    assert platform_paths.path_from_file_url(
        "file:///C:\\Users\\k\\a.mp3", os_name="win") == "C:\\Users\\k\\a.mp3"


def test_query_and_fragment_are_dropped_like_uri_path():
    """Perl uses $uri->path, which excludes ?query and #fragment."""
    assert platform_paths.path_from_file_url(
        "file:///m/a.mp3?mode=ro#x") == "/m/a.mp3"


def test_traversal_urls_are_rejected():
    """Misc.pm:271-274 — no '..' in file URLs."""
    assert platform_paths.path_from_file_url("file:///etc/../secret") == ""
    assert platform_paths.path_from_file_url(
        "file:///etc/../secret", allow_traversal=True) == "/etc/../secret"


def test_non_file_urls_are_returned_unchanged():
    """Misc.pm:231-236 — 'Path isn't a file URL' → returned as-is."""
    assert platform_paths.path_from_file_url("http://x/y.mp3") == "http://x/y.mp3"
    assert platform_paths.path_from_file_url("spotify:track:1") == "spotify:track:1"
    # The port's own tmp:// pseudo scheme still strips the scheme.
    assert platform_paths.path_from_file_url("tmp:///x y") == "/x y"


def test_urls_are_passed_through_untouched():
    """Misc.pm:314 — isURL paths are not re-encoded."""
    for url in ("file:///x", "http://x/y", "db:track", "spotify:track:1"):
        assert platform_paths.file_url_from_path(url) == url


def test_sqlite_url_encoding():
    """A path with spaces must not produce a broken sqlite URI."""
    url = platform_paths.sqlite_url_for_path("/tmp/a b/l.db", read_only=True)
    assert url == "file:/tmp/a%20b/l.db?mode=ro"
    assert platform_paths.sqlite_url_for_path("/tmp/x.db").startswith("sqlite+aiosqlite:///")


def test_safe_filename_strips_windows_forbidden_characters():
    """No generated file name may contain <>:"/\\|?* or end in a dot/space."""
    assert platform_paths.safe_filename('a<b>c:d"e/f\\g|h?i*j.') == "a_b_c_d_e_f_g_h_i_j"
    assert platform_paths.safe_filename("Album. ") == "Album"
    assert platform_paths.safe_filename("") == "_"


def test_split_path_list_keeps_gvfs_mount_ids_intact():
    """A comma inside a gvfs mount id is not a list separator."""
    gvfs = "/run/user/1000/gvfs/smb-share:server=192.168.1.90,share=musik/Musik"
    assert platform_paths.split_path_list(gvfs) == [gvfs]
    assert platform_paths.split_path_list(f"{gvfs},/m/other") == [gvfs, "/m/other"]
    assert platform_paths.split_path_list("/a,/b , /c") == ["/a", "/b", "/c"]
    assert platform_paths.split_path_list(["x", "y"]) == ["x", "y"]
    assert platform_paths.split_path_list("") == []
    assert platform_paths.split_path_list(None) == []
    # And the folder layer uses the same splitter.
    from lyrion.media import folders

    assert folders.as_path_list(gvfs) == [gvfs]


# ---------------------------------------------------------------------------
# gvfs mount state and missing-path diagnostics
# ---------------------------------------------------------------------------


def test_gvfs_mount_root_and_detection():
    path = "/run/user/1000/gvfs/smb-share:server=h,share=s/Musik/Album"
    assert platform_paths.gvfs_mount_root(path) == Path(
        "/run/user/1000/gvfs/smb-share:server=h,share=s")
    assert platform_paths.is_gvfs_path(path)
    assert not platform_paths.is_gvfs_path("/mnt/media2/Musik")
    assert platform_paths.gvfs_mount_root("/mnt/x") is None


def test_gvfs_unmounted_mount_is_reported_as_not_mounted():
    """A gvfs mount dir that does not exist means the share is not attached."""
    path = "/run/user/1000/gvfs/smb-share:server=192.168.1.90,share=musik/Musik"
    assert platform_paths.gvfs_mount_state(path) == "not-mounted"
    code, message = platform_paths.explain_missing_path(path)
    assert code == platform_paths.MOUNT_NOT_MOUNTED
    assert "not mounted" in message


def test_gvfs_mount_states_with_a_mount_point(monkeypatch, tmp_path):
    """mounted / empty distinction, with the FUSE probe stubbed (no real mount)."""
    mount = tmp_path / "smb-share:server=h,share=s"
    child = mount / "Musik"
    monkeypatch.setattr(platform_paths, "gvfs_mount_root", lambda p: mount)
    monkeypatch.setattr(platform_paths, "_is_mount_point", lambda p: True)

    child.mkdir(parents=True)
    assert platform_paths.gvfs_mount_state(str(child)) == "mounted"

    for entry in mount.iterdir():
        entry.rmdir()
    assert platform_paths.gvfs_mount_state(str(child)) == "empty"

    # A directory that exists but is not a mount point → the share is gone.
    monkeypatch.setattr(platform_paths, "_is_mount_point", lambda p: False)
    assert platform_paths.gvfs_mount_state(str(child)) == "not-mounted"


def test_gvfs_share_with_entries_is_mounted_without_a_mount_entry(monkeypatch, tmp_path):
    """Regression: a readable gvfs share must never be "not-mounted".

    Measured on the live session this test models: the share directory
    ``/run/user/1000/gvfs/smb-share:server=media.local,share=media`` holds 20
    entries (305 in the ``Musik`` folder below it) while
    ``os.path.ismount(share)`` is ``False`` and ``/proc/self/mounts`` lists only
    the single ``gvfsd-fuse /run/user/1000/gvfs`` mount — gvfs shares are plain
    sub-directories of it.  The old check demanded a mount point and therefore
    answered ``not-mounted`` for a perfectly readable folder.
    """
    share = tmp_path / "smb-share:server=media.local,share=media"
    (share / "Musik" / "Album").mkdir(parents=True)
    (share / "Musik" / "Album" / "a.flac").write_bytes(b"x")
    monkeypatch.setattr(platform_paths, "gvfs_mount_root", lambda p: share)
    # Exactly what the real share reports: not a mount point of its own.
    monkeypatch.setattr(platform_paths, "_is_mount_point", lambda p: False)
    monkeypatch.setattr(platform_paths, "_proc_mount_points", lambda: set())

    music = str(share / "Musik")
    assert platform_paths.gvfs_mount_state(music) == "mounted"
    assert platform_paths.explain_missing_path(music) == (platform_paths.OK, "")
    assert platform_paths.warn_about_media_dirs([music]) == []
    assert platform_paths.is_usable_dir(music)


def test_gvfs_three_cases_stay_distinguishable(monkeypatch, tmp_path, caplog):
    """Detached share vs. attached-but-empty vs. item does not exist."""
    share = tmp_path / "smb-share:server=h,share=s"
    music = share / "Musik"
    monkeypatch.setattr(platform_paths, "gvfs_mount_root", lambda p: share)
    # The tmp tree only *models* the gvfs layout; the URL-less path needs the
    # predicate stubbed too so explain_missing_path takes the gvfs branch.
    monkeypatch.setattr(platform_paths, "is_gvfs_path", lambda p: True)

    # 1. The share is not attached at all: its directory is gone.
    assert platform_paths.gvfs_mount_state(str(music)) == "not-mounted"
    code, message = platform_paths.explain_missing_path(music)
    assert code == platform_paths.MOUNT_NOT_MOUNTED
    assert "not mounted" in message

    # 2. Attached, but the share lists nothing: empty, not not-mounted.
    monkeypatch.setattr(platform_paths, "_is_mount_point", lambda p: True)
    share.mkdir(parents=True)
    assert platform_paths.gvfs_mount_state(str(music)) == "empty"
    code, message = platform_paths.explain_missing_path(music)
    assert code == platform_paths.EMPTY
    assert "lists nothing" in message

    with caplog.at_level(logging.WARNING, logger="lyrion.platform.paths"):
        problems = platform_paths.warn_about_media_dirs([str(music)])
    assert problems[0][1] == platform_paths.EMPTY
    assert "Music folder is empty" in caplog.text

    # 3. Attached and readable, but the item is not in it → plain missing.
    (share / "Anderes").mkdir()
    (share / "Anderes" / "b.flac").write_bytes(b"x")
    assert platform_paths.gvfs_mount_state(str(music)) == "mounted"
    code, message = platform_paths.explain_missing_path(music)
    assert code == platform_paths.MISSING
    assert "not mounted" not in message
    assert not platform_paths.warn_about_media_dirs([str(share / "Anderes")])


def test_is_usable_dir_ignores_a_failing_stat(monkeypatch, tmp_path):
    """The scan guard lists the folder; it does not trust ``Path.is_dir()``.

    ``pathlib`` turns any ``OSError`` into ``False``, which is how a readable
    gvfs share became "Music directory unusable" while ``ls`` showed 305
    entries (Perl only needs ``-d``, ``Slim/Utils/Prefs.pm:707``).
    """
    music = tmp_path / "Musik"
    music.mkdir()
    (music / "a.flac").write_bytes(b"x")
    empty = tmp_path / "leer"
    empty.mkdir()

    assert platform_paths.is_usable_dir(music)
    assert platform_paths.is_usable_dir(empty), "an empty folder is still a folder"
    assert not platform_paths.is_usable_dir(tmp_path / "weg")
    assert not platform_paths.is_usable_dir(music / "a.flac"), "a file is no folder"

    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    assert platform_paths.is_usable_dir(music), "listing wins over a failed stat"


def test_resolve_music_dir_trusts_the_listing(monkeypatch, tmp_path):
    """A readable configured folder is usable even when ``stat`` lies.

    Measured bug: the live gvfs music folder (305 entries) was refused with
    "Music directory unusable (mount-not-mounted)" — the guard asked
    ``Path.is_dir()``, which turns a transient FUSE ``OSError`` into ``False``.
    The guard now lists the folder; Perl needs no more than ``-d``
    (``Slim/Utils/Prefs.pm:707``).
    """
    from lyrion.media import music_dir

    music = tmp_path / "Musik"
    music.mkdir()
    (music / "a.flac").write_bytes(b"x")

    class FakeConfig:
        def get(self, name, default=None):
            return str(music) if name == "musicdir" else ""

    monkeypatch.setattr("lyrion.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(Path, "is_dir", lambda self: False)

    assert music_dir.resolve_music_dir() == music


def test_missing_file_inside_a_mounted_folder_is_plainly_missing(tmp_path):
    """"file missing" must not be confused with "mount not mounted"."""
    code, message = platform_paths.explain_missing_path(tmp_path / "nope.mp3")
    assert code == platform_paths.MISSING
    assert "missing" in message
    assert "not mounted" not in message


def test_unreadable_folder_is_reported_as_not_readable(tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    folder.chmod(0o000)
    try:
        code, _ = platform_paths.explain_missing_path(folder)
    finally:
        folder.chmod(0o755)
    if os.geteuid() == 0:  # root ignores the mode
        pytest.skip("running as root: permissions are not enforced")
    assert code == platform_paths.NOT_READABLE


def test_empty_folder_is_reported_as_empty(tmp_path):
    code, message = platform_paths.explain_missing_path(tmp_path)
    assert code == platform_paths.EMPTY
    assert "empty" in message


# ---------------------------------------------------------------------------
# start-up warning for unusable music folders
# ---------------------------------------------------------------------------


def test_warn_about_media_dirs_reports_every_problem(tmp_path, caplog):
    """One warning per unusable folder — missing, empty and unmounted."""
    good = tmp_path / "good"
    good.mkdir()
    (good / "a.mp3").write_bytes(b"x")
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = tmp_path / "missing"
    gvfs = "/run/user/1000/gvfs/smb-share:server=192.168.1.90,share=musik/Musik"

    with caplog.at_level(logging.WARNING, logger="lyrion.platform.paths"):
        problems = platform_paths.warn_about_media_dirs(
            [str(good), str(empty), str(missing), gvfs])

    codes = {code for _, code, _ in problems}
    assert codes == {
        platform_paths.EMPTY,
        platform_paths.MISSING,
        platform_paths.MOUNT_NOT_MOUNTED,
    }
    assert str(good) not in {p for p, _, _ in problems}
    text = caplog.text
    assert "Music folder not mounted" in text
    assert "Music folder does not exist" in text
    assert "Music folder is empty" in text


def test_warn_about_media_dirs_without_configuration():
    problems = platform_paths.warn_about_media_dirs([])
    assert problems == []


def test_startup_check_uses_the_configured_preferences(caplog):
    """``__main__._warn_about_media_dirs`` reads mediadirs/audiodir/musicdir."""
    from lyrion import __main__ as entry

    class FakeConfig:
        values = {"mediadirs": ["/run/user/1000/gvfs/smb-share:server=h,share=s/M"],
                  "audiodir": "", "musicdir": ""}

        def get(self, name, default=None):
            return self.values.get(name, default)

    logger = logging.getLogger("lyrion")
    with caplog.at_level(logging.WARNING, logger="lyrion"):
        problems = entry._warn_about_media_dirs(FakeConfig(), logger)

    assert [code for _, code, _ in problems] == [platform_paths.MOUNT_NOT_MOUNTED]
    assert "Music folder not mounted" in caplog.text
    assert "Music folder check: 1 of 1" in caplog.text


def test_configured_media_dirs_reads_all_three_preferences():
    from lyrion import __main__ as entry

    class FakeConfig:
        values = {"mediadirs": ["/a", "/b"], "audiodir": "/a", "musicdir": "/c"}

        def get(self, name, default=None):
            return self.values.get(name, default)

    assert entry._configured_media_dirs(FakeConfig()) == ["/a", "/b", "/c"]


# ---------------------------------------------------------------------------
# case-insensitive filesystems
# ---------------------------------------------------------------------------


def test_case_insensitive_os_detection():
    assert platform_paths.fs_is_case_insensitive("win")
    assert platform_paths.fs_is_case_insensitive("mac")
    assert not platform_paths.fs_is_case_insensitive("linux")


def test_casefold_key_mirrors_perl_lc():
    """OS.pm:388-390 — noCaseFilename is lc(), not a full casefold."""
    assert platform_paths.casefold_key("Die Ärzte") == "die ärzte"
    assert platform_paths.casefold_key("ABC") == "abc"


def test_resolve_existing_case_finds_the_real_spelling(tmp_path):
    real = tmp_path / "MixedCase"
    real.mkdir()
    (real / "Track.MP3").write_bytes(b"x")

    # On a case-sensitive OS the path is returned unchanged.
    assert platform_paths.resolve_existing_case(
        tmp_path / "mixedcase", os_name="linux") == tmp_path / "mixedcase"
    # Windows/macOS resolve it to the on-disk spelling.
    assert platform_paths.resolve_existing_case(
        tmp_path / "mixedcase", os_name="win") == real
    assert platform_paths.resolve_existing_case(
        real / "track.mp3", os_name="mac") == real / "Track.MP3"
    # Unknown components stay as given (so "missing" can still be reported).
    assert platform_paths.resolve_existing_case(
        tmp_path / "nothere", os_name="win") == tmp_path / "nothere"


def test_track_id_lookup_falls_back_to_nocase(monkeypatch, tmp_path):
    """Windows/macOS: a URL differing in case must still resolve."""
    db = tmp_path / "lib.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, url TEXT)")
    con.execute("INSERT INTO tracks (id, url) VALUES (1, 'file:///m/Musik/01.mp3')")
    con.commit()

    from lyrion.media import folders

    monkeypatch.setattr(folders, "_ro_connection", lambda db_path=None: con)
    monkeypatch.setattr(platform_paths, "fs_is_case_insensitive", lambda *a, **k: True)
    assert folders._track_id_by_url("file:///m/musik/01.MP3") == 1

    # Case-sensitive OS: no fallback, exact match only.
    monkeypatch.setattr(platform_paths, "fs_is_case_insensitive", lambda *a, **k: False)
    assert folders._track_id_by_url("file:///m/musik/01.MP3") is None
    assert folders._track_id_by_url("file:///m/Musik/01.mp3") == 1
    con.close()


def test_resolve_folder_id_rejects_traversal_urls():
    from lyrion.media import folders

    assert folders.resolve_folder_id("file:///etc/../etc/passwd") is None


# ---------------------------------------------------------------------------
# Windows startability: no unguarded POSIX-only calls
# ---------------------------------------------------------------------------

#: POSIX-only API → the guard that must appear in the same function.
FORBIDDEN_CALLS = {
    "os.fork(": ('os.name != "posix"', "os.name == 'posix'", "sys.platform"),
    "os.setsid(": ('os.name != "posix"', "os.name == 'posix'", "sys.platform"),
    "os.getuid(": ('getattr(os, "getuid"', "IS_UNIX", "not IS_WINDOWS", "is_linux"),
    "os.setuid(": ('getattr(os, "setuid"', "IS_UNIX", "not IS_WINDOWS"),
    "os.killpg(": ('os.name != "posix"', "sys.platform"),
    "signal.SIGHUP": ('getattr(signal, "SIGHUP"', "hasattr(signal", "IS_WINDOWS"),
    "signal.SIGKILL": ('getattr(signal, "SIGKILL"', "hasattr(signal", "IS_WINDOWS"),
    "termios.": ("IS_WINDOWS", "os.name", "sys.platform"),
    "import pwd": ("IS_WINDOWS", "os.name", "sys.platform"),
    "import grp": ("IS_WINDOWS", "os.name", "sys.platform"),
    "pwd.getpwuid": ("IS_WINDOWS", "os.name", "sys.platform"),
    "subprocess.run([\"pkexec": ("IS_WINDOWS", "os.name", "sys.platform"),
}

#: Executable shell helpers that only exist on POSIX; a *call* to one would
#: break the Windows start.  Comments mentioning them are fine (the port's
#: documentation cites ``systemctl restart`` behaviour), so comments are
#: stripped before matching.
FORBIDDEN_SHELL = ("pkexec", "systemctl", "gio mount", "osascript")


def _code_without_comments(text: str) -> str:
    """Source text with comments and string literals removed."""
    import io
    import tokenize

    out: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(token.string)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return text
    return " ".join(out)


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_unguarded_posix_only_calls():
    """Every POSIX-only call sits behind a Windows guard (Perl's OSDetect too).

    This is the static half of the Windows requirement: on this host the Windows
    code path cannot be executed, so the guard is enforced structurally instead
    of being claimed as tested.
    """
    import warnings

    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        with warnings.catch_warnings():
            # Pre-existing docstrings elsewhere use invalid escapes ('\_');
            # they are unrelated to this check.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(text, node) or ""
            code = _code_without_comments(body)
            for needle, guards in FORBIDDEN_CALLS.items():
                if needle in code and not any(g in body for g in guards):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.name} → {needle}")
            for shell in FORBIDDEN_SHELL:
                if shell in code and not any(
                        g in body for g in ("IS_WINDOWS", "os.name", "sys.platform")):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.name} → {shell}")
    assert offenders == [], "unguarded POSIX-only calls:\n" + "\n".join(offenders)


def test_daemonize_is_a_noop_off_posix(monkeypatch, caplog):
    """--daemon on Windows must not fork: it logs and keeps running."""
    from lyrion import __main__ as entry

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(entry.os, "fork", lambda: pytest.fail("fork() called"), raising=False)
    with caplog.at_level(logging.WARNING):
        assert entry._daemonize() is True
    assert "not supported on this platform" in caplog.text


def test_no_hardcoded_linux_paths_in_normal_operation():
    """No /var, /run/user or /mnt default may leak into the normal code path.

    ``/var``/``/run`` appear only as documented Perl package locations or gvfs
    detection patterns; a bare default such as ``Path("/mnt/media/Musik")``
    must not exist any more (it would be wrong on every other OS).
    """
    banned = ('Path("/mnt/', "Path('/mnt/", 'Path("/var/lib/squeezeboxserver"',
              '= "/var/', "= '/var/")
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.relative_to(REPO_ROOT)}: {token}"


def test_scanner_config_defaults_are_resolved_not_hardcoded(monkeypatch, tmp_path):
    """ScanConfig() without a path resolves the configured music folder."""
    from lyrion.media import scanner

    music = tmp_path / "Musik"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"x")
    monkeypatch.setattr("lyrion.media.music_dir.resolve_music_dir", lambda: music)
    cfg = scanner.ScanConfig()
    assert cfg.base_path == music


def test_scan_refuses_without_a_music_folder(monkeypatch, caplog):
    """No configured folder → error, empty result, no walk of a stale path."""
    import asyncio

    from lyrion.media import scanner

    cfg = scanner.ScanConfig(base_path=None)
    cfg.base_path = None  # __post_init__ resolved it; force the unset case
    scanner_obj = scanner.MediaScanner(config=cfg)
    with caplog.at_level(logging.ERROR, logger=scanner.logger.name):
        results, stats = asyncio.run(scanner_obj.scan())
    assert results == []
    assert stats.errors == ["no music folder configured"]
    assert "No music folder to scan" in caplog.text
