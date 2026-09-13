#!/usr/bin/env python3
"""Entfernt den veralteten UNIQUE(titlesort, year)-Index auf ``albums``.

Hintergrund: Die Live-DB stammt aus einer früheren Schema-Version, in der die
Album-Tabelle ein Tabellen-Constraint ``UNIQUE (titlesort, year)`` hatte (in
SQLite als ``sqlite_autoindex_albums_1`` sichtbar). Das aktuelle Schema
identifiziert Alben über ``(titlesort, albumartist_sort)``
(``uq_album_titlesort_artist``, schema.py:268) — der Altindex widerspricht dem
und verbietet Alben mit gleichem Titel+Jahr und verschiedenen Künstlern.

SQLite kann einen Tabellen-Constraint nicht per ALTER entfernen, deshalb wird
die Tabelle nach dem offiziellen 12-Schritte-Verfahren neu aufgebaut:
neue Tabelle aus der gespeicherten DDL ohne die UNIQUE-Klausel, Daten kopieren,
alte Tabelle löschen, umbenennen, Indizes neu anlegen, prüfen.

Sicherheit: Backup vor jedem Schreibzugriff, Dry-Run als Default,
``PRAGMA foreign_key_check`` + ``integrity_check`` + Zeilen-/Inhaltsvergleich
danach. Nur mit ``--apply`` wird geschrieben.

Aufruf:
    python tools/_drop_stale_album_unique.py --db <pfad> [--apply]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

TARGET_INDEX_COLS = ("titlesort", "year")

# Indizes, die auf albums existieren sollen (unsere Schema-Definition,
# schema.py:269-270 + die von SQLAlchemy erzeugten Einzelspalten-Indizes).
DESIRED_INDEXES = [
    ("idx_album_year", "CREATE INDEX IF NOT EXISTS idx_album_year ON albums (year)"),
    ("idx_album_musicbrainz", "CREATE INDEX IF NOT EXISTS idx_album_musicbrainz ON albums (musicbrainz_id)"),
    ("idx_album_compilation", "CREATE INDEX IF NOT EXISTS idx_album_compilation ON albums (compilation)"),
]


def table_ddl(con: sqlite3.Connection, table: str) -> str:
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone()
    if not row or not row[0]:
        raise SystemExit(f"Tabelle {table} nicht gefunden")
    return row[0]


def unique_constraints(ddl: str) -> list[tuple[str, tuple[str, ...]]]:
    """Findet Tabellen-Constraints ``UNIQUE (a, b)`` in der DDL."""
    out = []
    for m in re.finditer(r"UNIQUE\s*\(([^)]*)\)", ddl, re.IGNORECASE):
        cols = tuple(c.strip().strip('"').strip("`") for c in m.group(1).split(","))
        out.append((m.group(0), cols))
    return out


def index_list(con: sqlite3.Connection, table: str) -> list[tuple[str, int, tuple[str, ...]]]:
    res = []
    for row in con.execute(f"PRAGMA index_list({table})"):
        name, unique = row[1], row[2]
        cols = tuple(r[2] for r in con.execute(f"PRAGMA index_info({name})"))
        res.append((name, unique, cols))
    return res


def counts(con: sqlite3.Connection) -> dict:
    q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
    return {
        "albums": q("SELECT COUNT(*) FROM albums"),
        "sum_id": q("SELECT COALESCE(SUM(id),0) FROM albums"),
        "notnull_titlesort": q("SELECT COUNT(*) FROM albums WHERE titlesort IS NOT NULL"),
        "links": q("SELECT COUNT(*) FROM tracks_albums"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="wirklich schreiben (sonst nur Dry-Run)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"{db} existiert nicht")

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        ddl = table_ddl(con, "albums")
        ucs = unique_constraints(ddl)
        stale = [c for c, cols in ucs if cols == TARGET_INDEX_COLS]
        idx = index_list(con, "albums")
        auto = [i for i in idx if i[0].startswith("sqlite_autoindex") and i[2] == TARGET_INDEX_COLS]
        print("Gefundene UNIQUE-Constraints:", ucs)
        print("Alter Index (titlesort, year):", auto or "— nicht vorhanden")
        if not stale and not auto:
            print("Nichts zu tun: der Altindex existiert nicht mehr.")
            return 0
        before = counts(con)
        print("Zeilen vorher:", before)

        new_ddl = ddl
        for clause in stale:
            new_ddl = new_ddl.replace(clause, "")
        new_ddl = new_ddl.replace("TABLE albums", "TABLE albums_new", 1)
        # zusätzliche Kommas/Leerraum aufräumen
        new_ddl = re.sub(r",\s*\)", ")", new_ddl)
        print("Neue DDL:", new_ddl[:400].replace("\n", " "), "…")

        if not args.apply:
            print("\nDRY-RUN — nichts geschrieben. Mit --apply ausführen.")
            return 0

        backup = db.with_suffix(db.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(db, backup)
        print("Backup:", backup)

        cols = [r[1] for r in con.execute("PRAGMA table_info(albums)")]
        collist = ", ".join(f'"{c}"' for c in cols)
        con.execute("PRAGMA foreign_keys=OFF")
        with con:  # eine Transaktion
            con.execute(new_ddl)
            con.execute(f"INSERT INTO albums_new ({collist}) SELECT {collist} FROM albums")
            con.execute("DROP TABLE albums")
            con.execute("ALTER TABLE albums_new RENAME TO albums")
            for _, stmt in DESIRED_INDEXES:
                con.execute(stmt)
            # aktuelles Schema: Alben eindeutig über (titlesort, albumartist_sort)
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_album_titlesort_artist "
                        "ON albums (titlesort, albumartist_sort)")
        con.execute("PRAGMA foreign_keys=ON")

        after = counts(con)
        print("Zeilen nachher:", after)
        if after != before:
            print("MISMATCH — bitte Backup einspielen:", backup)
            return 2
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        ic = con.execute("PRAGMA integrity_check").fetchone()[0]
        print("foreign_key_check:", "ok" if not fk else fk[:5])
        print("integrity_check:", ic)
        print("Indizes jetzt:", index_list(con, "albums"))
        if fk or ic != "ok":
            return 3
        print("OK")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
