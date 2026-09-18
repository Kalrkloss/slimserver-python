"""
Music import module for Pyrion Music Server.

Scans the configured music directory, extracts metadata with the media
scanner (mutagen) and writes tracks/albums/contributors into the SQLAlchemy
database (`tracks`, `albums`, `contributors` + junction tables).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select

from lyrion.database.schema import (
    Album,
    Contributor,
    Genre,
    Track,
    albums_contributors,
    tracks_albums,
    tracks_contributors,
    tracks_genres,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ImportConfig:
    """Configuration for music import.

    ``mode`` mirrors the LMS rescan modes (Slim/Control/Commands.pm
    rescanCommand): the default "full" scan reconciles deletions (tracks
    whose files vanished are removed together with orphaned albums/
    contributors); any other mode is an additive refresh that never
    deletes.
    """

    source_path: Path | None = None
    batch_size: int = 100
    overwrite_existing: bool = False
    mode: str = "full"

    @property
    def delete_missing(self) -> bool:
        """Only a full rescan may remove tracks that are gone from disk."""
        return self.mode in ("", "normal", "full")


@dataclass
class ImportStats:
    """Statistics for an import operation."""

    total_files: int = 0
    imported_files: int = 0
    updated_files: int = 0
    skipped_files: int = 0
    error_files: int = 0
    scanned_files: int = 0
    deleted_files: int = 0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


# Supported audio extensions (kept in sync with the media scanner)
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    "mp3", "flac", "ogg", "oga", "m4a", "mp4", "aac", "aiff", "aif",
    "wav", "wma", "opus", "spx", "ape", "tak", "m4b", "mpc", "mp+",
    "wv", "dsf", "dff", "ac3", "mp2",
})


def _file_url(path: Path) -> str:
    """LMS-style file:// URL for a local audio file."""
    return path.as_uri()


def _sort_string(value: str) -> str:
    """Basic sort key: lowercase, strip leading articles (der/die/das/the/a/an)."""
    s = value.strip().lower()
    for article in ("the ", "a ", "an ", "der ", "die ", "das ", "le ", "la ", "les "):
        if s.startswith(article):
            s = s[len(article):]
            break
    return s


# Default tag separator — Perl ``splitList`` (Slim/Utils/Prefs.pm:178).
GENRE_SEPARATOR = ";"


def split_tag(tag: str, separator: str = GENRE_SEPARATOR) -> list[str]:
    """Split a multi-value tag the way ``Slim::Music::Info::splitTag`` does.

    Perl ``Slim/Music/Info.pm:1005-1060``: split on the ``splitList``
    separator (default ``;``), trim each part, and return the tag unchanged
    when it does not actually split.  ``R&B`` / ``Rock & Roll`` are exempt
    (Info.pm:1015-1018, Bug 774).
    """
    if not tag:
        return []
    if re.match(r"^\s*R\s*&\s*B\s*$", tag, re.I) or \
            re.match(r"^\s*Rock\s*&\s*Roll\s*$", tag, re.I):
        return [tag.strip()]
    parts = [p.strip() for p in tag.split(separator)]
    parts = [p for p in parts if p]
    if len(parts) > 1:
        return parts
    trimmed = tag.strip()
    return [trimmed] if trimmed else []


def genre_namesearch(name: str) -> str:
    """Perl ``Genre::add`` namesearch = ``Text::ignoreCase($name, 1)``.

    ``Slim/Schema/Genre.pm:104`` calls ``ignoreCase`` → ``ignoreCaseArticles``
    (``Slim/Utils/Text.pm:134-176``): upper-case, punctuation → space, compact,
    strip.  It is the genres table's unique key (Genre.pm:31).
    """
    s = name.upper()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"  +", " ", s).strip()
    return s or name.upper()


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class MusicImporter:
    """Scans music files and imports them into the database."""

    def __init__(self, config: Optional[ImportConfig] = None) -> None:
        self.config = config or ImportConfig()
        self.stats = ImportStats()
        self._progress_callbacks: list[Callable[[ImportStats], Any]] = []
        # No source path configured: fall back to the configured music folder
        # (Perl picks the OS music folder as the default media dir,
        # ``Slim/Utils/Prefs.pm:687-712`` → ``OSDetect::dirsFor('music')``).
        if self.config.source_path is None:
            from lyrion.media.music_dir import resolve_music_dir

            self.config.source_path = resolve_music_dir()

    def add_progress_callback(self, cb: Callable[[ImportStats], Any]) -> None:
        self._progress_callbacks.append(cb)

    # -- main entry ---------------------------------------------------------

    async def import_music(self, progress_callback: Optional[Callable] = None) -> ImportStats:
        """Run a full scan + import of the configured music directory."""
        if progress_callback:
            self.add_progress_callback(progress_callback)

        logger.info("Starting music import from: %s", self.config.source_path)
        self.stats = ImportStats()
        self.stats.start_time = datetime.now()

        from lyrion.media.scan_state import SCAN_STATE

        source = self.config.source_path
        if source is None:
            logger.error(
                "No music folder configured — set 'mediadirs' (Perl "
                "defaultMediaDirs, Slim/Utils/Prefs.pm:687-712)")
            self.stats.end_time = datetime.now()
            SCAN_STATE.finish()
            return self.stats

        # Distinguish "folder is gone" from "the share is not mounted"
        # (gvfs/FUSE) — different operator action, same silent library.  The
        # test is a real listing, not ``Path.is_dir()`` (which swallows the
        # OSError of a FUSE share and rejected a readable 305-entry folder) and
        # not a mount-table probe (a gvfs share is a plain sub-directory of the
        # one gvfsd-fuse mount, Perl only needs ``-d``,
        # Slim/Utils/Prefs.pm:707).
        from lyrion.platform import paths as platform_paths

        if not platform_paths.is_usable_dir(source):
            code, message = platform_paths.explain_missing_path(source)
            logger.error("Music directory unusable (%s): %s", code, message)
            self.stats.end_time = datetime.now()
            SCAN_STATE.finish()
            return self.stats

        self.stats.scanned_files = 0
        # total is unknown until the walk finishes (streaming pipeline);
        # start with a non-zero placeholder so progress bars work.
        self.stats.total_files = 0
        SCAN_STATE.start(total=1)

        from lyrion.database.sqlite_helper import db_session

        # ── Streaming pipeline: walk → extract → import overlap ────────
        # The directory walk (SLOW on gvfs/SMB mounts — minutes) runs as
        # a producer thread feeding a queue; import batches consume from
        # it WHILE the walk continues, so found tracks appear in the
        # library incrementally instead of after the full scan.
        import queue as _queue
        import threading as _threading
        from lyrion.media.scanner import MediaScanner, ScanConfig
        scanner = MediaScanner(config=ScanConfig(base_path=self.config.source_path))

        file_q: _queue.Queue = _queue.Queue(maxsize=2000)
        SENTINEL = object()
        # URLs of every file the walk discovers — used by the deletion
        # reconciliation after a FULL scan (additive scans skip this).
        found_urls: set[str] | None = (set() if self.config.delete_missing
                                       else None)
        # Absolute paths of every directory the walk visits — the scan writes
        # one ``content_type='dir'`` row per entry (Perl records them in
        # ``scanned_files`` with size 0, Slim/Utils/Scanner/Local/AIO.pm:62-67).
        # Collected on every scan mode; only a FULL rescan also prunes the
        # directory rows of directories that disappeared (see below).
        found_dirs: set[str] = set()
        # Set by the consumer when abortscan arrived; the producer polls it
        # so a long gvfs walk stops quickly instead of draining fully.
        abort_ev = _threading.Event()

        def _produce() -> None:
            try:
                for p in self.config.source_path.rglob("*"):
                    if abort_ev.is_set():
                        logger.info("Scan walk aborted — stopping walker")
                        break
                    if p.is_dir():
                        # Perl records every walked directory (filesize 0) while
                        # scanning (Slim/Utils/Scanner/Local/AIO.pm:62-67,
                        # :135-142); we turn each of them into a ``dir`` row
                        # below so a folder id is a real ``tracks.id``.
                        found_dirs.add(str(p))
                        continue
                    if p.is_file() and p.suffix.lower().lstrip(".") in SUPPORTED_EXTENSIONS:
                        name = p.name.lower()
                        if name.startswith(".") or name in ("desktop.ini", "thumbs.db"):
                            continue
                        file_q.put(p)
                        if found_urls is not None:
                            found_urls.add(_file_url(p))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Walk aborted: %s", exc)
            finally:
                file_q.put(SENTINEL)

        walker = _threading.Thread(target=_produce, daemon=True,
                                   name="import-walk")
        walker.start()

        batch: list[Path] = []
        sem = asyncio.Semaphore(8)
        total_seen = 0
        aborted = False

        async def _extract(file_path: Path) -> tuple[Path, Any] | None:
            async with sem:
                try:
                    info = await scanner.scan_single_file(file_path)
                    return (file_path, info) if info is not None else None
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Extract failed for %s: %s", file_path, exc)
                    self.stats.error_files += 1
                    return None

        loop = asyncio.get_running_loop()
        walk_done = False
        while not (walk_done and not batch):
            if not batch:
                # Refill the batch from the queue (non-blocking-ish).
                while len(batch) < self.config.batch_size:
                    item = await loop.run_in_executor(None, file_q.get)
                    if item is SENTINEL:
                        walk_done = True
                        break
                    batch.append(item)
                    total_seen += 1
            if not batch:
                break
            self.stats.total_files = max(self.stats.total_files, total_seen)

            # Abort (abortscan): stop importing, discard the pending batch,
            # keep draining the queue until the walker's SENTINEL so the
            # producer thread never blocks on a full queue.
            if SCAN_STATE.abort_requested:
                aborted = True
                abort_ev.set()
                logger.info("Scan aborted — discarding %d queued file(s)",
                            len(batch))
                batch = []
                continue

            # Phase 1: metadata extraction (parallel, no DB). The mutagen
            # C calls release the GIL in worker threads.
            extracted: list[tuple[Path, Any]] = []
            for task in asyncio.as_completed(
                    [asyncio.create_task(_extract(p)) for p in batch]):
                self.stats.scanned_files += 1
                r = await task
                if r:
                    extracted.append(r)
                SCAN_STATE.update(done=self.stats.scanned_files,
                                  total=max(total_seen, 1))
            batch = []

            # Phase 2: short-lived session, inserts only — the library
            # grows incrementally while the walk continues.
            async with db_session() as session:
                await self._import_batch(session, extracted)
                self.stats.imported_files += len(extracted)
                await session.commit()
            self._emit_progress()
            logger.info("Imported %d/%d+ files", self.stats.scanned_files,
                        total_seen)

        walker.join(timeout=10)
        self.stats.end_time = datetime.now()

        # Directory rows: one ``content_type='dir'`` row per walked directory,
        # so a folder id is the row's ``tracks.id`` (Perl: the scan records the
        # dirs — Slim/Utils/Scanner/Local/AIO.pm:62-67/:135-142 — and the
        # browse creates the rows, Slim/Schema.pm:764-856).  Never on an
        # aborted scan (a half-walked tree would prune live folders).
        if not aborted and found_dirs:
            await self._sync_dir_rows(found_dirs)

        # Deletion reconciliation: a full scan removes tracks whose files
        # are no longer on disk (plus orphaned albums/contributors). Never
        # run on an aborted scan, and never when the walk found nothing
        # (a wrong/empty musicdir must not wipe the library).
        if not aborted and found_urls is not None and found_urls:
            self.stats.deleted_files = await self._reconcile_deletions(
                found_urls)

        self._emit_progress()
        SCAN_STATE.finish()
        logger.info(
            "Import complete: %d imported, %d deleted, %d errors, %d total "
            "(took %.1fs)",
            self.stats.imported_files, self.stats.deleted_files,
            self.stats.error_files, self.stats.total_files,
            (self.stats.end_time - self.stats.start_time).total_seconds(),
        )
        return self.stats

    async def _sync_dir_rows(self, found_dirs: set[str]) -> tuple[int, int]:
        """Store a ``dir`` row per walked directory (Perl's folder ids).

        The rows are written on the importer's own session, so they land in
        exactly the DB this scan writes to.  ``prune`` is only set for a FULL
        scan whose walk actually found audio (same guard as the track
        reconciliation): rows of directories that no longer exist are removed
        — Perl drops them all with a full wipe (``Slim/Schema.pm:2346-2357``
        ``wipeAllData``) and keeps stale ones through a normal rescan, because
        its deleted/changed sets are filtered with ``content_type != 'dir'``
        (``Slim/Utils/Scanner/Local.pm:186``).
        """
        from lyrion.database.sqlite_helper import db_session
        from lyrion.media import dir_rows

        prune = bool(self.config.delete_missing)
        created = pruned = 0
        # ``rglob('*')`` never yields the root itself; Perl stores it too
        # (Slim/Utils/Scanner/Local/AIO.pm:62-68 "Add the root directory to
        # the database", filesize 0) — and browsing the root creates its
        # ``tracks`` row anyway (Slim/Utils/Misc.pm:1082-1090).
        directories = set(found_dirs)
        if self.config.source_path is not None:
            directories.add(str(self.config.source_path))
        try:
            async with db_session() as session:
                created, pruned = await dir_rows.sync_dir_rows(
                    session, directories,
                    root=self.config.source_path, prune=prune)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - a scan must not fail on this
            logger.warning("Directory rows could not be synced: %s", exc)
            return (0, 0)
        if created or pruned:
            logger.info("Directory rows: %d created, %d orphaned removed",
                        created, pruned)
        return (created, pruned)

    async def _reconcile_deletions(self, found_urls: set[str]) -> int:
        """Remove tracks whose files disappeared since the last full scan.

        Only tracks outside the freshly-walked URL set are deleted; join
        rows cascade (FK ON), orphaned albums/contributors/genres without
        any remaining track are removed too (Perl full-rescan semantics).
        Returns the number of deleted tracks.
        """
        from lyrion.database.sqlite_helper import db_session
        from lyrion.media import dir_rows
        from sqlalchemy import text

        if not found_urls:
            logger.warning("Full scan found no files — refusing deletion "
                           "reconciliation")
            return 0

        async with db_session() as session:
            # ``content_type != 'dir'``: Perl's deleted-set is built with the
            # same filter (Slim/Utils/Scanner/Local.pm:186 ``$ctFilter``), so a
            # directory row is never treated as a vanished audio file — its
            # cleanup is the orphan prune of ``_sync_dir_rows`` (and Perl's
            # full wipe, Slim/Schema.pm:2346-2357).
            rows = await session.execute(text(
                "SELECT url FROM tracks WHERE "
                f"{dir_rows.not_dir_clause()}"))
            db_urls = {row[0] for row in rows}
            missing = sorted(db_urls - found_urls)
            if not missing:
                return 0
            # Chunked deletes stay under SQLite's per-statement variable
            # limit even for 50k+ track libraries.
            for i in range(0, len(missing), 900):
                chunk = missing[i:i + 900]
                binds = ", ".join(f":p{n}" for n in range(len(chunk)))
                await session.execute(
                    text(f"DELETE FROM tracks WHERE url IN ({binds})"),
                    {f"p{n}": url for n, url in enumerate(chunk)})
            # Explicit join-row + orphan cleanup — required both when the
            # FK pragma is off (tests, legacy connections) and to drop
            # albums/contributors/genres that no track references anymore
            # (the FK cascades only reach the join tables).
            await session.execute(text(
                "DELETE FROM tracks_albums WHERE track NOT IN "
                "(SELECT id FROM tracks)"))
            await session.execute(text(
                "DELETE FROM tracks_albums WHERE album NOT IN "
                "(SELECT id FROM albums)"))
            await session.execute(text(
                "DELETE FROM tracks_contributors WHERE track NOT IN "
                "(SELECT id FROM tracks)"))
            await session.execute(text(
                "DELETE FROM tracks_contributors WHERE contributor NOT IN "
                "(SELECT id FROM contributors)"))
            await session.execute(text(
                "DELETE FROM tracks_genres WHERE track NOT IN "
                "(SELECT id FROM tracks)"))
            await session.execute(text(
                "DELETE FROM albums_contributors WHERE album NOT IN "
                "(SELECT id FROM albums)"))
            await session.execute(text(
                "DELETE FROM albums_contributors WHERE contributor NOT IN "
                "(SELECT id FROM contributors)"))
            await session.execute(text(
                "DELETE FROM albums WHERE id NOT IN "
                "(SELECT DISTINCT album FROM tracks_albums)"))
            await session.execute(text(
                "DELETE FROM contributors WHERE id NOT IN "
                "(SELECT DISTINCT contributor FROM tracks_contributors)"))
            await session.execute(text(
                "DELETE FROM genres WHERE id NOT IN "
                "(SELECT DISTINCT genre FROM tracks_genres)"))
            await session.commit()
            logger.info("Deletion reconciliation removed %d track(s)",
                        len(missing))
            return len(missing)

    # -- file collection ----------------------------------------------------

    async def _collect_files(self) -> list[Path]:
        """Walk the music directory and return all supported audio files."""
        loop = asyncio.get_running_loop()
        results: list[Path] = []

        def _walk() -> list[Path]:
            found: list[Path] = []
            for p in self.config.source_path.rglob("*"):
                if p.is_file() and p.suffix.lower().lstrip(".") in SUPPORTED_EXTENSIONS:
                    name = p.name.lower()
                    if name.startswith(".") or name in ("desktop.ini", "thumbs.db"):
                        continue
                    found.append(p)
            return found

        results = await loop.run_in_executor(None, _walk)
        return sorted(results)

    # -- single track -------------------------------------------------------

    async def _import_batch(self, session, extracted: list[tuple[Path, Any]]) -> None:
        """Import one batch with batch-level lookups (once per batch,
        not per track)."""
        # Existing tracks for the batch URLs.
        urls = [_file_url(p) for p, _ in extracted]
        track_by_url: dict[str, Track] = {t.url: t for t in (
            await session.execute(
                select(Track).where(Track.url.in_(urls)))).scalars()}

        # Track upserts first — one flush per batch (not per track) gives
        # every new track its id for the membership sets below.
        for file_path, info in extracted:
            try:
                await self._import_track(session, file_path, info, track_by_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Import failed for %s: %s", file_path, exc)
                self.stats.error_files += 1
        await session.flush()

        # Batch-level membership sets (existing join rows for these ids).
        track_ids = [t.id for t in track_by_url.values()]
        ta_set: set[tuple] = set()
        tc_set: set[tuple] = set()
        ac_set: set[tuple] = set()
        tg_set: set[tuple] = set()
        tg_tracks: set[int] = set()
        if track_ids:
            for r in (await session.execute(
                    select(tracks_albums.c.track, tracks_albums.c.album)
                    .where(tracks_albums.c.track.in_(track_ids)))).all():
                ta_set.add((r[0], r[1]))
            for r in (await session.execute(
                    select(tracks_contributors.c.track,
                           tracks_contributors.c.contributor)
                    .where(tracks_contributors.c.track.in_(track_ids)))).all():
                tc_set.add((r[0], r[1]))
            for r in (await session.execute(
                    select(tracks_genres.c.track, tracks_genres.c.genre)
                    .where(tracks_genres.c.track.in_(track_ids)))).all():
                tg_set.add((r[0], r[1]))
                tg_tracks.add(r[0])
        album_ids = [a.id for a in
                     (await session.execute(select(Album))).scalars()]
        if album_ids:
            for r in (await session.execute(
                    select(albums_contributors.c.album,
                           albums_contributors.c.contributor)
                    .where(albums_contributors.c.album.in_(album_ids)))).all():
                ac_set.add((r[0], r[1]))

        # Existing albums/contributors for the batch keys.
        # Album identity = (title sort, artist sort) — NOT year (different
        # track years must not split a compilation) and NOT title alone
        # (different artists' same-title albums must not merge).
        album_keys = set()
        artist_names: set[str] = set()
        for _, info in extracted:
            album_name = getattr(info, "album", None) or "Unknown Album"
            artist = getattr(info, "artist", None) or "Unknown Artist"
            compilation = bool(getattr(info, "compilation", False))
            artist_key = "various artists" if compilation else _sort_string(artist)
            album_keys.add((_sort_string(album_name), artist_key))
            if artist and artist != "Unknown Artist":
                artist_names.add(artist.strip().lower())
        album_by_key: dict[tuple, Album] = {
            (a.titlesort, a.albumartist_sort): a for a in (
                await session.execute(
                    select(Album).where(
                        Album.titlesort.in_([k[0] for k in album_keys])))).scalars()
            if (a.titlesort, a.albumartist_sort) in album_keys}
        # Altbestand: Alben, die vor der Spalte ``albumartist_sort`` entstanden
        # sind, tragen dort NULL (die Migration füllt nicht nach). Der Import
        # sucht über (titlesort, artist_sort), findet sie also nie und legt eine
        # zweite Zeile an — die alte UNIQUE-Bedingung (titlesort, year) lehnt
        # das ab und reißt die ganze Transaktion mit (im Live-Lauf beobachtet:
        # "UNIQUE constraint failed: albums.titlesort, albums.year").
        # Deshalb zusätzlich über (titlesort, year) indexieren, adoptieren und
        # ``albumartist_sort`` nachtragen.
        album_by_title_year: dict[tuple, Album] = {
            (a.titlesort, a.year): a for a in (
                await session.execute(
                    select(Album).where(
                        Album.titlesort.in_([k[0] for k in album_keys])))).scalars()
            if not a.albumartist_sort}
        # Zweite Ebene: ALLE Zeilen des Stapels über (titlesort, year). Nötig,
        # weil der alte UNIQUE-Index (titlesort, year) jede zweite Zeile mit
        # gleichem Titel+Jahr verbietet: hat eine Zeile schon einen Artist-Sort
        # (z. B. durch einen Titel mit anderem Künstler-Tag adoptiert), findet
        # der exakte Schlüssel sie nicht mehr und der Insert stirbt. Bis der
        # Altindex entfernt ist, wird die Zeile hier gefunden und
        # wiederverwendet (ein vorhandener Artist-Sort bleibt stehen).
        album_by_title_year_all: dict[tuple, Album] = {
            (a.titlesort, a.year): a for a in (
                await session.execute(
                    select(Album).where(
                        Album.titlesort.in_([k[0] for k in album_keys])))).scalars()}
        contrib_by_name: dict[str, Contributor] = {
            c.namespell: c for c in (
                await session.execute(
                    select(Contributor).where(
                        Contributor.namespell.in_(list(artist_names))))).scalars()}

        # Existing genres for the batch keys (LIB-10; Perl Genre::add()).
        genre_keys: set[str] = set()
        for _, info in extracted:
            for gname in split_tag(getattr(info, "genre", "") or ""):
                genre_keys.add(genre_namesearch(gname))
        genre_by_namespell: dict[str, Genre] = {
            g.namespell: g for g in (
                await session.execute(
                    select(Genre).where(
                        Genre.namespell.in_(list(genre_keys))))).scalars()} \
            if genre_keys else {}

        # Album/contributor links for the batch tracks.
        for file_path, info in extracted:
            try:
                await self._import_links(session, file_path, info,
                                         track_by_url, album_by_key,
                                         contrib_by_name, ta_set, tc_set, ac_set,
                                         genre_by_namespell, tg_set, tg_tracks,
                                         album_by_title_year,
                                         album_by_title_year_all)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Import failed for %s: %s", file_path, exc)
                self.stats.error_files += 1

    async def _import_track(
        self, session, file_path: Path, info: Any,
        track_by_url: dict[str, Track],
    ) -> None:
        """Upsert one track (no joins — those run in _import_links)."""
        url = _file_url(file_path)
        title = (info.title or file_path.stem) if hasattr(info, "title") else file_path.stem
        mtime = getattr(info, "last_modified", None)
        modtime = int(mtime.timestamp()) if mtime is not None else 0
        filesize = getattr(info, "filesize", 0) or 0
        mimetype = getattr(info, "mimetype", None)
        bitrate = getattr(info, "bitrate", None) or None
        sample_rate = getattr(info, "sample_rate", None) or None
        channels = getattr(info, "channels", None) or None
        tracknum = getattr(info, "track", None) or None
        disc = getattr(info, "disc_number", None) or getattr(info, "disc", None) or None
        comment = getattr(info, "comment", None)
        compilation = bool(getattr(info, "compilation", False))
        # duration is in milliseconds in the new ScanResult
        duration_ms = getattr(info, "duration", 0) or 0
        duration = duration_ms / 1000.0
        genre = getattr(info, "genre", None) or ""
        year = getattr(info, "year", 0) or 0
        # ReplayGain (Perl Schema.pm:2915-2945, aus den Track-Tags gemungt).
        rg_gain = getattr(info, "replay_gain", None)
        rg_peak = getattr(info, "replay_peak", None)

        track = track_by_url.get(url)
        if track is None:
            track = Track(
                url=url,
                titlesort=_sort_string(title),
                title=title,
                content_type=mimetype or self._guess_mime(file_path),
                modtime=modtime,
                filesize=filesize,
                bitrate=bitrate,
                samplerate=sample_rate,
                channels=channels,
                duration=duration,
                year=year or None,
                genre=genre or None,
                tracknum=tracknum,
                disc=disc,
                comment=comment,
                replay_gain=rg_gain,
                replay_peak=rg_peak,
                lastscanned=datetime.utcnow(),
                audio=1,
                video=0,
                disabled=0,
                compilation=1 if compilation else 0,
            )
            session.add(track)
            track_by_url[url] = track
        else:
            # Update mutable fields
            track.title = title
            track.titlesort = _sort_string(title)
            track.modtime = modtime
            track.filesize = filesize
            track.bitrate = bitrate or track.bitrate
            track.samplerate = sample_rate or track.samplerate
            track.channels = channels or track.channels
            track.duration = duration or track.duration
            track.year = year or track.year
            track.genre = genre or track.genre
            track.tracknum = tracknum or track.tracknum
            # Perl (Schema.pm:2925) schreibt nur, wenn der Tag existiert; ein
            # entfernter Tag löscht den DB-Wert also nicht.
            if rg_gain is not None:
                track.replay_gain = rg_gain
            if rg_peak is not None:
                track.replay_peak = rg_peak
            track.lastscanned = datetime.utcnow()
            await session.flush()

    async def _import_links(
        self, session, file_path: Path, info: Any,
        track_by_url: dict[str, Track],
        album_by_key: dict[tuple, Album],
        contrib_by_name: dict[str, Contributor],
        ta_set: set, tc_set: set, ac_set: set,
        genre_by_namespell: dict[str, Genre] | None = None,
        tg_set: set | None = None,
        tg_tracks: set | None = None,
        album_by_title_year: dict[tuple, Album] | None = None,
        album_by_title_year_all: dict[tuple, Album] | None = None,
    ) -> None:
        """Album + contributor + genre links for a track (Core inserts only)."""
        url = _file_url(file_path)
        track = track_by_url[url]
        artist = (info.artist or "Unknown Artist") if hasattr(info, "artist") else "Unknown Artist"
        album_name = info.album or "Unknown Album" if hasattr(info, "album") else "Unknown Album"
        year = getattr(info, "year", 0) or 0
        compilation = bool(getattr(info, "compilation", False))
        artist_key = "various artists" if compilation else _sort_string(artist)
        key = (_sort_string(album_name), artist_key)
        # Album-ReplayGain (Perl Schema.pm:1299-1322; Kommentar dort: "we do
        # want to update album gain tags if they are changed").
        album_rg = getattr(info, "album_replay_gain", None)
        album_peak = getattr(info, "album_replay_peak", None)

        album = album_by_key.get(key)
        if album is None and album_by_title_year:
            # Vor der Spalte entstandene Zeile adoptieren (Begründung beim
            # Aufbau der Map) und den fehlenden Artist-Sort nachtragen, damit
            # der nächste Lauf den exakten Schlüssel findet.
            album = album_by_title_year.pop((_sort_string(album_name),
                                             year or None), None)
            if album is not None:
                album.albumartist_sort = artist_key
                album_by_key[key] = album
        if album is None and album_by_title_year_all:
            # Zweite Ebene (siehe Map): gleiche Titel+Jahr-Zeile wiederverwenden,
            # statt in den alten UNIQUE-Index zu laufen. Ein bereits gesetzter
            # Artist-Sort bleibt unangetastet.
            album = album_by_title_year_all.get((_sort_string(album_name),
                                                 year or None))
            if album is not None and not album.albumartist_sort:
                album.albumartist_sort = artist_key
            if album is not None:
                album_by_key[key] = album
        if album is None:
            # Cover artwork: scanner found cover.jpg/png/… in the track's
            # folder — store the path so the API can serve it to players.
            artwork = getattr(info, "artwork_path", None)
            album = Album(
                titlesort=_sort_string(album_name),
                title=album_name,
                albumartist_sort=artist_key,
                year=year or None,
                compilation=1 if compilation else 0,
                artwork=str(artwork) if artwork else None,
                artwork_front=str(artwork) if artwork else None,
                replay_gain=album_rg,
                replay_peak=album_peak,
            )
            session.add(album)
            await session.flush()
            album_by_key[key] = album
        else:
            if not album.artwork:
                # Album existed without artwork (e.g. imported before this
                # column was filled) — backfill from this track's folder.
                artwork = getattr(info, "artwork_path", None)
                if artwork:
                    album.artwork = str(artwork)
                    album.artwork_front = str(artwork)
            # Album-Gain aktualisieren, wenn ein Tag vorhanden ist
            # (Perl Schema.pm:1306-1310).
            if album_rg is not None:
                album.replay_gain = album_rg
            if album_peak is not None:
                album.replay_peak = album_peak
        # Retag cleanup: drop a stale album link (track re-tagged to a
        # different album) — otherwise the old album keeps listing the track.
        await session.execute(
            tracks_albums.delete().where(
                (tracks_albums.c.track == track.id)
                & (tracks_albums.c.album != album.id)))
        if (track.id, album.id) not in ta_set:
            await session.execute(
                tracks_albums.insert().values(track=track.id, album=album.id))
            ta_set.add((track.id, album.id))

        if artist and artist != "Unknown Artist":
            contrib = contrib_by_name.get(artist.strip().lower())
            if contrib is None:
                contrib = Contributor(
                    namespell=artist.strip().lower(),
                    name=artist.strip(),
                    sortname=_sort_string(artist),
                )
                session.add(contrib)
                await session.flush()
                contrib_by_name[artist.strip().lower()] = contrib
            # Retag cleanup: drop stale artist links for this track.
            await session.execute(
                tracks_contributors.delete().where(
                    (tracks_contributors.c.track == track.id)
                    & (tracks_contributors.c.contributor != contrib.id)
                    & (tracks_contributors.c.role == 1)))
            if (track.id, contrib.id) not in tc_set:
                await session.execute(
                    tracks_contributors.insert().values(
                        track=track.id, contributor=contrib.id, role=1))
                tc_set.add((track.id, contrib.id))
            if (album.id, contrib.id) not in ac_set:
                await session.execute(
                    albums_contributors.insert().values(
                        album=album.id, contributor=contrib.id, role=1))
                ac_set.add((album.id, contrib.id))

        # Genre links (LIB-10).  Perl ``Slim::Schema::Genre::add``
        # (Slim/Schema/Genre.pm:91-132): split the tag, upsert one ``genres``
        # row per value (unique key = namesearch) and ``REPLACE INTO
        # genre_track``.  A re-tag drops stale links like the album/artist
        # cleanups above.
        genre_names = split_tag(getattr(info, "genre", "") or "")
        if genre_by_namespell is None:
            genre_by_namespell = {}
        if tg_set is None:
            tg_set = set()
        if genre_names or (tg_tracks and track.id in tg_tracks):
            new_ids: list[int] = []
            for name in genre_names:
                key = genre_namesearch(name)
                genre = genre_by_namespell.get(key)
                if genre is None:
                    genre = Genre(namespell=key, name=name,
                                  sortkey=_sort_string(name))
                    session.add(genre)
                    await session.flush()
                    genre_by_namespell[key] = genre
                new_ids.append(genre.id)
                if (track.id, genre.id) not in tg_set:
                    await session.execute(
                        tracks_genres.insert().values(
                            track=track.id, genre=genre.id))
                    tg_set.add((track.id, genre.id))
            # Retag cleanup: drop genre links this track no longer carries.
            await session.execute(
                tracks_genres.delete().where(
                    (tracks_genres.c.track == track.id)
                    & (tracks_genres.c.genre.notin_(new_ids))))
            if tg_tracks is not None and new_ids:
                tg_tracks.add(track.id)

    @staticmethod
    def _guess_mime(path: Path) -> str:
        ext = path.suffix.lower().lstrip(".")
        mime_map = {
            "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
            "oga": "audio/ogg", "opus": "audio/ogg", "m4a": "audio/mp4",
            "aac": "audio/aac", "aiff": "audio/aiff", "aif": "audio/aiff",
            "wav": "audio/wav", "wma": "audio/x-ms-wma", "ape": "audio/x-ape",
        }
        return mime_map.get(ext, "audio/mpeg")

    # -- progress -----------------------------------------------------------

    def _emit_progress(self) -> None:
        for cb in self._progress_callbacks:
            try:
                cb(self.stats)
            except Exception:  # noqa: BLE001
                logger.exception("Progress callback failed")
