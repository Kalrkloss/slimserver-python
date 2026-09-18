"""Migration: give an existing library the directory rows Perl keeps.

Why
---
Perl identifies a folder with the ``id`` of its ``tracks`` row of content type
``dir`` (``Slim/Control/Queries.pm:2429-2430``; the row is created by
``findAndScanDirectoryTree`` / the browse, ``Slim/Utils/Misc.pm:1082-1090`` and
``Slim/Control/Queries.pm:2263-2268``).  ``lyrion.media.dir_rows`` implements
that model: the scanner stores one row per walked directory and the browse
creates a missing one on demand.  A library that was scanned **before** that
change has no such rows, so its folders only get ids while they are browsed.
This migration writes them once, up front, out of the track URLs — no scan, no
restart, no filesystem walk (unless ``--walk`` is requested).

What it does (in order)
-----------------------
1. Resolve the browse roots — ``--root`` arguments, else Perl's media dirs
   (``Slim/Utils/Misc.pm:727-756`` ``getMediaDirs`` through
   ``lyrion.media.folders.effective_media_dirs``).  Without a root it stops
   instead of guessing.
2. Read every distinct track URL of the library
   (``SELECT DISTINCT url FROM tracks WHERE content_type != 'dir'``) and, for
   each, walk its ancestor directories up to the root (Perl's browse does that
   per level, ``Slim/Control/Queries.pm:2263-2268``).
3. With ``--walk``: additionally ``os.walk`` the roots, so directories that
   hold no track at all get a row too (Perl's ``readDirectory`` lists them,
   ``Slim/Utils/Misc.pm:973-1043``).  Off by default: a walk over an SMB
   library with 100k+ files takes minutes, and the next scan does it anyway.
4. Report the plan: how many rows exist, how many would be created
   (``INSERT OR IGNORE`` — the URL is UNIQUE, so re-running changes nothing)
   and how many dir rows are orphaned (their directory is gone).
5. ``--apply`` writes the missing rows in batches; ``--prune`` additionally
   deletes the orphaned rows (Perl drops all directory rows with a full wipe,
   ``Slim/Schema.pm:2346-2357`` ``wipeAllData``, and keeps stale ones through a
   normal rescan because its deleted/changed sets are filtered with
   ``content_type != 'dir'``, ``Slim/Utils/Scanner/Local.pm:186``).

Usage
-----
::

    # dry run (default): prints the plan, writes nothing
    python -m lyrion.database.migrate_dir_rows

    # the actual migration — writes ``content_type='dir'`` rows
    python -m lyrion.database.migrate_dir_rows --apply

    # also remove rows of directories that no longer exist
    python -m lyrion.database.migrate_dir_rows --apply --prune

    # explicit root / other DB (a copy is fine — see the tests)
    python -m lyrion.database.migrate_dir_rows --root /mnt/media/Musik \\
        --db /tmp/lyrion-copy.db --apply

The default is a dry run on purpose: writing into the live library DB is the
operator's decision.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Iterable, Sequence

from lyrion.media import dir_rows

#: Rows written per transaction (keeps a big library's write lock short).
BATCH_SIZE = 500


def browse_roots(explicit: Sequence[str] | None = None) -> list[str]:
    """Roots the folder browse starts from (Perl ``getMediaDirs``)."""
    if explicit:
        return [p for p in (str(x).strip() for x in explicit) if p]
    try:
        from lyrion.media.folders import effective_media_dirs

        return [str(p) for p in effective_media_dirs("audio") if str(p).strip()]
    except Exception as exc:  # noqa: BLE001 - no config / media layer
        print(f"cannot resolve the media dirs: {exc}", file=sys.stderr)
        return []


def _under_roots(path: str, roots: list[str]) -> str | None:
    """The root ``path`` lives in, or ``None``."""
    for root in roots:
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return root
    return None


def collect_directories(db_path: str | os.PathLike[str] | None,
                        roots: list[str], *, walk: bool = False
                        ) -> tuple[set[str], int]:
    """Directories the migration should carry a row for.

    Returns ``(directories, track_urls_seen)``.  Directories come from the
    ancestors of every track URL below a root (Perl's browse population) and,
    with ``walk``, from the filesystem below the roots as well (Perl's
    ``readDirectory`` tree).
    """
    dirs: set[str] = set()
    con, owned = dir_rows.connect(db_path)
    seen = 0
    try:
        if con is None:
            return (dirs, 0)
        try:
            rows = con.execute(
                "SELECT DISTINCT url FROM tracks WHERE "
                f"{dir_rows.not_dir_clause()}").fetchall()
        except sqlite3.Error as exc:
            print(f"cannot read the library: {exc}", file=sys.stderr)
            return (dirs, 0)
    finally:
        if owned and con is not None:
            con.close()

    for row in rows:
        url = str(row[0] or "")
        if not url:
            continue
        seen += 1
        path = dir_rows.dir_path(url)
        if not path:
            continue
        # A track URL denotes a *file* (``content_type='dir'`` rows are
        # excluded above), so its directory is a pure string operation — no
        # stat on every one of 80k gvfs/SMB paths.
        path = os.path.dirname(os.path.normpath(path))
        root = _under_roots(path, roots)
        while path and root and (path == root or path.startswith(root + "/")):
            dirs.add(path)
            if path == root:
                break
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent

    if walk:
        for root in roots:
            if not os.path.isdir(root):
                continue
            dirs.add(os.path.normpath(root))
            for dirpath, dirnames, _files in os.walk(root):
                for name in dirnames:
                    dirs.add(os.path.normpath(os.path.join(dirpath, name)))
    return (dirs, seen)


def plan_migration(db_path: str | os.PathLike[str] | None, *,
                   roots: Sequence[str] | None = None,
                   walk: bool = False) -> dict[str, Any]:
    """Measure what :func:`apply_migration` would do (no writes)."""
    started = time.time()
    resolved = browse_roots(roots)
    result: dict[str, Any] = {
        "db": str(dir_rows.library_db_path(db_path) or ""),
        "roots": resolved,
        "batch_size": BATCH_SIZE,
    }
    if not resolved:
        result["error"] = "no browse root could be resolved (pass --root)"
        result["seconds"] = round(time.time() - started, 2)
        return result
    if not os.path.exists(result["db"]):
        result["error"] = f"library DB not found: {result['db']}"
        result["seconds"] = round(time.time() - started, 2)
        return result

    dirs, track_urls = collect_directories(db_path, resolved, walk=walk)
    existing: dict[str, int] = {}
    orphans: dict[str, int] = {}
    not_on_disk = 0
    for root in resolved:
        for path, rid in dir_rows.iter_root_dir_rows(root,
                                                     db_path=db_path).items():
            existing[path] = rid
            if path == root or os.path.isdir(path):
                continue
            not_on_disk += 1
            if walk:
                # Only a real filesystem walk proves that a missing directory
                # is gone rather than on an unmounted share — without it the
                # rows are reported, never pruned.
                orphans[path] = rid
    result.update({
        "track_urls": track_urls,
        "directories": len(dirs),
        "existing_dir_rows": len(existing),
        "to_create": len([p for p in dirs if p not in existing]),
        "orphan_rows": len(orphans),
        "batches": (len([p for p in dirs if p not in existing])
                    + BATCH_SIZE - 1) // BATCH_SIZE,
    })
    if not_on_disk:
        result["existing_but_no_dir_on_disk"] = not_on_disk
    result["seconds"] = round(time.time() - started, 2)
    return result


def apply_migration(db_path: str | os.PathLike[str] | None, *,
                    roots: Sequence[str] | None = None,
                    walk: bool = False, prune: bool = False) -> dict[str, Any]:
    """Write the missing directory rows (and prune orphans with ``prune``).

    ``prune`` requires ``walk``: without a filesystem walk the set of known
    directories comes from the track URLs only, so a directory that simply
    holds no track would look like an orphan and be deleted.  A missing
    directory is never pruned just because a share is unmounted.
    """
    started = time.time()
    resolved = browse_roots(roots)
    report: dict[str, Any] = {
        "db": str(dir_rows.library_db_path(db_path) or ""),
        "roots": resolved, "batch_size": BATCH_SIZE, "prune": prune,
    }
    if not resolved:
        report["error"] = "no browse root could be resolved (pass --root)"
        return report
    if prune and not walk:
        report["error"] = ("--prune needs --walk (without a filesystem walk a "
                           "folder without tracks would look orphaned)")
        return report
    dirs, track_urls = collect_directories(db_path, resolved, walk=walk)
    report["track_urls"] = track_urls
    report["directories"] = len(dirs)

    con, owned = dir_rows.connect(db_path, write=True)
    if con is None:
        report["error"] = (f"library DB is not writable: "
                           f"{dir_rows.library_db_path(db_path)}")
        return report
    created = 0
    try:
        def _row_count() -> int:
            try:
                return int(con.execute(
                    "SELECT COUNT(*) FROM tracks WHERE content_type = 'dir'"
                ).fetchone()[0])
            except sqlite3.Error:  # pragma: no cover - defensive
                return 0

        before = _row_count()
        for root in resolved:
            # ``root`` itself: Perl stores the browsed root row too
            # (Slim/Utils/Misc.pm:1082-1090).
            dirs.add(os.path.normpath(root))
        ordered = sorted(dirs)
        for i in range(0, len(ordered), BATCH_SIZE):
            batch = ordered[i:i + BATCH_SIZE]
            dir_rows.ensure_dir_ids(batch, conn=con)
            print(f"  {min(i + BATCH_SIZE, len(ordered))}/{len(ordered)} "
                  f"directories", flush=True)
        # ``ensure_dir_ids`` returns the id of *every* directory it was given
        # (existing or new); the migration reports what it actually created.
        created = max(_row_count() - before, 0)
        report["stored"] = created
        if prune:
            pruned = 0
            for root in resolved:
                pruned += dir_rows.prune_orphan_dir_rows(root, dirs, conn=con)
            report["pruned"] = pruned
    finally:
        if owned:
            con.close()
    report["seconds"] = round(time.time() - started, 2)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lyrion.database.migrate_dir_rows",
        description="Store Perl's ``content_type='dir'`` rows for an existing "
                    "library (dry run by default).")
    parser.add_argument("--apply", action="store_true",
                        help="actually write the rows (default: dry run)")
    parser.add_argument("--prune", action="store_true",
                        help="also delete directory rows whose directory is "
                             "gone (only with --apply)")
    parser.add_argument("--walk", action="store_true",
                        help="also walk the roots on disk (slow on SMB/NFS; "
                             "covers folders without any track)")
    parser.add_argument("--root", action="append", default=None,
                        help="browse root (repeatable); default: the media "
                             "dirs / scanner roots")
    parser.add_argument("--db", default=None,
                        help="library DB path (default: the configured one)")
    args = parser.parse_args(argv)

    db = args.db
    if not args.apply:
        report = plan_migration(db, roots=args.root, walk=args.walk)
        _print_report(report, dry_run=True)
        if report.get("error"):
            return 1
        print("\nDry run — nothing was written. Re-run with --apply (and "
              "optionally --prune) to migrate.")
        return 0
    report = apply_migration(db, roots=args.root, walk=args.walk,
                             prune=args.prune)
    _print_report(report, dry_run=False)
    return 1 if report.get("error") else 0


def _print_report(report: dict[str, Any], *, dry_run: bool) -> None:
    print(f"library DB : {report.get('db', dir_rows.library_db_path())}")
    print(f"roots      : {', '.join(report.get('roots') or []) or '-'}")
    if report.get("error"):
        print(f"ERROR      : {report['error']}")
        return
    print(f"track URLs : {report.get('track_urls', 0)}")
    print(f"directories: {report.get('directories', 0)} "
          f"({report.get('existing_dir_rows', 0)} already have a dir row)")
    if dry_run:
        print(f"to create  : {report.get('to_create', 0)} "
              f"(= {report.get('batches', 0)} batch(es) of "
              f"{report.get('batch_size')})")
        print(f"orphans    : {report.get('orphan_rows', 0)} "
              f"directory rows whose folder is gone (--prune)")
        if report.get("existing_but_no_dir_on_disk"):
            print(f"  note     : {report['existing_but_no_dir_on_disk']} "
                  f"directories are not on disk right now (unmounted share?) "
                  f"— they are NOT counted as orphans")
    else:
        print(f"stored     : {report.get('stored', 0)} directory rows")
        if report.get("prune"):
            print(f"pruned     : {report.get('pruned', 0)} orphaned rows")
    print(f"measured   : {report.get('seconds', 0)} s")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
