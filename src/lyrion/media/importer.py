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

    source_path: Path = Path("/mnt/media2/Musik")
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
    "mp3", "flac", "ogg", "oga", "m4a", "aac", "aiff", "aif",
    "wav", "wma", "opus", "spx", "ape", "tak", "m4b", "mpc", "mp+",
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

            # Phase 1: metadata extraction (no DB)
            extracted: list[tuple[Path, Any]] = []
            for file_path in batch:
                self.stats.scanned_files += 1
                try:
                    info = await scanner.scan_single_file(file_path)
                    if info is not None:
                        extracted.append((file_path, info))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Extract failed for %s: %s", file_path, exc)
                    self.stats.error_files += 1

            # Phase 2: short-lived session, inserts only
            async with db_session() as session:
                for file_path, info in extracted:
                    try:
                        await self._import_track(session, file_path, info)
                        self.stats.imported_files += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Import failed for %s: %s", file_path, exc)
                        self.stats.error_files += 1
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

    async def _import_track(self, session, file_path: Path, info: Any) -> None:
        """Upsert one track + its album/contributors."""
        from lyrion.database.schema import Album, Contributor, Track

        url = _file_url(file_path)
        title = (info.title or file_path.stem) if hasattr(info, "title") else file_path.stem
        artist = (info.artist or "Unknown Artist") if hasattr(info, "artist") else "Unknown Artist"
        album_name = info.album or "Unknown Album" if hasattr(info, "album") else "Unknown Album"
        genre = info.genre or "" if hasattr(info, "genre") else ""
        year = info.year or 0 if hasattr(info, "year") else 0
        # duration is in milliseconds in the new ScanResult
        duration_ms = getattr(info, "duration", 0) or 0
        duration = duration_ms / 1000.0
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

        # Existing track?
        track = (
            await session.execute(select(Track).where(Track.url == url))
        ).scalar_one_or_none()

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
            await session.flush()  # get track.id
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

        # Album (create/get) — eager-load tracks to avoid sync lazy loads
        from sqlalchemy.orm import selectinload
        album = (
            await session.execute(
                select(Album)
                .options(selectinload(Album.tracks))
                .where(
                    Album.titlesort == _sort_string(album_name),
                    Album.year == (year or None),
                )
            )
        ).scalar_one_or_none()
        if album is None:
            album = Album(
                titlesort=_sort_string(album_name),
                title=album_name,
                year=year or None,
                compilation=1 if compilation else 0,
            )
            session.add(album)
            await session.flush()
        if track not in album.tracks:
            album.tracks.append(track)

        # Artist contributor (create/get) — eager-load to avoid sync lazy loads
        if artist and artist != "Unknown Artist":
            contrib = (
                await session.execute(
                    select(Contributor)
                    .options(
                        selectinload(Contributor.tracks),
                        selectinload(Contributor.albums),
                    )
                    .where(
                        Contributor.namespell == artist.strip().lower()
                    )
                )
            ).scalar_one_or_none()
            if contrib is None:
                contrib = Contributor(
                    namespell=artist.strip().lower(),
                    name=artist.strip(),
                    sortname=_sort_string(artist),
                )
                session.add(contrib)
                await session.flush()
            if track not in contrib.tracks:
                contrib.tracks.append(track)
            if album not in contrib.albums:
                contrib.albums.append(album)

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
