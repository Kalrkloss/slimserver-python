"""
Music import module for Lyrion Music Server.

Scans the configured music directory, extracts metadata with the media
scanner (mutagen) and writes tracks/albums/contributors into the SQLAlchemy
database (`tracks`, `albums`, `contributors` + junction tables).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ImportConfig:
    """Configuration for music import."""

    source_path: Path = Path("/mnt/media/Musik")
    batch_size: int = 100
    overwrite_existing: bool = False


@dataclass
class ImportStats:
    """Statistics for an import operation."""

    total_files: int = 0
    imported_files: int = 0
    updated_files: int = 0
    skipped_files: int = 0
    error_files: int = 0
    scanned_files: int = 0
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


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class MusicImporter:
    """Scans music files and imports them into the database."""

    def __init__(self, config: Optional[ImportConfig] = None) -> None:
        self.config = config or ImportConfig()
        self.stats = ImportStats()
        self._progress_callbacks: list[Callable[[ImportStats], Any]] = []

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

        if not self.config.source_path.is_dir():
            logger.error("Music directory does not exist: %s", self.config.source_path)
            self.stats.end_time = datetime.now()
            return self.stats

        from lyrion.database.sqlite_helper import db_session

        # Collect files first (async walk)
        files = await self._collect_files()
        self.stats.total_files = len(files)
        self.stats.scanned_files = 0
        logger.info("Found %d audio files", len(files))

        # Import in batches. Extract metadata BEFORE opening the DB session:
        # the write transaction must only be open during the (fast) inserts,
        # not during the slow mutagen extraction, or other writers (playlist
        # save, radio add) would block for minutes ("database is locked").
        from lyrion.media.scanner import MediaScanner, ScanConfig
        scanner = MediaScanner(config=ScanConfig(base_path=self.config.source_path))

        for batch_start in range(0, len(files), self.config.batch_size):
            batch = files[batch_start:batch_start + self.config.batch_size]

            # Phase 1: metadata extraction (parallel, no DB). The mutagen
            # C calls release the GIL in worker threads, so 8 concurrent
            # workers give a large speedup on big libraries.
            extracted: list[tuple[Path, Any]] = []
            sem = asyncio.Semaphore(8)

            async def _extract(file_path: Path) -> tuple[Path, Any] | None:
                async with sem:
                    try:
                        info = await scanner.scan_single_file(file_path)
                        return (file_path, info) if info is not None else None
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Extract failed for %s: %s", file_path, exc)
                        self.stats.error_files += 1
                        return None

            for task in asyncio.as_completed(
                    [asyncio.create_task(_extract(p)) for p in batch]):
                self.stats.scanned_files += 1
                r = await task
                if r:
                    extracted.append(r)

            # Phase 2: short-lived session, inserts only. Batch lookups
            # (existing tracks/albums/contributors + join membership) run
            # once per batch, not once per track — the per-track queries
            # were the bottleneck (aiosqlite thread round-trips).
            async with db_session() as session:
                await self._import_batch(session, extracted)
                self.stats.imported_files += len(extracted)
                await session.commit()
            self._emit_progress()
            logger.info("Imported %d/%d files", batch_start + len(batch), len(files))

        self.stats.end_time = datetime.now()
        self._emit_progress()
        logger.info(
            "Import complete: %d imported, %d errors, %d total "
            "(took %.1fs)",
            self.stats.imported_files, self.stats.error_files, self.stats.total_files,
            (self.stats.end_time - self.stats.start_time).total_seconds(),
        )
        return self.stats

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
        from sqlalchemy import select
        from lyrion.database.schema import (
            Album, Contributor, Track, albums_contributors,
            tracks_albums, tracks_contributors,
        )

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
        album_ids = [a.id for a in
                     (await session.execute(select(Album))).scalars()]
        if album_ids:
            for r in (await session.execute(
                    select(albums_contributors.c.album,
                           albums_contributors.c.contributor)
                    .where(albums_contributors.c.album.in_(album_ids)))).all():
                ac_set.add((r[0], r[1]))

        # Existing albums/contributors for the batch keys.
        album_keys = set()
        artist_names: set[str] = set()
        for _, info in extracted:
            album_name = getattr(info, "album", None) or "Unknown Album"
            year = getattr(info, "year", 0) or 0
            album_keys.add((_sort_string(album_name), year or None))
            artist = getattr(info, "artist", None) or "Unknown Artist"
            if artist and artist != "Unknown Artist":
                artist_names.add(artist.strip().lower())
        album_by_key: dict[tuple, Album] = {
            (a.titlesort, a.year): a for a in (
                await session.execute(
                    select(Album).where(
                        Album.titlesort.in_([k[0] for k in album_keys])))).scalars()
            if (a.titlesort, a.year) in album_keys}
        contrib_by_name: dict[str, Contributor] = {
            c.namespell: c for c in (
                await session.execute(
                    select(Contributor).where(
                        Contributor.namespell.in_(list(artist_names))))).scalars()}

        # Album/contributor links for the batch tracks.
        for file_path, info in extracted:
            try:
                await self._import_links(session, file_path, info,
                                         track_by_url, album_by_key,
                                         contrib_by_name, ta_set, tc_set, ac_set)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Import failed for %s: %s", file_path, exc)
                self.stats.error_files += 1

    async def _import_track(
        self, session, file_path: Path, info: Any,
        track_by_url: dict[str, Track],
    ) -> None:
        """Upsert one track (no joins — those run in _import_links)."""
        from lyrion.database.schema import Track

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
            track.lastscanned = datetime.utcnow()
            await session.flush()

    async def _import_links(
        self, session, file_path: Path, info: Any,
        track_by_url: dict[str, Track],
        album_by_key: dict[tuple, Album],
        contrib_by_name: dict[str, Contributor],
        ta_set: set, tc_set: set, ac_set: set,
    ) -> None:
        """Album + contributor links for a track (Core inserts only)."""
        from lyrion.database.schema import (
            Album, Contributor, albums_contributors,
            tracks_albums, tracks_contributors,
        )
        from sqlalchemy import select

        url = _file_url(file_path)
        track = track_by_url[url]
        artist = (info.artist or "Unknown Artist") if hasattr(info, "artist") else "Unknown Artist"
        album_name = info.album or "Unknown Album" if hasattr(info, "album") else "Unknown Album"
        year = getattr(info, "year", 0) or 0
        compilation = bool(getattr(info, "compilation", False))
        key = (_sort_string(album_name), year or None)

        album = album_by_key.get(key)
        if album is None:
            album = Album(
                titlesort=_sort_string(album_name),
                title=album_name,
                year=year or None,
                compilation=1 if compilation else 0,
            )
            session.add(album)
            await session.flush()
            album_by_key[key] = album
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
