"""Directory rows in ``tracks`` — Perl's folder-id model.

Perl does not invent folder ids: a directory is a **row in ``tracks``** with
``content_type = 'dir'``, and the ``id`` a client gets for a folder is that
row's ``tracks.id``.

Perl reference (read-only checkout ``/tmp/lms-ref``, live 9.1.1 probed
read-only on 192.168.1.90:9000)

* ``Slim/Music/Info.pm:1482-1484`` — the content type of a directory::

      # sanity check for folders
      if ($filepath && -d $filepath) {
          $type = 'dir';
      }

* ``Slim/Schema.pm:764-856`` ``objectForUrl`` — the row is created on demand
  (``create => 1`` → ``updateOrCreate``); ``Slim/Schema.pm:1774`` makes a
  directory take the *simple* create path, so no album/contributor links and
  no ``tracks_persistent`` row are built for it::

      if (($playlist && $columnValueHash{extid}) || !defined $ct || $ct eq 'dir'
          || $ct eq 'lnk' || !$columnValueHash{'audio'}) {

* ``Slim/Control/Queries.pm:2263-2268`` — browsing creates the row of every
  displayed child (the first pass only *checks* existence, :2249-2257, "don't
  create the dir objects in the first pass - we can create them later when
  paging through the list"), and ``:2429-2430`` hands out ``$item->id()``::

      $item = $bmfCache{$url} || Slim::Schema->objectForUrl({
          'url'      => $url,
          'create'   => 1,
          'readTags' => 1,
          'playlist' => Slim::Music::Info::isPlaylist($url),
      }) if $url;
      ...
      $request->addResultLoop($loopname, $chunkCount, 'id', $id);
      $request->addResultLoop($loopname, $chunkCount, 'filename', $realName);

* ``Slim/Utils/Misc.pm:1060-1104`` ``findAndScanDirectoryTree`` — the browsed
  directory itself becomes the ``topLevelObj`` row (``objectForUrl({url,
  create => 1, readTags => 1, commit => 1})``) and its mtime is stored
  (``:1131-1136`` ``$topLevelObj->timestamp($fsMTime); $topLevelObj->update;``).
* ``Slim/Control/Queries.pm:2311-2316`` — a ``folder_id`` is resolved back
  through that row (``$params->{'id'} = $folderId`` →
  ``Slim::Schema->find('Track', $params->{'id'})``).
* ``Slim/Utils/Scanner/Local/AIO.pm:62-67`` and ``:135-142`` — the *scan*
  records every directory it walks (``filesize`` 0: "size, 0 for dirs").
* ``Slim/Utils/Scanner/Local.pm:186`` — the rescan's deleted/changed sets are
  built with ``content_type != 'dir'``, i.e. directory rows are never treated
  as audio files; a full wipe drops them all
  (``Slim/Schema.pm:2346-2357`` ``wipeAllData`` → ``wipeDB``).
* ``Slim/Control/Queries.pm:4843`` — the songs/titles query ignores them::

      my $where = '(tracks.audio = 1 AND tracks.content_type NOT IN ("cpl",
                   "src", "ssp", "dir") ';

  and ``Slim/Schema.pm:2186-2190`` counts total *time* over ``tracks.audio =
  1`` only.

Live 9.1.1 (read-only 2026-09-18), ``["musicfolder","0","5","tags:stuo"]``::

    {"id": 204572, "filename": "Accept", "type": "folder",
     "url": "file:///mnt/media/Musik/Accept", "title": "Accept", "ct": "dir"}

What our port stores
--------------------
The row is written with the Perl values the schema can hold: ``url`` (the
percent-encoded ``file://`` URL the importer stores), ``content_type='dir'``,
``filesize=0``, ``audio=0``, ``modtime`` = the directory's mtime.  ``title``
and ``titlesort`` stay **empty**: the name a client shows for a folder is
``filename``, which Perl computes from the URL
(``Slim/Music/Info.pm:919-943`` ``fileName``), never from ``tracks.title`` —
and the port has title searches outside this change's file scope
(``control/*``) that must not start matching directories, which is what a
stored title would do.  Perl's own ``titles`` query filters ``dir`` rows out
anyway (``Queries.pm:4843``).

Case handling (Windows/macOS) and gvfs paths come from
:mod:`lyrion.platform.paths`: ``file_url_from_path`` escapes exactly like the
importer's ``Path.as_uri()`` (``smb-share%3Aserver%3Dx%2Cshare%3Dy``), a
lookup retries with ``COLLATE NOCASE`` on a case-insensitive filesystem, and
SQLite's ``LIKE`` already folds ASCII case.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import urllib.parse
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Perl's ``content_type`` of a directory (``Slim/Music/Info.pm:1482-1484``).
DIR_CONTENT_TYPE = "dir"

#: Perl's exclusion list of the songs/titles query
#: (``Slim/Control/Queries.pm:4843``).  Our ``content_type`` holds MIME types
#: for files, so only ``dir`` can ever match — the constant is kept complete
#: because it documents the Perl rule.
DIR_EXCLUDED_CONTENT_TYPES: tuple[str, ...] = ("cpl", "src", "ssp", "dir")


# ---------------------------------------------------------------------------
# SQL fragments (Perl's own filters)
# ---------------------------------------------------------------------------

def not_dir_clause(alias: str = "") -> str:
    """``content_type != 'dir'`` — a directory row is not a track.

    Perl uses the literal ``content_type != 'dir'`` in the rescan sets
    (``Slim/Utils/Scanner/Local.pm:186``, ``:240``, ``:282``) and
    ``Slim/Schema/ResultSet/Track.pm:85``.
    """
    col = f"{alias}.content_type" if alias else "content_type"
    return f"({col} IS NULL OR {col} != '{DIR_CONTENT_TYPE}')"


def songs_clause(alias: str = "tracks") -> str:
    """Perl's base WHERE of ``titlesQuery`` — ``Queries.pm:4843``.

    The Perl clause is ``tracks.audio = 1 AND tracks.content_type NOT IN
    ("cpl", "src", "ssp", "dir")``; the ``dir`` member is NULL-safe here
    because our ``content_type`` holds MIME types and a row written before the
    importer set one carries NULL — a Perl row never does, and
    ``NULL NOT IN (…)`` would silently drop those tracks.
    """
    quoted = ", ".join(f'"{t}"' for t in DIR_EXCLUDED_CONTENT_TYPES)
    return (f"{alias}.audio = 1 AND ({alias}.content_type IS NULL OR "
            f"{alias}.content_type NOT IN ({quoted}))")


def duration_clause(alias: str = "tracks") -> str:
    """Perl's ``totalTime`` — ``Slim/Schema.pm:2186-2190`` (``audio = 1``)."""
    return f"{alias}.audio = 1"


# ---------------------------------------------------------------------------
# Path ↔ URL, display name
# ---------------------------------------------------------------------------

def dir_url(path: str | os.PathLike[str]) -> str:
    """``file://`` URL of a directory — Perl ``fileURLFromPath`` (Misc.pm:307-351)."""
    text = str(path)
    if text.lower().startswith(("file://", "tmp://", "http://", "https://")):
        return text
    from lyrion.platform import paths as platform_paths

    return platform_paths.file_url_from_path(text)


def dir_path(url: str | os.PathLike[str]) -> str:
    """Directory path of a ``file://`` URL — Perl ``pathFromFileURL``."""
    text = str(url)
    if not text.lower().startswith(("file://", "tmp://")):
        return text
    from lyrion.platform import paths as platform_paths

    return platform_paths.path_from_file_url(text)


def display_name(path: str | os.PathLike[str]) -> str:
    """Perl ``plainTitle($path, 'dir')`` — ``Slim/Music/Info.pm:669-692``.

    ``fileName`` = last path component (``Info.pm:919-943``); a directory is
    exempt from the suffix stripping (``:686``) and underscores become
    spaces (``:689-691``).
    """
    text = str(path).replace("\\", "/")
    if text.lower().startswith("file://"):
        text = dir_path(text)
    name = os.path.basename(text.rstrip("/")) or text
    return name.replace("_", " ")


def is_dir_row(row: Any) -> bool:
    """True when a ``tracks`` row is a directory row (Perl ``isDir``)."""
    if row is None:
        return False
    try:
        value = row["content_type"]
    except (TypeError, KeyError, IndexError):
        try:
            value = row.get("content_type")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - tuple row without the column
            return False
    return str(value or "") == DIR_CONTENT_TYPE


# ---------------------------------------------------------------------------
# The row values Perl writes (Slim/Schema.pm:1774 → _createTrack)
# ---------------------------------------------------------------------------

#: Columns + values of a directory row.  ``Slim/Schema.pm:1774`` (``$ct eq
#: 'dir'`` → simple ``_createTrack`` path, no links), ``Slim/Utils/Scanner/
#: Local/AIO.pm:67`` (``filesize`` 0, "size, 0 for dirs"), ``audio`` 0 (the
#: ``!$columnValueHash{'audio'}`` arm of the same check).
_DIR_ROW_VALUES: dict[str, Any] = {
    "content_type": DIR_CONTENT_TYPE,
    "title": "",
    "titlesort": "",
    "filesize": 0,
    "audio": 0,
    "video": 0,
    "remote": 0,
    "disabled": 0,
    "compilation": 0,
    "artflow_flag": 0,
    "duration": 0.0,
    "playcount": 0,
}


def _row_values(directory: str) -> dict[str, Any]:
    path = _as_path(directory)
    values = dict(_DIR_ROW_VALUES)
    values["url"] = dir_url(path)
    try:
        values["modtime"] = int(os.stat(path).st_mtime)
    except OSError:
        values["modtime"] = 0
    return values


def _as_path(value: str | os.PathLike[str]) -> str:
    """Absolute directory path of a path or URL token ('' when unusable)."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith(("file://", "tmp://")):
        text = dir_path(text)
    return os.path.normpath(text)


# ---------------------------------------------------------------------------
# DB connections
# ---------------------------------------------------------------------------

def config_db_path() -> str | None:
    """Library DB path from the active config (the web layer's own source)."""
    try:
        from lyrion.config import get_config

        path = str(get_config().db_path)
    except Exception:  # noqa: BLE001 - no config yet (import time, tests)
        return None
    return path or None


def library_db_path(explicit: str | os.PathLike[str] | None = None) -> str | None:
    """Library DB path — an explicit path wins, otherwise the configured one."""
    if explicit:
        return str(explicit)
    return config_db_path()


def _open(db_path: str | os.PathLike[str] | None = None, *,
          conn: sqlite3.Connection | None = None,
          write: bool = False) -> tuple[sqlite3.Connection | None, bool]:
    """Return ``(connection, owned)``; ``(None, False)`` when unavailable."""
    if conn is not None:
        return conn, False
    path = library_db_path(db_path)
    if not path or not os.path.exists(path):
        return None, False
    try:
        if write:
            con = sqlite3.connect(str(path), timeout=30)
            con.execute("PRAGMA busy_timeout=30000")
        else:
            uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        logger.warning("dir rows: cannot open library DB %s: %s", path, exc)
        return None, False
    return con, True


def connect(db_path: str | os.PathLike[str] | None = None, *,
            write: bool = False) -> tuple[sqlite3.Connection | None, bool]:
    """Open the library DB — returns ``(connection, owned)``.

    Public wrapper around :func:`_open` for callers that run many row
    operations on one handle (the migration), so they do not pay a connection
    per batch.  ``owned`` tells the caller whether it must close it.
    """
    return _open(db_path, write=write)


def _table_columns(con: sqlite3.Connection) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute("PRAGMA table_info(tracks)")}
    except sqlite3.Error:
        return set()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def _select_id(con: sqlite3.Connection, url: str) -> int | None:
    """``tracks.id`` of the row for ``url``, exact then case-folded.

    Perl's ``_retrieveTrack`` looks the URL up after canonicalisation
    (``Slim/Schema.pm:827-829``); on Windows/macOS a differently-cased path
    denotes the same object, so a ``COLLATE NOCASE`` retry follows — the same
    rule ``folders._track_id_by_url`` uses (``Slim/Utils/OS.pm:388-390``).
    """
    try:
        row = con.execute("SELECT id FROM tracks WHERE url = ? LIMIT 1",
                          (url,)).fetchone()
        if row is None or row[0] is None:
            from lyrion.platform import paths as platform_paths

            if platform_paths.fs_is_case_insensitive():
                row = con.execute(
                    "SELECT id FROM tracks WHERE url = ? COLLATE NOCASE LIMIT 1",
                    (url,)).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


def lookup_dir_id(directory: str | os.PathLike[str] = "", *,
                  db_path: str | os.PathLike[str] | None = None,
                  conn: sqlite3.Connection | None = None) -> int | None:
    """Row id already stored for a directory (no write)."""
    path = _as_path(directory)
    if not path:
        return None
    con, owned = _open(db_path, conn=conn)
    if con is None:
        return None
    try:
        return _select_id(con, dir_url(path))
    finally:
        if owned:
            con.close()


def dir_row_by_id(track_id: object, *,
                  db_path: str | os.PathLike[str] | None = None,
                  conn: sqlite3.Connection | None = None) -> dict | None:
    """``{"id", "url", "path", "content_type"}`` of a numeric ``folder_id``.

    Perl: ``$params->{'id'} = $folderId`` → ``Slim::Schema->find('Track',
    $params->{'id'})`` → ``$topLevelObj->path``
    (``Slim/Control/Queries.pm:2311-2316``, ``Slim/Utils/Misc.pm:1067-1074``).
    The lookup is by id only — Perl does not check the content type here, so
    neither do we; the caller decides what to do with a non-directory row.
    """
    try:
        wanted = int(str(track_id).strip())
    except (TypeError, ValueError):
        return None
    if wanted < 0:
        return None
    con, owned = _open(db_path, conn=conn)
    if con is None:
        return None
    try:
        row = con.execute("SELECT url, content_type FROM tracks WHERE id = ?"
                          " LIMIT 1", (wanted,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if owned:
            con.close()
    if not row or not row[0]:
        return None
    text = str(row[0])
    if not text.lower().startswith(("file://", "tmp://")):
        return None
    path = dir_path(text)
    if not path:
        return None
    return {"id": wanted, "url": text, "path": os.path.normpath(path),
            "content_type": str(row[1] or "")}


def dir_path_by_id(track_id: object, *,
                   db_path: str | os.PathLike[str] | None = None,
                   conn: sqlite3.Connection | None = None) -> str | None:
    """Directory path behind a numeric ``folder_id`` (see :func:`dir_row_by_id`)."""
    row = dir_row_by_id(track_id, db_path=db_path, conn=conn)
    return str(row["path"]) if row else None


# ---------------------------------------------------------------------------
# Create / update (Perl objectForUrl create => 1)
# ---------------------------------------------------------------------------

def ensure_dir_row(directory: str | os.PathLike[str] = "", *,
                   db_path: str | os.PathLike[str] | None = None,
                   conn: sqlite3.Connection | None = None,
                   commit: bool = True) -> int | None:
    """Perl ``objectForUrl({url, create => 1})`` for a directory.

    Returns the row id, creating the row when it is missing and refreshing
    ``modtime`` when the directory's mtime changed
    (``Slim/Utils/Misc.pm:1131-1136``).  ``None`` = no row could be
    established (no DB, schema without ``content_type``, read-only DB) — the
    caller then keeps its non-Perlish fallback token instead of failing the
    browse.
    """
    path = _as_path(directory)
    if not path:
        return None
    url = dir_url(path)
    con, owned = _open(db_path, conn=conn, write=True)
    if con is None:
        return None
    try:
        existing = _select_id(con, url)
        values = _row_values(path)
        columns = _table_columns(con)
        if "content_type" not in columns:
            # A tracks table without the marker cannot hold a Perl dir row;
            # do not pretend it did.
            return existing
        if existing is not None:
            if "modtime" in columns:
                try:
                    con.execute(
                        "UPDATE tracks SET modtime = ? WHERE id = ? AND "
                        "(modtime IS NULL OR modtime != ?)",
                        (values["modtime"], existing, values["modtime"]))
                    if commit:
                        con.commit()
                except sqlite3.Error:  # pragma: no cover - defensive
                    pass
            return existing
        usable = {k: v for k, v in values.items() if k in columns}
        if "url" not in usable or "titlesort" not in usable:
            return None
        names = ", ".join(usable)
        marks = ", ".join("?" * len(usable))
        # OR IGNORE: a concurrent rescan may have inserted the same URL.
        cur = con.execute(f"INSERT OR IGNORE INTO tracks ({names}) "
                          f"VALUES ({marks})", tuple(usable.values()))
        if commit:
            con.commit()
        if cur.rowcount:
            return int(cur.lastrowid) if cur.lastrowid is not None else \
                _select_id(con, url)
        return _select_id(con, url)
    except sqlite3.Error as exc:
        logger.warning("dir rows: cannot store '%s': %s", path, exc)
        return None
    finally:
        if owned:
            con.close()


def ensure_dir_ids(directories: Iterable[str | os.PathLike[str]], *,
                   db_path: str | os.PathLike[str] | None = None,
                   conn: sqlite3.Connection | None = None
                   ) -> dict[str, int]:
    """``{absolute directory: tracks.id}`` for many folders in one connection.

    Missing rows are created like :func:`ensure_dir_row`.  Directories that
    could not be stored are absent from the result.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for value in directories:
        path = _as_path(value)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    if not paths:
        return {}
    con, owned = _open(db_path, conn=conn, write=True)
    if con is None:
        return {}
    out: dict[str, int] = {}
    try:
        columns = _table_columns(con)
        if "content_type" not in columns:
            return {}
        urls = {path: dir_url(path) for path in paths}
        marks = ", ".join("?" * len(urls))
        found: dict[str, int] = {}
        try:
            for row in con.execute(
                    f"SELECT id, url FROM tracks WHERE url IN ({marks})",
                    tuple(urls.values())):
                found[str(row[1])] = int(row[0])
        except sqlite3.Error:
            found = {}
        missing: list[str] = []
        for path, url in urls.items():
            hit = found.get(url)
            if hit is None:
                missing.append(path)
            else:
                out[path] = hit
        for path in missing:
            values = {k: v for k, v in _row_values(path).items()
                      if k in columns}
            if "url" not in values or "titlesort" not in values:
                continue
            names = ", ".join(values)
            marks2 = ", ".join("?" * len(values))
            try:
                cur = con.execute(
                    f"INSERT OR IGNORE INTO tracks ({names}) VALUES ({marks2})",
                    tuple(values.values()))
            except sqlite3.Error as exc:
                logger.warning("dir rows: cannot store '%s': %s", path, exc)
                continue
            if cur.rowcount:
                out[path] = (int(cur.lastrowid) if cur.lastrowid is not None
                             else (_select_id(con, urls[path]) or 0))
                if not out[path]:
                    out.pop(path, None)
            else:
                hit2 = _select_id(con, urls[path])
                if hit2 is not None:
                    out[path] = hit2
        if out:
            con.commit()
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        logger.warning("dir rows: batch store failed: %s", exc)
    finally:
        if owned:
            con.close()
    return out


# ---------------------------------------------------------------------------
# Reading the tree below a root
# ---------------------------------------------------------------------------

def child_dir_rows(directory: str | os.PathLike[str], *,
                   db_path: str | os.PathLike[str] | None = None,
                   conn: sqlite3.Connection | None = None
                   ) -> list[tuple[int, str]]:
    """``[(id, path)]`` of the directory rows *directly* inside ``directory``.

    The row of a subdirectory carries its own URL, so a directory that holds
    no track at all is still visible in the browse — exactly what Perl's
    ``readDirectory`` (``Slim/Utils/Misc.pm:973-1043``) returns for it.
    """
    path = _as_path(directory)
    if not path:
        return []
    prefix = dir_url(path).rstrip("/") + "/"
    like = prefix + "%"
    off = len(prefix) + 1             # 1-based index behind "<dir>/"
    con, owned = _open(db_path, conn=conn)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT id, url FROM tracks WHERE content_type = 'dir'"
            " AND url LIKE ? AND instr(substr(url, ?), '/') = 0",
            (like, off)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if owned:
            con.close()
    out: list[tuple[int, str]] = []
    for row in rows:
        child = dir_path(str(row[1]))
        if child and child != path and child.startswith(path + os.sep):
            out.append((int(row[0]), os.path.normpath(child)))
    return out


def iter_root_dir_rows(root: str | os.PathLike[str], *,
                       db_path: str | os.PathLike[str] | None = None,
                       conn: sqlite3.Connection | None = None
                       ) -> dict[str, int]:
    """``{path: id}`` of every directory row at or below ``root``."""
    path = _as_path(root)
    if not path:
        return {}
    url = dir_url(path)
    like = url.rstrip("/") + "/%"
    con, owned = _open(db_path, conn=conn)
    if con is None:
        return {}
    try:
        rows = con.execute(
            "SELECT id, url FROM tracks WHERE content_type = 'dir'"
            " AND (url = ? OR url LIKE ?)", (url, like)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if owned:
            con.close()
    out: dict[str, int] = {}
    for row in rows:
        child = dir_path(str(row[1]))
        if child:
            out[os.path.normpath(child)] = int(row[0])
    return out


def _fold(path: str) -> str:
    """Comparison key of a path: case-folded on a case-insensitive FS."""
    try:
        from lyrion.platform import paths as platform_paths

        if platform_paths.fs_is_case_insensitive():
            return path.casefold()
    except Exception:  # noqa: BLE001 - platform layer optional here
        pass
    return path


def prune_orphan_dir_rows(root: str | os.PathLike[str],
                          keep: Iterable[str],
                          *,
                          db_path: str | os.PathLike[str] | None = None,
                          conn: sqlite3.Connection | None = None) -> int:
    """Delete directory rows below ``root`` whose directory is gone.

    Perl removes every directory row with the library on a full wipe
    (``Slim/Schema.pm:2346-2357`` ``wipeAllData`` → ``wipeDB``) and *keeps*
    stale ones through a normal rescan, because the rescan's deleted/changed
    sets are filtered with ``content_type != 'dir'``
    (``Slim/Utils/Scanner/Local.pm:186``).  This port creates directory rows
    during the scan, so it also owns their cleanup: rows below a scanned root
    whose path is not among the walked directories are dropped.  Returns the
    number of deleted rows.
    """
    path = _as_path(root)
    if not path:
        return 0
    stored = iter_root_dir_rows(root, db_path=db_path, conn=conn)
    if not stored:
        return 0
    keep_keys = {_fold(_as_path(p)) for p in keep if str(p).strip()}
    victims = [p for p in stored if _fold(p) not in keep_keys]
    if not victims:
        return 0
    con, owned = _open(db_path, conn=conn, write=True)
    if con is None:
        return 0
    removed = 0
    try:
        for victim in victims:
            rid = stored[victim]
            if rid is None:
                continue
            for table in ("tracks_albums", "tracks_contributors",
                          "tracks_genres"):
                try:
                    con.execute(f"DELETE FROM {table} WHERE track = ?", (rid,))
                except sqlite3.Error:  # pragma: no cover - table optional
                    pass
            try:
                con.execute("DELETE FROM tracks WHERE id = ?", (rid,))
                removed += 1
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
        con.commit()
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        logger.warning("dir rows: prune failed below %s: %s", path, exc)
    finally:
        if owned:
            con.close()
    if removed:
        logger.info("Removed %d orphaned directory row(s) below %s",
                    removed, path)
    return removed


# ---------------------------------------------------------------------------
# Scan-time sync (used by the importer — same session as the rest of the scan)
# ---------------------------------------------------------------------------

async def sync_dir_rows(session, directories: Iterable[str | os.PathLike[str]],
                        *, root: str | os.PathLike[str] | None = None,
                        prune: bool = False) -> tuple[int, int]:
    """Store the directories a scan walked; optionally drop orphaned rows.

    Perl records every walked directory during the scan with ``filesize`` 0
    (``Slim/Utils/Scanner/Local/AIO.pm:62-67``, ``:135-142``) and turns them
    into ``tracks`` rows on demand; this port stores the row right away, so a
    folder id is a real ``tracks.id`` before the first browse.  Returns
    ``(created, pruned)``.  Runs on the *importer's* SQLAlchemy session so the
    rows land in exactly the DB the scan writes to.
    """
    from sqlalchemy import text

    paths: list[str] = []
    seen: set[str] = set()
    for value in directories:
        path = _as_path(value)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    if not paths:
        return (0, 0)

    columns = set()
    try:
        result = await session.execute(text("PRAGMA table_info(tracks)"))
        columns = {str(row[1]) for row in result.fetchall()}
    except Exception as exc:  # noqa: BLE001 - lean/foreign schema
        logger.warning("dir rows: cannot inspect the tracks schema: %s", exc)
        return (0, 0)
    if "content_type" not in columns:
        return (0, 0)

    urls = {path: dir_url(path) for path in paths}
    found: dict[str, int] = {}
    try:
        result = await session.execute(
            text("SELECT id, url FROM tracks WHERE content_type = 'dir'"))
        found = {str(row[1]): int(row[0]) for row in result.fetchall()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("dir rows: cannot read existing rows: %s", exc)

    created = 0
    for path, url in urls.items():
        if url in found:
            continue
        values = {k: v for k, v in _row_values(path).items() if k in columns}
        if "url" not in values or "titlesort" not in values:
            continue
        names = ", ".join(values)
        marks = ", ".join(f":v{n}" for n in range(len(values)))
        params = {f"v{n}": v for n, v in enumerate(values.values())}
        try:
            await session.execute(
                text(f"INSERT OR IGNORE INTO tracks ({names}) VALUES ({marks})"),
                params)
            created += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop it
            logger.warning("dir rows: cannot store '%s': %s", path, exc)
    pruned = 0
    if prune and root:
        pruned = await _prune_orphans_session(session, root, urls.values())
    return (created, pruned)


async def _prune_orphans_session(session, root: str | os.PathLike[str],
                                 keep_urls: Iterable[str]) -> int:
    """Session-side variant of :func:`prune_orphan_dir_rows`."""
    from sqlalchemy import text

    path = _as_path(root)
    if not path:
        return 0
    url = dir_url(path)
    like = url.rstrip("/") + "/%"
    try:
        result = await session.execute(
            text("SELECT id, url FROM tracks WHERE content_type = 'dir'"
                 " AND (url = :p0 OR url LIKE :p1)"), {"p0": url, "p1": like})
        rows = result.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dir rows: cannot list rows below %s: %s", path, exc)
        return 0
    keep_keys = {_fold(_as_path(u)) for u in keep_urls}
    removed = 0
    for row in rows:
        rid, stored_url = int(row[0]), str(row[1])
        if _fold(_as_path(stored_url)) in keep_keys:
            continue
        try:
            await session.execute(
                text("DELETE FROM tracks WHERE id = :id"), {"id": rid})
            removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("dir rows: cannot delete id %s: %s", rid, exc)
    return removed
