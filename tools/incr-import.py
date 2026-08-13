#!/usr/bin/env python3
"""Incremental import: add the tracks the full scan missed (mp4, cue,
m3u, ...) — taken from the Perl reference DB minus the Python DB,
existing on disk. Uses the batch importer (idempotent upserts)."""
import asyncio
import sqlite3
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, "/root/lyrion-python/src")

PERL_DB = "/var/lib/squeezeboxserver/cache/library.db"
PY_DB = "/root/.lyrion/Lyrion/Prefs/lyrion.db"


def norm(u: str) -> str:
    return urllib.parse.unquote(u).replace("file://", "").lower()


def find_missing() -> list[Path]:
    perl = sqlite3.connect(f"file:{PERL_DB}?mode=ro", uri=True)
    perl_urls = [r[0] for r in perl.execute("SELECT url FROM tracks")]
    perl.close()
    perl_norm = {norm(u) for u in perl_urls}
    py = sqlite3.connect(f"file:{PY_DB}?mode=ro", uri=True)
    pyp = set(norm(r[0]) for r in py.execute("SELECT url FROM tracks"))
    py.close()
    on_disk = []
    for u in perl_urls:
        if norm(u) in pyp:
            continue
        p = Path(urllib.parse.unquote(u).replace("file://", ""))
        if p.is_file():
            on_disk.append(p)
    return sorted(set(on_disk))


async def main() -> int:
    from lyrion.database.sqlite_helper import init_db, db_session
    from lyrion.media.importer import ImportConfig, MusicImporter
    from lyrion.media.scanner import MediaScanner, ScanConfig

    await init_db()
    missing = find_missing()
    print(f"Fehlende Dateien auf Platte: {len(missing)}")
    if not missing:
        return 0
    imp = MusicImporter(ImportConfig(source_path=Path("/mnt/media/Musik")))
    scanner = MediaScanner(config=ScanConfig(base_path=Path("/mnt/media/Musik")))

    ok = err = 0
    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        extracted = []
        sem = asyncio.Semaphore(8)

        async def _extract(p: Path):
            async with sem:
                try:
                    info = await scanner.scan_single_file(p)
                    return (p, info) if info is not None else None
                except Exception:
                    return None

        for task in asyncio.as_completed([asyncio.create_task(_extract(p)) for p in batch]):
            r = await task
            if r:
                extracted.append(r)
        async with db_session() as session:
            await imp._import_batch(session, extracted)
            await session.commit()
        ok += len(extracted)
        err += len(batch) - len(extracted)
        print(f"  {i + len(batch)}/{len(missing)} importiert")
    print(f"Fertig: {ok} importiert, {err} ohne Metadaten")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
