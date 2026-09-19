"""Roh-Verifikation der Album-Identität an echten Sampler-Dateien.

Läuft gegen eine KOPIE der Live-DB (kein Schreibzugriff auf die Live-DB) und
importiert echte Dateien über den echten Pfad (MediaScanner + MusicImporter).

Aufruf: .venv/bin/python3 tools/verify_album_identity.py [--apply-fix]
  ohne Schalter  -> nur Bestandsaufnahme der Kopie (VORHER)
  mit  Schalter  -> zusätzlich Scan+Import der Ordner (NACHHER)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker, create_async_engine,
)

LIVE_DB = Path.home() / ".lyrion" / "Lyrion" / "Prefs" / "lyrion.db"
WORK_DIR = Path("/tmp/album-identity-verify")
COPY = WORK_DIR / "lyrion.db"

SAMPLER_TITLE = "german top 100 single charts"
SAMPLER_FOLDER_HINT = "/Musik/GSC"     # der Ordner mit 100 Sampler-Tracks


def copy_live_db() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(str(COPY))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{COPY}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    return con


def path_from_url(url: str) -> str:
    """In einen echten Dateisystem-Pfad (für den Scanner)."""
    return urllib.parse.unquote(url.replace("file://", ""))


def prefix_from_url(url: str) -> str:
    """DB-URL-Präfix des Ordners — genau die Form, in der ``tracks.url`` steht."""
    return url.rsplit("/", 1)[0] + "/"


REPORT_SQL = """
SELECT a.id, a.title, a.albumartist_sort, a.compilation,
       (SELECT COUNT(*) FROM tracks_albums ta JOIN tracks t ON t.id = ta.track
         WHERE ta.album = a.id AND t.url LIKE :prefix) AS tracks,
       (SELECT COUNT(DISTINCT ac.contributor) FROM albums_contributors ac
         WHERE ac.album = a.id AND ac.role = 1) AS album_contributors,
       (SELECT COUNT(DISTINCT tc.contributor) FROM tracks_contributors tc
         WHERE tc.role = 1 AND tc.track IN
               (SELECT ta.track FROM tracks_albums ta JOIN tracks t ON t.id = ta.track
                 WHERE ta.album = a.id AND t.url LIKE :prefix)) AS track_artists
  FROM albums a
 WHERE a.titlesort = :ts
   AND EXISTS (SELECT 1 FROM tracks_albums ta JOIN tracks t ON t.id = ta.track
                WHERE ta.album = a.id AND t.url LIKE :prefix)
 ORDER BY a.id
"""


def report(title_sort: str, prefix: str, label: str) -> list[tuple]:
    con = db()
    rows = con.execute(REPORT_SQL,
                       {"ts": title_sort.lower(), "prefix": prefix + "%"}).fetchall()
    con.close()
    print(f"\n=== {label} | Titel='{title_sort}' | Ordner-Praefix={prefix}")
    print(f"    Album-Zeilen mit Tracks in diesem Ordner: {len(rows)}")
    for r in rows:
        print(f"      album id={r[0]:>6}  albumartist_sort={r[2]!r:<22} "
              f"compilation={r[3]}  tracks={r[4]:<4} "
              f"album-contributors={r[5]:<4} track-artists={r[6]}")
    if rows:
        keys = {r[2] for r in rows}
        flags = {(r[3]) for r in rows}
        print(f"    Anzahl Alben={len(rows)}  Schluessel={keys}  "
              f"compilation-Flags={flags}")
    return rows


def folder_summary(prefix: str, label: str) -> None:
    con = db()
    albums = con.execute(
        "SELECT COUNT(DISTINCT ta.album) FROM tracks_albums ta JOIN tracks t "
        "ON t.id = ta.track WHERE t.url LIKE :p",
        {"p": prefix + "%"}).fetchone()[0]
    comp = con.execute(
        "SELECT COUNT(DISTINCT a.id) FROM albums a JOIN tracks_albums ta "
        "ON ta.album = a.id JOIN tracks t ON t.id = ta.track "
        "WHERE t.url LIKE :p AND a.compilation = 1",
        {"p": prefix + "%"}).fetchone()[0]
    tracks = con.execute("SELECT COUNT(*) FROM tracks WHERE url LIKE :p",
                         {"p": prefix + "%"}).fetchone()[0]
    empty = con.execute(
        "SELECT COUNT(*) FROM albums WHERE NOT EXISTS "
        "(SELECT 1 FROM tracks_albums WHERE album = albums.id)").fetchone()[0]
    con.close()
    print(f"    [{label}] Album-Zeilen mit Tracks im Ordner: {albums} "
          f"(davon compilation=1: {comp}) | Tracks im Ordner: {tracks} | "
          f"leere Album-Zeilen in der DB: {empty}")


def find_normal_album() -> tuple[str, str, str] | None:
    """(Titel, Titel-Sort, Ordner-Praefix) eines Albums mit EINEM Interpreten."""
    con = db()
    urls = [r[0] for r in con.execute(
        "SELECT t.url FROM tracks t JOIN tracks_albums ta ON ta.track = t.id "
        "JOIN albums a ON a.id = ta.album WHERE a.compilation = 0 "
        "GROUP BY a.id HAVING COUNT(*) >= 8 LIMIT 300")]
    for url in urls:
        prefix = prefix_from_url(url)
        row = con.execute(
            "SELECT COUNT(DISTINCT a.id), COUNT(DISTINCT a.albumartist_sort), "
            "COUNT(*) FROM albums a JOIN tracks_albums ta ON ta.album = a.id "
            "JOIN tracks t ON t.id = ta.track WHERE t.url LIKE :p",
            {"p": prefix + "%"}).fetchone()
        if row[0] == 1 and row[1] == 1 and row[2] >= 8:
            album = con.execute(
                "SELECT a.title, a.titlesort FROM albums a "
                "JOIN tracks_albums ta ON ta.album = a.id JOIN tracks t ON t.id = ta.track "
                "WHERE t.url LIKE :p LIMIT 1", {"p": prefix + "%"}).fetchone()
            con.close()
            return album[0], album[1], prefix
    con.close()
    return None


def find_same_title_pair() -> list[tuple[str, str, str]]:
    """Titel, die in ZWEI verschiedenen Ordnern je ein Album haben."""
    con = db()
    rows = con.execute("""
        SELECT a.titlesort, a.albumartist_sort, t.url
          FROM albums a JOIN tracks_albums ta ON ta.album = a.id
          JOIN tracks t ON t.id = ta.track
         WHERE a.titlesort IN (
               SELECT titlesort FROM albums GROUP BY titlesort
                HAVING COUNT(*) = 2 AND COUNT(DISTINCT albumartist_sort) = 2)
         GROUP BY a.titlesort, a.albumartist_sort""").fetchall()
    for ts, key, url in rows:
        prefix = prefix_from_url(url)
        others = con.execute(
            "SELECT DISTINCT t2.url FROM albums a2 JOIN tracks_albums ta2 "
            "ON ta2.album = a2.id JOIN tracks t2 ON t2.id = ta2.track "
            "WHERE a2.titlesort = :ts AND t2.url NOT LIKE :p LIMIT 1",
            {"ts": ts, "p": prefix + "%"}).fetchone()
        if others:
            other_prefix = prefix_from_url(others[0])
            if other_prefix != prefix:
                con.close()
                return [(ts, prefix, "Ordner A"), (ts, other_prefix, "Ordner B")]
    con.close()
    return []


async def extract(folder: Path) -> list:
    from lyrion.media.scanner import MediaScanner, ScanConfig
    scanner = MediaScanner(config=ScanConfig(base_path=folder))
    files = sorted(p for p in folder.iterdir() if p.is_file()
                   and p.suffix.lower().lstrip(".") in
                   ("mp3", "flac", "ogg", "oga", "m4a", "mp4", "aac", "wav",
                    "wma", "opus", "ape", "aiff", "aif"))
    out = []
    for p in files:
        info = await scanner.scan_single_file(p)
        if info is not None:
            out.append((p, info))
    return out


async def import_folder(prefix: str, batch_size: int) -> int:
    """Echter Pfad: MediaScanner + MusicImporter gegen die DB-KOPIE."""
    from lyrion.media.importer import ImportConfig, MusicImporter

    folder = Path(path_from_url(prefix.rstrip("/")))
    extracted = await extract(folder)
    print(f"    extrahiert: {len(extracted)} Dateien aus {folder}")
    engine = create_async_engine(f"sqlite+aiosqlite:///{COPY}")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    importer = MusicImporter(ImportConfig(source_path=folder, batch_size=batch_size,
                                          mode="incremental"))
    async with Session() as session:
        for i in range(0, len(extracted), batch_size):
            await importer._import_batch(session, extracted[i:i + batch_size])
            await session.commit()
        await session.execute(text("PRAGMA foreign_keys=OFF"))
    # Scan-Ende: Perl räumt Album-Zeilen ohne Track ab (Slim/Schema/Album.pm:383-406;
    # Aufruf Slim/Utils/Scanner/Local.pm:855). Im Server erledigt das
    # MusicImporter._prune_orphan_albums (über die globale db_session — die ist
    # in diesem Standalone-Skript nicht initialisiert, deshalb hier direkt).
    con = sqlite3.connect(str(COPY))
    with con:
        removed = con.execute("DELETE FROM albums WHERE id NOT IN "
                              "(SELECT album FROM tracks_albums)").rowcount
        con.execute("DELETE FROM albums_contributors WHERE album NOT IN "
                    "(SELECT id FROM albums)")
    con.close()
    print(f"    Scan-Ende: leere Album-Zeilen entfernt: {removed}")
    await engine.dispose()
    return len(extracted)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-fix", action="store_true")
    args = parser.parse_args()

    copy_live_db()
    print(f"Kopie der Live-DB: {COPY} ({COPY.stat().st_size // 1024 // 1024} MB)")

    con = db()
    sampler_url = con.execute(
        "SELECT t.url FROM tracks t JOIN tracks_albums ta ON ta.track = t.id "
        "JOIN albums a ON a.id = ta.album WHERE a.titlesort = :ts "
        "AND t.url LIKE :p LIMIT 1",
        {"ts": SAMPLER_TITLE, "p": "%" + SAMPLER_FOLDER_HINT + "/%"}).fetchone()[0]
    con.close()
    sampler_prefix = prefix_from_url(sampler_url)
    normal = find_normal_album()
    pair = find_same_title_pair()

    report(SAMPLER_TITLE, sampler_prefix, "VORHER Sampler")
    folder_summary(sampler_prefix, "VORHER Sampler")
    if normal:
        report(normal[1], normal[2], "VORHER normales Album")
        folder_summary(normal[2], "VORHER normales Album")
    for entry in pair:
        report(entry[0], entry[1], f"VORHER gleicher Titel / {entry[2]}")

    if not args.apply_fix:
        return

    print("\n--- Scan + Import der echten Ordner (DB-Kopie, mode=incremental) ---")
    n = await import_folder(sampler_prefix, batch_size=50)  # Grenze im Ordner
    print(f"    Sampler: {n} Dateien importiert")
    report(SAMPLER_TITLE, sampler_prefix, "NACHHER Sampler")
    folder_summary(sampler_prefix, "NACHHER Sampler")
    if normal:
        n = await import_folder(normal[2], batch_size=50)
        print(f"    normales Album: {n} Dateien importiert")
        report(normal[1], normal[2], "NACHHER normales Album")
        folder_summary(normal[2], "NACHHER normales Album")
    for entry in pair:
        n = await import_folder(entry[1], batch_size=50)
        print(f"    {entry[2]}: {n} Dateien importiert")
        report(entry[0], entry[1], f"NACHHER gleicher Titel / {entry[2]}")


if __name__ == "__main__":
    asyncio.run(main())
